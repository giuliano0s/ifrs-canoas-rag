"""Testes do campus de uma pagina do portal de ingresso (campus_da_pagina em pipelines/chunker.py), com textos fixos e sem rede.

Uso, a partir da raiz: python -m tests.test_paginas_de_outro_campus
"""
from pipelines.chunker import campus_da_pagina, campus_do_pdf, e_canoas

PORTAL = "https://ingresso.ifrs.edu.br/2026/cursos/tecnico-em-eletromecanica/"


def de_outro_campus(url, titulo, texto):
    campus = campus_da_pagina(url, titulo, texto)
    return bool(campus) and not e_canoas(campus)


def testar():
    # pagina de curso: o campo "Curso oferecido no Campus:" com o nome na linha seguinte, no texto extraido
    curso = ("Técnico em Eletromecânica\nDuração:\n3 anos\nVagas:\n30\nCurso oferecido no Campus:\n{campus}\n"
             "Outros Cursos do mesmo Campus\nTécnico em Administração")
    assert campus_da_pagina(PORTAL, "Técnico em Eletromecânica - Processo Seletivo IFRS 2026/1",
                            curso.format(campus="Farroupilha")) == "Farroupilha"
    assert de_outro_campus(PORTAL, "", curso.format(campus="Zona Norte (Porto Alegre)"))
    assert not de_outro_campus(PORTAL, "", curso.format(campus="Canoas"))
    assert campus_da_pagina(PORTAL, "", "Curso oferecido no Campus: Bento Gonçalves") == "Bento Gonçalves"

    # paginas de campus e de chamada: pelo endereco e pelo titulo
    assert de_outro_campus("https://ingresso.ifrs.edu.br/2025/campus/riogrande/", "Rio Grande - Processo Seletivo", "")
    assert not de_outro_campus("https://ingresso.ifrs.edu.br/2025/campus/canoas/", "Canoas - Processo Seletivo", "")
    assert de_outro_campus("https://ingresso.ifrs.edu.br/2025/chamadas/1a-chamada-15/",
                           "1ª CHAMADA | Prova | Campus Bento Gonçalves - Processo Seletivo IFRS 2025/1", "")
    assert not de_outro_campus("https://ingresso.ifrs.edu.br/2025/chamadas/chamada-publica-7/",
                               "CHAMADA PÚBLICA | ENEM | Campus Canoas - Processo Seletivo IFRS 2025/1", "")

    # titulo que nomeia a unidade em qualquer posicao; a palavra "campus" sem unidade nao conta
    assert de_outro_campus("https://ingresso.ifrs.edu.br/2026-2/editais/edital-no-8-2026/",
                           "EDITAL Nº 8/2026 - INGRESSO NO SEGUNDO SEMESTRE DE 2026 NO CAMPUS ZONA NORTE (PORTO ALEGRE)", "")
    assert de_outro_campus("https://ingresso.ifrs.edu.br/2026/publicacoes/ps-viamao/",
                           "PS CAMPUS VIAMÃO - PRORROGAÇÃO DO PRAZO PARA RECURSO", "")
    assert campus_da_pagina("https://ingresso.ifrs.edu.br/2026/estudantes-do-campus-participam/",
                            "Estudantes do campus participam de feira", "") is None

    # PDF do portal pelo timbre: "Campus X" nas primeiras linhas e nenhuma citacao a Canoas no documento
    timbre = ("MINISTÉRIO DA EDUCAÇÃO\nSecretaria de Educação Profissional e Tecnológica\nInstituto Federal de Educação, "
              "Ciência e Tecnologia do Rio Grande do Sul\n{unidade}\nORIENTAÇÕES SOBRE O INÍCIO DAS AULAS\n{corpo}")
    pdf = "https://ingresso.ifrs.edu.br/2025/wp-content/uploads/sites/35/2025/02/ORIENTACOES.pdf"
    assert campus_do_pdf(pdf, timbre.format(unidade="Campus Porto Alegre", corpo="Compareça ao bloco A.")) == "porto alegre"
    assert campus_do_pdf(pdf, timbre.format(unidade="CAMPUS VIAMÃO", corpo="Banca no dia 20.")) == "viamao"
    assert e_canoas(campus_do_pdf(pdf, timbre.format(unidade="Campus Porto Alegre",
                                                     corpo="Quadro de vagas: Campus Canoas, 30 vagas.")))
    assert campus_do_pdf(pdf, timbre.format(unidade="Gabinete do Reitor",
                                            corpo="Edital geral. O exame ocorre no Campus Osório.")) is None
    assert campus_do_pdf("https://ifrs.edu.br/wp-content/uploads/2026/06/Regulamento.pdf",
                         timbre.format(unidade="Campus Porto Alegre", corpo="Evento aberto a todos os campi.")) is None
    # unidade escrita como "Campus Avançado X", no titulo e no timbre
    assert de_outro_campus("https://ingresso.ifrs.edu.br/2027/publicacoes/x/",
                           "Resultado | Campus Avançado Veranópolis - Processo Seletivo", "")
    assert campus_do_pdf(pdf, timbre.format(unidade="Campus Avançado Veranópolis", corpo="Banca.")) == "veranopolis"
    # "Rio Grande do Sul" do timbre nao e o Campus Rio Grande
    assert campus_do_pdf(pdf, timbre.format(unidade="Pró-Reitoria de Ensino", corpo="Edital geral.")) is None

    # nao pertencem a um campus so: listagem de cursos e de campi, edital, cronograma
    assert campus_da_pagina("https://ingresso.ifrs.edu.br/2027/campi/", "Campi deste Processo Seletivo",
                            "Campus\nAlvorada\nEndereço:\nRua Professor Darcy Ribeiro") is None
    assert campus_da_pagina("https://ingresso.ifrs.edu.br/2026/cursos/", "Cursos - Processo Seletivo IFRS 2026/1",
                            "Curso\nCampus\nNível\nBacharelado em Administração\nErechim\nGraduação") is None
    assert campus_da_pagina("https://ingresso.ifrs.edu.br/2026-2/cronograma/", "Cronograma - Processo Seletivo",
                            "17/06/2026\nPublicação da Classificação Geral") is None

    # fora do portal nunca: edital de Canoas com a frase em prosa e noticia do site do campus que cita outro campus
    assert campus_da_pagina("https://ifrs.edu.br/canoas/wp-content/uploads/sites/6/2026/03/Edital-troca-de-curso.pdf", "",
                            "poderão solicitar transferência para curso oferecido no Campus:\nOsório") is None
    assert campus_da_pagina("https://ifrs.edu.br/canoas/estudantes-visitam-o-campus-osorio/",
                            "Estudantes visitam | Campus Osório - IFRS", "") is None
    # no portal, a prosa sem os dois-pontos do campo nao conta
    assert campus_da_pagina("https://ingresso.ifrs.edu.br/2026/publicacoes/edital-x/", "Edital X - Processo Seletivo",
                            "estudantes de curso oferecido no Campus Osório, desde que estejam no segundo semestre") is None
    print("ok: paginas de outro campus")


if __name__ == "__main__":
    testar()
