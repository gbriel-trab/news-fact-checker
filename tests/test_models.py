"""Testes das estruturas de dados da coleta."""

from tests.test_storage import artigo


class TestTextoDisponivel:
    """Feeds não usam os campos de forma consistente, e a extração precisa do
    texto mais longo independentemente de onde o veículo o colocou."""

    def test_prefere_conteudo_quando_ele_traz_a_materia(self):
        item = artigo(resumo="Linha fina.", conteudo="Corpo inteiro da matéria.")
        assert item.texto == "Corpo inteiro da matéria."

    def test_usa_resumo_quando_o_feed_nao_preenche_conteudo(self):
        """Caso do G1 e da Agência Brasil: matéria completa vem em `summary`."""
        item = artigo(resumo="Matéria completa, com vários parágrafos.", conteudo="")
        assert item.texto == "Matéria completa, com vários parágrafos."

    def test_sem_texto_algum_devolve_vazio(self):
        assert artigo(resumo="", conteudo="").texto == ""
