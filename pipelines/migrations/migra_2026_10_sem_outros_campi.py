"""Migração 2026-10: apaga da base os chunks de outro campus gravados antes do corte na ingestão.

Até 19/10/2026 a seção de outro campus de um edital multi-campus e a linha de vaga de outro campus de um
quadro de vagas entravam na base marcadas campus_scope "outro", só para o filtro de campus tirá-las de toda
busca. Desde então a ingestão as corta (`sem_outros_campi`, em pipelines/chunker.py); esta migração apaga as
que já estavam gravadas. Ficam os chunks "outro" de documento institucional (o PDI) e de página multi-campus,
que seguem outra regra. URL que fica sem nenhum chunk volta como nova no run seguinte e é relida pelo
código novo, que grava só o que é de Canoas ou sem campus.

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY):
  python -m pipelines.migrations.migra_2026_10_sem_outros_campi backup     # salva vetor e metadata dos chunks de outro campus (sem escrita)
  python -m pipelines.migrations.migra_2026_10_sem_outros_campi aplicar    # apaga os chunks salvos no backup
  python -m pipelines.migrations.migra_2026_10_sem_outros_campi verificar
  python -m pipelines.migrations.migra_2026_10_sem_outros_campi reverter   # regrava os chunks do backup
"""

import json
import os
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from dotenv import load_dotenv

load_dotenv(RAIZ / ".env")

from pipelines.chunker import e_canoas
from pipelines.config import index

BACKUP = RAIZ / "backups" / "migracao_2026_10_sem_outros_campi.json"

# rotulo da secao de campus de um edital e linha estruturada de um quadro de vagas
_SECAO = re.compile(r"\[Edital, seção do Campus ([^\]]+)\]")
_VAGA = re.compile(r"Campus (.+?) - .+?\(.*?\):\s*\d+\s*vagas", re.IGNORECASE)


def _campus_do_chunk(texto):
    # campus do rotulo de secao ou da linha de vaga; None quando o chunk nao e de secao nem de vaga
    achado = _SECAO.match(texto) or _VAGA.match(texto)
    return achado.group(1).strip() if achado else None


def _varrer():
    # (ids dos chunks de outro campus, total de chunks por URL, chunks de outro campus por URL)
    ids, por_url, outros = [], {}, {}
    cursor = ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for v in res.vectors:
            md = v.metadata or {}
            url = md.get("source_url", "")
            por_url[url] = por_url.get(url, 0) + 1
            campus = _campus_do_chunk(md.get("text") or "") if md.get("campus_scope") == "outro" else None
            if campus and not e_canoas(campus):
                ids.append(v.id)
                outros[url] = outros.get(url, 0) + 1
        cursor = res.next_cursor
        if cursor == "":
            break
    return ids, por_url, outros


def backup():
    ids, por_url, outros = _varrer()
    salvos = []
    for i in range(0, len(ids), 100):
        salvos += [{"id": v.id, "vector": list(v.vector), "metadata": v.metadata}
                   for v in index.fetch(ids=ids[i:i + 100], include_vectors=True, include_metadata=True) if v]
    BACKUP.parent.mkdir(exist_ok=True)
    BACKUP.write_text(json.dumps(salvos, ensure_ascii=False), encoding="utf-8")
    esvaziadas = [url for url, n in outros.items() if n == por_url[url]]
    print(f"backup: {len(salvos)} chunks de outro campus em {len(outros)} documentos -> {BACKUP}")
    print(f"  documentos que ficam sem chunk e serao relidos no run seguinte: {len(esvaziadas)}")


def aplicar():
    salvos = json.loads(BACKUP.read_text(encoding="utf-8"))
    ids = [s["id"] for s in salvos]
    for i in range(0, len(ids), 1000):
        index.delete(ids=ids[i:i + 1000])
    print(f"aplicado: {len(ids)} chunks apagados")


def verificar():
    ids, _, outros = _varrer()
    print(f"chunks de outro campus na base: {len(ids)} em {len(outros)} documentos")


def reverter():
    salvos = json.loads(BACKUP.read_text(encoding="utf-8"))
    for i in range(0, len(salvos), 100):
        index.upsert(vectors=[(s["id"], s["vector"], s["metadata"]) for s in salvos[i:i + 100]])
    print(f"revertido: {len(salvos)} chunks regravados")


if __name__ == "__main__":
    modos = {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}
    if len(sys.argv) != 2 or sys.argv[1] not in modos:
        sys.exit(f"uso: python -m pipelines.migrations.migra_2026_10_sem_outros_campi {{{'|'.join(modos)}}}")
    modos[sys.argv[1]]()
