"""Ingestão (fase 5): estado da base por URL, ids determinísticos url#i, embedding em
lote e upsert/replace no Upstash.

Replace seguro: só deleta os chunks antigos de uma URL mudada que PRODUZIU chunk novo;
se o parse do conteúdo novo falhou, o antigo é preservado (nunca deleta sem repor).
"""

import time

from google.genai import errors as genai_errors

from pipelines.config import REINGEST, google_client, index


def carregar_estado():
    # estado do que ja esta no Upstash, por URL: {url: {"source_hash":..., "ids":[...], "published_at":...}}
    # o source_hash e o published_at sao iguais em todos os chunks de uma mesma URL
    estado = {}
    cursor = ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for v in res.vectors:
            m = v.metadata or {}
            u = m.get("source_url", "")
            e = estado.setdefault(u, {"source_hash": m.get("source_hash"), "ids": [], "published_at": m.get("published_at")})
            e["ids"].append(v.id)
        cursor = res.next_cursor
        if cursor == "":
            break
    return estado

def _agrupar_e_anotar(chunks):
    # agrupa chunks por URL e da a cada um o id deterministico url#i (o source_hash ja vem no chunk)
    por_url = {}
    for c in chunks:
        por_url.setdefault(c["source_url"], []).append(c)
    for url, cs in por_url.items():
        for i, c in enumerate(cs):
            c["id"] = f"{url}#{i}"
    return por_url

def ingest_chunks(chunks, batch_size=100):
    # cada chunk ja traz "id" e "source_hash"; embeda e faz upsert
    total = len(chunks)
    for i in range(0, total, batch_size):
        batch = chunks[i:i + batch_size]
        texts = [c["text"] for c in batch]

        # embeda com retry
        for attempt in range(5):
            try:
                result = google_client.models.embed_content(
                    model="gemini-embedding-001",
                    contents=texts
                )
                break
            except genai_errors.APIError as e:
                # rate limit (429) do Gemini: espera e tenta de novo; qualquer outro erro sobe
                if e.code != 429:
                    raise
                wait = 30 * (attempt + 1)
                print(f"  Rate limit, aguardando {wait}s...")
                time.sleep(wait)

        vectors = []
        for chunk, embedding in zip(batch, result.embeddings):
            meta = {
                "text":         chunk["text"],
                "source_url":   chunk["source_url"],
                "title":        chunk["title"],
                "type":         chunk["type"],
                "published_at": chunk.get("published_at"),
                "source_hash":  chunk.get("source_hash"),
                "campus_scope": chunk.get("campus_scope"),
                "curso_escopo": chunk.get("curso_escopo"),
            }
            # marcadores de grade so nos chunks de grade; a chave ausente nos demais mantem o metadata enxuto
            if chunk.get("is_schedule"):
                meta["is_schedule"]     = True
                meta["schedule_source"] = chunk.get("schedule_source")
            vectors.append((chunk["id"], embedding.values, meta))

        index.upsert(vectors=vectors)
        print(f"Inseridos {min(i + batch_size, total)}/{total} chunks")

def run_ingest(chunks, estado, urls_mudadas):
    print("\n" + "="*60)
    print("FASE 5 — INGESTÃO NO UPSTASH")
    print("="*60)

    por_url = _agrupar_e_anotar(chunks)
    todos   = [c for cs in por_url.values() for c in cs]

    if REINGEST:
        index.reset()
        print("Index zerado.")
        ingest_chunks(todos)
        print("Ingestão concluída.")
        return

    # replace das paginas mudadas: deleta os chunks antigos antes de inserir os novos.
    # a deteccao (novo / mudou / inalterado) ja aconteceu no crawler, via source_hash.
    # so deleta o antigo de uma URL mudada que PRODUZIU chunk novo (esta em por_url); se o
    # parse do conteudo novo falhou (PDF corrompido, planilha vazia), o antigo e preservado
    # e a URL segue "mudada" ate um run futuro conseguir reprocessa-la (nunca deleta sem repor).
    reinseridas = urls_mudadas & set(por_url)
    ids_deletar = [i for url in reinseridas for i in estado.get(url, {}).get("ids", [])]
    for i in range(0, len(ids_deletar), 1000):
        index.delete(ids=ids_deletar[i:i + 1000])
    if ids_deletar:
        print(f"Removidos {len(ids_deletar)} chunks antigos de {len(reinseridas)} paginas mudadas")
    nao_repostas = urls_mudadas - reinseridas
    if nao_repostas:
        print(f"AVISO: {len(nao_repostas)} URLs mudadas sem conteudo novo valido; antigo preservado")

    ingest_chunks(todos)
    print(f"Ingestão concluída: {len(por_url)} URLs (novas + mudadas), {len(todos)} chunks")
