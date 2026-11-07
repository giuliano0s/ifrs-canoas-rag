"""Migração 2026-11: etiqueta de curso de cada trecho da base pela fase pipelines/cursos.py.

Até 07/11/2026 o curso vinha só do classify_curso_escopo no chunker: regex sobre título e cabeçalho, só em
documento definidor (PPC, matriz, regulamento), com um espaço simples entre as palavras. Numa amostra de 341
documentos rotulados ele achava 34% dos documentos de curso; o PPC da Engenharia Eletrônica de 2021, com
quebras de linha no título, entrava no contexto de outros cursos sem identificação. A fase nova (regras
objetivas, classificador por LLM, etiqueta por trecho nas seções) roda em toda ingestão; esta migração faz a
primeira passada sobre a base inteira, com backup das etiquetas de antes. Só o metadata de curso muda
(curso_escopo, cursos, curso_fonte); vetor e texto ficam intactos.

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY e OPENAI_API_KEY):
  python -m pipelines.migrations.migra_2026_11_curso_por_trecho backup     # salva as etiquetas de curso de todos os trechos (sem escrita)
  python -m pipelines.migrations.migra_2026_11_curso_por_trecho aplicar    # roda a fase de curso sem prazo e grava
  python -m pipelines.migrations.migra_2026_11_curso_por_trecho verificar
  python -m pipelines.migrations.migra_2026_11_curso_por_trecho reverter   # devolve as etiquetas do backup
"""

import json
import os
import sys
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from dotenv import load_dotenv

load_dotenv(RAIZ / ".env")

from pipelines.config import index
from pipelines.cursos import CAMPOS, _aplicar, carregar_base, run_cursos

BACKUP = RAIZ / "backups" / "migracao_2026_11_curso_por_trecho.json"


def _etiquetas():
    # {id: {campo: valor}} das etiquetas de curso gravadas hoje, em todos os trechos
    return {trecho["id"]: {campo: trecho.get(campo) for campo in CAMPOS}
            for trechos in carregar_base().values() for trecho in trechos}


def backup():
    # o backup de uma migracao ja aplicada e o unico jeito de reverte-la: nunca e sobrescrito
    if BACKUP.exists():
        sys.exit(f"backup já existe em {BACKUP}; apague-o à mão só se a migração não foi aplicada")
    etiquetas = _etiquetas()
    BACKUP.parent.mkdir(exist_ok=True)
    BACKUP.write_text(json.dumps(etiquetas, ensure_ascii=False), encoding="utf-8")
    print(f"backup: {len(etiquetas)} trechos, {sum(1 for e in etiquetas.values() if e['curso_escopo'])} com curso -> {BACKUP}")


def aplicar():
    if not BACKUP.exists():
        sys.exit("rode o backup antes")
    run_cursos(prazo_min=None)


def verificar():
    etiquetas = _etiquetas()
    print("trechos:", len(etiquetas))
    print("com curso_escopo, por fonte:", dict(Counter(e["curso_fonte"] for e in etiquetas.values() if e["curso_escopo"])))
    print("com a lista cursos:", sum(1 for e in etiquetas.values() if e["cursos"]))
    print("curso_escopo sem fonte (etiqueta antiga, documento pendente ou fora do escopo):",
          sum(1 for e in etiquetas.values() if e["curso_escopo"] and not e["curso_fonte"]))
    print("por curso:", dict(Counter(e["curso_escopo"] for e in etiquetas.values() if e["curso_escopo"]).most_common()))


def reverter():
    antes = json.loads(BACKUP.read_text(encoding="utf-8"))
    agora = _etiquetas()
    mudancas = [(id_, antes[id_]) for id_, etiqueta in agora.items() if id_ in antes and etiqueta != antes[id_]]
    print(f"revertidos: {_aplicar(mudancas)} de {len(mudancas)} trechos")


if __name__ == "__main__":
    modos = {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}
    if len(sys.argv) != 2 or sys.argv[1] not in modos:
        sys.exit(__doc__)
    modos[sys.argv[1]]()
