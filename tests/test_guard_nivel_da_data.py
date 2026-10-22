"""Testes da checagem D (nivel de ensino da data) de rag/chain.py, com contexto fixo e sem rede.

Uso, a partir da raiz: python -m tests.test_guard_nivel_da_data
"""
from rag.chain import _guard_nivel_da_data

CONTEXTO = "\n".join([
    "[1] Fonte: https://ifrs.edu.br/canoas/calendario-2026.pdf | Data: 2026",
    "Formaturas dos Cursos Técnicos Integrados ao Ensino Médio: 16 a 17 de dezembro de 2026.",
    "Formatura dos cursos superiores 2026/1: 21 de agosto de 2026.",
    "Início das aulas: 16 de fevereiro de 2026.",
    "Formaturas dos Cursos Técnicos Integrados ao Ensino Médio: 17 a 18 de dezembro de 2025.",
])


def trocou(texto):
    return _guard_nivel_da_data(texto, CONTEXTO) != texto


def testar():
    # data do ensino medio dada aos superiores, em tres formas de escrever
    assert trocou("A próxima formatura dos cursos superiores está marcada para 16 de dezembro de 2026. [1]")
    assert trocou("A próxima formatura dos cursos superiores está prevista para 16 a 17 de dezembro de 2026. [1]")
    assert trocou("A próxima formatura da graduação é em 16/12/2026 [1].")

    # a troca explica de quem e a data e mantem a citacao
    novo = _guard_nivel_da_data("A formatura dos cursos superiores será em 16 de dezembro de 2026 [1].", CONTEXTO)
    assert "Formaturas dos Cursos Técnicos Integrados ao Ensino Médio" in novo
    assert "não consta nos documentos [1]." in novo

    # so a frase errada muda; a certa e o resto do texto ficam
    texto = ("A formatura dos cursos superiores de 2026/1 foi em 21 de agosto de 2026 [1]. "
             "A próxima formatura dos cursos superiores será em 16 de dezembro de 2026. [2]\n\nOutra informação.")
    novo = _guard_nivel_da_data(texto, CONTEXTO)
    assert novo.startswith("A formatura dos cursos superiores de 2026/1 foi em 21 de agosto de 2026 [1].")
    assert novo.endswith("\n\nOutra informação.") and "16 de dezembro de 2026. [2]" not in novo

    # passam sem mudar: data do nivel certo, frase com os dois niveis, frase sem nivel, data fora do contexto,
    # data de linha sem nivel explicito e ano diferente do da linha
    assert not trocou("A formatura dos cursos superiores de 2026/1 foi em 21 de agosto de 2026. [1]")
    assert not trocou("A formatura dos cursos técnicos integrados será de 16 a 17 de dezembro de 2026. [1]")
    assert not trocou("A formatura dos cursos superiores não é em 16 de dezembro: essa data é dos cursos técnicos integrados. [1]")
    assert not trocou("A formatura será em 16 de dezembro de 2026. [1]")
    assert not trocou("A formatura dos cursos superiores será em 5 de março de 2027. [1]")
    assert not trocou("As aulas dos cursos superiores começam em 16 de fevereiro de 2026. [1]")
    assert not trocou("A formatura dos cursos superiores foi em 17 de dezembro de 2024. [1]")

    # texto sem datas ou contexto vazio: devolve o proprio texto
    assert _guard_nivel_da_data("Olá! Como posso ajudar?", CONTEXTO) == "Olá! Como posso ajudar?"
    assert _guard_nivel_da_data("A formatura dos cursos superiores é em 16/12/2026.", "") == "A formatura dos cursos superiores é em 16/12/2026."
    print("ok: checagem D")


if __name__ == "__main__":
    testar()
