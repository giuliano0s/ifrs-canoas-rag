# IFRS Canoas RAG

Sistema RAG (Retrieval-Augmented Generation) desenvolvido para consulta inteligente de conteúdos do site do IFRS Canoas.

O projeto também gera uma versão mockada do site do IFRS Canoas com um widget de chat integrado no canto inferior direito da interface, permitindo iniciar conversas contextuais diretamente pela página web.

O projeto realiza:

- Crawling de páginas do IFRS
- Extração de conteúdo HTML e PDF
- Processamento e divisão de texto em chunks
- Geração de embeddings
- Indexação vetorial no Upstash
- Recuperação semântica de contexto
- Geração de respostas com a OpenAI (GPT-6 Luna no atendimento, GPT-5.6 Luna na ingestão), com a resposta transmitida enquanto é escrita
- Etiqueta de curso em cada trecho (regras objetivas e um classificador por LLM), para a informação de um curso não ser dada como de outro
- Checagens determinísticas na saída (vazamento do prompt, ressalva de ano, soma inventada, nível da data, "hoje" e prazo contra a data do pedido)
- Proteção contra abuso: rate limit por dispositivo com teto por IP, tetos de tamanho de entrada e teto global de uso diário
- Telemetria opcional de produção com Langfuse (perguntas reais viram novos casos de teste)
- Backend stateless pronto para deploy serverless no Vercel

<br>
<img width="1553" height="659" alt="image" src="https://github.com/user-attachments/assets/2b8f92a7-3f96-4b37-ba33-d96bf26c0f07" />

---

# Tecnologias utilizadas

- Python 3.12+
- Flask
- langchain-text-splitters (chunker)
- Upstash Vector
- Upstash Redis
- OpenAI API (geração)
- Google Gemini API (embedding)
- Langfuse (telemetria de produção)
- BeautifulSoup
- PyMuPDF
- Vercel

---

# Estrutura do projeto

```txt
.
├── .github/workflows/    # CI: ingest diário (GitHub Actions)
├── api/                  # Entrypoint serverless (Vercel)
├── data/                 # Dados brutos e processados
├── eval/                 # Bateria de testes (golden set + validador)
├── pipelines/            # Ingestão modular (crawler, parsers, chunker, ingest, editais, cursos + orquestrador)
├── rag/                  # Lógica RAG
├── ui/                   # Interface web
├── requirements.txt
└── README.md
```

---

# Funcionalidades

## 1. Crawling

O sistema percorre o site do IFRS Canoas e o portal de ingresso (`ingresso.ifrs.edu.br`) e coleta:

- Conteúdo HTML
- Links internos
- Arquivos PDF
- Planilhas Google publicadas

Cada recurso é hasheado antes do parse: só o que é novo ou mudou segue para o LLM e o embedding. Conteúdo de outro campus e listas com nomes de pessoas ficam fora da base. Os editais da Reitoria que dizem respeito a Canoas entram por uma fase própria.

## 2. Extração de texto

O conteúdo coletado é convertido em texto estruturado.

PDFs são processados utilizando PyMuPDF.
Tabelas de horário (grades) são lidas pelas coordenadas do PDF, porque a extração de texto embaralha a tabela 2D; o conteúdo é reorganizado por professor, e a visão multimodal fica como reserva quando o layout muda.
Calendários, quadros de vagas e planilhas são estruturados por LLM, com conferência contra o texto original.
A data de publicação de cada documento é inferida por LLM a partir do início e do fim do próprio documento.

## 3. Chunking

Os textos são divididos em partes menores para melhorar:

- recuperação semântica
- precisão das respostas
- eficiência do embedding

Listas de registros (planilhas, grades) viram um trecho por registro; o calendário e o cronograma do ingresso viram um trecho por evento, entregue com o mês ou o cronograma inteiro.

## Curso de cada trecho

No fim de cada ingestão, uma fase etiqueta o curso de cada trecho da base: primeiro por regras objetivas (página do curso, sigla da turma na grade, linha do quadro de vagas, título de PPC ou regulamento), depois por um classificador LLM nos documentos que citam algum curso, e trecho a trecho nos documentos com uma seção por curso. O contexto mostra o curso de cada trecho, e a busca rebaixa trechos de outro curso quando a pergunta nomeia um.

## 4. Embeddings

Os chunks são transformados em vetores com o `gemini-embedding-001` (3072 dimensões).

## 5. Banco vetorial

Os embeddings são armazenados no Upstash Vector para busca vetorial.

## 6. Geração de respostas

Quando o usuário faz uma pergunta:

1. O rate limit por dispositivo é verificado antes de qualquer chamada à LLM
2. Um agente decide entre buscar nos documentos (escrevendo a consulta) ou pedir esclarecimento
3. A busca traz os candidatos do Upstash e os reordena por similaridade, data, campus e curso
4. O modelo responde a partir dos trechos recuperados, citando as fontes
5. As checagens de saída corrigem a resposta antes de ela chegar ao aluno

Se nenhum chunk atinge o score mínimo, o modelo é informado disso e responde honestamente que não encontrou, sem inventar e sem buscar na internet.

## 7. Rate limit e sessão

O backend é stateless: não guarda histórico nem sessão. O histórico da conversa vive no cliente e é enviado em cada requisição, o que torna o serviço compatível com o ambiente serverless do Vercel.

O rate limit usa Upstash Redis (20 requisições por minuto por dispositivo, com teto de 120 por minuto por IP, porque o wifi do campus compartilha IP) para proteger contra abuso de chamadas à LLM.

A página servida ao usuário é um snapshot estático do site do IFRS, gerado offline (e commitado) com o widget de chat e uma faixa de aviso já injetados, não buscado ao vivo a cada requisição. Assim a página só muda quando a base muda, e o filesystem read-only do Vercel não é um problema. A faixa avisa que é um ambiente de teste não oficial.

---

# Instalação

## 1. Clone o repositório

```bash
git clone https://github.com/giuliano0s/ifrs-canoas-rag.git
cd ifrs-canoas-rag
```

## 2. Crie um ambiente virtual

Windows:

```bash
py -3.12 -m venv .venv
.venv\Scripts\activate
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 3. Instale as dependências

Para desenvolvimento local (ingestão, avaliação e rodar o app), use as dependências de dev, que já incluem a produção:

```bash
pip install -r requirements-dev.txt
```

`requirements.txt` é só a produção (o que a função serverless do Vercel instala): roda o serviço de chat, mas não tem o crawler, o parser de PDF, o chunker nem a avaliação. Use-o apenas se for rodar exclusivamente o app.

---

# Configuração

Crie um arquivo `.env` na raiz do projeto.

Exemplo:

```env
OPENAI_API_KEY=sua_chave
GEMINI_API_KEY_T1=sua_chave
UPSTASH_ENDPOINT=https://SEU_INDEX.upstash.io
UPSTASH_API_KEY=token_read_only
UPSTASH_WRITE_API_KEY=token_read_write
UPSTASH_REDIS_ENDPOINT=https://SEU_REDIS.upstash.io
UPSTASH_REDIS_API_KEY=token_redis
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://us.cloud.langfuse.com
```

---

# Upstash Vector

O projeto utiliza exclusivamente Upstash Vector como banco vetorial.

Crie um index no Upstash com 3072 dimensões e função de similaridade cosine, e configure as credenciais no arquivo `.env`.

A busca em produção usa o token read-only (`UPSTASH_API_KEY`). A ingestão usa o token de escrita (`UPSTASH_WRITE_API_KEY`), que fica apenas no ambiente local e nao e cadastrado no Vercel.

---

# Pipeline de ingestão

Execute o script responsável por:

1. Crawling
2. Parsing
3. Chunking
4. Embeddings
5. Indexação no Upstash
6. Editais da Reitoria e curso de cada trecho
7. Snapshot da página

O orquestrador é `pipelines/ingest_pipeline.py`:

```bash
python pipelines/ingest_pipeline.py
```

A ingestão também roda **automaticamente todo dia** via GitHub Actions (`.github/workflows/ingest.yml`, incremental): atualiza a base no Upstash e commita o snapshot da página de volta, o que dispara o redeploy no Vercel. Roda o mesmo `pipelines/ingest_pipeline.py`; dá pra disparar na mão pela aba Actions ("Run workflow").

---

# Executando a interface

Rode como módulo a partir da raiz do projeto (os imports dependem dos pacotes `rag.` e `ui.`):

```bash
python -m ui.app
```

A porta padrão é 5000. Para usar outra (útil se a 5000 estiver ocupada), defina `PORT`:

```bash
# Windows (PowerShell)
$env:PORT=5050; python -m ui.app
```

A aplicação ficará disponível em `http://localhost:<porta>`.

---

# Avaliação (eval)

O projeto inclui uma bateria de testes automatizada que mede a qualidade do assistente sobre um conjunto curado de perguntas (o *golden set*). Como o pipeline passa pela LLM (não é determinístico), cada caso roda N vezes e reporta-se a **taxa de acerto** com intervalo de confiança, não um simples passa/falha.

A avaliação percorre 7 fases, espelhando o caminho real do agente:

1. **Decisão** - escolheu a ação certa (buscar, perguntar, corrigir uma premissa falsa, recusar)?
2. **Formulação da query** - a busca levou o discriminador certo (curso, ano, tipo)?
3. **Retrieval** - o documento correto foi recuperado?
4. **Answerability** - a resposta existe na base (separa "a busca falhou" de "a base não tem")?
5. **Geração** - a resposta é fiel ao contexto, relevante e correta?
6. **Comportamento** - recusou/perguntou quando devia, ressalvou dado antigo, resistiu a jailbreak?
7. **Citação** - as fontes citadas estão entre as recuperadas?

As fases 1-4 e 7 são checadas por regra (sem LLM). As fases 5-6 usam um juiz LLM.

Requer as dependências de desenvolvimento (`pip install -r requirements-dev.txt`). Roda em dois passos:

```bash
# 1) coleta: roda o agente e salva os traces (caro; só quando o agente muda)
EVAL_N=5 python -m eval.run_eval coletar

# 2) validação: aplica as fases sobre a coleta e gera o relatório (barato, instantâneo)
python -m eval.run_eval validar
```

O `validar` gera automaticamente `eval/runs/relatorio.md`: um placar por fase, o que o assistente acertou e o que errou (com o motivo de cada falha). O juiz das fases 5-6 tem dois modos, via a variável `EVAL_JUDGE`:

- **`claude`** (padrão): não usa API paga; o próprio Claude Code avalia as respostas.

Um conjunto de controle (`EVAL_CONJUNTO=controle`), que nunca ajusta prompt nem código, serve de régua de não-regressão.
- **`gemini`**: avalia chamando a API do Gemini, num comando só (`EVAL_JUDGE=gemini python -m eval.run_eval validar`).

---

# Deploy (Vercel)

O projeto é servido como uma função serverless no Vercel. O entrypoint está em `api/index.py` e o `vercel.json` define o tempo máximo de execução da função.

Cadastre no painel do Vercel as variáveis de ambiente, usando o token read-only do Upstash Vector em `UPSTASH_API_KEY`:

- `UPSTASH_API_KEY` (read-only)
- `UPSTASH_ENDPOINT`
- `UPSTASH_REDIS_API_KEY`
- `UPSTASH_REDIS_ENDPOINT`
- `GEMINI_API_KEY_T1` (embedding da pergunta)
- `OPENAI_API_KEY` (geração; o modelo vem da tabela `PERFIS` em `rag/llm.py`)
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` (opcionais; ligam a telemetria em produção)
- `GLOBAL_DAILY_MAX` (opcional; teto de requisições por dia, default 2000)

Não cadastre `UPSTASH_WRITE_API_KEY` no Vercel. Ela é usada apenas na ingestão local. Diferente dela, as chaves do Langfuse VÃO no Vercel, pois a telemetria roda em produção.

---

# Fluxo do sistema

```txt
Usuário
   ↓
Pergunta (com histórico do cliente)
   ↓
Rate limit por dispositivo (Upstash Redis)
   ↓
Agente decide: buscar ou pedir esclarecimento
   ↓
Busca vetorial no Upstash + reordenação (data, campus, curso)
   ↓
Construção do contexto (com data e curso de cada trecho)
   ↓
GPT-6 Luna gera a resposta em streaming (ou informa que não encontrou)
   ↓
Checagens de saída e fontes
   ↓
Resposta final
```

---

# Licença

O código deste projeto (crawler, pipeline de ingestão, RAG, widget e avaliação) está sob a licença **MIT**, copyright de **giuliano0s**, ver o arquivo [LICENSE](LICENSE). Na prática: qualquer um pode usar, copiar, modificar e redistribuir, **desde que mantenha o aviso de copyright e a licença** (ou seja, o crédito a giuliano0s) em todas as cópias.

A licença cobre apenas este código. O conteúdo do IFRS Campus Canoas (páginas, PDFs, textos e a identidade visual do site espelhado) pertence ao IFRS e **não** é licenciado aqui; o projeto é um ambiente de teste não oficial, desenvolvido para fins acadêmicos e educacionais.

