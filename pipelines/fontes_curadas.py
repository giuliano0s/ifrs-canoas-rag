"""Fontes curadas: textos curtos com um fato oficial que a base raspada não traz, por decisão (as listas
com nomes de candidatos ficam fora, por privacidade) ou por estrutura (o portal só mostra o resultado num
buscador). Vivem em data/info/fontes_curadas.json, cada uma com a origem do fato e a data até a qual vale
sem revisão, e entram pela ingestão como qualquer documento: hash do conteúdo, troca quando muda.

Cada fonte é buscada pelas frases no formato das perguntas que ela responde (textos_busca, um vetor por
frase) e entregue com o texto inteiro, como os eventos do calendário; o contexto do atendimento colapsa
trechos repetidos, então a fonte ocupa no máximo uma vaga. A URL leva um fragmento (#...), que o crawler
descarta de todo link que segue: a identidade da fonte curada nunca colide com a de uma página raspada, e
o link continua abrindo a página oficial.

Uso avulso, sem rodar o crawler: python -m pipelines.fontes_curadas
"""

import json
import os
from datetime import date

from pipelines.config import FONTES_CURADAS_PATH, REINGEST, REPARSE, index
from pipelines.hashing import source_hash

TIPO = "curada"
_CAMPOS = ("url", "titulo", "texto", "origem", "revisar_ate")


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


def chunks_da_fonte(fonte, ano):
    """Monta os chunks de uma fonte curada.

    Args:
        fonte: entrada do arquivo de dados.
        ano: ano gravado em published_at; entra no hash, então a fonte se renova a cada virada de ano e
            não envelhece no rerank por data.

    Returns:
        Um chunk por frase de busca, todos com o texto inteiro (sem frases, o próprio texto é buscado).
    """
    hash_fonte = source_hash(json.dumps({**fonte, "ano": ano}, ensure_ascii=False, sort_keys=True))
    base = {"text": fonte["texto"], "source_url": fonte["url"], "title": fonte["titulo"], "type": TIPO,
            "published_at": ano, "source_hash": hash_fonte}
    return [{**base, "texto_busca": frase} for frase in fonte.get("textos_busca") or [None]]


def run_fontes_curadas(estado):
    """Compara as fontes do arquivo com a base e prepara o que a ingestão grava.

    Args:
        estado: estado da base por URL (carregar_estado), com o tipo de cada URL.

    Returns:
        (chunks das fontes novas ou mudadas, URLs de fontes mudadas a substituir). Os chunks de uma fonte
        tirada do arquivo são apagados aqui.
    """
    print("\n" + "=" * 60)
    print("FASE 4.5 — FONTES CURADAS")
    print("=" * 60)
    hoje = date.today()
    fontes = carregar()

    # fonte nova ou com conteúdo diferente do gravado entra; vencida segue valendo, com aviso
    chunks, mudadas = [], set()
    for fonte in fontes:
        if fonte["revisar_ate"] < hoje.isoformat():
            _avisar(f"revisão vencida desde {fonte['revisar_ate']}: {fonte['url']} (origem: {fonte['origem']})")
        novos = chunks_da_fonte(fonte, hoje.year)
        gravada = estado.get(fonte["url"])
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
