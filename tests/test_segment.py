"""Testes da segmentação em sentenças.

A sentença é a unidade que o modelo recebe numerada e referencia de volta em
cada tripla. Quebra errada aqui vira índice errado na tripla, e a rastreabilidade
até a origem — que é o que o princípio de citar a fonte exige — se perde sem
levantar erro.
"""

from src.segment import em_sentencas


class TestQuebraBasica:
    def test_separa_por_ponto(self):
        assert em_sentencas(
            "O ministro anunciou a medida ontem. A oposição criticou a decisão."
        ) == [
            "O ministro anunciou a medida ontem.",
            "A oposição criticou a decisão.",
        ]

    def test_separa_por_interrogacao_e_exclamacao(self):
        assert len(em_sentencas(
            "Quem autorizou a operação policial? Ninguém soube responder ainda! "
            "O caso segue sob investigação."
        )) == 3

    def test_texto_vazio(self):
        assert em_sentencas("") == []
        assert em_sentencas("   ") == []


class TestFalsosFins:
    """Os casos em que o ponto não termina a frase."""

    def test_nao_quebra_em_abreviacao(self):
        assert em_sentencas(
            "O Sr. Silva assumiu a pasta em janeiro deste ano."
        ) == ["O Sr. Silva assumiu a pasta em janeiro deste ano."]

    def test_nao_quebra_em_separador_de_milhar(self):
        """'R$ 1.000' viraria duas sentenças e o valor se perderia."""
        assert em_sentencas(
            "O contrato prevê repasse de R$ 1.250.000 ao município neste ano."
        ) == ["O contrato prevê repasse de R$ 1.250.000 ao município neste ano."]

    def test_nao_quebra_em_numero_de_lei(self):
        assert len(em_sentencas(
            "A Lei 8.666 foi revogada pelo novo marco de licitações."
        )) == 1

    def test_nao_quebra_em_inicial_de_nome(self):
        assert len(em_sentencas(
            "O relator Luiz F. da Costa votou pela rejeição do parecer."
        )) == 1

    def test_abreviacao_no_meio_e_fim_real_no_final(self):
        resultado = em_sentencas(
            "O Dr. Antunes assinou o laudo pericial. O documento foi anexado."
        )
        assert resultado == [
            "O Dr. Antunes assinou o laudo pericial.",
            "O documento foi anexado.",
        ]


class TestDescartes:
    def test_descarta_fragmento_curto(self):
        """Legenda de foto e crédito não sustentam afirmação verificável."""
        resultado = em_sentencas(
            "Foto: Reuters. O presidente assinou o decreto na manhã desta terça."
        )
        assert resultado == [
            "O presidente assinou o decreto na manhã desta terça."
        ]

    def test_preserva_sentenca_curta_mas_valida(self):
        """Regressão: o corte era por caracteres e engolia afirmação boa.

        "O STF decidiu." tem os mesmos 14 caracteres de "Foto: Reuters." e é
        perfeitamente verificável. O que separa os dois é ter verbo, não
        comprimento.
        """
        assert em_sentencas(
            "O relator leu o voto. O STF decidiu. A sessão foi encerrada."
        ) == [
            "O relator leu o voto.",
            "O STF decidiu.",
            "A sessão foi encerrada.",
        ]

    def test_descarta_chamada_de_navegacao(self):
        assert em_sentencas(
            "Leia mais. O contrato foi assinado pelo ministro nesta quinta."
        ) == ["O contrato foi assinado pelo ministro nesta quinta."]

    def test_preserva_aspas_de_fechamento_na_sentenca(self):
        resultado = em_sentencas(
            'O relator disse que "a decisão é definitiva". '
            "O julgamento terminou em seguida."
        )
        assert len(resultado) == 2
        assert resultado[0].endswith('".')
