"""Parser de planilhas (fase 3.5): estrutura o CSV já baixado pelo crawler (Google Sheets
publicados no site) em frases simples via LLM e resolve a data pela cadeia
url -> url do pai -> conteúdo do pai -> conteúdo do CSV.
"""

import json
import time

from pipelines.config import (HEADERS, PARSED_DIR, SHEETS_FALHAS, SHEETS_PARSED_PATH,
                              google_client)
from pipelines.dates import extract_date_from_text, extract_date_from_url
from pipelines.parser_html import parse_html_page


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

    SHEETS_PARSED_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nParsed: {len(results)} planilhas")
    return results
