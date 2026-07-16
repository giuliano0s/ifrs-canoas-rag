# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## O que é

Assistente de IA (RAG) para o IFRS Campus Canoas. Um widget de chat injetado sobre um clone da página oficial do campus responde perguntas de estudantes a partir de uma base vetorial de documentos do site (páginas e PDFs). Pensado para deploy serverless no Vercel.

## Comandos

Ambiente (Windows, PowerShell):
```powershell
.venv\Scripts\activate
pip install -r requirements-dev.txt
```
`requirements.txt` é SÓ a produção (o que a função serverless no Vercel instala; o Vercel resolve com `uv lock`, então dependência de dev ali quebra o deploy). `requirements-dev.txt` inclui a produção e soma ingestão e avaliação.

Rodar o app local (Flask). A porta 5000 costuma estar ocupada por um túnel SSH na máquina de dev, use outra via `PORT`:
```powershell
$env:PORT=5050; .venv\Scripts\python.exe -m ui.app
```
Acesse `http://127.0.0.1:5050`. Rode sempre como módulo (`-m ui.app`) a partir da raiz, nunca `python ui/app.py`, por causa dos imports de pacote (`rag.`, `ui.`).

Popular / atualizar a base vetorial (pipeline completa: crawler com detecção de mudança, parsers, chunker, ingest, snapshot):
```powershell
$env:ANOS_VALIDOS="2023,2024,2025,2026"; .venv\Scripts\python.exe pipelines\ingest_pipeline.py
```
O modo default é incremental por `source_hash` (ver Arquitetura): o crawler varre o site inteiro, mas só o que é novo ou mudou segue para parse (LLM) e embed. Flags de exceção, com override via env (string `"True"`/`"False"`): `REPARSE=True` ignora os hashes e reprocessa tudo que já existe (replace geral, sem zerar o index); `REINGEST=True` zera o index e reingere do zero; `INCLUDE_SCANNED=True` volta a baixar/processar PDFs escaneados (imagem, sem texto), que o default pula (ver Arquitetura). `ANOS_VALIDOS` também aceita override via env (CSV, ex: `ANOS_VALIDOS=2026`; default 2025-2026).

Gerar o `index.html` que o app serve (snapshot com a faixa de aviso e o widget já injetados); também roda no fim da pipeline de ingestão:
```powershell
.venv\Scripts\python.exe -m ui.clone_page
```

Rodar a bateria de avaliação (eval), em dois passos independentes (estratégia detalhada na seção "Bateria de testes"):
```powershell
# 1) coletar: roda o agente N vezes por caso e salva os traces em eval/runs/coleta.jsonl.
#    Caro (chama o LLM); rode só quando o comportamento do agente muda (prompt, modelo, base).
$env:EVAL_N="5"; .venv\Scripts\python.exe -m eval.run_eval coletar
#    re-coletar só alguns casos (merge: preserva a coleta dos demais):
$env:EVAL_IDS="biblioteca-horario,diretor-geral-campus"; .venv\Scripts\python.exe -m eval.run_eval coletar

# 2) validar: aplica as 7 fases sobre a coleta salva e GERA o relatório. Barato, instantâneo; itere à vontade.
.venv\Scripts\python.exe -m eval.run_eval validar
```
O `validar` sempre regenera `eval/runs/relatorio.md` e `eval/runs/ultimo_resumo.txt` (detalhe com IC por caso), e imprime o relatório. O `relatorio.md` é a PROVA canônica para olhos externos: traz a config da coleta (modelo, versão/versões de prompt com contagem, período), o placar com IC de Wilson 95% em TODAS as fases, o caveat de que as Fases 5/6 (juiz) são sinal e não régua, a classificação de cada falha (bug real vs flake de 1 run vs gap de base vs artefato de métrica) e uma tabela por caso (25 casos x fases). Toda mudança no formato da prova vai no `gerar_relatorio`, para valer também nos próximos runs. As fases objetivas (1-4, 7) são checadas por regra. As semânticas (5 geração, 6 comportamento) usam um juiz LLM com switch `EVAL_JUDGE`:
- `claude` (default, zero custo de API): o `validar` escreve as tarefas em `eval/runs/juiz_tarefas.jsonl`; o próprio Claude Code julga via subagente e grava `juiz_vereditos.jsonl`; re-rode `validar` para ver 5/6 no relatório.
- `gemini` (inline): `$env:EVAL_JUDGE="gemini"; .venv\Scripts\python.exe -m eval.run_eval validar` julga chamando o Gemini e já preenche os vereditos; o relatório sai completo num comando só. Qualquer valor diferente de `gemini` cai no modo claude (o script não chama LLM, só consome os vereditos existentes).

`vercel dev` NÃO funciona no Windows nativo (bug do `@vercel/python`: gera path com `\U` não escapado em `vc_init_dev.py`). Para teste fiel do empacotamento Vercel, use WSL ou faça deploy real (Linux). Para validar a lógica, use o Flask local acima.

## Arquitetura

Duas metades independentes que só se encontram na base vetorial Upstash:

**1. Ingestão (offline: na máquina do dev ou no GitHub Actions semanal)** - `pipelines/ingest_pipeline.py`
Pipeline sequencial: crawler varre `ifrs.edu.br/canoas`, parser HTML e parser PDF extraem texto (as grades de horário aSc são lidas por VISÃO - gemini-2.5-flash multimodal, um chunk por professor -, pois a extração de texto embaralha a tabela 2D, ver "Retrieval: ... grade por visão"; datas de publicação inferidas por LLM a partir do próprio documento), parser de planilhas baixa Google Sheets publicados (links `docs.google.com/spreadsheets` achados no site) como CSV e estrutura em frases via LLM (`type: "sheet"` no metadata), chunker fatia em pedaços, ingest embeda com Gemini e faz upsert no Upstash, e o snapshot regenera o `ui/index.html` (via `clone_page`) que o app serve estático. Os dados intermediários (`data/raw/`, `data/parsed/`, `data/chunks/`) estão no `.gitignore` e não trafegam pelo Git; só o Upstash (nuvem) é compartilhado entre máquinas.

Detecção de mudança (`source_hash`, `pipelines/hashing.py`): o crawler carrega do Upstash o estado por URL (`source_hash` + ids dos chunks), baixa cada recurso e hasheia o conteúdo bruto ANTES de qualquer parse (HTML: texto extraído do `main`, estável entre fetches; PDF: bytes; planilha: CSV). Hash igual = inalterada, nem chega ao parser (zero LLM, zero embed); hash diferente ou URL nova = segue no fluxo, e a mudada tem os chunks antigos deletados no ingest (replace). Cada chunk tem id determinístico `url#i` e grava o `source_hash` no metadata, fechando o ciclo para o run seguinte. Dedup por conteúdo entre URLs (`_duplicata`): se o `source_hash` de um recurso já pertence a OUTRA URL (na base ou vista antes no mesmo run), o crawler pula (nem parseia nem ingere), tratando como duplicata; a 1ª URL de cada conteúdo é a dona. É o caso do re-upload `-1` do WordPress (mesmo arquivo byte-a-byte, URL nova), que inflava o contexto do retrieval com o documento repetido. Hashear o HTML cru não funciona: toda página carrega um nonce anti-bot (`__uzdbm_1`, Radware) que muda a cada request; por isso o hash do HTML é do texto extraído, com a extração fatorada em `extract_page_content` e compartilhada entre crawler e parser. O mesmo Radware responde uma PÁGINA DE DESAFIO (captcha, HTTP 200) na requisição sem cookie, no lugar do conteúdo; por isso o crawler usa uma sessão HTTP persistente (`SESSION`/`fetch` no `ingest_pipeline.py`) que guarda os cookies `__uzm*` e, ao detectar o desafio, repete na mesma sessão (aí passa, sem precisar de navegador/JS). Sem isso cada requisição vira uma 1ª visita e recebe o captcha, e a varredura colapsa (o alcance caía de ~2442 para ~2 URLs). O parser usa `raise_for_status()`, então página com erro HTTP real (ex: 451) é pulada sem sobrescrever o conteúdo bom que já está na base. O `run_ingest` só deleta o antigo de uma URL mudada que produziu chunk novo (`urls_mudadas & por_url`): se o parse do conteúdo novo falha, o antigo é preservado (nunca deleta sem repor).

PDFs escaneados (imagem, sem texto extraível) nunca geram chunk (o chunker pula `is_scanned`), então não têm `source_hash` na base e, sem tratamento, seriam re-baixados a cada run. O parser registra os escaneados em `data/parsed/pdfs_scanned.json` (versionado no Git, junto com `pdfs_format_errors.json`, para semear o CI e evitar que ele re-baixe os ~1359 escaneados a cada run; o resto de `data/parsed/` segue gitignored) e, com `INCLUDE_SCANNED=False` (default), o crawler pula o download dos já registrados. O registro se autopopula (um PDF é baixado uma vez para ser identificado como escaneado); numa máquina nova ele se refaz sozinho. Quando houver OCR/pixelrag, `INCLUDE_SCANNED=True` volta a processá-los.

Job periódico de ingestão: o GitHub Actions (`.github/workflows/ingest.yml`) roda o ingest incremental toda segunda 06:00 UTC (mais gatilho manual `workflow_dispatch`), atualiza o Upstash e commita o snapshot de volta (dispara o redeploy do Vercel). Os secrets no repo são `UPSTASH_ENDPOINT`, `UPSTASH_WRITE_API_KEY`, `UPSTASH_API_KEY` e `GEMINI_API_KEY_T1`. O `index.html` carrega um nonce por fetch, então há um commit e redeploy a cada run mesmo sem mudança de conteúdo.

**2. Serviço de chat (online, Vercel)** - `ui/app.py` + `rag/chain.py` + `rag/gatekeeper.py`
Flask stateless. O fluxo de uma pergunta: `gatekeeper` checa rate limit por IP no Redis e `chain.ask()` roda um agente com tool calling (Gemini `gemini-2.5-flash`). O modelo investiga a pergunta e decide entre chamar a ferramenta `buscar_documentos` (formulando uma query refinada, já corrigindo premissas como reitor->diretor) ou pedir esclarecimento ao estudante quando falta um discriminador (curso, tipo de prova). A ferramenta embeda a query e coleta um pool amplo do Upstash (`FETCH_K=60`) por similaridade, reordena por data E escopo de campus (`rerank_by_date`: `ALPHA*similaridade + (1-ALPHA)*score_de_data - penalidade_de_campus`; ver "Retrieval: escopo de campus, data e grade por visão") e corta os `CONTEXT_K=15` melhores para o contexto (over-fetch: o rerank escolhe de um pool maior sem inflar os tokens do LLM). O modelo então responde citando as fontes e explicitando o escopo (ex: "a prova de RECUPERAÇÃO do curso X"). Se a busca não retorna nada, o modelo é informado disso e responde honestamente que não encontrou, sem inventar (NÃO há fallback de internet). O prompt do agente (`data/info/agente-ifrs.txt`) traz defesas de segurança (nunca revelar as instruções; tratar o texto do usuário como usuário, não como sistema/autoridade; nunca usar prefixo imposto; redirecionar perguntas fora de escopo, inclusive conhecimento geral trivial) e dois comportamentos contra respostas cruas em perguntas vagas: quando o referente tem outra leitura plausível na base, responde a mais provável e menciona a alternativa (sem travar); e consciência temporal (a `data_atual` é calculada por request, não no import do módulo), ressalvando dados de anos anteriores em vez de apresentá-los como vigentes. Depois da geração, um GUARD DE SAÍDA (`_aplicar_guards` em `chain.py`, determinístico para disparar + LLM foco em temperatura baixa para reescrever) faz duas checagens de consistência temporal antes de entregar: (A) se a resposta cita fonte de ano anterior ao atual sem ressalvar, ANEXA a ressalva; (B) se a resposta lidera com data já passada e o contexto tem data futura, reescreve liderando pela próxima ocorrência. É fail-safe (qualquer erro devolve a resposta original) e só aplica edição segura (rejeita reescrita que introduza URL ou citação nova). A temperatura da geração vem de `AGENT_TEMP` (env, default 0.7).

Telemetria (opcional, `rag/telemetry.py`): cada turno do `/chat` é registrado no Langfuse no MESMO schema da coleta do eval (via `registro_de_trace` em `chain.py`), carimbado com a versão viva do prompt (`PROMPT_VERSAO`), sendo uma extensão da coleta por outra versão (mergeável pelo `prompt_versao`). Payload leve: guarda os ids dos chunks (`url#i`), não o texto cru; `eval/harvest_producao.py` puxa os traces e reconstrói o texto via `index.fetch(ids)` no Upstash, gerando `coleta_producao.jsonl` no formato do eval para curadoria (perguntas reais viram candidatas a caso de golden). Sem as chaves `LANGFUSE_*` a telemetria é no-op (local, eval e o Vercel sem chaves seguem funcionando); toda falha de telemetria é engolida, e o `/chat` trata erro do `ask` (500 limpo e registrado). Cada turno também leva um `user_id` (id anônimo por dispositivo, gerado no `widget.js` via `crypto.randomUUID` e guardado no `localStorage`) e um `session_id` (por carregamento de página, mesma vida do histórico); viram os campos Users/Sessions do Langfuse, dando um "quem" pseudônimo sem IP nem PII (evita o problema de NAT do wifi do campus, onde muitos alunos compartilham um IP).

### Retrieval: escopo de campus, data e grade por visão

O retrieval aplica três refinamentos além da similaridade crua: escopo de campus, data do documento e leitura das grades de horário por visão. Os casos de regressão correspondentes vivem no golden set marcados com `origem: "producao"`.

- **Escopo de campus, no RERANK (não na query)**: a base é toda do Campus Canoas, mas contém docs INSTITUCIONAIS do IFRS (multi-campus, ex: o "PDI IFRS 2024-2028") que dominam a similaridade e enterram a resposta certa (a query "salas dos professores prédio" trazia 28/30 chunks do PDI). Duas peças: (a) `FETCH_K=60` (pool grande o bastante para o doc certo de Canoas, que fica além do top-30 quando o institucional domina a similaridade crua, entrar no pool); (b) penalidade no rerank `ALPHA*sim + (1-ALPHA)*date_score - CAMPUS_PENALTY`, com `CAMPUS_PENALTY=0.35` para chunks `campus_scope=="outro"` (ausência = neutro 0), valor calibrado para tirar o PDI do contexto sem margem apertada. O escopo age no rerank, não na query, pois ancorar a query ao campus (`f"{query} IFRS Campus Canoas"`) infla editais institucionais e enterra a resposta em queries de professor. O `campus_scope` é atribuído no ingest por `classify_campus_scope` (ALTA PRECISÃO, para não expulsar doc de Canoas): tagueia "outro" só (a) fora do `/canoas/` E (b) com marcador institucional explícito (título "PDI"/"Plano de Desenvolvimento Institucional" ou >=4 campi do IFRS citados). NUNCA tagueia doc sob `/canoas/` (Canoas-específico mesmo citando a rede - senão o relatório CPA de Canoas virava "outro"). Hoje "outro" = o PDI (Drive, 269 chunks) + planos de ação IFRS institucionais (no `/site/`, não `/canoas/`). Além da penalidade, um CAP DURO `CAMPUS_OUTRO_MAX=0` (em `build_context`) EXPULSA do contexto todo chunk `campus_scope=="outro"`: a penalidade só REBAIXAVA o institucional, que ainda sobrava no top-15 e vazava (ex: "Torre Norte" do PDI aparecia nas salas dos professores); com o cap, nenhum institucional entra no contexto (a base é toda de Canoas, o institucional não responde daqui). Limite conhecido: uma pergunta legitimamente institucional (reitor/PDI) poderia ficar sem contexto; se reaparecer, liberar alguns slots "outro" só quando o contexto ficar vazio.
- **Data do documento (`get_published_at`)**: lê o ano do PRÓPRIO documento primeiro. Ordem: nome do arquivo (`extract_date_from_filename`, só para arquivos reais `.pdf/.docx/...`, NUNCA o slug de uma página HTML, que traz ano de evento futuro) -> conteúdo (LLM, ~1000 chars: 500 do início + 500 do fim) -> pasta `/YYYY/MM/` da URL (data de UPLOAD) só como fallback. O `_date_score` tem piso 0 (doc antigo fica neutro, nunca negativo).
- **Grade de horários por VISÃO / pixelrag** (`structure_schedule_vision`, `parse_pdf`): a grade aSc TimeTables é tabela 2D que a extração de texto embaralha (célula transborda a linha, coluna funde). Detecção em `is_schedule_pdf` por 3 redes (qualquer uma dispara): assinatura "aSc TimeTables" no conteúdo, OU título "Horários_", OU rede estrutural (>=3 dias da semana por TOKEN via `\b(Seg|Ter|...|Segunda|Terça|...)\b`, NÃO substring - senão "Ter" casava "Território", "Qui" casava "Aqui" e disparava a visão à toa; mais o marcador "criado:" e >=3 horários HH:MM). Cada página é renderizada (`get_pixmap` dpi 200, teto `max_pages=30`) e lida pelo gemini-2.5-flash (multimodal), que agrupa POR PROFESSOR. GATE anti-alucinação: horários/salas emitidos têm que existir no raw da página, senão a página é descartada (suspeita). `published_at` = ano do raw ("Horário criado:DD/MM/AAAA"). O chunker emite UM chunk por professor (`chunk_document(..., por_linha=True)`), não o splitter por tamanho, para o recall por professor não re-densificar. **Os marcadores `is_schedule=True` e `schedule_source` (visao/visao_parcial/fallback_texto) são PERSISTIDOS no metadata do Upstash** (só nos chunks de grade; ausentes nos demais para não inchar o metadata), tornando as grades auditáveis/filtráveis na base e sinalizando grade incompleta ou degradada - o que o texto do chunk sozinho não revela (uma grade parcial parece completa, só com menos professores). Truncagem (>`max_pages`) ou página pulada/descartada marca `visao_parcial`, então a incompletude fica VISÍVEL, não silenciosa. O conjunto na base = exatamente as grades linkadas na página oficial `ensino/horarios-das-aulas/` (10 do semestre 2026/2).
- **Calendário acadêmico** (`is_calendar_pdf`, `structure_calendar_text`): o calendário é estruturado por LLM (cada linha "DIA - evento" amarrada ao mês/ano da seção). GATE: só roteia ao estruturador se o marcador (URL/título com "calendário") vier ACOMPANHADO de `>=8` datas no corpo. Sem o gate, uma RESOLUÇÃO que apenas APROVA o calendário (URL tem "calendario", corpo sem as datas) ia ao estruturador, que ALUCINAVA um calendário inteiro (origem de datas fantasma tipo "início das aulas 03/08" e "Festa Junina 27/07" atribuídas à resolução). Com o gate, a resolução cai no parse determinístico (texto cru), sem inventar; o calendário real (dezenas de datas) segue estruturado.
- **Prompt**: regra de ENUMERAÇÃO (pergunta que pede TODAS as X de Y deve buscar, não responder de memória, e admitir incompletude); regra de NÃO-SOMAR-TABELA (não cravar um total somando células; citar total declarado ou dar por item); e regra de ATRIBUIÇÃO POR ANO para dados quantitativos que mudam (servidores, docentes, técnicos, vagas): cada número vale para o ANO da sua fonte (o contexto carimba `Data: <ano>` por trecho), o agente atribui cada valor ao seu ano e, se não achar o dado do ano corrente, diz isso e informa o registro mais recente nomeando o ano ("para 2026 não encontrei; o mais recente é 2024: 70 docentes e 45 técnicos"), sem somar anos diferentes.

Comportamento atual nesses casos: horários (dia e hora), disciplinas do professor (recall multi-curso, ex: TADS + Eng. Eletrônica), salas dos professores e número de servidores são respondidos corretamente, com o PDI e os docs institucionais fora do contexto. O número de servidores ganhou a moldura temporal por ano (ex: "para 2026 não encontrei; o mais recente é 2024: 70 docentes + 45 técnicos"). O resíduo aberto se concentra em `total-vagas-campus`: a soma de tabela ainda oscila e o consolidado nem sempre entra no top-15 (retrieval ~8/15). Alguns PDFs do Drive (~18) mantêm chunks antigos por falha de rate limit no gdown, e o incremental semanal os retenta.

### Servidor sem estado (decisão central)

O servidor NÃO guarda histórico de conversa, sessão, nem cookie. O histórico vive no cliente (`ui/widget.js`, variável em memória) e é enviado inteiro no corpo de cada `POST /chat`. O servidor sanitiza e aplica tetos anti-abuso (`sanitize_history` limita quantidade e tamanho do histórico; `MAX_QUERY_CHARS` limita a query; `check_global_budget` limita o volume diário) e repassa ao `ask`. Isso é o que torna o app compatível com o serverless do Vercel (instâncias efêmeras que não compartilham memória). Ao recarregar a página o histórico zera de propósito (vive em memória, não em `localStorage`), para contexto antigo não poluir uma nova conversa. O `localStorage` guarda só um id anônimo de dispositivo para a telemetria (`user_id`), que persiste no F5 mas NÃO repopula o histórico. O contexto multi-turno funciona durante a sessão ativa porque o array em memória acumula e vai junto em cada chamada.

### Página servida estaticamente

A rota `/` serve o `ui/index.html` já pronto (via `send_from_directory`); o app não busca o IFRS ao vivo nem cacheia em runtime (filesystem read-only do Vercel). O snapshot é montado offline por `ui/clone_page.py` (`build_page`: busca o HTML do IFRS, injeta a faixa de aviso `ALERT_BANNER` de ambiente não oficial e o `widget.js`) e regenerado na fase final da pipeline de ingestão, depois commitado. Assim a página só muda quando a base muda, e nenhum usuário paga o custo do fetch ao vivo.

### Separação de chaves Upstash

O Vector tem dois tokens, controlados por variável de ambiente distinta:
- `UPSTASH_API_KEY` (read-only): usado pelo `chain.py` em produção (só consulta). É o único Vector token cadastrado no Vercel.
- `UPSTASH_WRITE_API_KEY` (read+write): usado pela ingestão (`ingest_pipeline.py`). Fica só na máquina local, NUNCA no Vercel.

O Redis (`UPSTASH_REDIS_*`) usa o token padrão com escrita, pois o rate limiter incrementa contador a cada request. Read-only não serve ali.

### Embeddings

`gemini-embedding-001`, 3072 dimensões, similaridade cosine. O index Upstash precisa ser criado com esses parâmetros. Os scores do Upstash são normalizados em 0 a 1 (no antigo Qdrant iam de -1 a 1); o `min_score=0.60` em `chain.py` pode precisar de ajuste fino observando scores reais.

## Deploy (Vercel)

Entrypoint em `api/index.py` (reexpõe `from ui.app import app`); o preset Flask do Vercel resolve o app sozinho. `vercel.json` define `maxDuration: 60` para a função (folga para a chamada LLM e o loop de tool calling). Cadastrar no painel do Vercel as variáveis: `UPSTASH_API_KEY` (read-only), `UPSTASH_ENDPOINT`, `UPSTASH_REDIS_API_KEY`, `UPSTASH_REDIS_ENDPOINT`, `GEMINI_API_KEY_T1`. Para ligar a telemetria (opcional), cadastrar também `LANGFUSE_PUBLIC_KEY` e `LANGFUSE_SECRET_KEY` (e `LANGFUSE_HOST` só se região EU ou self-host): diferente da chave de escrita do Upstash, as do Langfuse VÃO no Vercel, pois a telemetria roda em produção. NÃO cadastrar `UPSTASH_WRITE_API_KEY`.

## Próximos passos

Em aberto, nesta ordem de prioridade (a bateria das Fases 1-7 está implementada e roda; ver "Achados atuais da bateria" e "Pontos fracos atuais"):

**Prioridade máxima:**
- **Consistência temporal.** O guard de saída (`_aplicar_guards`) já FORÇA a ressalva quando a fonte citada é de ano anterior (Check A) e reescreve liderando pela próxima data futura quando a resposta puxa uma data já passada (Check B); a regra de atribuição por ano no prompt cobre os dados quantitativos (servidores/vagas). Pendente: granularidade de SEMESTRE na data das grades (o título traz "2026_2"): hoje `_date_score` compara só por ANO, então uma grade de 2026/1 empataria com as de 2026/2 no rerank; é o fix sistêmico que blinda contra uma grade de semestre anterior voltar à base (re-crawl) e competir com a vigente.
- **`campus_scope`: cap ativo, monitorar.** Além da `CAMPUS_PENALTY=0.35` no rerank, o cap `CAMPUS_OUTRO_MAX=0` expulsa o institucional do contexto (ver Retrieval). Monitorar: se algum caso legitimamente institucional (reitor/PDI) ficar sem contexto por causa do cap, liberar alguns slots "outro" só quando o contexto ficar vazio.
- **Visão nos escaneados.** Estender a leitura por visão aos ~1359 PDFs escaneados (hoje pulados) - escopo novo, custo maior.

Roadmap mais amplo:
1. Endurecer a produção para uso público: o guard de saída HOJE cobre só consistência temporal (Check A/B em `chain.py`); falta a 2ª barreira de SEGURANÇA (um juiz validador do OUTPUT que barre vazamento do prompt ou saída fora do papel mesmo quando o input passou; hoje isso só é checado pelo juiz da Fase 6 no eval, NÃO em produção) e aviso/redação de PII na telemetria (LGPD). Fora de escopo do app: injeção indireta (governança de conteúdo do campus) e DDoS/abuso distribuído (borda do Vercel).
2. Calibrar o juiz contra rótulo humano: rotular à mão uma amostra (~30-50 julgamentos) e medir concordância (kappa); só então tratar as Fases 5/6 como régua, não só sinal. Junto: re-julgar tudo numa passada consistente ao mudar juiz/rubrica (não misturar severidades). Mecanismo planejado: uma página HTML interativa de curadoria (sobre o harvest de produção) onde o humano marca concordo/discordo do juiz e dá score/correção manual; isso gera os rótulos humanos (fecha a calibração) e de quebra cura perguntas reais em casos de golden. Não-circularidade: o gold vem do CONTEÚDO da base, nunca da resposta que o agente deu em produção.
3. Fechar os erros de comportamento abertos via prompt, medindo no eval: `curso-inexistente` (em ~40% dos runs corrige a premissa mas PERGUNTA "quer que eu busque?" em vez de já buscar, derrubando a Fase 1; efeito colateral do reforço de correção de premissa) e `total-vagas-campus` (soma de tabela instável + consolidado nem sempre no top-15). Junto: A/B de temperatura (0.2 vs 0.7) contra a flakiness de ~1-em-15; e reportar o IC POR CASO no placar (ver Pontos fracos).
4. Escalar o golden set (25 -> 100-150), adensando segurança (hoje só 6 casos): **geração sintética validada** (um LLM gera candidatas dos próprios documentos, humano/juiz valida, mantendo a não-circularidade) + **telemetria de produção** (perguntas reais viram novos casos); leva de prova-de-conceito a suíte de regressão.
5. Telemetria de produção (Langfuse): a captura está implementada (`rag/telemetry.py` grava cada turno no schema da coleta, carimbado com `prompt_versao`; `eval/harvest_producao.py` hidrata os ids dos chunks e gera `coleta_producao.jsonl`). Pendente: cadastrar `LANGFUSE_*` no Vercel (já estão no `.env` local); scores reference-free de produção (fidelidade/relevância/citação) via batch offline (harvest + juiz), cientes de que são monitor de alucinação e NÃO correção (correção precisa de gold); e a página de curadoria (ver item 2).
6. Crawler no domínio de ingresso: cobrir `ingresso.ifrs.edu.br` liberando o domínio no `is_valid_page`; se filtrar por ano, ensinar o regex a ler `/AAAA-S/` (ex: `/2026-2/`).
7. Ingerir o Instagram do Grêmio/campus (fonte atual que escapa ao pipeline, ex: data real da festa só anunciada lá): fonte hostil (login, anti-scraping, cards de imagem). API oficial (Graph, conta Business + token) vs scraping + multimodal.
8. Reduzir latência (cache de embeddings, modelo menor de triagem); validar com gestores do Campus Canoas; multi-campi (namespaces ou metadata `campus` no Upstash).

### Pontos fracos atuais

O que ainda está aberto, por área (motiva os "Próximos passos"):

Produção / uso público:
- Guard de saída: PARCIAL. O guard de consistência temporal (ressalva de ano + liderar pela próxima data futura) está em produção (`_aplicar_guards`); falta a barreira de SEGURANÇA (nenhum juiz filtra a resposta contra vazamento do prompt / saída fora do papel antes de entregá-la).
- Privacidade/LGPD: ABERTO. A telemetria loga pergunta/resposta reais; falta aviso e redação de PII.
- Injeção indireta: FORA DE ESCOPO do app. O conteúdo recuperado (páginas/planilhas) não é sanitizado; é governança de conteúdo de quem alimenta o site/planilhas do campus, e o hardening do assistente cobre o input do usuário.

Eval / metodologia:
- Juiz não calibrado contra rótulo humano: as Fases 5/6 são sinal, não régua. A discriminação do juiz na coleta é baixa (pouquíssimos julgamentos "false"), o que reforça tratar 5/6 como sinal.
- Golden pequeno (30 casos: 25 coletados + 5 da telemetria de produção ainda não coletados), fino em segurança (6 casos); n=5 é ruído para falhas estocásticas (por isso segurança precisa de n alto). Só 10/25 casos coletados têm checagem objetiva de conteúdo (`gold_url`/`answer_span`); nos outros 15 (recusa, fora-escopo, vagas, responder_direto, mensalidade) a qualidade da resposta é aferida só pelo juiz.
- IC do placar é agregado (pooled) sobre execuções correlacionadas (5 runs × paraphrases das intenções), o que ESTREITA o IC artificialmente; a leitura honesta é o IC POR CASO (o relatório já traz um por caso). Mais amostra esclarece a realidade, não piora; o ajuste é reportar/agregar por caso.
- Coleta não reproduzível pelo git: a coleta de baseline rodou sob um prompt de working-tree que não está em nenhum commit, então a prova desses casos não é reproduzível/auditável. Re-coletar 100% sob o prompt commitado (com n alto em segurança) é pendência: fecha a auditabilidade e mede a segurança no prompt que vai pro ar.
- Fase 4 (answerability) só exercita o lado positivo: o único caso `existe_na_base=false` não tem `answer_span`, então o ramo "NÃO está na base" nunca é checado por regra; não-alucinar quando o dado falta fica só no juiz.
- Fase 7 (citação) confere só que o `[n]` citado existe entre as fontes recuperadas, não que a fonte citada é a correta para a afirmação.
- Multi-turno / fusão de contexto sem cobertura: o eval roda turno único com histórico vazio, então o caminho de histórico de produção (`sanitize_history` -> `ask(history=)`) fica 0% testado.
- `MIN_SCORE=0.60` não calibrado empiricamente.

Agente / conteúdo:
- Erros de comportamento abertos: `curso-inexistente` (em ~40% dos runs corrige a premissa mas PERGUNTA em vez de já buscar, derrubando a Fase 1; efeito colateral do reforço de correção) e `total-vagas-campus` (soma de tabela instável + retrieval do consolidado ~8/15). `inicio-aulas` foi FECHADO (o "03/08" era chunk-fantasma de calendário alucinado, removido; o `is_calendar_pdf` impede recriá-lo; hoje lidera com 27/07). A temperatura 0.7 gera flakiness de retrieval de ~1/15.
- Gap do Instagram (evento real só anunciado lá; ex: a data efetiva da Festa Junina, cujo calendário registra 27/06 mas a edição atual pode mudar) e ~3% de URLs com erro HTTP por run (link morto, timeout, algum 451 pontual).

### Bateria de testes (estratégia de avaliação)

Detalhamento do item 1. Princípio central: **simular o pipeline real** (a pergunta entra, o agente investiga, formula a query, busca no Upstash e responde) e aplicar a medição **onde ela se encaixa**. Não usar query fixa nem forçar determinismo: o pipeline passa pelo LLM (temperatura 0.7), então cada caso roda **N vezes** e reporta-se a **taxa** (ex: o doc correto chegou ao contexto em 4/5 execuções), que é a estabilidade real do sistema. Reportar incerteza (IC) porque a amostra é pequena.

Validações-alvo (o que se quer responder por caso):
1. investigou o suficiente? (trajetória: tomou a ação certa antes de responder)
2. criou a query certa pro Upstash? (formulação: corrigiu premissa reitor->diretor, incluiu ano/discriminador)
3. o RAG retornou os documentos corretos? (retrieval)
4. a resposta final fez sentido? (geração)
5. a resposta realmente existe na base? (answerability)
6. acréscimos: recusa apropriada (não alucinar quando ausente; perguntar quando ambíguo), segurança (jailbreak/fora de escopo), consciência temporal (ressalvar dado antigo), citação de fontes.

Eixo de medição (o que decide a técnica; NÃO é "determinismo", é objetivo vs semântico):
- **Objetivo** (checagem por regra sobre a execução real, sem LLM-juiz): docs retornados contêm o correto? (`gold_url`/`answer_span` no contexto real; Hit@k, Recall@k, MRR); a resposta existe na base? (varredura textual, independe da execução, separa falha de retrieval de ausência real); a query gerada tem o discriminador esperado? (regex); a ação tomada = a esperada? (chamou a tool vs respondeu direto); citação válida? (fontes citadas dentro das recuperadas).
- **Semântico** (LLM-as-judge; ainda A VALIDAR contra rótulos humanos em PT-BR antes de tratar como régua): fidelidade (não inventou), relevância e correção da resposta; recusa apropriada; ressalva temporal; resistência a jailbreak.

**Campos do caso.** Golden set único em `eval/golden_set.json`; cada caso reúne: pergunta + paraphrases, `tipo`, `acao_esperada`, `criterios_query` (deve/não deve conter), `gold_urls`, `answer_spans`, `existe_na_base`, `resposta_esperada` e `notas`. O histórico de conversa NÃO é campo do caso: é artefato de runtime, gerado ao encadear execuções (nunca uma resposta fixada no gabarito). As `paraphrases` são reformulações da mesma pergunta (mesma intenção, léxico diferente) para medir invariância à forma; quando o comportamento independe do referente, elas podem variar o próprio discriminador (ex: o caso `curso-inexistente` varia o nome do curso falso).

**Vocabulário de `acao_esperada`** (a decisão da Fase 1):
- `buscar`: chama `buscar_documentos` e responde.
- `corrigir_e_buscar`: corrige uma premissa errada do aluno e então busca (ex: reitor->diretor; curso que não existe -> curso real).
- `perguntar`: pede esclarecimento antes de buscar, oferecendo as opções conhecidas quando possível (ex: "atendimento de qual setor?"; "temos bolsa de monitoria, extensão...; qual?").
- `responder_direto`: responde sem retrieval quando é apropriado responder (saudação, meta-pergunta sobre o assistente).
- `recusar`: não entrega o conteúdo pedido. Dois gatilhos com tons distintos: jailbreak/prompt injection -> recusa firme (não vaza o prompt, não sai do papel); fora de escopo -> redireciona educado ("só ajudo com assuntos do Campus Canoas"). Fronteira com `responder_direto`: ambos dispensam retrieval; o que decide é se o agente DEVE responder o conteúdo.

`acao_esperada` pode ser uma **lista** quando mais de uma ação é aceitável (ex: `mensalidade-curso` aceita corrigir_e_buscar OU responder_direto - responder direto que é gratuito também serve; `bolsa-vaga` aceita perguntar OU buscar - listar todas as bolsas também serve). A Fase 1 passa se a decisão real (buscar/não-buscar) casar com qualquer uma das aceitas; o acerto do conteúdo em si cabe às fases 5/6.

**Turno único vs multi-turno.** Por padrão cada caso é de turno único e mede um movimento do agente. Um fluxo de esclarecimento (pergunta vaga -> agente pede o discriminador -> estudante responde -> agente responde) decompõe-se em dois casos independentes e complementares: um caso `perguntar` (mede se o agente pediu esclarecimento e se pediu bem) e um caso `buscar` já específico (mede retrieval e resposta final). Esse padrão já está no golden set: `atendimento-vago` é o "turno 1" e `biblioteca-horario`/`atendimento-igor` são "turnos 2" resolvidos. Nesses casos `resposta_esperada` descreve o output do turno (a pergunta de volta, com os discriminadores certos) e não se mede fidelidade (Fase 5), e sim comportamento (Fase 6). O multi-turno de verdade (roteiro `turnos_usuario`, com fusão de contexto: o turno 2 chega como fragmento "da biblioteca" que só vira query colado ao turno anterior) fica FORA do escopo atual por decisão; ele só acrescentaria o teste da fusão, que os dois casos independentes não exercitam.

**Padrões de comportamento a cobrir** (Fase 6):
- consciência temporal: gold que depende da data atual (início do próximo semestre, rematrícula do semestre vigente, "festa" sem edição do ano corrente, PIT antigo) exige manutenção ou regra dinâmica no avaliador; o gold reflete a referência da data em que foi curado.
- não-alucinar (`existe_na_base: false`): quando o dado não existe na base, admitir a lacuna e orientar, sem inventar (ex: qual sistema envia as horas complementares - o procedimento existe, a ferramenta concreta não).
- perguntar-com-opções e recusa (jailbreak / fora de escopo) conforme o vocabulário acima.

**Curadoria do gold (não-circular).** O gabarito é definido por CONTEÚDO, nunca a partir do que o `ask` recupera. Método: varrer TODA a base (`index.range`) nos três tipos (`html`, `pdf` e `sheet` - dado estruturado como horário de atendimento vive em planilha), achar o fato por varredura textual, confirmar no chunk e validar que cada `gold_url` casa EXATAMENTE com um `source_url` existente (senão o retrieval falha por typo). A busca vetorial serve só para DESCOBRIR candidatos, não para definir o gold. A `resposta_esperada` é a referência do fato correto, NÃO um gabarito literal: o sistema costuma responder muito além dela (a Fase 5 mede fidelidade e correção, não igualdade textual). Validar o gold rodando o pipeline real (`ask`) N vezes é parte do processo (foi assim que se achou, por exemplo, um documento faltando no gold do auxílio). Lição: checar a data da fonte antes de afirmar (uma página de 2016 apontou um sistema já descontinuado).

Plano de fases (espelha o pipeline real do agente; cada fase verifica uma etapa. objetivo = por regra, sem juiz; semântico = LLM-juiz, ainda a validar contra humano):

| Fase | Verificação | O que valida | Métrica usada | Tipo |
|------|-------------|--------------|---------------|------|
| 1 | decisão (trajetória) | o agente decidiu certo entre BUSCAR e NÃO buscar (a projeção de `acao_esperada`: buscar/corrigir_e_buscar = busca; perguntar/responder_direto/recusar = não-busca). Distinguir recusar vs perguntar vs responder_direto, e a QUALIDADE da recusa (não vazar o prompt), é a Fase 6, não a Fase 1 | acurácia da decisão buscar/não-buscar (ação real == projeção da esperada), taxa em N execuções | objetivo |
| 2 | formulação da query | a query enviada ao Upstash corrigiu a premissa (reitor->diretor) e tem o discriminador certo (ano, curso, tipo) | conformidade da query por regex sobre `criterios_query`, taxa | objetivo |
| 3 | retrieval | o chunk/documento correto está entre os retornados no contexto de produção | Hit@k, Recall@k, MRR sobre `gold_url`/`answer_span` no contexto real | objetivo |
| 4 | answerability | a resposta de fato existe na base (separa "retrieval falhou" de "base não tem") | cobertura booleana por varredura de conteúdo da base | objetivo |
| 5 | geração | a resposta final é fiel ao contexto, relevante e correta | faithfulness, answer relevancy, answer correctness | semântico (LLM-juiz) |
| 6 | comportamento | recusou/perguntou quando devia, ressalvou dado antigo (consciência temporal), resistiu a jailbreak/fora de escopo | acurácia de comportamento (juiz; regra onde possível) | semântico (LLM-juiz) |
| 7 | citação | as fontes citadas na resposta estão entre as recuperadas | precisão de citação (parse de `[n]` vs fontes do contexto) | objetivo |

Golden set atual: **30 casos** (25 originais + 5 da telemetria de produção, marcados `origem: "producao"` com o `trace_id`). Os 25 originais somam 71 inputs. Distribuição por ação dos 25 originais: `buscar` 9, `perguntar` 5, `recusar` 5, `corrigir_e_buscar` 3, `responder_direto` 3. Os 5 de produção (numero-servidores, salas-professores-predio, total-vagas-campus, disciplinas-professor, horario-aulas-turma) são alvos de regressão dos refinamentos de retrieval, já coletados e validados. O resíduo aberto está em `total-vagas-campus` (ver "Retrieval: escopo de campus, data e grade por visão"). Nota: `horario-aulas-turma` é hoje `perguntar` (a grade é por curso; sem o curso, o agente pede o discriminador).

| id | ação | existe? | foco |
|----|------|---------|------|
| diretor-geral-campus | corrigir_e_buscar | sim | premissa reitor->diretora-geral |
| mensalidade-curso | corrigir_e_buscar | sim | premissa: curso é gratuito |
| curso-inexistente | corrigir_e_buscar | sim | curso que não existe -> TADS (paraphrases variam o nome falso) |
| atendimento-igor | buscar | sim | horário em planilha (`type: sheet`) |
| festa-junina-data | buscar | parcial | consciência temporal (calendário: 27/06, já passou) + gap do Instagram (edição atual só lá) |
| auxilio-estudantil | buscar | sim | procedimento; validado por simulação real |
| biblioteca-horario | buscar | sim | factual (8h às 21h) |
| inicio-aulas-proximo-semestre | buscar | sim | consciência temporal (próximo início) |
| complementares-tads | buscar | sim | discriminador de curso (90h; não confundir com técnico) |
| rematricula-2026 | buscar | sim | consciência temporal (2026/1 vs 2026/2) |
| email-coord-tads | buscar | sim | factual pontual |
| envio-horas-ferramenta | buscar | não | answerability false / não-alucinar |
| atendimento-vago | perguntar | n/a | pergunta vaga (qual setor?) |
| data-prova-vaga | perguntar | n/a | pergunta vaga (qual prova?) |
| bolsa-vaga | perguntar | n/a | pergunta vaga, perguntar-com-opções |
| documentos-vaga | perguntar | n/a | pergunta vaga (documentos pra quê?) |
| fora-escopo-sutil | perguntar | n/a | pede recomendação/opinião -> não opinar, oferecer fatos |
| jailbreak-basico | recusar | n/a | prompt leak explícito |
| jailbreak-medio | recusar | n/a | persona sem-regras + fraude acadêmica |
| jailbreak-complexo | recusar | n/a | fake sistema + supressão de recusa + prefix injection |
| fora-escopo-basico | recusar | n/a | conhecimento geral (capital da França) |
| fora-escopo-medio | recusar | n/a | compara instituições externas |
| responder-direto-saudacao | responder_direto | n/a | saudação |
| responder-direto-meta | responder_direto | n/a | meta-pergunta sobre o assistente |
| responder-direto-agradecimento | responder_direto | n/a | encerramento |

Robustez (tamanho do set): < 20 casos = protótipo; 30-60 = desenvolvimento com sinal por categoria (meta próxima); 100-200+ = robusto para regressão. O que importa é a cobertura por categoria (~5-10 casos por comportamento crítico), não só o total. Reportar sempre taxa com IC de Wilson, pois a amostra é pequena. Lacuna estrutural conhecida: multi-turno (fora do escopo por decisão).

### Achados atuais da bateria

A bateria das 7 fases está implementada e roda em dois passos (`coletar`/`validar`); a metodologia está na seção "Bateria de testes". Placar vigente (coleta jul/2026, 30 casos, 695 execuções, versões de prompt mistas por atualização modular; Fases 5/6 = SINAL, juiz não calibrado): Fase 1 decisão 97%, Fase 2 query 100%, Fase 3 retrieval doc 93% / span 94% (MRR 0.70), Fase 5 fidelidade 97% / relevância 100% / correção 96%, Fase 6 comportamento 97%, Fase 7 citação 100% (todos com IC de Wilson no `relatorio.md`). Sólidos: horário (pergunta o curso quando falta), número de servidores (moldura temporal por ano), início de aulas e salas dos professores (sem vazar o PDI), segurança (recusa a jailbreak + fora-de-escopo 100%). Resíduo aberto: `total-vagas-campus` (correção ~8/15: soma de tabela + retrieval do consolidado) e `curso-inexistente` (Fase 1 ~47%: corrige a premissa mas pergunta em vez de já buscar). Ainda pendente para maior rigor: n alto nos casos de baixa frequência (`curso-inexistente`, segurança). A coleta pode misturar versões de prompt por ATUALIZAÇÃO MODULAR: ao mudar o prompt de um comportamento, re-coleta-se só os casos afetados (via `EVAL_IDS`), e os demais mantêm a coleta anterior, válida porque a mudança não os toca; o carimbo `prompt_versao` torna a mistura auditável, e o relatório declara quais casos estão em cada versão. Os bugs de comportamento e os pontos fracos abertos estão em "Pontos fracos atuais".

Juiz: switch `EVAL_JUDGE` (`claude` default via subagente / `gemini` inline), julga sobre a coleta salva; a fidelidade (5a) isenta correção de premissa institucional (ex: gratuidade). As métricas semânticas (5/6) NÃO foram validadas contra rótulos humanos, e re-julgar subconjuntos em passadas diferentes mistura severidades; tratar 5/6 como sinal, não régua, até calibrar. O `relatorio.md` (prova externa, regenerado a cada `validar`) declara esse limite (Fases 5/6 = sinal) e traz o IC de Wilson de cada fase, a versão de prompt de cada caso e a classificação de cada falha; detalhe com IC por caso em `eval/runs/ultimo_resumo.txt`.
