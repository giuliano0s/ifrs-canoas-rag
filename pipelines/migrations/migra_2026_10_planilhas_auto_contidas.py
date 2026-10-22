"""Migração 2026-10: planilhas relidas em registros auto-contidos e planilhas de outro campus apagadas.

Dois defeitos na base, ambos da ingestão de planilhas:
- o estruturador gravava o título e os cabeçalhos da planilha como registros próprios ("Horários de
  Atendimento ao Aluno", "Unidade: Campus Canoas/IFRS", "MANHÃ"), e os registros de verdade ficavam sem
  esse contexto ("Vale lanche (Cláusula 21) R$ 13,57", sem dizer que é do pregão de um serviço
  terceirizado). O trecho só com o título casava com qualquer pergunta que citasse o campus: nas
  consultas das coletas do golden e do controle, entrava no contexto de 125 de 399 buscas, em 1º lugar
  em 65, e o modelo colava a data dele (2026) em números de outros documentos (os 42 técnicos do
  relatório da CPA de 2025 apresentados como "dado de 2026"). O estruturador passa a devolver o assunto
  e o cabeçalho de cada seção à parte, e cada registro leva os dois (`structure_sheet`, em
  pipelines/parser_sheets.py);
- três grades de horário de outros campi (Técnico em Meio Ambiente e em Fabricação Mecânica, cursos que
  Canoas não oferece), ligadas em páginas do portal de ingresso, entravam no contexto das perguntas de
  horário de aula (85 trechos nas mesmas coletas). Planilha ligada fora do site do campus que não cita
  Canoas fica de fora (`e_do_campus`).

A releitura só substitui uma planilha quando toda linha do CSV volta como registro ou como cabeçalho;
senão, a planilha da base fica. A página onde cada planilha foi publicada vem da lista do último crawl
(data/raw/sheets.json); planilha da base fora dessa lista fica como está. A migração pode ser
reaplicada depois de uma correção no parser: o backup guarda só o estado anterior à primeira aplicação,
o plano é refeito, e o aplicar apaga o que está na base e não está no plano novo.

USO (nesta ordem; requer UPSTASH_WRITE_API_KEY, GEMINI_API_KEY_T1, OPENAI_API_KEY e LLM_PROVIDER=openai):
  python -m pipelines.migrations.migra_2026_10_planilhas_auto_contidas backup     # salva as planilhas da base (só na primeira vez), relê e monta o plano (sem escrita)
  python -m pipelines.migrations.migra_2026_10_planilhas_auto_contidas aplicar    # grava o plano
  python -m pipelines.migrations.migra_2026_10_planilhas_auto_contidas verificar
  python -m pipelines.migrations.migra_2026_10_planilhas_auto_contidas reverter   # volta as planilhas salvas no backup
"""

import json
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from dotenv import load_dotenv

load_dotenv(RAIZ / ".env")

from pipelines.chunker import chunks_de_planilha
from pipelines.config import SHEETS_PATH, index
from pipelines.crawler import download_sheet_csv
from pipelines.hashing import source_hash
from pipelines.ingest import ingest_chunks
from pipelines.parser_sheets import parse_sheet
from rag import llm

BACKUP = RAIZ / "backups" / "migracao_2026_10_planilhas_auto_contidas.json"
PLANO = RAIZ / "backups" / "migracao_2026_10_planilhas_auto_contidas_plano.json"


def _planilhas_na_base(com_vetor=True):
    # chunks de planilha agrupados por url
    por_url, cursor = {}, ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True, include_vectors=com_vetor)
        for v in res.vectors:
            md = v.metadata or {}
            if md.get("type") == "sheet":
                chunk = {"id": v.id, "metadata": md}
                if com_vetor:
                    chunk["vector"] = list(v.vector)
                por_url.setdefault(md["source_url"], []).append(chunk)
        cursor = res.next_cursor
        if not cursor:
            break
    return por_url


def _plano_da_planilha(url, parent):
    # chunks novos da planilha (lista vazia = apagar, por ser de outro campus), ou None para manter a base
    csv_text = download_sheet_csv(url)
    if not csv_text:
        return None, "download falhou, base mantida"
    planilha, motivo = parse_sheet({"url": url, "parent": parent, "csv_text": csv_text, "source_hash": source_hash(csv_text)})
    if motivo == "outro_campus":
        return [], "outro campus, apagada"
    if planilha is None or motivo:
        return None, f"{motivo}, base mantida"
    novos = chunks_de_planilha(planilha)
    for i, chunk in enumerate(novos):
        chunk["id"] = f"{url}#{i}"
    return novos, planilha["title"]


def backup():
    if llm.PROVIDER != "openai":
        sys.exit("defina LLM_PROVIDER=openai: a releitura e feita com o Luna")
    # o backup guarda o estado anterior a primeira aplicacao; uma releitura posterior reaproveita ele
    if BACKUP.exists():
        planilhas = json.loads(BACKUP.read_text(encoding="utf-8"))
        print(f"backup existente mantido: {sum(len(v) for v in planilhas.values())} chunks de {len(planilhas)} planilhas")
    else:
        planilhas = _planilhas_na_base()
        BACKUP.parent.mkdir(exist_ok=True)
        BACKUP.write_text(json.dumps(planilhas, ensure_ascii=False), encoding="utf-8")
        print(f"backup: {sum(len(v) for v in planilhas.values())} chunks de {len(planilhas)} planilhas -> {BACKUP}")

    # plano: chunks pelo caminho da ingestao, a partir do CSV atual de cada planilha
    pais = {s["url"]: s.get("parent") for s in json.loads(SHEETS_PATH.read_text(encoding="utf-8"))}
    plano = {}
    for url, chunks in planilhas.items():
        if url not in pais:
            print(f"  {url[-60:]}: fora da lista do ultimo crawl, base mantida")
            continue
        novos, origem = _plano_da_planilha(url, pais[url])
        if novos is not None:
            plano[url] = novos
        print(f"  {url[-60:]}: {len(chunks)} chunks -> {'mantidos' if novos is None else len(novos)} | {origem}")
    PLANO.write_text(json.dumps(plano, ensure_ascii=False), encoding="utf-8")
    print(f"plano de {len(plano)} planilhas -> {PLANO}")


def aplicar():
    plano = json.loads(PLANO.read_text(encoding="utf-8"))

    # insere os novos antes de apagar o que sobrou na base (nunca deleta sem repor); a sobra vem da base
    # atual, e nao do backup, para uma releitura reaplicada apagar tambem o que a anterior gravou
    novos = [c for chunks in plano.values() for c in chunks]
    if novos:
        ingest_chunks(novos)
    ids_novos = {c["id"] for c in novos}
    atuais = _planilhas_na_base(com_vetor=False)
    sobras = [c["id"] for url in plano for c in atuais.get(url, []) if c["id"] not in ids_novos]
    for i in range(0, len(sobras), 1000):
        index.delete(ids=sobras[i:i + 1000])
    print(f"aplicado: {len(novos)} chunks em {len(plano)} planilhas; {len(sobras)} ids antigos removidos")


def verificar():
    plano = json.loads(PLANO.read_text(encoding="utf-8"))
    na_base = _planilhas_na_base(com_vetor=False)
    for url, novos in plano.items():
        chunks = na_base.get(url, [])
        sem_assunto = sum(1 for c in chunks if not c["metadata"].get("text", "").startswith(f"{c['metadata'].get('title')}"))
        print(f"  {url[-60:]}: {len(chunks)}/{len(novos)} chunks na base, {sem_assunto} sem o assunto da planilha no texto")


def reverter():
    # o metadata volta sem campo nulo: o filtro de campus do atendimento descarta chunk com campo nulo
    planilhas = json.loads(BACKUP.read_text(encoding="utf-8"))
    plano = json.loads(PLANO.read_text(encoding="utf-8"))
    atuais = _planilhas_na_base(com_vetor=False)
    ids_atuais = [c["id"] for url in plano for c in atuais.get(url, [])]
    for i in range(0, len(ids_atuais), 1000):
        index.delete(ids=ids_atuais[i:i + 1000])
    vetores = [(c["id"], c["vector"], {k: v for k, v in c["metadata"].items() if v is not None})
               for url in plano for c in planilhas[url]]
    for i in range(0, len(vetores), 100):
        index.upsert(vectors=vetores[i:i + 100])
    print(f"revertido: {len(vetores)} chunks de {len(plano)} planilhas")


if __name__ == "__main__":
    modos = {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}
    if len(sys.argv) != 2 or sys.argv[1] not in modos:
        sys.exit(f"uso: python -m pipelines.migrations.migra_2026_10_planilhas_auto_contidas {{{'|'.join(modos)}}}")
    modos[sys.argv[1]]()
