"""Parser PDF (fase 3): texto extraível, detecção de escaneado, e os dois roteamentos
especiais gated — grade de horário lida por VISÃO (pixelrag, um chunk por professor) e
calendário acadêmico estruturado por LLM a partir da extração posicional.
"""

import json
import re
import time
import unicodedata

import fitz

from pipelines.config import (FORMAT_ERRORS_PATH, MIN_CHARS, PAGES_PARSED_PATH,
                              PARSED_DIR, PDFS_PARSED_PATH, PII_PATH, SAVE_INTERVAL, SCANNED_PATH)
from pipelines.dates import extract_date_from_text, get_published_at
from pipelines.urls import to_drive_view_url
from rag import llm


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
    # calendario que lista os eventos como "27 - Festa Junina" sob o cabecalho do mes, sem data completa
    n_eventos = len(re.findall(r"(?m)^\s*\d{1,2}(?:\s*(?:a|e|,)\s*\d{1,2})*(?:/\d{1,2})?\s*[-–]\s*\w", text or ""))
    return marcador and (n_datas >= 8 or n_eventos >= 8)

_MESES = ("JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO",
          "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO")
# cabecalho de mes da grade ("JANEIRO | 2026"): o bloco inteiro e so o mes e o ano; um bloco de
# observacoes que comeca pelo nome do mes ("Janeiro a março: matrículas...") nao e cabecalho
_CABECALHO_MES = re.compile(r"(?i)^(" + "|".join(_MESES) + r")\s*\|?\s*(20\d{2})$")
# marcas invisiveis de direcao de texto que alguns PDFs gravam em volta de cada palavra
_MARCAS_DIRECAO = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮⁦⁧⁨⁩"))

def _linhas_da_pagina_com_grade(page):
    """Le uma pagina de calendario com grade de dias pelas coordenadas de cada palavra.

    Alguns PDFs gravam o texto palavra por palavra, e o dia que abre um evento cai noutro bloco
    ("03" num bloco, "a 07- 2ª Etapa" noutro). Remontando cada linha visual pela posicao, o que
    fica a direita da coluna "Sab" da grade e observacao, e a esquerda ficam a grade e o
    cabecalho do mes.

    Args:
        page: pagina do PyMuPDF.

    Returns:
        Lista de (cabecalho do mes ou None, texto da observacao) na ordem de leitura, ou None se a
        pagina nao tem grade de dias (tabelas do fim, paginas de resolucao).
    """
    palavras = [(x0, y0, x1, y1, w.translate(_MARCAS_DIRECAO).strip())
                for x0, y0, x1, y1, w, *_ in page.get_text("words")]
    palavras = [p for p in palavras if p[4]]
    sabados = [p for p in palavras if p[4].lower() in ("sáb", "sab")]
    if not sabados:
        return None
    borda = max(p[2] for p in sabados) + 3

    # linhas visuais: palavras com o centro vertical a ate 3 pt da linha corrente
    linhas, atual, y_atual = [], [], None
    for p in sorted(palavras, key=lambda p: ((p[1] + p[3]) / 2, p[0])):
        y = (p[1] + p[3]) / 2
        if atual and abs(y - y_atual) > 3:
            linhas.append(atual)
            atual = []
        if not atual:
            y_atual = y
        atual.append(p)
    if atual:
        linhas.append(atual)

    # em cada linha, a esquerda da borda pode ser o cabecalho do mes; a direita e observacao
    saida = []
    for linha in linhas:
        linha.sort(key=lambda p: p[0])
        esquerda = " ".join(p[4] for p in linha if p[0] <= borda)
        direita = " ".join(p[4] for p in linha if p[0] > borda)
        cabecalho = _CABECALHO_MES.match(" ".join(esquerda.split()))
        saida.append((f"{cabecalho.group(1).upper()} {cabecalho.group(2)}" if cabecalho else None, direita))
    return saida

def extract_calendar_text(doc):
    # le cada pagina por posicao e prefixa CADA linha de observacao com o mes/ano da secao corrente,
    # corrigindo a ordem embaralhada do PDF; descarta a grade de dias. pagina com grade e lida pelas
    # palavras (_linhas_da_pagina_com_grade); a sem grade, pelos blocos de texto
    dias_semana = {"dom", "seg", "ter", "qua", "qui", "sex", "sáb", "sab"}
    saida = []
    secao_atual = None
    for page in doc:
        por_palavra = _linhas_da_pagina_com_grade(page)
        if por_palavra is not None:
            for cabecalho, texto in por_palavra:
                if cabecalho:
                    secao_atual = cabecalho
                if texto:
                    saida.append(f"({secao_atual}) {texto}" if secao_atual else texto)
            continue
        blocks = sorted(page.get_text("blocks"), key=lambda b: (round(b[1]), round(b[0])))
        for b in blocks:
            bloco = b[4].translate(_MARCAS_DIRECAO).strip()
            if not bloco:
                continue
            cabecalho = _CABECALHO_MES.match(" ".join(bloco.split()))
            if cabecalho:
                secao_atual = f"{cabecalho.group(1).upper()} {cabecalho.group(2)}"  # ex: "JUNHO 2026"
                continue
            for linha in bloco.splitlines():
                linha = linha.strip()
                # ignora ruido da grade: vazio, so numeros, ou dia da semana
                if not linha or linha.replace(" ", "").isdigit() or linha.lower() in dias_semana:
                    continue
                saida.append(f"({secao_atual}) {linha}" if secao_atual else linha)
    return "\n".join(saida)

_NUM_MES = {m.lower(): i + 1 for i, m in enumerate(_MESES)}
_DIAS_DO_EVENTO = re.compile(r"^(\d{1,2}(?:\s*(?:a|e|,)\s*\d{1,2})*)\s*[-–:]")
_FAIXA_BARRA = re.compile(r"\b(\d{1,2})\s*a\s*(\d{1,2})/(\d{1,2})\b")
_DATA_BARRA = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?\b")
_DIAS_DE_MES = re.compile(r"\b(\d{1,2}(?:\s*(?:a|e|,)\s*\d{1,2})*)\s+de\s+(" + "|".join(_NUM_MES) + r")\b")

def _valida(dia, mes):
    return {(int(dia), int(mes))} if 1 <= int(dia) <= 31 and 1 <= int(mes) <= 12 else set()

def _datas_do_cru(cru, ano=None):
    # (dia, mes) de cada data do texto posicional: eventos "(MES AAAA) 05 a 07 - X" levam o mes da
    # secao; faixas "05 a 07/01" e datas "17/02/2026" das tabelas levam o mes escrito, e a faixa que
    # vira o mes ("27 a 10/02") comeca no mes da secao. data com ano anterior ao do calendario (a
    # assinatura da resolucao, linha herdada do calendario anterior) nao e evento dele
    datas = set()
    for linha in cru.split("\n"):
        secao = re.match(r"\((\w+)[^)]*\)\s*(.*)$", linha)
        mes_secao = _NUM_MES.get(secao.group(1).lower()) if secao else None
        corpo = secao.group(2) if secao else linha
        for d1, d2, mes in _FAIXA_BARRA.findall(corpo):
            vira_o_mes = int(d1) > int(d2) and mes_secao
            datas |= _valida(d1, mes_secao if vira_o_mes else mes) | _valida(d2, mes)
        for dia, mes, ano_data in _DATA_BARRA.findall(corpo):
            if not (ano and ano_data and int(ano_data) < ano):
                datas |= _valida(dia, mes)
        evento = _DIAS_DO_EVENTO.match(corpo)
        if evento and mes_secao:
            for dia in re.findall(r"\d{1,2}", evento.group(1)):
                datas |= _valida(dia, mes_secao)
    return datas

def _datas_estruturadas(texto, ano=None):
    # (dia, mes) de cada data da saida do estruturador ("05 a 07 de janeiro", "03 e 04 de fevereiro");
    # linha so com ano anterior ao do calendario fica de fora, como no texto do PDF
    datas = set()
    for linha in texto.lower().split("\n"):
        anos = {int(a) for a in re.findall(r"\b(20\d{2})\b", linha)}
        if ano and anos and max(anos) < ano:
            continue
        for dias, mes in _DIAS_DE_MES.findall(linha):
            for dia in re.findall(r"\d{1,2}", dias):
                datas |= _valida(dia, _NUM_MES[mes])
        for dia, mes, _ in _DATA_BARRA.findall(linha):
            datas |= _valida(dia, mes)
    return datas

def confere_calendario(cru, estruturado):
    """Confere as datas do calendario estruturado contra o texto extraido do PDF.

    Args:
        cru: saida do extract_calendar_text.
        estruturado: saida do LLM, um evento por linha.

    Returns:
        Par (omitidas, inventadas) de listas de (dia, mes): datas do PDF ausentes da saida e
        datas da saida que o PDF nao traz.
    """
    cabecalho = re.match(r"(?i)\s*ano do calend[aá]rio:\s*(20\d{2})", estruturado or "")
    ano = int(cabecalho.group(1)) if cabecalho else None
    no_pdf, na_saida = _datas_do_cru(cru, ano), _datas_estruturadas(estruturado, ano)
    ordem = lambda data: (data[1], data[0])
    return sorted(no_pdf - na_saida, key=ordem), sorted(na_saida - no_pdf, key=ordem)

def _revisao_do_calendario(omitidas, inventadas):
    # devolve ao modelo, na leitura seguinte, as datas que a conferencia apontou
    lista = lambda datas: ", ".join(f"{dia:02d}/{mes:02d}" for dia, mes in datas)
    partes = []
    if omitidas:
        partes.append(f"estas datas estao no documento e faltaram na leitura anterior; inclua o evento de cada uma: {lista(omitidas)}")
    if inventadas:
        partes.append(f"estas datas da leitura anterior NAO estao no documento; corrija pelo documento ou remova a linha: {lista(inventadas)}")
    return "\n\n                Revisao da leitura anterior: " + "; ".join(partes) + "." if partes else ""

def structure_calendar_text(text):
    """Estrutura o calendario em um evento por linha, conferindo as datas contra o texto do PDF.

    Args:
        text: saida do extract_calendar_text.

    Returns:
        Par (texto, conferencia): a melhor de ate 3 leituras (a primeira sem data omitida nem
        inventada, ou a com menos divergencias) e o dict {"omitidas", "inventadas", "leituras"}.
        Sem leitura valida, devolve o texto cru e conferencia None.
    """
    prompt = f"""Este e o texto de um calendario academico do IFRS Campus Canoas, extraido de PDF (grades de dias misturadas com observacoes por mes).
                Extraia CADA evento datado em uma frase simples, uma por linha, sem texto adicional.
                Cada linha do calendario no formato "DIA - Nome do evento" (ou "DIA a DIA - Nome") pertence ao mes da secao em que aparece. Componha a data completa com dia, mes e ano.
                A PRIMEIRA linha da saida e obrigatoriamente "Ano do calendario: AAAA".
                Percorra o documento inteiro: inclua todos os meses, inclusive os do ano seguinte que o calendario trouxer (ex: janeiro e fevereiro do proximo ano). Nao pare em dezembro.
                Inclua tambem as tabelas do fim do documento, uma linha por celula datada: periodo letivo de cada nivel e de cada semestre ou trimestre, matricula online, processamento, homologacao e ajustes de matricula, e a lista de feriados. Nomeie o nivel e o semestre em cada linha.
                Use so as datas escritas no documento; evento com dia a definir ("xx") fica com "data a definir" e o mes.

                Formato de saida, um por linha:
                Ano do calendario: AAAA
                Nome do evento: DD de mes de AAAA.
                Nome do evento: DD a DD de mes de AAAA.

                Nao inclua a grade de dias, so os eventos. Nao escreva nada alem das frases.

                {text}"""
    melhor, conferencia, revisao = None, None, ""
    for attempt in range(3):
        try:
            content = (llm.completar(prompt + revisao, temperatura=0.3, leve=True) or "").strip()
        except Exception as e:
            wait = 30 * (attempt + 1)
            print(f"  ERRO estruturação calendário (tentativa {attempt+1}/3): {e}")
            time.sleep(wait)
            continue
        if not content.lower().startswith("ano do calend"):
            continue

        # fica com a leitura de menos divergencias; para na primeira sem nenhuma
        omitidas, inventadas = confere_calendario(text, content)
        if conferencia is None or len(omitidas) + len(inventadas) < conferencia["omitidas"] + conferencia["inventadas"]:
            melhor = content
            conferencia = {"omitidas": len(omitidas), "inventadas": len(inventadas)}
        conferencia["leituras"] = attempt + 1
        if not omitidas and not inventadas:
            break
        revisao = _revisao_do_calendario(omitidas, inventadas)
    if melhor is None:
        return text, None
    return melhor, conferencia

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
            content = llm.completar(prompt, temperatura=0.6, leve=True)
            if not content:
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
    "Regras: DIA por extenso (Segunda a Sexta), tirado do cabeçalho Seg/Ter/Qua/Qui/Sex da COLUNA onde a célula está "
    "(confira a coluna de cada célula antes de escrever o dia); use o horário e a sala da célula; nomes de professor são "
    "pessoas; se uma aula não tiver professor identificável, omita-a. Não escreva nada além das linhas."
)

def _norm_conteudo(s):
    return re.sub(r"\s+", "", (s or "").lower())

_DIAS_ABREV = {"seg": "segunda", "ter": "terca", "qua": "quarta", "qui": "quinta", "sex": "sexta", "sab": "sabado"}
_AULA_RX = re.compile(r"([^;:()]+?)\s*\((segunda|ter[cç]a|quarta|quinta|sexta|s[aá]bado)\b", re.IGNORECASE)

def _sem_acento(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()

def _dias_conferem(saida, pg):
    # GATE de dia por POSICAO: o gate de horario/sala confere so tokens contra o raw e deixa passar
    # dia trocado. Aqui cada aula emitida (disciplina + dia) e conferida contra a coluna onde a
    # disciplina aparece na pagina, medida pelas coordenadas das palavras e pelo cabecalho Seg..Sex.
    # Sem cabecalho de dias reconhecivel ou sem a disciplina na pagina, nao ha como conferir: passa.
    cabecalho = {}
    palavras = pg.get_text("words")
    for x0, y0, x1, y1, w, *_ in palavras:
        dia = _DIAS_ABREV.get(_sem_acento(w).strip(".:")[:3]) if len(w.strip(".:")) in (3, 5, 6, 7) else None
        if dia and (dia not in cabecalho or y0 < cabecalho[dia][1]):
            cabecalho[dia] = ((x0 + x1) / 2, y0)
    if len(cabecalho) < 3:
        return True
    centros = {dia: x for dia, (x, _) in cabecalho.items()}
    topo = max(y for _, y in cabecalho.values())

    # dias em que cada palavra aparece abaixo do cabecalho (coluna = cabecalho mais proximo em x)
    dias_da_palavra = {}
    for x0, y0, x1, y1, w, *_ in palavras:
        if y0 <= topo:
            continue
        chave = _sem_acento(w).strip(".,:;")
        dia = min(centros, key=lambda d: abs(centros[d] - (x0 + x1) / 2))
        dias_da_palavra.setdefault(chave, set()).add(dia)

    # cada aula e conferida pela palavra mais longa do nome que esta na pagina e que nao aparece em
    # outra disciplina da mesma saida ("Fís" de "Fís I" e de "Ed Fís I" e ambigua e nao serve)
    aulas = [(disciplina.strip(), dia) for disciplina, dia in _AULA_RX.findall(saida)]
    tokens_de = {d: {_sem_acento(t).strip(".,:;") for t in d.split()} for d, _ in aulas}
    for disciplina, dia in aulas:
        outras = set().union(*(tk for d, tk in tokens_de.items() if d != disciplina))
        candidatos = [t for t in tokens_de[disciplina] if len(t) >= 3 and t in dias_da_palavra and t not in outras]
        if not candidatos:
            continue
        token = max(candidatos, key=len)
        if _sem_acento(dia)[:5] not in {d[:5] for d in dias_da_palavra[token]}:
            return False
    return True

_DIA_EXTENSO = {"segunda": "Segunda", "terca": "Terça", "quarta": "Quarta", "quinta": "Quinta",
                "sexta": "Sexta", "sabado": "Sábado"}
_FAIXA_RX = re.compile(r"^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$")

def _textos_fora_das_celulas(spans, grupos, esquerda, topo, dias, faixas):
    """Lista os textos da area da grade que nenhuma celula lida cobriu.

    A area vai da coluna de rotulos de horario ate meia coluna depois de "Sex", e do cabecalho ate
    meia faixa depois do ultimo rotulo: sai do cabecalho e dos rotulos, nao das celulas, para nao
    encolher quando o desenho das celulas some. Ficam de fora a linha sem aula (rotulo da esquerda
    que nao e faixa de horario, como "Intervalo Manhã/Tarde") e a assinatura do gerador.

    Args:
        spans: (texto, tamanho, Rect) de todos os textos da pagina.
        grupos: celulas lidas, cada uma com os spans que entraram nela.
        esquerda: borda direita da coluna de rotulos de horario.
        topo: borda de baixo do cabecalho Seg..Sex.
        dias: Rect de cada dia do cabecalho.
        faixas: ((inicio, fim), y do rotulo) de cada faixa de horario.

    Returns:
        Lista de textos orfaos; vazia quando toda a grade foi lida.
    """
    centros = sorted((r.x0 + r.x1) / 2 for r in dias.values())
    coluna = (centros[-1] - centros[0]) / (len(centros) - 1)
    alturas = sorted(y for _, y in faixas)
    passo = (alturas[-1] - alturas[0]) / (len(alturas) - 1)
    direita, fundo = centros[-1] + coluna / 2, alturas[-1] + passo / 2
    linhas_sem_aula = [(r.y0 + r.y1) / 2 for t, _, r in spans
                       if r.x0 < esquerda and (r.y0 + r.y1) / 2 > topo and not t.isdigit() and not _FAIXA_RX.match(t)]
    usados = {(t, round(r.x0), round(r.y0)) for g in grupos for t, _, r in g["spans"].values()}

    # texto que atravessa a borda da propria celula: o desenho esta desalinhado do texto
    orfaos = [t for g in grupos for t, _, r in g["spans"].values()
              if r.x0 < g["rect"].x0 - 2 or r.x1 > g["rect"].x1 + 2 or r.y0 < g["rect"].y0 - 2 or r.y1 > g["rect"].y1 + 2]
    for texto, _, r in spans:
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        if not (esquerda < cx < direita and topo < cy < fundo) or texto == "aSc TimeTables":
            continue
        if (texto, round(r.x0), round(r.y0)) in usados or any(abs(cy - y) < passo / 2 for y in linhas_sem_aula):
            continue
        orfaos.append(texto)
    return orfaos

def ler_grade_asc(pg):
    # le uma pagina de grade aSc TimeTables pelas COORDENADAS, sem visao: cada celula e um retangulo
    # colorido; o dia e a coluna do cabecalho Seg..Sex mais proxima do centro da celula; o horario
    # vem das faixas da coluna da esquerda cujo rotulo cai dentro da celula; dentro da celula, a fonte
    # maior e a disciplina, a menor e o professor e a do meio e a sala. Devolve as linhas no formato
    # da visao (uma por professor) ou None quando a pagina nao tem a estrutura reconhecivel
    spans = [(s["text"].strip(), s["size"], fitz.Rect(s["bbox"]))
             for b in pg.get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"] if s["text"].strip()]
    dias = {}
    for texto, _, r in spans:
        dia = _DIAS_ABREV.get(_sem_acento(texto)[:3]) if len(texto) == 3 else None
        if dia and (dia not in dias or r.y0 < dias[dia].y0):
            dias[dia] = r
    if len(dias) < 5:
        return None
    topo = max(r.y1 for r in dias.values())
    esquerda = min(r.x0 for r in dias.values())
    rotulos = [(t, r) for t, _, r in spans if _FAIXA_RX.match(t) and r.x1 < esquerda]
    if len(rotulos) < 3:
        return None
    faixas = [(_FAIXA_RX.match(t).groups(), (r.y0 + r.y1) / 2) for t, r in rotulos]
    # a grade comeca logo apos a coluna dos rotulos de horario (as celulas de segunda vao alem do "Seg")
    esquerda = max(r.x1 for _, r in rotulos)

    # celulas: retangulos preenchidos (nao brancos) dentro da grade, sem repetir o mesmo retangulo
    celulas, vistos = [], set()
    for d in pg.get_drawings():
        r, cor = d.get("rect"), d.get("fill")
        if not r or not cor or r.width < 15 or r.height < 10 or min(cor) > 0.95:
            continue
        chave = tuple(round(v) for v in (r.x0, r.y0, r.x1, r.y1))
        if r.y0 < topo or r.x0 < esquerda - 5 or chave in vistos:
            continue
        vistos.add(chave)
        celulas.append(r)

    # titulo da turma (maior fonte fora do cabecalho) e semestre ("2026/2")
    turma = max((s for s in spans if s[2].y1 <= topo and s[0] not in ("Seg", "Ter", "Qua", "Qui", "Sex")),
                key=lambda s: s[1], default=("", 0, None))[0]
    semestre = next((t for t, _, _ in spans if re.fullmatch(r"20\d\d/[12]", t)), "")

    # aula com varios professores e pintada em listras diagonais, cada listra um poligono cujo contorno
    # cobre so parte da celula: retangulos que se sobrepoem com AREA positiva sao a mesma celula (uniao).
    # celulas vizinhas so encostam na borda (inclusive as metades lado a lado de uma celula dividida)
    def sobrepoe(a, b):
        return min(a.x1, b.x1) - max(a.x0, b.x0) > 2 and min(a.y1, b.y1) - max(a.y0, b.y0) > 2
    uniao = []
    for cel in celulas:
        juntos = [u for u in uniao if sobrepoe(u, cel)]
        for u in juntos:
            uniao.remove(u)
            cel = cel | u
        uniao.append(fitz.Rect(cel))
    centro_dia = {d: (r.x0 + r.x1) / 2 for d, r in dias.items()}
    grupos = []
    for cel in uniao:
        dentro = [s for s in spans if cel.contains(fitz.Point((s[2].x0 + s[2].x1) / 2, (s[2].y0 + s[2].y1) / 2))]
        cobertas = tuple(f for f, y in faixas if cel.y0 <= y <= cel.y1)
        if dentro and cobertas:
            dia = min(centro_dia, key=lambda d: abs(centro_dia[d] - (cel.x0 + cel.x1) / 2))
            grupos.append({"rect": cel, "dia": dia, "faixas": cobertas,
                           "spans": {(t, round(sz, 1), round(r.x0), round(r.y0)): (t, sz, r) for t, sz, r in dentro}})

    # texto da area da grade que nao caiu em celula lida: o desenho do PDF mudou (celula sem
    # preenchimento, coluna deslocada) e a leitura sairia parcial ou vazia em silencio
    if _textos_fora_das_celulas(spans, grupos, esquerda, topo, dias, faixas):
        return None

    # dentro da celula: fonte maior = disciplina, menor = professor(es), a do meio = sala
    y_da_faixa = dict(faixas)
    aulas = []
    for g in grupos:
        dentro = sorted(g["spans"].values(), key=lambda s: (round(s[2].y0), s[2].x0))
        tamanhos = sorted({round(s[1], 1) for s in dentro})
        juntar = lambda tam: " ".join(t for t, sz, _ in dentro if round(sz, 1) == tam).strip()
        disciplina = juntar(tamanhos[-1])
        professor = juntar(tamanhos[0]) if len(tamanhos) >= 2 else ""
        sala = juntar(tamanhos[1]) if len(tamanhos) >= 3 else ""
        # com so dois tamanhos, o texto menor e professor se estiver na metade de cima da celula (canto
        # superior direito no aSc); na metade de baixo e a sala de uma aula sem professor
        if len(tamanhos) == 2:
            menores = [r for t, sz, r in dentro if round(sz, 1) == tamanhos[0]]
            meio = (g["rect"].y0 + g["rect"].y1) / 2
            if all((r.y0 + r.y1) / 2 > meio for r in menores):
                professor, sala = "", juntar(tamanhos[0])
        cobertas = sorted(g["faixas"], key=lambda f: y_da_faixa[f])
        inicio, fim = cobertas[0][0], cobertas[-1][1]
        for nome in [p.strip() for p in professor.split("/") if p.strip()] or [""]:
            aulas.append((nome, disciplina, _DIA_EXTENSO[g["dia"]], inicio, fim, sala))
    rotulo = ", ".join(x for x in (turma, semestre) if x)
    if not aulas:
        return [f"Grade de horários da turma {rotulo}: nenhuma aula cadastrada no arquivo publicado."]

    # agrupa por professor, na ordem da semana, no formato da visao
    ordem_dia = list(_DIA_EXTENSO.values())
    por_professor = {}
    for nome, disc, dia, ini, fim, sala in sorted(aulas, key=lambda a: (ordem_dia.index(a[2]), a[3])):
        por_professor.setdefault(nome, []).append(f"{disc} ({dia} {ini}-{fim}" + (f", sala {sala})" if sala else ")"))
    linhas = []
    for nome, itens in por_professor.items():
        cabeca = f"Disciplinas do professor {nome}" if nome else "Aulas sem professor identificado"
        linhas.append(f"{cabeca} ({rotulo}): " + "; ".join(itens) + ".")
    return linhas

def _grade_ano_raw(raw_text, title):
    # ano do RAW, DETERMINISTICO (mais confiavel que regex sobre a saida da visao, que pode alucinar):
    # "Horario criado:DD/MM/AAAA" no cabecalho, ou o ano no titulo "Horarios_AAAA_S".
    m = re.search(r"criado:\s*\d{2}/\d{2}/(20[0-3]\d)", raw_text or "")
    if m:
        return m.group(1)
    m = re.search(r"(20[0-3]\d)", title or "")
    return m.group(1) if m else None

def structure_schedule_vision(doc, max_pages=30):
    # cada pagina e lida primeiro pelas COORDENADAS (ler_grade_asc, deterministico, sem LLM); so a
    # pagina cuja estrutura aSc nao e reconhecida vai para a visao abaixo.
    # PIXELRAG: renderiza cada pagina da grade e extrai por VISAO (modelo multimodal), agregando por
    # professor. O layout 2D da grade aSc derrota a extracao de texto (a celula transborda a linha e a
    # coluna funde); a visao le o grid como um humano. GATE anti-alucinacao: horarios/salas emitidos
    # tem que existir no raw da pagina, senao a pagina e DESCARTADA (nao entra fato inventado na base).
    # max_pages=30: as grades reais observadas tem <=10 paginas (margem 3x); e o teto so protege contra
    # um PDF patologico mal-detectado. Se um dia uma grade legitima exceder, o excedente NAO some em
    # silencio: truncado=True marca a grade como visao_parcial (schedule_source, agora persistido).
    # Retorna (texto_estruturado, flags) com contadores de paginas puladas/suspeitas/truncagem.
    linhas, puladas, suspeitas, por_coordenadas = [], 0, 0, 0
    truncado = doc.page_count > max_pages
    for i, pg in enumerate(doc):
        if i >= max_pages:
            break
        lidas = ler_grade_asc(pg)
        if lidas is not None:
            linhas.extend(lidas); por_coordenadas += 1
            continue
        raw_pg = pg.get_text()
        try:
            png = pg.get_pixmap(dpi=200).tobytes("png")
        except Exception as e:
            print(f"  ERRO render grade pag {i}: {e}"); puladas += 1; continue
        # ate 3 leituras: a leitura reprovada num gate e refeita (o erro da visao varia entre leituras)
        aceita, lida = None, False
        for leitura in range(3):
            out = None
            for attempt in range(3):
                try:
                    out = llm.completar(_SCHED_VISION_PROMPT, temperatura=0.1, imagens=[png]).strip(); break
                except Exception as e:
                    wait = 20 * (attempt + 1)
                    print(f"  ERRO visao grade pag {i} (tentativa {attempt+1}/3): {e}; aguardando {wait}s")
                    time.sleep(wait)
            if not out:
                continue
            lida = True

            # GATE: os horarios (HH:MM) e codigos de sala (LAB X, F04...) da saida devem estar no raw
            raw_n = _norm_conteudo(raw_pg)
            toks = re.findall(r"\d{1,2}:\d{2}|LAB\s*[A-Z]?\s*\d+|\b[A-Z]{1,3}\d{2,3}\b", out)
            if toks:
                # "08:00" da visao e "8:00" no raw sao o mesmo horario: compara sem o zero a esquerda
                ok = sum(1 for t in toks if _norm_conteudo(re.sub(r"^0(\d:)", r"\1", t)) in raw_n or _norm_conteudo(t) in raw_n)
                if ok / len(toks) < 0.75:
                    print(f"  grade pag {i} (leitura {leitura+1}): {100 - ok/len(toks)*100:.0f}% dos tokens da visao fora do raw")
                    continue
            if not _dias_conferem(out, pg):
                print(f"  grade pag {i} (leitura {leitura+1}): dia da semana diverge da coluna da disciplina na pagina")
                continue
            aceita = out; break
        if aceita:
            linhas.append(aceita)
        elif lida:
            print(f"  grade pag {i}: DESCARTADA apos 3 leituras reprovadas (suspeita de erro de leitura)")
            suspeitas += 1
        else:
            puladas += 1
    return "\n".join(linhas), {"paginas": min(doc.page_count, max_pages), "puladas": puladas,
                               "suspeitas": suspeitas, "truncado": truncado, "coordenadas": por_coordenadas}

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
            out = llm.completar(prompt, temperatura=0.0, leve=True).strip(); break
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
                if flags["puladas"] or flags["suspeitas"] or flags["truncado"]:
                    resultado["schedule_source"] = "visao_parcial"
                elif flags["coordenadas"] == flags["paginas"]:
                    resultado["schedule_source"] = "coordenadas"
                else:
                    resultado["schedule_source"] = "coordenadas_e_visao" if flags["coordenadas"] else "visao"
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
            text, conferencia = structure_calendar_text(extract_calendar_text(doc))
            # so o calendario estruturado vira um chunk por evento; o texto cru devolvido numa falha
            # do LLM segue no fatiamento por tamanho. data omitida ou inventada fica no relatorio
            if conferencia is not None:
                resultado["is_calendar"] = True
                resultado["calendar_check"] = conferencia
                if conferencia["omitidas"] or conferencia["inventadas"]:
                    print(f"  [CALENDARIO] {url} -> datas divergentes do PDF: {conferencia}")
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
        # leitura que entrou mas pede olho humano: grade que saiu do layout lido pelas coordenadas
        # (caiu na visao) e calendario com data omitida ou inventada em relacao ao PDF
        "a_conferir": {
            "grade_fora_das_coordenadas": [{"url": r["source_url"], "schedule_source": r.get("schedule_source")}
                                           for r in results if r.get("is_schedule") and r.get("schedule_source") != "coordenadas"],
            "calendario_com_datas_divergentes": [{"url": r["source_url"], **r["calendar_check"]} for r in results
                                                 if (r.get("calendar_check") or {}).get("omitidas")
                                                 or (r.get("calendar_check") or {}).get("inventadas")],
        },
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
