"""Migração 2026-07: unifica os ids da base Upstash no formato determinístico url#i.

CONTEXTO: a base tinha DUAS convenções de id convivendo: url#i (determinístico, gravado
pelo ingest atual, com replace incremental por URL) e ids numéricos legados ("8836", da
ingestão original, anterior ao id determinístico). Medição em 18/07/2026: 11.633 chunks,
4.966 legados em 1.519 URLs, ZERO URLs mistas (nenhuma URL com os dois formatos, então a
renumeração não colide). Dois formatos obrigam todo consumidor a tratar os dois casos e
quebram a premissa "id = url#i" em silêncio.

O QUE MUDA: só a IDENTIDADE. O vetor e o metadata de cada chunk ficam intactos (fetch com
vetor -> upsert sob o id novo -> delete do antigo; zero re-embed, zero LLM). A ordem dos
chunks de uma URL segue o id numérico legado crescente (a ordem de inserção original, que
é a ordem do documento).

EFEITO COLATERAL CONHECIDO: traces antigos da telemetria guardam os ids legados; o
harvest (index.fetch por id) não os encontra mais depois da migração. Aceito: o harvest
serve à curadoria de perguntas recentes.

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY no .env):
  python pipelines/migrations/migra_2026_07_ids_legados.py backup     # grava o mapeamento (obrigatório)
  python pipelines/migrations/migra_2026_07_ids_legados.py migrar     # idempotente/retomável
  python pipelines/migrations/migra_2026_07_ids_legados.py verificar  # 0 legados + amostra íntegra
  python pipelines/migrations/migra_2026_07_ids_legados.py reverter   # desfaz pelo mapeamento

O backup (mapeamento old_id -> new_id + metadata) vai em backups/ (gitignored). A
reversão não precisa dos vetores salvos: o vetor não muda, então reverter = fetch do id
novo -> upsert sob o id antigo -> delete do novo.
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

index = Index(url=os.environ["UPSTASH_ENDPOINT"], token=os.environ["UPSTASH_WRITE_API_KEY"])

BACKUP = RAIZ / "backups" / "migracao_2026_07_ids_legados.jsonl"
PADRAO_URL_I = re.compile(r".+#\d+$")


def _varrer_legados():
    # varre a base inteira e devolve os chunks cujo id NAO segue o formato url#i
    legados = []
    cursor = ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for v in res.vectors:
            if not PADRAO_URL_I.match(str(v.id)):
                legados.append({"id": str(v.id), "metadata": v.metadata or {}})
        cursor = res.next_cursor
        if cursor == "":
            break
    return legados


def _ordem_legada(id_str):
    # ordena os chunks de uma URL pela ordem de inserção original (id numérico crescente);
    # id não-numérico (não esperado) vai ao fim, em ordem lexicográfica estável
    return (0, int(id_str)) if id_str.isdigit() else (1, id_str)


def backup():
    legados = _varrer_legados()
    por_url = defaultdict(list)
    for c in legados:
        por_url[c["metadata"].get("source_url", "")].append(c)
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(BACKUP, "w", encoding="utf-8") as f:
        for url, cs in por_url.items():
            cs.sort(key=lambda c: _ordem_legada(c["id"]))
            for i, c in enumerate(cs):
                f.write(json.dumps({"old_id": c["id"], "new_id": f"{url}#{i}",
                                    "url": url, "metadata": c["metadata"]},
                                   ensure_ascii=False) + "\n")
                n += 1
    print(f"backup: {n} chunks legados de {len(por_url)} URLs -> {BACKUP}")


def _carregar_mapa():
    assert BACKUP.exists(), "rode primeiro o modo 'backup'"
    return [json.loads(l) for l in open(BACKUP, encoding="utf-8") if l.strip()]


def _fetch_com_vetor(ids):
    # fetch em lotes com vetor incluso; devolve {id: vector_object}
    out = {}
    for k in range(0, len(ids), 100):
        for v in index.fetch(ids=ids[k:k + 100], include_vectors=True, include_metadata=True):
            if v is not None:
                out[str(v.id)] = v
    return out


def migrar():
    # lotes GLOBAIS (nao por URL): ~160 requests em vez de ~4600. a ordem e sempre
    # upsert do id novo ANTES do delete do antigo: interrupcao no meio deixa duplicata
    # transitoria (inocua), nunca perda; re-rodar pula o que ja migrou (idempotente).
    mapa = _carregar_mapa()
    antigos = _fetch_com_vetor([m["old_id"] for m in mapa])
    pendentes = [m for m in mapa if m["old_id"] in antigos]
    print(f"pendentes: {len(pendentes)} de {len(mapa)} (o resto ja migrado)")
    vectors = [(m["new_id"], antigos[m["old_id"]].vector, antigos[m["old_id"]].metadata or {})
               for m in pendentes]
    for k in range(0, len(vectors), 50):
        index.upsert(vectors=vectors[k:k + 50])
        if (k // 50) % 20 == 0:
            print(f"  upsert {min(k + 50, len(vectors))}/{len(vectors)}")
    ids_antigos = [m["old_id"] for m in pendentes]
    for k in range(0, len(ids_antigos), 500):
        index.delete(ids=ids_antigos[k:k + 500])
    print(f"migração: {len(vectors)} chunks migrados nesta execução")


def verificar():
    legados = _varrer_legados()
    print(f"ids legados restantes: {len(legados)} (esperado 0)")
    mapa = _carregar_mapa()
    total = 0
    cursor = ""
    while True:
        res = index.range(cursor=cursor, limit=1000)
        total += len(res.vectors)
        cursor = res.next_cursor
        if cursor == "":
            break
    print(f"total de chunks na base: {total} (esperado: o mesmo de antes da migração)")
    # amostra: o texto sob o id novo tem que ser o MESMO texto do backup
    amostra = mapa[:: max(1, len(mapa) // 20)][:20]
    novos = _fetch_com_vetor([m["new_id"] for m in amostra])
    ok = sum(1 for m in amostra
             if (novos.get(m["new_id"]) is not None
                 and (novos[m["new_id"]].metadata or {}).get("text") == m["metadata"].get("text")))
    print(f"amostra de integridade: {ok}/{len(amostra)} textos idênticos ao backup")


def reverter():
    mapa = _carregar_mapa()
    por_url = defaultdict(list)
    for m in mapa:
        por_url[m["url"]].append(m)
    total_rev = 0
    for url, ms in sorted(por_url.items()):
        novos = _fetch_com_vetor([m["new_id"] for m in ms])
        vectors = [(m["old_id"], novos[m["new_id"]].vector, novos[m["new_id"]].metadata or {})
                   for m in ms if m["new_id"] in novos]
        if not vectors:
            continue
        for k in range(0, len(vectors), 50):
            index.upsert(vectors=vectors[k:k + 50])
        presentes = [m["new_id"] for m in ms if m["new_id"] in novos]
        for k in range(0, len(presentes), 500):
            index.delete(ids=presentes[k:k + 500])
        total_rev += len(vectors)
    print(f"reversão: {total_rev} chunks de volta ao id legado")


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    if modo not in ("backup", "migrar", "verificar", "reverter"):
        print(__doc__)
        sys.exit(1)
    {"backup": backup, "migrar": migrar, "verificar": verificar, "reverter": reverter}[modo]()
