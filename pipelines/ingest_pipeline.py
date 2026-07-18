"""
Pipeline de ingestão do IFRS RAG (orquestrador).
Executa sequencialmente: crawler > parser HTML > parser PDF > parser planilhas >
chunker > ingest > snapshot. Cada fase vive no seu módulo (pipelines/crawler.py,
parser_html.py, parser_pdf.py, parser_sheets.py, chunker.py, ingest.py), com a
configuração compartilhada em pipelines/config.py.

O crawler sempre varre o site inteiro e detecta mudança pelo source_hash do conteúdo
bruto baixado (HTML: texto do main; PDF: bytes; planilha: CSV), comparado ao gravado no
metadata do Upstash. Só o que é novo ou mudou segue para parse (LLM) e ingest (embed);
página mudada tem os chunks antigos substituídos (replace por id determinístico url#i).

Flags de controle:
  REPARSE         = True > ignora os hashes e reprocessa tudo (replace geral, sem zerar o index)
  REINGEST        = True > zera o index e reingere tudo do zero
  INCLUDE_SCANNED = True > baixa/processa PDFs escaneados (imagem). False (default) pula o
                    download dos ja conhecidos como escaneados. Deixar True com OCR/pixelrag.

  Default (REPARSE/REINGEST False) = incremental por source_hash.
"""

import sys
from datetime import datetime
from pathlib import Path

# permite importar pacotes do projeto ao rodar como script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipelines.config import ANOS_VALIDOS, REINGEST, SHEETS_FALHAS
from pipelines.crawler import run_crawler
from pipelines.parser_html import run_html_parser
from pipelines.parser_pdf import run_pdf_parser
from pipelines.parser_sheets import run_sheets_parser
from pipelines.chunker import run_chunker
from pipelines.ingest import carregar_estado, run_ingest
from pipelines.gerar_cursos_atuais import gerar as gerar_cursos_atuais
from ui.clone_page import clone_page


def main():
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


if __name__ == "__main__":
    main()
