"""Escopo de CURSO: fonte única do mapeamento curso -> padrão, usada nas duas pontas.

- INGESTÃO (pipelines.chunker): classify_curso_escopo tagueia o doc que DEFINE um curso
  (PPC, matriz, regulamento de TCC/estágio, plano de ensino) com o slug do curso.
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
    ("tec-desenv-sistemas", r"t[ée]cnico em desenvolvimento de sistemas|\btds\b"),
    ("tec-manutencao-informatica", r"manuten[çc][ãa]o e suporte em inform[áa]tica"),
    ("tec-admin",           r"t[ée]cnico em administra[çc][ãa]o"),
    ("tec-eletronica",      r"t[ée]cnico em eletr[ôo]nica"),
    ("tec-comercio",        r"t[ée]cnico em com[ée]rcio"),
    ("logistica",           r"tecnologia em log[íi]stica|curso[s]?\s+(?:superior\s+)?d[eo]\s+log[íi]stica"),
    ("automacao",           r"automa[çc][ãa]o industrial"),
    ("matematica",          r"licenciatura em matem[áa]tica"),
    ("eng-eletronica",      r"engenharia eletr[ôo]nica"),
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
    head = ((title or "") + " " + (text or "")[:3000]).lower()
    if not _DOC_DEFINE_CURSO.search(head):
        return None
    no_titulo = [s for s, p in CURSOS if re.search(p, (title or "").lower())]
    if len(no_titulo) == 1:
        return no_titulo[0]
    no_head = [s for s, p in CURSOS if re.search(p, head)]
    return no_head[0] if len(no_head) == 1 else None


def curso_da_query(query):
    # serving: retorna o slug se a pergunta nomeia UM curso claramente; None se nenhum ou vários
    # (aí não há alvo de curso e o rerank não penaliza ninguém). NÃO exige o marcador de documento:
    # é a pergunta do usuário, não um documento.
    q = (query or "").lower()
    achados = [s for s, p in CURSOS if re.search(p, q)]
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
