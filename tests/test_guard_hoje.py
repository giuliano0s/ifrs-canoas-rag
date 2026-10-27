"""Testes das checagens E ("hoje" e "amanha" no dia errado) e F (prazo encerrado ou aberto contra a data)
de rag/chain.py, sem rede.

Uso, a partir da raiz: python -m tests.test_guard_hoje
"""
from datetime import date

from rag.chain import _guard_hoje, _guard_prazo

HOJE = date(2026, 10, 24)


def testar_prazo():
    # encerrado sem ter encerrado: a data logo antes da afirmacao ainda nao passou
    assert _guard_prazo("As inscrições foram de 9 a 25 de outubro de 2026, então o prazo já encerrou [3].", HOJE) == \
        "As inscrições foram de 9 a 25 de outubro de 2026, então o prazo ainda não encerrou [3]."
    assert _guard_prazo("A rematrícula ocorre de 16 a 27 de novembro de 2026, portanto esse período já passou.", HOJE).endswith(
        "portanto esse período ainda não passou.")
    assert _guard_prazo("Com prazo até 30/10/2026, as inscrições não estão mais abertas.", HOJE).endswith(
        "as inscrições ainda estão abertas.")
    # aberto sem estar: a data ja passou
    assert _guard_prazo("As inscrições iam até 20 de outubro de 2026 e ainda estão abertas.", HOJE).endswith("e já encerraram.")
    assert _guard_prazo("O prazo era 10/10/2026, então ainda dá tempo.", HOJE).endswith("então já encerrou.")

    # virada do ano: data sem ano e a mais perto de hoje (dezembro passado em janeiro, janeiro seguinte em dezembro)
    frase = "As inscrições foram de 1 a 15 de dezembro, então já encerraram."
    assert _guard_prazo(frase, date(2027, 1, 10)) == frase
    frase = "A rematrícula vai de 12 a 14 de janeiro, e ainda dá tempo."
    assert _guard_prazo(frase, date(2026, 12, 10)) == frase
    assert _guard_hoje("O prazo vai até 1º de janeiro; amanhã é o último dia.", date(2026, 12, 31)) == \
        "O prazo vai até 1º de janeiro; amanhã é o último dia."
    # o ano citado na frase vale para a data sem ano, longe da virada
    frase = "O período da Etapa 2 de 2026, de 26 de janeiro a 16 de fevereiro, já passou."
    assert _guard_prazo(frase, date(2026, 10, 21)) == frase

    # passam sem mudar: data atual citada pelo modelo, data passada com "ja passou", data de hoje e frase sem data
    for texto in ("Como a data atual é 24 de outubro de 2026, os prazos de inscrição já passaram.",
                  "Como hoje é 24/10/2026, essa edição já passou.",
                  "As inscrições foram até 20 de outubro de 2026, então já encerraram.",
                  "As inscrições vão até 30 de outubro de 2026 e ainda estão abertas.",
                  "As inscrições vão até 24/10/2026 e ainda estão abertas.",
                  "Esse prazo já encerrou.",
                  "A 1ª etapa foi em 05/10/2026 e já passou; a próxima é em 10/11/2026."):
        assert _guard_prazo(texto, HOJE) == texto, texto


def testar():
    # prazo dado como hoje sem ser: a palavra vira a data do prazo
    novo = _guard_hoje("As inscrições vão até 25 de outubro de 2026 — portanto, hoje é o último dia para se inscrever. [3]", HOJE)
    assert "hoje" not in novo and "o último dia é 25 de outubro de 2026 para se inscrever. [3]" in novo
    novo = _guard_hoje("O prazo termina hoje, 25/10/2026.", HOJE)
    assert "hoje" not in novo.split(",")[0] and "termina em 25/10/2026" in novo
    assert "o último dia é 25/10" in _guard_hoje("Inscrições até 25/10: o último dia é hoje.", HOJE)

    # "hoje e <data>" com a data errada passa a trazer a data atual, no mesmo formato
    assert _guard_hoje("Como hoje é 25 de outubro de 2026, o prazo ainda está aberto.", HOJE) == \
        "Como hoje é 24 de outubro de 2026, o prazo ainda está aberto."
    assert _guard_hoje("Hoje é 25/10/2026.", HOJE) == "Hoje é 24/10/2026."

    # amanha e o dia seguinte ao do pedido
    assert "o último dia é 26 de outubro" in _guard_hoje("Vai até 26 de outubro: amanhã é o último dia.", HOJE)
    assert _guard_hoje("As inscrições vão até 25 de outubro; amanhã é o último dia.", HOJE) == \
        "As inscrições vão até 25 de outubro; amanhã é o último dia."

    # passam sem mudar: data certa, frase sem data, "hoje" sem afirmar data, e o resto do texto
    for texto in ("Como hoje é 24 de outubro de 2026, o prazo ainda está aberto.",
                  "As inscrições vão até 24/10/2026, então hoje é o último dia.",
                  "Hoje é o último dia para se inscrever.",
                  "Até hoje não saiu o resultado; a previsão era 20 de outubro.",
                  "A prova será em 7 de novembro de 2026."):
        assert _guard_hoje(texto, HOJE) == texto, texto
    texto = "Primeira frase certa. As inscrições vão até 25/10, hoje é o último dia.\n\nOutra informação."
    novo = _guard_hoje(texto, HOJE)
    assert novo.startswith("Primeira frase certa. ") and novo.endswith("\n\nOutra informação.")
    print("ok")


if __name__ == "__main__":
    testar()
    testar_prazo()
    print("ok: checagens E e F")
