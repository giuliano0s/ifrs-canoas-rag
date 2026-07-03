"""
Correcao cirurgica de datas sujas: para as URLs em data/info/urls_redatar.json,
remove o published_at invalido dos arquivos parsed locais e apaga os chunks
correspondentes no Upstash. Depois, rodar o ingest_pipeline incremental
reparseia so essas URLs (agora sem data) e reingere com a datacao saneada.

Uso: .venv\\Scripts\\python.exe pipelines\\redatar_urls.py
"""

import json
import os
from pathlib import Path
from dotenv import load_dotenv
from upstash_vector import Index

load_dotenv()

# caminhos dos dados e da lista de alvos
DATA_DIR   = Path("data")
PARSED     = [DATA_DIR / "parsed" / "pages_parsed.json",
              DATA_DIR / "parsed" / "pdfs_parsed.json",
              DATA_DIR / "parsed" / "sheets_parsed.json"]
ALVOS_PATH = DATA_DIR / "info" / "urls_redatar.json"

index = Index(url=os.getenv("UPSTASH_ENDPOINT"), token=os.getenv("UPSTASH_WRITE_API_KEY"))

# carrega as URLs alvo
alvos = set(json.loads(ALVOS_PATH.read_text(encoding="utf-8")))
print(f"URLs a redatar: {len(alvos)}")

# limpa o published_at dessas URLs nos arquivos parsed locais
for path in PARSED:
    if not path.exists():
        continue
    docs = json.loads(path.read_text(encoding="utf-8"))
    limpos = 0
    for d in docs:
        if d.get("source_url") in alvos and "published_at" in d:
            del d["published_at"]
            d.pop("date_source", None)
            limpos += 1
    path.write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {path.name}: {limpos} datas removidas")

# coleta os ids dos chunks dessas URLs no Upstash e deleta
ids = []
cursor = ""
while True:
    res = index.range(cursor=cursor, limit=1000, include_metadata=True)
    for v in res.vectors:
        if (v.metadata or {}).get("source_url") in alvos:
            ids.append(v.id)
    cursor = res.next_cursor
    if cursor == "":
        break

if ids:
    for i in range(0, len(ids), 1000):
        index.delete(ids=ids[i:i + 1000])
    print(f"Chunks deletados do Upstash: {len(ids)}")
else:
    print("Nenhum chunk correspondente no Upstash.")

print("Pronto. Rode o ingest_pipeline (incremental) para reparsear e reingerir essas URLs.")
