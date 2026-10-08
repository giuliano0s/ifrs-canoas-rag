"""Chunker (fase 4): fatia os documentos parseados em chunks com metadata.

Grades de horário viram UM chunk por linha (um professor por chunk), preservando a
granularidade que o retrieval por professor precisa. Também atribui aqui o campus_scope
doc-nível, consumido pelo rerank/cap do serving.
"""

import json
import re

from langchain_text_splitters import RecursiveCharacterTextSplitter

from pipelines.config import CHUNK_SIZE, CHUNKS_DIR, CHUNKS_PATH
from pipelines.urls import to_drive_view_url
from rag.cursos_escopo import classify_curso_escopo  # fonte unica do escopo de curso (ingestao + serving)

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

# cabecalho da secao de um campus nos editais de ingresso multi-campus: o marcador do quadro de vagas
# ("Campus Canoas (a descricao das vagas esta apos a tabela)", "Campus Alvorada: A descricao da tabela
# encontra-se ao final") ou a linha "Campus <campus do IFRS>:" sozinha. o inicio de um ANEXO encerra a secao
_CABECALHO_VAGAS = re.compile(
    r"(?m)^[ \t]*Campus ([A-ZÁÉÍÓÚÂÊÔÃÕÇ][^\n:(,]{1,40}?)[ \t]*:?[ \t]*\(?[ \t]*[Aa] descri")
_CABECALHO_LINHA = re.compile(r"(?m)^[ \t]*Campus ([A-ZÁÉÍÓÚÂÊÔÃÕÇ][^\n:(,]{1,40}?)[ \t]*:[ \t]*$")
_INICIO_ANEXO = re.compile(r"(?m)^[ \t]*ANEXO [IVXL]+\b")

def _campus_do_ifrs(nome):
    return nome.startswith("Canoas") or any(nome.startswith(c) for c in _CAMPI_IFRS)

def secoes_de_campus(text):
    # divide um documento multi-campus em secoes [(campus ou None, trecho)], cortando no cabecalho de
    # cada campus e no inicio de cada ANEXO. o corte vem ANTES do fatiamento por tamanho, entao nenhum
    # chunk mistura dois campi (o bug de fronteira do antigo refino por chunk). exige 2 campi pelo
    # marcador do quadro de vagas, ou 4 pela linha "Campus X:" (um livro com titulos "Campus Sertao:"
    # nao e edital); devolve None no fluxo normal
    cabecalhos = [(m.start(), m.group(1).strip()) for m in _CABECALHO_VAGAS.finditer(text)]
    if len({c for _, c in cabecalhos}) < 2:
        cabecalhos = [(m.start(), m.group(1).strip()) for m in _CABECALHO_LINHA.finditer(text)
                      if _campus_do_ifrs(m.group(1).strip())]
        if len({c for _, c in cabecalhos}) < 4:
            return None
    cortes = sorted(cabecalhos + [(m.start(), None) for m in _INICIO_ANEXO.finditer(text)],
                    key=lambda corte: corte[0])
    secoes = [(None, text[:cortes[0][0]])]
    for i, (pos, campus) in enumerate(cortes):
        fim = cortes[i + 1][0] if i + 1 < len(cortes) else len(text)
        secoes.append((campus, text[pos:fim]))
    return [(c, t) for c, t in secoes if t.strip()]

_MESES_CAL = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
              "setembro", "outubro", "novembro", "dezembro")
_ANO_CAL = re.compile(r"(?i)^\s*ano do calend[aá]rio:\s*(20\d{2})")
_MES_DE = re.compile(r"\bde (" + "|".join(_MESES_CAL) + r")\b(?: de (20\d{2}))?")
_MES_SOLTO = re.compile(r"\b(" + "|".join(_MESES_CAL) + r")\b(?:.*?\b(20\d{2})\b)?")

def _mes_do_evento(linha, ano):
    # (mes, ano) da primeira data da linha ("27 de junho de 2026"); sem "de <mes>", o primeiro mes citado
    baixa = linha.lower()
    achado = _MES_DE.search(baixa) or _MES_SOLTO.search(baixa)
    if not achado:
        return None
    return f"{achado.group(1)} de {achado.group(2) or ano}"

def chunks_de_calendario(text, metadata):
    """Fatia um calendario academico estruturado em um chunk por evento.

    Cada chunk e buscado pela frase do proprio evento (texto_busca, o que e embedado) e entregue ao
    contexto com o bloco do mes inteiro (text). O evento pontual casa forte com a pergunta, e a
    pergunta de conjunto (feriados do ano, eventos de novembro) recebe o mes completo.

    Args:
        text: saida do structure_calendar_text, um evento por linha apos "Ano do calendario: AAAA".
        metadata: metadata comum do documento.

    Returns:
        Lista de chunks; eventos do mesmo mes compartilham o mesmo text.
    """

    # ano do calendario e eventos unicos, na ordem do documento
    linhas = [ln.strip() for ln in text.split("\n") if ln.strip()]
    ano = next((m.group(1) for m in map(_ANO_CAL.match, linhas) if m), metadata.get("published_at"))
    cabecalho = f"Calendário acadêmico {ano} do Campus Canoas"
    eventos = list(dict.fromkeys(ln for ln in linhas if not _ANO_CAL.match(ln)))

    # bloco de cada mes, com o cabecalho do calendario
    por_mes = {}
    for evento in eventos:
        por_mes.setdefault(_mes_do_evento(evento, ano), []).append(evento)
    blocos = {mes: (f"{cabecalho}, eventos de {mes}:\n" if mes else f"{cabecalho}:\n") + "\n".join(lista)
              for mes, lista in por_mes.items()}
    return [{**metadata, "text": blocos[_mes_do_evento(evento, ano)], "texto_busca": f"{cabecalho}: {evento}"}
            for evento in eventos]

def chunks_de_pdf(pdf):
    # chunks de um PDF parseado, com o metadata que o ingest persiste
    url = to_drive_view_url(pdf["source_url"])
    base_meta = {
        "source_url":   url,
        "title":        pdf["title"],
        "type":         "pdf",
        "published_at": pdf.get("published_at"),
        "source_hash":  pdf.get("source_hash"),
    }
    # quadro de vagas: um chunk por curso, com campus_scope POR REGISTRO (o curso de Canoas fica
    # None e sobrevive ao cap; os outros campi ficam "outro" e sao despriorizados). o campus esta
    # EXPLICITO em cada linha e cada chunk e um curso atomico, entao nao ha bug de fronteira (o que
    # derrubou o antigo refino por chunk). o structurer ja emitiu uma linha por curso.
    if pdf.get("is_vagas"):
        return [{**base_meta, "text": ln.strip(), "campus_scope": None if "canoas" in ln.lower() else "outro"}
                for ln in pdf["text"].split("\n") if ln.strip()]
    metadata = {**base_meta,
                "campus_scope": classify_campus_scope(pdf["title"], pdf["text"], url),
                "curso_escopo": classify_curso_escopo(pdf["title"], pdf["text"], url)}
    # grade de horario: leva os marcadores para o metadata (persistidos no upsert). nao e so o
    # switch de parse: na base, is_schedule permite auditar/filtrar as grades e schedule_source
    # (visao/visao_parcial/fallback_texto) sinaliza grade incompleta ou degradada, o que o texto
    # do chunk sozinho nao revela (uma grade parcial parece completa, so com menos professores).
    if pdf.get("is_schedule"):
        metadata["is_schedule"]     = True
        metadata["schedule_source"] = pdf.get("schedule_source")
        return chunk_document(pdf["text"], metadata, por_linha=True)
    # calendario estruturado que nao cabe num chunk: um chunk por evento; o que cabe ja e uma unidade
    if pdf.get("is_calendar"):
        metadata["is_calendar"] = True
        if len(pdf["text"]) > CHUNK_SIZE:
            return chunks_de_calendario(pdf["text"], metadata)
        return chunk_document(pdf["text"], metadata)

    # edital multi-campus: cada secao de campus e fatiada a parte, e cada chunk dela leva o nome do
    # campus no texto (o pedaco de continuacao de um curso deixa de ficar sem campus) e o campus_scope
    # da secao, para o cap tirar do contexto o que nao e de Canoas
    # documento do site de Canoas nunca e dividido (mesma regra do classify_campus_scope)
    secoes = None if "/canoas/" in url else secoes_de_campus(pdf["text"])
    if not secoes:
        return chunk_document(pdf["text"], metadata)
    chunks = []
    for campus, trecho in secoes:
        if campus is None:
            chunks.extend(chunk_document(trecho, metadata))
            continue
        escopo = None if "canoas" in campus.lower() else "outro"
        for c in chunk_document(trecho, {**metadata, "campus_scope": escopo}):
            c["text"] = f"[Edital, seção do Campus {campus}]\n{c['text']}"
            chunks.append(c)
    return chunks

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
            "curso_escopo": classify_curso_escopo(page["title"], page["text"], page["source_url"]),
        }
        chunks.extend(chunk_document(page["text"], metadata))

    # processa PDFs (ignora escaneados)
    for pdf in pdfs_parsed:
        if not pdf["is_scanned"]:
            chunks.extend(chunks_de_pdf(pdf))

    # processa planilhas (Google Sheets estruturados em frases). por_linha=True: a planilha e uma
    # LISTA DE REGISTROS (o structure_sheet_text produz uma frase por linha, um por professor/setor),
    # entao cada registro vira seu proprio chunk. Sem isso, o splitter por tamanho empacota ~20
    # registros num chunk de 4000 chars, e uma consulta a UM registro ("contato do professor X")
    # disputa dentro do blocao e o dado certo nao chega ao contexto (mesmo motivo do por_linha da grade).
    for sheet in sheets_parsed:
        metadata = {
            "source_url":   sheet["source_url"],
            "title":        sheet["title"],
            "type":         "sheet",
            "published_at": sheet.get("published_at"),
            "source_hash":  sheet.get("source_hash"),
            "campus_scope": classify_campus_scope(sheet.get("title", ""), sheet["text"], sheet["source_url"]),
            "curso_escopo": classify_curso_escopo(sheet.get("title", ""), sheet["text"], sheet["source_url"]),
        }
        chunks.extend(chunk_document(sheet["text"], metadata, por_linha=True))

    CHUNKS_PATH.write_text(json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Total de chunks: {len(chunks)}")
    print(f"Salvo: {len(chunks)} chunks em {CHUNKS_PATH}")
    return chunks
