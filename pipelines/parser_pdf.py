"""Parser PDF (fase 3): texto extraível, detecção de escaneado, e os dois roteamentos
especiais gated — grade de horário lida por VISÃO (pixelrag, um chunk por professor) e
calendário acadêmico estruturado por LLM a partir da extração posicional.
"""

import json
import re
import time

import fitz
from google.genai import types

from pipelines.config import (FORMAT_ERRORS_PATH, MIN_CHARS, PAGES_PARSED_PATH,
                              PARSED_DIR, PDFS_PARSED_PATH, PII_PATH, SAVE_INTERVAL, SCANNED_PATH,
                              google_client)
from pipelines.dates import extract_date_from_text, get_published_at
from pipelines.urls import to_drive_view_url


def is_schedule_pdf(title, text=""):
    # deteccao ROBUSTA da grade, em 3 redes (qualquer uma dispara), para NENHUMA grade escapar:
    #  1) titulo "Horarios_..." (o padrao das grades exportadas do aSc); 2) assinatura do exportador
    #  "aSc TimeTables" no conteudo (checagem EXATA, nao substring "asc", que casaria em "basico" etc.);
    #  3) rede estrutural para grade nao-aSc (detalhe logo abaixo). Grade sem "Horarios_" no titulo
    #  (que antes virava texto cru e published_at=None) e pega e passa pela visao (pixelrag).
    t = text or ""
    if "Horários_" in title or "Horarios_" in title or "aSc TimeTables" in t[:1500]:
        return True
    # rede estrutural (grade sem titulo/assinatura aSc): dias da semana por TOKEN, nao por substring
    # (substring casava "Ter" em "Território", "Qui" em "Aqui" e dava falso-positivo, disparando a
    # visao multimodal a toa); o marcador de criacao no formato exato da grade ("criado:"); e >=3
    # faixas de horario HH:MM. Os tres juntos + o teto de paginas evitam a visao num doc mal-detectado.
    head = t[:2500]
    dias = len(re.findall(r"\b(Seg|Ter|Qua|Qui|Sex|Segunda|Terça|Quarta|Quinta|Sexta)\b", head))
    tem_criado = bool(re.search(r"criado\s*:", head, re.IGNORECASE))
    return dias >= 3 and tem_criado and len(re.findall(r"\d{1,2}:\d{2}", head)) >= 3

def is_calendar_pdf(url, text):
    # calendario academico: exige CONTEUDO de calendario (muitas datas), nao so a palavra no URL.
    # uma resolucao que apenas APROVA o calendario (sem as datas em anexo) tem ~0 datas no corpo; se
    # fosse tratada como calendario, o structure_calendar_text (LLM) ALUCINA datas que nao existem no
    # PDF (foi a origem do "03 de agosto"/"Festa Junina 27 de julho" fantasmas nas resolucoes).
    marcador = ("calendario" in url.lower()
                or "CALENDÁRIO ACADÊMICO" in (text or "")[:300].upper()
                or "CALENDARIO ACADEMICO" in (text or "")[:300].upper())
    n_datas = len(re.findall(r"\b\d{1,2}/\d{1,2}/20\d{2}\b|\b\d{1,2}\s+de\s+[a-zç]+\s+de\s+20\d{2}\b",
                             (text or "").lower()))
    return marcador and n_datas >= 8

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

_SCHED_VISION_PROMPT = (
    "Esta imagem é a grade de horário semanal de uma turma do IFRS Campus Canoas (formato aSc "
    "TimeTables: colunas = dias Seg a Sex; linhas = faixas de horário; cada célula preenchida traz a "
    "DISCIPLINA, o PROFESSOR e a SALA). Leia o cabeçalho da imagem para o curso/turma/turno e o ano/semestre.\n\n"
    "Extraia TODAS as aulas e agrupe POR PROFESSOR. Uma linha por professor, no formato EXATO:\n"
    "Disciplinas do professor NOME (curso/turma T, ANO/SEM): DISCIPLINA (DIA HH:MM-HH:MM, sala SALA); OUTRA (...).\n"
    "Regras: DIA por extenso (Segunda a Sexta); use o horário e a sala da célula; nomes de professor são "
    "pessoas; se uma aula não tiver professor identificável, omita-a. Não escreva nada além das linhas."
)

def _norm_conteudo(s):
    return re.sub(r"\s+", "", (s or "").lower())

def _grade_ano_raw(raw_text, title):
    # ano do RAW, DETERMINISTICO (mais confiavel que regex sobre a saida da visao, que pode alucinar):
    # "Horario criado:DD/MM/AAAA" no cabecalho, ou o ano no titulo "Horarios_AAAA_S".
    m = re.search(r"criado:\s*\d{2}/\d{2}/(20[0-3]\d)", raw_text or "")
    if m:
        return m.group(1)
    m = re.search(r"(20[0-3]\d)", title or "")
    return m.group(1) if m else None

def structure_schedule_vision(doc, max_pages=30):
    # PIXELRAG: renderiza cada pagina da grade e extrai por VISAO (Gemini multimodal), agregando por
    # professor. O layout 2D da grade aSc derrota a extracao de texto (a celula transborda a linha e a
    # coluna funde); a visao le o grid como um humano. GATE anti-alucinacao: horarios/salas emitidos
    # tem que existir no raw da pagina, senao a pagina e DESCARTADA (nao entra fato inventado na base).
    # max_pages=30: as grades reais observadas tem <=10 paginas (margem 3x); e o teto so protege contra
    # um PDF patologico mal-detectado. Se um dia uma grade legitima exceder, o excedente NAO some em
    # silencio: truncado=True marca a grade como visao_parcial (schedule_source, agora persistido).
    # Retorna (texto_estruturado, flags) com contadores de paginas puladas/suspeitas/truncagem.
    linhas, puladas, suspeitas = [], 0, 0
    truncado = doc.page_count > max_pages
    for i, pg in enumerate(doc):
        if i >= max_pages:
            break
        raw_pg = pg.get_text()
        try:
            png = pg.get_pixmap(dpi=200).tobytes("png")
        except Exception as e:
            print(f"  ERRO render grade pag {i}: {e}"); puladas += 1; continue
        out = None
        for attempt in range(3):
            try:
                r = google_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[types.Part.from_bytes(data=png, mime_type="image/png"), _SCHED_VISION_PROMPT],
                    config={"temperature": 0.1},
                )
                out = (r.text or "").strip(); break
            except Exception as e:
                wait = 20 * (attempt + 1)
                print(f"  ERRO visao grade pag {i} (tentativa {attempt+1}/3): {e}; aguardando {wait}s")
                time.sleep(wait)
        if not out:
            puladas += 1; continue
        # GATE: os horarios (HH:MM) e codigos de sala (LAB X, F04...) da saida devem estar no raw
        raw_n = _norm_conteudo(raw_pg)
        toks = re.findall(r"\d{1,2}:\d{2}|LAB\s*[A-Z]?\s*\d+|\b[A-Z]{1,3}\d{2,3}\b", out)
        if toks:
            ok = sum(1 for t in toks if _norm_conteudo(t) in raw_n)
            if ok / len(toks) < 0.75:
                print(f"  grade pag {i}: {100 - ok/len(toks)*100:.0f}% dos tokens da visao fora do raw; DESCARTADA (suspeita de alucinacao)")
                suspeitas += 1; continue
        linhas.append(out)
    return "\n".join(linhas), {"paginas": min(doc.page_count, max_pages), "puladas": puladas,
                               "suspeitas": suspeitas, "truncado": truncado}

# ── gate de PII e quadro de vagas de processo seletivo ───────────────────────────
_INSCR_RX = re.compile(r"\b\d{6,9}\b")
_SEP_WS = re.compile(r"\s+")

def _norm_ws(s):
    return _SEP_WS.sub(" ", (s or "").lower())

def is_pii_nominal_list(text, url="", title=""):
    # gate de PII FAIL-CLOSED: barra lista nominal de candidatos (ensalamento, homologados,
    # classificados). sinal robusto = densidade de numero de inscricao (6-9 digitos): listas reais
    # tem dezenas a centenas; editais 1-2; provas 0-2 (vao vazio na faixa 3-7, medido no corpus).
    # deteccao por CONTEUDO, nao por nome de arquivo, que engana (Campus-Canoas.pdf era ensalamento).
    n_inscr = len(set(_INSCR_RX.findall(text or "")))
    if n_inscr >= 8:
        return True
    marc = re.search(r"homologad|classificad|ensalament|inscri\w* aceitas|resultado|lista|convocac",
                     (url + " " + title).lower())
    return bool(marc) and n_inscr >= 3

def is_vagas_table(title, text, url):
    # quadro de vagas de processo seletivo: marcador "quadro de vagas" na URL/titulo + corpo com
    # secoes de campus e coluna de vagas. GATED como is_schedule_pdf/is_calendar_pdf.
    marc = re.search(r"quadros?[-_ ]de[-_ ]vagas?", (url + " " + title).lower())
    corpo = (text or "")[:6000].lower()
    tem = ("vagas ofertadas" in corpo or "total de vagas" in corpo
           or ("vagas prova" in corpo and "vagas enem" in corpo) or corpo.count("campus ") >= 2)
    return bool(marc) and tem

def _periodo_from_url(url):
    # periodo do processo seletivo = segmento logo APOS o dominio do ingresso (/AAAA-S/ ou /AAAA/),
    # nao a pasta de upload interna (/wp-content/uploads/sites/N/YYYY/MM/, que e data de upload).
    # retorna "AAAA/S" quando ha semestre, "AAAA" quando so ha o ano, ou None. Fallback preserva o
    # comportamento antigo (qualquer /AAAA-S/ na URL) para nao quebrar quem dependia dele.
    u = url or ""
    m = re.search(r"ingresso\.ifrs\.edu\.br/(20\d{2})(?:-(\d))?/", u)
    if m:
        return f"{m.group(1)}/{m.group(2)}" if m.group(2) else m.group(1)
    m = re.search(r"/(20\d{2})-(\d)/", u)
    return f"{m.group(1)}/{m.group(2)}" if m else None

def structure_vagas_table(text, periodo):
    # estrutura o quadro em UMA linha por curso, amarrando cada curso ao campus da sua secao e
    # incluindo o detalhamento completo (total + prova/ENEM por cota). GATE anti-alucinacao: a
    # linha so sobrevive se o par (curso, total) co-ocorrer numa janela do raw (barra numero
    # atribuido ao campus errado, o erro-classe "611 de Bento").
    per = periodo or "atual"
    prompt = f"""Você recebe o texto extraído de um QUADRO DE VAGAS de processo seletivo do IFRS. A tabela 2D foi achatada na extração, mas o texto tem seções por campus ("Campus X:") e, após cada tabela, uma descrição em prosa por curso com o total de vagas e a distribuição por cota.

Para CADA curso, emita UMA ÚNICA linha (sem quebra de linha interna), no formato:
Campus <CAMPUS> - <Curso> (<turno>, <duração>): <TOTAL> vagas ofertadas no Processo Seletivo {per}. Distribuição: <detalhamento completo de vagas por prova e por ENEM, por cota, como está no texto>.

Regras:
- O <CAMPUS> é o da seção onde o curso aparece. NUNCA troque o campus de um curso.
- <TOTAL> é o número total de vagas do curso.
- Inclua o detalhamento completo (vagas por prova e por ENEM, por cota) exatamente como no texto.
- NÃO invente cursos, campi ou números. Uma linha por curso, sem texto adicional.

TEXTO:
{text}"""
    out = ""
    for _ in range(3):
        try:
            r = google_client.models.generate_content(
                model="gemini-2.5-flash-lite", contents=prompt, config={"temperature": 0.0})
            out = (r.text or "").strip(); break
        except Exception as e:
            print(f"  ERRO structure vagas: {e}"); time.sleep(20)
    raw_n = _norm_ws(text)
    linhas_ok = []
    for ln in out.split("\n"):
        ln = ln.strip()
        m = re.match(r"campus\s+(.+?)\s*-\s*(.+?)\s*\(.*?\):\s*(\d+)\s*vagas", ln, re.IGNORECASE)
        if not m:
            continue
        curso_n, total = _norm_ws(m.group(2)), m.group(3)
        toks = [t for t in curso_n.split() if len(t) > 3][:3]
        ok = any(total in raw_n[p.start():p.start() + 400] and all(t in raw_n[p.start():p.start() + 400] for t in toks)
                 for p in re.finditer(re.escape(toks[0]), raw_n)) if toks else False
        if ok:
            linhas_ok.append(ln)
    return "\n".join(linhas_ok)

def parse_pdf(pdf_info, content):
    # recebe os bytes ja baixados (o download e o source_hash acontecem no crawler)
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
        is_scanned = len(text.strip()) < MIN_CHARS
        resultado = {
            "source_url": url,
            "title":      title,
            "is_scanned": is_scanned,
            "size_kb":    pdf_info["size_kb"],
            "parent":     pdf_info.get("parent", ""),
        }
        # gate de PII (fail-closed): lista nominal de candidatos NUNCA entra na base; o texto e
        # descartado (nem fica no resultado). qualquer incerteza pende para barrar, nao para vazar.
        if not is_scanned and is_pii_nominal_list(text, url, title):
            resultado["pii_blocked"] = True
            resultado["text"] = ""
            doc.close()
            return resultado

        # grade de horario -> visao (pixelrag); quadro de vagas -> structurer por registro;
        # calendario -> reextracao posicional. Todos com o doc ainda ABERTO (a visao renderiza paginas).
        if not is_scanned and is_schedule_pdf(title, text):
            resultado["is_schedule"] = True
            ano = _grade_ano_raw(text, title)   # ano deterministico do raw (nao da saida da visao)
            vis_text, flags = structure_schedule_vision(doc)
            if vis_text:
                text = vis_text
                resultado["schedule_source"] = ("visao_parcial"
                    if (flags["puladas"] or flags["suspeitas"] or flags["truncado"]) else "visao")
            else:
                text = structure_schedule_text(text)  # fallback se a visao nao retornar nada
                resultado["schedule_source"] = "fallback_texto"
            if ano:
                resultado["published_at"] = ano
                resultado["date_source"]  = "conteudo_grade"
            print(f"  [GRADE] {url} -> {resultado['schedule_source']} | ano={ano}"
                  + (f" | {flags}" if vis_text else ""))
        elif not is_scanned and is_vagas_table(title, text, url):
            # quadro de vagas: um registro por curso, amarrado ao campus da secao (gate anti-alucinacao)
            resultado["is_vagas"] = True
            periodo = _periodo_from_url(url)
            text = structure_vagas_table(text, periodo)
            if periodo:
                resultado["published_at"] = periodo.split("/")[0]
                resultado["date_source"]  = "periodo_ingresso"
            print(f"  [VAGAS] {url} -> {len(text.splitlines())} cursos | periodo={periodo}")
        elif not is_scanned and is_calendar_pdf(url, text):
            # calendario: reextrai por blocos posicionais para amarrar evento ao mes correto
            text = structure_calendar_text(extract_calendar_text(doc))
            ano = extract_date_from_text(text)
            if ano:
                resultado["published_at"] = ano
                resultado["date_source"]  = "conteudo_calendario"
        doc.close()
    except Exception as e:
        print(f"  ERRO parse: {e}")
        return None

    resultado["text"] = text.strip() if not is_scanned else ""
    return resultado

def run_pdf_parser(pdfs_dirty, estado):
    print("\n" + "="*60)
    print("FASE 3 — PARSER PDF (parseia os bytes ja baixados pelo crawler)")
    print("="*60)

    PARSED_DIR.mkdir(parents=True, exist_ok=True)
    format_errors = set(json.loads(FORMAT_ERRORS_PATH.read_text(encoding="utf-8"))) if FORMAT_ERRORS_PATH.exists() else set()
    scanned       = set(json.loads(SCANNED_PATH.read_text(encoding="utf-8"))) if SCANNED_PATH.exists() else set()
    pii_conhecidos = set(json.loads(PII_PATH.read_text(encoding="utf-8"))) if PII_PATH.exists() else set()
    print(f"PDFs a processar (novo+mudado): {len(pdfs_dirty)}")

    results, pdf_errors, n_scan, n_fmt, pii_urls = [], [], 0, 0, []
    for i, rec in enumerate(pdfs_dirty):
        url = rec["url"]
        result = parse_pdf(rec, rec["content"])
        if not result:
            pdf_errors.append(url); continue
        if result.get("format_error"):
            format_errors.add(url); n_fmt += 1; continue
        if result.get("pii_blocked"):
            # lista nominal de candidatos barrada pelo gate de PII: nao entra na base e e REGISTRADA
            # para o crawler pular o download nos proximos runs (sem chunk, ela nunca ganha
            # source_hash e voltaria como "nova" sempre). o registro guarda so a URL publica.
            pii_urls.append(url); pii_conhecidos.add(to_drive_view_url(url)); continue
        if result.get("is_scanned"):
            # sem texto extraivel: registra para o crawler pular o download nos proximos runs
            scanned.add(to_drive_view_url(url)); n_scan += 1; continue
        result["source_hash"] = rec["source_hash"]
        results.append(result)
        if (i + 1) % 100 == 0:
            print(f"  parseados {i+1}/{len(pdfs_dirty)}")

    # os tres registros sao versionados no git: escrever ORDENADO os torna deterministicos
    # (list(set) reordenava o json a cada run e sujava o commit semanal do CI com diff falso)
    FORMAT_ERRORS_PATH.write_text(json.dumps(sorted(format_errors), ensure_ascii=False, indent=2), encoding="utf-8")
    SCANNED_PATH.write_text(json.dumps(sorted(scanned), ensure_ascii=False, indent=2), encoding="utf-8")
    PII_PATH.write_text(json.dumps(sorted(pii_conhecidos), ensure_ascii=False, indent=2), encoding="utf-8")

    # RELATORIO DE PARSING (numeros + motivos): torna VISIVEL o que NAO entrou e por que. os motivos
    # transitorios (PII barrado, erro de download/parse) nao ficavam persistidos; agora ficam, com
    # amostras, para diagnosticar sem depender do log da run (fecha parte da "degradacao silenciosa").
    por_estrut = {"vaga": 0, "grade": 0, "calendario": 0, "normal": 0}
    for r in results:
        if r.get("is_vagas"):                              por_estrut["vaga"] += 1
        elif r.get("is_schedule"):                         por_estrut["grade"] += 1
        elif r.get("date_source") == "conteudo_calendario": por_estrut["calendario"] += 1
        else:                                              por_estrut["normal"] += 1
    relatorio = {
        "pdfs_processados": len(pdfs_dirty),
        "parseados_ok": len(results),
        "por_estruturacao": por_estrut,
        "nao_entraram": {"escaneado": n_scan, "erro_formato": n_fmt,
                         "pii_barrado": len(pii_urls), "erro_download_ou_parse": len(pdf_errors)},
        "amostras": {"pii_barrado": pii_urls[:15], "erro_download_ou_parse": pdf_errors[:15]},
    }
    (PARSED_DIR / "pdfs_parse_report.json").write_text(
        json.dumps(relatorio, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nParsed: {len(results)} PDFs {por_estrut} | NAO entraram: escaneado={n_scan} "
          f"formato={n_fmt} PII={len(pii_urls)} download/parse={len(pdf_errors)}")
    print(f"  relatorio em {(PARSED_DIR / 'pdfs_parse_report.json')}")

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
        if (i + 1) % SAVE_INTERVAL == 0:
            PDFS_PARSED_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  datas: {i+1}/{len(sem_data_pdf)} (checkpoint salvo)")

    PDFS_PARSED_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Enriquecimento de PDFs concluído.")
    return results
