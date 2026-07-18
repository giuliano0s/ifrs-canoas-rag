"""Parser HTML (fase 2): extração de título+texto do main e enriquecimento de data.

extract_page_content é compartilhada com o crawler: o source_hash de uma página é o hash
do MESMO texto extraído aqui (o HTML cru muda a cada request por causa do nonce anti-bot).
"""

import json

from bs4 import BeautifulSoup

from pipelines.config import HEADERS, PAGES_PARSED_PATH, PARSED_DIR, SAVE_INTERVAL, fetch
from pipelines.dates import get_published_at


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

def parse_html_page(url, headers=HEADERS):
    # baixa e extrai UMA pagina (usado pelo resolve_sheet_date para inferir a data do pai)
    try:
        response = fetch(url, timeout=10, headers=headers)
        response.raise_for_status()
    except Exception as e:
        print(f"  ERRO: {e}")
        return None
    title, text = extract_page_content(BeautifulSoup(response.text, "html.parser"))
    if text is None:
        return None
    return {"source_url": url, "title": title, "text": text}

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
        except Exception as e:
            print(f"  PAROU: {e}")
            break
        if (i + 1) % SAVE_INTERVAL == 0:
            PAGES_PARSED_PATH.write_text(json.dumps(pages_dirty, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  datas: {i+1}/{len(sem_data)} (checkpoint salvo)")

    PAGES_PARSED_PATH.write_text(json.dumps(pages_dirty, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Enriquecimento de HTMLs concluído.")
    return pages_dirty
