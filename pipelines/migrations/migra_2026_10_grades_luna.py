"""Migração 2026-10: relê as grades de horário com o GPT-5.6 Luna e o gate de dia por posição.

As grades da base foram lidas por visão com o gemini-2.5-flash, que troca o dia da semana (conferido
na imagem da grade PROFMAT 2026/2: "Quinta" onde a grade diz "Quarta"). O gate de horário/sala não
pega dia trocado; o gate de dia (`_dias_conferem`, em parser_pdf) passou a conferir a coluna de cada
disciplina pelas coordenadas da página. Esta migração relê as grades atuais da base com o provedor
configurado (LLM_PROVIDER=openai) e substitui os chunks de grade, com backup completo para reverter.

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY, OPENAI_API_KEY e LLM_PROVIDER=openai):
  python -m pipelines.migrations.migra_2026_10_grades_luna backup     # salva os chunks atuais e relê as grades (mostra o comparativo)
  python -m pipelines.migrations.migra_2026_10_grades_luna aplicar    # substitui os chunks de grade pela releitura
  python -m pipelines.migrations.migra_2026_10_grades_luna verificar
  python -m pipelines.migrations.migra_2026_10_grades_luna reverter   # volta os chunks salvos no backup
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
import requests

from pipelines.chunker import chunk_document, classify_campus_scope
from pipelines.config import fetch, index
from pipelines.hashing import source_hash
from pipelines.ingest import ingest_chunks
from pipelines.parser_pdf import parse_pdf
from rag import llm
from rag.cursos_escopo import classify_curso_escopo

BACKUP = RAIZ / "backups" / "migracao_2026_10_grades_luna.json"
RELEITURA = RAIZ / "backups" / "migracao_2026_10_grades_luna_releitura.json"


def _grades_na_base():
    # todos os chunks de grade (is_schedule), com vetor, agrupados pela URL de origem
    grades, cursor = {}, ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True, include_vectors=True)
        for v in res.vectors:
            md = v.metadata or {}
            if md.get("is_schedule"):
                grades.setdefault(md.get("source_url"), []).append(
                    {"id": v.id, "vector": list(v.vector), "metadata": md})
        cursor = res.next_cursor
        if cursor == "":
            break
    return grades


def _baixar(url):
    # link de visualizacao do Drive vira download direto; o resto passa pela sessao anti-bot
    m = re.search(r"drive\.google\.com/file/d/([^/]+)", url)
    if m:
        r = requests.get(f"https://drive.google.com/uc?export=download&id={m.group(1)}", timeout=60)
    else:
        r = fetch(url, timeout=60)
    r.raise_for_status()
    return r.content


def backup():
    if llm.PROVIDER != "openai":
        sys.exit("defina LLM_PROVIDER=openai: a releitura e feita com o Luna")
    grades = _grades_na_base()
    BACKUP.parent.mkdir(exist_ok=True)
    BACKUP.write_text(json.dumps(grades, ensure_ascii=False), encoding="utf-8")
    print(f"backup: {sum(len(v) for v in grades.values())} chunks de {len(grades)} grades -> {BACKUP}")

    # releitura de cada grade com o provedor atual e o gate de dia
    releitura = {}
    for url, chunks in grades.items():
        conteudo = _baixar(url)
        hash_novo = source_hash(conteudo)
        hash_base = chunks[0]["metadata"].get("source_hash")
        res = parse_pdf({"url": url, "size_kb": len(conteudo) // 1024, "parent": ""}, conteudo)
        if not res or not res.get("text"):
            print(f"  FALHOU a releitura, grade mantida: {url}")
            continue
        releitura[url] = {**res, "source_hash": hash_novo, "mesmo_arquivo": hash_novo == hash_base}
        linhas = [ln for ln in res["text"].split("\n") if ln.strip()]
        print(f"  {url[-60:]}: base {len(chunks)} chunks ({chunks[0]['metadata'].get('schedule_source')}) "
              f"-> releitura {len(linhas)} ({res.get('schedule_source')}), mesmo arquivo: {hash_novo == hash_base}")
    RELEITURA.write_text(json.dumps(releitura, ensure_ascii=False), encoding="utf-8")
    print(f"releitura de {len(releitura)} grades -> {RELEITURA}")


def aplicar():
    grades = json.loads(BACKUP.read_text(encoding="utf-8"))
    releitura = json.loads(RELEITURA.read_text(encoding="utf-8"))
    novos = []
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
        novos.extend(chunks)

        # so troca a grade que produziu chunk novo (nunca deleta sem repor)
        if chunks:
            index.delete(ids=[c["id"] for c in grades[url]])
    ingest_chunks(novos)
    print(f"aplicado: {len(novos)} chunks novos em {len(releitura)} grades")


def verificar():
    grades = _grades_na_base()
    for url, chunks in grades.items():
        fontes = {c["metadata"].get("schedule_source") for c in chunks}
        print(f"  {url[-60:]}: {len(chunks)} chunks, {fontes}")
    for url, chunks in grades.items():
        for c in chunks:
            if "PROFMAT (2)" in c["metadata"].get("text", ""):
                print("  amostra:", c["metadata"]["text"][:200])


def reverter():
    grades = json.loads(BACKUP.read_text(encoding="utf-8"))
    atuais = _grades_na_base()
    for url in grades:
        if url in atuais:
            index.delete(ids=[c["id"] for c in atuais[url]])
    vetores = [(c["id"], c["vector"], c["metadata"]) for cs in grades.values() for c in cs]
    for i in range(0, len(vetores), 100):
        index.upsert(vectors=vetores[i:i + 100])
    print(f"revertido: {len(vetores)} chunks restaurados de {len(grades)} grades")


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}.get(
        modo, lambda: sys.exit(__doc__))()
