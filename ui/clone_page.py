from pathlib import Path
from ui.app import build_page

OUTPUT = Path("ui/index.html")

def clone_page():
    """
    Gera o index.html de fallback com a faixa de aviso e o widget injetados.
    Usa a mesma montagem da pagina servida ao vivo pelo app.
    """
    html = build_page()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"Página salva em {OUTPUT}")

if __name__ == "__main__":
    clone_page()