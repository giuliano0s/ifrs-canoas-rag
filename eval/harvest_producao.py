"""Harvest da telemetria de producao (Langfuse) -> coleta local (formato do eval).

A telemetria de producao (rag/telemetry.py) grava cada turno no Langfuse no MESMO schema da
coleta, porem LEVE: guarda os ids dos chunks (url#i), nao o texto cru. Este script roda local,
puxa esses traces, reconstroi o texto de cada chunk via index.fetch no Upstash (id -> texto) e
escreve registros no formato da coleta em eval/runs/coleta_producao.jsonl, prontos para
inspecao e curadoria (perguntas reais viram candidatas a caso de golden).

Suporte parcial de fase: o registro reconstruido cobre retrieval-doc (contexto_urls) e
retrieval-span (chunks_textos); rank_rerank e sources nao sao guardados na telemetria (payload
leve), entao MRR e citacao exigem re-coletar o caso ao promove-lo a golden.

Cada turno e lido pela API v2 de observacoes como a observacao raiz "chat" (no Langfuse v4 nao ha
objeto de trace: o turno e a raiz, com o registro no metadata dela), dos ultimos `dias` dias.

Local (usa as chaves de leitura do .env: LANGFUSE_* e UPSTASH_*). Uso:
    python -m eval.harvest_producao [limite] [dias]
"""
import os, sys, json
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RAIZ not in sys.path:
    sys.path.insert(0, _RAIZ)

SAIDA = os.path.join(_RAIZ, "eval", "runs", "coleta_producao.jsonl")

# chaves do registro que o harvest le; a API v2 corta em 200 caracteres o valor que nao e pedido inteiro
CHAVES_METADATA = ("input", "erro", "acao_real", "resposta", "buscas", "modelo", "prompt_versao", "ts",
                   "session_id", "user_id")


def _decodifica(valor):
    """Devolve o valor do metadata como objeto Python.

    Args:
        valor: valor de uma chave do metadata vindo da API v2 de observacoes; a raiz gravada pelo SDK 5
            pode voltar como texto JSON, a gravada pelo SDK 2 volta como objeto.

    Returns:
        O valor decodificado, ou o proprio valor quando ele nao e texto JSON.
    """
    if isinstance(valor, str) and valor[:1] in ('[', '{', '"'):
        try:
            return json.loads(valor)
        except ValueError:
            return valor
    return valor


def _traces_langfuse(limite, dias=180):
    """Le os turnos de chat de producao do Langfuse, do mais recente para tras.

    Args:
        limite: numero maximo de turnos.
        dias: janela de busca, contada a partir de agora (a API v2 exige intervalo de tempo).

    Returns:
        Lista de pares (trace_id, metadata decodificado), um por turno.
    """
    from langfuse import Langfuse
    cli = Langfuse(
        public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
        secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
        base_url=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
    )

    # raizes "chat" da janela, paginando pelo cursor
    ate = datetime.now(timezone.utc)
    turnos, cursor = [], None
    while len(turnos) < limite:
        resp = cli.api.observations.get_many(
            name="chat", is_root_observation=True, fields="core,basic,metadata",
            expand_metadata=",".join(CHAVES_METADATA), limit=min(100, limite - len(turnos)), cursor=cursor,
            from_start_time=ate - timedelta(days=dias), to_start_time=ate,
        )
        for linha in resp.data or []:
            meta = {chave: _decodifica(valor) for chave, valor in (linha.metadata or {}).items()}
            turnos.append((linha.trace_id, meta))
        cursor = getattr(resp.meta, "cursor", None)
        if not resp.data or not cursor:
            break
    return turnos[:limite]


def _hydrata_textos(ids):
    # reconstroi id -> texto do chunk via Upstash (a telemetria guardou so os ids url#i)
    from upstash_vector import Index
    idx = Index(url=os.getenv("UPSTASH_ENDPOINT"), token=os.getenv("UPSTASH_API_KEY"))
    textos, ids = {}, list(dict.fromkeys(ids))  # dedup preservando ordem
    for i in range(0, len(ids), 100):
        for v in (idx.fetch(ids=ids[i:i + 100], include_metadata=True) or []):
            if v is not None:
                textos[v.id] = (v.metadata or {}).get("text", "")
    return textos


def harvest(limite=500, dias=180):
    # puxa os turnos, faz um unico fetch em lote de todos os ids e reescreve no formato da coleta
    traces = _traces_langfuse(limite, dias)
    todos_ids = [cid for _, m in traces
                 for b in (m.get("buscas") or [])
                 for cid in b.get("contexto_ids", [])]
    textos = _hydrata_textos(todos_ids)

    os.makedirs(os.path.dirname(SAIDA), exist_ok=True)
    n = 0
    with open(SAIDA, "w", encoding="utf-8") as f:
        for trace_id, m in traces:
            buscas = []
            for b in (m.get("buscas") or []):
                ids = b.get("contexto_ids", [])
                id2url = {h.get("id"): h.get("url") for h in b.get("hits", [])}
                buscas.append({
                    "query": b.get("query"),
                    "hits": b.get("hits", []),
                    "contexto_urls": [id2url.get(cid) for cid in ids],
                    "contexto_ids": ids,
                    "chunks_textos": [textos.get(cid, "") for cid in ids],
                })
            rec = {
                "case_id": f"prod-{trace_id}", "run": 0,
                "input": m.get("input"), "erro": m.get("erro"),
                "acao_real": m.get("acao_real"), "resposta": m.get("resposta"),
                "buscas": buscas,
                "modelo": m.get("modelo"), "prompt_versao": m.get("prompt_versao"),
                "ts": m.get("ts"), "session_id": m.get("session_id"), "user_id": m.get("user_id"), "origem": "producao",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    print(f"{n} registros de producao -> {SAIDA} (textos reconstruidos: {len(textos)} chunks)")


if __name__ == "__main__":
    harvest(int(sys.argv[1]) if len(sys.argv) > 1 else 500, int(sys.argv[2]) if len(sys.argv) > 2 else 180)
