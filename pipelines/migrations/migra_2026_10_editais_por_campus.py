"""Migração 2026-10: re-fatia por seção de campus os editais de ingresso multi-campus.

Nos editais do portal de ingresso, o registro de vagas de cada curso é longo (distribuição por
cota) e o fatiamento por tamanho deixava os pedaços de continuação SEM campus e SEM curso
("vagas; C5: ... - 2 vagas"). Numa pergunta de vagas eles enchiam o contexto e o modelo atribuía a
Canoas cursos de outros campi. O chunker passou a cortar o documento nas seções de campus antes de
fatiar (`secoes_de_campus` em pipelines/chunker.py): cada chunk leva o nome do campus da sua seção
e o campus_scope dela, e o cap tira do contexto o que não é de Canoas. Esta migração aplica isso aos
editais que já estão na base (o incremental só refaria quando o arquivo mudasse), com backup completo.

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY):
  python -m pipelines.migrations.migra_2026_10_editais_por_campus backup     # salva os chunks atuais e re-fatia (mostra o comparativo)
  python -m pipelines.migrations.migra_2026_10_editais_por_campus aplicar
  python -m pipelines.migrations.migra_2026_10_editais_por_campus verificar
  python -m pipelines.migrations.migra_2026_10_editais_por_campus reverter
"""

import collections
import json
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from dotenv import load_dotenv

load_dotenv(RAIZ / ".env")

from pipelines.chunker import chunks_de_pdf, secoes_de_campus
from pipelines.config import fetch, index
from pipelines.hashing import source_hash
from pipelines.ingest import ingest_chunks
from pipelines.parser_pdf import parse_pdf

BACKUP = RAIZ / "backups" / "migracao_2026_10_editais_por_campus.json"
NOVOS = RAIZ / "backups" / "migracao_2026_10_editais_por_campus_novos.json"


def _pdfs_na_base():
    # chunks de PDF fora do site de Canoas, com vetor, agrupados pela URL de origem
    docs, cursor = {}, ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True, include_vectors=True)
        for v in res.vectors:
            md = v.metadata or {}
            url = md.get("source_url", "") or ""
            if md.get("type") == "pdf" and "/canoas/" not in url:
                docs.setdefault(url, []).append({"id": v.id, "vector": list(v.vector), "metadata": md})
        cursor = res.next_cursor
        if cursor == "":
            break
    return docs


def _ordem(chunk):
    return int(chunk["id"].rsplit("#", 1)[1]) if "#" in chunk["id"] else 0


def backup():
    # alvo: PDFs cujo texto na base tem secoes de pelo menos 2 campi (mesma regra do chunker)
    docs = _pdfs_na_base()
    alvo = {u: cs for u, cs in docs.items()
            if secoes_de_campus("\n".join(c["metadata"].get("text", "") for c in sorted(cs, key=_ordem)))}
    BACKUP.parent.mkdir(exist_ok=True)
    BACKUP.write_text(json.dumps(alvo, ensure_ascii=False), encoding="utf-8")
    print(f"backup: {sum(len(v) for v in alvo.values())} chunks de {len(alvo)} PDFs -> {BACKUP}")

    # re-parse do arquivo atual e re-fatiamento por secao de campus
    novos = {}
    for url, antigos in alvo.items():
        conteudo = fetch(url, timeout=60).content
        res = parse_pdf({"url": url, "size_kb": len(conteudo) // 1024, "parent": ""}, conteudo)
        if not res or not res.get("text"):
            print(f"  FALHOU o parse, PDF mantido: {url}")
            continue
        res["published_at"] = antigos[0]["metadata"].get("published_at")
        res["source_hash"] = source_hash(conteudo)
        chunks = chunks_de_pdf(res)
        novos[url] = chunks
        escopos = collections.Counter(c.get("campus_scope") for c in chunks)
        print(f"  {url[-70:]}: base {len(antigos)} chunks -> {len(chunks)} {dict(escopos)}")
    NOVOS.write_text(json.dumps(novos, ensure_ascii=False), encoding="utf-8")
    print(f"re-fatiamento de {len(novos)} PDFs -> {NOVOS}")


def aplicar():
    alvo = json.loads(BACKUP.read_text(encoding="utf-8"))
    novos = json.loads(NOVOS.read_text(encoding="utf-8"))
    todos = []
    for url, chunks in novos.items():
        for i, c in enumerate(chunks):
            c["id"] = f"{url}#{i}"
        # so troca o PDF que produziu chunk novo (nunca deleta sem repor)
        if chunks:
            index.delete(ids=[c["id"] for c in alvo[url]])
            todos.extend(chunks)
    ingest_chunks(todos)
    print(f"aplicado: {len(todos)} chunks novos em {len(novos)} PDFs")


def verificar():
    novos = json.loads(NOVOS.read_text(encoding="utf-8"))
    docs = _pdfs_na_base()
    for url in novos:
        cs = docs.get(url, [])
        escopos = collections.Counter(c["metadata"].get("campus_scope") for c in cs)
        rotulados = sum(1 for c in cs if c["metadata"].get("text", "").startswith("[Edital, seção do Campus"))
        print(f"  {url[-70:]}: {len(cs)} chunks, {dict(escopos)}, {rotulados} com rótulo de campus")


def reverter():
    alvo = json.loads(BACKUP.read_text(encoding="utf-8"))
    docs = _pdfs_na_base()
    for url in alvo:
        if url in docs:
            index.delete(ids=[c["id"] for c in docs[url]])
    vetores = [(c["id"], c["vector"], c["metadata"]) for cs in alvo.values() for c in cs]
    for i in range(0, len(vetores), 100):
        index.upsert(vectors=vetores[i:i + 100])
    print(f"revertido: {len(vetores)} chunks restaurados de {len(alvo)} PDFs")


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}.get(
        modo, lambda: sys.exit(__doc__))()
