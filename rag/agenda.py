"""Leitura temporal do contexto: situa cada linha de evento datado em relação à data do pedido.

O modelo sem raciocínio erra a comparação de datas quando precisa varrer muitos eventos ("qual o próximo
feriado?" pulava o de 28/10 e respondia o de 02/11). Aqui o código faz a conta e escreve o resultado ao lado
de cada evento, e o modelo só lê: "Dia do Servidor Público (feriado/não letivo): 28 de outubro de 2026.
[daqui a 6 dias]".

Linha de evento datado é definida pela forma. Em qualquer trecho: "rótulo: data", em que o que vem depois
dos dois-pontos é só uma data ou um período (o formato do calendário acadêmico estruturado e das fontes
curadas). Só nos trechos de cronograma, em que quem chama pede (datas_soltas): a linha que é só uma data, com
o período podendo ocupar duas ou três linhas ("30/09 a" e "19/10/2026"; "30/09", "a" e "19/10/2026"). Fora do
cronograma a data sozinha na linha costuma ser a data de publicação do item de cima, numa listagem, e a
situação dela ao lado do título enganaria. Data no meio de uma frase nunca é marcada.
"""

import re
from datetime import date, timedelta

MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
         "novembro", "dezembro")
_MES = "|".join(MESES) + "|marco"
_DIA = r"(\d{1,2})(?:º|°)?"
_LIGA = r"\s+(a|e|até)\s+"

# as formas de data e de período que uma linha de evento traz, casadas contra o trecho inteiro
_FORMAS = (
    # "02 de novembro de 2026"
    ("dia", re.compile(rf"{_DIA}\s+de\s+({_MES})\s+de\s+(20\d{{2}})", re.I)),
    # "04 a 05 de novembro de 2026", "16 e 17 de dezembro de 2026"
    ("dias_do_mes", re.compile(rf"{_DIA}{_LIGA}{_DIA}\s+de\s+({_MES})\s+de\s+(20\d{{2}})", re.I)),
    # "27 de janeiro a 10 de fevereiro de 2026", "20 de dezembro de 2026 a 05 de janeiro de 2027"
    ("entre_meses", re.compile(rf"{_DIA}\s+de\s+({_MES})(?:\s+de\s+(20\d{{2}}))?\s+(?:a|até)\s+{_DIA}\s+de\s+({_MES})"
                               rf"\s+de\s+(20\d{{2}})", re.I)),
    # "outubro de 2026", "janeiro a março de 2026"
    ("meses", re.compile(rf"(?:({_MES})\s+a\s+)?({_MES})\s+de\s+(20\d{{2}})", re.I)),
    # "19/10/2026"
    ("numerica", re.compile(r"(\d{1,2})/(\d{1,2})/(20\d{2})")),
    # "30/09 a 19/10/2026", "28/12/2026 a 05/01/2027"
    ("faixa_numerica", re.compile(r"(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?\s+(?:a|até)\s+(\d{1,2})/(\d{1,2})/(20\d{2})", re.I)),
    # "05 a 07/01/2026", "07 e 08/12/2026"
    ("dias_numericos", re.compile(rf"{_DIA}{_LIGA}(\d{{1,2}})/(\d{{1,2}})/(20\d{{2}})", re.I)),
)
# linha que é só o começo de um período, com o fim na linha seguinte ("30/09 a", "De 20 a", "07 e")
_INICIO_DE_FAIXA = re.compile(r"(?:de\s+)?(\d{1,2}(?:º|°)?(?:/\d{1,2}(?:/20\d{2})?)?\s+(?:a|e|até))", re.I)
# linha que é só uma data, com ou sem o ano, e linha que é só a ligação entre duas datas ("a", "até")
_DATA_SOLTA = re.compile(r"\d{1,2}/\d{1,2}(?:/20\d{2})?")
_SO_LIGACAO = re.compile(r"a|até", re.I)
# teto do trecho testado como data: nenhuma forma passa disso, e linha longa não custa tempo
MAXIMO_DE_UMA_DATA = 60


def _mes(nome):
    nome = nome.lower()
    return 3 if nome == "marco" else MESES.index(nome) + 1


def _data(ano, mes, dia):
    # None para data impossível (31/02), em vez de erro
    try:
        return date(int(ano), int(mes), int(dia))
    except ValueError:
        return None


def _faixa(inicio_sem_ano, fim, ano_do_inicio, liga="a"):
    # período em que o começo pode vir sem o ano: vale o ano do fim, ou o anterior quando o período vira o ano.
    # ligados por "e", dois dias só formam período quando são seguidos ("16 e 17"); "02 e 09" são dias avulsos
    if fim is None:
        return None
    mes, dia = inicio_sem_ano
    inicio = _data(ano_do_inicio or fim.year, mes, dia)
    if inicio and inicio > fim and not ano_do_inicio:
        inicio = _data(fim.year - 1, mes, dia)
    if not inicio or inicio > fim or (liga.lower() == "e" and (fim - inicio).days != 1):
        return None
    return inicio, fim


def _ler(nome, partes):
    # (início, fim) de um casamento de uma das formas, ou None
    if nome == "dia":
        dia = _data(partes[2], _mes(partes[1]), partes[0])
        return (dia, dia) if dia else None
    if nome == "dias_do_mes":
        return _faixa((_mes(partes[3]), partes[0]), _data(partes[4], _mes(partes[3]), partes[2]), partes[4], partes[1])
    if nome == "entre_meses":
        return _faixa((_mes(partes[1]), partes[0]), _data(partes[5], _mes(partes[4]), partes[3]), partes[2])
    if nome == "meses":
        ano, ultimo = int(partes[2]), _mes(partes[1])
        primeiro = _mes(partes[0]) if partes[0] else ultimo
        fim = date(ano + ultimo // 12, ultimo % 12 + 1, 1) - timedelta(days=1)
        return (date(ano, primeiro, 1), fim) if primeiro <= ultimo else None
    if nome == "numerica":
        dia = _data(partes[2], partes[1], partes[0])
        return (dia, dia) if dia else None
    if nome == "faixa_numerica":
        return _faixa((partes[1], partes[0]), _data(partes[5], partes[4], partes[3]), partes[2])
    return _faixa((partes[3], partes[0]), _data(partes[4], partes[3], partes[2]), partes[4], partes[1])


def periodo(expressao):
    """Lê uma data ou um período escrito sozinho.

    Args:
        expressao: o trecho que deve ser só a data ("28 de outubro de 2026", "16 a 27 de novembro de 2026",
            "30/09 a 19/10/2026", "outubro de 2026"), com ou sem ponto final.

    Returns:
        (início, fim) como datas, iguais quando é um dia só; None quando o trecho não é só uma data, a data
        não traz o ano ou é impossível, ou quando são dois dias avulsos ligados por "e" ("02 e 09 de dezembro").
    """
    limpa = re.sub(r"\s+", " ", (expressao or "").strip()).rstrip(".").strip()
    if not limpa or len(limpa) > MAXIMO_DE_UMA_DATA:
        return None
    for nome, forma in _FORMAS:
        achado = forma.fullmatch(limpa)
        if achado:
            # mês que a regex aceita sem caixa mas não está na lista (letra com caixa especial) não é data
            try:
                return _ler(nome, achado.groups())
            except ValueError:
                return None
    return None


def _em_dias(dias):
    return "amanhã" if dias == 1 else f"em {dias} dias"


def situacao(inicio, fim, hoje):
    """Diz onde um evento está em relação a hoje.

    Args:
        inicio: primeiro dia do evento.
        fim: último dia do evento (igual ao início quando é um dia só).
        hoje: data do pedido.

    Returns:
        "já passou", "hoje", "amanhã", "daqui a N dias" ou, com hoje dentro do período, "em andamento" com
        o quanto falta para terminar ("em andamento, termina hoje", "em andamento, termina em 5 dias").
    """
    if fim < hoje:
        return "já passou"
    if inicio > hoje:
        dias = (inicio - hoje).days
        return "amanhã" if dias == 1 else f"daqui a {dias} dias"
    if inicio == fim:
        return "hoje"
    return "em andamento, termina hoje" if fim == hoje else f"em andamento, termina {_em_dias((fim - hoje).days)}"


def eventos_datados(texto, datas_soltas=False):
    """Acha as linhas de evento datado de um texto.

    Args:
        texto: texto de um trecho do contexto.
        datas_soltas: True nos trechos de cronograma, em que a linha que é só uma data também é evento.

    Returns:
        Lista de (índice da linha que recebe a marca, início, fim), na ordem do texto. No período partido em
        mais de uma linha ("30/09 a" e "19/10/2026"), o índice é o da última.
    """
    linhas = (texto or "").split("\n")
    achados, inicio_pendente, data_anterior = [], None, None
    for indice, linha in enumerate(linhas):
        limpa = linha.strip()
        if datas_soltas:
            # começo de período sozinho na linha ("30/09 a", "De 20 a"): espera o fim na linha seguinte
            comeco = _INICIO_DE_FAIXA.fullmatch(limpa)
            if comeco:
                inicio_pendente, data_anterior = comeco.group(1), None
                continue
            # a ligação sozinha na linha, depois de uma data: a data de cima era o começo do período
            if _SO_LIGACAO.fullmatch(limpa) and data_anterior:
                if achados and achados[-1][0] == data_anterior[0]:
                    achados.pop()
                inicio_pendente, data_anterior = f"{data_anterior[1]} a", None
                continue
            if inicio_pendente:
                lido = periodo(f"{inicio_pendente} {limpa}")
                inicio_pendente = None
                if lido:
                    achados.append((indice, *lido))
                    continue
            # a linha que é só uma data ou um período; a data sem o ano fica guardada, pode ser o começo de um
            data_anterior = (indice, limpa) if _DATA_SOLTA.fullmatch(limpa) else None
            lido = periodo(limpa)
            if lido:
                achados.append((indice, *lido))
            if lido or data_anterior:
                continue

        # "rótulo: data"
        lido = periodo(limpa.rsplit(":", 1)[1]) if ":" in limpa else None
        if lido:
            achados.append((indice, *lido))
    return achados


def marcar_eventos(texto, hoje, datas_soltas=False):
    """Escreve, ao lado de cada linha de evento datado, a situação dela em relação a hoje.

    Args:
        texto: texto de um trecho do contexto.
        hoje: data do pedido.
        datas_soltas: True nos trechos de cronograma, em que a linha que é só uma data também é marcada.

    Returns:
        O texto com " [situação]" no fim de cada linha de evento; o próprio texto quando não há nenhuma.
    """
    eventos = eventos_datados(texto, datas_soltas)
    if not eventos:
        return texto
    linhas = texto.split("\n")
    for indice, inicio, fim in eventos:
        linhas[indice] = f"{linhas[indice].rstrip()} [{situacao(inicio, fim, hoje)}]"
    return "\n".join(linhas)


def trechos_como_lidos(busca, hoje):
    """Refaz, de uma busca gravada numa coleta, o texto de cada trecho como o modelo o leu.

    Args:
        busca: registro de uma busca (trace["buscas"]): chunks_textos, com o texto da base, e
            contexto_datas_soltas, que diz em quais trechos a data sozinha na linha foi marcada (ausente nas
            coletas anteriores a 23/11/2026, em que nenhuma é marcada aqui).
        hoje: data do pedido daquela execução, ou None para devolver o texto da base.

    Returns:
        Lista com o texto de cada trecho, com a situação dos eventos datados em relação a hoje.
    """
    textos = busca.get("chunks_textos") or []
    if not hoje:
        return list(textos)
    soltas = busca.get("contexto_datas_soltas") or [False] * len(textos)
    return [marcar_eventos(texto, hoje, datas_soltas=solta) for texto, solta in zip(textos, soltas)]


def primeiro_dia(texto):
    """Primeiro dia entre os eventos datados do texto, para ordenar blocos de um calendário; None sem evento."""
    inicios = [inicio for _, inicio, _ in eventos_datados(texto)]
    return min(inicios) if inicios else None
