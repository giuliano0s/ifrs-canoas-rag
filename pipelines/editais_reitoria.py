"""Editais do portal da Reitoria (ifrs.edu.br/editais/): coleta dos que dizem respeito ao Campus Canoas.

O portal publica numa tabela única todos os editais do IFRS (data, categorias, última atualização), e cada
edital tem uma página com seus anexos (edital, retificações, resultados). O site do campus não liga a
maioria deles, então o crawler não os alcança.

A unidade de mudança é o EDITAL, não o arquivo: uma retificação nova muda o texto que vale no edital
original, e uma compra repetida tira a anterior. Quando a página ou algum anexo muda (hash do grupo), o
edital inteiro é reprocessado e todos os seus chunks são trocados. O registro versionado
(data/parsed/editais_reitoria.json) guarda por edital a decisão, o veredito do LLM, o hash do grupo e os
ids dos chunks, para o edital que fica fora não voltar todo dia nem trocar de veredito.

Fluxo de um edital:
1. decisão: Canoas no título entra; compra anulada ou que não cita Canoas em anexo legível fica fora; o
   resto o LLM classifica em de Canoas, da rede ou só de outra unidade, citando a frase que sustenta o
   veredito, conferida no texto lido;
2. recorte por anexo: lista de pessoas vira aviso (pipelines/listas_nomes.py); o LLM marca as linhas de
   outro campus e as de nomes (compra: as linhas a manter), e o texto final é sempre trecho do original;
   linha que cita Canoas nunca sai;
3. retificações "onde se lê / leia-se" aplicadas ao edital e às versões completas anteriores, trocando só
   o trecho casado; versão retificada completa substitui o documento de nome parecido; todo trecho de
   edital retificado leva o aviso de onde está o texto que vale.

USO: roda no fim da ingestão (pipelines/ingest_pipeline.py); sozinho, `python -m pipelines.editais_reitoria`
(variáveis UPSTASH_WRITE_API_KEY, GEMINI_API_KEY_T1, OPENAI_API_KEY; EDITAIS_DESDE=dd/mm/aaaa troca o início da
janela, que por padrão são os últimos 12 meses).
"""

import difflib
import hashlib
import html
import io
import json
import os
import re
import time
import unicodedata
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import fitz
from bs4 import BeautifulSoup

from pipelines.chunker import chunk_document
from pipelines.config import (CAMPI_IFRS, EDITAIS_DESDE, EDITAIS_REITORIA_PATH, EDITAIS_URL, INFO_DIR,
                              OUTRAS_UNIDADES, REINGEST, REPARSE, fetch, index)
from pipelines.ingest import ingest_chunks
from pipelines.listas_nomes import MOTIVOS_POR_NOME, aviso_lista, destino
from rag import llm

COMPRAS = {"Licitação", "Pregão Eletrônico", "Dispensa de Licitação", "Inexigibilidade", "Concorrência", "RDC"}
# .doc e .xls entram na lista de anexos mostrada ao LLM, mas sem texto legivel
EXTENSOES = (".pdf", ".docx", ".odt", ".xlsx", ".ods", ".doc", ".xls")
MIN_CHARS_ANEXO = 200
LIMITE_PRINCIPAL = 25000
LINHAS_POR_JANELA, CARACTERES_POR_JANELA = 400, 24000
# a janela cujo JSON volta invalido (o modelo as vezes entra em laco de repeticao) e partida ao meio ate este minimo
LINHAS_MINIMAS_POR_JANELA = 25
# teto de tokens da resposta: a marcacao de 400 linhas cabe em menos de 1.000, e um laco estoura em segundos
MAX_SAIDA_MARCACAO, MAX_SAIDA_CLASSIFICACAO = 4000, 2000
AVISO_NOMES = "[nomes de pessoas omitidos por proteção de dados; a lista completa está no documento original]"
AVISO_RETIFICACAO = "[Este edital tem retificação publicada; onde houver diferença, vale o texto da retificação: {urls}]"
TENTATIVAS_ANTES_DE_ACEITAR_FALHA = 3
# tempo da fase na ingestao diaria: a primeira carga (centenas de editais) se espalha por varios dias sem
# estourar o teto do workflow
PRAZO_MIN = 40
SIMULACAO_PATH = EDITAIS_REITORIA_PATH.with_name("editais_reitoria_simulacao.jsonl")

_LINHA_TABELA = re.compile(r"<tr>\s*<td>([\d/: ]+)</td>\s*<td><a href=\"([^\"]+)\">(.*?)</a></td>\s*<td>([\d/]*)</td>"
                           r"\s*<td>(.*?)</td>\s*<td>(.*?)</td>", re.S)
_CANOAS = re.compile(r"canoas", re.I)
_ANULADA = re.compile(r"anulad|revogad|cancelad|fracassad|desert", re.I)
_MODALIDADE_DE_COMPRA = re.compile(r"\s*(preg[aã]o|dispensa|inexigibilidade|concorr[eê]ncia|rdc\b|licita[çc][aã]o)",
                                   re.I)
_RETIFICACAO = re.compile(r"retific", re.I)
_ONDE_SE_LE = re.compile(r"onde\s+se\s+l[êe]\s*:?", re.I)
_LEIA_SE = re.compile(r"leia[\s\-:]*se\s*:?", re.I)
_FIM_DA_RETIFICACAO = re.compile(r"\s(?:Bento Gonçalves|Porto Alegre|Canoas)\s*(?:\(RS\)|/RS|-RS)?,\s*\d{1,2}\s+de\s"
                                 r"|\(Assinado digitalmente|Documento assinado", re.I)
_INVISIVEIS = "​‌‍﻿"
_PALAVRAS_VAZIAS_DO_NOME = {"retificacao", "retificado", "retificada", "retificao", "versao", "final", "em", "de", "do",
                            "da", "no", "n", "ass", "assinado", "pdf", "docx", "doc", "edital", "completo", "completof"}


def _sem_acento(texto):
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()


# unidade do IFRS que nao e Canoas, citada pelo nome; "Rio Grande do Sul", o estado, nao conta
_OUTRAS = re.compile("|".join(re.escape(_sem_acento(nome)) for nome in
                              [campus for campus in CAMPI_IFRS if campus != "Canoas"] + list(OUTRAS_UNIDADES))
                     .replace("rio\\ grande", "rio\\ grande(?!\\ do\\ sul)"))


def cita_outra_unidade(texto):
    """True quando o texto nomeia um campus que nao e Canoas, a Reitoria ou uma unidade em implantacao."""
    return bool(_OUTRAS.search(_sem_acento(texto or "")))


def _prompt(nome):
    return (INFO_DIR / nome).read_text(encoding="utf-8")


# ── tabela do portal e download do grupo ──────────────────────────────────────
def listar_editais(desde=EDITAIS_DESDE):
    """Editais da tabela do portal com data do edital a partir de `desde`.

    Returns:
        Lista de dicts com url, titulo, data (dd/mm/aaaa), atualizacao, categorias e status.
    """
    editais = []
    for atualizacao, url, titulo, data, categorias, status in _LINHA_TABELA.findall(fetch(EDITAIS_URL, timeout=60).text):
        if data and datetime.strptime(data, "%d/%m/%Y") >= desde:
            editais.append({"url": url, "titulo": html.unescape(re.sub("<.*?>", "", titulo)).strip(), "data": data,
                            "atualizacao": atualizacao.strip(), "categorias": re.findall(r'rel="tag">(.*?)</a>', categorias),
                            "status": re.sub("<.*?>", "", status).strip()})
    return editais


def texto_do_arquivo(url, conteudo):
    """Texto de um PDF ou de um formato zip+xml (docx, odt, xlsx, ods), com uma linha por paragrafo."""
    if url.lower().endswith(".pdf"):
        with fitz.open(stream=conteudo, filetype="pdf") as doc:
            return "\n".join(pagina.get_text() for pagina in doc)
    try:
        with zipfile.ZipFile(io.BytesIO(conteudo)) as pacote:
            partes = [pacote.read(nome).decode("utf-8", "ignore") for nome in pacote.namelist() if nome.endswith(".xml")]
    except zipfile.BadZipFile:
        return ""
    xml = re.sub(r"</(w:p|text:p|text:h|row|table:table-row|si)>", "\n", " ".join(partes))
    return html.unescape(re.sub(r"<[^>]+>", " ", xml))


def baixar_grupo(edital):
    """Baixa a pagina do edital e os anexos ligados nela; devolve o grupo com o hash, ou None se a pagina falhar.

    Returns:
        {"pagina": texto, "hash": hash do grupo, "anexos": [{"url", "nome", "hash", "texto"} ou com "falha"]}.
    """
    try:
        resposta = fetch(edital["url"], timeout=60)
        resposta.raise_for_status()
    except Exception as erro:
        print(f"  [EDITAIS] pagina indisponivel {edital['url']}: {erro}")
        return None
    principal = BeautifulSoup(resposta.text, "html.parser")
    principal = principal.find("main") or principal
    pagina = re.sub(r"\n+", "\n", principal.get_text("\n", strip=True))
    links = []
    for ancora in principal.find_all("a", href=True):
        href = ancora["href"].split("#")[0]
        if href.lower().endswith(EXTENSOES) and href not in links:
            links.append(href)

    anexos = []
    for link in links:
        anexo = {"url": link, "nome": link.rsplit("/", 1)[-1]}
        try:
            conteudo = fetch(link, timeout=60)
            conteudo.raise_for_status()
            anexo["hash"] = hashlib.sha256(conteudo.content).hexdigest()
            anexo["texto"] = texto_do_arquivo(link, conteudo.content)
        except Exception as erro:
            anexo["falha"] = f"{type(erro).__name__}: {str(erro)[:120]}"
        anexos.append(anexo)
    partes = [hashlib.sha256(pagina.encode()).hexdigest()] + sorted(f"{a['url']} {a.get('hash')}" for a in anexos)
    return {"pagina": pagina, "hash": hashlib.sha256("\n".join(partes).encode()).hexdigest(), "anexos": anexos}


# ── decisao por edital ────────────────────────────────────────────────────────
def _legiveis(grupo):
    return [anexo for anexo in grupo["anexos"] if len((anexo.get("texto") or "").strip()) >= MIN_CHARS_ANEXO]


def _cita_canoas(grupo):
    return bool(_CANOAS.search(grupo["pagina"]) or any(_CANOAS.search(anexo.get("texto") or "") for anexo in grupo["anexos"]))


def eh_compra(edital):
    # pela categoria do portal ou, no edital publicado ainda sem categoria, pela modalidade no inicio do titulo
    return bool(set(edital["categorias"]) & COMPRAS) or bool(_MODALIDADE_DE_COMPRA.match(edital["titulo"]))


def objeto_da_compra(titulo):
    """Objeto da compra sem o numero, para achar a mesma compra repetida."""
    return re.sub(r"\s+", " ", re.sub(r"^.*?\d+/\d{4}\s*[–-]\s*", "", titulo)).strip().lower()


def _normalizar(texto):
    return re.sub(r"\s+", " ", texto or "").strip().lower()


def cobertura(evidencia, bruto):
    """Fracao da frase citada coberta por blocos de 12+ caracteres presentes no texto lido, em qualquer ordem.

    Frase curta (menos de 12 caracteres) conta inteira ou nada.
    """
    frase, texto = _normalizar(evidencia), _normalizar(bruto)
    if len(frase) < 12:
        return 1.0 if frase and frase in texto else 0.0
    cobertos, inicio = 0, 0
    while inicio < len(frase):
        fim = inicio + 12
        if fim <= len(frase) and frase[inicio:fim] in texto:
            while fim < len(frase) and frase[inicio:fim + 1] in texto:
                fim += 1
            cobertos += fim - inicio
            inicio = fim
        else:
            inicio += 1
    return round(cobertos / len(frase), 3)


def _chamar(prompt, max_saida):
    # perfil de ingestao, esperando no limite por minuto da conta; queda e 5xx ja foram refeitos em llm.gerar
    for tentativa in range(6):
        try:
            return llm.gerar([{"papel": "usuario", "texto": prompt}], temperatura=0, prazo=llm.PRAZO_INGESTAO,
                             perfil="ingestao", max_saida=max_saida)
        except llm.ProvedorIndisponivel as erro:
            if erro.motivo != "fluxo_alto" or tentativa == 5:
                raise
            time.sleep(max(erro.tentar_em or 20, 5))


def classificar(edital, grupo):
    """Pede ao LLM se o edital e de Canoas, da rede ou so de outra unidade, com a frase que sustenta.

    Returns:
        Veredito {"categoria", "publico", "evidencia", "motivo", "cobertura"}.
    """
    legiveis = _legiveis(grupo)
    doc = next((anexo for anexo in legiveis if "edital" in anexo["nome"].lower()), legiveis[0] if legiveis else None)
    principal = re.sub(r"[ \t]+", " ", doc["texto"])[:LIMITE_PRINCIPAL] if doc else ""
    janelas = []
    for anexo in grupo["anexos"]:
        if anexo is doc:
            continue
        for achado in list(_CANOAS.finditer(anexo.get("texto") or ""))[:2]:
            texto = anexo["texto"]
            janelas.append(f"[{anexo['nome'][:60]}] " + re.sub(r"\s+", " ", texto[max(0, achado.start() - 250):achado.end() + 250]))
        if len(janelas) >= 8:
            break
    pagina = grupo["pagina"][:3000]
    prompt = _prompt("editais_classificacao.txt").format(
        campi=", ".join(CAMPI_IFRS), titulo=edital["titulo"], data=edital["data"], categorias=", ".join(edital["categorias"]),
        anexos=", ".join(anexo["nome"][:70] for anexo in grupo["anexos"]), pagina=pagina,
        nome_principal=doc["nome"] if doc else "nenhum", principal=principal or "(sem texto legível)",
        trechos="\n".join(janelas[:8]) or "(nenhum)")
    texto = (_chamar(prompt, MAX_SAIDA_CLASSIFICACAO).texto or "").strip().removeprefix("```json").removesuffix("```").strip()
    try:
        veredito = json.loads(texto)
    except json.JSONDecodeError:
        veredito = {"categoria": "erro_json"}
    veredito["cobertura"] = cobertura(veredito.get("evidencia") or "", pagina + "\n" + principal + "\n" + "\n".join(janelas))
    return veredito


def decidir(edital, grupo, veredito):
    """Devolve (entra, motivo) pelas regras fixas e, quando preciso, pelo veredito conferido."""
    if _CANOAS.search(edital["titulo"]):
        return True, "titulo"
    if eh_compra(edital) and _ANULADA.search(edital["titulo"]):
        return False, "compra_anulada"
    if eh_compra(edital) and not _cita_canoas(grupo) and _legiveis(grupo):
        return False, "compra_sem_canoas"
    if veredito is None:
        return True, "sem_veredito"
    categoria, evidencia = veredito.get("categoria"), veredito.get("evidencia") or ""
    if veredito.get("cobertura", 0) < 0.9:
        return True, "evidencia_nao_confere"
    if categoria == "canoas":
        return (True, "canoas") if _CANOAS.search(evidencia) or not cita_outra_unidade(evidencia) else (False, "canoas_contradito")
    if categoria == "outro":
        return (False, "outro") if cita_outra_unidade(evidencia) else (True, "outro_sem_unidade")
    return True, categoria or "sem_categoria"


def precisa_de_veredito(edital, grupo):
    """O LLM so e chamado para o que as regras fixas nao decidem."""
    if _CANOAS.search(edital["titulo"]):
        return False
    if eh_compra(edital) and (_ANULADA.search(edital["titulo"]) or (not _cita_canoas(grupo) and _legiveis(grupo))):
        return False
    return True


# ── recorte por anexo ─────────────────────────────────────────────────────────
def _linhas(texto):
    # linhas sem espaco extra; linha gigante vira pedacos de ate 1500 caracteres
    linhas = []
    for linha in texto.split("\n"):
        linha = re.sub(r"\s+", " ", linha).strip()
        while len(linha) > 1500:
            corte = linha.rfind(" ", 0, 1500)
            corte = corte if corte > 0 else 1500
            linhas.append(linha[:corte])
            linha = linha[corte:].strip()
        if linha:
            linhas.append(linha)
    return linhas


def _janelas(linhas):
    inicio, tamanho = 0, 0
    for indice, linha in enumerate(linhas):
        tamanho += len(linha) + 1
        if indice - inicio + 1 >= LINHAS_POR_JANELA or tamanho >= CARACTERES_POR_JANELA:
            yield inicio, linhas[inicio:indice + 1]
            inicio, tamanho = indice + 1, 0
    if inicio < len(linhas):
        yield inicio, linhas[inicio:]


def _intervalos(valor, primeira, ultima):
    numeros = set()
    for par in valor or []:
        try:
            inicio, fim = int(par[0]), int(par[-1])
        except (TypeError, ValueError, IndexError):
            continue
        numeros.update(range(max(primeira, inicio), min(ultima, fim) + 1))
    return numeros


def _marcar(prompt_base, nome, titulo, pedaco, deslocamento):
    # uma janela de linhas numeradas. JSON invalido parte a janela ao meio (o laco de repeticao do modelo some em
    # janela menor); a janela minima tem tres tentativas, e sem JSON o anexo inteiro sai
    numeradas = "\n".join(f"{deslocamento + indice + 1}: {linha}" for indice, linha in enumerate(pedaco))
    dividir = len(pedaco) >= 2 * LINHAS_MINIMAS_POR_JANELA
    for _ in range(1 if dividir else 3):
        texto = (_chamar(prompt_base.format(documento=nome, edital=titulo, linhas=numeradas), MAX_SAIDA_MARCACAO).texto or "")
        texto = texto.strip().removeprefix("```json").removesuffix("```").strip()
        try:
            marcas = json.loads(texto)
        except json.JSONDecodeError:
            continue
        primeira, ultima = deslocamento + 1, deslocamento + len(pedaco)
        return {chave: _intervalos(marcas.get(chave), primeira, ultima) for chave in ("outro_campus", "nomes", "manter")}
    if dividir:
        meio = len(pedaco) // 2
        antes = _marcar(prompt_base, nome, titulo, pedaco[:meio], deslocamento)
        depois = _marcar(prompt_base, nome, titulo, pedaco[meio:], deslocamento + meio)
        return {chave: antes[chave] | depois[chave] for chave in antes}
    raise ValueError(f"JSON invalido em 3 tentativas na janela que comeca na linha {deslocamento + 1}")


def _aviso(edital, anexo):
    legivel = re.sub(r"[-_]+", " ", anexo["nome"].rsplit(".", 1)[0]).strip()
    return aviso_lista(anexo["url"], f'{legivel}, do edital "{edital["titulo"]}"')


def recortar_anexo(edital, anexo):
    """Recorte de um anexo para a base, sempre trecho do original.

    Returns:
        {"url", "documento", "tipo", "texto", "lista": motivo ou None}.
    """
    texto, nome = anexo["texto"], anexo["nome"]
    compra = eh_compra(edital)
    registro = {"url": anexo["url"], "documento": nome, "tipo": "compra" if compra else "outros", "lista": None}

    # lista por numero de inscricao: vira o aviso sem chamar o LLM; a que depende so do formato de nome espera
    # a leitura do LLM. compra lista empresas e itens, e os codigos de material contariam como inscricao
    decisao, motivo = (None, None) if compra else destino(texto, anexo["url"], nome)
    if decisao and motivo not in MOTIVOS_POR_NOME:
        return {**registro, "lista": motivo, "texto": _aviso(edital, anexo)}

    linhas = _linhas(texto)
    prompt = _prompt("editais_compra.txt" if compra else "editais_marcar.txt")
    marcadas = {"outro_campus": set(), "nomes": set(), "manter": set()}
    for deslocamento, pedaco in _janelas(linhas):
        for chave, numeros in _marcar(prompt, nome, edital["titulo"], pedaco, deslocamento).items():
            marcadas[chave] |= numeros
    com_canoas = {numero for numero, linha in enumerate(linhas, 1) if _CANOAS.search(linha)}

    if compra:
        # fica o que o LLM manda manter e toda linha com Canoas; sem item proprio de Canoas (Canoas so numa
        # enumeracao de unidades), fica so a capa
        especificas = {numero for numero in com_canoas if len(_OUTRAS.findall(_sem_acento(linhas[numero - 1]))) < 2}
        manter = (marcadas["manter"] | com_canoas) if especificas else {n for n in marcadas["manter"] if n <= 150}
        return {**registro, "texto": "\n".join(linha for numero, linha in enumerate(linhas, 1) if numero in manter)}

    # demais: lista pelos nomes marcados; senao sai o que e de outro campus (nunca linha com Canoas) e cada
    # bloco de nomes vira um aviso
    decisao, motivo = destino(texto, anexo["url"], nome, linhas=len(linhas), nomes_marcados=len(marcadas["nomes"]))
    if decisao:
        return {**registro, "lista": motivo, "texto": _aviso(edital, anexo)}
    outro, nomes = marcadas["outro_campus"] - com_canoas, marcadas["nomes"]
    saida, no_bloco = [], False
    for numero, linha in enumerate(linhas, 1):
        if numero in nomes:
            if not no_bloco:
                saida.append(AVISO_NOMES)
            no_bloco = True
            continue
        no_bloco = False
        if numero not in outro:
            saida.append(linha)
    return {**registro, "texto": "\n".join(saida)}


# ── retificacoes ──────────────────────────────────────────────────────────────
def _normalizado_com_mapa(texto):
    # texto com espacos colapsados e sem caracteres invisiveis, e a posicao no original de cada caractere
    chars, mapa, espaco = [], [], False
    for posicao, caractere in enumerate(texto):
        if caractere in _INVISIVEIS:
            continue
        if caractere.isspace():
            if not espaco and chars:
                chars.append(" ")
                mapa.append(posicao)
            espaco = True
            continue
        chars.append(caractere)
        mapa.append(posicao)
        espaco = False
    return "".join(chars), mapa


def _localizar(antigo, texto, limiar=0.9):
    """Intervalo [inicio, fim) do texto (ja normalizado) que corresponde ao antigo; exato ou palavra a palavra."""
    posicao = texto.find(antigo)
    if posicao >= 0:
        return posicao, posicao + len(antigo)
    palavras = [(achado.start(), achado.end()) for achado in re.finditer(r"\S+", texto)]
    alvo = antigo.lower().split(" ")
    primeiras = set(alvo[:3])
    melhor, intervalo = 0.0, None
    for inicio in range(len(palavras)):
        if texto[palavras[inicio][0]:palavras[inicio][1]].lower() not in primeiras:
            continue
        for largura in (len(alvo) - 2, len(alvo), len(alvo) + 2):
            fim = min(len(palavras), inicio + max(largura, 1))
            janela = [texto[a:b].lower() for a, b in palavras[inicio:fim]]
            razao = difflib.SequenceMatcher(None, janela, alvo, autojunk=False).ratio()
            if razao > melhor:
                melhor, intervalo = razao, (palavras[inicio][0], palavras[fim - 1][1])
    return intervalo if melhor >= limiar else None


def _trocar(texto, antigo, novo):
    """Troca no texto original o trecho que corresponde ao antigo, mantendo as quebras de linha do resto."""
    normalizado, mapa = _normalizado_com_mapa(texto)
    intervalo = _localizar(antigo, normalizado)
    if not intervalo:
        return None
    inicio, fim = mapa[intervalo[0]], mapa[intervalo[1] - 1] + 1
    return texto[:inicio] + novo + texto[fim:]


def pares_onde_leia(texto):
    """Pares (texto antigo, texto novo) de uma retificacao no formato 'onde se le ... leia-se ...'."""
    pares = []
    for bloco in _ONDE_SE_LE.split(_normalizado_com_mapa(texto)[0])[1:]:
        partes = _LEIA_SE.split(bloco, maxsplit=1)
        if len(partes) != 2:
            continue
        antigo, novo = partes
        novo = _FIM_DA_RETIFICACAO.split(novo)[0]
        antigo, novo = re.sub(r"\s*\d+\.\s*$", "", antigo).strip(), re.sub(r"\s*\d+\.\s*$", "", novo).strip()
        if len(antigo) >= 15 and novo:
            pares.append((antigo, novo))
    return pares


def _tokens_do_nome(nome):
    partes = re.split(r"[^a-z0-9à-ú]+", nome.lower().rsplit(".", 1)[0])
    return {parte for parte in partes if parte and parte not in _PALAVRAS_VAZIAS_DO_NOME and not parte.isdigit()}


def consolidar_retificacoes(registros):
    """Aplica as retificacoes de um edital aos seus documentos, na ordem em que aparecem na pagina.

    'Onde se le / leia-se': o trecho antigo vira o novo em todo documento que o contem (originais e versoes
    completas anteriores); a retificacao ganha rotulos que dizem qual texto vale. Versao retificada
    completa: o documento anterior de nome mais parecido sai. Todo documento que nao e a propria lista de
    trocas leva o aviso de retificacao.

    Returns:
        Resumo {"pares", "aplicados", "substituidos"}.
    """
    documentos = [registro for registro in registros if registro.get("texto") and not registro.get("lista")]
    retificacoes = [doc for doc in documentos if _RETIFICACAO.search(doc["documento"])]
    resumo = {"pares": 0, "aplicados": 0, "substituidos": 0}
    if not retificacoes:
        return resumo
    de_trocas = [doc for doc in retificacoes if pares_onde_leia(doc["texto"])]
    alvos = [doc for doc in documentos if doc not in de_trocas]
    for retificacao in retificacoes:
        pares = pares_onde_leia(retificacao["texto"])
        if pares:
            for antigo, novo in pares:
                achou = False
                for alvo in alvos:
                    trocado = _trocar(alvo["texto"], antigo, novo) if alvo.get("texto") else None
                    if trocado is not None:
                        alvo["texto"], achou = trocado, True
                resumo["aplicados"] += achou
            resumo["pares"] += len(pares)
            retificacao["texto"] = _LEIA_SE.sub("TEXTO QUE VALE (retificado): ",
                                                _ONDE_SE_LE.sub("TEXTO ANTIGO (substituído, não vale mais): ", retificacao["texto"]))
            continue
        # versao completa: substitui o documento anterior de nome mais parecido
        meus = _tokens_do_nome(retificacao["documento"])
        anteriores = [doc for doc in documentos[:documentos.index(retificacao)] if doc.get("texto")]
        semelhanca = [(len(meus & _tokens_do_nome(doc["documento"])) / max(len(meus | _tokens_do_nome(doc["documento"])), 1), doc)
                      for doc in anteriores]
        nota, parecido = max(semelhanca, key=lambda par: par[0], default=(0, None))
        if nota >= 0.5:
            parecido["texto"], parecido["substituido_por"] = "", retificacao["url"]
            resumo["substituidos"] += 1
    urls = ", ".join(doc["url"] for doc in retificacoes)
    for alvo in alvos:
        if alvo.get("texto"):
            alvo["aviso_retificacao"] = AVISO_RETIFICACAO.format(urls=urls)
    return resumo


# ── edital inteiro ────────────────────────────────────────────────────────────
def chunks_do_edital(edital, pagina, registros):
    """Chunks do edital (pagina e anexos recortados), com o edital_url que agrupa e o aviso de retificacao."""
    ano = int(edital["data"][-4:])
    chunks = []
    decisao_pagina, _ = destino(pagina, edital["url"], edital["titulo"], pagina=True)
    documentos = [{"url": edital["url"], "documento": "pagina", "tipo": "pagina", "lista": decisao_pagina,
                   "texto": aviso_lista(edital["url"], edital["titulo"]) if decisao_pagina else pagina,
                   "hash": hashlib.sha256(pagina.encode()).hexdigest()}] + registros
    for documento in documentos:
        if not documento.get("texto"):
            continue
        titulo = edital["titulo"] if documento["documento"] == "pagina" else f"{edital['titulo']} - {documento['documento']}"
        metadata = {"source_url": documento["url"], "title": titulo, "type": "html" if documento["documento"] == "pagina" else "pdf",
                    "published_at": ano, "source_hash": documento.get("hash"), "edital_url": edital["url"]}
        if documento.get("lista"):
            partes = [{**metadata, "lista_nomes": True, "text": documento["texto"]}]
        else:
            partes = chunk_document(documento["texto"], metadata)
        for indice, chunk in enumerate(partes):
            chunk["id"] = f"{documento['url']}#{indice}"
            if documento.get("aviso_retificacao"):
                chunk["text"] = documento["aviso_retificacao"] + "\n" + chunk["text"]
            chunks.append(chunk)
    return chunks


def processar_edital(edital, grupo, veredito, urls_ocupadas=frozenset(), hashes_ocupados=frozenset()):
    """Decide e, se o edital entra, recorta e monta os chunks. Nao escreve na base.

    Args:
        edital: linha da tabela do portal.
        grupo: pagina e anexos baixados (baixar_grupo).
        veredito: veredito do LLM ja guardado, ou None (classificado aqui quando preciso).
        urls_ocupadas: enderecos que ja tem chunks na base por outro caminho (o crawler do campus ou outro
            edital); o anexo com um deles fica fora, para dois donos nao trocarem os chunks um do outro.
        hashes_ocupados: conteudos que ja estao na base sob o endereco de outro caminho.

    Returns:
        {"entra", "motivo", "veredito", "chunks", "falhas", "resumo"}.
    """
    if (veredito is None or veredito.get("categoria") == "erro_json") and precisa_de_veredito(edital, grupo):
        veredito = classificar(edital, grupo)
    entra, motivo = decidir(edital, grupo, veredito)
    resultado = {"entra": entra, "motivo": motivo, "veredito": veredito, "chunks": [], "falhas": [], "resumo": {}}
    if not entra:
        return resultado

    # anexo que ja esta na base por outro caminho nao entra de novo
    anexos = [anexo for anexo in _legiveis(grupo) if anexo["url"] not in urls_ocupadas and anexo["hash"] not in hashes_ocupados]

    def seguro(anexo):
        try:
            return {**recortar_anexo(edital, anexo), "hash": anexo["hash"]}
        except llm.ProvedorIndisponivel:
            raise
        except Exception as erro:
            return {"url": anexo["url"], "documento": anexo["nome"], "falha": f"{type(erro).__name__}: {str(erro)[:150]}"}

    with ThreadPoolExecutor(6) as pool:
        registros = list(pool.map(seguro, anexos))
    resultado["falhas"] = [registro for registro in registros if registro.get("falha")] + \
                          [anexo for anexo in grupo["anexos"] if anexo.get("falha")]
    registros = [registro for registro in registros if not registro.get("falha")]
    resultado["resumo"] = consolidar_retificacoes(registros)
    resultado["chunks"] = chunks_do_edital(edital, grupo["pagina"], registros)
    return resultado


# ── registro e execucao ───────────────────────────────────────────────────────
def carregar_registro():
    if EDITAIS_REITORIA_PATH.exists():
        return json.loads(EDITAIS_REITORIA_PATH.read_text(encoding="utf-8"))
    return {}


def salvar_registro(registro):
    EDITAIS_REITORIA_PATH.parent.mkdir(parents=True, exist_ok=True)
    EDITAIS_REITORIA_PATH.write_text(json.dumps(dict(sorted(registro.items())), ensure_ascii=False, indent=1), encoding="utf-8")


def _apagar(ids):
    for inicio in range(0, len(ids), 1000):
        index.delete(ids=ids[inicio:inicio + 1000])


def _compras_repetidas(registro):
    # entre compras que entram com o mesmo objeto, fica so a mais recente; as anteriores saem da base
    por_objeto = {}
    for url, item in registro.items():
        if item.get("objeto") and item.get("entra"):
            por_objeto.setdefault(item["objeto"], []).append(url)
    saem = []
    for urls in por_objeto.values():
        urls.sort(key=lambda url: datetime.strptime(registro[url]["data"], "%d/%m/%Y"))
        for url in urls[:-1]:
            saem += registro[url].get("ids", [])
            registro[url].update(entra=False, motivo="compra_repetida", ids=[])
    _apagar(saem)
    return len(saem)


def sair_da_janela(registro, desde):
    """Tira do registro os editais com data anterior a `desde` e devolve os ids dos chunks deles, a apagar."""
    antigos = [url for url, item in registro.items() if datetime.strptime(item["data"], "%d/%m/%Y") < desde]
    ids = [identificador for url in antigos for identificador in registro.pop(url).get("ids", [])]
    return antigos, ids


def _donos(registro):
    # endereco -> edital cujos chunks o gravaram
    return {identificador.rsplit("#", 1)[0]: edital_url for edital_url, item in registro.items()
            for identificador in item.get("ids", [])}


def run_editais_reitoria(estado, prazo_min=PRAZO_MIN, simular=False, somente=None):
    """Atualiza na base os editais da Reitoria que mudaram desde o ultimo registro.

    Args:
        estado: estado da base por URL (pipelines.ingest.carregar_estado), somado ao que o crawler gravou nesta
            execucao. Endereco que nao veio deste modulo (sem edital_url) nao e gravado de novo por ele.
        prazo_min: tempo maximo da fase, em minutos; o que nao couber fica para a proxima execucao.
        simular: True nao escreve na base nem no registro; os chunks que entrariam vao para SIMULACAO_PATH.
        somente: enderecos de edital a processar (amostra), mesmo sem mudanca.

    Returns:
        Contagem por desfecho.
    """
    print("\n" + "=" * 60)
    print("FASE 5b - EDITAIS DA REITORIA")
    print("=" * 60)
    inicio = time.monotonic()
    registro = carregar_registro()
    editais = listar_editais()

    # o edital que saiu da janela de 12 meses sai da base
    if not simular:
        antigos, ids_antigos = sair_da_janela(registro, EDITAIS_DESDE)
        _apagar(ids_antigos)
        if antigos:
            salvar_registro(registro)
            print(f"Fora da janela desde {EDITAIS_DESDE:%d/%m/%Y}: {len(antigos)} editais, {len(ids_antigos)} chunks apagados")

    # reprocessamento geral (REPARSE, REINGEST): todo edital volta a pendente, e o que nao couber no prazo de
    # hoje segue pendente amanha; o veredito ja guardado e reaproveitado
    if (REPARSE or REINGEST) and not simular:
        for item in registro.values():
            item["atualizacao"], item["hash"] = None, None
        salvar_registro(registro)

    # dono de cada endereco gravado por este modulo: o edital_url da base, completado pelo registro
    donos = {url: info["edital_url"] for url, info in estado.items() if info.get("edital_url")}
    donos.update(_donos(registro))
    ids_na_base = {}
    for url, info in estado.items():
        if info.get("edital_url"):
            ids_na_base.setdefault(info["edital_url"], set()).update(info.get("ids", []))
    do_crawler = {url for url in estado if url not in donos}
    hashes_do_crawler = {estado[url]["source_hash"] for url in do_crawler if estado[url].get("source_hash")}
    if somente:
        pendentes = [edital for edital in editais if edital["url"] in somente]
    else:
        pendentes = [edital for edital in editais
                     if registro.get(edital["url"], {}).get("atualizacao") != edital["atualizacao"]]
    print(f"Editais desde {EDITAIS_DESDE:%d/%m/%Y}: {len(editais)} | a processar: {len(pendentes)}")

    contagem = {"inalterados": 0, "entram": 0, "fora": 0, "adiados": 0, "chunks": 0, "para_depois": 0}
    simulados = []
    for numero, edital in enumerate(pendentes, 1):
        if time.monotonic() - inicio > prazo_min * 60:
            contagem["para_depois"] = len(pendentes) - numero + 1
            print(f"  prazo de {prazo_min} min esgotado: {contagem['para_depois']} editais ficam para a proxima execucao")
            break
        anterior = registro.get(edital["url"], {})
        grupo = baixar_grupo(edital)
        if grupo is None:
            contagem["adiados"] += 1
            continue
        if anterior.get("hash") == grupo["hash"] and not somente:
            anterior["atualizacao"] = edital["atualizacao"]
            contagem["inalterados"] += 1
            continue

        # erro do provedor de LLM (credito, queda persistente) para a fase; erro de um edital so o adia
        ocupadas = do_crawler | {url for url, dono in donos.items() if dono != edital["url"]}
        try:
            resultado = processar_edital(edital, grupo, anterior.get("veredito"), ocupadas, hashes_do_crawler)
        except llm.ProvedorIndisponivel:
            print(f"  provedor de LLM indisponivel: a fase para no edital {numero} de {len(pendentes)}")
            break
        except Exception as erro:
            contagem["adiados"] += 1
            print(f"  [{numero}] adiado ({type(erro).__name__}: {str(erro)[:100]}): {edital['titulo'][:60]}")
            continue
        novos = [chunk["id"] for chunk in resultado["chunks"]]
        antigos = sorted((set(anterior.get("ids", [])) | ids_na_base.get(edital["url"], set())) - set(novos))
        print(f"  [{numero}/{len(pendentes)}] {'entra' if resultado['entra'] else 'fora '} {resultado['motivo']:<22} "
              f"{len(novos):>3} chunks, {len(resultado['falhas'])} falhas | {edital['titulo'][:60]}")
        if simular:
            simulados.append({**edital, "entra": resultado["entra"], "motivo": resultado["motivo"],
                              "veredito": resultado["veredito"], "falhas": resultado["falhas"],
                              "retificacoes": resultado["resumo"], "apagaria": antigos, "chunks": resultado["chunks"]})
            contagem["entram" if resultado["entra"] else "fora"] += 1
            contagem["chunks"] += len(novos)
            continue

        # falha de rede ou de leitura num anexo: tenta de novo no proximo run; depois de algumas, aceita o que leu
        tentativas = anterior.get("tentativas", 0) + 1 if resultado["falhas"] else 0
        if resultado["falhas"] and tentativas < TENTATIVAS_ANTES_DE_ACEITAR_FALHA:
            registro[edital["url"]] = {**anterior, **{chave: edital[chave] for chave in ("titulo", "data", "categorias")},
                                       "veredito": resultado["veredito"], "tentativas": tentativas}
            contagem["adiados"] += 1
            salvar_registro(registro)
            continue

        # troca os chunks do edital: insere os novos e so depois apaga os antigos que sobraram
        if resultado["chunks"]:
            ingest_chunks(resultado["chunks"])
        _apagar(antigos)
        donos = {url: dono for url, dono in donos.items() if dono != edital["url"]}
        donos.update({identificador.rsplit("#", 1)[0]: edital["url"] for identificador in novos})
        registro[edital["url"]] = {
            "titulo": edital["titulo"], "data": edital["data"], "categorias": edital["categorias"],
            "atualizacao": edital["atualizacao"], "hash": grupo["hash"], "veredito": resultado["veredito"],
            "entra": resultado["entra"], "motivo": resultado["motivo"], "ids": novos,
            "objeto": objeto_da_compra(edital["titulo"]) if eh_compra(edital) else None,
            "falhas": len(resultado["falhas"]), "tentativas": 0}
        contagem["entram" if resultado["entra"] else "fora"] += 1
        contagem["chunks"] += len(novos)
        # salva a cada edital: uma queda no meio nao perde o que ja foi gravado na base
        salvar_registro(registro)

    if simular:
        with open(SIMULACAO_PATH, "w", encoding="utf-8") as arquivo:
            for item in simulados:
                arquivo.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"Simulacao em {SIMULACAO_PATH}: {contagem}")
        return contagem
    contagem["repetidas"] = _compras_repetidas(registro)
    salvar_registro(registro)
    print(f"Editais da Reitoria: {contagem}")
    return contagem


if __name__ == "__main__":
    from pipelines.ingest import carregar_estado
    run_editais_reitoria(carregar_estado(), prazo_min=float(os.getenv("EDITAIS_PRAZO_MIN", "100000")),
                         simular=os.getenv("EDITAIS_SIMULAR", "").lower() == "true",
                         somente=[url for url in os.getenv("EDITAIS_SOMENTE", "").split(",") if url])
