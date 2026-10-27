"""Migração 2026-10: tira da base as listas de pessoas gravadas antes do detector de pipelines/listas_nomes.py.

O filtro antigo só contava números de inscrição de 6 a 9 dígitos seguidos, e deixava passar a lista com o
número no formato "73.317-77", a de um ou dois candidatos e a de nomes sem número (seleções do campus).
Esta migração aplica ao que já está gravado a mesma decisão da ingestão (`destino`, em pipelines/listas_nomes.py):
o PDF de lista do processo seletivo de ingresso sai da base e é registrado em data/parsed/pdfs_pii.json, para o
crawler não baixá-lo de novo; as demais listas ficam com um único chunk, o aviso de que o resultado saiu e o
link, sem nomes, marcado com lista_nomes. O source_hash do documento é mantido, então a ingestão diária o vê como
inalterado e não o relê. A decisão olha o texto dos chunks juntados, e a ingestão o texto do documento: a
sobreposição do fatiamento (20%) repete parte do texto, o que não muda os números de inscrição distintos e
mexe pouco nas contagens e densidades.

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY e GEMINI_API_KEY_T1):
  python -m pipelines.migrations.migra_2026_10_listas_nominais backup     # salva vetor e metadata dos chunks das listas (sem escrita)
  python -m pipelines.migrations.migra_2026_10_listas_nominais aplicar    # apaga as do ingresso e troca as demais pelo aviso
  python -m pipelines.migrations.migra_2026_10_listas_nominais verificar
  python -m pipelines.migrations.migra_2026_10_listas_nominais reverter   # regrava os chunks do backup
"""

import json
import os
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from dotenv import load_dotenv

load_dotenv(RAIZ / ".env")

from google.genai import errors as genai_errors

from pipelines.config import PII_PATH, google_client, index
from pipelines.listas_nomes import aviso_lista, destino
from pipelines.urls import to_drive_view_url

BACKUP = RAIZ / "backups" / "migracao_2026_10_listas_nominais.json"


def _ordem(identificador):
    sufixo = identificador.rsplit("#", 1)[-1]
    return int(sufixo) if sufixo.isdigit() else 0


def _varrer():
    # {url: {"ids": [...], "destino": "fora"|"aviso", "motivo": str}} das listas de pessoas, pela regra unica
    # de pipelines/listas_nomes.py sobre o texto dos chunks juntados (o aviso ja gravado fica de fora)
    documentos, cursor = {}, ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for v in res.vectors:
            md = v.metadata or {}
            if md.get("type") not in ("pdf", "html") or md.get("lista_nomes"):
                continue
            doc = documentos.setdefault(md.get("source_url", ""), {"partes": [], "title": md.get("title", ""), "type": md["type"]})
            doc["partes"].append((_ordem(v.id), v.id, md.get("text") or ""))
        cursor = res.next_cursor
        if cursor == "":
            break
    listas = {}
    for url, doc in documentos.items():
        partes = sorted(doc["partes"])
        decisao, motivo = destino("\n".join(texto for _, _, texto in partes), url, doc["title"], pagina=doc["type"] == "html")
        if decisao:
            listas[url] = {"ids": [identificador for _, identificador, _ in partes], "destino": decisao, "motivo": motivo}
    return listas


def _embed(texto):
    for tentativa in range(5):
        try:
            return google_client.models.embed_content(model="gemini-embedding-001", contents=[texto]).embeddings[0].values
        except genai_errors.APIError as erro:
            if erro.code != 429:
                raise
            time.sleep(30 * (tentativa + 1))
    raise RuntimeError("embedding indisponivel")


def backup():
    listas = _varrer()
    ids = [identificador for lista in listas.values() for identificador in lista["ids"]]
    salvos = []
    for i in range(0, len(ids), 100):
        salvos += [{"id": v.id, "vector": list(v.vector), "metadata": v.metadata}
                   for v in index.fetch(ids=ids[i:i + 100], include_vectors=True, include_metadata=True) if v]
    BACKUP.parent.mkdir(exist_ok=True)
    BACKUP.write_text(json.dumps({"listas": listas, "chunks": salvos}, ensure_ascii=False), encoding="utf-8")
    fora = [url for url, lista in listas.items() if lista["destino"] == "fora"]
    print(f"backup: {len(salvos)} chunks de {len(listas)} listas -> {BACKUP}")
    print(f"  saem da base (PDF do ingresso): {len(fora)} | viram aviso: {len(listas) - len(fora)}")


def aplicar():
    dados = json.loads(BACKUP.read_text(encoding="utf-8"))
    metadata = {chunk["id"]: chunk["metadata"] for chunk in dados["chunks"]}
    registro = set(json.loads(PII_PATH.read_text(encoding="utf-8"))) if PII_PATH.exists() else set()
    apagar, avisos = [], 0
    for url, lista in dados["listas"].items():
        if lista["destino"] == "fora":
            apagar += lista["ids"]
            registro.add(to_drive_view_url(url))
            continue
        # um chunk so, no id do primeiro, com o aviso, a marca e o metadata do documento (source_hash mantido)
        primeiro = metadata[lista["ids"][0]]
        aviso = aviso_lista(url, primeiro.get("title", ""))
        novo = {chave: valor for chave, valor in primeiro.items() if chave not in ("texto_busca", "is_calendar")}
        novo["text"] = aviso
        novo["lista_nomes"] = True
        index.upsert(vectors=[(f"{url}#0", _embed(aviso), novo)])
        apagar += [identificador for identificador in lista["ids"] if identificador != f"{url}#0"]
        avisos += 1
    for i in range(0, len(apagar), 1000):
        index.delete(ids=apagar[i:i + 1000])
    PII_PATH.write_text(json.dumps(sorted(registro), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"aplicado: {avisos} listas trocadas pelo aviso, {len(apagar)} chunks apagados, registro de PII com {len(registro)} URLs")


def verificar():
    listas = _varrer()
    print(f"listas de pessoas na base: {len(listas)} documentos, {sum(len(lista['ids']) for lista in listas.values())} chunks")


def reverter():
    dados = json.loads(BACKUP.read_text(encoding="utf-8"))
    avisos = [f"{url}#0" for url, lista in dados["listas"].items()
              if lista["destino"] == "aviso" and f"{url}#0" not in lista["ids"]]
    if avisos:
        index.delete(ids=avisos)
    for i in range(0, len(dados["chunks"]), 100):
        index.upsert(vectors=[(chunk["id"], chunk["vector"], chunk["metadata"]) for chunk in dados["chunks"][i:i + 100]])
    registro = set(json.loads(PII_PATH.read_text(encoding="utf-8"))) if PII_PATH.exists() else set()
    registro -= {to_drive_view_url(url) for url, lista in dados["listas"].items() if lista["destino"] == "fora"}
    PII_PATH.write_text(json.dumps(sorted(registro), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"revertido: {len(dados['chunks'])} chunks regravados")


if __name__ == "__main__":
    modos = {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}
    if len(sys.argv) != 2 or sys.argv[1] not in modos:
        sys.exit(f"uso: python -m pipelines.migrations.migra_2026_10_listas_nominais {{{'|'.join(modos)}}}")
    modos[sys.argv[1]]()
