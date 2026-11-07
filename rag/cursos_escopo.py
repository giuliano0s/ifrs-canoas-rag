"""Escopo de CURSO: fonte única do mapeamento curso -> padrão, usada nas duas pontas.

- INGESTÃO (pipelines.cursos): as regras objetivas (página de curso, turma da grade, linha do quadro
  de vagas, título de documento definidor) e o gate do classificador por LLM usam estes padrões.
- SERVING (rag.chain): curso_da_query detecta se a pergunta nomeia um curso, para o rerank
  penalizar doc de curso DIFERENTE, e o rótulo do curso vai ao contexto.

Módulo folha (só `re`), sem dependência pesada, para ambos os pacotes importarem sem duplicar
os padrões (duplicar geraria drift ao adicionar/renomear um curso). É o par do campus_scope: os
dois são a mesma família de "escopo" que despriorizará no rerank o conteúdo fora do alvo da pergunta.
"""

import re

# slug canônico -> padrão que casa o nome do curso (no cabeçalho do doc e na pergunta do usuário).
# padrões TIGHT para não sobrepor (ex: TADS "análise e desenvolvimento" != Técnico "desenvolvimento
# de sistemas"). ordem: os mais específicos primeiro.
CURSOS = [
    ("gpi",                 r"gest[ãa]o de projetos e inova[çc]|\bgpi\b"),
    ("esp-educacao",        r"especializa[çc][ãa]o em educa[çc][ãa]o|integra[çc][ãa]o de saberes"),
    ("esp-linguagens",      r"linguagens contempor[âa]neas|\blce\b"),
    ("tads",                r"an[áa]lise e desenvolvimento de sistemas|\btads\b|\bads\b"),
    ("tec-desenv-sistemas", r"t[ée]cnico em desenvolvimento de sistemas|t[ée]cnico em inform[áa]tica\b|\btds\b"),
    ("tec-manutencao-informatica", r"manuten[çc][ãa]o e suporte em inform[áa]tica"),
    ("tec-admin",           r"t[ée]cnico em administra[çc][ãa]o"),
    ("tec-eletronica",      r"t[ée]cnico em eletr[ôo]nica"),
    ("tec-comercio",        r"t[ée]cnico em com[ée]rcio"),
    ("logistica",           r"tecnologia em log[íi]stica|tecn[óo]logo em log[íi]stica|\bcst\s+(?:em\s+)?log[íi]stica|"
                            r"curso[s]?\s+(?:superior\s+)?d[eo]\s+log[íi]stica"),
    ("automacao",           r"automa[çc][ãa]o industrial"),
    ("matematica",          r"licenciatura em matem[áa]tica|matem[áa]tica\s*[-–]\s*licenciatura"),
    ("eng-eletronica",      r"engenharia eletr[ôo]nica|engenharia el[ée]trica"),
    ("eng-mecanica",        r"engenharia mec[âa]nica"),
    ("operador-computador", r"operador de computador"),
    ("assistente-adm",      r"assistente administrativo"),
]
# As SIGLAS (ADS/TDS/GPI/LCE, além do TADS que já existia) e a variante "curso de Logística" foram
# levantadas minerando a base (jul/2026): docs reais usam só a sigla ou o nome curto. Elas não mudaram
# nenhuma tag da base atual (medido: 0 docs), então são rede para doc futuro; e ajudam a PRECISÃO,
# pois um doc que cita dois cursos passa a dar 2 matches e cai para neutro, em vez de herdar o único
# nome que o regex reconhecia. `tec-manutencao-informatica` era um curso AUSENTE da lista com PPC
# próprio de Canoas (PROEJA, 63 chunks): sem ele, um doc desse curso que citasse "Técnico em Comércio"
# de passagem era tagueado como Comércio.

# marcador de documento que DEFINE um curso; separa PPC/regulamento (curso-específico) de
# edital/notícia que só CITA cursos. sem isso, um edital que escreve o nome de um curso viraria
# falso-positivo daquele curso.
_DOC_DEFINE_CURSO = re.compile(
    r"projeto pedag[óo]gico|matriz curricular|\bppc\b|plano de ensino|regimento did[áa]tico|"
    r"trabalho de conclus[ãa]o de curso|"
    r"regulamento[\s_]*(?:de |do |da |para (?:a )?realiza\w+ do )?(?:tcc|est[áa]gio)",
    re.IGNORECASE)


def classify_curso_escopo(title, text, url=""):
    # doc-nível (vale para todos os chunks do doc): só classifica documento DEFINIDOR de curso, com
    # EXATAMENTE um curso declarado. título tem prioridade (declaração mais forte); senão o cabeçalho.
    # na dúvida (zero ou vários cursos, ou não é doc definidor), None (neutro).
    # espaços normalizados: o PDF quebra a linha entre as palavras do título ("PROJETO \n\nPEDAGÓGICO")
    title = re.sub(r"\s+", " ", title or "")
    head = re.sub(r"\s+", " ", title + " " + (text or "")[:3000]).lower()
    if not _DOC_DEFINE_CURSO.search(head):
        return None
    no_titulo = [s for s, p in CURSOS if re.search(p, title.lower())]
    if len(no_titulo) == 1:
        return no_titulo[0]
    no_head = [s for s, p in CURSOS if re.search(p, head)]
    return no_head[0] if len(no_head) == 1 else None


# nome curto do curso depois de "curso de", "técnico em", "licenciatura em" ("curso de Logística"): sem o
# prefixo, a palavra solta é também nome de disciplina ("Matemática III", "Introdução à Administração") e
# não identifica curso. "eletrônica" fica de fora: o campus tem o técnico e a engenharia
_PREFIXO_CURSO = (r"\b(?:curso|t[ée]cnico|tecn[óo]logo|tecnologia|licenciatura|cst)\s+"
                  r"(?:superior\s+|t[ée]cnico\s+)?(?:(?:de|do|da|em)\s+)?")
CURSOS_CURTOS = [
    ("logistica",    _PREFIXO_CURSO + r"log[íi]stica\b"),
    ("matematica",   _PREFIXO_CURSO + r"matem[áa]tica\b"),
    ("automacao",    _PREFIXO_CURSO + r"automa[çc][ãa]o\b"),
    ("tec-admin",    _PREFIXO_CURSO + r"administra[çc][ãa]o\b"),
    ("tec-comercio", _PREFIXO_CURSO + r"com[ée]rcio\b"),
]


def cursos_nomeados(texto):
    """Cursos do campus que o texto nomeia, pelo nome completo, pela sigla ou pelo nome curto.

    Args:
        texto: texto livre (pergunta, documento, trecho); espaços e quebras de linha são normalizados.

    Returns:
        Lista de slugs na ordem de CURSOS, sem repetição.
    """
    baixo = re.sub(r"\s+", " ", (texto or "").lower())
    achados = [slug for slug, padrao in CURSOS if re.search(padrao, baixo)]
    curtos = [slug for slug, padrao in CURSOS_CURTOS if slug not in achados and re.search(padrao, baixo)]
    return achados + curtos


def curso_da_query(query):
    # serving: retorna o slug se a pergunta nomeia UM curso claramente; None se nenhum ou vários
    # (aí não há alvo de curso e o rerank não penaliza ninguém). NÃO exige o marcador de documento:
    # é a pergunta do usuário, não um documento.
    achados = cursos_nomeados(query)
    return achados[0] if len(achados) == 1 else None


# nome legível por slug: rotula o documento no contexto ("Curso: ...") para o agente atribuir a
# regra ao curso certo, e não apresentar regra de um curso como se fosse de outro.
NOMES = {
    "gpi":                 "Especialização em Gestão de Projetos e Inovação (GPI)",
    "esp-educacao":        "Especialização em Educação",
    "esp-linguagens":      "Especialização em Linguagens Contemporâneas e Ensino",
    "tads":                "Tecnólogo em Análise e Desenvolvimento de Sistemas (TADS)",
    "tec-desenv-sistemas": "Técnico em Desenvolvimento de Sistemas",
    "tec-manutencao-informatica": "Técnico em Manutenção e Suporte em Informática (PROEJA)",
    "tec-admin":           "Técnico em Administração",
    "tec-eletronica":      "Técnico em Eletrônica",
    "tec-comercio":        "Técnico em Comércio",
    "logistica":           "Tecnólogo em Logística",
    "automacao":           "Tecnólogo em Automação Industrial",
    "matematica":          "Licenciatura em Matemática",
    "eng-eletronica":      "Bacharelado em Engenharia Eletrônica",
    "eng-mecanica":        "Bacharelado em Engenharia Mecânica",
    "operador-computador": "Operador de Computador (EJA-FIC)",
    "assistente-adm":      "Assistente Administrativo (EJA-FIC)",
}

def nome_curso(slug):
    return NOMES.get(slug, slug)


# páginas de curso do site do campus (endereço sem barra final -> slug): a própria página é do curso, e os
# documentos que só uma delas liga recebem o curso como pista para o classificador
PAGINAS_DE_CURSO = {
    "https://ifrs.edu.br/canoas/no-campus/cursos/gpi": "gpi",
    "https://ifrs.edu.br/canoas/no-campus/cursos/edu": "esp-educacao",
    "https://ifrs.edu.br/canoas/no-campus/cursos/lce": "esp-linguagens",
    "https://ifrs.edu.br/canoas/no-campus/cursos/curso-superior-de-tecnologia-em-analise-e-desenvolvimento-de-sistemas": "tads",
    "https://ifrs.edu.br/canoas/no-campus/cursos/curso-tecnico-em-desenvolvimento-de-sistemas-integrado-ao-ensino-medio": "tec-desenv-sistemas",
    "https://ifrs.edu.br/canoas/no-campus/cursos/proeja-tecnico-em-manutencao-e-suporte-em-informatica-integrado-ao-ensino-medio": "tec-manutencao-informatica",
    "https://ifrs.edu.br/canoas/no-campus/cursos/curso-tecnico-em-administracao-integrado-ao-ensino-medio": "tec-admin",
    "https://ifrs.edu.br/canoas/no-campus/cursos/curso-tecnico-em-eletronica-integrado-ao-ensino-medio": "tec-eletronica",
    "https://ifrs.edu.br/canoas/no-campus/cursos/tecnico-em-comercio": "tec-comercio",
    "https://ifrs.edu.br/canoas/no-campus/cursos/curso-superior-de-tecnologia-em-logistica": "logistica",
    "https://ifrs.edu.br/canoas/no-campus/cursos/curso-superior-de-tecnologia-em-automacao-industrial": "automacao",
    "https://ifrs.edu.br/canoas/no-campus/cursos/curso-superior-de-licenciatura-em-matematica": "matematica",
    "https://ifrs.edu.br/canoas/bacharelado-em-engenharia-eletrica-eletronica": "eng-eletronica",
    "https://ifrs.edu.br/canoas/bacharelado-em-engenharia-eletrica-eletronica-2": "eng-eletronica",
    "https://ifrs.edu.br/canoas/cursos/bacharelado-em-engenharia-eletrica-eletronica": "eng-eletronica",
}


def curso_da_pagina(url):
    # slug da página de curso pelo endereço, com ou sem barra final; None para os demais endereços
    return PAGINAS_DE_CURSO.get((url or "").split("#")[0].rstrip("/"))


# sigla da turma na grade de horários ("Disciplinas do professor X (ADM 3 - TARDE, 2026/2)", "(Lic Mat 8°Sem,
# 2026/2)", "Grade de horários da turma AUT 2º Sem"). PROEJA e PROFMAT ficam de fora: a turma PROEJA não diz o
# curso, e o PROFMAT não é curso da lista
SIGLAS_DE_TURMA = [
    (r"ADM \d", "tec-admin"),
    (r"D\. ?SIST", "tec-desenv-sistemas"),
    (r"ELE \d", "tec-eletronica"),
    (r"Eng\. Eletr", "eng-eletronica"),
    (r"LOG \d", "logistica"),
    (r"Lic\.? Mat", "matematica"),
    (r"TADS \d", "tads"),
    (r"AUT \d", "automacao"),
]
_TURMA = re.compile(r"(?:\(|\bturma\s+)(" + "|".join(sigla for sigla, _ in SIGLAS_DE_TURMA) + r")")


def curso_da_turma(texto):
    # slug pela sigla da turma do trecho de grade; None quando a turma não é de um curso da lista
    achado = _TURMA.search(texto or "")
    if not achado:
        return None
    return next(slug for sigla, slug in SIGLAS_DE_TURMA if re.fullmatch(sigla, achado.group(1)))


# linha do quadro de vagas estruturado ("Campus Canoas - Técnico em Eletrônica (Manhã, 4): 30 vagas ofertadas")
_LINHA_DE_VAGAS = re.compile(r"^Campus Canoas - (.+?) \(.*\bvagas ofertadas\b", re.S)


def curso_da_linha_de_vagas(texto):
    # slug do curso da linha do quadro de vagas; None quando o trecho não é uma linha do quadro ou o curso
    # não é exatamente um da lista
    achado = _LINHA_DE_VAGAS.match(texto or "")
    if not achado:
        return None
    cursos = cursos_nomeados(achado.group(1))
    return cursos[0] if len(cursos) == 1 else None
