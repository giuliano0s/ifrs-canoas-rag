"""Migração 2026-07: cleanup de relevância + escopo de curso na base.

Três operações sobre a base atual (a re-run com range grande inchou a base e trouxe conteúdo
irrelevante; e o curso_escopo passou a existir mas os chunks já ingeridos não o têm):

  1. DELETE do conteúdo externo (domínio não-IFRS, ex: filesusr.com): nem é IFRS, e não é capado
     por campus_scope, então pode vazar numa resposta. Removido (backup com vetor p/ reverter).
  2. TAG campus_scope="outro" em página de OUTRO campus hoje neutra (título/URL cita "campus <X>"
     não-Canoas e não cita Canoas): cap, não delete. História e períodos antigos ficam (recência
     do rerank desprioriza), só conteúdo de campus diferente é despriorizado.
  3. RE-TAG curso_escopo nos docs que DEFINEM um curso (classify_curso_escopo), doc-nível: fecha o
     erro de aplicar regra de um curso a outro (TCC da GPI dado como TADS). Só metadata (PATCH), sem
     re-embed.

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY):
  python -m pipelines.migrations.migra_2026_07_escopo_curso_e_cleanup backup     # identifica + salva (mostra contagens)
  python -m pipelines.migrations.migra_2026_07_escopo_curso_e_cleanup aplicar
  python -m pipelines.migrations.migra_2026_07_escopo_curso_e_cleanup verificar
  python -m pipelines.migrations.migra_2026_07_escopo_curso_e_cleanup reverter
"""

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from dotenv import load_dotenv

load_dotenv(RAIZ / ".env")
from upstash_vector import Index
from upstash_vector.types import MetadataUpdateMode

from rag.cursos_escopo import classify_curso_escopo

index = Index(url=os.environ["UPSTASH_ENDPOINT"], token=os.environ["UPSTASH_WRITE_API_KEY"])
BACKUP = RAIZ / "backups" / "migracao_2026_07_escopo_curso_e_cleanup.json"

_CAMPI_OUTROS = ["alvorada", "bento", "caxias", "erechim", "farroupilha", "feliz", "ibirub",
                 "osorio", "osório", "porto alegre", "porto-alegre", "restinga", "rio grande",
                 "rio-grande", "rolante", "sertao", "sertão", "vacaria", "verano", "veranó",
                 "viamao", "viamão", "zona norte", "zona-norte"]


def _idx(cid):
    m = re.search(r"#(\d+)$", str(cid))
    return int(m.group(1)) if m else 0


def _eh_externo(url):
    host = urlparse(url or "").netloc.lower()
    return bool(host) and not host.endswith("ifrs.edu.br") and "google.com" not in host


def _eh_outro_campus(title, url):
    # título/URL referencia um campus NÃO-Canoas ("campus <X>") e não cita Canoas. alta precisao.
    blob = ((title or "") + " " + (url or "")).lower()
    if "canoas" in blob:
        return False
    return any(re.search(r"campus[\s_-]+" + re.escape(c), blob) for c in _CAMPI_OUTROS)


def _plano():
    # varre a base (metadata) e monta o plano: external a deletar, campus a re-taguear, curso a re-taguear
    por_url = defaultdict(list)
    chunks = {}
    cursor = ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for v in res.vectors:
            m = v.metadata or {}
            chunks[str(v.id)] = m
            por_url[m.get("source_url", "")].append(str(v.id))
        cursor = res.next_cursor
        if cursor == "":
            break

    externos = [cid for cid, m in chunks.items() if _eh_externo(m.get("source_url", ""))]
    campus_retag = [cid for cid, m in chunks.items()
                    if m.get("campus_scope") is None
                    and _eh_outro_campus(m.get("title", ""), m.get("source_url", ""))]
    # curso: classifica por doc (título + 2 primeiros chunks) e aplica a todos os chunks do doc
    curso_retag = {}  # id -> slug
    for url, ids in por_url.items():
        ids.sort(key=_idx)
        head = " ".join(chunks[i].get("text", "") for i in ids[:2])
        slug = classify_curso_escopo(chunks[ids[0]].get("title", ""), head, url)
        if slug:
            for i in ids:
                if chunks[i].get("curso_escopo") != slug:
                    curso_retag[i] = slug
    return chunks, externos, campus_retag, curso_retag


def backup():
    chunks, externos, campus_retag, curso_retag = _plano()
    # external: precisa do VETOR para reverter um delete
    ext_full = []
    for k in range(0, len(externos), 100):
        for v in index.fetch(ids=externos[k:k + 100], include_vectors=True, include_metadata=True):
            if v is not None:
                ext_full.append({"id": str(v.id), "vector": v.vector, "metadata": v.metadata or {}})
    dados = {
        "externos": ext_full,
        "campus_retag": [{"id": i, "old": chunks[i].get("campus_scope")} for i in campus_retag],
        "curso_retag": [{"id": i, "old": chunks[i].get("curso_escopo"), "new": s} for i, s in curso_retag.items()],
    }
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    docs_curso = len({chunks[i].get("source_url") for i in curso_retag})
    print(f"PLANO (salvo em {BACKUP.name}):")
    print(f"  1. DELETE externo: {len(ext_full)} chunks")
    print(f"  2. TAG outro-campus (neutro->outro): {len(campus_retag)} chunks")
    print(f"  3. RE-TAG curso_escopo: {len(curso_retag)} chunks de {docs_curso} docs")
    # amostra para conferencia
    print("  amostra outro-campus:", [chunks[i].get("source_url","")[-50:] for i in campus_retag[:4]])


def aplicar():
    dados = json.loads(BACKUP.read_text(encoding="utf-8"))
    ext_ids = [c["id"] for c in dados["externos"]]
    for k in range(0, len(ext_ids), 500):
        index.delete(ids=ext_ids[k:k + 500])
    print(f"deletados {len(ext_ids)} chunks externos")
    for c in dados["campus_retag"]:
        index.update(id=c["id"], metadata={"campus_scope": "outro"}, metadata_update_mode=MetadataUpdateMode.PATCH)
    print(f"re-tagueados {len(dados['campus_retag'])} chunks campus_scope=outro")
    for c in dados["curso_retag"]:
        index.update(id=c["id"], metadata={"curso_escopo": c["new"]}, metadata_update_mode=MetadataUpdateMode.PATCH)
    print(f"re-tagueados {len(dados['curso_retag'])} chunks curso_escopo")


def verificar():
    _, externos, campus_retag, curso_retag = _plano()
    print(f"pos-aplicacao (esperado ~0 nos dois primeiros):")
    print(f"  externos restantes: {len(externos)}")
    print(f"  outro-campus neutros restantes: {len(campus_retag)}")
    print(f"  curso a re-taguear restantes: {len(curso_retag)} (0 = tudo taggeado)")
    total = 0
    cursor = ""
    while True:
        res = index.range(cursor=cursor, limit=1000)
        total += len(res.vectors)
        cursor = res.next_cursor
        if cursor == "":
            break
    print(f"  total de chunks na base: {total}")


def reverter():
    dados = json.loads(BACKUP.read_text(encoding="utf-8"))
    vecs = [(c["id"], c["vector"], c["metadata"]) for c in dados["externos"]]
    for k in range(0, len(vecs), 100):
        index.upsert(vectors=vecs[k:k + 100])
    for c in dados["campus_retag"]:
        index.update(id=c["id"], metadata={"campus_scope": c["old"]}, metadata_update_mode=MetadataUpdateMode.PATCH)
    for c in dados["curso_retag"]:
        index.update(id=c["id"], metadata={"curso_escopo": c["old"]}, metadata_update_mode=MetadataUpdateMode.PATCH)
    print(f"revertido: {len(vecs)} externos restaurados, campus/curso re-tag desfeitos")


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    if modo not in ("backup", "aplicar", "verificar", "reverter"):
        print(__doc__); sys.exit(1)
    {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}[modo]()
