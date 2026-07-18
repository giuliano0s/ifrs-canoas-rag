"""Inferência da data de publicação (published_at) de um documento.

A ordem é crítica e vai do que o PRÓPRIO documento diz para os fallbacks: nome do
arquivo -> conteúdo (LLM) -> pasta de upload da URL -> página pai (ver get_published_at).
"""

import re
import time

from pipelines.config import MAX_CHARS_FIM, MAX_CHARS_INICIO, google_client

DATE_URL_PATTERN = re.compile(r"/(\d{4})/")


def extract_date_from_url(url):
    match = DATE_URL_PATTERN.search(url)
    return match.group(1) if match else None

def extract_date_from_filename(url):
    # ano no NOME do arquivo (nao na pasta /YYYY/MM/, que e a data de UPLOAD do WordPress).
    # o nome costuma carregar o ano de referencia do doc (ex: Campus-Canoas_2019.pdf -> 2019).
    # SO para arquivos reais (PDF/doc/planilha): o "nome" de uma pagina HTML e o SLUG da
    # manchete, que traz o ano de um EVENTO (ex: .../processo-seletivo-2026/) e nao a data de
    # publicacao; datar HTML pelo slug jogaria noticia velha para o futuro no rerank temporal.
    base = url.rstrip("/").split("/")[-1]
    if not re.search(r"\.(pdf|docx?|xlsx?|pptx?|odt|ods)$", base, re.IGNORECASE):
        return None
    # lookaround de digito (nao \b): "_" e caractere de palavra, entao \b nao casa em "_2019".
    # so aceita se houver UM unico ano no nome; nomes com anos conflitantes caem pro conteudo.
    anos = set(re.findall(r"(?<![0-9])(20[0-3]\d)(?![0-9])", base))
    return anos.pop() if len(anos) == 1 else None

def extract_date_from_text(text):
    truncated = text[:MAX_CHARS_INICIO] + "\n...\n" + text[-MAX_CHARS_FIM:]
    prompt = f"Qual é o ano de publicação deste documento? Responda APENAS com o ano no formato YYYY. Se não encontrar, responda exatamente: None\n\n{truncated}"
    for attempt in range(3):
        try:
            response = google_client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
                config={"temperature": 0.0}
            )
            result = (response.text or "").strip()
            # saneia a saida: aceita so um ano plausivel (2000-2035), senao None
            match = re.search(r"\b(20[0-3]\d)\b", result)
            return match.group(1) if match else None
        except Exception as e:
            wait = 60 * (attempt + 1)
            print(f"  Rate limit, aguardando {wait}s... ({e})")
            time.sleep(wait)
    return None

def get_published_at(doc, max_retries=10, parent_dates=None):
    # data do PROPRIO documento primeiro (nome do arquivo -> conteudo), depois fallbacks.
    # ORDEM E CRITICA: a pasta /YYYY/MM/ da URL e a data de UPLOAD, nao a do doc; tentar a URL
    # primeiro fazia todo doc antigo re-upado herdar o ano errado (ex: relatorio de 2019 numa
    # pasta /2025/03/ virava published_at=2025, e o rerank temporal o tratava como novo).
    date = extract_date_from_filename(doc["source_url"])
    if date:
        return date, "nome_arquivo"

    text = doc.get("text", "")
    if text:
        for attempt in range(max_retries):
            try:
                date = extract_date_from_text(text)  # LLM no conteudo: desempata proposta vs vigencia
                break
            except Exception as e:
                wait = 10 * (attempt + 1)
                print(f"  Tentativa {attempt+1}/{max_retries} falhou. Aguardando {wait}s...")
                time.sleep(wait)
        else:
            raise Exception(f"Rate limit persistente após {max_retries} tentativas: {doc['source_url']}")
        if date:
            return date, "conteudo"

    # fallbacks: a pasta de upload da URL, depois a data herdada da pagina pai
    date = extract_date_from_url(doc["source_url"])
    if date:
        return date, "url_upload"
    parent = doc.get("parent", "")
    if parent:
        date = extract_date_from_url(parent) or (parent_dates or {}).get(parent)
        if date:
            return date, "pai"

    return None, "sem_data"
