"""Parser de planilhas (fase 3.5): estrutura o CSV já baixado pelo crawler (Google Sheets
publicados no site) em registros auto-contidos via LLM, um por linha, cada um com o assunto da
planilha e o cabeçalho da sua seção, e resolve a data pela cadeia
url -> url do pai -> conteúdo do pai -> conteúdo do CSV. A planilha vai ao modelo com as linhas
numeradas, e toda linha tem que voltar como registro ou como cabeçalho: a que falta é devolvida ao
modelo, como na conferência de datas do calendário. Planilha ligada fora do site do campus fica de
fora quando a página dela nomeia outro campus no título ou, sem campus no título, quando ela não cita
Canoas.
"""

import csv
import io
import json
import time

from pipelines.chunker import campus_citado, e_canoas
from pipelines.config import HEADERS, PARSED_DIR, SHEETS_FALHAS, SHEETS_PARSED_PATH
from pipelines.dates import extract_date_from_text, extract_date_from_url
from pipelines.parser_html import parse_html_page
from rag import llm


_PROMPT_PLANILHA = """Voce converte planilhas do IFRS Campus Canoas em registros. A planilha vem uma linha por linha do CSV, numerada (L1, L2, ...), com as celulas separadas por " | " (celula vazia fica vazia). Ela foi publicada na pagina: {pagina}

Responda SO com um objeto JSON, sem texto antes ou depois, neste formato:
{{"assunto": "...", "linhas_de_cabecalho": [1, 2], "secoes": [{{"cabecalho": "...", "registros": [{{"linhas": [3], "texto": "..."}}]}}]}}

REGRAS:
- assunto: nome curto do que a planilha e, em ate 12 palavras, a partir do titulo da planilha e do titulo e endereco da pagina, dizendo a que ela se refere (ex: "Horarios de atendimento ao aluno", "Remuneracao e beneficios do Pregao Eletronico 90.009/2099"). Data, numero de registro e demais identificadores longos NAO vao no assunto.
- linhas_de_cabecalho: as linhas que nao sao registro (titulo, identificacao do documento, nomes de colunas, cabecalho de secao, linha so com rotulo).
- secoes: os grupos de registros sob um mesmo cabecalho de secao da planilha (turno, curso e turma, sala, setor, cargo). Cabecalhos aninhados (ex: o turno e, abaixo dele, a turma) se juntam num cabecalho so. Planilha sem cabecalho de secao tem uma secao so, com cabecalho "".
- registros: toda linha com dado de um registro (um nome, um valor, uma aula) vira UM registro com o numero da linha, mesmo que outras colunas estejam vazias (ex: docente sem horario: o nome, a sala e o e-mail que houver). Um registro cujo valor esta na linha seguinte leva as duas linhas. O texto e uma frase que nao repete o assunto nem o cabecalho da secao, com nomes, salas, e-mails, dias, horarios e valores exatamente como na planilha, e com o nome da coluna quando ele da sentido ao valor (ex: "atende na segunda das 10h as 11h"). Se uma celula tiver varios itens, junte-os com virgula na mesma frase.
- Toda linha numerada aparece uma vez: em linhas_de_cabecalho ou num registro.
- Baseie-se ESTRITAMENTE na planilha. Nunca invente dados. Os dados do EXEMPLO abaixo sao ficticios e servem so para mostrar o formato: NUNCA os inclua na saida.
- Se a planilha nao tiver nenhuma linha de dados, devolva secoes vazia.

EXEMPLO (ficticio, apenas formato):
L1: Plantao de duvidas 2099 |  |
L2: MANHA |  |
L3: Docente | Sala | Segunda
L4: ZZZ Exemplo | X000 | 10h as 11h
L5: YYY Exemplo | X001 |
Saida de exemplo:
{{"assunto": "Plantao de duvidas 2099", "linhas_de_cabecalho": [1, 2, 3], "secoes": [{{"cabecalho": "Manha", "registros": [{{"linhas": [4], "texto": "ZZZ Exemplo atende na segunda das 10h as 11h, sala X000."}}, {{"linhas": [5], "texto": "YYY Exemplo, sala X001, sem horario informado."}}]}}]}}

AGORA CONVERTA ESTA PLANILHA:
{linhas}"""


class EstruturacaoFalhou(Exception):
    """O modelo nao devolveu o esquema pedido em nenhuma das leituras de uma planilha."""


def linhas_numeradas(csv_text):
    """Numera as linhas nao vazias de um CSV para o estruturador.

    Args:
        csv_text: conteudo CSV da planilha.

    Returns:
        Dicionario {numero: linha}, com as celulas separadas por " | " e os itens de uma celula
        com varias linhas separados por " / ".
    """

    linhas, numero = {}, 0
    for celulas in csv.reader(io.StringIO(csv_text)):
        celulas = [" / ".join(" ".join(item.split()) for item in celula.splitlines() if item.strip()) for celula in celulas]
        if any(celulas):
            numero += 1
            linhas[numero] = " | ".join(celulas)
    return linhas


def _ler_estrutura(resposta, numeros):
    """Valida o JSON devolvido pelo estruturador de planilhas.

    Args:
        resposta: texto devolvido pelo modelo, opcionalmente entre cercas de codigo.
        numeros: conjunto dos numeros de linha enviados ao modelo.

    Returns:
        (assunto, [(cabecalho, [registros])], linhas que nao voltaram), com cada texto numa linha so
        (espacos e quebras colapsados) e sem registro vazio, ou None se o texto nao seguir o esquema
        pedido. Linha de registro vazio conta como linha que nao voltou.
    """

    # remove a cerca de codigo e decodifica
    corpo = resposta.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        dados = json.loads(corpo)
    except json.JSONDecodeError:
        return None

    # confere o esquema do topo
    if not isinstance(dados, dict) or not isinstance(dados.get("assunto"), str):
        return None
    if not isinstance(dados.get("secoes"), list) or not isinstance(dados.get("linhas_de_cabecalho", []), list):
        return None
    cobertas = {numero for numero in dados.get("linhas_de_cabecalho", []) if isinstance(numero, int)}

    # confere cada secao e cada registro, acumulando as linhas cobertas. cada texto vira uma linha so,
    # porque o chunker corta o registro na quebra de linha
    secoes = []
    for secao in dados["secoes"]:
        if not isinstance(secao, dict) or not isinstance(secao.get("cabecalho", ""), str) or not isinstance(secao.get("registros"), list):
            return None
        registros = []
        for registro in secao["registros"]:
            if not isinstance(registro, dict) or not isinstance(registro.get("texto"), str) or not isinstance(registro.get("linhas"), list):
                return None
            texto = " ".join(registro["texto"].split())
            if texto:
                cobertas.update(numero for numero in registro["linhas"] if isinstance(numero, int))
                registros.append(texto)
        secoes.append((" ".join(secao.get("cabecalho", "").split()), registros))
    return " ".join(dados["assunto"].split()), secoes, sorted(numeros - cobertas)


def registros_auto_contidos(assunto, secoes):
    """Monta um registro por linha, cada um com o assunto da planilha e o cabecalho da sua secao.

    Args:
        assunto: do que a planilha trata.
        secoes: lista de (cabecalho, registros) na ordem da planilha.

    Returns:
        Texto com um registro por linha, ou string vazia se nao houver registro.
    """

    # o contexto vai junto de cada registro porque cada linha vira um chunk proprio
    linhas = []
    for cabecalho, registros in secoes:
        prefixo = " - ".join(parte for parte in (assunto, cabecalho) if parte)
        linhas.extend(f"{prefixo}: {registro}" if prefixo else registro for registro in registros)
    return "\n".join(linhas)


def structure_sheet(csv_text, pagina=""):
    """Estrutura o CSV de uma planilha em registros auto-contidos, via LLM, conferindo as linhas.

    Args:
        csv_text: conteudo CSV da planilha publicada.
        pagina: titulo e endereco da pagina onde a planilha foi publicada, pista para o assunto.

    Returns:
        (assunto, texto com um registro por linha, linhas que nao voltaram em nenhuma leitura),
        com a leitura que deixou menos linhas de fora em ate 3 (no empate, a com mais registros);
        None se a planilha nao tiver linha de dados.

    Raises:
        EstruturacaoFalhou: nenhuma das 3 leituras seguiu o esquema pedido.
    """

    linhas = linhas_numeradas(csv_text)
    prompt = _PROMPT_PLANILHA.format(pagina=pagina or "(sem titulo)",
                                     linhas="\n".join(f"L{numero}: {linha}" for numero, linha in linhas.items()))
    melhor, pedido = None, prompt
    for attempt in range(3):
        try:
            estrutura = _ler_estrutura(llm.completar(pedido, temperatura=0.2, leve=True), set(linhas))
        except Exception as e:
            print(f"  ERRO estruturação planilha (tentativa {attempt+1}/3): {e}")
            time.sleep(30 * (attempt + 1))
            continue
        if estrutura is None:
            print(f"  AVISO: estruturação de planilha fora do formato (tentativa {attempt+1}/3)")
            continue

        # fica a leitura com menos linhas de fora; as que faltaram voltam ao modelo na seguinte
        if melhor is None or _ordem_da_leitura(estrutura) < _ordem_da_leitura(melhor):
            melhor = estrutura
        if not melhor[2]:
            break
        faltas = "\n".join(f"L{numero}: {linhas[numero]}" for numero in melhor[2])
        print(f"  AVISO: {len(melhor[2])} linha(s) da planilha fora da estruturação (tentativa {attempt+1}/3)")
        pedido = f"{prompt}\n\nNUMA LEITURA ANTERIOR, ESTAS LINHAS FICARAM DE FORA. Inclua cada uma, como registro ou em linhas_de_cabecalho:\n{faltas}"

    # sem registro, a planilha nao tem dados
    if melhor is None:
        raise EstruturacaoFalhou("nenhuma leitura seguiu o esquema pedido")
    assunto, secoes, faltas = melhor
    texto = registros_auto_contidos(assunto, secoes)
    return (assunto, texto, faltas) if texto else None


def _ordem_da_leitura(estrutura):
    # menos linhas de fora primeiro; no empate, mais registros
    _, secoes, faltas = estrutura
    return len(faltas), -sum(len(registros) for _, registros in secoes)


def e_do_campus(parent, csv_text, titulo_pai=""):
    """Diz se uma planilha pertence ao Campus Canoas.

    Args:
        parent: pagina onde o link da planilha foi achado (None para link semeado pela whitelist).
        csv_text: conteudo CSV da planilha.
        titulo_pai: titulo dessa pagina; no portal de ingresso, cada pagina de chamada nomeia o campus
            so no titulo ("1a CHAMADA | Campus Alvorada - Processo Seletivo IFRS 2025/1").

    Returns:
        True se a pagina de origem e do site do campus; senao, se o titulo dela nomeia Canoas; e,
        quando o titulo nao nomeia campus nenhum, se a planilha cita Canoas.
    """

    if "/canoas/" in (parent or ""):
        return True
    campus = campus_citado(titulo_pai)
    if campus:
        return e_canoas(campus)
    return "canoas" in csv_text.lower()

def resolve_sheet_date(url, parent, csv_text, parent_page=None):
    # cadeia de fallback: url da planilha, url pai, llm no texto do pai, llm no csv, senao None
    date = extract_date_from_url(url) or (extract_date_from_url(parent) if parent else None)
    if date:
        return date
    if parent_page and parent_page.get("text"):
        date = extract_date_from_text(parent_page["text"])
        if date:
            return date
    return extract_date_from_text(csv_text)

def parse_sheet(rec):
    """Estrutura uma planilha baixada pelo crawler.

    Args:
        rec: dicionario com url, parent, csv_text e source_hash da planilha.

    Returns:
        (planilha parseada ou None, motivo). O motivo diz por que a planilha ficou de fora, ou
        quantas linhas ficaram fora da estruturacao; e vazio quando a planilha saiu inteira.
    """

    url, parent, csv_text = rec["url"], rec["parent"], rec["csv_text"]
    # a pagina onde a planilha foi publicada decide o campus, nomeia o assunto e serve de fonte para a data
    parent_page = parse_html_page(parent, HEADERS) if parent else None
    titulo_pai = (parent_page or {}).get("title", "")

    # planilha ligada fora do site do campus (o portal de ingresso lista todos os campi) so entra se for de Canoas
    if not e_do_campus(parent, csv_text, titulo_pai):
        return None, "outro_campus"
    try:
        estruturada = structure_sheet(csv_text, f"{titulo_pai} ({parent})" if parent else "")
    except EstruturacaoFalhou:
        return None, "estruturacao_falhou"
    if not estruturada:
        return None, "sem_dados"
    assunto, text, faltas = estruturada
    planilha = {
        "source_url":   url,
        "title":        assunto,
        "text":         text,
        "published_at": resolve_sheet_date(url, parent, csv_text, parent_page),
        "source_hash":  rec["source_hash"],
    }
    return planilha, (f"{len(faltas)} linha(s) fora da estruturacao, ingerida assim mesmo" if faltas else "")

def run_sheets_parser(sheets_dirty):
    print("\n" + "="*60)
    print("FASE 3.5 — PARSER PLANILHAS (estrutura o CSV ja baixado pelo crawler)")
    print("="*60)

    PARSED_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Planilhas a processar (novo+mudado): {len(sheets_dirty)}")

    results = []
    for rec in sheets_dirty:
        planilha, motivo = parse_sheet(rec)
        if motivo:
            print(f"  {'AVISO' if planilha else 'IGNORADA'}: planilha {motivo}"); SHEETS_FALHAS.append((rec["url"], motivo))
        if planilha:
            results.append(planilha)

    SHEETS_PARSED_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nParsed: {len(results)} planilhas")
    return results
