"""Testes da telemetria de rag/telemetry.py e da leitura do harvest de producao, sem rede.

Uso, a partir da raiz: python -m tests.test_telemetria
"""
import json
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import rag.telemetry as telemetry
from eval import harvest_producao
from rag.chain import PROMPT_VERSAO

CPF, TELEFONE = "123.456.789-09", "(51) 99876-5432"

TRACE = {
    "acao": "buscar",
    "buscas": [{"query": f"contato do aluno {TELEFONE}", "contexto_ids": ["https://ifrs.edu.br/canoas/a#0"],
                "hits": [{"id": "https://ifrs.edu.br/canoas/a#0", "url": "https://ifrs.edu.br/canoas/a",
                          "score": 0.812345, "no_contexto": True}]}],
    "tokens": {"entrada": 18000, "saida": 150},
    "chamadas": [{"etapa": "agente", "regra": "calendario", "perfil": "atendimento", "custo_usd": 0.0021}],
}

EXPORTADOR = InMemorySpanExporter()
CLIENTE = Langfuse(public_key="pk-lf-teste-local", secret_key="sk-lf-teste-local", base_url="http://127.0.0.1:9",
                   span_exporter=EXPORTADOR)


def _turno(**kwargs):
    # registra um turno com o exportador em memoria no lugar do envio ao Langfuse
    EXPORTADOR.clear()
    with mock.patch.object(telemetry, "_cliente", return_value=CLIENTE):
        telemetry.registrar_chat(**kwargs)
    return EXPORTADOR.get_finished_spans()


def testar_registro():
    spans = _turno(query=f"meu cpf é {CPF}, qual o horário da biblioteca?", history=[{"role": "user"}],
                   trace=TRACE, resposta=f"Anotei {CPF}. A biblioteca abre às 8h [1].", latencia_ms=4200,
                   session_id="sessao-1", user_id="usuario-1", primeiro_texto_ms=3100)

    # um turno vira uma observacao raiz "chat", sem pai
    assert len(spans) == 1, spans
    span = spans[0]
    attrs = dict(span.attributes)
    assert span.name == "chat" and span.parent is None
    assert attrs["langfuse.observation.type"] == "span"

    # atributos de correlacao propagados para o turno
    assert attrs["langfuse.trace.name"] == "chat"
    assert attrs["session.id"] == "sessao-1" and attrs["user.id"] == "usuario-1"
    assert attrs["langfuse.version"] == PROMPT_VERSAO
    assert list(attrs["langfuse.trace.tags"]) == ["regra:calendario"]
    assert not any(chave.startswith("langfuse.trace.metadata") for chave in attrs)

    # pergunta e resposta como input/output da raiz, com o dado pessoal redigido
    assert "[CPF]" in attrs["langfuse.observation.input"]
    assert "[REDIGIDO]" in attrs["langfuse.observation.output"]
    assert not any(CPF in str(valor) or TELEFONE in str(valor) for valor in attrs.values())

    # registro completo no metadata da raiz, uma chave por campo, cada valor em JSON
    meta = {chave.removeprefix("langfuse.observation.metadata."): json.loads(valor)
            for chave, valor in attrs.items() if chave.startswith("langfuse.observation.metadata.")}
    assert meta["buscas"][0]["query"] == "contato do aluno [TELEFONE]"
    assert meta["buscas"][0]["hits"][0]["score"] == 0.812
    assert meta["buscas"][0]["contexto_ids"] == ["https://ifrs.edu.br/canoas/a#0"]
    assert meta["acao_real"] == "buscar" and meta["origem"] == "producao"
    assert meta["prompt_versao"] == PROMPT_VERSAO and meta["history_len"] == 1
    assert meta["latencia_ms"] == 4200 and meta["primeiro_texto_ms"] == 3100
    assert meta["input"] == attrs["langfuse.observation.input"]
    assert meta["session_id"] == "sessao-1" and meta["user_id"] == "usuario-1"
    assert "erro" not in meta
    assert "langfuse.observation.level" not in attrs
    print("ok: registro do turno")


def testar_erro_e_falhas():
    # turno com erro: nivel ERROR, mensagem e saida vazia
    spans = _turno(query="oi", history=None, trace=None, resposta=None, latencia_ms=900,
                   erro="ProvedorIndisponivel[fluxo_alto]: 429", session_id=None, user_id=None)
    attrs = dict(spans[0].attributes)
    assert attrs["langfuse.observation.level"] == "ERROR"
    assert attrs["langfuse.observation.status_message"] == "ProvedorIndisponivel[fluxo_alto]: 429"
    assert "langfuse.observation.output" not in attrs and "session.id" not in attrs

    # sem as chaves a telemetria nao faz nada
    with mock.patch.object(telemetry, "_cliente", return_value=None):
        assert telemetry.registrar_chat("oi", None, None, "olá", 10) is None

    # falha do cliente nunca chega a quem chama
    quebrado = mock.Mock()
    quebrado.start_observation.side_effect = RuntimeError("langfuse fora")
    with mock.patch.object(telemetry, "_cliente", return_value=quebrado):
        telemetry.registrar_chat("oi", None, TRACE, "olá", 10)
    print("ok: erro e falhas da telemetria")


def testar_harvest():
    # valores do metadata nos dois formatos que a API v2 devolve
    assert harvest_producao._decodifica([{"query": "x"}]) == [{"query": "x"}]
    assert harvest_producao._decodifica('[{"query": "x"}]') == [{"query": "x"}]
    assert harvest_producao._decodifica('"texto"') == "texto"
    assert harvest_producao._decodifica("[CPF] qual o horário?") == "[CPF] qual o horário?"
    assert harvest_producao._decodifica(None) is None

    # duas paginas pelo cursor, raiz gravada pelo SDK 2 (objeto) e pelo SDK 5 (texto JSON)
    paginas = [
        SimpleNamespace(data=[SimpleNamespace(trace_id="t1", metadata={"buscas": [{"contexto_ids": ["a#0"]}],
                                                                       "resposta": "r1"})],
                        meta=SimpleNamespace(cursor="c2")),
        SimpleNamespace(data=[SimpleNamespace(trace_id="t2", metadata={"buscas": '[{"contexto_ids": ["b#1"]}]',
                                                                       "resposta": '"r2"'})],
                        meta=SimpleNamespace(cursor=None)),
    ]
    falso = mock.Mock()
    falso.api.observations.get_many.side_effect = paginas
    with mock.patch("langfuse.Langfuse", return_value=falso):
        turnos = harvest_producao._traces_langfuse(10, dias=30)
    assert turnos == [("t1", {"buscas": [{"contexto_ids": ["a#0"]}], "resposta": "r1"}),
                      ("t2", {"buscas": [{"contexto_ids": ["b#1"]}], "resposta": "r2"})]

    # consulta limitada no tempo, so raizes "chat" e com o metadata lido inteiro
    primeira, segunda = falso.api.observations.get_many.call_args_list
    assert primeira.kwargs["name"] == "chat" and primeira.kwargs["is_root_observation"] is True
    assert primeira.kwargs["cursor"] is None and segunda.kwargs["cursor"] == "c2"
    assert set(harvest_producao.CHAVES_METADATA) <= set(primeira.kwargs["expand_metadata"].split(","))
    assert "metadata" in primeira.kwargs["fields"].split(",")
    inicio, fim = primeira.kwargs["from_start_time"], primeira.kwargs["to_start_time"]
    assert isinstance(inicio, datetime) and (fim - inicio).days == 30
    print("ok: leitura do harvest")


if __name__ == "__main__":
    testar_registro()
    testar_erro_e_falhas()
    testar_harvest()
