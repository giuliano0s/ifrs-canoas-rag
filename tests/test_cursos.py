"""Testes da etiqueta de curso (rag/cursos_escopo.py e pipelines/cursos.py), com textos fixos e sem rede nem LLM.

Uso, a partir da raiz: python -m tests.test_cursos
"""
from pipelines.cursos import (_json, decisao_objetiva, etiquetas_dos_trechos, fora_do_escopo, normalizar,
                              texto_do_documento)
from rag.chain import CURSO_PENALTY, Hit, _curso_penalty, build_context
from rag.cursos_escopo import (classify_curso_escopo, curso_da_linha_de_vagas, curso_da_pagina, curso_da_query,
                               curso_da_turma, cursos_nomeados)

SITE = "https://ifrs.edu.br/canoas/wp-content/uploads/sites/6/2026/03/"


def trechos(*textos, url=SITE + "doc.pdf", **meta):
    return [{"id": f"{url}#{i}", "text": texto, "source_url": url, "title": meta.get("title", ""), **meta}
            for i, texto in enumerate(textos)]


def testar_regras_objetivas():
    # turma da grade: sigla de cada curso; PROEJA e PROFMAT nao dizem um curso da lista
    assert curso_da_turma("Disciplinas do professor Lindomar (ADM 3 - TARDE, 2026/2): Contabilidade") == "tec-admin"
    assert curso_da_turma("Disciplinas do professor Bruno (Lic Mat 8°Sem, 2026/2): TCC") == "matematica"
    assert curso_da_turma("Disciplinas do professor Joel (Eng. Eletrônica 3º, 2026/2)") == "eng-eletronica"
    assert curso_da_turma("Disciplinas do professor Aline (D. SIST. 1 - MANHÃ, 2026/2)") == "tec-desenv-sistemas"
    assert curso_da_turma("Disciplinas do professor X (ELE 2 - TARDE, 2026/2)") == "tec-eletronica"
    assert curso_da_turma("Grade de horários da turma AUT 2º Sem, 2026/2: nenhuma aula cadastrada") == "automacao"
    assert curso_da_turma("Disciplinas do professor X (PROEJA 4º, 2026/2)") is None
    assert curso_da_turma("Disciplinas do professor Bruno (PROFMAT (2), 2026/2)") is None

    # linha do quadro de vagas
    assert curso_da_linha_de_vagas("Campus Canoas - Tecnologia em Análise e Desenvolvimento de Sistemas (Noite, 6): "
                                   "40 vagas ofertadas no PS 2027/1.") == "tads"
    assert curso_da_linha_de_vagas("Campus Canoas - Licenciatura em Matemática** (Noite, 8): 30 vagas ofertadas") == "matematica"
    assert curso_da_linha_de_vagas("Campus Canoas tem 40 vagas") is None

    # titulo de PPC com quebras de linha entre as palavras (o PDF da Engenharia Eletronica)
    assert classify_curso_escopo("PROJETO \n \nPEDAGÓGICO DO CURSO DE ENGENHARIA\nELETRÔNICA", "") == "eng-eletronica"
    assert classify_curso_escopo("Notícia sobre a Engenharia Eletrônica", "visita técnica") is None

    # pagina de curso com e sem barra final
    assert curso_da_pagina("https://ifrs.edu.br/canoas/no-campus/cursos/gpi/") == "gpi"
    assert curso_da_pagina("https://ifrs.edu.br/canoas/no-campus/cursos/gpi/ingresso-2022/") is None


def testar_nomes_de_curso():
    # nome curto so com o prefixo de curso; a disciplina nao identifica curso
    assert curso_da_query("horas complementares do curso de logística") == "logistica"
    assert curso_da_query("nota mínima em matemática") is None
    assert curso_da_query("introdução à administração no TADS") == "tads"
    assert curso_da_query("tads ou técnico em administração?") is None
    # nomes antigos e variantes dos documentos
    assert cursos_nomeados("Colegiado do CST Logística") == ["logistica"]
    assert cursos_nomeados("Curso Superior de Matemática - Licenciatura") == ["matematica"]
    assert cursos_nomeados("Bacharelado em Engenharia Elétrica-Eletrônica") == ["eng-eletronica"]
    assert cursos_nomeados("Técnico em Informática Integrado ao Ensino Médio") == ["tec-desenv-sistemas"]


def testar_texto_do_documento():
    # sobreposicao do fatiador tirada; trecho de texto igual (eventos do mesmo mes) entra uma vez
    a = "início do documento " + "x" * 30 + " parte comum que se repete no trecho seguinte"
    b = "parte comum que se repete no trecho seguinte e o fim do documento"
    assert texto_do_documento(trechos(a, b)) == a + " e o fim do documento"
    assert texto_do_documento(trechos("mês de maio", "mês de maio", "junho")) == "mês de maio\njunho"


def testar_decisao_objetiva():
    pagina = "https://ifrs.edu.br/canoas/no-campus/cursos/curso-superior-de-tecnologia-em-logistica/"
    assert decisao_objetiva(pagina, trechos("texto", url=pagina), "texto")["cursos"] == ["logistica"]

    # grade: curso por trecho, a turma PROEJA fica sem etiqueta
    grade = trechos("Disciplinas do professor A (LOG 3º Sem, 2026/2): Gestão", "Disciplinas do professor B (PROEJA 2º, 2026/2)",
                    is_schedule=True)
    decisao = decisao_objetiva(SITE + "grade.pdf", grade, "")
    assert decisao["fonte"] == "grade" and list(decisao["por_trecho"].values()) == ["logistica", None]
    etiquetas = list(etiquetas_dos_trechos(decisao, grade, "", {}).values())
    assert etiquetas[0] == {"curso_escopo": "logistica", "cursos": None, "curso_fonte": "grade"}
    assert etiquetas[1] == {"curso_escopo": None, "cursos": None, "curso_fonte": None}

    # quadro de vagas, um curso por linha
    vagas = trechos("Campus Canoas - Técnico em Comércio (Noite, 6): 30 vagas ofertadas",
                    "Campus Canoas - Técnico em Eletrônica (Manhã, 4): 30 vagas ofertadas")
    assert list(decisao_objetiva(SITE + "vagas.pdf", vagas, "")["por_trecho"].values()) == ["tec-comercio", "tec-eletronica"]

    # documento definidor com o curso no titulo; noticia que so cita o curso vai ao modelo
    ppc = trechos("Projeto Pedagógico do Curso Superior de Tecnologia em Logística", title="PPC Logística 2024")
    assert decisao_objetiva(SITE + "ppc.pdf", ppc, ppc[0]["text"])["fonte"] == "titulo"
    noticia = trechos("Estudantes do curso de Logística visitam o porto", title="Visita técnica")
    assert decisao_objetiva("https://ifrs.edu.br/canoas/visita/", noticia, noticia[0]["text"]) is None


def testar_normalizar():
    # gate: slug que o texto nao nomeia e a pagina nao liga cai
    assert normalizar({"estrutura": "um_curso", "cursos": ["tads"]}, ["logistica"], None) == ("generico", [])
    assert normalizar({"estrutura": "um_curso", "cursos": ["tads"]}, [], "tads") == ("um_curso", ["tads"])
    assert normalizar({"estrutura": "um_curso", "cursos": ["tads", "logistica"]}, ["tads", "logistica"], None) == \
        ("varios_sem_secoes", ["tads", "logistica"])
    assert normalizar({"estrutura": "varios_com_secoes", "cursos": ["tads"]}, ["tads"], None) == ("varios_sem_secoes", ["tads"])
    assert normalizar({"estrutura": "um_coco", "cursos": ["tads"]}, ["tads"], None) == ("generico", [])
    assert normalizar({"estrutura": "um_curso", "cursos": ["inventado"]}, ["tads"], None) == ("generico", [])


def testar_etiquetas_do_modelo():
    vazio = {"curso_escopo": None, "cursos": None, "curso_fonte": None}
    doc = trechos("primeiro", "segundo")
    um = etiquetas_dos_trechos({"estrutura": "um_curso", "cursos": ["tads"], "fonte": "modelo"}, doc, "texto", {})
    assert all(e == {"curso_escopo": "tads", "cursos": None, "curso_fonte": "modelo"} for e in um.values())

    # curso so pelo link: documento definidor recebe o curso; o resto, a lista (sem penalidade)
    ligado = {"estrutura": "um_curso", "cursos": ["tec-comercio"], "fonte": "modelo", "so_ligacao": True}
    assert list(etiquetas_dos_trechos(ligado, doc, "MATRIZ CURRICULAR 2019", {}).values())[0]["curso_escopo"] == "tec-comercio"
    assert list(etiquetas_dos_trechos(ligado, doc, "Resultado da eleição", {}).values())[0] == \
        {"curso_escopo": None, "cursos": ["tec-comercio"], "curso_fonte": "modelo"}
    # "ementa" so como palavra: "complementares" e "implementação" nao fazem o documento definidor
    assert list(etiquetas_dos_trechos(ligado, doc, "Informações complementares sobre a implementação", {}).values())[0][
        "curso_escopo"] is None
    assert list(etiquetas_dos_trechos(ligado, doc, "Ementas das disciplinas", {}).values())[0]["curso_escopo"] == "tec-comercio"

    varios = etiquetas_dos_trechos({"estrutura": "varios_sem_secoes", "cursos": ["tads", "logistica"], "fonte": "modelo"},
                                   doc, "", {})
    assert list(varios.values())[0]["cursos"] == ["tads", "logistica"]
    assert list(etiquetas_dos_trechos({"estrutura": "generico", "cursos": []}, doc, "", {}).values()) == [vazio, vazio]

    # secoes: o trecho do modelo vira curso; o que nomeia dois cursos e o comum ficam com a lista
    secoes = trechos("Seção do TADS: estágio de 240 horas", "Análise e Desenvolvimento de Sistemas e Tecnologia em Logística: disposições",
                     "disposições gerais")
    vereditos = {secoes[0]["id"]: "tads", secoes[1]["id"]: "tads", secoes[2]["id"]: None}
    saida = list(etiquetas_dos_trechos({"estrutura": "varios_com_secoes", "cursos": ["tads", "logistica"]}, secoes, "",
                                       vereditos).values())
    assert saida[0]["curso_escopo"] == "tads"
    assert saida[1]["curso_escopo"] is None and saida[1]["cursos"] == ["tads", "logistica"]
    assert saida[2]["curso_escopo"] is None and saida[2]["cursos"] == ["tads", "logistica"]


def testar_escopo_e_rotulo():
    assert fora_do_escopo(SITE + "x.pdf", trechos("a", edital_url="https://ifrs.edu.br/editais/x"))
    assert fora_do_escopo(SITE + "x.pdf", trechos("a", campus_scope="outro"))
    assert not fora_do_escopo(SITE + "x.pdf", trechos("a"))

    # rotulo do contexto: um curso, varios cursos, nenhum
    def hit(n, **meta):
        return Hit(f"u{n}#0", 0.9, {"source_url": f"https://ifrs.edu.br/canoas/{n}", "text": "t", "published_at": "2026", **meta})
    contexto = build_context([hit(1, curso_escopo="tads"), hit(2, cursos=["tads", "logistica"]), hit(3)])[0]
    assert "| Curso: Tecnólogo em Análise e Desenvolvimento de Sistemas (TADS)\n" in contexto
    assert "| Cursos: Tecnólogo em Análise e Desenvolvimento de Sistemas (TADS), Tecnólogo em Logística\n" in contexto
    assert "[3] Fonte: https://ifrs.edu.br/canoas/3 | Data: 2026\n" in contexto

    # penalidade so para curso unico diferente do perguntado; a lista de varios cursos nao e penalizada
    assert _curso_penalty(hit(1, curso_escopo="logistica"), "tads") == CURSO_PENALTY
    assert _curso_penalty(hit(1, curso_escopo="tads"), "tads") == 0.0
    assert _curso_penalty(hit(1, cursos=["logistica"]), "tads") == 0.0
    assert _curso_penalty(hit(1, curso_escopo="logistica"), None) == 0.0


def testar_resposta_do_modelo():
    # resposta que nao e objeto vira erro do documento (pendente), nunca derruba a fase
    assert _json('```json\n{"estrutura": "generico", "cursos": []}\n```')["estrutura"] == "generico"
    for ruim in ("null", '["tads"]', '"tads"'):
        try:
            _json(ruim)
        except ValueError:
            continue
        raise AssertionError(ruim)


if __name__ == "__main__":
    testar_regras_objetivas()
    testar_nomes_de_curso()
    testar_texto_do_documento()
    testar_decisao_objetiva()
    testar_normalizar()
    testar_etiquetas_do_modelo()
    testar_escopo_e_rotulo()
    testar_resposta_do_modelo()
    print("ok: etiqueta de curso")
