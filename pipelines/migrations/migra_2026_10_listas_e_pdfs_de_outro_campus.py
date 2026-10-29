"""Migração 2026-10: aplica à base as regras de 29/10/2026 de lista de pessoas e de documento de outro campus.

Até 29/10/2026 a regra de lista de pessoas não reconhecia o número de inscrição com dois dígitos depois do ponto
("70.54-59") nem a tabela de resultado sem palavra de situação do vocabulário (a lista de um candidato só, com
"Matrícula Condicional"; as seleções do campus, com o número de ordem no lugar da inscrição). O PDF do portal de
ingresso com timbre ou título de outro campus e nenhuma citação a Canoas (convocação, orientação de início das aulas,
quadro de vagas só de outros campi) entrava inteiro, e a página do
portal com o campus no título fora do formato "| Campus X -" também. Esta migração aplica ao que já está gravado
as mesmas decisões da ingestão, na mesma ordem: página HTML pelo campus (`campus_da_pagina`, o crawler decide antes
do parser); PDF pela lista de pessoas (`destino`) e depois pelo timbre (`campus_do_pdf`). A lista do processo
seletivo de ingresso sai da base e entra em data/parsed/pdfs_pii.json; a de outra origem fica com um chunk só, o
aviso com o link (source_hash mantido, a ingestão o vê inalterado); o documento de outro campus sai da base.
Documento dos editais da Reitoria (metadata edital_url) fica de fora: aquele módulo decide a lista com o sinal do
LLM, que esta migração não tem.

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY e GEMINI_API_KEY_T1):
  python -m pipelines.migrations.migra_2026_10_listas_e_pdfs_de_outro_campus backup     # salva vetor e metadata dos chunks afetados (sem escrita)
  python -m pipelines.migrations.migra_2026_10_listas_e_pdfs_de_outro_campus aplicar    # apaga, troca pelo aviso e registra
  python -m pipelines.migrations.migra_2026_10_listas_e_pdfs_de_outro_campus verificar
  python -m pipelines.migrations.migra_2026_10_listas_e_pdfs_de_outro_campus reverter   # regrava os chunks do backup
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

from pipelines.chunker import campus_da_pagina, campus_do_pdf, e_canoas
from pipelines.config import PII_PATH, google_client, index
from pipelines.listas_nomes import aviso_lista, destino
from pipelines.urls import to_drive_view_url

BACKUP = RAIZ / "backups" / "migracao_2026_10_listas_e_pdfs_de_outro_campus.json"


def _ordem(identificador):
    sufixo = identificador.rsplit("#", 1)[-1]
    return int(sufixo) if sufixo.isdigit() else 0


def _de_outro_campus(campus):
    return bool(campus) and not e_canoas(campus)


def _varrer():
    # {url: {"ids": [...], "acao": "fora"|"aviso"|"outro_campus", "motivo": str}} pela regra da ingestao
    documentos, cursor = {}, ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for vetor in res.vectors:
            meta = vetor.metadata or {}
            if meta.get("type") not in ("pdf", "html") or meta.get("edital_url"):
                continue
            doc = documentos.setdefault(meta.get("source_url", ""), {"partes": [], "title": meta.get("title", ""),
                                                                      "type": meta["type"], "aviso": False})
            doc["partes"].append((_ordem(vetor.id), vetor.id, meta.get("text") or ""))
            doc["aviso"] = doc["aviso"] or bool(meta.get("lista_nomes"))
        cursor = res.next_cursor
        if cursor == "":
            break

    # pagina: campus antes de tudo; pdf: lista de pessoas e depois o timbre. o aviso ja gravado so passa pelo campus
    afetados = {}
    for url, doc in documentos.items():
        partes = sorted(doc["partes"])
        ids = [identificador for _, identificador, _ in partes]
        texto = "\n".join(t for _, _, t in partes)
        if doc["type"] == "html":
            campus = campus_da_pagina(url, doc["title"], texto)
            if _de_outro_campus(campus):
                afetados[url] = {"ids": ids, "acao": "outro_campus", "motivo": campus}
                continue
            if not doc["aviso"]:
                decisao, motivo = destino(texto, url, doc["title"], pagina=True)
                if decisao:
                    afetados[url] = {"ids": ids, "acao": decisao, "motivo": motivo}
            continue
        if not doc["aviso"]:
            decisao, motivo = destino(texto, url, doc["title"])
            if decisao:
                afetados[url] = {"ids": ids, "acao": decisao, "motivo": motivo}
                continue
        campus = campus_do_pdf(url, texto)
        if _de_outro_campus(campus):
            afetados[url] = {"ids": ids, "acao": "outro_campus", "motivo": campus}
    return afetados


def _embed(texto):
    for tentativa in range(5):
        try:
            return google_client.models.embed_content(model="gemini-embedding-001", contents=[texto]).embeddings[0].values
        except genai_errors.APIError as erro:
            if erro.code != 429:
                raise
            time.sleep(30 * (tentativa + 1))
    raise RuntimeError("embedding indisponivel")


def _resumo(afetados):
    contagem = {}
    for doc in afetados.values():
        contagem[doc["acao"]] = contagem.get(doc["acao"], 0) + 1
    return contagem


def backup():
    # o backup de uma migracao ja aplicada e o unico jeito de reverte-la: nunca e sobrescrito
    if BACKUP.exists():
        sys.exit(f"backup já existe em {BACKUP}; apague-o à mão só se a migração não foi aplicada")
    afetados = _varrer()
    ids = [identificador for doc in afetados.values() for identificador in doc["ids"]]
    salvos = []
    for inicio in range(0, len(ids), 100):
        salvos += [{"id": vetor.id, "vector": list(vetor.vector), "metadata": vetor.metadata}
                   for vetor in index.fetch(ids=ids[inicio:inicio + 100], include_vectors=True, include_metadata=True)
                   if vetor]
    BACKUP.parent.mkdir(exist_ok=True)
    BACKUP.write_text(json.dumps({"afetados": afetados, "chunks": salvos}, ensure_ascii=False), encoding="utf-8")
    print(f"backup: {len(salvos)} chunks de {len(afetados)} documentos -> {BACKUP}")
    print(f"  por ação: {_resumo(afetados)}")


def aplicar():
    dados = json.loads(BACKUP.read_text(encoding="utf-8"))
    metadata = {chunk["id"]: chunk["metadata"] for chunk in dados["chunks"]}
    registro = set(json.loads(PII_PATH.read_text(encoding="utf-8"))) if PII_PATH.exists() else set()
    apagar, avisos = [], 0
    for url, doc in dados["afetados"].items():
        if doc["acao"] != "aviso":
            apagar += doc["ids"]
            if doc["acao"] == "fora":
                registro.add(to_drive_view_url(url))
            continue
        # um chunk so, no id do primeiro, com o aviso, a marca e o metadata do documento (source_hash mantido)
        primeiro = metadata[doc["ids"][0]]
        aviso = aviso_lista(url, primeiro.get("title", ""))
        novo = {chave: valor for chave, valor in primeiro.items() if chave not in ("texto_busca", "is_calendar")}
        novo["text"] = aviso
        novo["lista_nomes"] = True
        index.upsert(vectors=[(f"{url}#0", _embed(aviso), novo)])
        apagar += [identificador for identificador in doc["ids"] if identificador != f"{url}#0"]
        avisos += 1
    for inicio in range(0, len(apagar), 1000):
        index.delete(ids=apagar[inicio:inicio + 1000])
    PII_PATH.write_text(json.dumps(sorted(registro), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"aplicado: {avisos} listas trocadas pelo aviso, {len(apagar)} chunks apagados, "
          f"registro de PII com {len(registro)} URLs")


def verificar():
    afetados = _varrer()
    print(f"documentos ainda afetados na base: {len(afetados)}, {sum(len(d['ids']) for d in afetados.values())} chunks "
          f"{_resumo(afetados)}")


def reverter():
    dados = json.loads(BACKUP.read_text(encoding="utf-8"))
    avisos = [f"{url}#0" for url, doc in dados["afetados"].items()
              if doc["acao"] == "aviso" and f"{url}#0" not in doc["ids"]]
    if avisos:
        index.delete(ids=avisos)
    for inicio in range(0, len(dados["chunks"]), 100):
        index.upsert(vectors=[(chunk["id"], chunk["vector"], chunk["metadata"])
                              for chunk in dados["chunks"][inicio:inicio + 100]])
    registro = set(json.loads(PII_PATH.read_text(encoding="utf-8"))) if PII_PATH.exists() else set()
    registro -= {to_drive_view_url(url) for url, doc in dados["afetados"].items() if doc["acao"] == "fora"}
    PII_PATH.write_text(json.dumps(sorted(registro), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"revertido: {len(dados['chunks'])} chunks regravados")


if __name__ == "__main__":
    modos = {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}
    if len(sys.argv) != 2 or sys.argv[1] not in modos:
        sys.exit(f"uso: python -m pipelines.migrations.migra_2026_10_listas_e_pdfs_de_outro_campus {{{'|'.join(modos)}}}")
    modos[sys.argv[1]]()
