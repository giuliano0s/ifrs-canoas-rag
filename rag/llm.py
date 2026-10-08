"""Camada de provedor de LLM para toda a GERAÇÃO do projeto: serving (agente + guards) e ingestão
(estruturadores de calendário, vagas, grade e planilha, inferência de data e leitura das grades
por visão).

Por que existe: os modelos Gemini 2.5 viraram legado (acesso restrito desde 09/2026, sem garantia de
continuidade), e os sucessores do Google custam mais que o GPT-5.6 Luna nesta carga, que é 99% tokens
de ENTRADA. Em vez de espalhar `if provedor == ...` pelo código, tudo fala UMA linguagem neutra
(mensagens, imagens e ferramentas descritas em dicionários) e cada provedor tem um adaptador que
traduz para o seu SDK. Trocar de provedor é mudar uma variável de ambiente; adicionar um terceiro é
escrever um adaptador.

ESCOPO DELIBERADO: só a GERAÇÃO troca de provedor. O EMBEDDING (`gemini-embedding-001`, 3072 dims,
desligamento anunciado para 14/05/2028) NUNCA troca aqui: os 18 mil chunks da base foram embeddados
com ele, e um espaço vetorial diferente invalidaria a base inteira (exigiria re-embed, recriar o
índice e recalibrar MIN_SCORE/ALPHA). Isso é migração de base, não troca de modelo.

Configuração por ambiente:
  LLM_PROVIDER      = "gemini" (default) | "openai"
  OPENAI_API_KEY    = chave da OpenAI (só necessária com LLM_PROVIDER=openai)
  OPENAI_MODEL      = "gpt-5.6-luna" (default; env permite corrigir o id sem tocar no código)
  OPENAI_REASONING  = "none" (default) | "low" | "medium" | "high". Com "none" a geração usa o
                      chat.completions com temperatura; com raciocínio ligado usa a API Responses, a
                      única que aceita ferramenta junto com raciocínio, e que não aceita temperatura.
  GEMINI_MODEL      = "gemini-2.5-flash" (default; agente, guards e visão)
  GEMINI_MODEL_LEVE = "gemini-2.5-flash-lite" (default; estruturadores e data, chamados com leve=True)
"""

import base64
import json
import os
import re
import time
from collections import namedtuple

from dotenv import load_dotenv

load_dotenv()

# variavel vazia (como vem do .env.example) cai no default, igual a variavel ausente
PROVIDER = (os.getenv("LLM_PROVIDER") or "gemini").strip().lower()
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
GEMINI_MODEL_LEVE = os.getenv("GEMINI_MODEL_LEVE") or "gemini-2.5-flash-lite"
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-5.6-luna"
OPENAI_REASONING = (os.getenv("OPENAI_REASONING") or "none").strip().lower()
# prazo por chamada da ingestao (completar), em segundos: um calendario estruturado leva de 30 a 55 s
# no luna, e o prazo de 20 s do cliente, feito para o aluno nao esperar, o derrubaria
PRAZO_INGESTAO = 180

# resposta neutra: ou o modelo produziu texto, ou pediu uma ferramenta (nunca os dois no fluxo atual)
Resposta = namedtuple("Resposta", "texto chamada")

# ferramenta descrita de forma neutra (JSON Schema), traduzida por cada adaptador
FERRAMENTA_BUSCA = {
    "nome": "buscar_documentos",
    "descricao": ("Busca na base de documentos do IFRS Campus Canoas (paginas e PDFs do site). "
                  "Passe uma frase curta e natural com o assunto e o discriminador certo (curso, tipo "
                  "de prova, etc), com nomes por extenso como a instituicao os escreve, sem 'IFRS' "
                  "nem 'Campus Canoas', salvo em perguntas sobre o campus como um todo."),
    "parametros": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Consulta refinada para a busca vetorial, no vocabulario dos documentos do campus.",
            }
        },
        "required": ["query"],
    },
}

# FORMATO NEUTRO DE MENSAGEM (lista, na ordem da conversa):
#   {"papel": "usuario",    "texto": "...", "imagens": [png_bytes, ...]}  -> imagens opcionais
#   {"papel": "modelo",     "texto": "..."}                      -> turno de texto do modelo
#   {"papel": "modelo",     "chamada": {"nome": ..., "args": {...}}}  -> modelo pediu ferramenta
#   {"papel": "ferramenta", "nome": ..., "resultado": "..."}      -> devolucao da ferramenta


def _data_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# ── adaptador Gemini ────────────────────────────────────────────────────────────
def _gerar_gemini(mensagens, sistema, ferramentas, temperatura, leve, prazo=None):
    from google.genai import types
    cliente = _cliente_gemini()

    contents = []
    for m in mensagens:
        if m["papel"] == "usuario":
            imagens = [types.Part.from_bytes(data=png, mime_type="image/png") for png in m.get("imagens", [])]
            contents.append(types.Content(role="user", parts=imagens + [types.Part(text=m["texto"])]))
        elif m["papel"] == "modelo" and m.get("chamada"):
            contents.append(types.Content(role="model", parts=[types.Part(
                function_call=types.FunctionCall(name=m["chamada"]["nome"], args=m["chamada"]["args"]))]))
        elif m["papel"] == "modelo":
            contents.append(types.Content(role="model", parts=[types.Part(text=m.get("texto") or "")]))
        elif m["papel"] == "ferramenta":
            contents.append(types.Content(role="user", parts=[types.Part.from_function_response(
                name=m["nome"], response={"documentos": m["resultado"]})]))

    cfg = {"temperature": temperatura}
    if sistema:
        cfg["system_instruction"] = sistema
    if ferramentas:
        cfg["tools"] = [types.Tool(function_declarations=[
            types.FunctionDeclaration(name=f["nome"], description=f["descricao"],
                                      parameters=_schema_gemini(f["parametros"]))
            for f in ferramentas])]
        cfg["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)

    r = cliente.models.generate_content(model=GEMINI_MODEL_LEVE if leve else GEMINI_MODEL, contents=contents,
                                        config=types.GenerateContentConfig(**cfg))
    for parte in (r.candidates[0].content.parts or []):
        fc = getattr(parte, "function_call", None)
        if fc:
            return Resposta(texto=None, chamada={"nome": fc.name, "args": dict(fc.args or {})})
    return Resposta(texto=(r.text or "").strip(), chamada=None)


def _schema_gemini(js):
    # JSON Schema neutro -> types.Schema do Gemini (só os tipos que a ferramenta usa)
    from google.genai import types
    props = {k: types.Schema(type=v.get("type", "string").upper(), description=v.get("description"))
             for k, v in (js.get("properties") or {}).items()}
    return types.Schema(type=js.get("type", "object").upper(), properties=props,
                        required=js.get("required") or [])


_CLIENTE_GEMINI = None
def _cliente_gemini():
    global _CLIENTE_GEMINI
    if _CLIENTE_GEMINI is None:
        from google import genai as google_genai
        _CLIENTE_GEMINI = google_genai.Client(api_key=os.getenv("GEMINI_API_KEY_T1"))
    return _CLIENTE_GEMINI


# ── adaptador OpenAI ────────────────────────────────────────────────────────────
def _gerar_openai(mensagens, sistema, ferramentas, temperatura, leve, prazo=None):
    # sem raciocinio: chat.completions com temperatura; com raciocinio: API Responses.
    # o luna ja e o modelo barato, entao leve nao muda o modelo aqui
    if OPENAI_REASONING == "none":
        return _gerar_openai_chat(mensagens, sistema, ferramentas, temperatura, prazo)
    return _gerar_openai_responses(mensagens, sistema, ferramentas, prazo)


def _gerar_openai_chat(mensagens, sistema, ferramentas, temperatura, prazo=None):
    cliente = _cliente_openai()

    msgs = []
    if sistema:
        msgs.append({"role": "system", "content": sistema})
    # ids de tool_call: a OpenAI exige casar a chamada com a devolucao por id, e o Gemini nao usa id.
    # Como a conversa e reconstruida a cada turno, ids deterministicos por posicao bastam.
    n_call = 0
    for m in mensagens:
        if m["papel"] == "usuario" and m.get("imagens"):
            msgs.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": _data_url(png)}}
                                                     for png in m["imagens"]] + [{"type": "text", "text": m["texto"]}]})
        elif m["papel"] == "usuario":
            msgs.append({"role": "user", "content": m["texto"]})
        elif m["papel"] == "modelo" and m.get("chamada"):
            msgs.append({"role": "assistant", "tool_calls": [{
                "id": f"call_{n_call}", "type": "function",
                "function": {"name": m["chamada"]["nome"],
                             "arguments": json.dumps(m["chamada"]["args"], ensure_ascii=False)}}]})
            n_call += 1
        elif m["papel"] == "modelo":
            msgs.append({"role": "assistant", "content": m.get("texto") or ""})
        elif m["papel"] == "ferramenta":
            msgs.append({"role": "tool", "tool_call_id": f"call_{max(n_call - 1, 0)}",
                         "content": m["resultado"]})

    # sem raciocinio: no chat.completions o luna so aceita temperature e tools com reasoning_effort "none"
    kwargs = {"model": OPENAI_MODEL, "messages": msgs, "temperature": temperatura, "reasoning_effort": "none"}
    if ferramentas:
        kwargs["tools"] = [{"type": "function", "function": {
            "name": f["nome"], "description": f["descricao"], "parameters": f["parametros"]}}
            for f in ferramentas]

    if prazo:
        kwargs["timeout"] = prazo
    r = cliente.chat.completions.create(**kwargs)
    escolha = r.choices[0].message
    chamadas = getattr(escolha, "tool_calls", None)
    if chamadas:
        c = chamadas[0]
        args = c.function.arguments
        return Resposta(texto=None, chamada={"nome": c.function.name,
                                             "args": json.loads(args) if isinstance(args, str) else (args or {})})
    return Resposta(texto=(escolha.content or "").strip(), chamada=None)


def _gerar_openai_responses(mensagens, sistema, ferramentas, prazo=None):
    cliente = _cliente_openai()

    # conversa como itens da API Responses; chamada e devolucao casam pelo call_id posicional
    itens = []
    n_call = 0
    for m in mensagens:
        if m["papel"] == "usuario" and m.get("imagens"):
            itens.append({"role": "user", "content": [{"type": "input_image", "image_url": _data_url(png)}
                                                      for png in m["imagens"]] + [{"type": "input_text", "text": m["texto"]}]})
        elif m["papel"] == "usuario":
            itens.append({"role": "user", "content": m["texto"]})
        elif m["papel"] == "modelo" and m.get("chamada"):
            itens.append({"type": "function_call", "call_id": f"call_{n_call}", "name": m["chamada"]["nome"],
                          "arguments": json.dumps(m["chamada"]["args"], ensure_ascii=False)})
            n_call += 1
        elif m["papel"] == "modelo":
            itens.append({"role": "assistant", "content": m.get("texto") or ""})
        elif m["papel"] == "ferramenta":
            itens.append({"type": "function_call_output", "call_id": f"call_{max(n_call - 1, 0)}",
                          "output": m["resultado"]})

    # sem temperature (recusada com raciocinio ligado) e sem guardar a conversa na OpenAI
    kwargs = {"model": OPENAI_MODEL, "input": itens, "reasoning": {"effort": OPENAI_REASONING}, "store": False}
    if sistema:
        kwargs["instructions"] = sistema
    if ferramentas:
        kwargs["tools"] = [{"type": "function", "name": f["nome"], "description": f["descricao"],
                            "parameters": f["parametros"]} for f in ferramentas]

    if prazo:
        kwargs["timeout"] = prazo
    r = cliente.responses.create(**kwargs)
    for item in r.output:
        if item.type == "function_call":
            return Resposta(texto=None, chamada={"nome": item.name, "args": json.loads(item.arguments or "{}")})
    return Resposta(texto=(r.output_text or "").strip(), chamada=None)


_CLIENTE_OPENAI = None
def _cliente_openai():
    global _CLIENTE_OPENAI
    if _CLIENTE_OPENAI is None:
        chave = os.getenv("OPENAI_API_KEY")
        if not chave:
            raise RuntimeError("LLM_PROVIDER=openai exige OPENAI_API_KEY no ambiente (.env ou Vercel)")
        from openai import OpenAI
        # sem novas tentativas no SDK: a recusa por limite (429) tem que chegar na hora ao chamador,
        # para o aluno saber que a espera e fluxo alto; queda e 5xx sao refeitos em gerar()
        _CLIENTE_OPENAI = OpenAI(api_key=chave, max_retries=0, timeout=20)
    return _CLIENTE_OPENAI


class ProvedorIndisponivel(Exception):
    """O provedor recusou ou não respondeu: limite por minuto (motivo "fluxo_alto", com a espera
    sugerida em `tentar_em` segundos), crédito esgotado, queda ou timeout. O chamador avisa o aluno
    e pede para tentar de novo, sem trocar de provedor."""

    def __init__(self, mensagem, motivo="indisponivel", tentar_em=None):
        super().__init__(mensagem)
        self.motivo = motivo
        self.tentar_em = tentar_em


def _espera_sugerida(erro):
    # segundos ate o limite liberar, pelos cabecalhos da recusa ("retry-after", ou "1.9s"/"120ms"/"6m0s")
    cabecalhos = getattr(getattr(erro, "response", None), "headers", None) or {}
    for nome in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        valor = cabecalhos.get(nome)
        if not valor:
            continue
        try:
            return float(valor)
        except ValueError:
            partes = re.findall(r"([\d.]+)(ms|m|s)", valor)
            if partes:
                return sum(float(n) * {"ms": 0.001, "s": 1, "m": 60}[u] for n, u in partes)
    return None


def _classificar(erro):
    # (motivo, tentar_em) do erro transitorio do provedor; None quando o erro nao e do provedor
    try:
        import openai
        if isinstance(erro, openai.RateLimitError):
            if getattr(erro, "code", None) == "insufficient_quota":
                return "credito", None
            return "fluxo_alto", _espera_sugerida(erro)
        if isinstance(erro, (openai.APIConnectionError, openai.InternalServerError)):
            return "indisponivel", None
    except ImportError:
        pass
    try:
        from google.genai import errors as genai_errors
        if isinstance(erro, genai_errors.APIError):
            if getattr(erro, "code", None) == 429:
                return "fluxo_alto", None
            if getattr(erro, "code", None) in (500, 502, 503, 504):
                return "indisponivel", None
    except ImportError:
        pass
    return None


# ── entradas usadas pelo serving e pela ingestão ────────────────────────────────
_ADAPTADORES = {"gemini": _gerar_gemini, "openai": _gerar_openai}

def gerar(mensagens, sistema=None, ferramentas=None, temperatura=0.7, leve=False, prazo=None):
    # ponto único de geração: devolve Resposta(texto, chamada) qualquer que seja o provedor.
    # leve=True pede o modelo barato do provedor (estruturadores e inferência de data da ingestão);
    # prazo troca o tempo maximo da chamada (o cliente da OpenAI usa 20 s, o tempo do atendimento)
    adaptador = _ADAPTADORES.get(PROVIDER)
    if adaptador is None:
        raise RuntimeError(f"LLM_PROVIDER desconhecido: {PROVIDER!r} (use 'gemini' ou 'openai')")
    # queda e 5xx sao refeitos aqui (2 novas tentativas, 1s e 2s); limite e credito sobem na hora
    for tentativa in range(3):
        try:
            return adaptador(mensagens, sistema, ferramentas, temperatura, leve, prazo)
        except Exception as e:
            classe = _classificar(e)
            if classe is None:
                raise
            motivo, tentar_em = classe
            if motivo == "indisponivel" and tentativa < 2:
                time.sleep(tentativa + 1)
                continue
            raise ProvedorIndisponivel(f"{type(e).__name__}: {str(e)[:200]}", motivo, tentar_em) from e


def completar(prompt, temperatura, leve=False, imagens=None):
    # prompt único, sem conversa nem ferramenta, devolvendo só o texto (uso da ingestão)
    mensagem = {"papel": "usuario", "texto": prompt}
    if imagens:
        mensagem["imagens"] = imagens
    return gerar([mensagem], temperatura=temperatura, leve=leve, prazo=PRAZO_INGESTAO).texto or ""


def modelo_ativo():
    # identidade do modelo em uso, para carimbar trace/telemetria e o relatório do eval
    if PROVIDER == "gemini":
        return GEMINI_MODEL
    return OPENAI_MODEL if OPENAI_REASONING == "none" else f"{OPENAI_MODEL}+raciocinio-{OPENAI_REASONING}"
