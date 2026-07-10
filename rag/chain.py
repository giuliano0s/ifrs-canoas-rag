import os
import sys
from dotenv import load_dotenv
from upstash_vector import Index
from google import genai as google_genai
from google.genai import types
import time
from datetime import datetime

data_atual = datetime.now().strftime("%d/%m/%Y")

load_dotenv()

# configurações
UPSTASH_ENDPOINT = os.getenv("UPSTASH_ENDPOINT")
UPSTASH_API_KEY = os.getenv("UPSTASH_API_KEY")
GEMINI_API_KEY_T1 = os.getenv("GEMINI_API_KEY_T1")
AGENT_NAME = "agente-ifrs"
ALPHA = 0.7 # score personalizado
MIN_YEAR = 2020
MIN_SCORE = 0.60 # score minimo do upstash para um chunk entrar no contexto
FETCH_K = 30 # chunks coletados do upstash por similaridade (pool do rerank)
CONTEXT_K = 15 # chunks que de fato vao ao contexto do modelo, apos o rerank
MODEL = "gemini-2.5-flash"

# clientes
index = Index(url=UPSTASH_ENDPOINT, token=UPSTASH_API_KEY)
google_client = google_genai.Client(api_key=GEMINI_API_KEY_T1)

# carrega prompt do agente
_prompt_path = os.path.join(os.path.dirname(__file__), f"../data/info/{AGENT_NAME}.txt")
with open(_prompt_path, "r", encoding="utf-8") as f:
    agent_prompt = f.read()

# lista de cursos atuais, injetada no prompt: o agente corrige "curso inexistente" -> curso real
import json
_cursos_path = os.path.join(os.path.dirname(__file__), "../data/info/cursos_atuais.json")
try:
    with open(_cursos_path, "r", encoding="utf-8") as f:
        cursos_atuais = ", ".join(json.load(f).get("cursos", []))
except Exception:
    cursos_atuais = "(lista indisponivel)"


def _safe(s):
    # protege os prints de log contra caracteres fora do encoding do console (evita crash no Windows)
    enc = sys.stdout.encoding or "utf-8"
    return str(s).encode(enc, errors="replace").decode(enc)


# ferramenta de busca exposta ao modelo; ele decide quando e com qual query chamar
buscar_documentos_tool = types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name="buscar_documentos",
        description=(
            "Busca na base de documentos do IFRS Campus Canoas (paginas e PDFs do site). "
            "Passe uma query especifica, no vocabulario da instituicao, com o discriminador "
            "certo (curso, tipo de prova, etc)."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "query": types.Schema(
                    type="STRING",
                    description="Consulta refinada para a busca vetorial, no vocabulario dos documentos do campus.",
                )
            },
            required=["query"],
        ),
    )
])


def search(query, top_k):
    for attempt in range(3):
        try:
            result = google_client.models.embed_content(
                model="gemini-embedding-001",
                contents=query
            )
            vector = result.embeddings[0].values
            hits = index.query(
                vector=vector,
                top_k=top_k,
                include_metadata=True,
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


def build_context(hits, min_score=MIN_SCORE, top_n=CONTEXT_K):
    filtered = [h for h in hits if h.score >= min_score][:top_n]

    context = ""
    sources = {}
    seen_urls = {}
    counter = 1

    for h in filtered:
        url = h.metadata["source_url"]

        # deduplica URLs no índice de fontes
        if url not in seen_urls:
            seen_urls[url] = counter
            sources[counter] = url
            counter += 1

        source_num = seen_urls[url]
        published_at = h.metadata.get("published_at") or "data desconhecida"
        context += f"[{source_num}] Fonte: {url} | Data: {published_at}\n"
        context += h.metadata["text"] + "\n\n"

    return context, filtered, sources


def _date_score(hit):
    max_year = datetime.now().year
    raw = hit.metadata.get("published_at")
    if not raw:
        return 0.5
    try:
        year = int(raw)
        return (year - MIN_YEAR) / (max_year - MIN_YEAR)
    except (ValueError, TypeError):
        return 0.5


def rerank_by_date(hits):
    return sorted(
        hits,
        key=lambda h: ALPHA * h.score + (1 - ALPHA) * _date_score(h),
        reverse=True
    )


def _executar_busca(search_query, trace=None):
    # coleta um pool grande por similaridade, reordena por data e corta para o contexto
    hits = search(search_query, top_k=FETCH_K)
    rank_sim = {h.id: i for i, h in enumerate(hits)}  # posicao por similaridade, antes do rerank
    hits = rerank_by_date(hits)
    context, filtered, sources = build_context(hits)

    # log de depuracao: pool coletado, reordenado, e quais chunks entraram no contexto final
    filtered_ids = {h.id for h in filtered}

    # captura estruturada da busca para o trace (eval/telemetria), quando solicitado
    if trace is not None:
        trace.setdefault("buscas", []).append({
            "query": search_query,
            "hits": [
                {"url": h.metadata.get("source_url"), "score": h.score,
                 "rank_sim": rank_sim.get(h.id), "rank_rerank": i,
                 "tipo": h.metadata.get("type"), "no_contexto": h.id in filtered_ids}
                for i, h in enumerate(hits)
            ],
            "contexto_urls": [h.metadata.get("source_url") for h in filtered],
            "chunks_textos": [h.metadata.get("text", "") for h in filtered],
            "sources": dict(sources),
        })
    print("\n" + "="*80)
    print(_safe(f"[RETRIEVAL] query formulada para o Upstash: {search_query}"))
    print(f"[RETRIEVAL] coletados={len(hits)} | no contexto={len(filtered)} (min_score={MIN_SCORE}, teto={CONTEXT_K})")
    for h in hits:
        m = h.metadata
        rerank_score = ALPHA * h.score + (1 - ALPHA) * _date_score(h)
        if h.id in filtered_ids:
            marca = "CONTEXTO "
        elif h.score < MIN_SCORE:
            marca = "score<min"
        else:
            marca = "cortado  "
        trecho = (m.get("text") or "").replace("\n", " ")[:100]
        print(_safe(f"  [{marca}] score={h.score:.3f} rerank={rerank_score:.3f} data={m.get('published_at')} tipo={m.get('type')}"))
        print(_safe(f"            titulo={m.get('title')}"))
        print(_safe(f"            url={m.get('source_url')}"))
        print(_safe(f"            texto={trecho}"))
    print("="*80 + "\n")

    if not filtered:
        return None
    sources_text = "\n".join([f"[{i}] {url}" for i, url in sources.items()])
    return f"{context}\nFONTES:\n{sources_text}"


def _history_text(history):
    # serializa o histórico para os prompts em texto (fallback)
    text = ""
    for msg in history:
        role = "Estudante" if msg["role"] == "user" else "Assistente"
        text += f"{role}: {msg['content']}\n"
    return text


def _fallback_internet(query, history_text):
    # base sem resultado: responde buscando na internet, priorizando o ifrs
    response = google_client.models.generate_content(
        model=MODEL,
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


def ask(query, history=None, max_steps=3, trace=None):
    history = history or []

    # inicializa o trace opcional (eval/telemetria); em producao trace=None e nada muda
    if trace is not None:
        trace.update({"input": query, "acao": "nao_buscar", "buscas": [], "resposta": None})

    # monta a conversa como turnos (historico + pergunta atual) para o tool calling
    contents = []
    for msg in history:
        role = "user" if msg["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part(text=query)]))

    # config com o prompt do agente como system_instruction e a ferramenta de busca
    config = types.GenerateContentConfig(
        system_instruction=agent_prompt.format(data_atual=data_atual, cursos=cursos_atuais),
        tools=[buscar_documentos_tool],
        temperature=0.7,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    # loop de investigacao: o modelo pergunta, busca ou responde ate produzir texto
    for _ in range(max_steps):
        response = google_client.models.generate_content(model=MODEL, contents=contents, config=config)
        cand = response.candidates[0].content

        fc = None
        for part in (cand.parts or []):
            if getattr(part, "function_call", None):
                fc = part.function_call
                break

        # sem chamada de ferramenta: e uma pergunta de clarificacao ou resposta final
        if not fc:
            resposta = (response.text or "").strip()
            if trace is not None:
                trace["resposta"] = resposta
            return resposta

        # o modelo pediu busca: executa e devolve o contexto, ou cai no fallback se vazio
        if trace is not None:
            trace["acao"] = "buscar"
        search_query = (fc.args or {}).get("query", query)
        context = _executar_busca(search_query, trace=trace)
        if context is None:
            resposta = _fallback_internet(query, _history_text(history))
            if trace is not None:
                trace["resposta"] = resposta
                trace["fallback_internet"] = True
            return resposta

        contents.append(cand)
        contents.append(types.Content(role="user", parts=[
            types.Part.from_function_response(name=fc.name, response={"documentos": context})
        ]))

    # esgotou os passos: forca uma resposta final em texto
    response = google_client.models.generate_content(model=MODEL, contents=contents, config=config)
    resposta = (response.text or "").strip()
    if trace is not None:
        trace["resposta"] = resposta
    return resposta
