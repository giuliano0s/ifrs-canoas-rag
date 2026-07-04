# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## O que e

Assistente de IA (RAG) para o IFRS Campus Canoas. Um widget de chat injetado sobre um clone da pagina oficial do campus responde perguntas de estudantes a partir de uma base vetorial de documentos do site (paginas e PDFs). Pensado para deploy serverless no Vercel.

## Comandos

Ambiente (Windows, PowerShell):
```powershell
.venv\Scripts\activate
pip install -r requirements.txt
```

Rodar o app local (Flask). A porta 5000 costuma estar ocupada por um tunel SSH na maquina de dev, use outra via `PORT`:
```powershell
$env:PORT=5050; .venv\Scripts\python.exe -m ui.app
```
Acesse `http://127.0.0.1:5050`. Rode sempre como modulo (`-m ui.app`) a partir da raiz, nunca `python ui/app.py`, por causa dos imports de pacote (`rag.`, `ui.`).

Popular / atualizar a base vetorial (pipeline completa: crawler, parser HTML, parser PDF, chunker, ingest, snapshot):
```powershell
$env:RECRAWL="False"; $env:REPARSE="False"; $env:REINGEST="False"; $env:ANOS_VALIDOS="2023,2024,2025,2026"; .venv\Scripts\python.exe pipelines\ingest_pipeline.py
```
Flags de controle no topo do arquivo: `RECRAWL`, `REPARSE`, `REINGEST` (cada `False` = incremental na sua fase; default no codigo hoje e `RECRAWL=True`, os demais `False`). Todas aceitam override via env (string `"True"`/`"False"`). Hoje `RECRAWL` fica `True` de proposito: no incremental o crawler pula paginas pai ja indexadas e nao acha subpaginas novas. `ANOS_VALIDOS` tambem aceita override via env (CSV, ex: `ANOS_VALIDOS=2026`; default 2025-2026).

Gerar o `index.html` que o app serve (snapshot com a faixa de aviso e o widget ja injetados); tambem roda no fim da pipeline de ingestao:
```powershell
.venv\Scripts\python.exe -m ui.clone_page
```

`vercel dev` NAO funciona no Windows nativo (bug do `@vercel/python`: gera path com `\U` nao escapado em `vc_init_dev.py`). Para teste fiel do empacotamento Vercel, use WSL ou faca deploy real (Linux). Para validar a logica, use o Flask local acima.

## Arquitetura

Duas metades independentes que so se encontram na base vetorial Upstash:

**1. Ingestao (offline, roda na maquina do dev)** — `pipelines/ingest_pipeline.py`
Pipeline sequencial: crawler varre `ifrs.edu.br/canoas`, parser HTML e parser PDF extraem texto (PDFs de horario passam por gemini-2.5-flash-lite para estruturar; datas de publicacao inferidas pelo mesmo modelo), parser de planilhas baixa Google Sheets publicados (links `docs.google.com/spreadsheets` achados no site) como CSV e estrutura em frases via LLM (`type: "sheet"` no metadata), chunker fatia em pedacos, ingest embeda com Gemini e faz upsert no Upstash, e o snapshot regenera o `ui/index.html` (via `clone_page`) que o app serve estatico. Os notebooks `01_crawler` a `05_rag` sao a versao exploratoria das mesmas fases; o `.py` e a versao de producao consolidada. Os dados intermediarios (`data/raw/`, `data/parsed/`, `data/chunks/`) estao no `.gitignore` e nao trafegam pelo Git — so o Upstash (nuvem) e compartilhado entre maquinas.

**2. Servico de chat (online, Vercel)** — `ui/app.py` + `rag/chain.py` + `rag/gatekeeper.py`
Flask stateless. O fluxo de uma pergunta: `gatekeeper` checa rate limit por IP no Redis e `chain.ask()` roda um agente com tool calling (Gemini `gemini-2.5-flash`). O modelo investiga a pergunta e decide entre chamar a ferramenta `buscar_documentos` (formulando uma query refinada, ja corrigindo premissas como reitor->diretor) ou pedir esclarecimento ao estudante quando falta um discriminador (curso, tipo de prova). A ferramenta embeda a query e coleta um pool amplo do Upstash (`FETCH_K=30`) por similaridade, reordena por data (`rerank_by_date`) e corta os `CONTEXT_K=15` melhores para o contexto (over-fetch: o rerank escolhe de um pool maior sem inflar os tokens do LLM). O modelo entao responde citando as fontes e explicitando o escopo (ex: "a prova de RECUPERACAO do curso X"). Se a busca nao retorna nada, cai no fallback de internet via `google_search`. O prompt do agente (`data/info/agente-ifrs.txt`) instrui dois comportamentos contra respostas cruas em perguntas vagas: quando o referente tem outra leitura plausivel na base, responde a mais provavel e menciona a alternativa (sem travar); e consciencia temporal, ressalvando dados de anos anteriores a data atual em vez de apresenta-los como vigentes.

### Servidor sem estado (decisao central)

O servidor NAO guarda historico de conversa, sessao, nem cookie. O historico vive no cliente (`ui/widget.js`, variavel em memoria) e e enviado inteiro no corpo de cada `POST /chat`. O servidor sanitiza (`sanitize_history`, teto de `MAX_HISTORY_MESSAGES`) e repassa ao `ask`. Isso e o que torna o app compativel com o serverless do Vercel (instancias efemeras que nao compartilham memoria). Ao recarregar a pagina o historico zera de proposito (sem `localStorage`), para contexto antigo nao poluir uma nova conversa. O contexto multi-turno funciona durante a sessao ativa porque o array em memoria acumula e vai junto em cada chamada.

### Pagina servida estaticamente

A rota `/` serve o `ui/index.html` ja pronto (via `send_from_directory`); o app nao busca o IFRS ao vivo nem cacheia em runtime (filesystem read-only do Vercel). O snapshot e montado offline por `ui/clone_page.py` (`build_page`: busca o HTML do IFRS, injeta a faixa de aviso `ALERT_BANNER` de ambiente nao oficial e o `widget.js`) e regenerado na fase final da pipeline de ingestao, depois commitado. Assim a pagina so muda quando a base muda, e nenhum usuario paga o custo do fetch ao vivo.

### Separacao de chaves Upstash

O Vector tem dois tokens, controlados por variavel de ambiente distinta:
- `UPSTASH_API_KEY` (read-only) — usado pelo `chain.py` em producao (so consulta). E o unico Vector token cadastrado no Vercel.
- `UPSTASH_WRITE_API_KEY` (read+write) — usado pela ingestao (`ingest_pipeline.py`, notebook 04, delete no `test.ipynb`). Fica so na maquina local, NUNCA no Vercel.

O Redis (`UPSTASH_REDIS_*`) usa o token padrao com escrita, pois o rate limiter incrementa contador a cada request. Read-only nao serve ali.

### Embeddings

`gemini-embedding-001`, 3072 dimensoes, similaridade cosine. O index Upstash precisa ser criado com esses parametros. Os scores do Upstash sao normalizados em 0 a 1 (no antigo Qdrant iam de -1 a 1); o `min_score=0.60` em `chain.py` pode precisar de ajuste fino observando scores reais.

## Deploy (Vercel)

Entrypoint em `api/index.py` (reexpoe `from ui.app import app`); o preset Flask do Vercel resolve o app sozinho. `vercel.json` define `maxDuration: 60` para a funcao (folga para a chamada LLM e o fallback de internet). Cadastrar no painel do Vercel as variaveis: `UPSTASH_API_KEY` (read-only), `UPSTASH_ENDPOINT`, `UPSTASH_REDIS_API_KEY`, `UPSTASH_REDIS_ENDPOINT`, `GEMINI_API_KEY_T1`. NAO cadastrar `UPSTASH_WRITE_API_KEY`.

## Proximos passos

Em aberto, nesta ordem de prioridade:
1. Bateria de testes automatizados (retrieval, respostas, edge cases). Pre-requisito para o job periodico. Desenho em 4 camadas na secao abaixo.
2. Crawler no dominio de ingresso: cobrir `ingresso.ifrs.edu.br` (processo seletivo) liberando o dominio no `is_valid_page`; se for filtrar por ano la, ensinar o regex a ler o formato `/AAAA-S/` (ano-semestre, ex: `/2026-2/`).
3. Ingerir o Instagram do Gremio/campus: fonte de informacao atual que hoje escapa ao pipeline (ex: data real da festa julina, so anunciada la, diverge do calendario). Fonte hostil: exige login, tem anti-scraping, e muitos anuncios sao cards de imagem (precisariam de OCR/LLM multimodal). Opcoes a decidir: API oficial (Graph API, exige conta Business + app Meta + token, estavel e legitima, pega legendas) vs scraping + multimodal (mais poderoso para imagens, mas fragil e na zona cinzenta dos termos).
5. Recrawl funcional e eficiente: reestruturar o crawler para re-escanear apenas paginas indice/listagem (scan) em vez de re-baixar o site inteiro como o `RECRAWL=True` faz hoje, achando subpaginas novas sem o custo do recrawl total. Inclui limpeza e otimizacao do fluxo.
6. Reexecucao periodica: job agendado da pipeline de ingestao, apos a bateria de testes validar.
7. Reduzir latencia (se necessario): cache de embeddings frequentes, modelo menor para triagem.
8. Validar com gestores do Campus Canoas.
9. Expandir para multiplos campi (possibilidade remota): namespaces ou metadata `campus` no Upstash, pipeline parametrizada.

### Bateria de testes (desenho em 4 camadas)

Detalhamento do item 1. O sistema e um RAG agentico (o modelo decide buscar/perguntar/refinar), entao avaliar so a resposta final e enganoso: parte das falhas reais e de decisao ou de retrieval, nao de geracao. Segue o estado da arte de 2026 (RAGAS/DeepEval, LLM-as-a-judge, avaliacao de trajetoria de agente). Ordem sugerida: comecar por Camada 0 + 1 (barato e pega a maioria das falhas), evoluir depois.

- Camada 0 (golden set): conjunto curado de perguntas + comportamento/resposta esperados, validado a mao. Comeca dos casos reais de falha ja documentados (reitor->diretor, "que prova?", diretor 2024-2028, festa junina, horario do Igor) e cresce com geracao sintetica revisada. E o regression suite: toda mudanca roda contra ele.
- Camada 1 (retrieval): rotular as URLs corretas de cada pergunta (o log `[RETRIEVAL]` ajuda) e medir Hit@k, Recall@k, MRR sobre o pool `FETCH_K=30` e sobre o `CONTEXT_K=15` final. A diferenca isola se o rerank enterra doc bom.
- Camada 2 (trajetoria agentica): avaliar a decisao, nao so a resposta. Escolheu a acao certa (buscar/perguntar/responder)? A query formulada era boa (corrigiu premissa? incluiu ano)?
- Camada 3 (geracao, LLM-as-a-judge): faithfulness e answer relevancy (sem gabarito), answer correctness (com gabarito), mais rubricas nossas (explicitou escopo? citou fontes? ressalvou dado antigo?). Juiz validado contra rotulos humanos em PT-BR antes de escalar.

Stack sugerido: DeepEval como espinha (pytest-native, vira CI/regressao e cobre RAG + agente), RAGAS opcional para as metricas RAG canonicas, tracing (Phoenix/TruLens) so em producao. Juiz: Gemini.
