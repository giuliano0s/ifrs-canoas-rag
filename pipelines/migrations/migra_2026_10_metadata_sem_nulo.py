"""Migração 2026-10: remove do metadata da base os campos gravados como nulo.

O filtro de metadata do Upstash não seleciona nulo: `campus_scope != 'outro'` descarta o chunk com
campus_scope nulo, e não há sintaxe para casar nulo. O atendimento passa a tirar o institucional/outro
campus já na busca (`CAMPUS_FILTRO`, em rag/chain.py), o que exige o chunk de Canoas SEM o campo. Na
base havia 11.866 chunks com campus_scope nulo, 4.383 sem o campo e 2.355 "outro". Só o metadata muda
(update OVERWRITE sem os campos nulos); vetor e id ficam intactos.

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY):
  python -m pipelines.migrations.migra_2026_10_metadata_sem_nulo backup     # salva o metadata dos chunks com campo nulo (sem escrita)
  python -m pipelines.migrations.migra_2026_10_metadata_sem_nulo aplicar    # regrava o metadata sem os campos nulos
  python -m pipelines.migrations.migra_2026_10_metadata_sem_nulo verificar
  python -m pipelines.migrations.migra_2026_10_metadata_sem_nulo reverter   # regrava o metadata salvo no backup
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from dotenv import load_dotenv

load_dotenv(RAIZ / ".env")

from upstash_vector.types import MetadataUpdateMode

from pipelines.config import index

BACKUP = RAIZ / "backups" / "migracao_2026_10_metadata_sem_nulo.json"


def _com_nulo():
    # {id: metadata} dos chunks com pelo menos um campo nulo
    achados, cursor = {}, ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for v in res.vectors:
            md = v.metadata or {}
            if any(valor is None for valor in md.values()):
                achados[v.id] = md
        cursor = res.next_cursor
        if cursor == "":
            break
    return achados


def _regravar(itens):
    # update OVERWRITE de metadata, em paralelo; devolve os ids que falharam
    def um(par):
        chunk_id, md = par
        try:
            return None if index.update(id=chunk_id, metadata=md, metadata_update_mode=MetadataUpdateMode.OVERWRITE) else chunk_id
        except Exception:
            return chunk_id
    with ThreadPoolExecutor(8) as ex:
        return [i for i in ex.map(um, itens) if i]


def backup():
    achados = _com_nulo()
    BACKUP.parent.mkdir(exist_ok=True)
    BACKUP.write_text(json.dumps(achados, ensure_ascii=False), encoding="utf-8")
    campos = {}
    for md in achados.values():
        for chave, valor in md.items():
            if valor is None:
                campos[chave] = campos.get(chave, 0) + 1
    print(f"backup: {len(achados)} chunks com campo nulo -> {BACKUP}")
    print(f"  campos nulos: {campos}")


def aplicar():
    achados = json.loads(BACKUP.read_text(encoding="utf-8"))
    limpos = [(i, {chave: valor for chave, valor in md.items() if valor is not None}) for i, md in achados.items()]
    falhas = _regravar(limpos)
    print(f"aplicado: {len(limpos) - len(falhas)} de {len(limpos)} chunks sem campo nulo; falhas: {len(falhas)}")
    if falhas:
        print("  rode aplicar de novo para refazer as falhas:", falhas[:5])


def verificar():
    restantes = _com_nulo()
    print(f"chunks com campo nulo na base: {len(restantes)}")


def reverter():
    achados = json.loads(BACKUP.read_text(encoding="utf-8"))
    falhas = _regravar(list(achados.items()))
    print(f"revertido: {len(achados) - len(falhas)} de {len(achados)} chunks; falhas: {len(falhas)}")


if __name__ == "__main__":
    modos = {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}
    if len(sys.argv) != 2 or sys.argv[1] not in modos:
        sys.exit(f"uso: python -m pipelines.migrations.migra_2026_10_metadata_sem_nulo {{{'|'.join(modos)}}}")
    modos[sys.argv[1]]()
