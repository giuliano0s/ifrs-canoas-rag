import os
import sys
from dotenv import load_dotenv
from upstash_vector import Index
from google import genai as google_genai
from google.genai import types
import time
from datetime import datetime

load_dotenv()

# configurações
UPSTASH_ENDPOINT = os.getenv("UPSTASH_ENDPOINT")
UPSTASH_API_KEY = os.getenv("UPSTASH_API_KEY")
GEMINI_API_KEY_T1 = os.getenv("GEMINI_API_KEY_T1")
AGENT_NAME = "agente-ifrs"
ALPHA = 0.7 # score personalizado
MIN_YEAR = 2020
MIN_SCORE = 0.60 # score minimo do upstash para um chunk entrar no contexto
FETCH_K = 60 # chunks coletados do upstash por similaridade (pool do rerank). 60 (nao 30) para o
             # rerank por campus/data alcancar docs de Canoas que ficam alem do top-30 quando docs
             # institucionais do IFRS (ex: PDI multi-campus) dominam a similaridade crua da query.
CONTEXT_K = 15 # chunks que de fato vao ao contexto do modelo, apos o rerank
# escopo de campus: sem ancorar a query (o anchor "IFRS Campus Canoas" inflava docs institucionais
# e enterrava a resposta certa em queries de professor); o rerank penaliza docs de fora de Canoas
CAMPUS_PENALTY = 0.35 # penalidade no rerank para chunks fora de Canoas (metadata campus_scope="outro");
                      # 0.35 (nao 0.20) para excluir do contexto o institucional que domina o pool
                      # (o PDI vazava 1 chunk a 0.20 numa query de salas; a 0.30+ some)
MODEL = "gemini-2.5-flash"

# clientes
index = Index(url=UPSTASH_ENDPOINT, token=UPSTASH_API_KEY)
google_client = google_genai.Client(api_key=GEMINI_API_KEY_T1)

# carrega prompt do agente + carimbo de versao (hash do conteudo, igual ao do eval): identifica
# em cada registro/telemetria qual versao do prompt gerou aquela resposta
import hashlib
_prompt_path = os.path.join(os.path.dirname(__file__), f"../data/info/{AGENT_NAME}.txt")
with open(_prompt_path, "r", encoding="utf-8") as f:
    agent_prompt = f.read()
PROMPT_VERSAO = hashlib.sha256(agent_prompt.encode("utf-8")).hexdigest()[:12]

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
        # piso 0: um doc antigo (ano < MIN_YEAR) fica neutro, nunca com score NEGATIVO (que o
        # empurraria para baixo de forma artificial). recencia vira contribuicao em [0, 1].
        return max(0.0, (year - MIN_YEAR) / (max_year - MIN_YEAR))
    except (ValueError, TypeError):
        return 0.5


def _campus_penalty(hit):
    # docs institucionais do IFRS ou de outro campus (metadata campus_scope="outro", ex: o PDI
    # IFRS 2024-2028) sao despriorizados: a base e do Campus Canoas, entao conteudo de outro campus
    # nao deve responder como se fosse daqui. Ausencia de campus_scope = neutro (0), nao penaliza.
    return CAMPUS_PENALTY if (hit.metadata.get("campus_scope") == "outro") else 0.0

def rerank_by_date(hits):
    return sorted(
        hits,
        key=lambda h: ALPHA * h.score + (1 - ALPHA) * _date_score(h) - _campus_penalty(h),
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
                {"id": h.id, "url": h.metadata.get("source_url"), "score": h.score,
                 "rank_sim": rank_sim.get(h.id), "rank_rerank": i,
                 "tipo": h.metadata.get("type"), "no_contexto": h.id in filtered_ids}
                for i, h in enumerate(hits)
            ],
            "contexto_urls": [h.metadata.get("source_url") for h in filtered],
            "contexto_ids": [h.id for h in filtered],
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


def registro_de_trace(trace, query, resposta, erro=None):
    # registro canonico de uma execucao a partir do trace: o MESMO schema da coleta do eval
    # e da telemetria de producao (input, acao, buscas, resposta). quem chama acrescenta o
    # que e proprio (case_id/run no eval; stamp/session_id na producao).
    t = trace or {}
    return {
        "input": query,
        "erro": erro,
        "acao_real": t.get("acao"),
        "resposta": resposta,
        "buscas": t.get("buscas", []),
    }


def ask(query, history=None, max_steps=3, trace=None, data_atual=None):
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

    # data por request (nao no import): instancia serverless quente nao congela a data.
    # override opcional: o eval fixa a data de referencia nos casos temporais; em producao
    # vem None e usa a data real de hoje.
    data_atual = data_atual or datetime.now().strftime("%d/%m/%Y")

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

        # o modelo pediu busca: executa e devolve o contexto
        if trace is not None:
            trace["acao"] = "buscar"
        search_query = (fc.args or {}).get("query", query)
        context = _executar_busca(search_query, trace=trace)
        # sem resultado na base: informa o modelo e deixa ele responder honestamente que nao
        # encontrou (o prompt manda dizer isso); nao busca na internet nem inventa
        if context is None:
            context = "Nenhum documento relevante foi encontrado na base para esta consulta."

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
