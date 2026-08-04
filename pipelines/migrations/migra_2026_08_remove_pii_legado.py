"""Migração 2026-08: remove da base as listas nominais LEGADAS (PII) do site do campus.

CONTEXTO: o gate de PII (`is_pii_nominal_list`) barra lista nominal de candidatos na INGESTÃO, mas
só a partir de jul/2026. Os documentos ingeridos ANTES dele (relação de candidatos homologados /
inscritos de seleção de bolsista e de professor substituto) seguem na base e podem ser citados numa
resposta, expondo nome de pessoa real. Antes de abrir o assistente a teste público, esses chunks
saem. Não é o mesmo caso das portarias de pessoal com SIAPE (servidor em ato oficial, dado
funcional público): o critério aqui é LISTA NOMINAL DE CANDIDATO.

CRITÉRIO (o mesmo do gate, aplicado sobre o que já está na base): densidade de número de inscrição
(>= 5 números de 6-9 dígitos distintos no chunk) OU marcador de lista nominal no título/URL
(homologad|classificad|inscrit|candidat|relacao final|selecionados) junto de >= 3 inscrições.

USO (requer UPSTASH_WRITE_API_KEY):
  python -m pipelines.migrations.migra_2026_08_remove_pii_legado backup     # identifica e salva (com vetor)
  python -m pipelines.migrations.migra_2026_08_remove_pii_legado aplicar
  python -m pipelines.migrations.migra_2026_08_remove_pii_legado verificar
  python -m pipelines.migrations.migra_2026_08_remove_pii_legado reverter
"""

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

from dotenv import load_dotenv

load_dotenv(RAIZ / ".env")
from upstash_vector import Index

index = Index(url=os.environ["UPSTASH_ENDPOINT"], token=os.environ["UPSTASH_WRITE_API_KEY"])
BACKUP = RAIZ / "backups" / "migracao_2026_08_pii_legado.jsonl"

_INSCR = re.compile(r"\b\d{6,9}\b")
# marcador de LISTA NOMINAL DE CANDIDATO no titulo/URL. exige o marcador E a densidade de inscricao:
# so a densidade e frouxa demais (medido: pegava PPC, plano de acao, relatorio RAR e compilado de
# portaria, que tem muitos codigos numericos por outros motivos, e apagar PPC quebraria o escopo de curso).
_MARCADOR = re.compile(r"homologad|classificad|inscri[çc][oõ]es[-_ ]aceitas|lista[-_ ]de[-_ ]votantes|"
                       r"rela[çc][aã]o[-_ ](final|de[-_ ]candidat)|selecionados|ensalament|"
                       r"resultado[-_ ](final|da[-_ ]\d|\d)|classificacao[-_ ]geral", re.IGNORECASE)
# nunca remover documento definidor/institucional, mesmo que a densidade numerica bata
_NUNCA = re.compile(r"\bppc\b|projeto[-_ ]pedag|matriz[-_ ]curricular|plano[-_ ]de[-_ ]acao|"
                    r"\brar\b|relatorio|regulamento|portaria|resolucao|edital[-_ ]n", re.IGNORECASE)


def _e_pii(texto, url, title):
    blob = (url or "") + " " + (title or "")
    if _NUNCA.search(blob):
        return False
    n = len(set(_INSCR.findall(texto or "")))
    return bool(_MARCADOR.search(blob)) and n >= 5


def _varrer():
    achados = []
    cursor = ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for v in res.vectors:
            m = v.metadata or {}
            if _e_pii(m.get("text", ""), m.get("source_url", ""), m.get("title", "")):
                achados.append((str(v.id), m))
        cursor = res.next_cursor
        if cursor == "":
            break
    return achados


def backup():
    achados = _varrer()
    ids = [i for i, _ in achados]
    completos = []
    for k in range(0, len(ids), 100):
        for v in index.fetch(ids=ids[k:k + 100], include_vectors=True, include_metadata=True):
            if v is not None:
                completos.append({"id": str(v.id), "vector": v.vector, "metadata": v.metadata or {}})
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    with open(BACKUP, "w", encoding="utf-8") as f:
        for c in completos:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    por_doc = Counter(m.get("source_url", "") for _, m in achados)
    print(f"a remover: {len(completos)} chunks de {len(por_doc)} documentos -> {BACKUP.name}")
    for u, n in por_doc.most_common():
        print(f"  {n:>3} chunks  {u[-78:]}")


def aplicar():
    linhas = [json.loads(l) for l in open(BACKUP, encoding="utf-8") if l.strip()]
    ids = [c["id"] for c in linhas]
    for k in range(0, len(ids), 500):
        index.delete(ids=ids[k:k + 500])
    print(f"removidos {len(ids)} chunks de lista nominal legada")


def verificar():
    achados = _varrer()
    print(f"chunks de lista nominal restantes: {len(achados)} (esperado 0)")
    for i, m in achados[:8]:
        print(f"  {i[-40:]}  {m.get('source_url','')[-50:]}")
    total = 0
    cursor = ""
    while True:
        res = index.range(cursor=cursor, limit=1000)
        total += len(res.vectors)
        cursor = res.next_cursor
        if cursor == "":
            break
    print(f"total de chunks na base: {total}")


def reverter():
    linhas = [json.loads(l) for l in open(BACKUP, encoding="utf-8") if l.strip()]
    vecs = [(c["id"], c["vector"], c["metadata"]) for c in linhas]
    for k in range(0, len(vecs), 100):
        index.upsert(vectors=vecs[k:k + 100])
    print(f"revertido: {len(vecs)} chunks restaurados")


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    if modo not in ("backup", "aplicar", "verificar", "reverter"):
        print(__doc__); sys.exit(1)
    {"backup": backup, "aplicar": aplicar, "verificar": verificar, "reverter": reverter}[modo]()
