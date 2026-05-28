import os
import uuid
from flask import Flask, request, jsonify, send_from_directory, session
from flask_cors import CORS
from rag.chain import ask

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.secret_key = os.urandom(24)
CORS(app, supports_credentials=True, origins=["http://127.0.0.1:5500", "http://127.0.0.1:5000"])

# armazena histórico por sessão em memória
histories = {}

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(BASE_DIR, filename)

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    query = data.get("query", "").strip()

    if not query:
        return jsonify({"error": "query vazia"}), 400

    # identifica sessão do usuário
    session_id = session.get("id")
    if not session_id:
        session_id = str(uuid.uuid4())
        session["id"] = session_id

    # recupera ou cria histórico da sessão
    history = histories.get(session_id, [])

    # gera resposta
    response = ask(query, history=history)

    # atualiza histórico
    history.append({"role": "user", "content": query})
    history.append({"role": "assistant", "content": response})
    histories[session_id] = history

    return jsonify({"response": response})

if __name__ == "__main__":
    app.run(debug=True, port=5000)