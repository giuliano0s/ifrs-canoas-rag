"""Testes do detector de lista de pessoas (pipelines/listas_nomes.py), com textos fixos e sem rede.

Uso, a partir da raiz: python -m tests.test_listas_nomes
"""
from pipelines.listas_nomes import aviso_lista, destino, e_do_ingresso, motivo_lista

INGRESSO = "https://ingresso.ifrs.edu.br/2025-2/wp-content/uploads/sites/36/2025/06/Classificacao-Geral-1.pdf"
CAMPUS = "https://ifrs.edu.br/canoas/wp-content/uploads/sites/6/2026/03/4-Monitorias-2026-Resultado-Final.pdf"


def testar():
    # listas reais, nos formatos encontrados na base
    assert motivo_lista("CLASSIFICAÇÃO DOS CANDIDATOS EM 1ª CHAMADA\nINSCR. CANDIDATO STATUS\n74.303325 JULIA HELLEN "
                        "GASPERIM MENEZES 1 0.00 0.00 - ELIMINADO", INGRESSO)
    hetero = "Resultado Final da Comissão de Heteroidentificação\nNome Número de Inscrição Resultado\n" + "\n".join(
        f"PESSOA NUMERO {letra} DA SILVA 73.317-{n} Deferida" for n, letra in zip(range(10, 30), "ABCDEFGHIJKLMNOPQRST"))
    assert motivo_lista(hetero, "https://ingresso.ifrs.edu.br/2025-2/x/Resultado-Final-Heteroidentificacao.pdf")
    monitoria = ("CLASSIFICAÇÃO FINAL da seleção de bolsistas do PROGRAMA DE MONITORIA 2026\n"
                 "Lavínia Machado Goldschmidt Classificado(a)\nAlex Samuel Trindade Suplente\n"
                 "Mariana Rosa Rodrigues Classificado(a)\nAndrew Silva Pereira Desclassificado(a)\n"
                 "Ícaro Chagas Silva Desclassificado(a)")
    assert motivo_lista(monitoria, CAMPUS)
    assert motivo_lista("Resultado do processo de troca de turno\nAna Paula Souza Lima Deferido\n"
                        "Bruno Costa Pereira Indeferido", "https://ifrs.edu.br/canoas/x/Resultado-troca-de-turno.pdf")
    assert motivo_lista(" ".join(f"CPF 123.456.78{n}-0{n}" for n in range(10)), "https://ifrs.edu.br/canoas/x/qualquer.pdf")

    # nao sao lista: portaria de servidores com SIAPE, cronograma, calendario, edital, pagina de noticia
    assert not motivo_lista("Servidor(a) Siape Funções no núcleo\nAline da Silveira Muniz 2158855 Coordenadora\n"
                            "Jaqueline Justen Souza 1979619 Membro\n" * 4, "https://ifrs.edu.br/canoas/x/NAPNE.pdf")
    assert not motivo_lista("17/06/2026 Publicação da Classificação Geral e Aprovados em 1ª Chamada\n"
                            "01/07/2026 Divulgação dos homologados\n" * 6, "https://ingresso.ifrs.edu.br/2026-2/cronograma/",
                            pagina=True)
    assert not motivo_lista("O edital seleciona bolsistas; os classificados serão convocados. Processo "
                            "23419.003532/2026-30. Telefone (51) 3415-8200.", "https://ifrs.edu.br/canoas/editais/edital-x/",
                            pagina=True)
    assert not motivo_lista("Projeto do Campus Canoas é o vencedor nacional. A professora Maria Clara Souza Lima "
                            "coordenou a equipe classificada.", "https://ifrs.edu.br/canoas/projeto-vencedor-nacional/",
                            pagina=True)

    # destino: PDF de lista do ingresso fica fora; pagina do ingresso e lista de outra origem viram aviso
    assert e_do_ingresso(INGRESSO) and not e_do_ingresso(CAMPUS)
    assert destino(hetero, INGRESSO)[0] == "fora"
    assert destino(hetero, INGRESSO, pagina=True)[0] == "aviso"
    assert destino(monitoria, CAMPUS)[0] == "aviso"
    assert destino("Edital de seleção de bolsistas. Inscrições até 30/10.", CAMPUS) == (None, None)
    # o aviso diz onde conferir e nao traz nome
    aviso = aviso_lista(CAMPUS, "Resultado Final da Seleção de Monitores")
    assert "proteção de dados" in aviso and CAMPUS in aviso and not motivo_lista(aviso, CAMPUS)
    print("ok: listas de pessoas")


if __name__ == "__main__":
    testar()
