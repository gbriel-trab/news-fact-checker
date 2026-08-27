"""Testes da seleção de matérias para extração.

O que importa aqui é dinheiro: cada matéria selecionada é uma chamada paga, e
a seleção errada gasta sem poder produzir confirmação nenhuma.
"""

from src import extract
from src.storage import conecta, salva, salva_extracao
from tests.test_storage import artigo


def _base(tmp_path, materias):
    """materias: lista de (veiculo, titulo, tamanho_do_texto, ja_extraida)."""
    from src import llm
    from src.llm import Uso

    conexao = conecta(tmp_path / "t.db")
    uso = Uso(modelo=llm.EXTRACAO, entrada=1, saida=1,
              cache_leitura=0, cache_escrita=0)
    for i, (veiculo, titulo, tamanho, extraida) in enumerate(materias):
        salva(conexao, artigo(url=f"https://x/{i}", titulo=titulo,
                              veiculo=veiculo, conteudo="c" * tamanho))
        if extraida:
            linha = conexao.execute(
                "SELECT id FROM artigos WHERE url_norm LIKE ?",
                (f"%/{i}",)).fetchone()
            salva_extracao(conexao, linha["id"], [], llm.EXTRACAO.id,
                           "v1", 1, uso)
    return conexao


TITULO_A = "Caixa tem lucro de R$ 3,9 bilhões no segundo trimestre"
TITULO_B = "Lucro da Caixa cresce 5,9% e chega a R$ 3,9 bilhões"
TITULO_OUTRO = "Eclipse lunar quase total será visível no Brasil hoje"


class TestParParcial:
    def test_completa_o_par_quando_metade_ja_foi_extraida(self, tmp_path):
        """O caso mais barato de todos: uma chamada fecha uma confirmação.

        Contar só as pendentes descartava exatamente esta história.
        """
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 2000, True),
            ("Folha", TITULO_B, 2000, False),
        ])
        escolhidas = extract._por_historia(conexao, 5)
        assert [m["veiculo"] for m in escolhidas] == ["Folha"]
        conexao.close()

    def test_nao_seleciona_quando_tudo_ja_foi_extraido(self, tmp_path):
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 2000, True),
            ("Folha", TITULO_B, 2000, True),
        ])
        assert extract._por_historia(conexao, 5) == []
        conexao.close()


class TestFonteUnica:
    def test_veiculo_sozinho_nao_entra(self, tmp_path):
        """Não pode ser corroborado por definição — a chamada seria gasto sem
        retorno possível. Continua no acervo; só não entra nesta fila."""
        conexao = _base(tmp_path, [("G1", TITULO_A, 2000, False)])
        assert extract._por_historia(conexao, 5) == []
        conexao.close()

    def test_duas_materias_do_mesmo_veiculo_nao_formam_par(self, tmp_path):
        """Mesma redação publicando duas vezes não é confirmação independente."""
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 2000, False),
            ("G1", TITULO_B, 2000, False),
        ])
        assert extract._por_historia(conexao, 5) == []
        conexao.close()


class TestPeneiraSemantica:
    def test_titulos_de_assuntos_diferentes_nao_formam_par(self, tmp_path):
        """Termo em comum não é assunto em comum. Ver `_por_historia`."""
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 2000, False),
            ("Folha", TITULO_OUTRO, 2000, False),
        ])
        assert extract._por_historia(conexao, 5) == []
        conexao.close()


class TestTextoInsuficiente:
    def test_so_manchete_nao_entra(self, tmp_path):
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 2000, False),
            ("Folha", TITULO_B, 100, False),
        ])
        assert extract._por_historia(conexao, 5) == []
        conexao.close()
