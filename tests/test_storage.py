"""Testes da deduplicação e do versionamento do acervo."""

import pytest

from src.models import Artigo, ResultadoGravacao
from src.normalize import hash_conteudo, normaliza_url
from src.storage import conecta, estatisticas, salva


def artigo(
    url: str = "https://exemplo.com/materia",
    titulo: str = "Título",
    resumo: str = "Resumo",
    conteudo: str = "Corpo",
    fonte: str = "Veículo",
) -> Artigo:
    return Artigo(
        fonte=fonte,
        titulo=titulo,
        url_original=url,
        url_norm=normaliza_url(url),
        resumo=resumo,
        conteudo=conteudo,
        data_publicacao="2026-08-25T12:00:00+00:00",
        hash_conteudo=hash_conteudo(titulo, resumo, conteudo),
    )


@pytest.fixture
def conexao(tmp_path):
    con = conecta(tmp_path / "teste.db")
    yield con
    con.close()


class TestDeduplicacao:
    def test_artigo_inedito_e_novo(self, conexao):
        assert salva(conexao, artigo()) is ResultadoGravacao.NOVO

    def test_mesmo_artigo_de_novo_e_duplicado(self, conexao):
        """O caso dominante: coletando a cada 30 min, quase tudo já é conhecido."""
        salva(conexao, artigo())
        assert salva(conexao, artigo()) is ResultadoGravacao.DUPLICADO

    def test_duplicata_nao_gera_registro(self, conexao):
        salva(conexao, artigo())
        salva(conexao, artigo())
        assert estatisticas(conexao)["registros"] == 1

    def test_urls_diferentes_sao_artigos_diferentes(self, conexao):
        salva(conexao, artigo(url="https://exemplo.com/a"))
        assert salva(conexao, artigo(url="https://exemplo.com/b")) is (
            ResultadoGravacao.NOVO
        )

    def test_rastreamento_diferente_nao_duplica(self, conexao):
        """Mesma matéria divulgada por dois canais chega com URLs distintas."""
        salva(conexao, artigo(url="https://exemplo.com/x?utm_source=twitter"))
        resultado = salva(conexao, artigo(url="https://exemplo.com/x?utm_source=rss"))
        assert resultado is ResultadoGravacao.DUPLICADO
        assert estatisticas(conexao)["registros"] == 1


class TestVersionamento:
    def test_conteudo_editado_e_atualizacao(self, conexao):
        """Retratação costuma ser edição da mesma página, sem trocar a URL."""
        salva(conexao, artigo(conteudo="Texto original"))
        resultado = salva(conexao, artigo(conteudo="Texto corrigido"))
        assert resultado is ResultadoGravacao.ATUALIZADO

    def test_versao_anterior_e_preservada(self, conexao):
        salva(conexao, artigo(conteudo="Texto original"))
        salva(conexao, artigo(conteudo="Texto corrigido"))

        linhas = conexao.execute(
            "SELECT versao, conteudo FROM artigos ORDER BY versao"
        ).fetchall()

        assert [linha["versao"] for linha in linhas] == [1, 2]
        assert linhas[0]["conteudo"] == "Texto original"
        assert linhas[1]["conteudo"] == "Texto corrigido"

    def test_materia_editada_conta_uma_vez(self, conexao):
        salva(conexao, artigo(conteudo="a"))
        salva(conexao, artigo(conteudo="b"))
        numeros = estatisticas(conexao)
        assert numeros["registros"] == 2
        assert numeros["materias"] == 1

    def test_volta_ao_conteudo_anterior_nao_gera_versao(self, conexao):
        """Veículo que desfaz a edição não deve inflar o histórico."""
        salva(conexao, artigo(conteudo="a"))
        salva(conexao, artigo(conteudo="b"))
        assert salva(conexao, artigo(conteudo="a")) is ResultadoGravacao.DUPLICADO
        assert estatisticas(conexao)["registros"] == 2


class TestEstatisticas:
    def test_acervo_vazio(self, conexao):
        assert estatisticas(conexao) == {
            "registros": 0,
            "materias": 0,
            "fontes": 0,
            "bytes_conteudo": 0,
        }

    def test_conta_fontes_distintas(self, conexao):
        salva(conexao, artigo(url="https://a.com/x", fonte="A"))
        salva(conexao, artigo(url="https://b.com/x", fonte="B"))
        assert estatisticas(conexao)["fontes"] == 2
