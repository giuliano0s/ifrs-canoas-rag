"""Fontes curadas: textos curtos com um fato oficial que a base raspada não traz, por decisão (as listas
com nomes de candidatos ficam fora, por privacidade) ou por estrutura (o portal só mostra o resultado num
buscador). Vivem em data/info/fontes_curadas.json, cada uma com a origem do fato e a data até a qual vale
sem revisão, e entram pela ingestão como qualquer documento: hash do conteúdo, troca quando muda.

Cada fonte é buscada pelas frases no formato das perguntas que ela responde (textos_busca, um vetor por
frase) e entregue com o texto inteiro, como os eventos do calendário; o contexto do atendimento colapsa
trechos repetidos, então a fonte ocupa no máximo uma vaga. A URL leva um fragmento (#...), que o crawler
descarta de todo link que segue: a identidade da fonte curada nunca colide com a de uma página raspada, e
o link continua abrindo a página oficial.

O texto de uma fonte pode trazer um campo entre chaves ({datas_do_resultado}) que a ingestão preenche com
um dado derivado dos documentos da base (DERIVADOS): a parte que muda a cada processo seletivo sai do
cronograma publicado, e não de uma data escrita à mão que envelheceria. Quando o dado não pode ser derivado,
a fonte que já está na base não é regravada e fica como está, com aviso; a que ainda não está (primeira
ingestão, ou base zerada) entra com o texto de reserva do campo. Chave com outro nome é texto comum.

Uso avulso, sem rodar o crawler: python -m pipelines.fontes_curadas
"""

import json
import os
import re
from datetime import date

from pipelines.chunker import eventos_de_cronograma, nome_do_evento, outra_unidade_nomeada, rotulos_de_trilha
from pipelines.config import FONTES_CURADAS_PATH, INGRESSO_URL, REINGEST, REPARSE, index
from pipelines.hashing import source_hash
from rag.agenda import MESES, periodo

TIPO = "curada"
_CAMPOS = ("url", "titulo", "texto", "origem", "revisar_ate")
_CAMPO_DERIVADO = re.compile(r"\{(\w+)\}")

# cronograma de um processo seletivo no portal de ingresso (a página geral, sem o filtro de trilha) e, no
# mesmo site, a página em que o resultado é publicado
_CRONOGRAMA_DO_PROCESSO = re.compile(re.escape(INGRESSO_URL) + r"[^/?#]+/cronograma/")
_PROCESSO_NO_TITULO = re.compile(r"Processo Seletivo(?: IFRS)? (\d{4}/\d)")
# evento em que o resultado sai: a publicação da classificação (geral, e a do ENEM quando tem data própria). Nos
# cinco processos da base em 23/11/2026 (2025/1 a 2027/1) o nome traz as duas palavras, nessa ordem
_EVENTO_DE_RESULTADO = re.compile(r"publica[cç][aã]o.*classifica[cç][aã]o", re.I)


def _avisar(mensagem):
    # no GitHub Actions vira anotação na página da execução; local, linha de log
    prefixo = "::warning title=Fonte curada::" if os.getenv("GITHUB_ACTIONS") == "true" else "AVISO: "
    print(prefixo + mensagem)


def carregar():
    """Lê as fontes do arquivo de dados.

    Returns:
        Lista de fontes válidas. Entrada sem campo obrigatório ou sem o fragmento # na URL fica de fora,
        com aviso, e os chunks que ela já tem na base são preservados.
    """
    if not FONTES_CURADAS_PATH.exists():
        return []
    fontes = []
    for fonte in json.loads(FONTES_CURADAS_PATH.read_text(encoding="utf-8")).get("fontes", []):
        faltando = [campo for campo in _CAMPOS if not fonte.get(campo)]
        if faltando or "#" not in fonte.get("url", ""):
            _avisar(f"fonte ignorada ({fonte.get('url')}): falta {', '.join(faltando) or 'o fragmento # na url'}")
            continue
        fontes.append(fonte)
    return fontes


def _por_extenso(dia):
    return f"{dia.day:02d} de {MESES[dia.month - 1]} de {dia.year}"


def _data_por_extenso(inicio, fim):
    # no formato das linhas de evento que o atendimento situa em relação a hoje (rag.agenda)
    if inicio == fim:
        return _por_extenso(fim)
    if (inicio.month, inicio.year) == (fim.month, fim.year):
        return f"{inicio.day:02d} a {_por_extenso(fim)}"
    return f"{_por_extenso(inicio)} a {_por_extenso(fim)}"


def resultados_do_processo(url, titulo, texto):
    """Lê, no cronograma de um processo seletivo, quando o resultado é publicado.

    Args:
        url: endereço do cronograma no portal de ingresso.
        titulo: título da página, que nomeia o processo ("Cronograma - Processo Seletivo IFRS 2027/1").
        texto: texto da página.

    Returns:
        Dicionário com o processo ("2027/1"), o site dele e os eventos de resultado, cada um como
        (nome, início, fim); None quando o título não nomeia o processo ou nenhum evento de publicação da
        classificação tem data legível. Evento que nomeia outra unidade do IFRS ("PS CAMPUS VIAMÃO") fica de fora.
    """
    processo = _PROCESSO_NO_TITULO.search(titulo or "")
    eventos = eventos_de_cronograma(texto or "")
    rotulos = rotulos_de_trilha(eventos)
    resultados = []
    for datas, linhas in eventos:
        # o evento é julgado pelo nome junto dos rótulos que o abrem: o rótulo pode nomear outro campus ("PS
        # Campus Viamão"), e o nome que se repete em vários eventos (um por modo de ingresso) conta como rótulo
        abertura, nome = nome_do_evento(linhas, rotulos)
        cabecalho = [re.sub(r"\s+", " ", linha).strip(" .") for linha in abertura + [nome]]
        lido = periodo(datas)
        if lido and _EVENTO_DE_RESULTADO.search(" ".join(cabecalho)) and not outra_unidade_nomeada(" ".join(cabecalho)):
            resultados.append((next((linha for linha in cabecalho if _EVENTO_DE_RESULTADO.search(linha)), cabecalho[-1]), *lido))
    if not processo or not resultados:
        return None
    return {"processo": processo.group(1), "site": url[:url.rindex("cronograma/")], "eventos": resultados}


def datas_do_resultado(documentos, hoje):
    """Monta as linhas com a data de publicação do resultado de cada processo seletivo de ingresso em vigor.

    Em vigor são os processos com resultado ainda por sair e o último que já saiu: quem pergunta pelo
    resultado ou está esperando o próximo, ou entrou no mais recente.

    Args:
        documentos: {url: (título, texto)} dos documentos da base.
        hoje: data da ingestão.

    Returns:
        Uma linha por evento, do processo mais recente para o mais antigo, no formato "rótulo: data" que o
        atendimento situa em relação à data de cada pergunta; None quando nenhum cronograma de processo
        seletivo com data de resultado está na base.
    """
    processos = [lido for url, (titulo, texto) in documentos.items() if _CRONOGRAMA_DO_PROCESSO.fullmatch(url)
                 for lido in [resultados_do_processo(url, titulo, texto)] if lido]
    if not processos:
        return None

    # processos com resultado por sair, e o último com todos os resultados publicados
    def ultimo_resultado(processo):
        return max(fim for _, _, fim in processo["eventos"])
    por_sair = [processo for processo in processos if ultimo_resultado(processo) >= hoje]
    publicados = [processo for processo in processos if ultimo_resultado(processo) < hoje]
    em_vigor = por_sair + ([max(publicados, key=ultimo_resultado)] if publicados else [])

    linhas = []
    for processo in sorted(em_vigor, key=ultimo_resultado, reverse=True):
        for nome, inicio, fim in processo["eventos"]:
            linhas.append(f"Processo Seletivo {processo['processo']} (site {processo['site']}), {nome}: "
                          f"{_data_por_extenso(inicio, fim)}.")
    return "\n".join(linhas)


# dado derivado da base que o texto de uma fonte pode pedir, pelo nome do campo entre chaves: a função que o
# deriva e o texto de reserva, que entra só quando não há de onde derivar e a fonte ainda não está na base
DERIVADOS = {
    "datas_do_resultado": (datas_do_resultado,
                           "(o site de cada processo seletivo e a data de publicação do resultado estão no cronograma, "
                           "em https://ingresso.ifrs.edu.br)"),
}


def campos_derivados(texto):
    """Campos derivados que o texto pede; chaves com outro nome são texto comum."""
    return set(_CAMPO_DERIVADO.findall(texto or "")) & set(DERIVADOS)


def texto_da_fonte(fonte, documentos, hoje, com_reserva=False):
    """Preenche os campos derivados do texto de uma fonte.

    Args:
        fonte: entrada do arquivo de dados.
        documentos: {url: (título, texto)} dos documentos da base de que os derivados dependem.
        hoje: data da ingestão.
        com_reserva: True usa o texto de reserva do campo que não pôde ser derivado.

    Returns:
        O texto pronto; None quando um campo não pôde ser derivado e a reserva não foi pedida.
    """
    texto = fonte["texto"]
    for campo in campos_derivados(texto):
        derivar, reserva = DERIVADOS[campo]
        valor = derivar(documentos, hoje) or (reserva if com_reserva else None)
        if not valor:
            return None
        texto = texto.replace("{" + campo + "}", valor)
    return texto


def chunks_da_fonte(fonte, ano, texto=None):
    """Monta os chunks de uma fonte curada.

    Args:
        fonte: entrada do arquivo de dados.
        ano: ano gravado em published_at; entra no hash, então a fonte se renova a cada virada de ano e
            não envelhece no rerank por data.
        texto: texto com os campos derivados preenchidos; sem ele, o texto do arquivo.

    Returns:
        Um chunk por frase de busca, todos com o texto inteiro (sem frases, o próprio texto é buscado).
    """
    texto = texto or fonte["texto"]
    hash_fonte = source_hash(json.dumps({**fonte, "texto": texto, "ano": ano}, ensure_ascii=False, sort_keys=True))
    base = {"text": texto, "source_url": fonte["url"], "title": fonte["titulo"], "type": TIPO,
            "published_at": ano, "source_hash": hash_fonte}
    return [{**base, "texto_busca": frase} for frase in fonte.get("textos_busca") or [None]]


def documentos_para_derivar(estado, chunks_novos=()):
    """Reúne os documentos de que os campos derivados dependem: os cronogramas do portal de ingresso.

    Args:
        estado: estado da base por URL (carregar_estado).
        chunks_novos: chunks produzidos nesta ingestão, que valem no lugar do que está gravado.

    Returns:
        {url: (título, texto)}; todo chunk de um cronograma carrega o cronograma inteiro, então basta um.
    """
    documentos = {chunk["source_url"]: (chunk.get("title"), chunk["text"]) for chunk in chunks_novos
                  if _CRONOGRAMA_DO_PROCESSO.fullmatch(chunk["source_url"])}
    gravados = {url: entrada["ids"][0] for url, entrada in estado.items()
                if _CRONOGRAMA_DO_PROCESSO.fullmatch(url) and url not in documentos and entrada.get("ids")}
    if gravados:
        for url, vetor in zip(gravados, index.fetch(ids=list(gravados.values()), include_metadata=True)):
            if vetor and vetor.metadata:
                documentos[url] = (vetor.metadata.get("title"), vetor.metadata.get("text") or "")
    return documentos


def run_fontes_curadas(estado, chunks_novos=()):
    """Compara as fontes do arquivo com a base e prepara o que a ingestão grava.

    Args:
        estado: estado da base por URL (carregar_estado), com o tipo de cada URL.
        chunks_novos: chunks das páginas novas ou mudadas nesta ingestão, lidos pelos campos derivados.

    Returns:
        (chunks das fontes novas ou mudadas, URLs de fontes mudadas a substituir). Os chunks de uma fonte
        tirada do arquivo são apagados aqui.
    """
    print("\n" + "=" * 60)
    print("FASE 4.5 — FONTES CURADAS")
    print("=" * 60)
    hoje = date.today()
    fontes = carregar()
    documentos = (documentos_para_derivar(estado, chunks_novos)
                  if any(campos_derivados(fonte["texto"]) for fonte in fontes) else {})

    # fonte nova ou com conteúdo diferente do gravado entra; vencida segue valendo, com aviso
    chunks, mudadas = [], set()
    for fonte in fontes:
        if fonte["revisar_ate"] < hoje.isoformat():
            _avisar(f"revisão vencida desde {fonte['revisar_ate']}: {fonte['url']} (origem: {fonte['origem']})")
        # campo que não pôde ser derivado (os cronogramas não estão na base nem vieram nesta ingestão): a fonte
        # que já está gravada fica como está; a que não está entra com o texto de reserva, para não sumir
        texto = texto_da_fonte(fonte, documentos, hoje)
        gravada = estado.get(fonte["url"])
        if texto is None and gravada and not REINGEST:
            _avisar(f"campo derivado sem dado na base, fonte mantida como está gravada: {fonte['url']}")
            continue
        if texto is None:
            _avisar(f"campo derivado sem dado na base, fonte gravada com o texto de reserva: {fonte['url']}")
            texto = texto_da_fonte(fonte, documentos, hoje, com_reserva=True)
        novos = chunks_da_fonte(fonte, hoje.year, texto)
        if gravada and gravada.get("source_hash") == novos[0]["source_hash"] and not (REPARSE or REINGEST):
            continue
        chunks.extend(novos)
        if gravada:
            mudadas.add(fonte["url"])

    # fonte que saiu do arquivo sai da base
    urls = {fonte["url"] for fonte in fontes}
    orfas = [i for url, e in estado.items() if e.get("type") == TIPO and url not in urls for i in e["ids"]]
    if orfas and not REINGEST:
        index.delete(ids=orfas)
    print(f"Fontes curadas: {len(fontes)} no arquivo, {len(chunks)} chunks a gravar, {len(orfas)} de fontes removidas")
    return chunks, mudadas


if __name__ == "__main__":
    from pipelines.ingest import carregar_estado, run_ingest

    estado_atual = carregar_estado()
    novos_chunks, urls_mudadas = run_fontes_curadas(estado_atual)
    if novos_chunks:
        run_ingest(novos_chunks, estado_atual, urls_mudadas)
