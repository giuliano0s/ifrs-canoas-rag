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

Popular / atualizar a base vetorial (pipeline completa: crawler, parser HTML, parser PDF, chunker, ingest):
```powershell
.venv\Scripts\python.exe pipelines\ingest_pipeline.py
```
Flags de controle no topo do arquivo: `RECRAWL`, `REPARSE`, `REINGEST` (todas `False` = modo incremental).

Gerar o `index.html` de fallback com a faixa de aviso e o widget ja injetados:
```powershell
.venv\Scripts\python.exe -m ui.clone_page
```

`vercel dev` NAO funciona no Windows nativo (bug do `@vercel/python`: gera path com `\U` nao escapado em `vc_init_dev.py`). Para teste fiel do empacotamento Vercel, use WSL ou faca deploy real (Linux). Para validar a logica, use o Flask local acima.

## Arquitetura

Duas metades independentes que so se encontram na base vetorial Upstash:

**1. Ingestao (offline, roda na maquina do dev)** — `pipelines/ingest_pipeline.py`
Pipeline sequencial de 6 fases: crawler varre `ifrs.edu.br/canoas`, parser HTML e parser PDF extraem texto (PDFs de horario passam por gemini-2.5-flash-lite para estruturar; datas de publicacao inferidas pelo mesmo modelo), chunker fatia em pedacos, ingest embeda com Gemini e faz upsert no Upstash, e o snapshot regenera o `ui/index.html` (via `clone_page`) que o app serve estatico. Os notebooks `01_crawler` a `05_rag` sao a versao exploratoria das mesmas fases; o `.py` e a versao de producao consolidada. Os dados intermediarios (`data/raw/`, `data/parsed/`, `data/chunks/`) estao no `.gitignore` e nao trafegam pelo Git — so o Upstash (nuvem) e compartilhado entre maquinas.

**2. Servico de chat (online, Vercel)** — `ui/app.py` + `rag/chain.py` + `rag/gatekeeper.py`
Flask stateless. O fluxo de uma pergunta: `gatekeeper` checa rate limit por IP no Redis, `chain.ask()` embeda a query, busca no Upstash, reordena por data (`rerank_by_date`), monta contexto e chama o Gemini. Se nada passa o `min_score`, cai num fallback de busca na internet via Gemini google_search.

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
1. Bateria de testes automatizados (retrieval, respostas, edge cases). Pre-requisito para o job periodico.
2. Mais anos na base: expandir `ANOS_VALIDOS` (hoje 2025-2026) para incluir 2023-2024, com filtros de relevancia.
3. Crawler no dominio de ingresso: cobrir `ingresso.ifrs.edu.br` (processo seletivo) liberando o dominio no `is_valid_page`; se for filtrar por ano la, ensinar o regex a ler o formato `/AAAA-S/` (ano-semestre, ex: `/2026-2/`).
4. Recrawl funcional e eficiente: reestruturar o crawler para re-escanear apenas paginas indice/listagem (scan) em vez de re-baixar o site inteiro como o `RECRAWL=True` faz hoje, achando subpaginas novas sem o custo do recrawl total. Inclui limpeza e otimizacao do fluxo.
5. Reexecucao periodica: job agendado da pipeline de ingestao, apos a bateria de testes validar.
6. Reduzir latencia (se necessario): cache de embeddings frequentes, modelo menor para triagem.
7. Validar com gestores do Campus Canoas.
8. Expandir para multiplos campi (possibilidade remota): namespaces ou metadata `campus` no Upstash, pipeline parametrizada.
