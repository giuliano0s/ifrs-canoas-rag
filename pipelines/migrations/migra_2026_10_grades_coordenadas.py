"""Migração 2026-10: relê as grades de horário pelas coordenadas do PDF, sem visão.

A grade aSc é desenhada como retângulos preenchidos sob o cabeçalho Seg..Sex, com as faixas de
horário na coluna da esquerda; `ler_grade_asc` (pipelines/parser_pdf.py) monta cada aula pela posição
do texto na célula, sem LLM. A releitura por visão da migração de 03/10 (migra_2026_10_grades_luna)
deixou a turma D. SIST. 2 - TARDE como `visao_parcial` e gravou chunks "nenhuma aula identificável" na
grade vazia de AUT; pelas coordenadas, as 10 grades saem completas (`schedule_source` "coordenadas").

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY):
  python -m pipelines.migrations.migra_2026_10_grades_coordenadas backup     # salva os chunks atuais e relê as grades (sem escrita)
  python -m pipelines.migrations.migra_2026_10_grades_coordenadas aplicar    # substitui os chunks de grade pela releitura
  python -m pipelines.migrations.migra_2026_10_grades_coordenadas verificar
  python -m pipelines.migrations.migra_2026_10_grades_coordenadas reverter   # volta os chunks salvos no backup
"""

import json
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from pipelines.chunker import chunk_document, classify_campus_scope
from pipelines.config import index
from pipelines.hashing import source_hash
from pipelines.ingest import ingest_chunks
from pipelines.migrations.migra_2026_10_grades_luna import _baixar, _grades_na_base
from pipelines.parser_pdf import parse_pdf
from rag.cursos_escopo import classify_curso_escopo

BACKUP = RAIZ / "backups" / "migracao_2026_10_grades_coordenadas.json"
RELEITURA = RAIZ / "backups" / "migracao_2026_10_grades_coordenadas_releitura.json"


def backup():
    grades = _grades_na_base()
    BACKUP.parent.mkdir(exist_ok=True)
    BACKUP.write_text(json.dumps(grades, ensure_ascii=False), encoding="utf-8")
    print(f"backup: {sum(len(v) for v in grades.values())} chunks de {len(grades)} grades -> {BACKUP}")

    # releitura de cada grade pelo parser atual (coordenadas, com a visao so para pagina nao reconhecida)
    releitura = {}
    for url, chunks in grades.items():
        conteudo = _baixar(url)
        res = parse_pdf({"url": url, "size_kb": len(conteudo) // 1024, "parent": ""}, conteudo)
        if not res or not res.get("text"):
            print(f"  FALHOU a releitura, grade mantida: {url}")
            continue
        releitura[url] = {**res, "source_hash": source_hash(conteudo)}
        linhas = [ln for ln in res["text"].split("\n") if ln.strip()]
        print(f"  {url[-60:]}: base {len(chunks)} chunks ({chunks[0]['metadata'].get('schedule_source')}) "
              f"-> releitura {len(linhas)} ({res.get('schedule_source')})")
    RELEITURA.write_text(json.dumps(releitura, ensure_ascii=False), encoding="utf-8")
    print(f"releitura de {len(releitura)} grades -> {RELEITURA}")


def aplicar():
    grades = json.loads(BACKUP.read_text(encoding="utf-8"))
    releitura = json.loads(RELEITURA.read_text(encoding="utf-8"))
    novos, antigos = [], []
    for url, res in releitura.items():
        meta = {
            "source_url":   url,
            "title":        res.get("title", ""),
            "type":         "pdf",
            "published_at": res.get("published_at") or grades[url][0]["metadata"].get("published_at"),
            "source_hash":  res["source_hash"],
            "campus_scope": classify_campus_scope(res.get("title", ""), res["text"], url),
            "curso_escopo": classify_curso_escopo(res.get("title", ""), res["text"], url),
            "is_schedule":  True,
            "schedule_source": res.get("schedule_source"),
        }
        chunks = chunk_document(res["text"], meta, por_linha=True)
        for i, c in enumerate(chunks):
            c["id"] = f"{url}#{i}"
        if chunks:
            novos.extend(chunks)
            antigos.extend(c["id"] for c in grades[url])

    # insere os novos antes de apagar o que sobrou do antigo (nunca deleta sem repor)
    ingest_chunks(novos)
    ids_novos = {c["id"] for c in novos}
    sobras = [i for i in antigos if i not in ids_novos]
    for i in range(0, len(sobras), 1000):
        index.delete(ids=sobras[i:i + 1000])
    print(f"aplicado: {len(novos)} chunks novos em {len(releitura)} grades; {len(sobras)} ids antigos removidos")


def verificar():
    grades = _grades_na_base()
    for url, chunks in grades.items():
        fontes = {c["metadata"].get("schedule_source") for c in chunks}
        print(f"  {url[-60:]}: {len(chunks)} chunks, {fontes}")


def reverter():
    # o metadata volta sem campo nulo: o filtro de campus do atendimento descarta chunk com campo nulo
    grades = json.loads(BACKUP.read_text(encoding="utf-8"))
    atuais = _grades_na_base()
    for url in grades:
        if url in atuais:
            index.delete(ids=[c["id"] for c in atuais[url]])
    vetores = [(c["id"], c["vector"], {k: v for k, v in c["metadata"].items() if v is not None})
               for chunks in grades.values() for c in chunks]
    for i in range(0, len(vetores), 100):
        index.upsert(vectors=vetores[i:i + 100])
    print(f"revertido: {len(vetores)} chunks de {len(grades)} grades")


if __name__ == "__main__":
    modos = {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}
    if len(sys.argv) != 2 or sys.argv[1] not in modos:
        sys.exit(f"uso: python -m pipelines.migrations.migra_2026_10_grades_coordenadas {{{'|'.join(modos)}}}")
    modos[sys.argv[1]]()
