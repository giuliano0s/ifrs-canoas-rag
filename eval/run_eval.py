"""Validador (bateria de testes): pipeline unico.

Roda o golden set pelo `ask` de producao (com o hook de trace) e aplica as metricas
do DeepEval. Um arquivo = um pipeline: carga do golden -> adaptador (LLMTestCase) ->
metricas do dominio -> resumo.
"""
import os, sys, io, json, contextlib

# console em UTF-8 (relatorios/emojis do DeepEval quebrariam no cp1252 do Windows)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RAIZ not in sys.path:
    sys.path.insert(0, _RAIZ)

from deepeval.test_case import LLMTestCase
from deepeval.metrics import BaseMetric
from rag.chain import ask

GOLDEN = os.path.join(os.path.dirname(__file__), "golden_set.json")
RESUMO = os.path.join(os.path.dirname(__file__), "runs", "ultimo_resumo.txt")


# carga do golden set
def carregar_casos():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)["casos"]

def inputs_do_caso(caso):
    # a pergunta principal e cada paraphrase sao inputs distintos do mesmo caso
    return [caso["pergunta"]] + list(caso.get("paraphrases", []))


# adaptador: golden -> LLMTestCase, rodando o ask real e lendo o trace
@contextlib.contextmanager
def _silencia_log():
    buf = io.StringIO(); antigo = sys.stdout; sys.stdout = buf
    try:
        yield
    finally:
        sys.stdout = antigo

def caso_para_testcase(caso, input_texto):
    trace = {}
    with _silencia_log():
        resposta = ask(input_texto, trace=trace)
    b0 = (trace.get("buscas") or [{}])[0]
    return LLMTestCase(
        input=input_texto,
        actual_output=resposta,
        expected_output=caso.get("resposta_esperada"),
        retrieval_context=b0.get("chunks_textos") or None,
        metadata={
            "case_id": caso["id"],
            "acao_esperada": caso["acao_esperada"],
            "acao_real": trace.get("acao"),
            "query_formulada": b0.get("query"),
            "criterios_query": caso.get("criterios_query", {}),
            "gold_urls": caso.get("gold_urls", []),
            "answer_spans": caso.get("answer_spans", []),
            "existe_na_base": caso.get("existe_na_base"),
            "contexto_urls": b0.get("contexto_urls", []),
        },
    )

def gerar_test_cases(casos, n=1, filtro_ids=None):
    # N execucoes por input (pergunta + paraphrases) de cada caso
    tcs = []
    for caso in casos:
        if filtro_ids and caso["id"] not in filtro_ids:
            continue
        for inp in inputs_do_caso(caso):
            for _ in range(n):
                tcs.append(caso_para_testcase(caso, inp))
    return tcs


# metricas custom do dominio (fases objetivas que nenhum framework tem prontas)
def _espera_busca(acao_esperada):
    return acao_esperada in ("buscar", "corrigir_e_buscar")

class AcaoMetric(BaseMetric):
    """Fase 1 (decisao): buscou vs nao-buscou conforme o esperado. Objetivo, sem LLM.

    Mede so a decisao buscar/nao-buscar; o subtipo do nao-buscar
    (perguntar/responder_direto/recusar) fica para a Fase 6 (semantica).
    """
    def __init__(self, threshold=1.0):
        self.threshold = threshold

    def measure(self, test_case):
        meta = test_case.metadata or {}
        esperadas = meta.get("acao_esperada")
        if isinstance(esperadas, str):
            esperadas = [esperadas]
        real_busca = (meta.get("acao_real") == "buscar")
        # passa se a decisao real (buscar/nao-buscar) casa com QUALQUER acao aceita
        ok = any(_espera_busca(e) == real_busca for e in esperadas)
        self.score = 1.0 if ok else 0.0
        self.success = self.score >= self.threshold
        self.reason = f"aceitas={esperadas}; agente {'buscou' if real_busca else 'nao buscou'}"
        return self.score

    async def a_measure(self, test_case):
        return self.measure(test_case)

    def is_successful(self):
        return self.success

    @property
    def __name__(self):
        return "Acao (Fase 1)"


# orquestracao: roda os casos, aplica a metrica e salva um resumo legivel
def main(n=1, filtro_ids=None):
    casos = carregar_casos()
    tcs = gerar_test_cases(casos, n=n, filtro_ids=filtro_ids)

    metric = AcaoMetric()
    linhas, passou = [], 0
    for tc in tcs:
        m = tc.metadata
        metric.measure(tc)
        ok = metric.is_successful()
        passou += ok
        linhas.append(f"{'PASS' if ok else 'FAIL'} | {m['case_id']:<32} | "
                       f"esperada={m['acao_esperada']:<18} | real={m['acao_real']}")

    os.makedirs(os.path.dirname(RESUMO), exist_ok=True)
    with open(RESUMO, "w", encoding="utf-8") as f:
        f.write(f"Fase 1 (acao): {passou}/{len(tcs)} inputs passaram\n\n")
        f.write("\n".join(linhas) + "\n")
    print(f"Fase 1 (acao): {passou}/{len(tcs)} passaram. Resumo em {RESUMO}")


if __name__ == "__main__":
    main()
