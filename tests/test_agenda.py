"""Testes da leitura temporal do contexto (rag/agenda.py) e da ordem dos blocos de calendário, sem rede.

Uso, a partir da raiz: python -m tests.test_agenda
"""
from datetime import date

from rag.agenda import eventos_datados, marcar_eventos, periodo, primeiro_dia, situacao, trechos_como_lidos
from rag import chain
from rag.chain import Hit, _montar_contexto, build_context

HOJE = date(2026, 10, 22)


def testar_periodo():
    # as formas do calendario academico
    assert periodo("28 de outubro de 2026.") == (date(2026, 10, 28), date(2026, 10, 28))
    assert periodo("1º de maio de 2026") == (date(2026, 5, 1), date(2026, 5, 1))
    assert periodo("04 a 05 de novembro de 2026.") == (date(2026, 11, 4), date(2026, 11, 5))
    assert periodo("16 e 17 de dezembro de 2026") == (date(2026, 12, 16), date(2026, 12, 17))
    assert periodo("27 de janeiro a 10 de fevereiro de 2026.") == (date(2026, 1, 27), date(2026, 2, 10))
    assert periodo("23 de fevereiro a 18 de dezembro de 2026") == (date(2026, 2, 23), date(2026, 12, 18))
    assert periodo("outubro de 2026.") == (date(2026, 10, 1), date(2026, 10, 31))
    assert periodo("janeiro a março de 2026.") == (date(2026, 1, 1), date(2026, 3, 31))
    assert periodo("dezembro de 2026") == (date(2026, 12, 1), date(2026, 12, 31))

    # periodo que vira o ano, com e sem o ano no comeco
    assert periodo("20 de dezembro a 05 de janeiro de 2027") == (date(2026, 12, 20), date(2027, 1, 5))
    assert periodo("20 de dezembro de 2026 a 05 de janeiro de 2027") == (date(2026, 12, 20), date(2027, 1, 5))
    assert periodo("28/12 a 05/01/2027") == (date(2026, 12, 28), date(2027, 1, 5))

    # as formas do cronograma do portal de ingresso
    assert periodo("05/01/2027") == (date(2027, 1, 5), date(2027, 1, 5))
    assert periodo("30/09 a 19/10/2026") == (date(2026, 9, 30), date(2026, 10, 19))
    assert periodo("05 a 07/01/2026") == (date(2026, 1, 5), date(2026, 1, 7))

    # nao e so uma data: sem ano, data impossivel, texto em volta, vazio
    for texto in ("28 de outubro", "31 de fevereiro de 2026", "até 28 de outubro de 2026 pelo sistema",
                  "não haverá.", "2026", "", None, "14:00", "30/09 a"):
        assert periodo(texto) is None, texto


def testar_situacao():
    dia = date(2026, 10, 28)
    assert situacao(dia, dia, HOJE) == "daqui a 6 dias"
    assert situacao(date(2026, 10, 23), date(2026, 10, 23), HOJE) == "amanhã"
    assert situacao(HOJE, HOJE, HOJE) == "hoje"
    assert situacao(date(2026, 10, 12), date(2026, 10, 12), HOJE) == "já passou"
    assert situacao(date(2026, 10, 19), date(2026, 10, 21), HOJE) == "já passou"
    assert situacao(date(2026, 10, 26), date(2026, 10, 30), HOJE) == "daqui a 4 dias"
    assert situacao(date(2026, 10, 19), date(2026, 10, 22), HOJE) == "em andamento, termina hoje"
    assert situacao(date(2026, 10, 19), date(2026, 10, 23), HOJE) == "em andamento, termina amanhã"
    assert situacao(date(2026, 10, 1), date(2026, 10, 31), HOJE) == "em andamento, termina em 9 dias"
    assert situacao(HOJE, date(2026, 10, 30), HOJE) == "em andamento, termina em 8 dias"


CALENDARIO = """Calendário acadêmico 2026 do Campus Canoas, eventos de outubro de 2026:
Semana Cultural Interescolar (Lei 14.988/2024): outubro de 2026.
Nossa Senhora Aparecida - Padroeira do Brasil (feriado): 12 de outubro de 2026.
Solicitação de ingresso por transferência e de diplomado (Graduação): 26 a 30 de outubro de 2026.
Dia do Servidor Público (feriado/não letivo): 28 de outubro de 2026.
Graduação — matrícula online do 2º semestre: não haverá."""

CRONOGRAMA = """Cronograma - Processo Seletivo IFRS 2027/1
Atualizado em 10 de novembro de 2026
30/09 a
                                                  19/10/2026
PS Regular
PERÍODO PARA INSCRIÇÃO
A inscrição deve ser realizada no site, conforme publicação prevista para o dia 10/11/2026.
22/10/2026
PUBLICAÇÃO DA LISTA PRELIMINAR
05/01/2027
PUBLICAÇÃO DA CLASSIFICAÇÃO GERAL E DO RESULTADO DO PROCESSO SELETIVO"""


def testar_marcas():
    # calendario: so as linhas "rotulo: data" ganham a situacao; cabecalho e linha sem data ficam como estao
    marcado = marcar_eventos(CALENDARIO, HOJE).split("\n")
    assert marcado[0] == CALENDARIO.split("\n")[0]
    assert marcado[1].endswith("outubro de 2026. [em andamento, termina em 9 dias]")
    assert marcado[2].endswith("12 de outubro de 2026. [já passou]")
    assert marcado[3].endswith("26 a 30 de outubro de 2026. [daqui a 4 dias]")
    assert marcado[4].endswith("28 de outubro de 2026. [daqui a 6 dias]")
    assert marcado[5] == "Graduação — matrícula online do 2º semestre: não haverá."

    # cronograma (datas_soltas): a data em linha propria, e o periodo partido em duas linhas marcado na segunda
    marcado = marcar_eventos(CRONOGRAMA, HOJE, datas_soltas=True).split("\n")
    assert marcado[1] == "Atualizado em 10 de novembro de 2026"
    assert marcado[2] == "30/09 a"
    assert marcado[3].endswith("19/10/2026 [já passou]")
    assert marcado[6] == CRONOGRAMA.split("\n")[6]
    assert marcado[7] == "22/10/2026 [hoje]"
    assert marcado[9] == "05/01/2027 [daqui a 75 dias]"
    assert [indice for indice, _, _ in eventos_datados(CRONOGRAMA, datas_soltas=True)] == [3, 7, 9]

    # fora do cronograma a data sozinha na linha nao e marcada: numa listagem ela e a publicacao do item de cima
    listagem = "EDITAL Nº 03/2026 - VAGAS NÃO PREENCHIDAS 2026/1\n04/02/2026\nEDITAL Nº 02/2026\n20/01/2026"
    assert marcar_eventos(listagem, date(2026, 2, 5)) == listagem
    assert marcar_eventos(CRONOGRAMA, HOJE) == CRONOGRAMA

    def soltas(texto, hoje):
        return marcar_eventos(texto, hoje, datas_soltas=True)

    # o comeco do periodo em outra linha, nas formas dos editais de ingresso: a situacao e a do periodo inteiro
    assert soltas("De 04 a \n10/02/2026\nPeríodo de inscrições", date(2026, 2, 5)) == \
        "De 04 a \n10/02/2026 [em andamento, termina em 5 dias]\nPeríodo de inscrições"
    assert soltas("07 e \n08/12/2026\nRecurso do gabarito", date(2026, 12, 7)).split("\n")[1] == \
        "08/12/2026 [em andamento, termina amanhã]"
    assert soltas("23 a\n25/10/2026", HOJE).split("\n")[1] == "25/10/2026 [amanhã]"
    assert soltas("30/09\na\n19/10/2026\nInscrições", date(2026, 10, 1)) == \
        "30/09\na\n19/10/2026 [em andamento, termina em 18 dias]\nInscrições"
    assert soltas("17/12/2026\naté\n19/12/2026", HOJE) == "17/12/2026\naté\n19/12/2026 [daqui a 56 dias]"
    assert soltas("30/09 a 19/10/2026\nInscrições", HOJE).split("\n")[0] == "30/09 a 19/10/2026 [já passou]"
    # comeco de periodo sem o fim na linha seguinte nao contamina a linha de baixo, e "a" solto sem data antes
    assert soltas("30/09 a\nPS Regular\n19/10/2026", HOJE) == "30/09 a\nPS Regular\n19/10/2026 [já passou]"
    assert soltas("Vagas\na\n19/10/2026", HOJE) == "Vagas\na\n19/10/2026 [já passou]"

    # dois dias avulsos ligados por "e" nao sao um periodo; dois dias seguidos sao
    assert marcar_eventos("Sábados letivos: 02 e 09 de dezembro de 2026.", date(2026, 12, 5)) == \
        "Sábados letivos: 02 e 09 de dezembro de 2026."
    assert marcar_eventos("Formaturas: 16 e 17 de dezembro de 2026.", date(2026, 12, 16)).endswith("[em andamento, termina amanhã]")
    assert periodo("02 e 09/12/2026") is None and periodo("07 e 08/12/2026") == (date(2026, 12, 7), date(2026, 12, 8))

    # data no meio de uma frase, horario e texto sem evento nao mudam, com ou sem as datas soltas
    for texto in ("As inscrições vão de 9 a 25 de outubro de 2026, pelo sistema.",
                  "Reunião do conselho: 12 de março de 2026, às 14:00",
                  "Atendimento: segunda a sexta, das 8h às 21h.",
                  "Publicado em 03/11/2026 por Comunicação", ""):
        assert marcar_eventos(texto, HOJE) == texto and soltas(texto, HOJE) == texto, texto

    # texto estranho nao levanta erro: mes com letra de caixa especial, linha enorme, numeros sem fim, None
    for texto in ("Prazo: 05 de JANEİRO de 2026.", "Prazo: 12 de ſetembro de 2026", "9" * 9000, "1/" * 4000,
                  "Rótulo: " + "12 de março de 2026 " * 500, None):
        assert marcar_eventos(texto, HOJE, datas_soltas=True) == texto, str(texto)[:30]

    assert primeiro_dia(CALENDARIO) == date(2026, 10, 1)
    assert primeiro_dia("sem data") is None

    # a coleta guarda o texto da base e onde a data solta foi marcada: o que o modelo leu se refaz daqui
    busca = {"chunks_textos": [CALENDARIO, CRONOGRAMA, listagem], "contexto_datas_soltas": [False, True, False]}
    lidos = trechos_como_lidos(busca, HOJE)
    assert lidos == [marcar_eventos(CALENDARIO, HOJE), marcar_eventos(CRONOGRAMA, HOJE, datas_soltas=True), listagem]
    assert trechos_como_lidos(busca, None) == busca["chunks_textos"]
    assert trechos_como_lidos({"chunks_textos": [CRONOGRAMA]}, HOJE) == [CRONOGRAMA]


def _hit(n, texto, url="https://ifrs.edu.br/canoas/calendario.pdf", **metadata):
    return Hit(f"{url}#{n}", 0.9, {"source_url": url, "text": texto, "published_at": "2026", **metadata})


def testar_contexto():
    novembro = "Calendário, eventos de novembro de 2026:\nFinados (feriado): 02 de novembro de 2026."
    outubro = "Calendário, eventos de outubro de 2026:\nDia do Servidor Público (feriado/não letivo): 28 de outubro de 2026."
    janeiro = "Calendário, eventos de janeiro de 2027:\nRecesso/férias escolares: 04 a 29 de janeiro de 2027."
    sem_data = "Calendário:\nGraduação — início do 2º semestre: não haverá."
    pagina = "Página do campus sobre o calendário."
    antigo = "Calendário 2025, eventos de novembro de 2025:\nFinados: 02 de novembro de 2025."
    hits = [_hit(0, novembro, is_calendar=True), _hit(1, pagina, url="https://ifrs.edu.br/canoas/pagina/"),
            _hit(2, sem_data, is_calendar=True), _hit(3, janeiro, is_calendar=True),
            _hit(4, antigo, url="https://ifrs.edu.br/canoas/calendario-2025.pdf", is_calendar=True),
            _hit(5, outubro, is_calendar=True)]

    # os blocos do mesmo calendario trocam de lugar entre si, em ordem cronologica; os outros trechos ficam
    contexto, escolhidos, fontes, _ = build_context(hits, hoje=HOJE)
    assert [h.metadata["text"] for h in escolhidos] == [outubro, pagina, novembro, janeiro, antigo, sem_data]
    assert fontes == {1: "https://ifrs.edu.br/canoas/calendario.pdf", 2: "https://ifrs.edu.br/canoas/pagina/",
                      3: "https://ifrs.edu.br/canoas/calendario-2025.pdf"}

    # o modelo le a situacao de cada evento; sem a data do pedido, e para as checagens, o texto vai cru
    assert "28 de outubro de 2026. [daqui a 6 dias]" in contexto
    assert "02 de novembro de 2026. [daqui a 11 dias]" in contexto
    assert "02 de novembro de 2025. [já passou]" in contexto
    assert "04 a 29 de janeiro de 2027. [daqui a 74 dias]" in contexto
    cru = _montar_contexto(escolhidos)[0]
    assert "[daqui" not in cru and "[já passou]" not in cru
    assert cru == build_context(hits)[0]
    assert contexto.replace(" [daqui a 6 dias]", "").replace(" [daqui a 11 dias]", "").replace(
        " [já passou]", "").replace(" [daqui a 74 dias]", "") == cru

    # a data sozinha na linha so e marcada no trecho buscado pela frase do evento (cronograma, fonte curada)
    cronograma, listagem = "Cronograma\n05/01/2027\nPUBLICAÇÃO DO RESULTADO", "EDITAL Nº 03/2026\n04/02/2026"
    contexto = build_context([_hit(0, cronograma, url="https://ingresso.ifrs.edu.br/2027/cronograma/", texto_busca="x"),
                              _hit(1, listagem, url="https://ifrs.edu.br/canoas/editais/")], hoje=HOJE)[0]
    assert "05/01/2027 [daqui a 75 dias]\n" in contexto and "04/02/2026\n" in contexto and "04/02/2026 [" not in contexto

    # erro inesperado na marcacao nao derruba a busca: o trecho segue como esta na base
    original = chain.marcar_eventos
    chain.marcar_eventos = lambda texto, hoje, datas_soltas=False: 1 / 0
    try:
        assert build_context(hits, hoje=HOJE)[0] == cru
    finally:
        chain.marcar_eventos = original


if __name__ == "__main__":
    testar_periodo()
    testar_situacao()
    testar_marcas()
    testar_contexto()
    print("ok: leitura temporal do contexto")
