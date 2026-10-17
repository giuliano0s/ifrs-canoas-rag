"""Migração 2026-10: apaga da base a cópia de um recurso gravada sob um endereço com parâmetro de rastreio.

Um link do site com `?utm_source=chatgpt.com` levou o crawler a gravar o cronograma do ingresso 2026/1 duas
vezes, com e sem o parâmetro, e as duas cópias ocupavam vagas no contexto. O crawler passou a tirar os
parâmetros de rastreio (`canonical_url`, em pipelines/urls.py) dos links que segue; esta migração apaga as
cópias já gravadas cujo endereço sem rastreio também está na base. Cópia sem o par na base fica e é listada.

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY):
  python -m pipelines.migrations.migra_2026_10_url_sem_rastreio backup     # salva vetor e metadata das cópias (sem escrita)
  python -m pipelines.migrations.migra_2026_10_url_sem_rastreio aplicar    # apaga as cópias salvas no backup
  python -m pipelines.migrations.migra_2026_10_url_sem_rastreio verificar
  python -m pipelines.migrations.migra_2026_10_url_sem_rastreio reverter   # regrava as cópias do backup
"""

import json
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from dotenv import load_dotenv

load_dotenv(RAIZ / ".env")

from pipelines.config import index
from pipelines.urls import canonical_url

BACKUP = RAIZ / "backups" / "migracao_2026_10_url_sem_rastreio.json"


def _copias():
    # (ids das cópias com par sem rastreio na base, endereços com rastreio sem o par)
    ids_por_url, cursor = {}, ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for v in res.vectors:
            ids_por_url.setdefault((v.metadata or {}).get("source_url", ""), []).append(v.id)
        cursor = res.next_cursor
        if cursor == "":
            break
    rastreadas = [url for url in ids_por_url if canonical_url(url) != url]
    copias = [i for url in rastreadas if canonical_url(url) in ids_por_url for i in ids_por_url[url]]
    sem_par = [url for url in rastreadas if canonical_url(url) not in ids_por_url]
    return copias, sem_par


def backup():
    copias, sem_par = _copias()
    salvos = [{"id": v.id, "vector": list(v.vector), "metadata": v.metadata}
              for v in index.fetch(ids=copias, include_vectors=True, include_metadata=True) if v]
    BACKUP.parent.mkdir(exist_ok=True)
    BACKUP.write_text(json.dumps(salvos, ensure_ascii=False), encoding="utf-8")
    print(f"backup: {len(salvos)} chunks de cópias com rastreio -> {BACKUP}")
    for url in sorted({s['metadata']['source_url'] for s in salvos}):
        print(f"  {url}")
    if sem_par:
        print(f"  com rastreio e sem o endereço limpo na base (ficam): {sem_par}")


def aplicar():
    salvos = json.loads(BACKUP.read_text(encoding="utf-8"))
    if salvos:
        index.delete(ids=[s["id"] for s in salvos])
    print(f"aplicado: {len(salvos)} chunks apagados")


def verificar():
    copias, sem_par = _copias()
    print(f"cópias com rastreio e par na base: {len(copias)} chunks | com rastreio sem o par: {len(sem_par)}")


def reverter():
    salvos = json.loads(BACKUP.read_text(encoding="utf-8"))
    if salvos:
        index.upsert(vectors=[(s["id"], s["vector"], s["metadata"]) for s in salvos])
    print(f"revertido: {len(salvos)} chunks regravados")


if __name__ == "__main__":
    modos = {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}
    if len(sys.argv) != 2 or sys.argv[1] not in modos:
        sys.exit(f"uso: python -m pipelines.migrations.migra_2026_10_url_sem_rastreio {{{'|'.join(modos)}}}")
    modos[sys.argv[1]]()
