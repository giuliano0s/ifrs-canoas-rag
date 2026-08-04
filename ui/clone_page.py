import requests
from pathlib import Path

# origem clonada e destino do snapshot
SOURCE_URL = "https://ifrs.edu.br/canoas/"
OUTPUT = Path("ui/index.html")

# faixa fixa de aviso de ambiente não oficial
# faixa de aviso: ambiente nao oficial + AVISO DE DADOS (LGPD). O aviso de dados e requisito para
# abrir a teste publico: as perguntas e respostas do chat sao registradas na telemetria para
# avaliar a qualidade do assistente, entao o usuario precisa saber ANTES de digitar, e ser
# orientado a nao informar dado pessoal (o servidor nao tem como impedir que ele digite).
ALERT_BANNER = """
<div style="position:fixed;top:0;left:0;right:0;z-index:99999;background:#9a3412;color:#fff;
text-align:center;font:600 13px/1.4 'Segoe UI',sans-serif;padding:8px 12px;">
Ambiente de teste não oficial. Esta página não é o site do IFRS Campus Canoas e serve apenas para
demonstração de um assistente de IA. Desenvolvido por
<a href="https://github.com/giuliano0s" target="_blank" rel="noopener"
style="color:#fff;text-decoration:underline;">giuliano0s</a>.
<span style="display:block;font-weight:400;font-size:12px;opacity:.95;">
As conversas com o assistente são registradas para avaliar a qualidade das respostas.
Não informe dados pessoais (nome completo, CPF, matrícula, telefone) no chat.</span></div>
<div style="height:52px;"></div>
"""


def build_page():
    # busca o html ao vivo do ifrs e injeta a faixa de aviso e o widget
    resp = requests.get(SOURCE_URL, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }, timeout=15)
    resp.raise_for_status()
    html = resp.text.replace("<body", ALERT_BANNER + "<body", 1) if "<body" in resp.text else ALERT_BANNER + resp.text
    return html.replace("</body>", '<script src="widget.js"></script></body>')


def clone_page():
    # gera o index.html estatico servido pelo app
    html = build_page()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"Página salva em {OUTPUT}")


if __name__ == "__main__":
    clone_page()
