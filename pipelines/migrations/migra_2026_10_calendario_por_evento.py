"""Migração 2026-10: calendários acadêmicos relidos e fatiados em um chunk por evento.

Três defeitos na base, todos da ingestão antiga:
- o calendário estruturado era fatiado por tamanho (~4000 chars, ~55 eventos por chunk), e a pergunta
  sobre UM evento disputava dentro do blocão (em 9 de 18 consultas simuladas o calendário nem chegava
  ao contexto). Cada evento passa a ser um chunk buscado pela frase do evento e entregue com o mês
  inteiro (`chunks_de_calendario`, em pipelines/chunker.py);
- a extração tomava como cabeçalho de mês o bloco de observações que começa pelo nome do mês ("Janeiro
  a março: matrículas..."), perdendo os eventos de janeiro, e o estruturador largava as tabelas do fim
  (período letivo por nível, matrícula online, homologação, ajustes). A releitura usa a extração
  corrigida e a conferência de datas contra o PDF (`confere_calendario`, em pipelines/parser_pdf.py);
- três resoluções foram estruturadas antes do filtro de calendário (`is_calendar_pdf`) e guardam evento
  inventado (a Resolução 37/2023 tem uma "Festa Junina em 31/05/2023" que o documento não diz). Elas
  voltam ao texto do PDF, como o parser atual faz.

A releitura só substitui o texto da base quando a conferência sai sem data omitida nem inventada; senão,
o texto estruturado da base fica, já fatiado por evento.

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY, GEMINI_API_KEY_T1, OPENAI_API_KEY e LLM_PROVIDER=openai):
  python -m pipelines.migrations.migra_2026_10_calendario_por_evento backup     # salva os chunks atuais, relê e monta o plano (sem escrita)
  python -m pipelines.migrations.migra_2026_10_calendario_por_evento aplicar    # substitui os chunks pelo plano
  python -m pipelines.migrations.migra_2026_10_calendario_por_evento verificar
  python -m pipelines.migrations.migra_2026_10_calendario_por_evento reverter   # volta os chunks salvos no backup
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

from pipelines.chunker import chunks_de_pdf
from pipelines.config import index
from pipelines.hashing import source_hash
from pipelines.ingest import ingest_chunks
from pipelines.migrations.migra_2026_10_grades_luna import _baixar
from pipelines.parser_pdf import parse_pdf
from rag import llm

BACKUP = RAIZ / "backups" / "migracao_2026_10_calendario_por_evento.json"
PLANO = RAIZ / "backups" / "migracao_2026_10_calendario_por_evento_plano.json"


def _ordem(chunk_id):
    sufixo = chunk_id.rsplit("#", 1)[-1]
    return int(sufixo) if sufixo.isdigit() else 0


def _calendarios_na_base():
    # chunks de PDF de calendario estruturado (texto do 1o chunk abre com "Ano do calendario"), com vetor
    por_url, cursor = {}, ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True, include_vectors=True)
        for v in res.vectors:
            md = v.metadata or {}
            if md.get("type") == "pdf":
                por_url.setdefault(md.get("source_url"), []).append(
                    {"id": v.id, "vector": list(v.vector), "metadata": md})
        cursor = res.next_cursor
        if cursor == "":
            break
    calendarios = {}
    for url, chunks in por_url.items():
        chunks.sort(key=lambda c: _ordem(c["id"]))
        primeiro = chunks[0]["metadata"]
        if primeiro.get("is_calendar") or primeiro.get("text", "").lower().startswith("ano do calend"):
            calendarios[url] = chunks
    return calendarios


def _texto_do_documento(chunks):
    # une as linhas dos chunks sobrepostos na ordem do documento, sem repetir a sobreposicao. chunk ja
    # fatiado por evento traz o evento em texto_busca (o text e o bloco do mes, com cabecalho)
    if chunks and chunks[0]["metadata"].get("texto_busca"):
        md = chunks[0]["metadata"]
        eventos = list(dict.fromkeys(c["metadata"]["texto_busca"].split(": ", 1)[1] for c in chunks))
        return f"Ano do calendário: {md.get('published_at')}\n" + "\n".join(eventos)
    linhas = []
    for c in chunks:
        for ln in c["metadata"].get("text", "").split("\n"):
            ln = ln.strip()
            if ln and ln not in linhas:
                linhas.append(ln)
    return "\n".join(linhas)


def _releitura(url, chunks):
    # texto que entra no lugar do da base: releitura conferida, texto estruturado da base, ou texto do PDF
    md = chunks[0]["metadata"]
    conteudo = _baixar(url)
    res = parse_pdf({"url": url, "size_kb": len(conteudo) // 1024, "parent": ""}, conteudo) or {}
    conferencia = res.get("calendar_check")
    pdf = {"source_url": url, "title": md.get("title") or res.get("title", ""), "type": "pdf",
           "published_at": md.get("published_at"), "source_hash": md.get("source_hash")}
    if res.get("is_calendar") and not conferencia["omitidas"] and not conferencia["inventadas"]:
        origem = "releitura conferida"
        pdf.update(text=res["text"], is_calendar=True, source_hash=source_hash(conteudo),
                   published_at=res.get("published_at") or md.get("published_at"))
    elif res.get("is_calendar"):
        origem = f"base (releitura divergente: {conferencia})"
        pdf.update(text=_texto_do_documento(chunks), is_calendar=True)
    elif res.get("text"):
        origem = "texto do PDF (fora do filtro de calendario)"
        pdf.update(text=res["text"], source_hash=source_hash(conteudo))
    else:
        return None, "releitura falhou, base mantida"
    return pdf, origem


def backup():
    if llm.PROVIDER != "openai":
        sys.exit("defina LLM_PROVIDER=openai: a releitura e feita com o Luna")
    calendarios = _calendarios_na_base()
    BACKUP.parent.mkdir(exist_ok=True)
    BACKUP.write_text(json.dumps(calendarios, ensure_ascii=False), encoding="utf-8")
    print(f"backup: {sum(len(v) for v in calendarios.values())} chunks de {len(calendarios)} calendarios -> {BACKUP}")

    # plano: chunks pelo caminho do chunker atual, a partir do texto escolhido para cada documento
    plano = {}
    for url, chunks in calendarios.items():
        pdf, origem = _releitura(url, chunks)
        if pdf is None:
            print(f"  {url[-60:]}: {origem}")
            continue
        novos = chunks_de_pdf(pdf)
        for i, c in enumerate(novos):
            c["id"] = f"{url}#{i}"
        plano[url] = novos
        blocos = len({c["text"] for c in novos})
        print(f"  {url[-60:]}: {len(chunks)} chunks -> {len(novos)} ({blocos} textos distintos) | {origem}")
    PLANO.write_text(json.dumps(plano, ensure_ascii=False), encoding="utf-8")
    print(f"plano de {len(plano)} documentos -> {PLANO}")


def aplicar():
    calendarios = json.loads(BACKUP.read_text(encoding="utf-8"))
    plano = json.loads(PLANO.read_text(encoding="utf-8"))

    # insere os novos antes de apagar o que sobrou do antigo (nunca deleta sem repor)
    novos = [c for chunks in plano.values() for c in chunks]
    ingest_chunks(novos)
    ids_novos = {c["id"] for c in novos}
    sobras = [c["id"] for url in plano for c in calendarios[url] if c["id"] not in ids_novos]
    for i in range(0, len(sobras), 1000):
        index.delete(ids=sobras[i:i + 1000])
    print(f"aplicado: {len(novos)} chunks em {len(plano)} documentos; {len(sobras)} ids antigos removidos")


def verificar():
    plano = json.loads(PLANO.read_text(encoding="utf-8"))
    for url, novos in plano.items():
        res = index.fetch(ids=[c["id"] for c in novos], include_metadata=True)
        presentes = [v for v in res if v]
        marcados = sum(1 for v in presentes if (v.metadata or {}).get("is_calendar"))
        print(f"  {url[-60:]}: {len(presentes)}/{len(novos)} chunks na base, {marcados} com is_calendar")


def reverter():
    # o metadata volta sem campo nulo: o filtro de campus do atendimento descarta chunk com campo nulo
    calendarios = json.loads(BACKUP.read_text(encoding="utf-8"))
    plano = json.loads(PLANO.read_text(encoding="utf-8"))
    ids_plano = [c["id"] for chunks in plano.values() for c in chunks]
    for i in range(0, len(ids_plano), 1000):
        index.delete(ids=ids_plano[i:i + 1000])
    vetores = [(c["id"], c["vector"], {k: v for k, v in c["metadata"].items() if v is not None})
               for chunks in calendarios.values() for c in chunks]
    for i in range(0, len(vetores), 100):
        index.upsert(vectors=vetores[i:i + 100])
    print(f"revertido: {len(vetores)} chunks de {len(calendarios)} documentos")


if __name__ == "__main__":
    modos = {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}
    if len(sys.argv) != 2 or sys.argv[1] not in modos:
        sys.exit(f"uso: python -m pipelines.migrations.migra_2026_10_calendario_por_evento {{{'|'.join(modos)}}}")
    modos[sys.argv[1]]()
