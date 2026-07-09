"""Gera data/info/cursos_atuais.json a partir das paginas de curso ja na base.

Passo final da pipeline de ingestao; sem LLM e sem rede nova (le do Upstash). A lista
reflete os cursos que o RAG de fato conhece, e serve o validador (Fase 2, criterio do
caso curso-inexistente) e pode alimentar o prompt do agente.
"""
import json, os, re
from pathlib import Path
from dotenv import load_dotenv
from upstash_vector import Index

load_dotenv()

_OUT = Path(__file__).resolve().parent.parent / "data" / "info" / "cursos_atuais.json"

# URL de pagina de curso individual (exclui categorias como /superiores, /pos, /especializacao).
# cobre superiores/tecnicos (curso-*, tecnico-em-*, proeja-tecnico-*) e o bacharelado (fora de /cursos/)
_PADRAO_CURSO = re.compile(
    r"/no-campus/cursos/(curso-|tecnico-em-|proeja-tecnico-)"
    r"|/bacharelado-em-engenharia"
)

# prefixos/sufixos removidos para extrair o nucleo do nome do curso
_PREFIXOS = [
    "Curso Superior de Tecnologia em ", "Curso Superior de ", "Curso Técnico em ",
    "Bacharelado em ", "Curso de ",
]

def _nucleo(titulo):
    # extrai o nucleo do nome (ex: "Curso Superior de Tecnologia em Analise..." -> "Analise...")
    t = (titulo or "").split(" - Campus")[0].strip()
    for pre in _PREFIXOS:
        if t.startswith(pre):
            t = t[len(pre):]
            break
    t = re.split(r" Integrado ao Ensino| – Modalidade| - Modalidade", t)[0]
    return t.strip()

def gerar(index=None, out=_OUT):
    # le do Upstash (read-only basta) e coleta as paginas de curso individuais
    if index is None:
        index = Index(url=os.getenv("UPSTASH_ENDPOINT"), token=os.getenv("UPSTASH_API_KEY"))

    por_url = {}
    cursor = ""
    while True:
        res = index.range(cursor=cursor, limit=1000, include_metadata=True)
        for v in res.vectors:
            m = v.metadata or {}
            u = m.get("source_url", "")
            if _PADRAO_CURSO.search(u) and u not in por_url:
                por_url[u] = _nucleo(m.get("title", ""))
        cursor = res.next_cursor
        if cursor == "":
            break

    cursos = sorted({t for t in por_url.values() if t})
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"cursos": cursos, "urls": sorted(por_url)}, f, ensure_ascii=False, indent=2)
    print(f"[cursos_atuais] {len(cursos)} cursos -> {out}")
    return cursos

if __name__ == "__main__":
    gerar()
