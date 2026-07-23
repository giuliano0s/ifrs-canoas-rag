"""Configuração compartilhada da pipeline de ingestão: flags de execução, constantes,
caminhos de dados, sessão HTTP anti-bot e clientes de API (Gemini e Upstash de escrita).

Todo módulo de fase (crawler, parsers, chunker, ingest) importa daqui; este módulo não
importa de nenhum deles, o que mantém o grafo de dependências acíclico.
"""

import os
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai as google_genai
from upstash_vector import Index

load_dotenv()

# anos válidos do crawler; override opcional via env ANOS_VALIDOS (CSV, ex: "2026" ou "2025,2026").
# o default deriva da data (ano anterior + corrente) em vez de congelar um intervalo fixo, que
# apodreceria na virada do ano sem ninguém notar.
_anos_env    = os.getenv("ANOS_VALIDOS")
ANOS_VALIDOS = ({int(a) for a in _anos_env.split(",") if a.strip()} if _anos_env
                else {datetime.now().year - 1, datetime.now().year})

def _flag_env(nome, default):
    # le uma flag booleana da env (CSV "True"/"False"), preservando o default se nao setada
    valor = os.getenv(nome)
    return valor.strip().lower() == "true" if valor else default

# flags de controle de execução; override opcional via env (ex: $env:REPARSE="True")
REPARSE  = _flag_env("REPARSE", False)
REINGEST = _flag_env("REINGEST", False)
# baixar PDFs escaneados (sem texto). False = pula os ja registrados como escaneados,
# evitando o re-download recorrente; deixar True quando houver OCR/pixelrag.
INCLUDE_SCANNED = _flag_env("INCLUDE_SCANNED", False)

# configurações gerais
BASE_URL        = "https://ifrs.edu.br/canoas/"
# portal de ingresso do IFRS: fonte-de-registro das vagas OFERTADAS (que o site do campus nao tem).
# entra no crawl como dominio valido (is_valid_page) semeado pela whitelist; o gate de PII barra as
# listas nominais de candidatos e o filtro de ano limita ao periodo corrente (/AAAA-S/).
INGRESSO_URL    = "https://ingresso.ifrs.edu.br/"
DRIVE_DOWNLOAD  = "https://drive.google.com/uc?export=download&id="
CHUNK_SIZE      = 4000
MIN_CHARS       = 150 # minimo de caracteres que um PDF "não-scan" possui
SAVE_INTERVAL   = 50

# maximo de caracteres para inferência de data
MAX_CHARS_INICIO = 500
MAX_CHARS_FIM   = 500

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# sessao HTTP persistente: o site tem o anti-bot Radware, que na 1a visita "sem cookie" responde
# uma pagina de captcha (HTTP 200) no lugar do conteudo e SETA cookies (__uzm*). guardando esses
# cookies na sessao, a requisicao seguinte passa e vem o conteudo real (como faz um navegador).
# sem isso, cada requests.get e uma 1a visita -> sempre captcha -> o crawl nao acha links e colapsa.
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def _e_desafio(resp):
    # pagina de desafio do anti-bot (Radware/captcha) que volta com HTTP 200 no lugar do conteudo
    corpo = resp.text or ""
    return "Radware" in corpo or "captcha" in corpo.lower()


def fetch(url, timeout=10, headers=None):
    # busca guardando os cookies do anti-bot na SESSION; se vier a pagina de desafio, repete na
    # mesma sessao (agora ja com o cookie), que e o que faz passar. ate 2 tentativas extras.
    resp = SESSION.get(url, timeout=timeout, headers=headers)
    for _ in range(2):
        if not (getattr(resp, "ok", False) and _e_desafio(resp)):
            break
        time.sleep(0.5)
        resp = SESSION.get(url, timeout=timeout, headers=headers)
    return resp

# caminhos de dados
DATA_DIR          = Path("data")
RAW_DIR           = DATA_DIR / "raw"
PARSED_DIR        = DATA_DIR / "parsed"
CHUNKS_DIR        = DATA_DIR / "chunks"
INFO_DIR          = DATA_DIR / "info"

PAGES_PATH        = RAW_DIR / "pages.json"
PDFS_PATH         = RAW_DIR / "pdfs.json"
SHEETS_PATH       = RAW_DIR / "sheets.json"
PAGES_PARSED_PATH = PARSED_DIR / "pages_parsed.json"
PDFS_PARSED_PATH  = PARSED_DIR / "pdfs_parsed.json"
SHEETS_PARSED_PATH = PARSED_DIR / "sheets_parsed.json"
FORMAT_ERRORS_PATH = PARSED_DIR / "pdfs_format_errors.json"
SCANNED_PATH       = PARSED_DIR / "pdfs_scanned.json"
CHUNKS_PATH       = CHUNKS_DIR / "chunks.json"
WHITELIST_PATH    = INFO_DIR / "whitelist.txt"

SHEETS_FALHAS = []  # DEBUG: planilhas que falharam no parse, para relatar residuo no fim

# clientes de API
google_client = google_genai.Client(api_key=os.getenv("GEMINI_API_KEY_T1"))

index = Index(
    url=os.getenv("UPSTASH_ENDPOINT"),
    token=os.getenv("UPSTASH_WRITE_API_KEY")
)
