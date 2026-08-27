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
    veiculo: str = "Veículo",
    editoria: str = "Geral",
) -> Artigo:
    return Artigo(
        veiculo=veiculo,
        editoria=editoria,
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
            "veiculos": 0,
            "bytes_texto": 0,
        }

    def test_conta_veiculos_distintos(self, conexao):
        salva(conexao, artigo(url="https://a.com/x", veiculo="A"))
        salva(conexao, artigo(url="https://b.com/x", veiculo="B"))
        assert estatisticas(conexao)["veiculos"] == 2

    def test_editorias_do_mesmo_veiculo_contam_como_um(self, conexao):
        """Duas editorias da mesma redação não são fontes independentes.

        Contá-las como duas inflaria a corroboração e produziria "confirmado"
        onde há apenas um veículo publicando — o falso positivo que o
        princípio 5 manda evitar acima de tudo.
        """
        salva(conexao, artigo(url="https://g1.com/a", veiculo="G1", editoria="Política"))
        salva(conexao, artigo(url="https://g1.com/b", veiculo="G1", editoria="Economia"))
        assert estatisticas(conexao)["veiculos"] == 1


class TestCarregaUmaExtracaoPorMateria:
    """A mesma matéria extraída duas vezes não pode virar duas afirmações.

    Extrair a mesma matéria com outro modelo é o que `compare.py` exige para
    avaliar. Sem este recorte a avaliação virava o que o acervo lia: as triplas
    do teste, com os erros do teste, sem nada indicar isso.
    """

    def _monta(self, tmp_path):
        from src import llm
        from src.extract import Tripla
        from src.llm import Uso
        from src.storage import conecta, salva, salva_extracao

        conexao = conecta(tmp_path / "t.db")
        salva(conexao, artigo(url="https://x/1", titulo="T"))
        artigo_id = conexao.execute("SELECT id FROM artigos").fetchone()["id"]

        def tripla(objeto):
            return Tripla(
                sujeito="A", sujeito_canonico="A", relacao="outro",
                objeto=objeto, objeto_canonico=objeto, tipo_relacao="evento",
                origem="EXTRACTED", valor_numero=None, valor_unidade=None,
                valor_contexto=None, data_fato=None, sentenca=0,
            )

        uso = Uso(modelo=llm.EXTRACAO, entrada=1, saida=1,
                  cache_leitura=0, cache_escrita=0)
        salva_extracao(conexao, artigo_id, [tripla("do modelo ativo")],
                       llm.EXTRACAO.id, "v1", 1, uso)
        salva_extracao(conexao, artigo_id, [tripla("de outro modelo")],
                       "modelo-de-teste", "v2", 1, uso)
        return conexao

    def test_so_a_extracao_do_modelo_ativo_entra(self, tmp_path):
        """Mais recente não basta: a do teste foi gravada depois."""
        from src import grafo

        conexao = self._monta(tmp_path)
        afirmacoes = grafo.carrega(conexao)
        assert [a.objeto for a in afirmacoes] == ["do modelo ativo"]
        conexao.close()

    def test_janela_de_data_nao_desfaz_o_recorte(self, tmp_path):
        """A janela é um segundo parâmetro na mesma consulta — fácil de trocar
        de posição e passar a filtrar pelo modelo errado sem erro nenhum."""
        from src import grafo

        conexao = self._monta(tmp_path)
        afirmacoes = grafo.carrega(conexao, desde="2000-01-01")
        assert [a.objeto for a in afirmacoes] == ["do modelo ativo"]
        conexao.close()
