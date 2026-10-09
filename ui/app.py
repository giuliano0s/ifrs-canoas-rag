import itertools
import json
import os
import time
from flask import Flask, Response, request, jsonify, send_from_directory, abort
from rag.chain import ask_stream
from rag.gatekeeper import check_rate_limit, check_global_budget
from rag.llm import ProvedorIndisponivel
from rag.telemetry import registrar_chat

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

# tetos anti-abuso/custo do input: quantidade E tamanho (nao so a contagem de mensagens)
MAX_HISTORY_MESSAGES = 20
MAX_QUERY_CHARS = 2000       # uma pergunta
MAX_MSG_CHARS = 4000         # cada mensagem do historico
MAX_HISTORY_CHARS = 12000    # historico inteiro (soma), corta as mais antigas
# arquivos que a rota estatica pode servir; qualquer outro caminho vira 404 (nao vaza o fonte)
ALLOWED_STATIC = {"widget.js", "index.html"}


# serve o snapshot estatico gerado pela pipeline de ingestao
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/<path:filename>")
def static_files(filename):
    # so serve o whitelist; qualquer outro caminho (ex: /app.py) vira 404, sem vazar o fonte
    if filename not in ALLOWED_STATIC:
        abort(404)
    return send_from_directory(BASE_DIR, filename)


def sanitize_history(raw):
    if not isinstance(raw, list):
        return []
    cleaned = []
    for msg in raw:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            cleaned.append({"role": role, "content": content[:MAX_MSG_CHARS]})
    cleaned = cleaned[-MAX_HISTORY_MESSAGES:]
    # teto de tamanho total: mantem as mais recentes ate encher o orcamento de chars
    total, limitado = 0, []
    for msg in reversed(cleaned):
        total += len(msg["content"])
        if total > MAX_HISTORY_CHARS:
            break
        limitado.append(msg)
    limitado.reverse()
    return limitado


@app.route("/chat", methods=["POST"])
def chat():
    # le o corpo ANTES do rate limit apenas para extrair o user_id (parse de JSON e barato e nao
    # toca na llm); o limite por dispositivo depende dele, senao o NAT do wifi do campus faria uma
    # turma inteira compartilhar a cota de um IP.
    data = request.get_json(silent=True) or {}
    # identidade anonima vinda do cliente (rate limit + telemetria): teto de tamanho para nao virar bomba
    session_id = data.get("session_id")
    session_id = session_id[:64] if isinstance(session_id, str) else None
    user_id = data.get("user_id")
    user_id = user_id[:64] if isinstance(user_id, str) else None

    # rate limit por dispositivo (com teto de IP por tras) antes de tocar na llm; depende do Redis,
    # entao se ele falhar, fail-closed com 503 LIMPO em vez de estourar um 500 sem controle
    try:
        allowed, reset = check_rate_limit(request, user_id=user_id)
    except Exception:
        return jsonify({"error": "servico temporariamente indisponivel, tente em instantes"}), 503
    if not allowed:
        return jsonify({"error": "muitas requisicoes, tente novamente em instantes"}), 429

    query = data.get("query", "").strip()

    if not query:
        return jsonify({"error": "query vazia"}), 400
    if len(query) > MAX_QUERY_CHARS:
        return jsonify({"error": "pergunta muito longa, resuma um pouco"}), 413

    # teto global de volume no dia (proxy de gasto); protege o custo do Gemini
    if not check_global_budget():
        return jsonify({"error": "o assistente atingiu o limite de uso de hoje, tente novamente amanha"}), 503

    # histórico chega pronto do cliente, servidor não guarda estado
    history = sanitize_history(data.get("history"))

    # roda o agente em streaming. o 1o evento sai antes de abrir a resposta: limite ou queda do provedor
    # na 1a chamada ainda vira 503 com o aviso de fluxo alto do widget; falha depois dele vira evento
    # "erro" dentro do fluxo. a telemetria do turno (Langfuse) e registrada uma vez, ao fim
    trace, inicio = {}, time.time()
    eventos = ask_stream(query, history=history, trace=trace)
    try:
        primeiro = next(eventos)
    except Exception as e:
        erro = (f"ProvedorIndisponivel[{e.motivo}]: {str(e)[:200]}" if isinstance(e, ProvedorIndisponivel)
                else f"{type(e).__name__}: {str(e)[:200]}")
        registrar_chat(query, history, trace, None, int((time.time() - inicio) * 1000), erro=erro,
                       session_id=session_id, user_id=user_id)
        return jsonify(_aviso_de_falha(e)), (503 if isinstance(e, ProvedorIndisponivel) else 500)

    def transmitir():
        resposta, erro, primeiro_texto_ms = None, None, None
        try:
            for evento in itertools.chain([primeiro], eventos):
                if evento["tipo"] == "texto" and primeiro_texto_ms is None:
                    primeiro_texto_ms = int((time.time() - inicio) * 1000)
                if evento["tipo"] == "fim":
                    resposta = evento["resposta"]
                yield json.dumps(evento, ensure_ascii=False) + "\n"
        except Exception as e:
            erro = (f"ProvedorIndisponivel[{e.motivo}]: {str(e)[:200]}" if isinstance(e, ProvedorIndisponivel)
                    else f"{type(e).__name__}: {str(e)[:200]}")
            yield json.dumps({"tipo": "erro", **_aviso_de_falha(e)}, ensure_ascii=False) + "\n"
        finally:
            registrar_chat(query, history, trace, resposta, int((time.time() - inicio) * 1000), erro=erro,
                           session_id=session_id, user_id=user_id, primeiro_texto_ms=primeiro_texto_ms)

    return Response(transmitir(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _aviso_de_falha(erro):
    # mensagem ao aluno por tipo de falha. fluxo alto no provedor do LLM leva a espera sugerida, e o
    # widget avisa e tenta de novo sozinho; credito ou queda: tentar mais tarde; o resto: erro generico
    if isinstance(erro, ProvedorIndisponivel) and erro.motivo == "fluxo_alto":
        return {"error": "Muitas perguntas ao mesmo tempo agora. Tente novamente em instantes.",
                "fluxo_alto": True, "tentar_em": erro.tentar_em}
    if isinstance(erro, ProvedorIndisponivel):
        return {"error": "O assistente está indisponível no momento. Tente novamente em alguns minutos."}
    return {"error": "Não consegui responder agora. Tente novamente em instantes."}

if __name__ == "__main__":
    # execucao local de dev: liga o dump de retrieval (DEBUG do pacote rag) no console.
    # no serverless este bloco nao roda e nenhum handler e configurado, entao so WARNING+
    # chega ao log da plataforma (a pergunta do usuario nao vai ao log de funcao).
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("rag").setLevel(logging.DEBUG)
    port = int(os.getenv("PORT", "5000"))
    app.run(debug=True, port=port, threaded=True)
