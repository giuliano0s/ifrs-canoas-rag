"""Confere se os dados verificáveis das respostas de uma coleta aparecem no que o agente leu.

Um dado (data, horário, número com unidade, valor em reais, e-mail, endereço de site, sala) está ancorado
quando aparece nos trechos de todas as buscas da execução, no prompt do agente, na data atual que ele
recebeu (a simulada do caso, ou o dia da coleta), na lista de cursos ou na pergunta. O que não aparece vira candidato a dado inventado, gravado para leitura humana. A checagem não vê
dado certo atribuído à coisa errada (a data de um evento dada a outro): isso fica para o juiz.

Na resposta, uma faixa de datas conta pelas duas pontas; no que o agente leu, a faixa é expandida dia a dia,
para "de 05 a 09/01" na resposta casar com "05/01 a 13/01" no documento.

Uso:
    python -m eval.ancoragem eval/runs/coleta.jsonl
    python -m eval.ancoragem a.jsonl b.jsonl --bracos atual,regra --prompts data/info/agente-ifrs.txt antigo.txt
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from eval.run_eval import _wilson
from rag.agenda import trechos_como_lidos
from rag.ancoragem import MES_RX, ancorada, datas, normalizar, trechos_de_data

CURSOS = Path("data/info/cursos_atuais.json")
SAIDA_PADRAO = Path("eval/runs/ancoragem_candidatos.jsonl")


def horas(texto):
    """Horários citados ("8h", "13h10", "17:30", "17:30h", "13h10min"), em minutos desde 0h."""
    achados = set()
    for hora, minuto_h, minuto_dois_pontos in re.findall(
            r"\b(\d{1,2})(?:h(\d{2})?|:(\d{2}))(?!\d)(?!\s*(?:horas|h-a|semestres))", texto, re.I):
        if int(hora) <= 23:
            achados.add(int(hora) * 60 + int(minuto_h or minuto_dois_pontos or 0))
    return achados


def _canonico(numero):
    """Número sem separador de milhar, com ponto decimal e sem casas decimais zeradas ("500,00" vira "500")."""
    return re.sub(r"\.0+$", "", numero.replace(".", "").replace(",", "."))


def numeros(texto):
    """Números soltos (inteiros e decimais) na forma de `_canonico`."""
    return {_canonico(numero) for numero in re.findall(r"\b\d{1,3}(?:\.\d{3})+\b|\b\d+(?:,\d+)?\b", texto)}


def atomos(corpo):
    """Dados verificáveis do corpo da resposta, como pares (tipo, valor)."""
    lista = [("data", data) for data in sorted({data for _, _, citadas in trechos_de_data(corpo) for data in citadas},
                                                key=str)]

    # horários, fora das datas
    sem_datas = re.sub(rf"\b\d{{1,2}}(?:º)? (?:(?:a|e|até) \d{{1,2}} )?de ({MES_RX})(?: de \d{{4}})?"
                       r"|\b\d{1,2}(?: (?:a|e|até) \d{1,2})?/\d{1,2}(?:/\d{2,4})?", " ", corpo, flags=re.I)
    lista += [("hora", minuto) for minuto in horas(sem_datas)]

    # contatos, endereços e salas
    for email in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", corpo):
        lista.append(("email", email.lower().rstrip(".")))
    for url in re.findall(r"(?:https?://)?(?:[\w-]+\.)+(?:edu\.br|gov\.br|com|org|br)(?:/[^\s)\]>,;]*)?", corpo):
        if "@" not in url:
            lista.append(("url", re.sub(r"^https?://", "", url).rstrip("/.:").lower()))
    lista += [("sala", sala) for sala in re.findall(r"\b[A-Z]\d{3}\b", corpo)]

    # números com unidade e valores em reais, fora de datas e horários
    sem_horas = re.sub(r"\b\d{1,2}[h:]\d{0,2}\b", " ", sem_datas)
    for numero, unidade in re.findall(
            r"\b(\d+(?:[.,]\d+)?)\s*(vagas?|horas?|h\b|semestres?|anos?|dias?|alunos?|estudantes?|servidores?|docentes?"
            r"|professores?|técnicos?|pontos?|%|reais|créditos?|disciplinas?|turmas?|cursos?)", sem_horas, re.I):
        if not re.fullmatch(r"20\d\d", numero):
            lista.append(("numero", _canonico(numero) + " " + unidade.lower()))
    for valor in re.findall(r"R\$\s?(\d[\d.,]*)", sem_horas):
        lista.append(("numero", _canonico(valor.rstrip(".,")) + " reais"))
    return lista


def ancorado(tipo, valor, lido):
    """Diz se o dado aparece no que o agente leu (dicionário montado por `_o_que_leu`)."""
    if tipo == "data":
        return ancorada(valor, lido["datas"])
    if tipo == "hora":
        return valor in lido["horas"]
    if tipo in ("email", "sala"):
        return valor.lower() in lido["texto"]
    if tipo == "url":
        return valor in lido["texto"] or any(endereco.startswith(valor) for endereco in lido["urls"])
    if tipo == "numero":
        return valor.split()[0] in lido["numeros"]
    return True


def _datas_de_referencia():
    """Data simulada (`data_referencia`) de cada caso do golden e do controle, por id."""
    referencias = {}
    for caminho in (Path("eval/golden_set.json"), Path("eval/controle_set.json")):
        if caminho.exists():
            for caso in json.loads(caminho.read_text(encoding="utf-8")).get("casos", []):
                if caso.get("data_referencia"):
                    referencias[caso["id"]] = caso["data_referencia"]
    return referencias


def _data_atual(registro, referencias):
    """A data atual que o agente recebeu no prompt: a simulada do caso ou o dia da coleta."""
    if registro.get("case_id") in referencias:
        return referencias[registro["case_id"]]
    return datetime.fromisoformat(registro["ts"]).strftime("%d/%m/%Y") if registro.get("ts") else ""


def _o_que_leu(registro, fixo, data_atual):
    """Texto e dados extraídos de tudo que o agente leu numa execução, inclusive a data atual do prompt."""
    buscas = registro.get("buscas") or []
    # o agente lê cada evento datado com a situação dele em relação à data do pedido ("[daqui a 6 dias]"),
    # que a coleta não guarda: é refeita aqui, pela mesma função do atendimento
    try:
        hoje = datetime.strptime(data_atual, "%d/%m/%Y").date()
    except ValueError:
        hoje = None
    trechos = " ".join(trecho for busca in buscas for trecho in trechos_como_lidos(busca, hoje))
    urls = {re.sub(r"^https?://", "", url or "").rstrip("/").lower()
            for busca in buscas for url in (busca.get("contexto_urls") or [])}
    texto = " ".join([trechos, fixo, data_atual, registro.get("input") or "", " ".join(urls)])
    return {"texto": texto.lower(), "urls": urls, "datas": datas(normalizar(texto), expandir=True),
            "horas": horas(texto), "numeros": numeros(texto)}


def main():
    """Lê as coletas, imprime o placar por arquivo e braço e grava os candidatos a dado inventado."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("coletas", nargs="+", type=Path)
    parser.add_argument("--prompts", nargs="+", type=Path, default=[Path("data/info/agente-ifrs.txt")],
                        help="prompts que valem como fonte (o de cada braço comparado)")
    parser.add_argument("--bracos", default="", help="lista separada por vírgula; vazio = todos")
    parser.add_argument("--saida", type=Path, default=SAIDA_PADRAO)
    argumentos = parser.parse_args()
    bracos = {braco for braco in argumentos.bracos.split(",") if braco}
    fixo = " ".join(caminho.read_text(encoding="utf-8") for caminho in [*argumentos.prompts, CURSOS])
    referencias = _datas_de_referencia()

    # confere cada resposta contra o que a execução leu
    totais, candidatos = defaultdict(Counter), []
    for coleta in argumentos.coletas:
        for linha in coleta.open(encoding="utf-8"):
            registro = json.loads(linha) if linha.strip() else {}
            if not registro.get("resposta") or (bracos and registro.get("braco") not in bracos):
                continue
            grupo = f"{coleta.name} [{registro.get('braco') or '-'}]"
            corpo = registro["resposta"].split("Fontes:")[0]
            lido = _o_que_leu(registro, fixo, _data_atual(registro, referencias))
            dados = atomos(corpo)
            sem_ancora = [(tipo, valor) for tipo, valor in dados if not ancorado(tipo, valor, lido)]
            totais[grupo].update(respostas=1, dados=len(dados), candidatos=len(sem_ancora),
                                 respostas_com_candidato=int(bool(sem_ancora)))
            if sem_ancora:
                candidatos.append({"coleta": coleta.name, "case_id": registro.get("case_id"),
                                   "braco": registro.get("braco"), "run": registro.get("run"),
                                   "input": registro.get("input"), "buscou": bool(registro.get("buscas")),
                                   "sem_ancora": [f"{tipo}:{valor}" for tipo, valor in sem_ancora],
                                   "corpo": corpo.strip()})

    # placar por grupo e total, com IC de Wilson 95% da taxa por dado
    totais["TOTAL"] = sum(totais.values(), Counter())
    for grupo, contagem in totais.items():
        baixo, alto = _wilson(contagem["candidatos"], contagem["dados"])
        print(f"{grupo}: {contagem['respostas']} respostas, {contagem['dados']} dados, "
              f"{contagem['candidatos']} sem âncora em {contagem['respostas_com_candidato']} respostas "
              f"(IC 95% {baixo:.2%} a {alto:.2%} por dado)")
    argumentos.saida.parent.mkdir(parents=True, exist_ok=True)
    with argumentos.saida.open("w", encoding="utf-8") as saida:
        for candidato in candidatos:
            saida.write(json.dumps(candidato, ensure_ascii=False) + "\n")
    tipos = Counter(item.split(":")[0] for candidato in candidatos for item in candidato["sem_ancora"])
    print(f"candidatos gravados em {argumentos.saida}: {len(candidatos)} respostas, por tipo {dict(tipos)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
