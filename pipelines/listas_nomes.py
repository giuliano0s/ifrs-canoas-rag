"""Detecta lista de pessoas (candidatos, inscritos, selecionados) num documento e monta o aviso que a substitui.

Lista do processo seletivo de ingresso nunca entra na base: quem pergunta é levado ao portal pela fonte curada.
Lista de outra origem (seleções do campus, editais da reitoria) entra só como aviso: diz que o resultado foi
publicado e onde conferir, sem nenhum nome. Detecção por regra e fail-closed: na dúvida, é lista.

Sinais: número de inscrição em qualquer formato ("73.317-77", "74.303325", "300401"), CPF inteiro ou
mascarado, palavras de situação (deferida, ausente, eliminado...) e sequências com cara de nome de pessoa.
"""

import re

# trechos que tem cara de numero de inscricao, mas nao sao: processo, CNPJ, UASG, CEP, telefone, portaria
_NAO_IDENTIFICADOR = re.compile(
    r"\b\d{5}\.\d{6}/\d{4}-\d{2}\b|\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b|uasg\W{0,3}\d+|\bcep\W{0,3}[\d.\-]+"
    r"|\(\d{2}\)\s*\d{4,5}-?\d{4}|portaria\s+(?:n[ºo°.]*\s*)?[\d./]+|\bmatr[íi]cula\W{0,3}[#\d]+", re.I)
_IDENTIFICADOR = re.compile(
    r"\b\d{2,3}\.\d{3}-\d{1,3}\b|\b\d{2}\.\d{6}\b|\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b\d{2,3}\*{2,}[\d.\-]{3,}|\b\d{6,9}\b")
_SITUACAO = re.compile(
    r"\b(?:deferid[oa]s?|indeferid[oa]s?|homologad[oa]s?|n[ãa]o\s+homologad[oa]|classificad[oa]s?|desclassificad[oa]s?"
    r"|aprovad[oa]s?|reprovad[oa]s?|ausentes?|eliminad[oa]s?|contemplad[oa]s?|selecionad[oa]s?|suplentes?"
    r"|convocad[oa]s?|matriculad[oa]s?|habilitad[oa]s?|inabilitad[oa]s?)\b", re.I)
_MARCADOR = re.compile(
    r"classifica|resultado|homolog|lista|convoca|chamada|inscrit|inscri[çc][õo]es\s+aceitas|heteroidentifica"
    r"|contemplad|selecionad|ensalament|aprovad", re.I)
# no nome do arquivo em que um LLM achou nome de pessoa, "divulgacao" tambem anuncia lista (dias das entrevistas)
_MARCADOR_NO_ARQUIVO = re.compile(_MARCADOR.pattern + r"|divulga", re.I)
_PALAVRA = r"[A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõçü]+|[A-ZÁÉÍÓÚÂÊÔÃÕÇ]{2,}"
# nome de pessoa: 3 a 6 palavras com inicial maiuscula na mesma linha
_NOME = re.compile(rf"\b(?:{_PALAVRA})(?:[ \t]+(?:de|da|do|dos|das|e|DE|DA|DO|DOS|DAS|E))?(?:[ \t]+(?:{_PALAVRA})){{2,5}}\b")
# palavras comuns de documento que nunca fazem parte de um nome de pessoa
_NAO_NOME = re.compile(
    r"\b(?:publica[çc][ãa]o|divulga[çc][ãa]o|realiza[çc][ãa]o|prova|pr[áa]tica|instrumento|musical|datas?|importantes"
    r"|saiba|manh[ãa]|tarde|noite|integral|bacharelado|matr[íi]cula|online|per[íi]odo|hor[áa]rio|local|sala|pr[ée]dio"
    r"|bloco|etapa|fase|geral|final|preliminar|parcial|vagas?|ensino|m[ée]dio|pesquisa|extens[ãa]o|pr[óo]|reitora?"
    r"|diretora?|coordenadora?|oficial|rep[úu]blica|federativa|profissional|tecnol[óo]gica|desenvolvimento"
    r"|institucional|benef[íi]cios|assist[êe]ncia|estudantil|bairro|calend[áa]rio|dom|seg|ter|qua|qui|sex|s[áa]b"
    r"|janeiro|fevereiro|mar[çc]o|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro|sorteio"
    r"|p[úu]blico|banca|recurso|recursos|entrega|documentos|cota|cotas|turno|modalidade|ampla|concorr[êe]ncia)\b",
    re.I)
_INSTITUCIONAL = re.compile(
    r"campus|instituto|federal|edital|anexo|minist|secretaria|reitoria|ifrs|rio grande|educa|tecnologia|ci[êe]ncia"
    r"|curso|programa|projeto|processo|seletivo|resultado|coordena|diretoria|departamento|comiss|superior|t[ée]cnico"
    r"|licenciatura|engenharia|an[áa]lise|sistemas|administra|inscri|lista|chamada|classifica|homolog|portaria|rua\b",
    re.I)
_INGRESSO = re.compile(r"^https?://ingresso\.ifrs\.edu\.br/", re.I)
# sinais por mil caracteres: listas reais ficam acima, documentos que so citam de passagem ficam abaixo
DENSIDADE_MINIMA = 2.0
# motivos que dependem so de sequencias com cara de nome de pessoa, sem numero de inscricao
MOTIVOS_POR_NOME = {"nomes_e_situacao", "resultado_curto", "nomes_em_densidade"}

AVISO_LISTA = ("Resultado publicado: {titulo}. Por proteção de dados pessoais, o assistente não informa os nomes "
               "desta lista nem confirma se uma pessoa está nela; quem quer saber confere no documento oficial: {url}")


def _sem_falsos(texto):
    # apaga, mantendo as posicoes, o que parece numero de inscricao e nao e; com "Siape" no documento, o
    # numero de 7 digitos e a matricula do servidor numa portaria, ato publico que fica na base
    texto = _NAO_IDENTIFICADOR.sub(lambda achado: " " * len(achado.group(0)), texto)
    if re.search(r"\bsiape", texto, re.I):
        texto = re.sub(r"\b\d{7}\b", lambda achado: " " * len(achado.group(0)), texto)
    return texto


def _identificadores(texto):
    return set(_IDENTIFICADOR.findall(_sem_falsos(texto)))


def _nomes(texto):
    # posicao final de cada nome de pessoa; a palavra de situacao colada no nome ("Fulano Silva Classificado")
    # fica fora dele, para contar como a situacao que vem depois
    fins = []
    for achado in _NOME.finditer(texto):
        trecho = achado.group(0)
        corte = _SITUACAO.search(trecho)
        if corte:
            trecho = trecho[:corte.start()].rstrip()
        if len(trecho.split()) < 2 or _INSTITUCIONAL.search(trecho) or _NAO_NOME.search(trecho):
            continue
        fins.append(achado.start() + len(trecho))
    return fins


def _seguidos_de_situacao(fins, texto, alcance=150):
    # quantos nomes ou numeros (pela posicao final) tem uma palavra de situacao logo depois: a linha de uma lista
    return sum(1 for fim in fins if _SITUACAO.search(texto[fim:fim + alcance]))


def motivo_lista(texto, url="", titulo="", pagina=False):
    """Diz por que o documento e uma lista de pessoas, ou None quando nao e.

    Args:
        texto: texto extraido do documento.
        url: endereco do documento (o nome do arquivo conta como marcador).
        titulo: titulo do documento.
        pagina: True para pagina HTML, que segue regras mais estritas que o PDF de resultado.

    Returns:
        "identificadores" (8 ou mais numeros de inscricao ou CPF), "identificador_e_situacao" (marcador de
        resultado com ao menos um numero e uma palavra de situacao), "marcador_e_identificadores" (marcador
        com 3 ou mais numeros), "nomes_e_situacao" (marcador com 2 ou mais nomes seguidos de situacao),
        "resultado_curto" (resultado anunciado no endereco ou titulo, ate 4 mil caracteres, com nomes e
        situacao), "nomes_em_densidade" (resultado anunciado no endereco ou titulo e muitos nomes), ou None. Em todos, os sinais tambem precisam ser densos (por mil caracteres): uma lista
        traz numero, nome ou situacao a cada linha, e um edital, um PPC ou um pregao so os cita de passagem.
    """
    texto = texto or ""
    mil = max(len(texto), 1) / 1000
    identificadores = len(_identificadores(texto))
    if identificadores >= 8 and identificadores / mil >= DENSIDADE_MINIMA:
        return "identificadores"
    marcador = bool(_MARCADOR.search(f"{url} {titulo} {texto[:800]}"))
    if not marcador:
        return None
    # numero seguido de situacao ("74.303325 JULIA ... ELIMINADO"): lista mesmo com um candidato so
    if identificadores >= 1:
        sem_falsos = _sem_falsos(texto)
        numeros = [achado.end() for achado in _IDENTIFICADOR.finditer(sem_falsos)]
        pares = _seguidos_de_situacao(numeros, sem_falsos)
        if pares >= 1 and pares / mil >= DENSIDADE_MINIMA / 4:
            return "identificador_e_situacao"
    if identificadores >= 3 and identificadores / mil >= DENSIDADE_MINIMA / 2:
        return "marcador_e_identificadores"
    situacoes = len(_SITUACAO.findall(texto))
    nomes = _nomes(texto)
    # nome seguido de situacao ("Lavinia Machado Goldschmidt Deferida"), em densidade de lista. pagina HTML
    # (noticia, pagina de edital) cita pessoas com frequencia e exige mais pares que o PDF de resultado
    minimo_de_pares = 5 if pagina else 2
    if situacoes >= 2:
        pares = _seguidos_de_situacao(nomes, texto)
        if pares >= minimo_de_pares and pares / mil >= DENSIDADE_MINIMA / 2:
            return "nomes_e_situacao"
    # o endereco ou o titulo anuncia resultado: PDF curto com nomes e situacao, ou nomes em densidade de lista
    # mesmo sem situacao ao lado (noticia com os classificados de uma olimpiada)
    marcador_no_nome = bool(_MARCADOR.search(f"{url} {titulo}"))
    if not pagina and marcador_no_nome and len(texto) <= 4000 and len(nomes) >= 2 and situacoes >= 1:
        return "resultado_curto"
    densidade_de_nomes = (4 if pagina else 2) * DENSIDADE_MINIMA
    if marcador_no_nome and len(nomes) >= 5 and len(nomes) / mil >= densidade_de_nomes:
        return "nomes_em_densidade"
    return None


def e_do_ingresso(url):
    """Documento do portal do processo seletivo de ingresso, cuja lista nunca entra na base."""
    return bool(_INGRESSO.match(url or ""))


def destino(texto, url="", titulo="", pagina=False, linhas=0, nomes_marcados=None):
    """Decide o que fazer com um documento: o unico lugar da regra, usado pelos parsers, pela migracao e
    pela coleta dos editais da Reitoria.

    Args:
        texto: texto extraido do documento.
        url: endereco do documento.
        titulo: titulo do documento.
        pagina: True para pagina HTML.
        linhas: total de linhas do documento, quando um LLM marcou as linhas com nome de pessoa.
        nomes_marcados: linhas com nome de pessoa marcadas pelo LLM, ou None quando nenhum LLM leu o
            documento. Sao mais um sinal: 10% ou mais das linhas, ou algum nome num documento cujo endereco
            anuncia resultado, tambem fazem lista. Zero desfaz os sinais que dependem so do formato de nome
            (titulo de projeto e assinatura tambem tem iniciais maiusculas); os de numero de inscricao ficam.

    Returns:
        (None, None) quando nao e lista de pessoas; ("fora", motivo) para PDF do processo seletivo de
        ingresso, que nao entra na base e vai ao registro de PDFs barrados; ("aviso", motivo) para as
        demais, que entram so com o aviso_lista. Pagina HTML de lista vira aviso mesmo no ingresso,
        porque so o PDF tem registro que impede o crawler de rele-lo todo dia.
    """
    motivo = motivo_lista(texto, url, titulo, pagina)
    if motivo in MOTIVOS_POR_NOME and nomes_marcados == 0 and linhas:
        motivo = None
    if not motivo and nomes_marcados and linhas:
        if nomes_marcados / linhas >= 0.10 or _MARCADOR_NO_ARQUIVO.search(url.rsplit("/", 1)[-1]):
            motivo = "nomes_marcados"
    if not motivo:
        return None, None
    return ("fora" if e_do_ingresso(url) and not pagina else "aviso"), motivo


def aviso_lista(url, titulo=""):
    """Texto que substitui a lista: o resultado existe e onde conferir, sem nomes."""
    nome = titulo or re.sub(r"[-_]+", " ", (url or "").rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0])
    return AVISO_LISTA.format(titulo=nome.strip(), url=url)
