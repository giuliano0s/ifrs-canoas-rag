"""Testes da checagem G (data que o agente não leu) de rag/chain.py e da leitura de datas de rag/ancoragem.py,
sem rede.

Uso, a partir da raiz: python -m tests.test_guard_data_sem_ancora
"""
from datetime import date

from rag import chain
from rag.ancoragem import ancorada, datas, trechos_de_data
from rag.chain import DATA_SEM_ANCORA, _checar_texto, _guard_data_sem_ancora

HOJE = date(2026, 10, 12)
LIDO = """Calendário acadêmico 2026 do Campus Canoas, eventos de dezembro de 2026:
Término do segundo semestre - Cursos Superiores: 11 de dezembro de 2026.
Exames - Cursos Superiores: 14 a 18 de dezembro de 2026.
Último dia letivo - Cursos Técnicos Integrados: 18 de dezembro de 2026.
Recesso/férias escolares: 04 a 29 de janeiro de 2027.
Reingresso e trancamento, período: 13, 14 e 15 de julho
Inscrições da Etapa 4 até 31 de julho de 2026.
Envio da documentação: 06/01 a 12/01/2027
Início das aulas (todos os cursos): 18 de fevereiro de 2027.
DATA ATUAL: 12/10/2026
quando começam as férias de fim de ano?"""


def testar_leitura_de_datas():
    # faixa: so as pontas na resposta, dia a dia no que foi lido
    assert datas("de 12 a 14 de janeiro de 2026", expandir=False) == {(12, 1, 2026), (14, 1, 2026)}
    assert (13, 1, 2026) in datas("de 12 a 14 de janeiro de 2026", expandir=True)
    assert datas("28 de dezembro a 05 de janeiro", expandir=True) >= {(28, 12, None), (31, 12, None), (1, 1, None), (5, 1, None)}
    # a leitura simples e larga de proposito (o pedaco "08 a 13/08/2027" da faixa tambem conta): do lado do que
    # foi lido, sobrar data so ancora mais; do lado da resposta vale a leitura por trechos, sem sobra
    assert datas("05 a 07/01/2026 e 02/08 a 13/08/2027", expandir=False) >= {(5, 1, 2026), (7, 1, 2026), (2, 8, 2027), (13, 8, 2027)}
    assert [citadas for _, _, citadas in trechos_de_data("05 a 07/01/2026 e 02/08 a 13/08/2027")] == [
        {(5, 1, 2026), (7, 1, 2026)}, {(2, 8, 2027), (13, 8, 2027)}]
    assert datas("1º de maio, 30/04/26 e 45/13/2026", expandir=False) == {(1, 5, None), (30, 4, 2026)}

    # lista de dias do mesmo mes
    assert datas("Período: 13, 14 e 15 de julho", expandir=True) == {(13, 7, None), (14, 7, None), (15, 7, None)}
    assert datas("dias 3, 10, 17 e 24 de outubro de 2026", expandir=False) == {(3, 10, 2026), (10, 10, 2026), (17, 10, 2026),
                                                                              (24, 10, 2026)}

    # cada data da resposta uma vez so, com a posicao; a faixa nao devolve de novo a data solta dentro dela
    texto = "As provas vão de 14 a 18 de dezembro de 2026 e o resultado sai em 05/01/2027."
    trechos = trechos_de_data(texto)
    assert [texto[inicio:fim] for inicio, fim, _ in trechos] == ["14 a 18 de dezembro de 2026", "05/01/2027"]
    assert trechos[0][2] == {(14, 12, 2026), (18, 12, 2026)} and trechos[1][2] == {(5, 1, 2027)}

    # numero solto ligado por "ate" nao e comeco de faixa; entre duas datas completas, e
    texto = "A Etapa 4 até 31 de julho de 2026 e a Etapa 5 de 1 de agosto até 10 de agosto."
    assert [texto[inicio:fim] for inicio, fim, _ in trechos_de_data(texto)] == ["31 de julho de 2026", "1 de agosto até 10 de agosto"]

    # toda data lida por trechos tambem sai na leitura simples
    for texto in ("de 12 a 14 de janeiro de 2026, e em 20/03", "13, 14 e 15 de julho e 16 e 17 de dezembro de 2026",
                  "30/09 a 19/10/2026; 05 a 07/01/2026; 1º de maio de 2026", "sem data nenhuma, turma 2026/2 e sala F111"):
        assert {data for _, _, citadas in trechos_de_data(texto) for data in citadas} <= datas(texto, expandir=False), texto

    # ancora: mesmo dia e mes, e o ano so conflita quando os dois lados o trazem
    lidas = datas(LIDO, expandir=True)
    assert ancorada((18, 12, 2026), lidas) and ancorada((18, 12, None), lidas) and ancorada((13, 7, 2026), lidas)
    assert ancorada((20, 1, 2027), lidas) and not ancorada((20, 1, 2026), lidas) and not ancorada((19, 12, 2026), lidas)


def testar_checagem():
    def checar(texto):
        return _guard_data_sem_ancora(texto, LIDO, HOJE)

    # a deducao (o dia seguinte ao ultimo dia letivo) sai, e o resto da frase fica
    assert checar("O recesso começa em 19 de dezembro de 2026, após o último dia letivo, em 18 de dezembro [1].") == \
        f"O recesso começa em {DATA_SEM_ANCORA}, após o último dia letivo, em 18 de dezembro [1]."
    assert checar("A formatura será dia 20/12.") == f"A formatura será dia {DATA_SEM_ANCORA}."
    # faixa com uma ponta fora do que foi lido sai inteira; ano que conflita tambem
    assert checar("Os exames vão de 14 a 21 de dezembro de 2026 [1].") == f"Os exames vão de {DATA_SEM_ANCORA} [1]."
    assert checar("As aulas começam em 18 de fevereiro de 2026.") == f"As aulas começam em {DATA_SEM_ANCORA}."
    # so a data errada sai, quando ha outras na frase
    assert checar("O semestre termina em 11 de dezembro e a formatura é em 22 de dezembro.") == \
        f"O semestre termina em 11 de dezembro e a formatura é em {DATA_SEM_ANCORA}."

    # passam sem mudar: data lida, dentro de um periodo lido, de lista, sem ano, numero solto com "ate",
    # a da pergunta e a de ontem, hoje e amanha
    for texto in ("O semestre dos cursos superiores termina em 11 de dezembro de 2026 [1].",
                  "Os exames vão de 14 a 18 de dezembro; o dia 16/12 cai nesse período.",
                  "O recesso vai de 4 a 29 de janeiro de 2027, e em 15 de janeiro ainda é recesso.",
                  "O reingresso pode ser pedido em 13 de julho, 14 de julho ou 15 de julho.",
                  "As inscrições da Etapa 4 foram até 31 de julho de 2026.",
                  "A documentação pode ser enviada de 06/01 a 12/01/2027, inclusive em 9 de janeiro.",
                  "Como hoje é 12 de outubro de 2026, amanhã, 13/10, ainda dá tempo; ontem foi 11 de outubro.",
                  "O edital está em https://ifrs.edu.br/canoas/inscricoes-de-20-a-25-de-marco/ e em www.ifrs.edu.br/20/03/x [2].",
                  "Não encontrei essa data.", ""):
        assert checar(texto) == texto, texto

    # fracao nao e data
    for texto in ("O estudante pode cursar até 1/3 da carga horária a distância.", "É preciso frequentar 3/4 das aulas.",
                  "A aprovação exige 2/3 dos votos."):
        assert checar(texto) == texto, texto

    # no documento a data vem partida por quebra de linha, espaco duplo ou com o ordinal em outro sinal
    partido = "Recurso do gabarito\n23 a\n25/10/2026\nFormaturas: 16  a 17 de dezembro de 2026.\nFeriado: 1° de maio de 2026"
    for texto in ("O recurso foi de 23 a 25 de outubro de 2026 [1].", "As formaturas são em 16 e 17 de dezembro de 2026.",
                  "O feriado é em 1º de maio de 2026."):
        assert _guard_data_sem_ancora(texto, partido, HOJE) == texto, texto

    # texto que ja derrubou a leitura: faixa que comeca em 29/02 e mes com letra de caixa especial
    for texto in ("Prazos 29 e 01/02/2028.", "Os prazos vão de 29/02 a 01/02/2028.", "Em 05 de JANEİRO de 2026."):
        datas(texto, expandir=True)
        trechos_de_data(texto)
        _guard_data_sem_ancora(texto, texto, HOJE)

    # data da conversa vale: o que o agente leu inclui a pergunta e as respostas anteriores
    assert _guard_data_sem_ancora("Você disse 22 de dezembro.", LIDO + "\nminha prova é dia 22 de dezembro?", HOJE) == \
        "Você disse 22 de dezembro."


def testar_no_fluxo():
    # roda por ultimo, com a letra G; sem a leitura do agente, nao roda
    alteracoes = []
    frase = "O recesso começa em 19 de dezembro de 2026 [1]."
    assert _checar_texto(frase, LIDO, HOJE, alteracoes, leitura="prompt e pergunta") == f"O recesso começa em {DATA_SEM_ANCORA} [1]."
    assert alteracoes == ["G"]
    assert _checar_texto(frase, LIDO, HOJE) == frase
    assert _checar_texto(frase, LIDO, None, leitura="prompt") == frase

    # a data que a checagem E escreve (a de hoje) e a que a D cita (a da linha do contexto) passam pela G
    alteracoes = []
    corrigido = _checar_texto("Como hoje é 13 de outubro de 2026, o prazo segue aberto.", LIDO, HOJE, alteracoes, leitura="p")
    assert corrigido == "Como hoje é 12 de outubro de 2026, o prazo segue aberto." and alteracoes == ["E"]
    # uma checagem que falha e pulada, sem derrubar a resposta nem as outras (a previa do streaming passa por aqui)
    original = chain._guard_soma_propria
    chain._guard_soma_propria = lambda corpo, contexto: 1 / 0
    try:
        alteracoes = []
        assert _checar_texto(frase, LIDO, HOJE, alteracoes, leitura="p") == f"O recesso começa em {DATA_SEM_ANCORA} [1]."
        assert alteracoes == ["G"]
        assert chain._Previa(LIDO, HOJE, "p").receber(frase + " ") == f"O recesso começa em {DATA_SEM_ANCORA} [1]. "
    finally:
        chain._guard_soma_propria = original

    # sem busca, a data vem so do prompt e da conversa
    assert _checar_texto("A prova será em 30 de novembro.", "", HOJE, leitura="quando é a prova?") == \
        f"A prova será em {DATA_SEM_ANCORA}."
    assert _checar_texto("Hoje é 12/10/2026.", "", HOJE, leitura="DATA ATUAL: 12/10/2026") == "Hoje é 12/10/2026."


if __name__ == "__main__":
    testar_leitura_de_datas()
    testar_checagem()
    testar_no_fluxo()
    print("ok: checagem G")
