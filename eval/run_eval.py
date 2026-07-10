"""Validador do golden set, em dois modos independentes:

  coletar  -> roda o `ask` real N vezes por input e SALVA o trace de cada execucao em
              eval/runs/coleta.jsonl. Unica etapa que chama o agente (cara). Rode 1x por
              mudanca de comportamento do agente (prompt, modelo, base).
  validar  -> carrega a coleta, junta com o golden por case_id e aplica as fases objetivas
              (1, 2, ...). Sem agente: instantaneo. Itere aqui a vontade (mexer em
              criterios_query ou na metrica so exige re-validar, nao re-coletar).

Uso:  python -m eval.run_eval coletar    (usa EVAL_N, EVAL_THROTTLE)
      python -m eval.run_eval validar
"""
import os, sys, io, re, json, time, unicodedata, contextlib
from datetime import datetime

# console em UTF-8 (Windows cp1252 quebra acento/emoji)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RAIZ not in sys.path:
    sys.path.insert(0, _RAIZ)

_DIR   = os.path.dirname(__file__)
GOLDEN = os.path.join(_DIR, "golden_set.json")
COLETA = os.path.join(_DIR, "runs", "coleta.jsonl")
RESUMO = os.path.join(_DIR, "runs", "ultimo_resumo.txt")
CURSOS = os.path.join(_RAIZ, "data", "info", "cursos_atuais.json")

# throttle entre execucoes na coleta: o ask manda contexto grande (~12k tokens/busca);
# em rajada estoura o limite de tokens/min da API. espaçar mantem abaixo do teto.
THROTTLE = float(os.environ.get("EVAL_THROTTLE", "4"))

# carga do golden set
def carregar_casos():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)["casos"]

def inputs_do_caso(caso):
    # a pergunta principal e cada paraphrase sao inputs distintos do mesmo caso
    return [caso["pergunta"]] + list(caso.get("paraphrases", []))

# ── COLETA: roda o agente e salva o trace (uma execucao por linha) ───────────────

@contextlib.contextmanager
def _silencia_log():
    buf = io.StringIO(); antigo = sys.stdout; sys.stdout = buf
    try:
        yield
    finally:
        sys.stdout = antigo

def _executar(case_id, inp, run):
    # roda o ask real com retry; erro transitorio de API vira ERRO da execucao (nunca
    # derruba a coleta). o registro guarda so a SAIDA; as expectativas vem do golden ao validar.
    from rag.chain import ask
    trace, resposta, erro = {}, None, None
    for tent in range(3):
        try:
            trace = {}
            with _silencia_log():
                resposta = ask(inp, trace=trace)
            erro = None
            break
        except Exception as e:
            erro = f"{type(e).__name__}: {str(e)[:120]}"
            time.sleep(8 * (tent + 1))
    return {
        "case_id":   case_id,
        "input":     inp,
        "run":       run,
        "erro":      erro,
        "acao_real": trace.get("acao"),
        "resposta":  resposta,
        "buscas":    trace.get("buscas", []),
    }

def coletar(n=1, filtro_ids=None):
    casos = carregar_casos()
    alvo  = [c for c in casos if not filtro_ids or c["id"] in filtro_ids]
    total = sum(len(inputs_do_caso(c)) for c in alvo) * n
    os.makedirs(os.path.dirname(COLETA), exist_ok=True)
    print(f"Coletando {total} execucoes (n={n}) -> {COLETA}", flush=True)
    feitos = 0
    with open(COLETA, "w", encoding="utf-8") as f:
        for caso in alvo:
            for inp in inputs_do_caso(caso):
                for r in range(n):
                    rec = _executar(caso["id"], inp, r)
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n"); f.flush()
                    feitos += 1
                    if feitos % 20 == 0:
                        print(f"  {feitos}/{total} execucoes", flush=True)
                    time.sleep(THROTTLE)
    print(f"Coleta concluida: {feitos} execucoes em {COLETA}", flush=True)

# ── normalizacao e expansao de placeholders ─────────────────────────────────────

def _norm(s):
    # minusculas + sem acento, para casar "análise" com "analise"
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))

_CURSOS_CACHE = None
def _cursos_atuais():
    global _CURSOS_CACHE
    if _CURSOS_CACHE is None:
        try:
            _CURSOS_CACHE = [_norm(c) for c in json.load(open(CURSOS, encoding="utf-8"))["cursos"]]
        except Exception:
            _CURSOS_CACHE = []
    return _CURSOS_CACHE

def _termos_do_slot(slot):
    # slot: string (um termo) ou lista (qualquer-um). "@cursos_atuais"/"@ano_atual" expandem.
    itens = slot if isinstance(slot, list) else [slot]
    termos = []
    for t in itens:
        if t == "@cursos_atuais":
            termos += _cursos_atuais()
        elif t == "@ano_atual":
            termos.append(str(datetime.now().year))
        else:
            termos.append(_norm(t))
    return termos

# ── FASE 1: decisao (buscar vs nao-buscar) ───────────────────────────────────────

def _espera_busca(a):
    return a in ("buscar", "corrigir_e_buscar")

def fase1(rec, caso):
    esperadas = caso["acao_esperada"]
    if isinstance(esperadas, str):
        esperadas = [esperadas]
    buscou = (rec.get("acao_real") == "buscar")
    # passa se a decisao real casa com QUALQUER acao aceita
    return any(_espera_busca(e) == buscou for e in esperadas)

# ── FASE 2: formulacao da query (so quando houve busca) ──────────────────────────

def _ausente(termo, q):
    # palavra inteira, para "reitor" nao casar dentro de "reitoria"
    return re.search(r"\b" + re.escape(termo) + r"\b", q) is None

def fase2(rec, caso):
    # N/A (None) se nao buscou; senao checa a query contra criterios_query.
    # deve_conter: todos os slots satisfeitos (slot string=substring; lista=qualquer-um).
    # nao_deve_conter: nenhum termo presente (palavra inteira) em nenhuma query.
    queries = [b.get("query") for b in rec.get("buscas", []) if b.get("query")]
    if not queries:
        return None
    crit = caso.get("criterios_query", {})
    deve = crit.get("deve_conter", [])
    nao  = crit.get("nao_deve_conter", [])
    qs = [_norm(q) for q in queries]
    deve_ok = any(all(any(t in q for t in _termos_do_slot(slot)) for slot in deve) for q in qs)
    nao_ok  = all(_ausente(_norm(t), q) for t in nao for q in qs)
    return bool(deve_ok and nao_ok)

# ── FASE 3: retrieval (o gold chegou ao contexto recuperado) ─────────────────────

def _contexto_urls(rec):
    urls = []
    for b in rec.get("buscas", []):
        urls += b.get("contexto_urls", [])
    return urls

def fase3_doc(rec, caso):
    # N/A se sem gold_urls ou nao buscou; senao: algum gold_url esta no contexto (Hit@k)
    golds = caso.get("gold_urls", [])
    if not golds or not rec.get("buscas"):
        return None
    ctx = set(_contexto_urls(rec))
    return any(g in ctx for g in golds)

def fase3_rr(rec, caso):
    # reciprocal rank do 1o gold no contexto (para o MRR): 1/pos, 0 se nao entrou. None se N/A.
    golds = caso.get("gold_urls", [])
    if not golds or not rec.get("buscas"):
        return None
    ordem = sorted((h.get("rank_rerank", 10**9), h.get("url"))
                   for b in rec.get("buscas", []) for h in b.get("hits", []) if h.get("no_contexto"))
    for i, (_, u) in enumerate(ordem):
        if u in golds:
            return 1.0 / (i + 1)
    return 0.0

def fase3_span(rec, caso):
    # N/A se sem answer_spans ou nao buscou; senao: algum span aparece nos chunks recuperados
    spans = caso.get("answer_spans", [])
    if not spans or not rec.get("buscas"):
        return None
    textos = _norm(" \n ".join(t for b in rec.get("buscas", []) for t in b.get("chunks_textos", [])))
    return any(_norm(s) in textos for s in spans)

# ── agregacao por caso + IC de Wilson ────────────────────────────────────────────

def _wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n; d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    h = z*((p*(1-p)/n + z*z/(4*n*n)) ** 0.5) / d
    return (max(0, c-h), min(1, c+h))

def _bloco(titulo, por_caso):
    # por_caso: {case_id: [bool, ...]} ja sem None
    linhas = [titulo]
    tp = tn = 0
    ordem = sorted(por_caso, key=lambda c: sum(por_caso[c]) / max(1, len(por_caso[c])))
    for caso in ordem:
        vals = por_caso[caso]
        if not vals:
            continue
        k, n = sum(vals), len(vals); tp += k; tn += n
        lo, hi = _wilson(k, n)
        linhas.append(f"  {caso:<32} {k}/{n} {k/n*100:4.0f}%  [{lo*100:3.0f}%, {hi*100:3.0f}%]")
    cab = f"{titulo}  ->  {tp}/{tn} = {tp/max(1,tn)*100:.0f}% (por execucao)"
    linhas[0] = cab
    return linhas

def _bloco_fase3(por_caso):
    # por caso: doc-hit, span-hit e DE ONDE veio a falha de span:
    #   retrieval = o gold nem chegou ao contexto  |  chunk = o gold veio, mas nao o pedaco com o fato
    tdoc = ndoc = tspan = nspan = m_ret = m_chunk = 0
    linhas = [""]
    def taxa_doc(c):
        ds = [d for d, _ in por_caso[c] if d is not None]
        return sum(ds) / len(ds) if ds else 1.0
    for caso in sorted(por_caso, key=taxa_doc):
        vals = por_caso[caso]
        ds = [d for d, s in vals if d is not None]
        ss = [s for d, s in vals if s is not None]
        ret = sum(1 for d, s in vals if s is False and not d)  # span falhou porque o doc nao veio
        chk = sum(1 for d, s in vals if s is False and d)      # span falhou com o doc presente (chunk)
        tdoc += sum(ds); ndoc += len(ds); tspan += sum(ss); nspan += len(ss); m_ret += ret; m_chunk += chk
        doc_s  = f"{sum(ds)}/{len(ds)}" if ds else "  -"
        span_s = f"{sum(ss)}/{len(ss)}" if ss else "  -"
        origem = f"   (span: {ret} retrieval + {chk} chunk)" if (ret or chk) else ""
        linhas.append(f"  {caso:<32} doc {doc_s:>6}  span {span_s:>6}{origem}")
    linhas[0] = (f"FASE 3 (retrieval)  ->  doc {tdoc}/{ndoc} = {tdoc/max(1,ndoc)*100:.0f}% | "
                 f"span {tspan}/{nspan} = {tspan/max(1,nspan)*100:.0f}%  |  "
                 f"falhas de span: {m_ret} retrieval + {m_chunk} chunk")
    return linhas

# ── VALIDAR: le a coleta, junta com o golden, aplica as fases ────────────────────

def validar():
    if not os.path.exists(COLETA):
        print(f"Sem coleta em {COLETA}. Rode antes: python -m eval.run_eval coletar")
        return
    casos = {c["id"]: c for c in carregar_casos()}
    regs  = [json.loads(l) for l in open(COLETA, encoding="utf-8") if l.strip()]
    erros = sum(1 for r in regs if r.get("erro"))

    f1, f2, f3, rrs = {}, {}, {}, {}
    for r in regs:
        if r.get("erro"):
            continue
        caso = casos.get(r["case_id"])
        if not caso:
            continue
        cid = r["case_id"]
        f1.setdefault(cid, []).append(fase1(r, caso))
        v2 = fase2(r, caso)
        if v2 is not None:
            f2.setdefault(cid, []).append(v2)
        d, s = fase3_doc(r, caso), fase3_span(r, caso)
        if d is not None or s is not None:
            f3.setdefault(cid, []).append((d, s))
        rr = fase3_rr(r, caso)
        if rr is not None:
            rrs.setdefault(cid, []).append(rr)

    out  = [f"Coleta: {len(regs)} execucoes ({erros} com erro de API, excluidas)\n"]
    out += _bloco("FASE 1 (decisao)", f1)
    out += [""]
    out += _bloco("FASE 2 (formulacao da query; so execucoes que buscaram)", f2)
    out += [""]
    out += _bloco_fase3(f3)
    todos_rr = [x for v in rrs.values() for x in v]
    if todos_rr:
        out += ["", f"MRR (gold_url no contexto): {sum(todos_rr)/len(todos_rr):.2f} (media sobre {len(todos_rr)} execucoes com gold)"]
    texto = "\n".join(out) + "\n"

    os.makedirs(os.path.dirname(RESUMO), exist_ok=True)
    with open(RESUMO, "w", encoding="utf-8") as f:
        f.write(texto)
    print(texto)

if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else "validar"
    if modo == "coletar":
        coletar(n=int(os.environ.get("EVAL_N", "1")))
    elif modo == "validar":
        validar()
    else:
        print(f"modo desconhecido: {modo}. Use: coletar | validar")
