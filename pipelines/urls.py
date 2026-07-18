"""Classificação e transformação de URLs: o que é página/PDF/planilha/Drive, o que o
crawler ignora, e as conversões de URL (CSV de planilha, download/visualização do Drive).

Funções puras (sem rede); usadas pelo crawler, pelos parsers e pelo chunker.
"""

import re

from pipelines.config import ANOS_VALIDOS, BASE_URL, DRIVE_DOWNLOAD

# extensões ignoradas pelo crawler
IGNORED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp",
                      ".xlsx", ".xls", ".zip", ".ods", ".doc", ".ppt", ".docx")

# keywords de páginas de baixo valor
IGNORE_KEYWORDS = ["/10anos/", "covid", "apnps", "retornoseguro", "vacina",
                   "/paginateste/", "balanco-", "balanco_", "demonstracao-",
                   "demonstracao_", "ata-concamp", "ptd-", "ptd_", "plano-de-trabalho"]


def is_valid_page(url):
    return url.startswith(BASE_URL)

def is_pdf_by_extension(url):
    return url.lower().endswith(".pdf")

def is_drive_link(url):
    return "drive.google.com/file/d/" in url

def is_drive_url(url):
    return "drive.google.com" in url

def is_gsheet_link(url):
    return "docs.google.com/spreadsheets" in url

def gsheet_csv_url(url):
    # converte um link de planilha na variante de exportacao CSV, preservando o gid da aba se houver;
    # sem gid, omite o parametro (forcar gid=0 quebra planilhas cuja aba padrao tem outro id)
    gid_match = re.search(r"gid=(\d+)", url)
    gid_param = f"&gid={gid_match.group(1)}" if gid_match else ""

    pub_match = re.search(r"(https://docs\.google\.com/spreadsheets/d/e/[^/]+)", url)
    if pub_match:
        return f"{pub_match.group(1)}/pub?single=true&output=csv{gid_param}"

    normal_match = re.search(r"(https://docs\.google\.com/spreadsheets/d/[^/]+)", url)
    if normal_match:
        return f"{normal_match.group(1)}/export?format=csv{gid_param}"

    return url

def extract_drive_id(url):
    parts = url.split("/file/d/")
    return parts[1].split("/")[0]

def build_download_url(url):
    if is_drive_link(url):
        return DRIVE_DOWNLOAD + extract_drive_id(url)
    return url

def to_drive_view_url(url):
    if "drive.google.com/uc" in url:
        match = re.search(r"id=([a-zA-Z0-9_-]+)", url)
        if match:
            return f"https://drive.google.com/file/d/{match.group(1)}/view"
    return url

def should_ignore(url):
    url_lower = url.lower()
    if any(url_lower.endswith(ext) for ext in IGNORED_EXTENSIONS):
        return True
    # ignora URLs que contenham anos fora do range válido
    year_match = re.search(r"/(\d{4})/", url)
    if year_match and int(year_match.group(1)) not in ANOS_VALIDOS:
        return True
    if any(k in url_lower for k in IGNORE_KEYWORDS):
        return True
    return False
