import os
import time
from flask import Flask, request, jsonify, send_from_directory, abort
from rag.chain import ask
from rag.gatekeeper import check_rate_limit, check_global_budget
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
    # rate limit por IP antes de tocar na llm; depende do Redis, entao se ele falhar, fail-closed
    # com 503 LIMPO (nao servir sem a protecao) em vez de estourar um 500 sem controle
    try:
        allowed, reset = check_rate_limit(request)
    except Exception:
        return jsonify({"error": "servico temporariamente indisponivel, tente em instantes"}), 503
    if not allowed:
        return jsonify({"error": "muitas requisicoes, tente novamente em instantes"}), 429

    data = request.get_json(silent=True) or {}
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
    # identidade anonima vinda do cliente (telemetria): teto de tamanho para nao virar bomba
    session_id = data.get("session_id")
    session_id = session_id[:64] if isinstance(session_id, str) else None
    user_id = data.get("user_id")
    user_id = user_id[:64] if isinstance(user_id, str) else None

    # roda o agente com trace e envia a telemetria do turno (Langfuse); a telemetria é
    # opcional e protegida, e o try garante que uma falha do ask vire 500 limpo (e fique
    # registrada) em vez de estourar sem rastro
    trace, inicio, erro, response = {}, time.time(), None, None
    try:
        response = ask(query, history=history, trace=trace)
    except Exception as e:
        erro = f"{type(e).__name__}: {str(e)[:200]}"
    latencia_ms = int((time.time() - inicio) * 1000)
    registrar_chat(query, history, trace, response, latencia_ms, erro=erro, session_id=session_id, user_id=user_id)

    if erro is not None:
        return jsonify({"error": "erro ao processar a pergunta"}), 500
    return jsonify({"response": response})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(debug=True, port=port, threaded=True)
