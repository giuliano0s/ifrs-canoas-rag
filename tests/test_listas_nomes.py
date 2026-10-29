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

    # um LLM leu as linhas: nenhum nome desfaz o sinal de formato de nome (lista de projetos homologados), mas
    # nao o de numero de inscricao; nome achado num arquivo de divulgacao faz lista
    projetos = ("torna pública a homologação das propostas\nGrupo de Pesquisa Situação da proposta\n"
                "Grupo de Manufatura Aplicada Homologada\nRede Sul de Inovação Agroecológica Homologada\n"
                "(Assinado digitalmente em 06/05/2026 10:17)\nFLAVIA SANTOS TWARDOWSKI PINTO\nPRO-REITOR(A)")
    url_projetos = "https://ifrs.edu.br/wp-content/uploads/2026/05/RESULTADO-FINAL-DAS-PROPOSTAS-HOMOLOGADAS.pdf"
    assert destino(projetos, url_projetos)[0] == "aviso"
    assert destino(projetos, url_projetos, linhas=7, nomes_marcados=0) == (None, None)
    assert destino(hetero, INGRESSO, linhas=22, nomes_marcados=0)[0] == "fora"
    entrevistas = "https://ifrs.edu.br/wp-content/uploads/2026/03/DIVULGACAO-DOS-DIAS-E-HORARIOS-DAS-ENTREVISTAS.pdf"
    assert destino("Entrevistas no dia 10/03, sala 2.", entrevistas, linhas=49, nomes_marcados=2)[0] == "aviso"
    assert destino("Entrevistas no dia 10/03, sala 2.", entrevistas, linhas=49, nomes_marcados=0) == (None, None)
    # tabela de resultado (sinal estrutural, sem depender de reconhecer nome): inscricao "70.54-59", lista de um
    # candidato so com situacao fora do vocabulario e selecao do campus sem numero de inscricao
    cabecalho = "Resultado Final da Matrícula\nNome\nNúmero de\nInscrição\nCota de\nAvaliação\nResultado Final\n"
    linhas_geo = "\n".join(f"PESSOA NUMERO {letra} DA SILVA\n70.54-{numero}\nC1\nSuplente"
                           for numero, letra in zip(range(1, 21), "ABCDEFGHIJKLMNOPQRST"))
    url_geo = INGRESSO.replace("Classificacao-Geral-1", "Resultado-Final-Geoprocessamento")
    assert motivo_lista(cabecalho + linhas_geo, url_geo)
    # o formato "70.54-59" sozinho, sem o cabecalho da tabela, ja faz lista pelos numeros de inscricao
    assert motivo_lista("Resultado Final da Matrícula\n" + linhas_geo, url_geo) == "identificadores"
    unico = cabecalho + "PESSOA UNICA DA SILVA\n71.300414\nC11\nMatrícula\nCondicional\nApresentar documento até 30/04"
    assert motivo_lista(unico, "https://ingresso.ifrs.edu.br/x/Resultado-Final-Pedagogia.pdf") == "tabela_de_resultado"
    assert destino(unico, "https://ingresso.ifrs.edu.br/x/Resultado-Final-Pedagogia.pdf")[0] == "fora"
    titulos = ("Candidato\nNº\nInscrição\nAcesso*\n(AC, PPP ou PcD)\nPontuação\ntotal\nPESSOA DOCENTE DA SILVA\n08\nAC\n50"
               "\nOUTRA PESSOA DE SOUZA\n11\nAC\n45")
    assert destino(titulos, CAMPUS.replace("4-Monitorias-2026-Resultado-Final", "2-Resultado-Prova-de-Titulos"))[0] == "aviso"
    # cabecalhos de selecao do campus: coluna unida por barra, parentese entre colunas, coluna "Vínculo"
    selecao = CAMPUS.replace("4-Monitorias-2026-Resultado-Final", "4-Resultado-Final-do-Edital")
    assert motivo_lista("Inscrição/Candidato\nAcesso (AC, PP e\nPcD)*\nEntrevista\nPESSOA X DA SILVA\nAC\n37\n37\n1º",
                        selecao) == "tabela_de_resultado"
    assert motivo_lista("N.º inscrição\nCandidato\n(Ordem alfabética)\nData da\nprova\nHorário\n01\nPESSOA X DA SILVA\n"
                        "29/03/2023\n18h", selecao) == "tabela_de_resultado"
    assert motivo_lista("Inscrição\nNome\nVínculo\n46885-1\nPESSOA X DA SILVA\nPROFMAT", selecao) == "tabela_de_resultado"
    assert motivo_lista("Inscrição -\nCandidato\nAcesso (AC, PP e PcD)\nEntrevista\n03 - PESSOA X DA SILVA\nAC\n38",
                        selecao) == "tabela_de_resultado"
    # a primeira tabela vazia nao conta, a segunda com registro sim
    assert motivo_lista("Inscrição/Candidato\nAcesso\nNota Final\nNão houve candidatos\nPEDAGÓGICO\n"
                        "Inscrição/Candidato\nAcesso\nNota Final\nPESSOA X DA SILVA\nAC\n40", selecao) == "tabela_de_resultado"

    # ficha de candidato preenchida (resposta a recurso de prova) sai; a ficha em branco do formulario fica
    recurso = ("RECURSO DO GABARITO PRELIMINAR\nNúmero da Questão: 04\nNome do Candidato: PESSOA X DA SILVA "
               "Inscrição: 70285262\nTipo de Recurso: Anulação da Questão\nJustificativa do Candidato: a questão 4...")
    url_recurso = "https://ingresso.ifrs.edu.br/2025/x/Respostas-aos-Recursos-do-Gabarito.pdf"
    assert motivo_lista(recurso, url_recurso) == "ficha_de_candidato" and destino(recurso, url_recurso)[0] == "fora"
    assert not motivo_lista("ANEXO II - FORMULÁRIO DE RECURSO\nNome do Candidato: ____________________ "
                            "Inscrição: ______________\nCurso: ______________", url_recurso)
    # variacoes da ficha: "do(a)", valor na linha seguinte, rotulo "Candidato:" e inscricao antes do nome
    for ficha in ("Nome do(a) Candidato(a): PESSOA X DA SILVA Inscrição: 2886-42",
                  "Nome do Candidato:\nPESSOA X DA SILVA\nInscrição:\n70285262",
                  "Candidato: PESSOA X DA SILVA\nNº de inscrição: 73.317-77",
                  "Inscrição: 70285262\nNome do Candidato: PESSOA X DA SILVA"):
        assert motivo_lista("RECURSO DO GABARITO\n" + ficha, url_recurso) == "ficha_de_candidato", ficha
    # um ano ou um periodo no lugar da inscricao (etiqueta de envelope de um edital) nao e ficha
    assert not motivo_lista("Modelo de etiqueta do envelope\nNome do candidato: completo, sem abreviações\n"
                            "Inscrição: 2026/1\nCurso pretendido", "https://ingresso.ifrs.edu.br/x/EDITAL-No-15-2026.pdf")

    # nao sao tabela de resultado: capa de prova (so os dois campos), tabela vazia, frase de edital
    assert not motivo_lista("É permitida a reprodução para fins didáticos.\nNome do Candidato\nNúmero de Inscrição\n"
                            "IFRS – PS 2026/2 – Cursos Superiores\nPágina 2\nMATEMÁTICA\n01. Observe a sequência 300401",
                            "https://ingresso.ifrs.edu.br/x/CADERNO-DE-PROVA.pdf")
    assert not motivo_lista(cabecalho + "Observações:\nBento Gonçalves, 22/07/2025\n1 de 2",
                            "https://ingresso.ifrs.edu.br/x/Nao-ha-candidatos-Logistica.pdf")
    # numero de pagina solto longe do cabecalho de uma tabela vazia nao e registro
    assert not motivo_lista(cabecalho + "Observações:\n" + "Texto do rodapé da página. " * 20 + "\n2\nEdital",
                            "https://ingresso.ifrs.edu.br/x/Convocacao-sem-convocados.pdf")
    # anexo de formulario com linha de preenchimento e numero de pagina logo depois
    assert not motivo_lista("ANEXO III FORMULÁRIO DE RECURSO\nNome do candidato\nNúmero de inscrição\nCurso\n"
                            "____________________\n23\nEdital do processo seletivo",
                            "https://ingresso.ifrs.edu.br/x/EDITAL-DE-INGRESSO.pdf")
    assert not motivo_lista("O candidato deverá informar nome, número de inscrição e data de nascimento no recurso.\n12",
                            "https://ifrs.edu.br/canoas/editais/edital-x/", pagina=True)

    # o aviso diz onde conferir e nao traz nome
    aviso = aviso_lista(CAMPUS, "Resultado Final da Seleção de Monitores")
    assert "proteção de dados" in aviso and CAMPUS in aviso and not motivo_lista(aviso, CAMPUS)
    print("ok: listas de pessoas")


if __name__ == "__main__":
    testar()
