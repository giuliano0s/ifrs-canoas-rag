"""
Pipeline de ingestão do IFRS RAG.
Executa sequencialmente: crawler > parser HTML > parser PDF > chunker > ingest > snapshot.

O crawler sempre varre o site inteiro e detecta mudança pelo source_hash do conteúdo
bruto baixado (HTML: texto do main; PDF: bytes; planilha: CSV), comparado ao gravado no
metadata do Upstash. Só o que é novo ou mudou segue para parse (LLM) e ingest (embed);
página mudada tem os chunks antigos substituídos (replace por id determinístico url#i).

Flags de controle:
  REPARSE   = True > ignora os hashes e reprocessa tudo (replace geral, sem zerar o index)
  REINGEST  = True > zera o index e reingere tudo do zero

  Default (ambas False) = incremental por source_hash.

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
from pipelines.gerar_cursos_atuais import gerar as gerar_cursos_atuais
from pipelines.hashing import source_hash

load_dotenv()

# ── variáveis globais ──────────────────────────────────────────────────────────

# anos válidos do crawler; override opcional via env ANOS_VALIDOS (CSV, ex: "2026" ou "2025,2026")
_anos_env    = os.getenv("ANOS_VALIDOS")
ANOS_VALIDOS = {int(a) for a in _anos_env.split(",") if a.strip()} if _anos_env else set(range(2025, 2027))

def _flag_env(nome, default):
    # le uma flag booleana da env (CSV "True"/"False"), preservando o default se nao setada
    valor = os.getenv(nome)
    return valor.strip().lower() == "true" if valor else default

# flags de controle de execução; override opcional via env (ex: $env:REPARSE="True")
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

def run_crawler(estado):
    print("\n" + "="*60)
    print("FASE 1 — CRAWLER (descobre + detecta mudanca por source_hash)")
    print("="*60)

    RAW_DIR.mkdir(parents=True, exist_ok=True)

    visited = set()
    queue   = deque([BASE_URL])
    queued  = set([BASE_URL])

    # whitelist de URLs forcadas
    whitelist = set()
    if WHITELIST_PATH.exists():
        for line in WHITELIST_PATH.read_text(encoding="utf-8").splitlines():
            u = line.strip()
            if u and not u.startswith("#"):
                whitelist.add(u)
                if u not in queued:
                    queue.append(u); queued.add(u)
        print(f"Whitelist: {len(whitelist)} URLs forçadas")

    # listas de descoberta (referencia) e conjunto dirty (novo+mudado, ja com o conteudo baixado)
    pages_all, pdfs_all, sheets_all       = [], [], []
    pages_dirty, pdfs_dirty, sheets_dirty = [], [], []
    sheet_cands, drive_cands              = [], []
    urls_mudadas = set()
    st_html = {"nova": 0, "mudada": 0, "inalterada": 0}
    st_pdfd = {"nova": 0, "mudada": 0, "inalterada": 0}

    # BFS: baixa cada URL, segue os links (acha filhos novos) e classifica HTML e PDF direto ali mesmo
    while queue:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        print(f"[{len(visited)}/{len(visited)+len(queue)}] {url}")

        try:
            response = requests.get(url, timeout=10, headers=HEADERS)
            response.raise_for_status()
            if "Radware" in response.text or "captcha" in response.text.lower():
                print(f"  BLOQUEADO: {url}")
                continue
        except Exception as e:
            print(f"  ERRO: {e}")
            continue

        # PDF direto: o crawler ja tem os bytes -> hasheia e classifica sem baixar de novo
        if is_pdf_by_extension(url) or "application/pdf" in response.headers.get("Content-Type", ""):
            content = response.content
            pdfs_all.append({"url": url, "parent": ""})
            sh     = source_hash(content)
            chave  = to_drive_view_url(url)
            status = _classificar(chave, sh, estado)
            st_pdfd[status] += 1
            if status != "inalterada":
                pdfs_dirty.append({"url": url, "size_kb": round(len(content)/1024, 2),
                                   "parent": "", "content": content, "source_hash": sh})
                if status == "mudada":
                    urls_mudadas.add(chave)
            continue

        if not is_valid_page(url):
            continue

        soup = BeautifulSoup(response.text, "html.parser")

        # classifica a pagina HTML pelo texto extraido (ignora listagens, que sao so navegacao)
        if not ("/page/" in url or "/category/" in url):
            pages_all.append(url)
            title, text = extract_page_content(soup)
            if text is not None:
                sh     = source_hash(text)
                status = _classificar(url, sh, estado)
                st_html[status] += 1
                if status != "inalterada":
                    pages_dirty.append({"source_url": url, "title": title, "text": text, "source_hash": sh})
                    if status == "mudada":
                        urls_mudadas.add(url)

        # segue os links (sempre, para descobrir filhos novos); coleta planilhas e PDFs do Drive
        novos = 0
        for tag in soup.find_all("a", href=True):
            full_url = urljoin(url, tag["href"]).split("#")[0]
            if full_url not in whitelist and should_ignore(full_url):
                continue
            if full_url in visited or full_url in queued:
                continue
            if is_valid_page(full_url):
                queue.append(full_url); queued.add(full_url); novos += 1
            elif is_gsheet_link(full_url):
                sheet_cands.append({"url": full_url, "parent": url}); queued.add(full_url); novos += 1
            elif is_drive_link(full_url):
                download_url = build_download_url(full_url)
                if download_url not in queued:
                    drive_cands.append({"url": download_url, "parent": url}); queued.add(download_url); novos += 1
            elif is_pdf_by_extension(full_url):
                queue.append(full_url); queued.add(full_url); novos += 1
        print(f"  {novos} novos links")
        time.sleep(0.3)

    print(f"\nHTML: {st_html['nova']} novas, {st_html['mudada']} mudadas, {st_html['inalterada']} inalteradas")
    print(f"PDF direto: {st_pdfd['nova']} novas, {st_pdfd['mudada']} mudadas, {st_pdfd['inalterada']} inalteradas")

    # pos-passo: baixa, hasheia e classifica os PDFs do Drive e as planilhas (nao baixados no BFS)
    print(f"\nClassificando {len(drive_cands)} PDFs do Drive e {len(sheet_cands)} planilhas...")
    st_pdf = {"nova": 0, "mudada": 0, "inalterada": 0}
    for cand in drive_cands:
        url, parent = cand["url"], cand["parent"]
        pdfs_all.append({"url": url, "parent": parent})
        content = download_pdf_bytes(url, HEADERS)
        if content is None:
            continue
        sh     = source_hash(content)
        chave  = to_drive_view_url(url)
        status = _classificar(chave, sh, estado)
        st_pdf[status] += 1
        if status != "inalterada":
            pdfs_dirty.append({"url": url, "size_kb": round(len(content)/1024, 2),
                               "parent": parent, "content": content, "source_hash": sh})
            if status == "mudada":
                urls_mudadas.add(chave)

    st_sheet = {"nova": 0, "mudada": 0, "inalterada": 0}
    for cand in sheet_cands:
        url, parent = cand["url"], cand["parent"]
        sheets_all.append({"url": url, "parent": parent})
        csv_text = download_sheet_csv(url)
        if not csv_text:
            SHEETS_FALHAS.append((url, "download")); continue
        sh     = source_hash(csv_text)
        status = _classificar(url, sh, estado)
        st_sheet[status] += 1
        if status != "inalterada":
            sheets_dirty.append({"url": url, "parent": parent, "csv_text": csv_text, "source_hash": sh})
            if status == "mudada":
                urls_mudadas.add(url)

    print(f"PDF Drive: {st_pdf['nova']} novas, {st_pdf['mudada']} mudadas, {st_pdf['inalterada']} inalteradas")
    print(f"Planilhas: {st_sheet['nova']} novas, {st_sheet['mudada']} mudadas, {st_sheet['inalterada']} inalteradas")

    # referencia/debug: URLs descobertas (sem conteudo, os bytes nao vao pro json)
    PAGES_PATH.write_text(json.dumps(pages_all,   ensure_ascii=False, indent=2), encoding="utf-8")
    PDFS_PATH.write_text(json.dumps(pdfs_all,     ensure_ascii=False, indent=2), encoding="utf-8")
    SHEETS_PATH.write_text(json.dumps(sheets_all, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDescobertas: {len(pages_all)} paginas, {len(pdfs_all)} PDFs, {len(sheets_all)} planilhas")
    print(f"Dirty (novo+mudado): {len(pages_dirty)} paginas, {len(pdfs_dirty)} PDFs, {len(sheets_dirty)} planilhas")
    print(f"A substituir (mudadas): {len(urls_mudadas)}")

    return pages_dirty, pdfs_dirty, sheets_dirty, urls_mudadas

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

def extract_page_content(soup):
    # titulo + texto principal (main[role=main] sem ruidos). Retorna (title, text|None).
    # usado pelo crawler (para o source_hash) e pelo parse_html_page, com a MESMA logica.
    title_tag = soup.find("title")
    title     = title_tag.get_text(strip=True) if title_tag else ""
    main = soup.find("main", role="main")
    if not main:
        return title, None
    for tag in main.find_all("div", class_="ultimos-posts"):
        tag.decompose()
    for tag in main.find_all("ul", class_="crunchify-social"):
        tag.decompose()
    for tag in main.find_all("a", class_="sr-only"):
        tag.decompose()
    return title, main.get_text(separator="\n", strip=True)

def parse_html_page(url, headers):
    # baixa e extrai UMA pagina (usado pelo resolve_sheet_date para inferir a data do pai)
    try:
        response = requests.get(url, timeout=10, headers=headers)
        response.raise_for_status()
    except Exception as e:
        print(f"  ERRO: {e}")
        return None
    title, text = extract_page_content(BeautifulSoup(response.text, "html.parser"))
    if text is None:
        return None
    return {"source_url": url, "title": title, "text": text}

def _classificar(url, sh, estado):
    # NOVA (nao existe na base) / MUDADA (source_hash diferente) / INALTERADA (igual).
    # REPARSE ou REINGEST forcam reparse: tratam o que ja existe como MUDADA (replace).
    prev = estado.get(url)
    if prev is None:
        return "nova"
    if REPARSE or REINGEST:
        return "mudada"
    return "inalterada" if prev.get("source_hash") == sh else "mudada"

def run_html_parser(pages_dirty):
    print("\n" + "="*60)
    print("FASE 2 — PARSER HTML (enriquecimento de data; o texto ja veio do crawler)")
    print("="*60)

    PARSED_DIR.mkdir(parents=True, exist_ok=True)
    print(f"HTMLs a processar (novo+mudado): {len(pages_dirty)}")

    # enriquecimento de datas (o crawler ja entregou o texto extraido de cada pagina dirty)
    sem_data = [p for p in pages_dirty if "published_at" not in p]
    print(f"HTMLs sem data: {len(sem_data)} de {len(pages_dirty)}")
    for i, doc in enumerate(sem_data):
        try:
            date, source = get_published_at(doc)
            if date:
                doc["published_at"] = date
                doc["date_source"]  = source
            print(f"[{i+1}/{len(sem_data)}] {source or 'sem data'} — {date or 'N/A'}")
        except Exception as e:
            print(f"  PAROU: {e}")
            break
        if (i + 1) % SAVE_INTERVAL == 0:
            PAGES_PARSED_PATH.write_text(json.dumps(pages_dirty, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  Checkpoint salvo: {i+1} processados")

    PAGES_PARSED_PATH.write_text(json.dumps(pages_dirty, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Enriquecimento de HTMLs concluído.")
    return pages_dirty

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

def is_calendar_pdf(url, text):
    # detecta o calendario academico pela URL ou pelo cabecalho do conteudo
    return "calendario" in url.lower() or "CALENDÁRIO ACADÊMICO" in text[:200].upper() or "CALENDARIO ACADEMICO" in text[:200].upper()

_MESES = ("JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO",
          "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO")

def extract_calendar_text(doc):
    # le os blocos por posicao (top-down, esquerda-direita) e prefixa CADA linha de
    # observacao com o mes/ano da secao corrente, corrigindo a ordem embaralhada do PDF;
    # descarta a grade de dias (linhas so com numeros/dias da semana)
    dias_semana = {"dom", "seg", "ter", "qua", "qui", "sex", "sáb", "sab"}
    saida = []
    secao_atual = None
    for page in doc:
        blocks = sorted(page.get_text("blocks"), key=lambda b: (round(b[1]), round(b[0])))
        for b in blocks:
            bloco = b[4].strip()
            if not bloco:
                continue
            cabecalho = next((m for m in _MESES if bloco.upper().startswith(m)), None)
            if cabecalho:
                secao_atual = " ".join(bloco.replace("|", " ").split())  # ex: "JUNHO 2026"
                continue
            for linha in bloco.splitlines():
                linha = linha.strip()
                # ignora ruido da grade: vazio, so numeros, ou dia da semana
                if not linha or linha.replace(" ", "").isdigit() or linha.lower() in dias_semana:
                    continue
                saida.append(f"({secao_atual}) {linha}" if secao_atual else linha)
    return "\n".join(saida)

def structure_calendar_text(text):
    prompt = f"""Este e o texto de um calendario academico do IFRS Campus Canoas, extraido de PDF (grades de dias misturadas com observacoes por mes).
                Extraia CADA evento datado em uma frase simples, uma por linha, sem texto adicional.
                Cada linha do calendario no formato "DIA - Nome do evento" (ou "DIA a DIA - Nome") pertence ao mes da secao em que aparece. Componha a data completa com dia, mes e ano.
                Comece com o ano do calendario.

                Formato de saida, um por linha:
                Ano do calendario: 2026
                Festa Junina do Campus Canoas: 27 de julho de 2026 (sabado letivo).
                Recesso: 02 a 31 de janeiro de 2026.

                Nao inclua a grade de dias, so os eventos das observacoes. Nao escreva nada alem das frases.

                {text}"""
    for attempt in range(3):
        try:
            response = google_client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
                config={"temperature": 0.3}
            )
            content = response.text
            if content is None:
                return text
            return content.strip()
        except Exception as e:
            wait = 30 * (attempt + 1)
            print(f"  ERRO estruturação calendário (tentativa {attempt+1}/3): {e}")
            time.sleep(wait)
    return text

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

def parse_pdf(pdf_info, content):
    # recebe os bytes ja baixados (o download e o source_hash acontecem no run_pdf_parser)
    url = pdf_info["url"]
    if not content.startswith(b"%PDF"):
        print(f"  NÃO É PDF: {url}")
        return {"source_url": url, "format_error": True}
    try:
        doc   = fitz.open(stream=content, filetype="pdf")
        title = doc.metadata.get("title", "").strip()
        text  = ""
        for page in doc:
            text += page.get_text()
        # calendario: reextrai por blocos posicionais para amarrar evento ao mes correto
        eh_calendario = is_calendar_pdf(url, text)
        calendar_text = extract_calendar_text(doc) if eh_calendario else None
        doc.close()
    except Exception as e:
        print(f"  ERRO parse: {e}")
        return None

    is_scanned = len(text.strip()) < MIN_CHARS
    resultado = {
        "source_url": url,
        "title":      title,
        "is_scanned": is_scanned,
        "size_kb":    pdf_info["size_kb"],
        "parent":     pdf_info.get("parent", "")
    }

    if not is_scanned and is_schedule_pdf(title):
        print(f"  Estruturando horário...")
        text = structure_schedule_text(text)
        print(text)
    elif not is_scanned and eh_calendario:
        print(f"  Estruturando calendário...")
        text = structure_calendar_text(calendar_text)
        # o ano de vigencia do calendario vem do conteudo (URL tem so o mes de publicacao)
        ano = extract_date_from_text(text)
        if ano:
            resultado["published_at"] = ano
            resultado["date_source"]  = "conteudo_calendario"

    resultado["text"] = text.strip() if not is_scanned else ""
    return resultado

def run_pdf_parser(pdfs_dirty, estado):
    print("\n" + "="*60)
    print("FASE 3 — PARSER PDF (parseia os bytes ja baixados pelo crawler)")
    print("="*60)

    PARSED_DIR.mkdir(parents=True, exist_ok=True)
    format_errors = set(json.loads(FORMAT_ERRORS_PATH.read_text(encoding="utf-8"))) if FORMAT_ERRORS_PATH.exists() else set()
    print(f"PDFs a processar (novo+mudado): {len(pdfs_dirty)}")

    results, pdf_errors = [], []
    for i, rec in enumerate(pdfs_dirty):
        url = rec["url"]
        result = parse_pdf(rec, rec["content"])
        if not result:
            pdf_errors.append(url); continue
        if result.get("format_error"):
            format_errors.add(url); continue
        result["source_hash"] = rec["source_hash"]
        results.append(result)
        print(f"[{i+1}/{len(pdfs_dirty)}] {url}")

    FORMAT_ERRORS_PATH.write_text(json.dumps(list(format_errors), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nParsed: {len(results)} PDFs | Erros: {len(pdf_errors)}")
    print(f"Escaneados: {sum(1 for r in results if r.get('is_scanned'))}")

    # enriquecimento de datas (so nos PDFs dirty, nao escaneados)
    print("\nEnriquecendo datas dos PDFs...")
    sem_data_pdf = [p for p in results if "published_at" not in p and not p.get("is_scanned")]
    print(f"PDFs sem data: {len(sem_data_pdf)} de {len(results)}")

    # datas dos pais: da base (estado) + das paginas dirty deste run, para PDFs orfaos herdarem
    parent_dates = {u: e["published_at"] for u, e in estado.items() if e.get("published_at")}
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
            PDFS_PARSED_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  Checkpoint salvo: {i+1} processados")

    PDFS_PARSED_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Enriquecimento de PDFs concluído.")
    return results

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
    prompt = f"""Voce converte planilhas do IFRS Campus Canoas (CSV) em frases simples, uma por linha. Preserve nomes, salas, e-mails, dias e horarios exatamente como no CSV. Comece com o titulo/assunto, se houver.

REGRAS:
- Baseie-se ESTRITAMENTE nas linhas do CSV. Nunca invente dados.
- Os dados do EXEMPLO abaixo sao ficticios e servem so para mostrar o formato. NUNCA os inclua na saida.
- Se o CSV so tiver cabecalhos, virgulas ou nada, responda exatamente: VAZIO

EXEMPLO (ficticio, apenas formato):
CSV de exemplo:
Docente,Sala,Segunda
ZZZ Exemplo,X000,10h as 11h
Saida de exemplo:
ZZZ Exemplo atende na segunda das 10h as 11h, sala X000.

AGORA CONVERTA ESTE CSV:
{csv_text}"""
    for attempt in range(3):
        try:
            response = google_client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
                config={"temperature": 0.2}
            )
            content = (response.text or "").strip()
            # sem dados reais na planilha, o modelo responde VAZIO
            return None if content == "VAZIO" or not content else content
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

def run_sheets_parser(sheets_dirty):
    print("\n" + "="*60)
    print("FASE 3.5 — PARSER PLANILHAS (estrutura o CSV ja baixado pelo crawler)")
    print("="*60)

    PARSED_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Planilhas a processar (novo+mudado): {len(sheets_dirty)}")

    results = []
    for i, rec in enumerate(sheets_dirty):
        url, parent, csv_text = rec["url"], rec["parent"], rec["csv_text"]
        text = structure_sheet_text(csv_text)
        if not text:
            print(f"  IGNORADA: planilha sem dados uteis"); SHEETS_FALHAS.append((url, "sem_dados")); continue
        results.append({
            "source_url":   url,
            "title":        "",
            "text":         text,
            "published_at": resolve_sheet_date(url, parent, csv_text),
            "source_hash":  rec["source_hash"],
        })
        print(f"[{i+1}/{len(sheets_dirty)}] {url}")

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
            "published_at": page.get("published_at"),
            "source_hash":  page.get("source_hash"),
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
        }
        chunks.extend(chunk_document(pdf["text"], metadata))

    # processa planilhas (Google Sheets estruturados em frases)
    for sheet in sheets_parsed:
        metadata = {
            "source_url":   sheet["source_url"],
            "title":        sheet["title"],
            "type":         "sheet",
            "published_at": sheet.get("published_at"),
            "source_hash":  sheet.get("source_hash"),
        }
        chunks.extend(chunk_document(sheet["text"], metadata))

    CHUNKS_PATH.write_text(json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Total de chunks: {len(chunks)}")
    print(f"Salvo: {len(chunks)} chunks em {CHUNKS_PATH}")
    return chunks

# ── funções de ingestão ────────────────────────────────────────────────────────

def carregar_estado():
    # estado do que ja esta no Upstash, por URL: {url: {"source_hash":..., "ids":[...], "published_at":...}}
    # o source_hash e o published_at sao iguais em todos os chunks de uma mesma URL
    estado = {}
    cursor = ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for v in res.vectors:
            m = v.metadata or {}
            u = m.get("source_url", "")
            e = estado.setdefault(u, {"source_hash": m.get("source_hash"), "ids": [], "published_at": m.get("published_at")})
            e["ids"].append(v.id)
        cursor = res.next_cursor
        if cursor == "":
            break
    return estado

def _agrupar_e_anotar(chunks):
    # agrupa chunks por URL e da a cada um o id deterministico url#i (o source_hash ja vem no chunk)
    por_url = {}
    for c in chunks:
        por_url.setdefault(c["source_url"], []).append(c)
    for url, cs in por_url.items():
        for i, c in enumerate(cs):
            c["id"] = f"{url}#{i}"
    return por_url

def ingest_chunks(chunks, batch_size=100):
    # cada chunk ja traz "id" e "source_hash"; embeda e faz upsert
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
        for chunk, embedding in zip(batch, result.embeddings):
            vectors.append((
                chunk["id"],
                embedding.values,
                {
                    "text":         chunk["text"],
                    "source_url":   chunk["source_url"],
                    "title":        chunk["title"],
                    "type":         chunk["type"],
                    "published_at": chunk.get("published_at"),
                    "source_hash":  chunk.get("source_hash"),
                }
            ))

        index.upsert(vectors=vectors)
        print(f"Inseridos {min(i + batch_size, total)}/{total} chunks")

def run_ingest(chunks, estado, urls_mudadas):
    print("\n" + "="*60)
    print("FASE 5 — INGESTÃO NO UPSTASH")
    print("="*60)

    por_url = _agrupar_e_anotar(chunks)
    todos   = [c for cs in por_url.values() for c in cs]

    if REINGEST:
        index.reset()
        print("Index zerado.")
        ingest_chunks(todos)
        print("Ingestão concluída.")
        return

    # replace das paginas mudadas: deleta os chunks antigos antes de inserir os novos.
    # a deteccao (novo / mudou / inalterado) ja aconteceu nos parsers, via source_hash.
    ids_deletar = [i for url in urls_mudadas for i in estado.get(url, {}).get("ids", [])]
    for i in range(0, len(ids_deletar), 1000):
        index.delete(ids=ids_deletar[i:i + 1000])
    if ids_deletar:
        print(f"Removidos {len(ids_deletar)} chunks antigos de {len(urls_mudadas)} paginas mudadas")

    ingest_chunks(todos)
    print(f"Ingestão concluída: {len(por_url)} URLs (novas + mudadas), {len(todos)} chunks")

# ── execução principal ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    inicio = datetime.now()
    print(f"Pipeline iniciado em {inicio.strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"Anos válidos: {sorted(ANOS_VALIDOS)}")

    # estado atual da base (source_hash + ids por URL); vazio quando REINGEST vai zerar tudo
    estado = {} if REINGEST else carregar_estado()
    print(f"Estado do Upstash: {len(estado)} URLs conhecidas")

    # o crawler baixa, hasheia e classifica na fase de crawl; emite so o dirty (novo+mudado) com conteudo
    pages_dirty, pdfs_dirty, sheets_dirty, urls_mudadas = run_crawler(estado)

    pages_parsed  = run_html_parser(pages_dirty)
    pdfs_parsed   = run_pdf_parser(pdfs_dirty, estado)
    sheets_parsed = run_sheets_parser(sheets_dirty)

    chunks        = run_chunker(pages_parsed, pdfs_parsed, sheets_parsed)
    run_ingest(chunks, estado, urls_mudadas)

    # regenera o snapshot estatico da pagina servido pelo app
    print("\n" + "="*60)
    print("FASE 6 — SNAPSHOT DA PÁGINA")
    print("="*60)
    try:
        clone_page()
    except Exception as e:
        print(f"Falha ao gerar snapshot (ingestao ja concluida): {e}")

    # regenera a lista de cursos atuais (usada pelo validador e opcionalmente pelo agente)
    try:
        gerar_cursos_atuais()
    except Exception as e:
        print(f"Falha ao gerar cursos_atuais (ingestao ja concluida): {e}")

    # DEBUG: residuo de planilhas nao ingeridas, com o motivo
    if SHEETS_FALHAS:
        print(f"\n[RESIDUO] {len(SHEETS_FALHAS)} planilha(s) nao ingeridas:")
        for url, motivo in SHEETS_FALHAS:
            print(f"  [{motivo}] {url}")

    fim = datetime.now()
    print(f"\nPipeline concluído em {fim.strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"Tempo total: {fim - inicio}")