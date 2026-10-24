import os
import re
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# cliente Langfuse carregado sob demanda e memoizado. a telemetria e OPCIONAL: sem as chaves
# (LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY) o cliente fica None e tudo vira no-op, entao rodar
# local, a bateria de eval e o Vercel sem as chaves cadastradas seguem funcionando sem tocar em nada.
_client = None
_estado = None  # None = ainda nao tentou inicializar; True = ligado; False = desligado

def _cliente():
    global _client, _estado
    if _estado is None:
        pub, sec = os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY")
        base_url = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
        try:
            from langfuse import Langfuse
            _client = Langfuse(public_key=pub, secret_key=sec, base_url=base_url) if (pub and sec) else None
        except Exception:
            _client = None
        _estado = _client is not None
    return _client


# dados pessoais que o aluno pode digitar na pergunta (CPF, e-mail, telefone com DDD ou celular,
# numero longo como matricula); o telefone exige DDD ou o 9 do celular, para nao pegar "2026-2027"
_PII = [
    (re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"), "[CPF]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[EMAIL]"),
    (re.compile(r"\(?\b\d{2}\)?\s?9?\d{4}-?\d{4}\b|\b9\d{4}-?\d{4}\b"), "[TELEFONE]"),
    (re.compile(r"\b\d{8,}\b"), "[NUMERO]"),
]


def redigir_pii(texto):
    """Troca os dados pessoais do texto por rotulos.

    Args:
        texto: texto digitado pelo aluno (pergunta) ou derivado dele (query de busca).

    Returns:
        Par (texto redigido, lista dos trechos redigidos).
    """
    achados = []
    for padrao, rotulo in _PII:
        achados.extend(m.group(0) for m in padrao.finditer(texto or ""))
        texto = padrao.sub(rotulo, texto or "")
    return texto, achados


def _sem_eco(texto, achados):
    # a resposta mantem os contatos publicos da base; so o dado que o proprio aluno digitou e trocado
    for trecho in sorted(set(achados), key=len, reverse=True):
        texto = texto.replace(trecho, "[REDIGIDO]")
    return texto


def _tags_raciocinio(resumo):
    # marcas do turno para filtrar no Langfuse: regra que pediu raciocinio, se ele foi usado e se o
    # prazo estourou e a resposta saiu da reserva sem raciocinio
    tags = []
    if resumo.get("regra"):
        tags.append(f"regra:{resumo['regra']}")
    if resumo.get("usado"):
        tags.append("raciocinio")
    if resumo.get("reserva"):
        tags.append("reserva")
    return tags


# registra um turno de chat no Langfuse (sink duravel: o Vercel tem FS read-only) usando o MESMO
# schema da coleta do eval, via registro_de_trace. e uma "extensao" da coleta, carimbada com a
# versao viva do prompt: perguntas reais viram candidatas a caso de golden depois da curadoria.
# payload LEVE: guarda os ids dos chunks (url#i), nao o texto cru; o harvest reconstroi o texto
# localmente com index.fetch(ids). tudo dentro de try: falha de telemetria nunca afeta a resposta.
# o turno e UMA observacao raiz "chat": pergunta e resposta no input/output dela, o registro completo
# no metadata dela; sessao, usuario, versao do prompt e marcas vao por propagate_attributes, que no
# modelo do Langfuse v4 carimba toda observacao do turno (o metadata propagado aceita so texto curto,
# por isso o registro fica na raiz e nao e propagado).
def registrar_chat(query, history, trace, resposta, latencia_ms, erro=None, session_id=None, user_id=None,
                   primeiro_texto_ms=None):
    cli = _cliente()
    if cli is None:
        return
    try:
        from langfuse import propagate_attributes
        from rag.chain import registro_de_trace, MODEL, PROMPT_VERSAO

        # nada do que o aluno digitou sai com dado pessoal: pergunta, queries de busca e eco na resposta
        query, achados = redigir_pii(query)
        resposta = _sem_eco(resposta, achados) if resposta else resposta
        rec = registro_de_trace(trace, query, resposta, erro)
        rec["buscas"] = [
            {"query": redigir_pii(b.get("query"))[0],
             "contexto_ids": b.get("contexto_ids", []),
             "hits": [{"id": h.get("id"), "url": h.get("url"),
                       "score": round(h.get("score") or 0.0, 3),
                       "no_contexto": h.get("no_contexto")} for h in (b.get("hits") or [])]}
            for b in rec.get("buscas", [])
        ]
        rec.update({
            "modelo": MODEL,
            "prompt_versao": PROMPT_VERSAO,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "history_len": len(history or []),
            "latencia_ms": latencia_ms,
            "primeiro_texto_ms": primeiro_texto_ms,
            "session_id": session_id,
            "user_id": user_id,
            "origem": "producao",
        })

        # raiz do turno criada dentro do escopo de propagacao; o flush envia antes da funcao serverless congelar
        with propagate_attributes(trace_name="chat", session_id=session_id, user_id=user_id, version=PROMPT_VERSAO,
                                  tags=_tags_raciocinio(rec.get("raciocinio") or {})):
            cli.start_observation(name="chat", as_type="span", input=query, output=resposta, metadata=rec,
                                  level="ERROR" if erro else None, status_message=erro).end()
        cli.flush()
    except Exception:
        pass
