"""
Pipeline de ingestão do IFRS RAG.
Executa sequencialmente: crawler > parser HTML > parser PDF > chunker > ingest > snapshot.

Flags de controle:
  RECRAWL   = True > recomeça o crawler do zero
  REPARSE   = True > reparseia todos os HTMLs e PDFs
  REINGEST  = True > recria a collection e reingeere tudo

  False em qualquer flag = modo incremental (pula o que já foi feito)

Pipeline desenvolvida originalmente em ../notebooks
"""

import json
import os
import re
import sys
import time
import tempfile
from collections import deque
from pathlib import Path
from datetime import datetime

import fitz
import gdown
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai as google_genai
from langchain.text_splitter import RecursiveCharacterTextSplitter
from upstash_vector import Index
from urllib.parse import urljoin

# permite importar pacotes do projeto ao rodar como script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ui.clone_page import clone_page

load_dotenv()

# ── variáveis globais ──────────────────────────────────────────────────────────

# anos válidos do crawler; override opcional via env ANOS_VALIDOS (CSV, ex: "2026" ou "2025,2026")
_anos_env    = os.getenv("ANOS_VALIDOS")
ANOS_VALIDOS = {int(a) for a in _anos_env.split(",") if a.strip()} if _anos_env else set(range(2025, 2027))

def _flag_env(nome, default):
    # le uma flag booleana da env (CSV "True"/"False"), preservando o default se nao setada
    valor = os.getenv(nome)
    return valor.strip().lower() == "true" if valor else default

# flags de controle de execução; override opcional via env (ex: $env:RECRAWL="False")
RECRAWL  = _flag_env("RECRAWL", True)
REPARSE  = _flag_env("REPARSE", False)
REINGEST = _flag_env("REINGEST", False)

# configurações gerais
BASE_URL        = "https://ifrs.edu.br/canoas/"
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

# padrão de anos válidos gerado dinamicamente
_anos_str = "|".join(str(a) for a in ANOS_VALIDOS)
VALID_YEAR_PATTERN = re.compile(rf"({_anos_str})")
DATE_URL_PATTERN   = re.compile(r"/(\d{4})/")

# extensões ignoradas pelo crawler
IGNORED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp",
                      ".xlsx", ".xls", ".zip", ".ods", ".doc", ".ppt", ".docx")

# keywords de páginas de baixo valor
IGNORE_KEYWORDS = ["/10anos/", "covid", "apnps", "retornoseguro", "vacina",
                   "/paginateste/", "balanco-", "balanco_", "demonstracao-",
                   "demonstracao_", "ata-concamp", "ptd-", "ptd_", "plano-de-trabalho"]

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
CHUNKS_PATH       = CHUNKS_DIR / "chunks.json"
WHITELIST_PATH    = INFO_DIR / "whitelist.txt"

SHEETS_FALHAS = []  # DEBUG: planilhas que falharam no parse, para relatar residuo no fim

# clientes de API
google_client = google_genai.Client(api_key=os.getenv("GEMINI_API_KEY_T1"))

index = Index(
    url=os.getenv("UPSTASH_ENDPOINT"),
    token=os.getenv("UPSTASH_WRITE_API_KEY")
)

# ── funções do crawler ─────────────────────────────────────────────────────────

def is_valid_page(url):
    return url.startswith(BASE_URL)

def is_pdf_by_extension(url):
    return url.lower().endswith(".pdf")

def is_drive_link(url):
    return "drive.google.com/file/d/" in url

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

def run_crawler():
    print("\n" + "="*60)
    print("FASE 1 — CRAWLER")
    print("="*60)

    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if RECRAWL:
        pages_found  = []
        pdfs_found   = []
        sheets_found = []
        visited      = set()
        queue        = deque([BASE_URL])
        queued       = set([BASE_URL])
    else:
        pages_found  = json.loads(PAGES_PATH.read_text(encoding="utf-8"))  if PAGES_PATH.exists()  else []
        pdfs_found   = json.loads(PDFS_PATH.read_text(encoding="utf-8"))   if PDFS_PATH.exists()   else []
        sheets_found = json.loads(SHEETS_PATH.read_text(encoding="utf-8")) if SHEETS_PATH.exists() else []
        already_known = set(pages_found) | {p["url"] for p in pdfs_found} | {s["url"] for s in sheets_found}
        visited = already_known.copy()
        queued  = already_known.copy()
        queue   = deque([BASE_URL])
        print(f"Dados existentes: {len(pages_found)} páginas, {len(pdfs_found)} PDFs, {len(sheets_found)} planilhas")
        print(f"URLs já conhecidas ignoradas: {len(already_known)}")

    # carrega whitelist
    whitelist = set()
    if WHITELIST_PATH.exists():
        for line in WHITELIST_PATH.read_text(encoding="utf-8").splitlines():
            url = line.strip()
            if url and not url.startswith("#"):
                whitelist.add(url)
                if url not in queued:
                    queue.append(url)
                    queued.add(url)
        print(f"Whitelist: {len(whitelist)} URLs forçadas")

    # loop principal
    while queue:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        total = len(visited) + len(queue)
        print(f"[{len(visited)}/{total}] Visitando: {url}")

        try:
            response = requests.get(url, timeout=10, headers=HEADERS)
            response.raise_for_status()
            if "Radware" in response.text or "captcha" in response.text.lower():
                print(f"  BLOQUEADO: {url}")
                continue
        except Exception as e:
            print(f"  ERRO: {e}")
            continue

        # registra PDF
        if is_pdf_by_extension(url) or "application/pdf" in response.headers.get("Content-Type", ""):
            size_kb = len(response.content) / 1024
            pdfs_found.append({"url": url, "size_kb": round(size_kb, 2)})
            print(f"  PDF encontrado: {round(size_kb, 2)} KB")
            continue

        if not is_valid_page(url):
            continue

        # registra página HTML (ignora páginas de listagem)
        is_listing = "/page/" in url or "/category/" in url
        if not is_listing:
            pages_found.append(url)

        soup = BeautifulSoup(response.text, "html.parser")
        novos = 0
        for tag in soup.find_all("a", href=True):
            href     = tag["href"]
            full_url = urljoin(url, href).split("#")[0]

            if full_url not in whitelist and should_ignore(full_url):
                continue
            if full_url in visited or full_url in queued:
                continue

            if is_valid_page(full_url):
                queue.append(full_url)
                queued.add(full_url)
                novos += 1
            elif is_gsheet_link(full_url):
                if full_url not in queued:
                    sheets_found.append({"url": full_url, "parent": url})
                    queued.add(full_url)
                    novos += 1
            elif is_drive_link(full_url):
                download_url = build_download_url(full_url)
                if download_url not in queued:
                    pdfs_found.append({"url": download_url, "size_kb": 0, "parent": url})
                    queued.add(download_url)
                    novos += 1
            elif is_pdf_by_extension(full_url):
                queue.append(full_url)
                queued.add(full_url)
                novos += 1

        print(f"  {novos} novos links adicionados à fila")
        time.sleep(0.3)

    # salva
    PAGES_PATH.write_text(json.dumps(pages_found,   ensure_ascii=False, indent=2), encoding="utf-8")
    PDFS_PATH.write_text(json.dumps(pdfs_found,     ensure_ascii=False, indent=2), encoding="utf-8")
    SHEETS_PATH.write_text(json.dumps(sheets_found, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSalvo: {len(pages_found)} páginas, {len(pdfs_found)} PDFs e {len(sheets_found)} planilhas")

    return pages_found, pdfs_found, sheets_found

# ── funções de extração de data ────────────────────────────────────────────────

def extract_date_from_url(url):
    match = DATE_URL_PATTERN.search(url)
    return match.group(1) if match else None

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
    # datacao do proprio documento primeiro: url propria, depois llm no texto proprio
    date = extract_date_from_url(doc["source_url"])
    if date:
        return date, "url"

    text = doc.get("text", "")
    if text:
        for attempt in range(max_retries):
            try:
                date = extract_date_from_text(text)
                break
            except Exception as e:
                wait = 10 * (attempt + 1)
                print(f"  Tentativa {attempt+1}/{max_retries} falhou. Aguardando {wait}s...")
                time.sleep(wait)
        else:
            raise Exception(f"Rate limit persistente após {max_retries} tentativas: {doc['source_url']}")
        if date:
            return date, "llm"

    # ultimo recurso: herda a data da pagina pai (lookup no que ja foi datado, sem fetch)
    parent = doc.get("parent", "")
    if parent:
        date = extract_date_from_url(parent) or (parent_dates or {}).get(parent)
        if date:
            return date, "pai"

    return None, "sem_data"

# ── funções do parser HTML ─────────────────────────────────────────────────────

def parse_html_page(url, headers):
    try:
        response = requests.get(url, timeout=10, headers=headers)
        response.raise_for_status()
    except Exception as e:
        print(f"  ERRO: {e}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    title_tag = soup.find("title")
    title     = title_tag.get_text(strip=True) if title_tag else ""

    main = soup.find("main", role="main")
    if not main:
        return None

    # remove ruídos
    for tag in main.find_all("div", class_="ultimos-posts"):
        tag.decompose()
    for tag in main.find_all("ul", class_="crunchify-social"):
        tag.decompose()
    for tag in main.find_all("a", class_="sr-only"):
        tag.decompose()

    text = main.get_text(separator="\n", strip=True)
    return {"source_url": url, "title": title, "text": text}

def run_html_parser(pages_found):
    print("\n" + "="*60)
    print("FASE 2 — PARSER HTML")
    print("="*60)

    PARSED_DIR.mkdir(parents=True, exist_ok=True)

    if not REPARSE and PAGES_PARSED_PATH.exists():
        results       = json.loads(PAGES_PARSED_PATH.read_text(encoding="utf-8"))
        already_parsed = {r["source_url"] for r in results}
        print(f"Já parseadas: {len(results)} páginas")
    else:
        results        = []
        already_parsed = set()

    errors = []
    for i, url in enumerate(pages_found):
        if url in already_parsed:
            continue
        print(f"[{i+1}/{len(pages_found)}] {url}")
        result = parse_html_page(url, HEADERS)
        if result:
            results.append(result)
        else:
            errors.append(url)
        time.sleep(0.3)

    PAGES_PARSED_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nParsed: {len(results)} páginas | Erros: {len(errors)}")

    # enriquecimento de datas
    print("\nEnriquecendo datas dos HTMLs...")
    pages_parsed = json.loads(PAGES_PARSED_PATH.read_text(encoding="utf-8"))
    sem_data     = [p for p in pages_parsed if "published_at" not in p]
    print(f"HTMLs sem data: {len(sem_data)} de {len(pages_parsed)}")

    for i, doc in enumerate(sem_data):
        try:
            date, source = get_published_at(doc)
            if date:
                doc["published_at"] = date
                doc["date_source"]  = source
            print(f"[{i+1}/{len(sem_data)}] {source or 'sem data'} — {date or 'N/A'}")
        except Exception as e:
            print(f"  PAROU: {e}")
            PAGES_PARSED_PATH.write_text(json.dumps(pages_parsed, ensure_ascii=False, indent=2), encoding="utf-8")
            break
        if (i + 1) % SAVE_INTERVAL == 0:
            PAGES_PARSED_PATH.write_text(json.dumps(pages_parsed, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  Checkpoint salvo: {i+1} processados")

    PAGES_PARSED_PATH.write_text(json.dumps(pages_parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Enriquecimento de HTMLs concluído.")
    return pages_parsed

# ── funções do parser PDF ──────────────────────────────────────────────────────

def is_drive_url(url):
    return "drive.google.com" in url

def download_pdf_bytes(url, headers):
    if is_drive_url(url):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            gdown.download(url, tmp_path, quiet=True)
            with open(tmp_path, "rb") as f:
                return f.read()
        except Exception as e:
            print(f"  ERRO gdown: {e}")
            return None
        finally:
            os.remove(tmp_path)
    try:
        response = requests.get(url, timeout=30, headers=headers)
        response.raise_for_status()
        return response.content
    except Exception as e:
        print(f"  ERRO download: {e}")
        return None

def is_schedule_pdf(title):
    return "Horários_" in title or "Horarios_" in title

def structure_schedule_text(text):
    prompt = f"""Extraia as informações de professor e disciplina deste horário em frases simples. Adicione o ano primeiro
                Siga EXATAMENTE este formato, uma frase por linha, sem texto adicional:

                Ano documento: 2026
                Professor X leciona Disciplina Y na Sala Z no Curso W semestre N.

                Exemplo:
                Ano documento: 2026
                Rafael Pinto leciona Estrutura de Dados no LAB E10 (INF) no TADS 3º semestre.
                Márcio Bigolin leciona Desenvolvimento Web II no LAB D10 (INF) no TADS 5º semestre.

                Não escreva nada além das frases no formato acima.

                {text}"""
    for attempt in range(3):
        try:
            response = google_client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
                config={"temperature": 0.6}
            )
            content = response.text
            if content is None:
                return text
            return content.strip()
        except Exception as e:
            wait = 30 * (attempt + 1)
            print(f"  ERRO estruturação (tentativa {attempt+1}/3): {e}")
            print(f"  Aguardando {wait}s...")
            time.sleep(wait)
    return text

def parse_pdf(pdf_info, headers):
    url     = pdf_info["url"]
    content = download_pdf_bytes(url, headers)
    if content is None:
        return None
    if not content.startswith(b"%PDF"):
        print(f"  NÃO É PDF: {url}")
        return {"source_url": url, "format_error": True}
    try:
        doc   = fitz.open(stream=content, filetype="pdf")
        title = doc.metadata.get("title", "").strip()
        text  = ""
        for page in doc:
            text += page.get_text()
        doc.close()
    except Exception as e:
        print(f"  ERRO parse: {e}")
        return None

    is_scanned = len(text.strip()) < MIN_CHARS

    if not is_scanned and is_schedule_pdf(title):
        print(f"  Estruturando horário...")
        text = structure_schedule_text(text)
        print(text)

    return {
        "source_url": url,
        "title":      title,
        "text":       text.strip() if not is_scanned else "",
        "is_scanned": is_scanned,
        "size_kb":    pdf_info["size_kb"],
        "parent":     pdf_info.get("parent", "")
    }

def run_pdf_parser(pdfs_found):
    print("\n" + "="*60)
    print("FASE 3 — PARSER PDF")
    print("="*60)

    PARSED_DIR.mkdir(parents=True, exist_ok=True)

    # carrega erros de formato conhecidos
    format_errors = set(json.loads(FORMAT_ERRORS_PATH.read_text(encoding="utf-8"))) if FORMAT_ERRORS_PATH.exists() else set()

    if not REPARSE and PDFS_PARSED_PATH.exists():
        pdf_results        = json.loads(PDFS_PARSED_PATH.read_text(encoding="utf-8"))
        already_parsed_pdfs = {r["source_url"] for r in pdf_results}
        print(f"Já parseados: {len(pdf_results)} PDFs")
    else:
        pdf_results         = []
        already_parsed_pdfs = set()

    pdf_errors = []
    for i, pdf_info in enumerate(pdfs_found):
        url = pdf_info["url"]
        if url in already_parsed_pdfs or url in format_errors:
            continue
        print(f"[{i+1}/{len(pdfs_found)}] {url}")
        result = parse_pdf(pdf_info, HEADERS)
        if result:
            if result.get("format_error"):
                format_errors.add(url)
            else:
                pdf_results.append(result)
        else:
            pdf_errors.append(url)

    PDFS_PARSED_PATH.write_text(json.dumps(pdf_results,       ensure_ascii=False, indent=2), encoding="utf-8")
    FORMAT_ERRORS_PATH.write_text(json.dumps(list(format_errors), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nParsed: {len(pdf_results)} PDFs | Erros: {len(pdf_errors)}")
    print(f"Escaneados: {sum(1 for r in pdf_results if r['is_scanned'])}")

    # enriquecimento de datas
    print("\nEnriquecendo datas dos PDFs...")
    pdfs_parsed  = json.loads(PDFS_PARSED_PATH.read_text(encoding="utf-8"))
    sem_data_pdf = [p for p in pdfs_parsed if "published_at" not in p and not p.get("is_scanned")]
    print(f"PDFs sem data: {len(sem_data_pdf)} de {len(pdfs_parsed)}")

    # indice de datas das paginas ja parseadas, para PDFs orfaos herdarem do pai sem novo fetch
    parent_dates = {}
    if PAGES_PARSED_PATH.exists():
        for p in json.loads(PAGES_PARSED_PATH.read_text(encoding="utf-8")):
            if p.get("published_at"):
                parent_dates[p["source_url"]] = p["published_at"]

    for i, doc in enumerate(sem_data_pdf):
        date, source = get_published_at(doc, parent_dates=parent_dates)
        doc["published_at"] = date
        doc["date_source"]  = source
        print(f"[{i+1}/{len(sem_data_pdf)}] {source or 'sem data'} — {date or 'N/A'}")
        if (i + 1) % SAVE_INTERVAL == 0:
            PDFS_PARSED_PATH.write_text(json.dumps(pdfs_parsed, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  Checkpoint salvo: {i+1} processados")

    PDFS_PARSED_PATH.write_text(json.dumps(pdfs_parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Enriquecimento de PDFs concluído.")
    return pdfs_parsed

# ── funções do parser de planilhas (Google Sheets publicados) ───────────────────

def download_sheet_csv(url):
    try:
        response = requests.get(gsheet_csv_url(url), timeout=30, headers=HEADERS)
        response.raise_for_status()
        response.encoding = "utf-8"
        return response.text
    except Exception as e:
        print(f"  ERRO download planilha: {e}")
        return None

def structure_sheet_text(csv_text):
    prompt = f"""Este e o conteudo CSV de uma planilha do IFRS Campus Canoas. Converta cada linha de dados em frases simples e completas, uma por linha, sem texto adicional.
                Comece com o titulo/assunto da planilha e o ano, se houver.
                Nao repita cabecalhos. Ignore linhas vazias. Preserve nomes, salas, e-mails, dias e horarios exatamente como estao.

                Exemplo de saida:
                Horarios de atendimento ao aluno.
                Aline Noimann atende na terca das 15h as 17h e na quarta das 15h as 16h, sala F113, email aline.noimann@canoas.ifrs.edu.br.

                CSV:
                {csv_text}"""
    for attempt in range(3):
        try:
            response = google_client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
                config={"temperature": 0.3}
            )
            content = response.text
            return content.strip() if content else None
        except Exception as e:
            wait = 30 * (attempt + 1)
            print(f"  ERRO estruturação planilha (tentativa {attempt+1}/3): {e}")
            time.sleep(wait)
    return None

def resolve_sheet_date(url, parent, csv_text):
    # cadeia de fallback: url da planilha, url pai, llm no texto do pai, llm no csv, senao None
    date = extract_date_from_url(url) or (extract_date_from_url(parent) if parent else None)
    if date:
        return date
    if parent:
        parent_page = parse_html_page(parent, HEADERS)
        if parent_page and parent_page.get("text"):
            date = extract_date_from_text(parent_page["text"])
            if date:
                return date
    return extract_date_from_text(csv_text)

def run_sheets_parser(sheets_found):
    print("\n" + "="*60)
    print("FASE 3.5 — PARSER PLANILHAS")
    print("="*60)

    PARSED_DIR.mkdir(parents=True, exist_ok=True)

    if not REPARSE and SHEETS_PARSED_PATH.exists():
        results = json.loads(SHEETS_PARSED_PATH.read_text(encoding="utf-8"))
        already_parsed = {r["source_url"] for r in results}
        print(f"Já parseadas: {len(results)} planilhas")
    else:
        results = []
        already_parsed = set()

    for i, item in enumerate(sheets_found):
        url    = item["url"]
        parent = item.get("parent", "")
        if url in already_parsed:
            continue
        print(f"[{i+1}/{len(sheets_found)}] {url}")
        csv_text = download_sheet_csv(url)
        if not csv_text:
            print(f"  FALHA: download da planilha retornou vazio"); SHEETS_FALHAS.append(url); continue
        text = structure_sheet_text(csv_text)
        if not text:
            print(f"  FALHA: estruturacao da planilha retornou vazio"); SHEETS_FALHAS.append(url); continue
        results.append({
            "source_url":   url,
            "title":        "",
            "text":         text,
            "published_at": resolve_sheet_date(url, parent, csv_text),
        })

    SHEETS_PARSED_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nParsed: {len(results)} planilhas")
    return results

# ── funções do chunker ─────────────────────────────────────────────────────────

splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=int(CHUNK_SIZE * 0.2),
    length_function=len,
)

def chunk_document(text, metadata):
    if len(text) <= CHUNK_SIZE:
        return [{"text": text, **metadata}]
    parts = splitter.split_text(text)
    return [{"text": part, **metadata} for part in parts]

def to_drive_view_url(url):
    if "drive.google.com/uc" in url:
        match = re.search(r"id=([a-zA-Z0-9_-]+)", url)
        if match:
            return f"https://drive.google.com/file/d/{match.group(1)}/view"
    return url

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
            "published_at": page.get("published_at")
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
            "published_at": pdf.get("published_at")
        }
        chunks.extend(chunk_document(pdf["text"], metadata))

    # processa planilhas (Google Sheets estruturados em frases)
    for sheet in sheets_parsed:
        metadata = {
            "source_url":   sheet["source_url"],
            "title":        sheet["title"],
            "type":         "sheet",
            "published_at": sheet.get("published_at")
        }
        chunks.extend(chunk_document(sheet["text"], metadata))

    CHUNKS_PATH.write_text(json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Total de chunks: {len(chunks)}")
    print(f"Salvo: {len(chunks)} chunks em {CHUNKS_PATH}")
    return chunks

# ── funções de ingestão ────────────────────────────────────────────────────────

def get_existing_urls():
    existing = set()
    cursor   = ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for v in res.vectors:
            existing.add(v.metadata["source_url"])
        cursor = res.next_cursor
        if cursor == "":
            break
    return existing

def ingest_chunks(chunks, batch_size=100, start_id=0):
    total = len(chunks)
    for i in range(0, total, batch_size):
        batch = chunks[i:i + batch_size]
        texts = [c["text"] for c in batch]

        # embeda com retry
        for attempt in range(5):
            try:
                result = google_client.models.embed_content(
                    model="gemini-embedding-001",
                    contents=texts
                )
                break
            except Exception as e:
                if "429" in str(e):
                    wait = 30 * (attempt + 1)
                    print(f"  Rate limit, aguardando {wait}s...")
                    time.sleep(wait)
                else:
                    raise e

        vectors = []
        for j, (chunk, embedding) in enumerate(zip(batch, result.embeddings)):
            vectors.append((
                str(start_id + i + j),
                embedding.values,
                {
                    "text":         chunk["text"],
                    "source_url":   chunk["source_url"],
                    "title":        chunk["title"],
                    "type":         chunk["type"],
                    "published_at": chunk.get("published_at")
                }
            ))

        index.upsert(vectors=vectors)
        print(f"Inseridos {min(i + batch_size, total)}/{total} chunks")

def run_ingest(chunks):
    print("\n" + "="*60)
    print("FASE 5 — INGESTÃO NO UPSTASH")
    print("="*60)

    if REINGEST:
        index.reset()
        print("Index zerado.")
        start_id   = 0
        new_chunks = chunks
    else:
        existing_urls = get_existing_urls()
        new_chunks    = [c for c in chunks if c["source_url"] not in existing_urls]
        start_id      = index.info().vector_count
        print(f"Chunks novos a inserir: {len(new_chunks)}")

    ingest_chunks(new_chunks, start_id=start_id)
    print("Ingestão concluída.")

# ── execução principal ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    inicio = datetime.now()
    print(f"Pipeline iniciado em {inicio.strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"Anos válidos: {sorted(ANOS_VALIDOS)}")

    pages_found, pdfs_found, sheets_found = run_crawler()
    pages_parsed            = run_html_parser(pages_found)
    pdfs_parsed             = run_pdf_parser(pdfs_found)
    sheets_parsed           = run_sheets_parser(sheets_found)
    chunks                  = run_chunker(pages_parsed, pdfs_parsed, sheets_parsed)
    run_ingest(chunks)

    # regenera o snapshot estatico da pagina servido pelo app
    print("\n" + "="*60)
    print("FASE 6 — SNAPSHOT DA PÁGINA")
    print("="*60)
    try:
        clone_page()
    except Exception as e:
        print(f"Falha ao gerar snapshot (ingestao ja concluida): {e}")

    # DEBUG: residuo de planilhas que falharam no parse
    if SHEETS_FALHAS:
        print(f"\n[RESIDUO] {len(SHEETS_FALHAS)} planilha(s) falharam:")
        for u in SHEETS_FALHAS:
            print(f"  {u}")

    fim = datetime.now()
    print(f"\nPipeline concluído em {fim.strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"Tempo total: {fim - inicio}")