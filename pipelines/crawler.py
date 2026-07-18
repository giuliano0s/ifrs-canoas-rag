"""Crawler (fase 1): BFS no site do campus com detecção de mudança por source_hash.

Baixa cada recurso, hasheia o conteúdo bruto (HTML: texto do main; PDF: bytes; planilha:
CSV) e compara com o estado da base: só o dirty (novo+mudado) segue para parse/ingest,
já com o conteúdo baixado. Também deduplica conteúdo idêntico sob URLs diferentes.
"""

import json
import os
import tempfile
import time
from collections import deque
from urllib.parse import urljoin

import gdown
from bs4 import BeautifulSoup

from pipelines.config import (HEADERS, BASE_URL, INCLUDE_SCANNED, PAGES_PATH, PDFS_PATH,
                              RAW_DIR, REINGEST, REPARSE, SCANNED_PATH, SHEETS_FALHAS,
                              SHEETS_PATH, WHITELIST_PATH, fetch)
from pipelines.hashing import source_hash
from pipelines.parser_html import extract_page_content
from pipelines.urls import (build_download_url, gsheet_csv_url, is_drive_link,
                            is_drive_url, is_gsheet_link, is_pdf_by_extension,
                            is_valid_page, should_ignore, to_drive_view_url)


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
        response = fetch(url, timeout=30, headers=headers)
        response.raise_for_status()
        return response.content
    except Exception as e:
        print(f"  ERRO download: {e}")
        return None

def download_sheet_csv(url):
    try:
        response = fetch(gsheet_csv_url(url), timeout=30)
        response.raise_for_status()
        response.encoding = "utf-8"
        return response.text
    except Exception as e:
        print(f"  ERRO download planilha: {e}")
        return None

def _classificar(url, sh, estado):
    # NOVA (nao existe na base) / MUDADA (source_hash diferente) / INALTERADA (igual).
    # REPARSE ou REINGEST forcam reparse: tratam o que ja existe como MUDADA (replace).
    prev = estado.get(url)
    if prev is None:
        return "nova"
    if REPARSE or REINGEST:
        return "mudada"
    return "inalterada" if prev.get("source_hash") == sh else "mudada"

def _duplicata(url, sh, hash_base, vistos_hash):
    # True se este conteudo (source_hash) ja pertence a OUTRA URL, na base ou vista antes neste
    # run: e o MESMO documento sob URL diferente (ex: re-upload "-1" do WordPress). Deve pular.
    # A 1a URL de cada conteudo vira a dona; as repetidas seguintes caem aqui e sao ignoradas.
    dono = hash_base.get(sh) or vistos_hash.get(sh)
    if dono and dono != url:
        return True
    vistos_hash.setdefault(sh, url)
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

    # dedup por conteudo entre URLs distintas: mapa source_hash -> URL dona (na base) mais o
    # acumulador do que ja foi visto neste run. Conteudo repetido sob outra URL (ex: re-upload
    # "-1" do WordPress) e pulado, para o mesmo documento nao inflar o contexto do retrieval.
    hash_base = {}
    for u, e in estado.items():
        if e.get("source_hash"):
            hash_base.setdefault(e["source_hash"], u)
    vistos_hash = {}
    n_dup = 0

    # PDFs ja conhecidos como escaneados: com INCLUDE_SCANNED=False, nem sao baixados de novo
    scanned_conhecidos = set(json.loads(SCANNED_PATH.read_text(encoding="utf-8"))) if SCANNED_PATH.exists() else set()
    if not INCLUDE_SCANNED and scanned_conhecidos:
        print(f"Escaneados conhecidos (pulados sem baixar): {len(scanned_conhecidos)}")

    # BFS: baixa cada URL, segue os links (acha filhos novos) e classifica HTML e PDF direto ali mesmo
    n_erro = n_bloq = 0
    while queue:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        if len(visited) % 100 == 0:
            print(f"  visitadas {len(visited)} | fila {len(queue)} | dirty {len(pages_dirty)+len(pdfs_dirty)} | erros {n_erro+n_bloq}")

        try:
            response = fetch(url, timeout=10)
            response.raise_for_status()
            if "Radware" in response.text or "captcha" in response.text.lower():
                n_bloq += 1
                continue
        except Exception as e:
            n_erro += 1
            continue

        # PDF direto: o crawler ja tem os bytes -> hasheia e classifica sem baixar de novo
        if is_pdf_by_extension(url) or "application/pdf" in response.headers.get("Content-Type", ""):
            content = response.content
            pdfs_all.append({"url": url, "parent": ""})
            sh     = source_hash(content)
            chave  = to_drive_view_url(url)
            if _duplicata(chave, sh, hash_base, vistos_hash):
                n_dup += 1
                continue
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
                sh = source_hash(text)
                if _duplicata(url, sh, hash_base, vistos_hash):
                    n_dup += 1
                else:
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
            elif is_pdf_by_extension(full_url) and (INCLUDE_SCANNED or full_url in whitelist or to_drive_view_url(full_url) not in scanned_conhecidos):
                queue.append(full_url); queued.add(full_url); novos += 1
        time.sleep(0.3)

    print(f"\nCrawl: {len(visited)} URLs visitadas | {n_erro} erros, {n_bloq} bloqueados")
    print(f"HTML: {st_html['nova']} novas, {st_html['mudada']} mudadas, {st_html['inalterada']} inalteradas")
    print(f"PDF direto: {st_pdfd['nova']} novas, {st_pdfd['mudada']} mudadas, {st_pdfd['inalterada']} inalteradas")

    # pos-passo: baixa, hasheia e classifica os PDFs do Drive e as planilhas (nao baixados no BFS)
    print(f"\nClassificando {len(drive_cands)} PDFs do Drive e {len(sheet_cands)} planilhas...")
    st_pdf = {"nova": 0, "mudada": 0, "inalterada": 0}
    for cand in drive_cands:
        url, parent = cand["url"], cand["parent"]
        if not INCLUDE_SCANNED and to_drive_view_url(url) in scanned_conhecidos:
            continue
        pdfs_all.append({"url": url, "parent": parent})
        content = download_pdf_bytes(url, HEADERS)
        if content is None:
            continue
        sh     = source_hash(content)
        chave  = to_drive_view_url(url)
        if _duplicata(chave, sh, hash_base, vistos_hash):
            n_dup += 1
            continue
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
        if _duplicata(url, sh, hash_base, vistos_hash):
            n_dup += 1
            continue
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
    print(f"Duplicatas de conteudo (mesmo hash sob outra URL, puladas): {n_dup}")

    return pages_dirty, pdfs_dirty, sheets_dirty, urls_mudadas
