"""Âncora das datas: diz se uma data citada numa resposta aparece no que o agente leu.

Uma data está ancorada quando aparece, com o mesmo dia e mês (e o mesmo ano, quando os dois lados o
trazem), nos trechos recuperados, no prompt, na pergunta ou na conversa. Na resposta, uma faixa de datas
conta pelas duas pontas; no que o agente leu, a faixa é expandida dia a dia, para "de 05 a 09/01" na resposta
casar com "05/01 a 13/01" no documento.

Fonte única da leitura de datas para as duas pontas que a usam: a checagem G do atendimento (rag/chain.py),
que troca a data sem âncora, e a medição offline das coletas (eval/ancoragem.py).
"""
import re
from datetime import date, timedelta

MESES = {"janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
         "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
MES_RX = "|".join(MESES)
FAIXA_MAXIMA_DIAS = 120

# as formas de data, da mais longa para a mais curta: a lista e a faixa vêm antes da data solta que elas contêm
# "13, 14 e 15 de julho"
_LISTA_NO_MES = re.compile(rf"\b(\d{{1,2}}(?:, \d{{1,2}})+ e \d{{1,2}}) de ({MES_RX})(?: de (\d{{4}}))?", re.I)
_FAIXA_ENTRE_MESES = re.compile(
    rf"\b(\d{{1,2}})(?:º)? de ({MES_RX}) (?:a|até) (\d{{1,2}}) de ({MES_RX})(?: de (\d{{4}}))?", re.I)
# o número que abre a faixa não pode ser o mês de uma data ("06/01 a 12/01/2027" não é do dia 1 ao 12)
_FAIXA_NO_MES = re.compile(rf"(?<![\d/])(\d{{1,2}})(?:º)? (?:a|e|até) (\d{{1,2}}) de ({MES_RX})(?: de (\d{{4}}))?", re.I)
_FAIXA_NUMERICA = re.compile(r"\b(\d{1,2})/(\d{1,2}) (?:a|até) (\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?")
_FAIXA_DE_DIAS_NUMERICA = re.compile(r"(?<![\d/])(\d{1,2}) (?:a|e|até) (\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?")
_DATA_POR_EXTENSO = re.compile(rf"\b(\d{{1,2}})(?:º)? de ({MES_RX})(?: de (\d{{4}}))?", re.I)
_DATA_NUMERICA = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")


def _ano(texto):
    """Ano de quatro dígitos a partir do texto capturado, ou None."""
    if not texto:
        return None
    ano = int(texto)
    return ano + 2000 if ano < 100 else ano


def _faixa(inicio, fim, ano, expandir):
    """Datas de uma faixa (dia, mês) a (dia, mês): só as pontas, ou dia a dia atravessando o mês e o ano."""
    if not expandir:
        return {(*inicio, ano), (*fim, ano)}
    # data impossível (31/02, ou 29/02 levado para um ano sem ele) deixa a faixa só com as pontas
    try:
        primeiro = date(ano or 2026, inicio[1], inicio[0])
        ultimo = date(ano or 2026, fim[1], fim[0])
        if ultimo < primeiro:
            primeiro = primeiro.replace(year=primeiro.year - 1)
    except ValueError:
        return {(*inicio, ano), (*fim, ano)}
    if (ultimo - primeiro).days > FAIXA_MAXIMA_DIAS:
        return {(*inicio, ano), (*fim, ano)}
    dias = (primeiro + timedelta(days=passo) for passo in range((ultimo - primeiro).days + 1))
    return {(dia.day, dia.month, ano) for dia in dias}


def _datas_do_achado(forma, achado, expandir):
    # datas (dia, mês, ano) de um casamento de uma das formas; vazio para mês ou dia fora do calendário, e para
    # o mês que a regex aceita sem caixa mas não está na lista (letra com caixa especial)
    try:
        return _datas_da_forma(forma, achado.groups(), expandir)
    except KeyError:
        return set()


def _datas_da_forma(forma, partes, expandir):
    if forma is _LISTA_NO_MES:
        return {(int(dia), MESES[partes[1].lower()], _ano(partes[2])) for dia in re.findall(r"\d+", partes[0])}
    if forma is _FAIXA_ENTRE_MESES:
        return _faixa((int(partes[0]), MESES[partes[1].lower()]), (int(partes[2]), MESES[partes[3].lower()]),
                      _ano(partes[4]), expandir)
    if forma is _FAIXA_NO_MES:
        mes = MESES[partes[2].lower()]
        return _faixa((int(partes[0]), mes), (int(partes[1]), mes), _ano(partes[3]), expandir)
    if forma is _FAIXA_NUMERICA:
        if 1 <= int(partes[1]) <= 12 and 1 <= int(partes[3]) <= 12:
            return _faixa((int(partes[0]), int(partes[1])), (int(partes[2]), int(partes[3])), _ano(partes[4]), expandir)
        return set()
    if forma is _FAIXA_DE_DIAS_NUMERICA:
        if 1 <= int(partes[2]) <= 12:
            return _faixa((int(partes[0]), int(partes[2])), (int(partes[1]), int(partes[2])), _ano(partes[3]), expandir)
        return set()
    if forma is _DATA_POR_EXTENSO:
        return {(int(partes[0]), MESES[partes[1].lower()], _ano(partes[2]))}
    if 1 <= int(partes[1]) <= 12 and 1 <= int(partes[0]) <= 31:
        return {(int(partes[0]), int(partes[1]), _ano(partes[2]))}
    return set()


_FORMAS = (_LISTA_NO_MES, _FAIXA_ENTRE_MESES, _FAIXA_NO_MES, _FAIXA_NUMERICA, _FAIXA_DE_DIAS_NUMERICA, _DATA_POR_EXTENSO, _DATA_NUMERICA)


# faixas que começam por um número sem mês ("4 a 31 de julho", "05 a 07/01")
_FAIXAS_DE_NUMERO_SOLTO = (_FAIXA_NO_MES, _FAIXA_DE_DIAS_NUMERICA)
# o que vem depois de "d/m" quando ele é fração, e não data: "1/3 da carga horária", "2/3 dos votos"
_RESTO_DE_FRACAO = re.compile(r"\s+d(?:[aeo]s?)\s", re.I)


def normalizar(texto):
    """Texto com os espaços reduzidos a um e o ordinal na forma que as datas esperam.

    Nos documentos a data vem partida por quebra de linha, espaço duplo ou espaço fixo ("23 a" e "25/10/2026" em
    linhas seguidas; "16  a 17 de dezembro"; "1° de maio"); as formas de data casam com espaço simples.
    """
    return re.sub(r"\s+", " ", texto or "").replace("°", "º")


def datas(texto, expandir):
    """Conjunto de (dia, mês, ano ou None) citados no texto.

    Args:
        texto: texto livre (resposta ou contexto).
        expandir: True expande as faixas dia a dia; False guarda só as pontas.

    Returns:
        Conjunto de tuplas (dia, mês, ano), com ano None quando o texto não o traz.
    """
    achados = set()
    for forma in _FORMAS:
        for achado in forma.finditer(texto):
            achados |= _datas_do_achado(forma, achado, expandir)
    return achados


def trechos_de_data(texto):
    """Acha cada data ou faixa de datas escrita no texto, com a posição dela.

    Args:
        texto: texto livre (uma frase da resposta).

    Returns:
        Lista de (início, fim, datas) em ordem de posição, sem sobreposição: a faixa "12 a 14 de janeiro de
        2026" é um trecho só, com as duas pontas, e a data solta dentro dela não volta de novo. Número solto
        ligado a uma data por "até" não é começo de faixa ("a Etapa 4 até 31 de julho"): fica só a data. Fração
        não é data ("1/3 da carga horária").
    """
    trechos = []
    for forma in _FORMAS:
        for achado in forma.finditer(texto):
            if forma in _FAIXAS_DE_NUMERO_SOLTO and " até " in achado.group(0):
                continue
            if forma is _DATA_NUMERICA and not achado.group(3) and _RESTO_DE_FRACAO.match(texto, achado.end()):
                continue
            livre = all(achado.end() <= inicio or achado.start() >= fim for inicio, fim, _ in trechos)
            pontas = _datas_do_achado(forma, achado, expandir=False) if livre else set()
            if pontas:
                trechos.append((achado.start(), achado.end(), pontas))
    return sorted(trechos, key=lambda trecho: trecho[0])


def ancorada(data, lidas):
    """Diz se uma data aparece entre as datas lidas.

    Args:
        data: (dia, mês, ano ou None) citada na resposta.
        lidas: conjunto de datas do que o agente leu, com as faixas expandidas (datas(texto, expandir=True)).

    Returns:
        True quando alguma data lida tem o mesmo dia e mês e o ano não conflita (ano ausente casa com qualquer).
    """
    dia, mes, ano = data
    return any(dia_lido == dia and mes_lido == mes and (ano is None or ano_lido is None or ano_lido == ano)
               for dia_lido, mes_lido, ano_lido in lidas)
