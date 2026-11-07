"""Curso de cada trecho da base (fase depois do ingest e dos editais da Reitoria).

Metadata gravado por trecho, só quando há etiqueta (a ausência é o genérico):
- curso_escopo: slug do único curso do trecho. O rerank do atendimento rebaixa trecho de curso diferente do
  perguntado, e o contexto rotula "Curso: <nome>".
- cursos: lista de slugs quando o trecho é de vários cursos sem seção, ou quando o curso vem só do link de uma
  página de curso num documento que não é definidor (PPC, matriz, regulamento). Rótulo "Cursos: ...", sem
  penalidade.
- curso_fonte: de onde veio a etiqueta (pagina_de_curso, grade, vagas, titulo, modelo, modelo_trecho).

Decisão por documento, em cascata:
1. regra objetiva, sem LLM: a própria página de curso; a grade de horários pela sigla da turma e o quadro de
   vagas pela linha, trecho a trecho; o documento definidor cujo título nomeia um curso só.
2. candidato (o texto nomeia algum curso, ou uma página de um curso só liga o documento): o classificador por
   LLM (perfil ingestao_curso, temperatura 0), com gate: todo slug devolvido tem que estar nomeado no texto ou
   ser o da página que liga.
3. o resto é genérico, sem chamada.
O documento com seções por curso vai ao LLM trecho a trecho; o trecho que nomeia dois cursos recebe a lista.

Estabilidade: o veredito do LLM fica no registro data/parsed/cursos_escopo.json pela chave da entrada efetiva
(versão dos prompts e da lista de cursos, texto do documento, ligações); a execução seguinte só chama o modelo
quando a chave muda. Sem veredito de hoje (falha do provedor, prazo), vale o último veredito do registro, de
outra versão do texto; sem nenhum, a etiqueta gravada fica como está.

A aplicação é um update PATCH só nos trechos cuja etiqueta muda, sem reembedar. Ficam de fora os trechos dos
editais da Reitoria (edital_url, dono pipelines/editais_reitoria.py) e os de outro campus.

Rodar sozinha: python -m pipelines.cursos (CURSOS_SIMULAR=True não grava na base; CURSOS_PRAZO_MIN limita).
"""

import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup
from upstash_vector.types import MetadataUpdateMode

from pipelines.config import INFO_DIR, PARSED_DIR, fetch, index
from pipelines.urls import build_download_url, canonical_url, to_drive_view_url
from rag import llm
from rag.cursos_escopo import (CURSOS, NOMES, PAGINAS_DE_CURSO, _DOC_DEFINE_CURSO, classify_curso_escopo,
                               curso_da_linha_de_vagas, curso_da_pagina, curso_da_turma, cursos_nomeados)

REGISTRO_PATH = PARSED_DIR / "cursos_escopo.json"
SIMULACAO_PATH = PARSED_DIR / "cursos_simulacao.jsonl"
PROMPT_DOCUMENTO = INFO_DIR / "cursos_documento.txt"
PROMPT_TRECHO = INFO_DIR / "cursos_trecho.txt"
PERFIL = "ingestao_curso"
CAMPOS = ("curso_escopo", "cursos", "curso_fonte")
PARALELO = 6
MAX_SAIDA = 600
PRAZO_MIN = 20
SIMULAR = os.getenv("CURSOS_SIMULAR", "False") == "True"

# documento cujo curso vem so do link da pagina de curso: com um destes marcadores ele define o curso e recebe
# curso_escopo; sem eles, recebe a lista cursos (rotulo sem penalidade)
_DEFINIDOR_LIGADO = re.compile(_DOC_DEFINE_CURSO.pattern + r"|atividades complementares|regimento interno|\bementas?\b",
                               re.IGNORECASE)
_ORDEM = {slug: posicao for posicao, (slug, _) in enumerate(CURSOS)}


def _hash(*partes):
    return hashlib.sha1("\x1f".join(partes).encode("utf-8")).hexdigest()[:16]


def versao():
    # muda quando muda o prompt, a lista de cursos ou o modelo: invalida os vereditos do registro de proposito
    return _hash(PROMPT_DOCUMENTO.read_text(encoding="utf-8"), PROMPT_TRECHO.read_text(encoding="utf-8"),
                 json.dumps(CURSOS), json.dumps(NOMES, ensure_ascii=False), llm.PERFIS[PERFIL].modelo)


def ordenar(slugs):
    return sorted(set(slugs), key=lambda slug: _ORDEM.get(slug, 99))


# ── texto e ligacoes ────────────────────────────────────────────────────────────
def _ordem_do_id(id_):
    sufixo = id_.rsplit("#", 1)[-1]
    return int(sufixo) if sufixo.isdigit() else 0


def texto_do_documento(trechos):
    """Remonta o texto de um documento a partir dos trechos gravados.

    Args:
        trechos: trechos do documento na ordem do id (url#i), cada um com "text".

    Returns:
        O texto, sem repetir trecho de texto igual (eventos do calendario e do cronograma compartilham o bloco)
        e sem a sobreposicao do fatiador entre trechos vizinhos.
    """
    texto, vistos = "", set()
    for trecho in trechos:
        parte = trecho.get("text") or ""
        if not parte or parte in vistos:
            continue
        vistos.add(parte)

        # maior sufixo do texto que e prefixo do trecho, procurado pelo inicio do trecho na cauda
        cauda = texto[-1300:]
        sobreposicao, inicio = 0, cauda.find(parte[:40])
        while inicio >= 0:
            if parte.startswith(cauda[inicio:]):
                sobreposicao = len(cauda) - inicio
                break
            inicio = cauda.find(parte[:40], inicio + 1)
        texto += ("\n" if texto and not sobreposicao else "") + parte[sobreposicao:]
    return texto


def _endereco(href, base):
    # endereco do link como o crawler grava o documento (sem rastreio nem fragmento; Drive na forma /view)
    url = canonical_url(urljoin(base, href).split("#")[0])
    return to_drive_view_url(build_download_url(url)).rstrip("/")


def _links_do_main(url):
    resposta = fetch(url, timeout=20)
    resposta.raise_for_status()
    soup = BeautifulSoup(resposta.text, "html.parser")
    principal = soup.find("main", role="main") or soup.find("main")
    if principal is None:
        raise ValueError("pagina sem main")
    return {_endereco(ancora["href"], url) for ancora in principal.find_all("a", href=True)
            if urljoin(url, ancora["href"]).startswith("http")}


def ligacoes_de_cursos(anteriores):
    """Documentos ligados pelas paginas de curso do site (e pelas subpaginas delas, um nivel).

    Args:
        anteriores: links por pagina da execucao anterior, usados quando a pagina falha hoje.

    Returns:
        Tupla (ligado, por_pagina): ligado = {endereco: slug} so dos documentos que paginas de UM curso ligam
        (link de varias paginas e menu ou documento do campus); por_pagina = {pagina: [links]} para o registro.
    """
    por_pagina = {}
    for pagina in PAGINAS_DE_CURSO:
        try:
            links = _links_do_main(pagina + "/")
            for filha in sorted(link for link in links if link.startswith(pagina + "/")):
                links |= _links_do_main(filha + "/")
            if not links:
                raise ValueError("pagina sem links")
            por_pagina[pagina] = sorted(links)
        except Exception as erro:
            print(f"  pagina de curso sem leitura ({str(erro)[:80]}); ficam os links da execucao anterior: {pagina}")
            por_pagina[pagina] = anteriores.get(pagina, [])

    # documento ligado por paginas de um curso so, sem contar a propria pagina de curso
    slugs_por_link = defaultdict(set)
    for pagina, links in por_pagina.items():
        for link in links:
            if not curso_da_pagina(link):
                slugs_por_link[link].add(PAGINAS_DE_CURSO[pagina])
    ligado = {link: next(iter(slugs)) for link, slugs in slugs_por_link.items() if len(slugs) == 1}
    return ligado, por_pagina


def carregar_base():
    # trechos da base por source_url, na ordem do id, com o metadata inteiro (inclusive o texto)
    por_url, cursor = defaultdict(list), ""
    while True:
        pagina = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for vetor in pagina.vectors:
            meta = vetor.metadata or {}
            por_url[meta.get("source_url") or ""].append({**meta, "id": vetor.id})
        cursor = pagina.next_cursor
        if not cursor:
            break
    for trechos in por_url.values():
        trechos.sort(key=lambda trecho: _ordem_do_id(trecho["id"]))
    return por_url


# ── decisao ─────────────────────────────────────────────────────────────────────
def fora_do_escopo(url, trechos):
    # editais da Reitoria tem dono proprio; outro campus nao entra na busca; lista de nomes e so o aviso
    return (not url or any(t.get("edital_url") for t in trechos)
            or all(t.get("campus_scope") == "outro" for t in trechos)
            or all(t.get("lista_nomes") for t in trechos))


def decisao_objetiva(url, trechos, texto):
    """Etiqueta por regra objetiva, sem LLM.

    Returns:
        Dicionario {"estrutura", "cursos", "fonte"} e, nas grades e quadros de vagas, "por_trecho" ({id: slug});
        None quando nenhuma regra se aplica.
    """
    slug = curso_da_pagina(url)
    if slug:
        return {"estrutura": "um_curso", "cursos": [slug], "fonte": "pagina_de_curso"}

    # grade e quadro de vagas: o curso esta escrito em cada registro
    if any(t.get("is_schedule") for t in trechos):
        por_trecho = {t["id"]: curso_da_turma(t.get("text")) for t in trechos}
        return {"estrutura": "por_registro", "cursos": ordenar(s for s in por_trecho.values() if s), "fonte": "grade",
                "por_trecho": por_trecho}
    if all((t.get("text") or "").startswith("Campus Canoas - ") and "vagas ofertadas" in t.get("text", "") for t in trechos):
        por_trecho = {t["id"]: curso_da_linha_de_vagas(t.get("text")) for t in trechos}
        return {"estrutura": "por_registro", "cursos": ordenar(s for s in por_trecho.values() if s), "fonte": "vagas",
                "por_trecho": por_trecho}

    slug = classify_curso_escopo(trechos[0].get("title") or "", texto, url)
    if slug:
        return {"estrutura": "um_curso", "cursos": [slug], "fonte": "titulo"}
    return None


def visao(url, trechos, texto, ligado):
    """Entrada do classificador por documento: cabecalho, inicio e fim do texto e as linhas que nomeiam cursos."""
    linhas = texto.split("\n")
    com_curso = [f"L{numero}: {linha.strip()[:200]}" for numero, linha in enumerate(linhas, 1) if cursos_nomeados(linha)]
    primeiro = trechos[0]
    cabecalho = (f"Título: {primeiro.get('title') or ''}\nEndereço: {url}\n"
                 f"Tipo: {primeiro.get('type')} | Ano: {primeiro.get('published_at')} | Linhas: {len(linhas)} | "
                 f"Caracteres: {len(texto)}\n")
    if ligado:
        cabecalho += f"Páginas de curso do site que ligam este documento: {ligado} ({NOMES[ligado]})\n"
    else:
        cabecalho += "Nenhuma página de curso liga este documento.\n"
    partes = [cabecalho, "=== INÍCIO DO TEXTO ===\n" + texto[:4000]]
    if len(texto) > 5000:
        partes.append("=== FIM DO TEXTO ===\n" + texto[-1000:])
    partes.append(f"=== LINHAS QUE NOMEIAM CURSOS ({len(com_curso)} no total, até 80 mostradas) ===\n" + "\n".join(com_curso[:80]))
    return "\n".join(partes)


def _json(texto):
    # objeto JSON da resposta, tolerando cerca de codigo
    limpo = (texto or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    inicio, fim = limpo.find("{"), limpo.rfind("}")
    objeto = json.loads(limpo[inicio:fim + 1]) if inicio >= 0 else json.loads(limpo)
    if not isinstance(objeto, dict):
        raise ValueError("resposta nao e um objeto JSON")
    return objeto


def _chamar(sistema, usuario):
    # perfil do classificador, esperando no limite por minuto; queda e 5xx ja foram refeitos em llm.gerar;
    # JSON invalido e pedido de novo uma vez
    for tentativa in range(6):
        try:
            resposta = llm.gerar([{"papel": "usuario", "texto": usuario}], sistema=sistema, temperatura=0,
                                 prazo=llm.PRAZO_INGESTAO, perfil=PERFIL, max_saida=MAX_SAIDA)
            try:
                return _json(resposta.texto), resposta.uso
            except (json.JSONDecodeError, ValueError):
                if tentativa >= 1:
                    raise
        except llm.ProvedorIndisponivel as erro:
            if erro.motivo != "fluxo_alto" or tentativa == 5:
                raise
            time.sleep(max(erro.tentar_em or 20, 5))


def normalizar(veredito, nomeados, ligado):
    """Aplica o gate e as regras de forma ao veredito do modelo.

    Args:
        veredito: JSON devolvido pelo modelo.
        nomeados: slugs nomeados no texto do documento.
        ligado: slug da pagina de curso que liga o documento, ou None.

    Returns:
        Tupla (estrutura, cursos): slug fora da lista, ou que nem o texto nomeia nem a pagina liga, cai; "um_curso"
        sem exatamente um slug vira "varios_sem_secoes" ou "generico"; "varios_com_secoes" com menos de dois vira
        "varios_sem_secoes".
    """
    estrutura = (veredito.get("estrutura") or "").strip()
    cursos = [slug for slug in veredito.get("cursos") or [] if slug in NOMES and (slug in nomeados or slug == ligado)]
    cursos = ordenar(cursos)
    if estrutura not in ("um_curso", "varios_com_secoes", "varios_sem_secoes", "generico"):
        estrutura = "generico"
    if not cursos:
        return "generico", []
    if estrutura == "um_curso" and len(cursos) != 1:
        estrutura = "varios_sem_secoes"
    if estrutura == "varios_com_secoes" and len(cursos) < 2:
        estrutura = "varios_sem_secoes"
    if estrutura == "generico":
        cursos = []
    return estrutura, cursos


def etiquetas_dos_trechos(decisao, trechos, texto, vereditos_trecho):
    """Etiqueta desejada de cada trecho a partir da decisao do documento.

    Args:
        decisao: decisao do documento (objetiva ou do modelo), com "estrutura", "cursos", "fonte" e, quando
            houver, "por_trecho" e "so_ligacao".
        trechos: trechos do documento.
        texto: texto remontado do documento.
        vereditos_trecho: {id: slug ou None} do modelo por trecho, nos documentos com secoes.

    Returns:
        {id: {"curso_escopo", "cursos", "curso_fonte"}}, com None nos campos ausentes.
    """
    vazio = dict.fromkeys(CAMPOS)
    estrutura, cursos, fonte = decisao["estrutura"], decisao.get("cursos") or [], decisao.get("fonte")
    saida = {}
    for trecho in trechos:
        etiqueta = dict(vazio)
        if estrutura == "por_registro":
            slug = decisao["por_trecho"].get(trecho["id"])
            if slug:
                etiqueta.update(curso_escopo=slug, curso_fonte=fonte)
        elif estrutura == "um_curso" and decisao.get("so_ligacao") and not _DEFINIDOR_LIGADO.search(texto[:3000]):
            etiqueta.update(cursos=cursos, curso_fonte=fonte)
        elif estrutura == "um_curso":
            etiqueta.update(curso_escopo=cursos[0], curso_fonte=fonte)
        elif estrutura == "varios_sem_secoes":
            etiqueta.update(cursos=cursos, curso_fonte=fonte)
        elif estrutura == "varios_com_secoes":
            # trecho que nomeia dois cursos do documento e fronteira ou parte comum: lista, nunca um slug
            nomeados = [slug for slug in cursos_nomeados(trecho.get("text")) if slug in cursos]
            slug = vereditos_trecho.get(trecho["id"])
            if len(nomeados) < 2 and slug in cursos:
                etiqueta.update(curso_escopo=slug, curso_fonte="modelo_trecho")
            else:
                etiqueta.update(cursos=ordenar(nomeados) if len(nomeados) >= 2 else cursos, curso_fonte="modelo_trecho")
        saida[trecho["id"]] = etiqueta
    return saida


# ── execucao ────────────────────────────────────────────────────────────────────
def carregar_registro():
    if REGISTRO_PATH.exists():
        return json.loads(REGISTRO_PATH.read_text(encoding="utf-8"))
    return {}


def salvar_registro(registro):
    REGISTRO_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRO_PATH.write_text(json.dumps(registro, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def _aplicar(mudancas):
    # update PATCH so dos campos de curso (None apaga o campo); trecho que falha 3 vezes fica para a execucao
    # seguinte, que vai ve-lo de novo divergente
    def um(item):
        id_, campos = item
        for tentativa in range(3):
            try:
                return index.update(id_, metadata=campos, metadata_update_mode=MetadataUpdateMode.PATCH)
            except Exception as erro:
                if tentativa == 2:
                    print(f"  update falhou, fica para a proxima execucao: {id_} ({str(erro)[:80]})")
                    return False
                time.sleep(2 * (tentativa + 1))
    with ThreadPoolExecutor(max_workers=8) as executor:
        return sum(1 for ok in executor.map(um, mudancas) if ok)


def run_cursos(prazo_min=PRAZO_MIN):
    """Calcula a etiqueta de curso de cada trecho da base e grava as que mudaram.

    Args:
        prazo_min: minutos para chamar o modelo; o documento que nao couber fica pendente para a execucao seguinte.
            None para sem prazo.

    Returns:
        Resumo com as contagens da execucao.
    """
    print("\n" + "=" * 60)
    print("FASE 8 — CURSO DE CADA TRECHO")
    print("=" * 60)
    inicio = time.monotonic()
    registro = carregar_registro()
    versao_atual = versao()
    if registro.get("versao") != versao_atual:
        registro = {**registro, "versao": versao_atual, "documentos": {}}
    documentos_reg = registro.setdefault("documentos", {})

    ligado, por_pagina = ligacoes_de_cursos(registro.get("paginas", {}))
    registro["paginas"] = por_pagina
    base = carregar_base()
    print(f"Base: {sum(len(t) for t in base.values())} trechos em {len(base)} documentos; "
          f"{len(ligado)} documentos ligados por pagina de um curso")

    # cascata: regra objetiva, candidato ao modelo, generico
    decisoes, textos, chaves, pendentes_llm = {}, {}, {}, []
    etapas = Counter()
    for url, trechos in base.items():
        if fora_do_escopo(url, trechos):
            etapas["fora_do_escopo"] += 1
            continue
        texto = texto_do_documento(trechos)
        textos[url] = texto
        objetiva = decisao_objetiva(url, trechos, texto)
        if objetiva:
            decisoes[url] = objetiva
            etapas["regra_" + objetiva["fonte"]] += 1
            continue
        nomeados, pagina = cursos_nomeados(texto), ligado.get(url.rstrip("/"))
        if not nomeados and not pagina:
            decisoes[url] = {"estrutura": "generico", "cursos": [], "fonte": None}
            etapas["generico_sem_chamada"] += 1
            continue
        chave = _hash(versao_atual, texto, pagina or "")
        chaves[url] = chave
        anterior = documentos_reg.get(url)
        if anterior and anterior.get("chave") == chave:
            etapas["modelo_do_registro"] += 1
        else:
            pendentes_llm.append((url, chave, nomeados, pagina))
        decisoes[url] = None

    # classificador por documento, em paralelo, ate o prazo
    sistema = PROMPT_DOCUMENTO.read_text(encoding="utf-8").replace(
        "{cursos}", "\n".join(f"- {slug}: {nome}" for slug, nome in NOMES.items()))
    custo, trava = [0.0], threading.Lock()

    def soma_custo(uso):
        with trava:
            custo[0] += (uso or {}).get("custo_usd") or 0

    def classifica(item):
        url, chave, nomeados, pagina = item
        if prazo_min is not None and time.monotonic() - inicio > prazo_min * 60:
            return url, "adiado", None
        try:
            veredito, uso = _chamar(sistema, visao(url, base[url], textos[url], pagina))
        except Exception as erro:
            return url, "erro", str(erro)[:160]
        soma_custo(uso)
        estrutura, cursos = normalizar(veredito, nomeados, pagina)
        so_ligacao = estrutura == "um_curso" and cursos[0] not in nomeados
        # a frase de evidencia nao vai ao registro versionado: pode citar nome e matricula de servidor
        return url, "ok", {"chave": chave, "estrutura": estrutura, "cursos": cursos, "so_ligacao": so_ligacao,
                           "trechos": {}}

    with ThreadPoolExecutor(max_workers=PARALELO) as executor:
        for url, situacao, valor in executor.map(classifica, pendentes_llm):
            etapas["modelo_" + situacao] += 1
            if situacao == "ok":
                documentos_reg[url] = valor
            elif situacao == "erro":
                print(f"  classificador falhou, documento pendente: {url} ({valor})")

    # veredito do modelo valido nesta execucao: do registro, com a chave da entrada de hoje
    def veredito_valido(url):
        reg = documentos_reg.get(url)
        return reg if reg and reg.get("chave") == chaves.get(url) else None

    # classificador por trecho nos documentos com secoes por curso
    pedidos_trecho = []
    for url in decisoes:
        reg = veredito_valido(url)
        if decisoes[url] is None and reg and reg["estrutura"] == "varios_com_secoes":
            cursos = reg["cursos"]
            for trecho in base[url]:
                chave_trecho = _hash(versao_atual, ",".join(cursos), trecho.get("text") or "")
                if chave_trecho not in reg["trechos"]:
                    pedidos_trecho.append((url, chave_trecho, trecho))
    sistema_trecho = PROMPT_TRECHO.read_text(encoding="utf-8")

    def classifica_trecho(item):
        url, chave_trecho, trecho = item
        reg = documentos_reg[url]
        if prazo_min is not None and time.monotonic() - inicio > prazo_min * 60:
            return url, chave_trecho, "adiado"
        lista = ", ".join(f"{slug} ({NOMES[slug]})" for slug in reg["cursos"])
        usuario = f"Título do documento: {trecho.get('title') or ''}\n\n=== TRECHO ===\n{(trecho.get('text') or '')[:6000]}"
        try:
            veredito, uso = _chamar(sistema_trecho.replace("{cursos}", lista), usuario)
        except Exception:
            return url, chave_trecho, "erro"
        soma_custo(uso)
        slug = veredito.get("curso")
        return url, chave_trecho, slug if slug in reg["cursos"] else None

    with ThreadPoolExecutor(max_workers=PARALELO) as executor:
        for url, chave_trecho, slug in executor.map(classifica_trecho, pedidos_trecho):
            if slug in ("adiado", "erro"):
                etapas["trecho_" + slug] += 1
                continue
            documentos_reg[url]["trechos"][chave_trecho] = slug
            etapas["trecho_modelo"] += 1

    # decisao final de cada documento do modelo. sem veredito completo de hoje (erro, adiado), vale o ultimo
    # veredito do registro, de outra versao do texto: o documento reingerido hoje voltou sem etiqueta, e o
    # anterior e a melhor aposta ate o novo sair. sem nenhum veredito, a etiqueta gravada fica como esta
    mudancas, contagem, pendentes = [], Counter(), 0
    simulacao, finais = [], {}
    for url, decisao in decisoes.items():
        vereditos_trecho = {}
        if decisao is None:
            reg = veredito_valido(url)
            completo = reg and (reg["estrutura"] != "varios_com_secoes" or all(
                _hash(versao_atual, ",".join(reg["cursos"]), t.get("text") or "") in reg["trechos"] for t in base[url]))
            if not completo:
                pendentes += 1
                reg = documentos_reg.get(url)
                if not reg:
                    contagem.update(t["curso_escopo"] for t in base[url] if t.get("curso_escopo"))
                    continue
            decisao = {"estrutura": reg["estrutura"], "cursos": reg["cursos"], "fonte": "modelo",
                       "so_ligacao": reg.get("so_ligacao")}
            vereditos_trecho = {t["id"]: reg["trechos"].get(_hash(versao_atual, ",".join(reg["cursos"]), t.get("text") or ""))
                                for t in base[url]} if reg["estrutura"] == "varios_com_secoes" else {}
        finais[url] = {"estrutura": decisao["estrutura"], "cursos": decisao.get("cursos") or [],
                       "fonte": decisao.get("fonte"), "so_ligacao": bool(decisao.get("so_ligacao"))}
        gravados = {t["id"]: t for t in base[url]}
        for id_, etiqueta in etiquetas_dos_trechos(decisao, base[url], textos[url], vereditos_trecho).items():
            if etiqueta["curso_escopo"]:
                contagem[etiqueta["curso_escopo"]] += 1
            atual = gravados[id_]
            if any(atual.get(campo) != etiqueta[campo] for campo in CAMPOS):
                mudancas.append((id_, etiqueta))
                simulacao.append({"id": id_, "antes": {c: atual.get(c) for c in CAMPOS}, "depois": etiqueta})

    # registro sem documentos que sairam da base
    for url in list(documentos_reg):
        if url not in base:
            del documentos_reg[url]

    # sanidade: variacao grande de trechos por curso contra a execucao anterior vira aviso no GitHub Actions
    anterior = registro.get("contagem") or {}
    for slug in NOMES:
        antes, depois = anterior.get(slug, 0), contagem.get(slug, 0)
        if anterior and abs(depois - antes) > max(20, 0.3 * antes):
            print(f"::warning::trechos com curso {slug}: {antes} -> {depois}")
    # queda de mais da metade dos trechos com curso e leitura incompleta ou erro em massa: nao grava
    bloqueado = bool(anterior) and sum(contagem.values()) < 0.5 * sum(anterior.values())
    if bloqueado:
        print(f"::warning::trechos com curso caem de {sum(anterior.values())} para {sum(contagem.values())}; nada gravado")

    # o registro (vereditos pagos) e salvo antes de escrever na base, para uma falha do Upstash nao perde-los
    salvar_registro(registro)
    if SIMULAR or bloqueado:
        SIMULACAO_PATH.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in simulacao), encoding="utf-8")
        (PARSED_DIR / "cursos_decisoes.json").write_text(json.dumps(finais, ensure_ascii=False, indent=0), encoding="utf-8")
        print(f"Simulacao: {len(mudancas)} trechos mudariam (em {SIMULACAO_PATH})")
        aplicados = 0
    else:
        aplicados = _aplicar(mudancas)
        registro["contagem"] = dict(contagem)
        salvar_registro(registro)

    resumo = {"etapas": dict(etapas), "pendentes": pendentes, "trechos_mudados": len(mudancas), "aplicados": aplicados,
              "custo_usd": round(custo[0], 4), "minutos": round((time.monotonic() - inicio) / 60, 1),
              "trechos_por_curso": dict(contagem)}
    print(json.dumps(resumo, ensure_ascii=False))
    return resumo


if __name__ == "__main__":
    prazo = os.getenv("CURSOS_PRAZO_MIN")
    run_cursos(int(prazo) if prazo else None)
