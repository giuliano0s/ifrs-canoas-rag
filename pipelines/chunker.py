"""Chunker (fase 4): fatia os documentos parseados em chunks com metadata.

Grades de horário viram UM chunk por linha (um professor por chunk), preservando a
granularidade que o retrieval por professor precisa. Também atribui aqui o campus_scope
doc-nível, consumido pelo rerank/cap do serving.
"""

import json

from langchain_text_splitters import RecursiveCharacterTextSplitter

from pipelines.config import CHUNK_SIZE, CHUNKS_DIR, CHUNKS_PATH
from pipelines.urls import to_drive_view_url

_CAMPI_IFRS = ("Alvorada", "Bento Gon", "Caxias", "Erechim", "Farroupilha", "Feliz", "Ibirub",
               "Osório", "Osorio", "Porto Alegre", "Restinga", "Rio Grande", "Rolante", "Sertão",
               "Sertao", "Vacaria", "Veranó", "Verano", "Viamão", "Viamao")

def classify_campus_scope(title, text, url=""):
    # campus_scope="outro" (doc institucional/multi-campus -> penalizado no rerank) SO com marcador
    # EXPLICITO; default None (neutro). ALTA PRECISAO por decisao: um doc de Canoas tagueado errado
    # como "outro" leva -CAMPUS_PENALTY e some do top-15, entao na duvida deixa neutro.
    # 1) NUNCA tagueia doc do site do campus (/canoas/): e Canoas-especifico mesmo que cite a rede
    #    (ex: o relatorio CPA de Canoas menciona varios campi; sem isso ele virava "outro" errado).
    if "/canoas/" in (url or ""):
        return None
    # 2) fora do /canoas/: pega o PDI (titulo "PDI" ou "Plano de Desenvolvimento Institucional") e
    #    docs que enumeram varios campi do IFRS (planos de acao institucionais, etc.).
    t = (title or "").lower()
    head = (text or "")[:3000]
    if t.startswith("pdi") or "plano de desenvolvimento institucional" in head.lower():
        return "outro"
    if sum(1 for c in _CAMPI_IFRS if c in head) >= 4:
        return "outro"
    return None

# NOTA: aqui existiu um "refino POR CHUNK do campus_scope" (refinar_campus_scope) que liberava do
# "outro" os chunks de secoes de Canoas dentro de docs institucionais (ex: o Quadro 5.3 de vagas do
# PDI). Foi REMOVIDO em jul/2026 por ter um unico consumidor vivo (recuperar o Quadro 5.3 para o caso
# total-vagas-campus) e um bug de fronteira (chunk com a cauda do campus anterior liberado como
# Canoas, vazando o subtotal de Bento Gonçalves). Detalhes e como reintroduzir em CLAUDE.md, secao
# "Solucoes removidas". O escopo de campus hoje e so o doc-nivel (classify_campus_scope) + o cap.

splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=int(CHUNK_SIZE * 0.2),
    length_function=len,
)

def chunk_document(text, metadata, por_linha=False):
    # por_linha: grade de horario -> UM chunk por linha (um professor por chunk). Preserva a
    # granularidade que o retrieval por professor precisa; o chunker por tamanho re-densificaria
    # (varios professores num chunk), que foi justamente o gargalo do recall das grades.
    if por_linha:
        return [{"text": ln.strip(), **metadata} for ln in text.split("\n") if ln.strip()]
    if len(text) <= CHUNK_SIZE:
        return [{"text": text, **metadata}]
    parts = splitter.split_text(text)
    return [{"text": part, **metadata} for part in parts]

def run_chunker(pages_parsed, pdfs_parsed, sheets_parsed):
    print("\n" + "="*60)
    print("FASE 4 — CHUNKER")
    print("="*60)

    CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
    chunks = []

    # processa páginas HTML
    for page in pages_parsed:
        metadata = {
            "source_url":   page["source_url"],
            "title":        page["title"],
            "type":         "html",
            "published_at": page.get("published_at"),
            "source_hash":  page.get("source_hash"),
            "campus_scope": classify_campus_scope(page["title"], page["text"], page["source_url"]),
        }
        chunks.extend(chunk_document(page["text"], metadata))

    # processa PDFs (ignora escaneados)
    for pdf in pdfs_parsed:
        if pdf["is_scanned"]:
            continue
        metadata = {
            "source_url":   to_drive_view_url(pdf["source_url"]),
            "title":        pdf["title"],
            "type":         "pdf",
            "published_at": pdf.get("published_at"),
            "source_hash":  pdf.get("source_hash"),
            "campus_scope": classify_campus_scope(pdf["title"], pdf["text"], to_drive_view_url(pdf["source_url"])),
        }
        # grade de horario: leva os marcadores para o metadata (persistidos no upsert). nao e so o
        # switch de parse: na base, is_schedule permite auditar/filtrar as grades e schedule_source
        # (visao/visao_parcial/fallback_texto) sinaliza grade incompleta ou degradada, o que o texto
        # do chunk sozinho nao revela (uma grade parcial parece completa, so com menos professores).
        if pdf.get("is_schedule"):
            metadata["is_schedule"]     = True
            metadata["schedule_source"] = pdf.get("schedule_source")
        chunks.extend(chunk_document(pdf["text"], metadata, por_linha=pdf.get("is_schedule", False)))

    # processa planilhas (Google Sheets estruturados em frases)
    for sheet in sheets_parsed:
        metadata = {
            "source_url":   sheet["source_url"],
            "title":        sheet["title"],
            "type":         "sheet",
            "published_at": sheet.get("published_at"),
            "source_hash":  sheet.get("source_hash"),
            "campus_scope": classify_campus_scope(sheet.get("title", ""), sheet["text"], sheet["source_url"]),
        }
        chunks.extend(chunk_document(sheet["text"], metadata))

    CHUNKS_PATH.write_text(json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Total de chunks: {len(chunks)}")
    print(f"Salvo: {len(chunks)} chunks em {CHUNKS_PATH}")
    return chunks
