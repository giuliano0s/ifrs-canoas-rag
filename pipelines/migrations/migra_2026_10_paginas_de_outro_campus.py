"""Migração 2026-10: apaga da base as páginas do portal de ingresso que pertencem a outro campus.

O portal de ingresso tem uma página por curso e campus ("Curso oferecido no Campus: Farroupilha"), uma por
campus (/2025/campus/erechim/) e as de chamada ("1ª CHAMADA | Prova | Campus Osório"). Até 29/10/2026 o crawler
gravava todas: a de curso entrava sem campus_scope e disputava a busca com as de Canoas (362 das 386 páginas de
curso eram de outro campus). Desde então o crawler não as grava (`campus_da_pagina`, em pipelines/chunker.py);
esta migração apaga as que já estavam gravadas, inclusive as marcadas "outro", que a busca já excluía, para a
base ficar igual ao que a ingestão grava hoje. Aplicada em 29/10/2026 (535 chunks em 534 páginas).

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY):
  python -m pipelines.migrations.migra_2026_10_paginas_de_outro_campus backup     # salva vetor e metadata das páginas de outro campus (sem escrita)
  python -m pipelines.migrations.migra_2026_10_paginas_de_outro_campus aplicar    # apaga os chunks salvos no backup
  python -m pipelines.migrations.migra_2026_10_paginas_de_outro_campus verificar
  python -m pipelines.migrations.migra_2026_10_paginas_de_outro_campus reverter   # regrava os chunks do backup
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

from pipelines.chunker import campus_da_pagina, e_canoas
from pipelines.config import index

BACKUP = RAIZ / "backups" / "migracao_2026_10_paginas_de_outro_campus.json"


def _varrer():
    # {url: {"ids": [...], "ocultos": n}} das páginas do portal que pertencem a outro campus
    paginas = {}
    cursor = ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for vetor in res.vectors:
            meta = vetor.metadata or {}
            pagina = paginas.setdefault(meta.get("source_url", ""), {"titulo": meta.get("title") or "", "textos": [],
                                                                      "ids": [], "ocultos": 0})
            pagina["textos"].append(meta.get("text") or "")
            pagina["ids"].append(vetor.id)
            pagina["ocultos"] += meta.get("campus_scope") == "outro"
        cursor = res.next_cursor
        if cursor == "":
            break

    # campus pela estrutura do portal, sobre o texto inteiro da página
    outras = {}
    for url, pagina in paginas.items():
        campus = campus_da_pagina(url, pagina["titulo"], "\n".join(pagina["textos"]))
        if campus and not e_canoas(campus):
            outras[url] = {"ids": pagina["ids"], "ocultos": pagina["ocultos"]}
    return outras


def backup():
    # o backup de uma migracao ja aplicada e o unico jeito de reverte-la: nunca e sobrescrito
    if BACKUP.exists():
        sys.exit(f"backup já existe em {BACKUP}; apague-o à mão só se a migração não foi aplicada")
    outras = _varrer()
    ids = [identificador for pagina in outras.values() for identificador in pagina["ids"]]
    salvos = []
    for inicio in range(0, len(ids), 100):
        salvos += [{"id": vetor.id, "vector": list(vetor.vector), "metadata": vetor.metadata}
                   for vetor in index.fetch(ids=ids[inicio:inicio + 100], include_vectors=True, include_metadata=True)
                   if vetor]
    BACKUP.parent.mkdir(exist_ok=True)
    BACKUP.write_text(json.dumps(salvos, ensure_ascii=False), encoding="utf-8")
    visiveis = [url for url, pagina in outras.items() if not pagina["ocultos"]]
    print(f"backup: {len(salvos)} chunks em {len(outras)} páginas de outro campus -> {BACKUP}")
    print(f"  visíveis na busca: {len(visiveis)} páginas; já marcadas 'outro': {len(outras) - len(visiveis)}")


def aplicar():
    salvos = json.loads(BACKUP.read_text(encoding="utf-8"))
    ids = [salvo["id"] for salvo in salvos]
    for inicio in range(0, len(ids), 1000):
        index.delete(ids=ids[inicio:inicio + 1000])
    print(f"aplicado: {len(ids)} chunks apagados")


def verificar():
    outras = _varrer()
    print(f"páginas de outro campus na base: {len(outras)}, {sum(len(p['ids']) for p in outras.values())} chunks")


def reverter():
    salvos = json.loads(BACKUP.read_text(encoding="utf-8"))
    for inicio in range(0, len(salvos), 100):
        index.upsert(vectors=[(salvo["id"], salvo["vector"], salvo["metadata"]) for salvo in salvos[inicio:inicio + 100]])
    print(f"revertido: {len(salvos)} chunks regravados")


if __name__ == "__main__":
    modos = {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}
    if len(sys.argv) != 2 or sys.argv[1] not in modos:
        sys.exit(f"uso: python -m pipelines.migrations.migra_2026_10_paginas_de_outro_campus {{{'|'.join(modos)}}}")
    modos[sys.argv[1]]()
