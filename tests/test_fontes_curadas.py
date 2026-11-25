"""Testes das fontes curadas com campo derivado (pipelines/fontes_curadas.py), sem rede.

Uso, a partir da raiz: python -m tests.test_fontes_curadas
"""
from datetime import date

from pipelines import fontes_curadas
from pipelines.chunker import chunks_de_cronograma, eventos_de_cronograma, nome_do_evento, rotulos_de_trilha
from pipelines.fontes_curadas import (carregar, chunks_da_fonte, datas_do_resultado, documentos_para_derivar,
                                      resultados_do_processo, run_fontes_curadas, texto_da_fonte)
from rag.agenda import marcar_eventos

PORTAL = "https://ingresso.ifrs.edu.br/"

# cronograma com dois rotulos de trilha antes do nome do evento (o formato do 2027/1)
CRONOGRAMA_2027 = """Início
»
Cronograma
30/09 a
                                                  19/10/2026
PS Regular
PERÍODO PARA INSCRIÇÃO E SOLICITAÇÃO DE ISENÇÃO DA TAXA DE INSCRIÇÃO
22/10/2026
PS Regular
PUBLICAÇÃO DA LISTA PRELIMINAR DE INSCRITOS HOMOLOGADOS
06/12/2026
PS Regular
APLICAÇÃO DA PROVA
08/12/2026
PS Indígena
APLICAÇÃO DA PROVA DE REDAÇÃO
09/12/2026
PS Indígena
DIVULGAÇÃO DO GABARITO
10/12/2026
PS Regular
PS Indígena
REALIZAÇÃO DO SORTEIO PÚBLICO
O sorteio define a classificação dos candidatos inscritos nessa modalidade.
05/01/2027
PS Regular
PS Indígena
PUBLICAÇÃO DA CLASSIFICAÇÃO GERAL E DO RESULTADO DO PROCESSO SELETIVO
Publicação da Classificação Geral e Aprovados em 1ª Chamada.
06/01 a
                                                  12/01/2027
PS Regular
PS Indígena
PERÍODO PARA ENVIO DA DOCUMENTAÇÃO DE MATRÍCULA
22/01/2027
PS Regular
PREVISÃO DE PUBLICAÇÃO DA CLASSIFICAÇÃO PELO ENEM
Previsão"""

# cronograma sem rotulo de trilha, com um evento de outro campus (o formato do 2026/1 e do 2026/2)
CRONOGRAMA_2026_2 = """Início
/
Cronograma
23/04 a
                                                  04/05/2026
Período de Inscrição
17/06/2026
Publicação da Classificação Geral e Aprovados em 1ª Chamada
Publicação da Classificação Geral e Aprovados em 1ª Chamada. A publicação será realizada no site
18/06/2026
PS CAMPUS VIAMÃO – Publicação da Classificação e Aprovados em 1ª Chamada
01/07/2026
Prova, ENEM e Sorteio: Resultado Preliminar da matrícula em Primeira Chamada"""

CRONOGRAMA_2026_1 = """17/12/2025
Publicação da Classificação Geral e Aprovados em 1ª Chamada.
23/01/2026
ENEM – PUBLICAÇÃO DA CLASSIFICAÇÃO"""

DOCUMENTOS = {
    PORTAL + "2027/cronograma/": ("Cronograma - Processo Seletivo IFRS 2027/1", CRONOGRAMA_2027),
    PORTAL + "2026-2/cronograma/": ("Cronograma - Processo Seletivo IFRS 2026/2", CRONOGRAMA_2026_2),
    PORTAL + "2026/cronograma/": ("Cronograma - Processo Seletivo IFRS 2026/1", CRONOGRAMA_2026_1),
    # fora: filtro de trilha da mesma pagina, cronograma de matricula e pagina que nao e do portal
    PORTAL + "2027/cronograma/?trilha=regular": ("Cronograma - Processo Seletivo IFRS 2027/1", CRONOGRAMA_2027),
    PORTAL + "2026/publicacoes/cronograma-matricula-enem/": ("CRONOGRAMA MATRÍCULA ENEM - Processo Seletivo IFRS 2026/1",
                                                            "01/02/2026\nPublicação da classificação"),
    "https://ifrs.edu.br/canoas/cronograma/": ("Cronograma - Processo Seletivo IFRS 2030/1",
                                               "01/02/2030\nPublicação da classificação geral"),
}


def testar_nome_do_evento():
    eventos = eventos_de_cronograma(CRONOGRAMA_2027)
    rotulos = rotulos_de_trilha(eventos)
    assert rotulos == {"PS Regular", "PS Indígena"}
    nomes = [nome_do_evento(linhas, rotulos) for _, linhas in eventos]
    assert nomes[0] == (["PS Regular"], "PERÍODO PARA INSCRIÇÃO E SOLICITAÇÃO DE ISENÇÃO DA TAXA DE INSCRIÇÃO")
    assert nomes[6] == (["PS Regular", "PS Indígena"], "PUBLICAÇÃO DA CLASSIFICAÇÃO GERAL E DO RESULTADO DO PROCESSO SELETIVO")
    assert nome_do_evento(["PS Regular"], rotulos) == ([], "PS Regular")

    # a frase de busca do evento com dois rotulos traz o nome dele
    frases = [chunk["texto_busca"] for chunk in chunks_de_cronograma(CRONOGRAMA_2027, "Cronograma 2027/1", {})]
    assert frases[6] == ("Cronograma 2027/1: 05/01/2027 PS Regular PS Indígena PUBLICAÇÃO DA CLASSIFICAÇÃO GERAL E DO "
                         "RESULTADO DO PROCESSO SELETIVO")
    assert frases[0].endswith("30/09 a 19/10/2026 PS Regular PERÍODO PARA INSCRIÇÃO E SOLICITAÇÃO DE ISENÇÃO DA TAXA DE INSCRIÇÃO")


def testar_resultados_do_processo():
    lido = resultados_do_processo(PORTAL + "2027/cronograma/", *DOCUMENTOS[PORTAL + "2027/cronograma/"])
    assert lido["processo"] == "2027/1" and lido["site"] == PORTAL + "2027/"
    assert lido["eventos"] == [
        ("PUBLICAÇÃO DA CLASSIFICAÇÃO GERAL E DO RESULTADO DO PROCESSO SELETIVO", date(2027, 1, 5), date(2027, 1, 5)),
        ("PREVISÃO DE PUBLICAÇÃO DA CLASSIFICAÇÃO PELO ENEM", date(2027, 1, 22), date(2027, 1, 22))]

    # a descricao que cita "classificacao" nao conta (o sorteio), nem o evento de outro campus
    lido = resultados_do_processo(PORTAL + "2026-2/cronograma/", *DOCUMENTOS[PORTAL + "2026-2/cronograma/"])
    assert lido["eventos"] == [("Publicação da Classificação Geral e Aprovados em 1ª Chamada", date(2026, 6, 17), date(2026, 6, 17))]

    # rotulo de trilha que nomeia outro campus tira o evento, mesmo com o nome dele sem o campus
    outro_campus = "\n".join(f"0{dia}/02/2027\nPS Campus Viamão\n{nome}" for dia, nome in (
        (1, "INSCRIÇÕES"), (2, "SORTEIO"), (4, "PUBLICAÇÃO DA CLASSIFICAÇÃO E APROVADOS EM 1ª CHAMADA")))
    lido = resultados_do_processo(PORTAL + "2027/cronograma/", "Processo Seletivo IFRS 2027/1",
                                  CRONOGRAMA_2027 + "\n" + outro_campus)
    assert [fim for _, _, fim in lido["eventos"]] == [date(2027, 1, 5), date(2027, 1, 22)]

    # nome do resultado repetido em tres eventos (um por modo de ingresso) conta como rotulo e segue achado
    repetido = "\n".join(f"{dia}/01/2028\nPublicação da Classificação Geral\nModo de ingresso {modo}" for dia, modo in (
        (10, "prova"), (11, "sorteio"), (12, "ENEM")))
    lido = resultados_do_processo(PORTAL + "2028/cronograma/", "Processo Seletivo IFRS 2028/1", repetido)
    assert [(nome, fim) for nome, _, fim in lido["eventos"]] == [
        ("Publicação da Classificação Geral", date(2028, 1, 10)), ("Publicação da Classificação Geral", date(2028, 1, 11)),
        ("Publicação da Classificação Geral", date(2028, 1, 12))]

    # sem processo no titulo, ou sem evento de resultado, nao ha o que derivar
    assert resultados_do_processo(PORTAL + "x/cronograma/", "Cronograma", CRONOGRAMA_2026_1) is None
    assert resultados_do_processo(PORTAL + "x/cronograma/", "Processo Seletivo IFRS 2028/1", "01/02/2028\nInscrições") is None


def testar_datas_do_resultado():
    # antes do resultado do 2027/1: ele, por sair, e o 2026/2, o ultimo ja publicado; o 2026/1 fica de fora
    linhas = datas_do_resultado(DOCUMENTOS, date(2026, 11, 23)).split("\n")
    assert linhas == [
        "Processo Seletivo 2027/1 (site https://ingresso.ifrs.edu.br/2027/), PUBLICAÇÃO DA CLASSIFICAÇÃO GERAL E DO RESULTADO "
        "DO PROCESSO SELETIVO: 05 de janeiro de 2027.",
        "Processo Seletivo 2027/1 (site https://ingresso.ifrs.edu.br/2027/), PREVISÃO DE PUBLICAÇÃO DA CLASSIFICAÇÃO PELO ENEM: "
        "22 de janeiro de 2027.",
        "Processo Seletivo 2026/2 (site https://ingresso.ifrs.edu.br/2026-2/), Publicação da Classificação Geral e Aprovados em "
        "1ª Chamada: 17 de junho de 2026."]

    # entre a classificacao geral e a do ENEM, o 2027/1 ainda tem resultado por sair; depois dela, so ele fica
    assert len(datas_do_resultado(DOCUMENTOS, date(2027, 1, 10)).split("\n")) == 3
    depois = datas_do_resultado(DOCUMENTOS, date(2027, 2, 1)).split("\n")
    assert len(depois) == 2 and all("2027/1" in linha for linha in depois)

    # antes de o site do 2027/1 existir, o 2026/2 por sair e o 2026/1 ja publicado
    antigos = {url: documento for url, documento in DOCUMENTOS.items() if "2027" not in url}
    linhas = datas_do_resultado(antigos, date(2026, 5, 1)).split("\n")
    assert [linha.split(" (")[0] for linha in linhas] == ["Processo Seletivo 2026/2", "Processo Seletivo 2026/1",
                                                         "Processo Seletivo 2026/1"]
    assert datas_do_resultado({}, date(2026, 11, 23)) is None

    # as linhas saem no formato que o atendimento situa em relacao a hoje
    marcadas = marcar_eventos(datas_do_resultado(DOCUMENTOS, date(2026, 11, 23)), date(2026, 11, 23)).split("\n")
    assert marcadas[0].endswith("05 de janeiro de 2027. [daqui a 43 dias]")
    assert marcadas[2].endswith("17 de junho de 2026. [já passou]")


def testar_texto_e_chunks():
    fonte = {"url": PORTAL + "#resultados", "titulo": "Listas", "texto": "Caminho do buscador.\n{datas_do_resultado}\nEscopo.",
             "textos_busca": ["lista de aprovados", "fui aprovado?"], "origem": "x", "revisar_ate": "2099-01-01"}
    hoje = date(2026, 11, 23)
    texto = texto_da_fonte(fonte, DOCUMENTOS, hoje)
    assert texto.startswith("Caminho do buscador.\nProcesso Seletivo 2027/1 (site") and texto.endswith("\nEscopo.")
    assert "{" not in texto

    # sem dado para derivar, ou com campo desconhecido, a fonte nao tem texto; sem campo, o texto e o do arquivo
    assert texto_da_fonte(fonte, {}, hoje) is None
    # com a reserva pedida, o campo sem dado vira a frase que manda ao cronograma
    reserva = texto_da_fonte(fonte, {}, hoje, com_reserva=True)
    assert reserva.startswith("Caminho do buscador.\n(o site de cada processo seletivo") and "{" not in reserva
    # chave que nao e campo derivado e texto comum: a fonte nao congela por causa dela
    assert texto_da_fonte({**fonte, "texto": "Pesquise pelo {nome} ou {"}, {}, hoje) == "Pesquise pelo {nome} ou {"
    assert "{nome}" in texto_da_fonte({**fonte, "texto": "{nome}: {datas_do_resultado}"}, DOCUMENTOS, hoje)
    assert texto_da_fonte({**fonte, "texto": "sem campo"}, {}, hoje) == "sem campo"

    # o hash acompanha o texto derivado: muda quando o cronograma muda, e nao muda sem mudanca
    chunks = chunks_da_fonte(fonte, 2026, texto)
    assert [chunk["texto_busca"] for chunk in chunks] == fonte["textos_busca"]
    assert all(chunk["text"] == texto for chunk in chunks)
    assert chunks[0]["source_hash"] == chunks_da_fonte(fonte, 2026, texto)[0]["source_hash"]
    adiado = {**DOCUMENTOS, PORTAL + "2027/cronograma/": (DOCUMENTOS[PORTAL + "2027/cronograma/"][0],
                                                          CRONOGRAMA_2027.replace("05/01/2027", "07/01/2027"))}
    outro = texto_da_fonte(fonte, adiado, hoje)
    assert "07 de janeiro de 2027" in outro
    assert chunks_da_fonte(fonte, 2026, outro)[0]["source_hash"] != chunks[0]["source_hash"]

    # a fonte do arquivo real preenche todos os campos com os cronogramas de exemplo
    for real in carregar():
        pronto = texto_da_fonte(real, DOCUMENTOS, hoje)
        assert pronto and "{" not in pronto, real["url"]


class _IndexFalso:
    """Base de mentira: devolve o metadata gravado e registra o que foi apagado."""

    def __init__(self, vetores):
        self.vetores, self.apagados = vetores, []

    def fetch(self, ids, include_metadata=False):
        return [type("Vetor", (), {"metadata": self.vetores.get(i)})() for i in ids]

    def delete(self, ids):
        self.apagados.extend(ids)


def testar_run_fontes_curadas():
    fonte = {"url": PORTAL + "#resultados", "titulo": "Listas", "texto": "Caminho.\n{datas_do_resultado}",
             "textos_busca": ["lista de aprovados"], "origem": "x", "revisar_ate": "2099-01-01"}
    url_2027, url_2026_2 = PORTAL + "2027/cronograma/", PORTAL + "2026-2/cronograma/"
    falso = _IndexFalso({url_2027 + "#0": {"title": DOCUMENTOS[url_2027][0], "text": CRONOGRAMA_2027},
                         url_2026_2 + "#0": {"title": DOCUMENTOS[url_2026_2][0], "text": CRONOGRAMA_2026_2}})
    estado = {url_2027: {"ids": [url_2027 + "#0", url_2027 + "#1"], "type": "html"},
              url_2026_2: {"ids": [url_2026_2 + "#0"], "type": "html"},
              PORTAL + "#antiga": {"ids": ["antiga#0"], "type": "curada"}}
    originais = (fontes_curadas.index, fontes_curadas.carregar)
    fontes_curadas.index, fontes_curadas.carregar = falso, lambda: [fonte]
    try:
        # o cronograma vem da base; a pagina que mudou nesta ingestao vale no lugar da gravada
        documentos = documentos_para_derivar(estado)
        assert set(documentos) == {url_2027, url_2026_2} and documentos[url_2027][1] == CRONOGRAMA_2027
        novo = {"source_url": url_2027, "title": DOCUMENTOS[url_2027][0], "text": CRONOGRAMA_2027.replace("05/01/2027", "07/01/2027")}
        assert "07/01/2027" in documentos_para_derivar(estado, [novo])[url_2027][1]

        # fonte nova entra; igual a gravada nao volta; a que saiu do arquivo e apagada
        chunks, mudadas = run_fontes_curadas(estado)
        assert len(chunks) == 1 and not mudadas and "Processo Seletivo 2027/1" in chunks[0]["text"]
        assert falso.apagados == ["antiga#0"]
        gravado = {**estado, fonte["url"]: {"ids": ["f#0"], "type": "curada", "source_hash": chunks[0]["source_hash"]}}
        assert run_fontes_curadas(gravado) == ([], set())

        # cronograma mudou: a fonte e substituida
        chunks, mudadas = run_fontes_curadas(gravado, [novo])
        assert mudadas == {fonte["url"]} and "07 de janeiro de 2027" in chunks[0]["text"]

        # sem cronograma na base, a fonte gravada fica como esta
        sem_cronograma = {fonte["url"]: gravado[fonte["url"]]}
        assert run_fontes_curadas(sem_cronograma) == ([], set())

        # sem cronograma e sem a fonte na base (base zerada), ela entra com o texto de reserva, para nao sumir
        chunks, mudadas = run_fontes_curadas({})
        assert len(chunks) == 1 and not mudadas and chunks[0]["text"].startswith("Caminho.\n(o site de cada processo seletivo")
    finally:
        fontes_curadas.index, fontes_curadas.carregar = originais


if __name__ == "__main__":
    testar_nome_do_evento()
    testar_resultados_do_processo()
    testar_datas_do_resultado()
    testar_texto_e_chunks()
    testar_run_fontes_curadas()
    print("ok: fontes curadas")
