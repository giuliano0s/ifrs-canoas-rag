import requests
from pathlib import Path

URL = "https://ifrs.edu.br/canoas/"
OUTPUT = Path("ui/index.html")

def clone_page():
    """"
    Clona a página HTML do IFRS Canoas e salva localmente.
    """
    response = requests.get(URL, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    response.raise_for_status()
    
    html = response.text.replace(
    "</body>",
    '<script src="widget.js"></script></body>'
    )
    
    # salva o HTML original
    OUTPUT.mkdir(parents=True, exist_ok=True) if OUTPUT.is_dir() else OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"Página salva em {OUTPUT}")

if __name__ == "__main__":
    clone_page()