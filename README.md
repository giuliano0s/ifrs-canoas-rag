# IFRS Canoas RAG

Sistema RAG (Retrieval-Augmented Generation) desenvolvido para consulta inteligente de conteúdos do site do IFRS Canoas.

O projeto também gera uma versão mockada do site do IFRS Canoas com um widget de chat integrado no canto inferior direito da interface, permitindo iniciar conversas contextuais diretamente pela página web.

O projeto realiza:

- Crawling de páginas do IFRS
- Extração de conteúdo HTML e PDF
- Processamento e divisão de texto em chunks
- Geração de embeddings
- Indexação vetorial no Qdrant
- Recuperação semântica de contexto
- Geração de respostas com Gemini

<br>
<img width="1553" height="659" alt="image" src="https://github.com/user-attachments/assets/2b8f92a7-3f96-4b37-ba33-d96bf26c0f07" />

---

# Tecnologias utilizadas

- Python 3.12+
- Flask
- LangChain
- Qdrant
- Google Gemini API
- BeautifulSoup
- PyMuPDF
- Cerebras

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
Extração de datas como metadados com Llama3.1-8b usando cabeçalhos e rodapés de documentos.

## 3. Chunking

Os textos são divididos em partes menores para melhorar:

- recuperação semântica
- precisão das respostas
- eficiência do embedding

## 4. Embeddings

Os chunks são transformados em vetores utilizando modelos da API Gemini.

## 5. Banco vetorial

Os embeddings são armazenados no Qdrant para busca vetorial.

## 6. Geração de respostas

Quando o usuário faz uma pergunta:

1. O sistema busca os chunks mais relevantes
2. Monta um contexto
3. Envia o contexto ao modelo Gemini
4. Retorna uma resposta baseada nos documentos recuperados

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
GOOGLE_API_KEY=sua_chave
QDRANT_URL=https://SEU_CLUSTER.qdrant.io
QDRANT_API_KEY=sua_chave
```

---

# Qdrant Cloud

O projeto utiliza exclusivamente Qdrant Cloud como banco vetorial.

Crie um cluster no Qdrant Cloud e configure as credenciais no arquivo `.env`.

---

# Pipeline de ingestão

Execute o script ou os notebooks responsáveis por:

1. Crawling
2. Parsing
3. Chunking
4. Embeddings
5. Indexação no Qdrant

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

## Flask

```bash
python ui/app.py
```

A aplicação ficará disponível em:

```txt
http://localhost:5000
```

---

# Fluxo do sistema

```txt
Usuário
   ↓
Pergunta
   ↓
Busca vetorial no Qdrant
   ↓
Recuperação de chunks relevantes
   ↓
Construção do contexto
   ↓
Gemini gera resposta
   ↓
Resposta final
```

---

---

# Licença

Projeto desenvolvido para fins acadêmicos e educacionais.

