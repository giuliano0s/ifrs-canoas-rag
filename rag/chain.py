import os
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from google import genai as google_genai
import time
from datetime import datetime

data_atual = datetime.now().strftime("%d/%m/%Y")

load_dotenv()

# configurações
QDRANT_ENDPOINT = os.getenv("QDRANT_ENDPOINT")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
GEMINI_API_KEY_T1 = os.getenv("GEMINI_API_KEY_T1")
COLLECTION_NAME = "ifrs-canoas"
AGENT_NAME = "agente-ifrs"

# clientes
qdrant = QdrantClient(url=QDRANT_ENDPOINT, api_key=QDRANT_API_KEY)
google_client = google_genai.Client(api_key=GEMINI_API_KEY_T1)

# carrega prompt do agente
_prompt_path = os.path.join(os.path.dirname(__file__), f"../data/info/{AGENT_NAME}.txt")
with open(_prompt_path, "r", encoding="utf-8") as f:
    agent_prompt = f.read()


def search(query, top_k=10):
    for attempt in range(3):
        try:
            result = google_client.models.embed_content(
                model="gemini-embedding-001",
                contents=query
            )
            vector = result.embeddings[0].values
            hits = qdrant.search(
                collection_name=COLLECTION_NAME,
                query_vector=vector,
                limit=top_k,
                with_payload=True
            )
            return hits
        except Exception as e:
            if "429" in str(e):
                wait = 30 * (attempt + 1)
                print(f"  Rate limit embedding, aguardando {wait}s...")
                time.sleep(wait)
            else:
                raise e
    return []


def build_context(hits, min_score=0.60):
    filtered = [h for h in hits if h.score >= min_score]

    context = ""
    sources = {}
    seen_urls = {}
    counter = 1

    for h in filtered:
        url = h.payload["source_url"]

        # deduplica URLs no índice de fontes
        if url not in seen_urls:
            seen_urls[url] = counter
            sources[counter] = url
            counter += 1

        source_num = seen_urls[url]
        published_at = h.payload.get("published_at") or "data desconhecida"
        context += f"[{source_num}] Fonte: {url} | Data: {published_at}\n"
        context += h.payload["text"] + "\n\n"

    return context, filtered, sources


def ask(query, history=None, top_k=15):
    if history is None:
        history = []

    # monta histórico antes de qualquer caminho
    history_text = ""
    for msg in history:
        role = "Estudante" if msg["role"] == "user" else "Assistente"
        history_text += f"{role}: {msg['content']}\n"

    # search e construção de contexto, usa apenas as últimas 2 perguntas para o search
    recent_questions = [msg for msg in history if msg["role"] == "user"][-2:]
    recent_history_text = ""
    for msg in recent_questions:
        recent_history_text += f"Estudante: {msg['content']}\n"

    if recent_history_text:
        search_query = f"Data atual: {data_atual}\n{recent_history_text}\nPergunta atual: {query}" if recent_history_text else f"Data atual: {data_atual}\n{query}"
    else:
        search_query = query

    # search no qdrant
    hits = search(search_query, top_k=top_k)
    context, filtered, sources = build_context(hits)

    print(f"Chunks encontrados: {len(filtered)}")

    # se nao achou chunks, fallback para pesquisa na internet
    if not filtered:
        response = google_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Você é um assistente do IFRS Campus Canoas.\n\nHistórico:\n{history_text}\n\nResponda a seguinte pergunta buscando na internet, priorizando fontes do IFRS: {query}",
            config={"tools": [{"google_search": {}}], "temperature": 0.7},
        )

        result = response.text

        try:
            chunks = response.candidates[0].grounding_metadata.grounding_chunks
            for c in chunks:
                if c.web:
                    result += "\n\n*Esta resposta foi obtida por busca na internet, pois não encontrei o conteúdo na base de documentos do campus.*"
                    break
        except Exception:
            pass

        return result

    # se achou, gera a query
    sources_text = "\n".join([f"[{i}] {url}" for i, url in sources.items()])
    prompt = agent_prompt.format(
        context=context,
        query=query,
        sources=sources_text,
        history=history_text,
        data_atual=data_atual
    )

    response = google_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"temperature": 0.7}
    )
    return response.text