"""Migração 2026-07: re-chunka as planilhas (type:sheet) um-registro-por-chunk (por_linha).

CONTEXTO: as planilhas eram fatiadas por TAMANHO (~4000 chars), empacotando ~20 registros
(professores/setores) por chunk. Uma consulta a UM registro ("contato do professor X")
disputava dentro do blocão e o dado certo não chegava ao contexto. Medição (18/07/2026):
para a planilha de atendimento, o email do professor chegava ao contexto em só 5/25 casos
testados; com um chunk por linha, 25/25. O structure_sheet_text já produz uma frase por
registro, então o grão natural da planilha é a linha (mesmo padrão das grades de horário).

O QUE MUDA: cada planilha passa de N chunks grandes para M chunks (um por linha),
re-embedados. Conteúdo idêntico, só a granularidade e os embeddings mudam. Os ids seguem
determinísticos url#i (agora um por linha).

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY):
  python pipelines/migrations/migra_2026_07_sheets_por_linha.py backup     # salva os chunks atuais (com vetor)
  python pipelines/migrations/migra_2026_07_sheets_por_linha.py migrar     # re-chunka por linha (idempotente)
  python pipelines/migrations/migra_2026_07_sheets_por_linha.py verificar  # contagem + amostra de email no contexto
  python pipelines/migrations/migra_2026_07_sheets_por_linha.py reverter   # restaura os chunks do backup

O backup (id, vetor, metadata dos chunks originais) vai em backups/ (gitignored) e é o que
a reversão usa (o vetor dos chunks grandes não é recalculável, então precisa ser salvo).
"""

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from dotenv import load_dotenv

load_dotenv(RAIZ / ".env")
from upstash_vector import Index

from pipelines.chunker import chunk_document
from pipelines.config import google_client

index = Index(url=os.environ["UPSTASH_ENDPOINT"], token=os.environ["UPSTASH_WRITE_API_KEY"])
BACKUP = RAIZ / "backups" / "migracao_2026_07_sheets_por_linha.jsonl"


def _idx(cid):
    m = re.search(r"#(\d+)$", str(cid))
    return int(m.group(1)) if m else 0


def _chunks_por_url():
    # todos os chunks type:sheet, agrupados por URL, na ordem original (url#i crescente)
    por_url = defaultdict(list)
    cursor = ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True, include_vectors=True)
        for v in res.vectors:
            m = v.metadata or {}
            if m.get("type") == "sheet":
                por_url[m.get("source_url")].append({"id": str(v.id), "vector": v.vector, "metadata": m})
        cursor = res.next_cursor
        if cursor == "":
            break
    for cs in por_url.values():
        cs.sort(key=lambda c: _idx(c["id"]))
    return por_url


def _embed(textos):
    out = []
    for k in range(0, len(textos), 100):
        r = google_client.models.embed_content(model="gemini-embedding-001", contents=textos[k:k + 100])
        out += [e.values for e in r.embeddings]
    return out


def _meta_base(metadata):
    # metadata comum aos chunks de uma planilha (sem o text, que muda por linha)
    return {k: metadata.get(k) for k in ("source_url", "title", "type", "published_at",
                                         "source_hash", "campus_scope") if metadata.get(k) is not None}


def backup():
    por_url = _chunks_por_url()
    total = sum(len(cs) for cs in por_url.values())
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    with open(BACKUP, "w", encoding="utf-8") as f:
        for url, cs in por_url.items():
            for c in cs:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"backup: {total} chunks de {len(por_url)} planilhas -> {BACKUP}")


def _carregar_backup():
    assert BACKUP.exists(), "rode primeiro o modo 'backup'"
    por_url = defaultdict(list)
    for l in open(BACKUP, encoding="utf-8"):
        if l.strip():
            c = json.loads(l)
            por_url[c["metadata"].get("source_url")].append(c)
    for cs in por_url.values():
        cs.sort(key=lambda c: _idx(c["id"]))
    return por_url


def migrar():
    # idempotente: reconstroi o texto de cada planilha do backup, re-chunka por linha, e
    # substitui os chunks da URL (deleta os ids antigos E os novos calculados, depois upserta).
    por_url = _carregar_backup()
    total_novos = 0
    for j, (url, cs) in enumerate(sorted(por_url.items()), 1):
        texto = "\n".join(c["metadata"].get("text", "") for c in cs)
        novos = chunk_document(texto, _meta_base(cs[0]["metadata"]), por_linha=True)
        for i, c in enumerate(novos):
            c["id"] = f"{url}#{i}"
        # apaga o que existir da URL: ids antigos (backup) + ids novos (recalculados), cobrindo
        # execucao interrompida no meio; depois upserta os por-linha
        ids_apagar = list({c["id"] for c in cs} | {c["id"] for c in novos})
        for k in range(0, len(ids_apagar), 500):
            index.delete(ids=ids_apagar[k:k + 500])
        vetores = _embed([c["text"] for c in novos])
        vectors = [(c["id"], vec, {**_meta_base(cs[0]["metadata"]), "text": c["text"]})
                   for c, vec in zip(novos, vetores)]
        for k in range(0, len(vectors), 100):
            index.upsert(vectors=vectors[k:k + 100])
        total_novos += len(vectors)
        print(f"  [{j}/{len(por_url)}] {len(cs)} -> {len(vectors)} chunks | {url[:70]}")
    print(f"migração: {total_novos} chunks por-linha em {len(por_url)} planilhas")


def verificar():
    por_url = _chunks_por_url()
    n_sheet = sum(len(cs) for cs in por_url.values())
    # apos a migracao cada chunk deve ser uma unica linha curta (sem \n interno, poucos chars)
    multi = sum(1 for cs in por_url.values() for c in cs if "\n" in c["metadata"].get("text", ""))
    print(f"planilhas: {len(por_url)} URLs | {n_sheet} chunks | chunks com >1 linha (esperado 0): {multi}")
    # amostra: o email de um professor chega ao contexto de 'contato X'?
    import rag.chain as chain
    for nome, email in [("Gustavo", "gustavo.neuberger@canoas.ifrs.edu.br"),
                        ("Sandro", "sandro.silva@canoas.ifrs.edu.br"),
                        ("Adriana", "adriana.braun@canoas.ifrs.edu.br")]:
        hits = chain.rerank_by_date(chain.search(f"contato {nome}", top_k=chain.FETCH_K))
        _, filtered, _, _ = chain.build_context(hits)
        ctx = re.sub(r"\s+", "", " ".join(h.metadata.get("text", "") for h in filtered)).lower()
        print(f"  contato {nome}: email no contexto = {email.replace(' ','') in ctx}")


def reverter():
    por_url = _carregar_backup()
    total = 0
    for url, cs in sorted(por_url.items()):
        # apaga os chunks por-linha atuais da URL (ids url#0..#M) e restaura os do backup
        atuais = _chunks_por_url().get(url, [])
        ids_apagar = list({c["id"] for c in atuais} | {c["id"] for c in cs})
        for k in range(0, len(ids_apagar), 500):
            index.delete(ids=ids_apagar[k:k + 500])
        vectors = [(c["id"], c["vector"], c["metadata"]) for c in cs]
        for k in range(0, len(vectors), 100):
            index.upsert(vectors=vectors[k:k + 100])
        total += len(vectors)
    print(f"reversão: {total} chunks originais restaurados")


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    if modo not in ("backup", "migrar", "verificar", "reverter"):
        print(__doc__)
        sys.exit(1)
    {"backup": backup, "migrar": migrar, "verificar": verificar, "reverter": reverter}[modo]()
