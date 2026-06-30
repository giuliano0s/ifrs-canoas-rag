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
- Geração de respostas com Gemini
- Proteção contra abuso com rate limit por IP
- Backend stateless pronto para deploy serverless no Vercel

<br>
<img width="1553" height="659" alt="image" src="https://github.com/user-attachments/assets/2b8f92a7-3f96-4b37-ba33-d96bf26c0f07" />

---

# Tecnologias utilizadas

- Python 3.12+
- Flask
- LangChain
- Upstash Vector
- Upstash Redis
- Google Gemini API
- BeautifulSoup
- PyMuPDF
- Vercel

---

# Estrutura do projeto

```txt
.
├── data/                 # Dados brutos e processados
├── notebooks/            # Notebooks do pipeline
├── pipelines/            # Scripts principais de ingestão
├── rag/                  # Lógica RAG
├── ui/                   # Interface web
├── requirements.txt
└── README.md
```

---

# Funcionalidades

## 1. Crawling

O sistema percorre páginas do IFRS Canoas automaticamente e coleta:

- Conteúdo HTML
- Links internos
- Arquivos PDF

## 2. Extração de texto

O conteúdo coletado é convertido em texto estruturado.

PDFs são processados utilizando PyMuPDF.
Documentos tabelas de horários são reprocessados para melhor performance em embedding.
Extração de datas como metadados com gemini-2.5-flash-lite usando cabeçalhos e rodapés de documentos.

## 3. Chunking

Os textos são divididos em partes menores para melhorar:

- recuperação semântica
- precisão das respostas
- eficiência do embedding

## 4. Embeddings

Os chunks são transformados em vetores utilizando modelos da API Gemini.

## 5. Banco vetorial

Os embeddings são armazenados no Upstash Vector para busca vetorial.

## 6. Geração de respostas

Quando o usuário faz uma pergunta:

1. O rate limit por IP é verificado antes de qualquer chamada à LLM
2. O sistema busca os chunks mais relevantes
3. Monta um contexto
4. Envia o contexto ao modelo Gemini
5. Retorna uma resposta baseada nos documentos recuperados

Se nenhum chunk atinge o score mínimo, há um fallback de busca na internet via Gemini, priorizando fontes do IFRS.

## 7. Rate limit e sessão

O backend é stateless: não guarda histórico nem sessão. O histórico da conversa vive no cliente e é enviado em cada requisição, o que torna o serviço compatível com o ambiente serverless do Vercel.

O rate limit usa Upstash Redis (20 requisições por minuto por IP) para proteger contra abuso de chamadas à LLM.

A página servida ao usuário é um clone ao vivo do site do IFRS, montado a cada requisição (com cache em memória), e exibe uma faixa de aviso informando que é um ambiente de teste não oficial.

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

```bash
pip install -r requirements.txt
```

---

# Configuração

Crie um arquivo `.env` na raiz do projeto.

Exemplo:

```env
GEMINI_API_KEY_T1=sua_chave
UPSTASH_ENDPOINT=https://SEU_INDEX.upstash.io
UPSTASH_API_KEY=token_read_only
UPSTASH_WRITE_API_KEY=token_read_write
UPSTASH_REDIS_ENDPOINT=https://SEU_REDIS.upstash.io
UPSTASH_REDIS_API_KEY=token_redis
```

---

# Upstash Vector

O projeto utiliza exclusivamente Upstash Vector como banco vetorial.

Crie um index no Upstash com 3072 dimensões e função de similaridade cosine, e configure as credenciais no arquivo `.env`.

A busca em produção usa o token read-only (`UPSTASH_API_KEY`). A ingestão usa o token de escrita (`UPSTASH_WRITE_API_KEY`), que fica apenas no ambiente local e nao e cadastrado no Vercel.

---

# Pipeline de ingestão

Execute o script ou os notebooks responsáveis por:

1. Crawling
2. Parsing
3. Chunking
4. Embeddings
5. Indexação no Upstash

O script de pipeline está na pasta:

```txt
pipelines/
```

Os notebooks estão na pasta:

```txt
notebooks/
```

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

# Deploy (Vercel)

O projeto é servido como uma função serverless no Vercel. O entrypoint está em `api/index.py` e o `vercel.json` define o tempo máximo de execução da função.

Cadastre no painel do Vercel as variáveis de ambiente, usando o token read-only do Upstash Vector em `UPSTASH_API_KEY`:

- `UPSTASH_API_KEY` (read-only)
- `UPSTASH_ENDPOINT`
- `UPSTASH_REDIS_API_KEY`
- `UPSTASH_REDIS_ENDPOINT`
- `GEMINI_API_KEY_T1`

Não cadastre `UPSTASH_WRITE_API_KEY` no Vercel. Ela é usada apenas na ingestão local.

---

# Fluxo do sistema

```txt
Usuário
   ↓
Pergunta (com histórico do cliente)
   ↓
Rate limit por IP (Upstash Redis)
   ↓
Busca vetorial no Upstash
   ↓
Recuperação de chunks relevantes
   ↓
Construção do contexto
   ↓
Gemini gera resposta (ou fallback de internet)
   ↓
Resposta final
```

---

# Licença

Projeto desenvolvido para fins acadêmicos e educacionais.

