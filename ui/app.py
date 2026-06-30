import os
import time
import requests
from flask import Flask, request, jsonify, send_from_directory
from rag.chain import ask
from rag.gatekeeper import check_rate_limit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

# limite de trocas aceitas no histórico enviado pelo cliente
MAX_HISTORY_MESSAGES = 20

# origem clonada e cache em memória da página
SOURCE_URL = "https://ifrs.edu.br/canoas/"
PAGE_TTL = 600
_page_cache = {"html": None, "ts": 0.0}

# faixa fixa de aviso de ambiente não oficial
ALERT_BANNER = """
<div style="position:fixed;top:0;left:0;right:0;z-index:99999;background:#9a3412;color:#fff;
text-align:center;font:600 13px/1.4 'Segoe UI',sans-serif;padding:8px 12px;">
Ambiente de teste não oficial. Esta página não é o site do IFRS Campus Canoas e serve apenas para
demonstração de um assistente de IA.</div>
<div style="height:34px;"></div>
"""


def build_page():
    resp = requests.get(SOURCE_URL, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }, timeout=15)
    resp.raise_for_status()
    html = resp.text.replace("<body", ALERT_BANNER + "<body", 1) if "<body" in resp.text else ALERT_BANNER + resp.text
    return html.replace("</body>", '<script src="widget.js"></script></body>')


def get_page():
    now = time.monotonic()
    if _page_cache["html"] and now - _page_cache["ts"] < PAGE_TTL:
        return _page_cache["html"]
    try:
        html = build_page()
        _page_cache["html"] = html
        _page_cache["ts"] = now
        return html
    except Exception:
        # fallback para o cache antigo ou o arquivo salvo
        if _page_cache["html"]:
            return _page_cache["html"]
        with open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8") as f:
            return f.read()


@app.route("/")
def index():
    return get_page()

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