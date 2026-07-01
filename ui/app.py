import os
from flask import Flask, request, jsonify, send_from_directory
from rag.chain import ask
from rag.gatekeeper import check_rate_limit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

# limite de trocas aceitas no histórico enviado pelo cliente
MAX_HISTORY_MESSAGES = 20


# serve o snapshot estatico gerado pela pipeline de ingestao
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/<path:filename>")
def static_files(filename):
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
            cleaned.append({"role": role, "content": content})
    return cleaned[-MAX_HISTORY_MESSAGES:]


@app.route("/chat", methods=["POST"])
def chat():
    # bloqueia excesso de requisições por ip antes de tocar na llm
    allowed, reset = check_rate_limit(request)
    if not allowed:
        return jsonify({"error": "muitas requisicoes, tente novamente em instantes"}), 429

    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()

    if not query:
        return jsonify({"error": "query vazia"}), 400

    # histórico chega pronto do cliente, servidor não guarda estado
    history = sanitize_history(data.get("history"))

    response = ask(query, history=history)
    return jsonify({"response": response})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(debug=True, port=port, threaded=True)
