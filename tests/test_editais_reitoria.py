"""Testes da coleta dos editais da Reitoria (pipelines/editais_reitoria.py), com textos fixos, sem rede e
com um LLM falso.

Uso, a partir da raiz: python -m tests.test_editais_reitoria
"""
import json
from datetime import datetime
from types import SimpleNamespace

from pipelines import editais_reitoria as er

EDITAL = {"url": "https://ifrs.edu.br/editais/edital-proex-no-41-2026-bolsas/", "titulo": "EDITAL PROEX Nº 41/2026 BOLSAS",
          "data": "20/10/2026", "atualizacao": "21/10/2026 10:00", "categorias": ["Internos", "Extensão"]}
COMPRA = {**EDITAL, "url": "https://ifrs.edu.br/editais/pregao-90010-2026/", "titulo": "PREGÃO nº 90010/2026 – Vigilância",
          "categorias": ["Pregão Eletrônico"]}


def _grupo(*textos, pagina="Página do edital"):
    return {"pagina": pagina, "anexos": [{"url": f"https://ifrs.edu.br/wp-content/uploads/2026/10/anexo-{n}.pdf",
                                          "nome": f"anexo-{n}.pdf", "hash": f"h{n}", "texto": texto}
                                         for n, texto in enumerate(textos)]}


def _llm_falso(respostas):
    # devolve as respostas em ordem, como o modelo devolveria o texto
    fila = list(respostas)
    return lambda prompt, max_saida: SimpleNamespace(texto=fila.pop(0))


def testar_decisao():
    longo = "Texto do edital sem a unidade. " * 10
    assert er.cita_outra_unidade("Campus Rio Grande") and er.cita_outra_unidade("Campus Bento Gonçalves")
    assert not er.cita_outra_unidade("Instituto Federal do Rio Grande do Sul") and not er.cita_outra_unidade("Campus Canoas")

    # frase citada pelo LLM conferida no texto lido, em blocos, mesmo fora de ordem
    assert er.cobertura("as vagas do Campus Canoas são duas", "Edital: as vagas do Campus Canoas são duas.") == 1.0
    assert er.cobertura("vagas inventadas em Canoas para bolsistas", "nada disso aparece aqui") == 0.0
    assert er.cobertura("Canoas", "Campus Canoas") == 1.0

    canoas = {"categoria": "canoas", "evidencia": "uma vaga no Campus Canoas", "cobertura": 1.0}
    outro = {"categoria": "outro", "evidencia": "exclusivo para o Campus Osório", "cobertura": 1.0}
    assert er.decidir({**EDITAL, "titulo": "Edital Campus Canoas"}, _grupo(longo), None) == (True, "titulo")
    assert er.decidir(EDITAL, _grupo(longo), canoas) == (True, "canoas")
    assert er.decidir(EDITAL, _grupo(longo), outro) == (False, "outro")
    assert er.decidir(EDITAL, _grupo(longo), {**outro, "evidencia": "exclusivo para servidores"}) == (True, "outro_sem_unidade")
    assert er.decidir(EDITAL, _grupo(longo), {**outro, "cobertura": 0.5}) == (True, "evidencia_nao_confere")
    assert er.decidir(COMPRA, _grupo(longo), None) == (False, "compra_sem_canoas")
    assert er.decidir({**COMPRA, "titulo": "PREGÃO nº 1/2026 – Vigilância (ANULADO)"}, _grupo(longo), None)[1] == "compra_anulada"
    assert not er.precisa_de_veredito(COMPRA, _grupo(longo))
    assert er.precisa_de_veredito(EDITAL, _grupo(longo))
    assert er.objeto_da_compra(COMPRA["titulo"]) == "vigilância"

    # compra publicada ainda sem categoria no portal e reconhecida pelo titulo; contratacao de pessoal nao e compra
    assert er.eh_compra({"categorias": [], "titulo": "Pregão n° 403/2026 – SRP – Aquisição de equipamentos"})
    assert not er.eh_compra({"categorias": ["Gestão de Pessoas"],
                             "titulo": "EDITAL DGP 05/2026-PROCESSO SELETIVO SIMPLIFICADO PARA CONTRATAÇÃO DE ESTAGIÁRIOS"})

    # compra repetida: fica so a mais recente com o mesmo objeto
    registro = {"a": {"data": "01/02/2026", "entra": True, "objeto": "vigilância", "ids": ["x#0"]},
                "b": {"data": "01/06/2026", "entra": True, "objeto": "vigilância", "ids": ["y#0"]}}
    apagar, er._apagar = er._apagar, lambda ids: None
    try:
        assert er._compras_repetidas(registro) == 1
    finally:
        er._apagar = apagar
    assert registro["a"]["motivo"] == "compra_repetida" and registro["a"]["ids"] == [] and registro["b"]["entra"]

    # edital com data antes da janela sai do registro, e os chunks dele vao para apagar
    registro = {"velho": {"data": "26/10/2025", "ids": ["v#0", "v#1"]}, "novo": {"data": "27/10/2025", "ids": ["n#0"]}}
    assert er.sair_da_janela(registro, datetime(2025, 10, 27)) == (["velho"], ["v#0", "v#1"])
    assert list(registro) == ["novo"]


def testar_recorte():
    chamar = er._chamar
    try:
        # linha de outro campus sai, linha com Canoas nunca sai, linhas de nomes viram um aviso por bloco
        regras = [f"Regra {numero} do edital de bolsas." for numero in range(1, 21)]
        texto = "\n".join(["Edital de bolsas", "Campus Osório: 2 vagas", "Campus Canoas: 1 vaga", "Comissão",
                           "Ana Souza Lima", "Bruno Costa Pereira"] + regras)
        er._chamar = _llm_falso([json.dumps({"outro_campus": [[2, 3]], "nomes": [[5, 6]]})])
        recorte = er.recortar_anexo(EDITAL, _grupo(texto)["anexos"][0])
        assert recorte["lista"] is None
        assert recorte["texto"].split("\n") == ["Edital de bolsas", "Campus Canoas: 1 vaga", "Comissão", er.AVISO_NOMES] + regras

        # JSON invalido e refeito; tres falhas tiram o anexo
        er._chamar = _llm_falso(["nao e json", json.dumps({"outro_campus": [], "nomes": []})])
        assert er.recortar_anexo(EDITAL, _grupo(texto)["anexos"][0])["texto"] == texto
        er._chamar = _llm_falso(["x", "y", "z"])
        try:
            er.recortar_anexo(EDITAL, _grupo(texto)["anexos"][0])
            raise AssertionError("tres JSON invalidos deveriam tirar o anexo")
        except ValueError:
            pass

        # JSON invalido numa janela grande (laco de repeticao do modelo) parte a janela ao meio
        grande = "\n".join(f"Linha {numero} do edital." for numero in range(1, 61))
        er._chamar = _llm_falso(['{"outro_campus": [[47,43],[47,43],[47,43]', json.dumps({"outro_campus": [[2, 3]]}),
                                 json.dumps({"outro_campus": [[59, 60]]})])
        partido = er.recortar_anexo(EDITAL, _grupo(grande)["anexos"][0])["texto"].split("\n")
        assert len(partido) == 56 and "Linha 2 do edital." not in partido and "Linha 60 do edital." not in partido

        # muitas linhas de nomes: o anexo inteiro vira o aviso com o titulo do edital
        er._chamar = _llm_falso([json.dumps({"outro_campus": [], "nomes": [[5, 8]]})])
        lista = er.recortar_anexo(EDITAL, _grupo(texto)["anexos"][0])
        assert lista["lista"] == "nomes_marcados" and "proteção de dados" in lista["texto"] and EDITAL["titulo"] in lista["texto"]

        # compra: fica o que o LLM manda manter e a linha de item de Canoas; Canoas so numa enumeracao deixa a capa
        compra = "Pregão de vigilância\nObjeto: vigilância armada\nItem 1 Campus Canoas 2 postos\nItem 2 Campus Osório 1 posto"
        er._chamar = _llm_falso([json.dumps({"manter": [[1, 2]]})])
        assert er.recortar_anexo(COMPRA, _grupo(compra)["anexos"][0])["texto"].split("\n")[-1] == "Item 1 Campus Canoas 2 postos"
        enumeracao = "Pregão de vigilância\nObjeto: vigilância\nUnidades: Canoas, Osório, Feliz e Vacaria\nItem 1 Osório"
        er._chamar = _llm_falso([json.dumps({"manter": [[1, 2]]})])
        assert er.recortar_anexo(COMPRA, _grupo(enumeracao)["anexos"][0])["texto"] == "Pregão de vigilância\nObjeto: vigilância"
    finally:
        er._chamar = chamar


def testar_retificacoes():
    # o trecho antigo vira o novo mesmo quebrado em linhas, e o resto do texto mantem as quebras
    original = "1. Das inscrições\nAs inscrições vão até\n20 de outubro de 2026, pelo formulário.\n2. Do resultado\nDia 30."
    trocado = er._trocar(original, "As inscrições vão até 20 de outubro de 2026, pelo formulário.",
                         "As inscrições vão até 27 de outubro de 2026, pelo formulário.")
    assert trocado == "1. Das inscrições\nAs inscrições vão até 27 de outubro de 2026, pelo formulário.\n2. Do resultado\nDia 30."
    assert er._trocar(original, "texto que não existe em lugar nenhum do edital", "x") is None

    retificacao = ("RETIFICAÇÃO\nOnde se lê: As inscrições vão até 20 de outubro de 2026, pelo formulário.\n"
                   "Leia-se: As inscrições vão até 27 de outubro de 2026, pelo formulário.\nPorto Alegre, 21 de outubro de 2026.")
    assert er.pares_onde_leia(retificacao) == [("As inscrições vão até 20 de outubro de 2026, pelo formulário.",
                                                "As inscrições vão até 27 de outubro de 2026, pelo formulário.")]
    registros = [{"url": "u/edital.pdf", "documento": "Edital-41-2026.pdf", "texto": original},
                 {"url": "u/retif.pdf", "documento": "Retificacao-Edital-41-2026.pdf", "texto": retificacao}]
    resumo = er.consolidar_retificacoes(registros)
    assert resumo == {"pares": 1, "aplicados": 1, "substituidos": 0}
    assert "27 de outubro" in registros[0]["texto"] and "u/retif.pdf" in registros[0]["aviso_retificacao"]
    assert "TEXTO QUE VALE" in registros[1]["texto"] and "aviso_retificacao" not in registros[1]

    # versao retificada completa substitui o documento de nome parecido
    registros = [{"url": "u/a.pdf", "documento": "Edital-PROEX-41-2026.pdf", "texto": "versão antiga " * 20},
                 {"url": "u/b.pdf", "documento": "Edital-PROEX-41-2026-retificado.pdf", "texto": "versão nova " * 20}]
    assert er.consolidar_retificacoes(registros)["substituidos"] == 1
    assert registros[0]["texto"] == "" and registros[0]["substituido_por"] == "u/b.pdf"


def testar_chunks():
    registros = [{"url": "u/edital.pdf", "documento": "edital.pdf", "texto": "Texto do edital", "hash": "h1",
                  "aviso_retificacao": "[retificado]"},
                 {"url": "u/resultado.pdf", "documento": "resultado.pdf", "texto": "aviso da lista", "hash": "h2",
                  "lista": "nomes_marcados"}]
    chunks = er.chunks_do_edital(EDITAL, "Página do edital", registros)
    assert [chunk["id"] for chunk in chunks] == [f"{EDITAL['url']}#0", "u/edital.pdf#0", "u/resultado.pdf#0"]
    assert all(chunk["edital_url"] == EDITAL["url"] and chunk["published_at"] == 2026 for chunk in chunks)
    assert chunks[1]["text"] == "[retificado]\nTexto do edital"
    assert chunks[2]["lista_nomes"] and chunks[2]["text"] == "aviso da lista"

    # anexo ja gravado por outro caminho nao entra de novo
    longo = "Campus Canoas: texto do edital. " * 10
    chamar = er._chamar
    er._chamar = _llm_falso([json.dumps({"outro_campus": [], "nomes": []})])
    try:
        grupo = _grupo(longo, longo + " outro")
        edital_canoas = {**EDITAL, "titulo": "Edital Campus Canoas"}
        resultado = er.processar_edital(edital_canoas, grupo, None, urls_ocupadas={grupo["anexos"][1]["url"]})
    finally:
        er._chamar = chamar
    assert resultado["entra"] and {chunk["source_url"] for chunk in resultado["chunks"]} == {edital_canoas["url"],
                                                                                            grupo["anexos"][0]["url"]}


if __name__ == "__main__":
    testar_decisao()
    testar_recorte()
    testar_retificacoes()
    testar_chunks()
    print("ok: editais da Reitoria")
