"""Camada de provedor de LLM para toda a GERAÇÃO do projeto: serving (agente + guards) e ingestão
(estruturadores de calendário, vagas, grade e planilha, inferência de data e leitura das grades
por visão).

Por que existe: os modelos Gemini 2.5 viraram legado (acesso restrito desde 09/2026, sem garantia de
continuidade), e os sucessores do Google custam mais que o Luna da OpenAI nesta carga, que é 99% tokens
de ENTRADA. Em vez de espalhar `if provedor == ...` pelo código, tudo fala UMA linguagem neutra
(mensagens, imagens e ferramentas descritas em dicionários) e cada provedor tem um adaptador que
traduz para o seu SDK. Adicionar um terceiro provedor é escrever um adaptador.

O modelo de cada uso (atendimento, atendimento com raciocínio, ingestão) mora na tabela PERFIS, no
código: trocar de modelo é editar a tabela, e o Vercel e o GitHub Actions seguem o código, sem
variável de ambiente. Sem raciocínio, a geração da OpenAI usa o chat.completions com temperatura; com
raciocínio, a API Responses, a única que aceita ferramenta junto com raciocínio, e que não aceita
temperatura. Cada chamada devolve o próprio registro de uso (perfil, tokens, tempo, custo e, quando o
prazo do raciocínio estoura, a reserva usada), consumido pela telemetria e pelo eval.

ESCOPO DELIBERADO: só a GERAÇÃO troca de provedor. O EMBEDDING (`gemini-embedding-001`, 3072 dims,
desligamento anunciado para 14/05/2028) NUNCA troca aqui: os 18 mil chunks da base foram embeddados
com ele, e um espaço vetorial diferente invalidaria a base inteira (exigiria re-embed, recriar o
índice e recalibrar MIN_SCORE/ALPHA). Isso é migração de base, não troca de modelo.

Ambiente: só as chaves (OPENAI_API_KEY, GEMINI_API_KEY_T1).
"""

import base64
import json
import os
import re
import time
from collections import namedtuple

from dotenv import load_dotenv

load_dotenv()

# perfil de geracao: provedor, modelo e esforco de raciocinio ("none" desliga); perfil com raciocinio
# leva o prazo em segundos de silencio do fluxo antes da primeira saida (texto ou pedido de ferramenta;
# o corte cai ~1 s depois dele) e o perfil de reserva que refaz a chamada quando o prazo estoura
Perfil = namedtuple("Perfil", "provedor modelo raciocinio prazo_saida reserva", defaults=(None, None))

# modelo de cada uso, fixado no codigo; as medicoes que sustentam cada linha estao no CLAUDE.md
# ("Achados atuais da bateria")
PERFIS = {
    # agente (decisao de buscar e resposta) e guards do atendimento
    "atendimento": Perfil("openai", "gpt-6-luna", "none"),
    # resposta do atendimento quando o contexto traz calendario, grade ou evento datado (regra em
    # rag/chain.py). sem raciocinio: o medium com prazo de 5 s e reserva, Perfil("openai", "gpt-6-luna",
    # "medium", prazo_saida=5, reserva="atendimento"), reprovou no A/B de 22/10/2026 pelo tempo maximo
    # (21,2 s em 3 buscas seguidas). a regra segue registrada na telemetria e no eval
    "atendimento_raciocinio": Perfil("openai", "gpt-6-luna", "none"),
    # estruturadores, inferencia de data e visao das grades da ingestao
    "ingestao": Perfil("openai", "gpt-5.6-luna", "none"),
}
# modelo do Gemini para as chamadas leves (leve=True) quando um perfil usa o Gemini
GEMINI_MODEL_LEVE = "gemini-2.5-flash-lite"

# precos por milhao de tokens: entrada, entrada lida do cache, escrita de cache (30 min) e saida, da
# pagina de precos da OpenAI em 21/10/2026; tokens de raciocinio sao cobrados como saida
PRECOS = {"gpt-6-luna": (0.10, 0.01, 0.125, 0.50), "gpt-5.6-luna": (0.20, 0.02, 0.25, 1.20)}

# prazo por chamada da ingestao (completar), em segundos: um calendario estruturado leva de 30 a 55 s
# no luna, e o prazo de 20 s do cliente, feito para o aluno nao esperar, o derrubaria
PRAZO_INGESTAO = 180

# resposta neutra: ou o modelo produziu texto, ou pediu uma ferramenta (nunca os dois no fluxo atual);
# uso = registro da chamada: tokens ("entrada", "saida", "cached", "cache_write", "raciocinio", quando
# o provedor informa), "perfil", "modelo", "esforco", "ms", "custo_usd" e, se o prazo do raciocinio
# estourou, "reserva_de" (perfil pedido) e "corte_ms"
Resposta = namedtuple("Resposta", "texto chamada uso", defaults=(None,))

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
def _gerar_gemini(mensagens, sistema, ferramentas, temperatura, leve, perfil, prazo=None):
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

    r = cliente.models.generate_content(model=GEMINI_MODEL_LEVE if leve else perfil.modelo, contents=contents,
                                        config=types.GenerateContentConfig(**cfg))
    meta = getattr(r, "usage_metadata", None)
    uso = {"entrada": getattr(meta, "prompt_token_count", 0) or 0,
           "saida": getattr(meta, "candidates_token_count", 0) or 0} if meta else None
    for parte in (r.candidates[0].content.parts or []):
        fc = getattr(parte, "function_call", None)
        if fc:
            return Resposta(texto=None, chamada={"nome": fc.name, "args": dict(fc.args or {})}, uso=uso)
    return Resposta(texto=(r.text or "").strip(), chamada=None, uso=uso)


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
def _gerar_openai(mensagens, sistema, ferramentas, temperatura, leve, perfil, prazo=None):
    # sem raciocinio: chat.completions com temperatura; com raciocinio: API Responses.
    # o luna ja e o modelo barato, entao leve nao muda o modelo aqui
    if perfil.raciocinio == "none":
        return _gerar_openai_chat(mensagens, sistema, ferramentas, temperatura, perfil, prazo)
    return _gerar_openai_responses(mensagens, sistema, ferramentas, perfil, prazo)


def _uso_openai(usage):
    # tokens informados pela OpenAI: entrada (com a parte lida e a gravada no cache) e saida (com a de
    # raciocinio); None quando a resposta nao traz
    if not usage:
        return None
    entrada = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", 0) or 0
    saida = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", 0) or 0
    det_entrada = getattr(usage, "prompt_tokens_details", None) or getattr(usage, "input_tokens_details", None)
    det_saida = getattr(usage, "completion_tokens_details", None) or getattr(usage, "output_tokens_details", None)
    return {"entrada": entrada, "saida": saida,
            "cached": getattr(det_entrada, "cached_tokens", 0) or 0,
            "cache_write": getattr(det_entrada, "cache_write_tokens", 0) or 0,
            "raciocinio": getattr(det_saida, "reasoning_tokens", 0) or 0}


def _kwargs_openai_chat(mensagens, sistema, ferramentas, temperatura, perfil, prazo=None):
    # pedido do chat.completions no formato da OpenAI, a partir da conversa neutra
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
    kwargs = {"model": perfil.modelo, "messages": msgs, "temperature": temperatura, "reasoning_effort": "none"}
    if ferramentas:
        kwargs["tools"] = [{"type": "function", "function": {
            "name": f["nome"], "description": f["descricao"], "parameters": f["parametros"]}}
            for f in ferramentas]

    if prazo:
        kwargs["timeout"] = prazo
    return kwargs


def _gerar_openai_chat(mensagens, sistema, ferramentas, temperatura, perfil, prazo=None):
    r = _cliente_openai().chat.completions.create(**_kwargs_openai_chat(mensagens, sistema, ferramentas,
                                                                         temperatura, perfil, prazo))
    escolha = r.choices[0].message
    uso = _uso_openai(r.usage)
    chamadas = getattr(escolha, "tool_calls", None)
    if chamadas:
        c = chamadas[0]
        args = c.function.arguments
        return Resposta(texto=None, chamada={"nome": c.function.name,
                                             "args": json.loads(args) if isinstance(args, str) else (args or {})}, uso=uso)
    return Resposta(texto=(escolha.content or "").strip(), chamada=None, uso=uso)


def _fluxo_openai_chat(mensagens, sistema, ferramentas, temperatura, perfil):
    # mesma chamada em streaming: devolve ("texto", pedaco) a cada pedaco e ("fim", Resposta) no final;
    # a chamada de ferramenta chega em pedacos de argumento, montados por indice
    kwargs = _kwargs_openai_chat(mensagens, sistema, ferramentas, temperatura, perfil)
    kwargs.update(stream=True, stream_options={"include_usage": True})
    texto, chamadas, uso = [], {}, None
    for parte in _cliente_openai().chat.completions.create(**kwargs):
        if getattr(parte, "usage", None):
            uso = _uso_openai(parte.usage)
        if not parte.choices:
            continue
        delta = parte.choices[0].delta
        if delta.content:
            texto.append(delta.content)
            yield "texto", delta.content
        for tc in delta.tool_calls or []:
            atual = chamadas.setdefault(tc.index, {"nome": "", "args": ""})
            if tc.function and tc.function.name:
                atual["nome"] += tc.function.name
            if tc.function and tc.function.arguments:
                atual["args"] += tc.function.arguments
    if chamadas:
        c = chamadas[min(chamadas)]
        yield "fim", Resposta(texto=None, chamada={"nome": c["nome"], "args": json.loads(c["args"] or "{}")}, uso=uso)
    else:
        yield "fim", Resposta(texto="".join(texto).strip(), chamada=None, uso=uso)


def _kwargs_openai_responses(mensagens, sistema, ferramentas, perfil, prazo=None):
    # pedido da API Responses a partir da conversa neutra; chamada e devolucao casam pelo call_id posicional
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
    kwargs = {"model": perfil.modelo, "input": itens, "reasoning": {"effort": perfil.raciocinio}, "store": False}
    if sistema:
        kwargs["instructions"] = sistema
    if ferramentas:
        kwargs["tools"] = [{"type": "function", "name": f["nome"], "description": f["descricao"],
                            "parameters": f["parametros"]} for f in ferramentas]

    if prazo:
        kwargs["timeout"] = prazo
    return kwargs


def _resposta_openai_responses(r):
    # Resposta neutra a partir da resposta completa da API Responses
    uso = _uso_openai(getattr(r, "usage", None))
    for item in r.output:
        if item.type == "function_call":
            return Resposta(texto=None, chamada={"nome": item.name, "args": json.loads(item.arguments or "{}")}, uso=uso)
    return Resposta(texto=(r.output_text or "").strip(), chamada=None, uso=uso)


def _gerar_openai_responses(mensagens, sistema, ferramentas, perfil, prazo=None):
    r = _cliente_openai().responses.create(**_kwargs_openai_responses(mensagens, sistema, ferramentas, perfil, prazo))
    return _resposta_openai_responses(r)


# motivo do ProvedorIndisponivel para o codigo de erro que a API Responses manda no proprio fluxo
_MOTIVO_ERRO_FLUXO = {"rate_limit_exceeded": "fluxo_alto", "insufficient_quota": "credito"}


def _e_saida(evento):
    # primeiro sinal de saida da API Responses: texto ou item de mensagem/ferramenta (o de raciocinio nao conta)
    if evento.type == "response.output_text.delta":
        return True
    return evento.type == "response.output_item.added" and getattr(evento.item, "type", None) in ("message", "function_call")


def _fluxo_openai_responses(mensagens, sistema, ferramentas, perfil):
    # mesma chamada em streaming: devolve ("texto", pedaco) a cada pedaco e ("fim", Resposta) no final,
    # montada da resposta completa que o ultimo evento do fluxo traz (a mesma do pedido sem streaming).
    # com prazo_saida, o pedido leva esse prazo de leitura do cliente HTTP: durante o raciocinio a API nao
    # manda nenhum byte (medido em 22/10/2026), entao a leitura estoura no prazo contado do ultimo evento
    # (~1 s depois do pedido) e a chamada sobe como PrazoEsgotado antes de qualquer texto
    import httpx
    import openai
    kwargs = _kwargs_openai_responses(mensagens, sistema, ferramentas, perfil)
    kwargs["stream"] = True
    if perfil.prazo_saida:
        kwargs["timeout"] = httpx.Timeout(perfil.prazo_saida, connect=5.0)
    saiu = False

    # leitura do fluxo; estouro de prazo antes da primeira saida vira PrazoEsgotado, depois dela e queda
    try:
        for evento in _cliente_openai().responses.create(**kwargs):
            saiu = saiu or _e_saida(evento)
            if evento.type == "response.output_text.delta":
                yield "texto", evento.delta
            elif evento.type in ("response.completed", "response.incomplete"):
                yield "fim", _resposta_openai_responses(evento.response)
                return
            elif evento.type in ("response.failed", "error"):
                erro = getattr(evento.response, "error", None) if evento.type == "response.failed" else evento
                codigo = getattr(erro, "code", None)
                raise ProvedorIndisponivel(f"API Responses: {codigo}: {str(getattr(erro, 'message', ''))[:200]}",
                                           _MOTIVO_ERRO_FLUXO.get(codigo, "indisponivel"))
    except (httpx.TimeoutException, openai.APITimeoutError) as e:
        if perfil.prazo_saida and not saiu:
            raise PrazoEsgotado(f"{perfil.modelo} sem saida em {perfil.prazo_saida} s") from e
        raise ProvedorIndisponivel(f"API Responses: {type(e).__name__} no meio do fluxo") from e
    raise ProvedorIndisponivel("API Responses: fluxo encerrado sem a resposta final")


def _fluxo_openai(mensagens, sistema, ferramentas, temperatura, perfil):
    # streaming: chat.completions sem raciocinio; com raciocinio, a API Responses
    if perfil.raciocinio == "none":
        return _fluxo_openai_chat(mensagens, sistema, ferramentas, temperatura, perfil)
    return _fluxo_openai_responses(mensagens, sistema, ferramentas, perfil)


_CLIENTE_OPENAI = None
def _cliente_openai():
    global _CLIENTE_OPENAI
    if _CLIENTE_OPENAI is None:
        chave = os.getenv("OPENAI_API_KEY")
        if not chave:
            raise RuntimeError("perfil da OpenAI exige OPENAI_API_KEY no ambiente (.env ou Vercel)")
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


class PrazoEsgotado(Exception):
    """O perfil com raciocínio não produziu saída dentro do prazo; a leitura do fluxo estourou antes de
    qualquer texto, e gerar_fluxo refaz a chamada no perfil de reserva."""


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
    if isinstance(erro, ProvedorIndisponivel):
        return erro.motivo, erro.tentar_em
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


def custo_usd(uso, modelo):
    """Custo de uma chamada pela tabela PRECOS.

    Args:
        uso: tokens da chamada (entrada, saida e, quando houver, cached e cache_write).
        modelo: id do modelo que respondeu.

    Returns:
        Custo em dolares, ou None quando o modelo nao tem preco na tabela ou o provedor nao informou o uso.
    """
    preco = PRECOS.get(modelo)
    if not preco or not uso or "entrada" not in uso:
        return None
    cached, gravado = uso.get("cached", 0), uso.get("cache_write", 0)
    normal = uso["entrada"] - cached - gravado
    return (normal * preco[0] + cached * preco[1] + gravado * preco[2] + uso.get("saida", 0) * preco[3]) / 1e6


def _carimbar(resposta, nome, inicio):
    # completa o registro de uso da chamada com o perfil que respondeu, o tempo e o custo
    perfil = PERFIS[nome]
    uso = dict(resposta.uso or {})
    uso.update(perfil=nome, modelo=perfil.modelo, esforco=perfil.raciocinio,
               ms=round((time.perf_counter() - inicio) * 1000), custo_usd=custo_usd(resposta.uso, perfil.modelo))
    return resposta._replace(uso=uso)


# ── entradas usadas pelo serving e pela ingestão ────────────────────────────────
_ADAPTADORES = {"gemini": _gerar_gemini, "openai": _gerar_openai}

def gerar(mensagens, sistema=None, ferramentas=None, temperatura=0.7, leve=False, prazo=None, perfil="atendimento"):
    # ponto único de geração: devolve Resposta(texto, chamada, uso) qualquer que seja o provedor do perfil.
    # leve=True pede o modelo barato do provedor (estruturadores e inferência de data da ingestão);
    # prazo troca o tempo maximo da chamada (o cliente da OpenAI usa 20 s, o tempo do atendimento)
    p = PERFIS[perfil]
    adaptador = _ADAPTADORES.get(p.provedor)
    if adaptador is None:
        raise RuntimeError(f"provedor desconhecido no perfil {perfil!r}: {p.provedor!r} (use 'gemini' ou 'openai')")
    inicio = time.perf_counter()
    # queda e 5xx sao refeitos aqui (2 novas tentativas, 1s e 2s); limite e credito sobem na hora
    for tentativa in range(3):
        try:
            return _carimbar(adaptador(mensagens, sistema, ferramentas, temperatura, leve, p, prazo), perfil, inicio)
        except Exception as e:
            classe = _classificar(e)
            if classe is None:
                raise
            motivo, tentar_em = classe
            if motivo == "indisponivel" and tentativa < 2:
                time.sleep(tentativa + 1)
                continue
            raise ProvedorIndisponivel(f"{type(e).__name__}: {str(e)[:200]}", motivo, tentar_em) from e


def _fluxo_com_retentativa(mensagens, sistema, ferramentas, temperatura, nome):
    # fluxo de um perfil; queda antes do primeiro texto e refeita como em gerar(), e depois dele a falha
    # sobe como ProvedorIndisponivel. PrazoEsgotado nao e erro do provedor e sobe direto
    p = PERFIS[nome]
    if p.provedor != "openai":
        r = gerar(mensagens, sistema, ferramentas, temperatura, perfil=nome)
        if r.texto:
            yield "texto", r.texto
        yield "fim", r
        return
    for tentativa in range(3):
        transmitiu = False
        try:
            for evento in _fluxo_openai(mensagens, sistema, ferramentas, temperatura, p):
                transmitiu = transmitiu or evento[0] == "texto"
                yield evento
            return
        except Exception as e:
            classe = _classificar(e)
            if classe is None:
                raise
            motivo, tentar_em = classe
            if motivo == "indisponivel" and tentativa < 2 and not transmitiu:
                time.sleep(tentativa + 1)
                continue
            raise ProvedorIndisponivel(f"{type(e).__name__}: {str(e)[:200]}", motivo, tentar_em) from e


def gerar_fluxo(mensagens, sistema=None, ferramentas=None, temperatura=0.7, perfil="atendimento"):
    """Gera em streaming: devolve ("texto", pedaco) a cada pedaco e ("fim", Resposta) no final.

    A OpenAI transmite de verdade (chat.completions sem raciocinio, API Responses com raciocinio); no
    Gemini a resposta inteira sai num pedaco so. Queda antes do primeiro pedaco e refeita como em gerar();
    depois dele, a falha sobe como ProvedorIndisponivel, porque o aluno ja viu parte do texto. Num perfil
    com prazo, se nenhuma saida chega a tempo a chamada e refeita no perfil de reserva, e o registro de
    uso da resposta leva "reserva_de" e "corte_ms".

    Args:
        mensagens: conversa no formato neutro.
        sistema: prompt de sistema.
        ferramentas: ferramentas no formato neutro.
        temperatura: temperatura da geracao (ignorada com raciocinio ligado).
        perfil: nome do perfil em PERFIS.

    Yields:
        Tuplas ("texto", str) e, por ultimo, ("fim", Resposta) com o registro de uso da chamada.
    """
    inicio = time.perf_counter()
    try:
        for tipo, valor in _fluxo_com_retentativa(mensagens, sistema, ferramentas, temperatura, perfil):
            yield tipo, (_carimbar(valor, perfil, inicio) if tipo == "fim" else valor)
        return
    except PrazoEsgotado:
        corte_ms = round((time.perf_counter() - inicio) * 1000)

    # reserva: a mesma chamada no perfil sem raciocinio, com o tempo contado desde o pedido original
    reserva = PERFIS[perfil].reserva
    for tipo, valor in _fluxo_com_retentativa(mensagens, sistema, ferramentas, temperatura, reserva):
        if tipo == "fim":
            valor = _carimbar(valor, reserva, inicio)
            valor = valor._replace(uso={**valor.uso, "reserva_de": perfil, "corte_ms": corte_ms})
        yield tipo, valor


def completar(prompt, temperatura, leve=False, imagens=None):
    # prompt único, sem conversa nem ferramenta, devolvendo só o texto (uso da ingestão)
    mensagem = {"papel": "usuario", "texto": prompt}
    if imagens:
        mensagem["imagens"] = imagens
    return gerar([mensagem], temperatura=temperatura, leve=leve, prazo=PRAZO_INGESTAO, perfil="ingestao").texto or ""


def modelo_ativo():
    # identidade do atendimento, para carimbar trace/telemetria e o relatório do eval
    base, extra = PERFIS["atendimento"], PERFIS.get("atendimento_raciocinio")
    nome = base.modelo if base.raciocinio == "none" else f"{base.modelo}+raciocinio-{base.raciocinio}"
    if extra and extra.raciocinio != "none":
        nome += (f"+raciocinio-{extra.raciocinio}-por-regra" if extra.modelo == base.modelo
                 else f"+{extra.modelo}-{extra.raciocinio}-por-regra")
    return nome
