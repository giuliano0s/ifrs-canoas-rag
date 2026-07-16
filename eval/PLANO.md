# Plano do validador (bateria de testes)

Plano de desenvolvimento e arquitetura do validador que roda o golden set contra o
pipeline real e mede as 7 fases. A estratégia (o "o quê") está no `CLAUDE.md`, seção
"Bateria de testes"; aqui está o "como".

> **Nota de estado (jul/2026):** este é o plano ORIGINAL; a implementação real divergiu e vale o que está no CLAUDE.md (seções "Bateria de testes" e "Achados atuais"). O eval roda em `eval/run_eval.py` (custom, dois passos `coletar`/`validar`, fases em Python puro), com o juiz das Fases 5/6 via switch `EVAL_JUDGE` (Claude Code por subagente, ou Gemini inline), e NÃO via DeepEval-como-runner + datasets do Langfuse como desenhado abaixo. O Langfuse cobre a telemetria de PRODUÇÃO, não o dataset do eval; o DeepEval está instalado mas é opcional. O texto abaixo fica como registro do desenho inicial.

**Princípio de construção: não reinventar.** Usar ferramentas consagradas do mercado
para tudo que já existe pronto (tracing, armazenamento, dashboards, agregação, métricas
de avaliação de RAG, LLM-as-judge). Escrever à mão SÓ a lógica específica do nosso
domínio que nenhuma ferramenta tem.

## Princípios herdados do CLAUDE.md

- **Simular o pipeline real**: o validador chama o MESMO `ask` de produção.
- **Objetivo onde encaixa, semântico onde é necessário** (fases 1-4/7 vs 5-6).
- **Não-circular**: gabarito vem do conteúdo da base (já curado no golden set).
- **N execuções + taxa + IC**: pipeline estocástico (temperatura 0.7); mede-se estabilidade.

## Stack (ferramentas de mercado)

| ferramenta | papel | cobre |
|---|---|---|
| **Langfuse** | observabilidade LLM (hub) | tracing, armazenamento, dashboards, **datasets** (o golden set), **scores**, comparação de versões, telemetria de produção |
| **DeepEval** | framework de avaliação | runner (pytest-native), métricas prontas de RAG (retrieval, faithfulness, relevancy, correctness), G-Eval para métricas custom, integração com Langfuse |
| **Gemini** | LLM-juiz | usado pelo DeepEval como avaliador nas fases semânticas |

O mesmo trace serve para os dois usos: **eval** (offline, na máquina do dev) e
**telemetria de produção** (no Vercel). A diferença é só o destino/contexto do sink
Langfuse. PII ignorado por ora (ambiente de teste).

## Arquitetura

```
ask() instrumentado com Langfuse (@observe)  -->  trace (query, hits, acao, sources, resposta)
        |                                              |
        | (eval, offline)                              | (producao, Vercel)
        v                                              v
  DeepEval roda o golden set (dataset Langfuse)   Langfuse coleta os traces reais
  N vezes contra o ask real; aplica as metricas   (monitoramento + perguntas reais
  (prontas + custom); envia scores ao Langfuse    que viram novos casos do dataset)
        |
        v
  Langfuse: dashboards, taxa por caso/fase, comparacao entre versoes
```

- Instrumentar o `ask` uma vez com o SDK do Langfuse; isso serve eval E produção.
- O `golden_set.json` vira um **dataset** no Langfuse (ou é lido direto pelo DeepEval).
- DeepEval roda cada input via o `ask` real, N vezes, aplica as métricas e registra os
  scores no Langfuse. Agregação, histórico, dashboards e comparação de versões: tudo do
  Langfuse/DeepEval, nada à mão.

## O que NÃO construímos (vem pronto) vs o que escrevemos

**Pronto (não escrever):** armazenamento de traces, dashboards/análise, agregação de
métricas, comparação entre versões, runner de testes, métricas de RAG (retrieval,
faithfulness, answer relevancy/correctness), LLM-as-judge, integração eval<->trace.

**Escrever (mínimo, só o que é do nosso domínio):**
- Instrumentação do `ask` com o SDK Langfuse (uma vez).
- Carregar o `golden_set.json` como dataset.
- As métricas custom que nenhuma ferramenta tem prontas (via G-Eval / métrica custom do
  DeepEval): ação esperada (Fase 1), regex de query (Fase 2), answerability por varredura
  da base (Fase 4), citação de fontes (Fase 7), e o critério de comportamento (Fase 6).

## Mapeamento das 7 fases -> de onde vem

| Fase | Tipo | Origem |
|---|---|---|
| 1 decisão | objetivo | **custom** (compara `trace.acao` vs `acao_esperada`) |
| 2 formulação | objetivo | **custom** (regex sobre `criterios_query`) |
| 3 retrieval | objetivo | **DeepEval/RAGAS** (contextual precision/recall) + custom leve p/ `gold_url` exato / Hit@k |
| 4 answerability | objetivo | **custom** (varredura da base via `index.range`; não existe pronto) |
| 5 geração | semântico | **DeepEval/RAGAS pronto** (faithfulness, answer relevancy, answer correctness) |
| 6 comportamento | semântico | **DeepEval G-Eval** (critério nosso: recusou/perguntou/ressalvou; jailbreak) |
| 7 citação | objetivo | **custom leve** (parse de `[n]` vs fontes do trace) |

As métricas custom são pequenas funções de critério dentro do DeepEval; o runner, a
agregação, o IC e o relatório vêm do framework/Langfuse.

## Marcos

- **M0 - Langfuse + trace**: subir Langfuse (cloud, free tier, ambiente de teste),
  instrumentar o `ask` com o SDK; ver os traces aparecerem no dashboard.
- **M1 - dataset + métricas prontas**: golden set como dataset; DeepEval rodando as
  métricas prontas (retrieval, faithfulness) nos casos `buscar`.
- **M2 - métricas custom**: ação (1), query-regex (2), answerability (4), citação (7),
  comportamento (6) como métricas custom no DeepEval.
- **M3 - N execuções**: rodar o dataset N vezes; ver taxa por caso/fase e comparação de
  versões no Langfuse.
- **M4 - telemetria de produção**: o mesmo trace, sink Langfuse ligado no Vercel
  (assíncrono/flush); monitorar produção.
- **M5 - loop**: perguntas reais capturadas em produção viram candidatas a novos casos
  do dataset (item 4 dos próximos passos), com curadoria não-circular.

## Custo e ambiente

- Langfuse cloud free tier + Gemini juiz. Rodada de eval com N=5 (~355 execuções do
  `ask`): ~US$ 3-4 (pipeline + juiz). PII ignorado por ora (teste); se produtizar de
  verdade, revisar (retenção, anonimização, ou Langfuse self-host).

## Decisões em aberto

- Instrumentação: `@observe` do Langfuse no `ask`/funções internas vs cliente manual.
- Fase 1 subtipo (perguntar/responder_direto/recusar no não-buscar): critério G-Eval.
- Gold temporal (aulas/rematrícula/festa/PIT): data de referência parametrizável.
