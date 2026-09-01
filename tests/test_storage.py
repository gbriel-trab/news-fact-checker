"""Testes da deduplicação e do versionamento do acervo."""

from datetime import datetime, timedelta, timezone

import pytest

from src.models import Artigo, ResultadoGravacao
from src.normalize import hash_conteudo, normaliza_url
from src.storage import conecta, estatisticas, salva

# Dinâmica, não fixa: a janela de agrupamento (agrupa.JANELA_DIAS) filtra
# por data de publicação, e uma data congelada faria a suíte inteira
# apodrecer em silêncio quando o fixture envelhecesse para fora da janela.
ONTEM = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def artigo(
    url: str = "https://exemplo.com/materia",
    titulo: str = "Título",
    resumo: str = "Resumo",
    conteudo: str = "Corpo",
    veiculo: str = "Veículo",
    editoria: str = "Geral",
    data_publicacao: str = ONTEM,
) -> Artigo:
    return Artigo(
        veiculo=veiculo,
        editoria=editoria,
        titulo=titulo,
        url_original=url,
        url_norm=normaliza_url(url),
        resumo=resumo,
        conteudo=conteudo,
        data_publicacao=data_publicacao,
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
        # vocab 2: dentro do conjunto compatível — o teste é sobre o
        # filtro de MODELO, não o de vocabulário.
        salva_extracao(conexao, artigo_id, [tripla("do modelo ativo")],
                       llm.EXTRACAO.id, "v1", 2, uso)
        salva_extracao(conexao, artigo_id, [tripla("de outro modelo")],
                       "modelo-de-teste", "v2", 2, uso)
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


class TestConsultas:
    """O par (afirmação que chegou, veredito que saiu) é o único registro de
    avaliação que o sistema tem. Sem ele não há como medir acerto depois."""

    def test_grava_e_conta_custo(self, tmp_path):
        from src.storage import conecta, salva_consulta

        conexao = conecta(tmp_path / "t.db")
        salva_consulta(conexao, "o salário mínimo vai a R$ 1.741",
                       "confirmado", "duas fontes afirmam", 10, 2, 2,
                       "modelo-x", 0.028)
        salva_consulta(conexao, "o BoJ elevou os juros", "sem_evidencia",
                       "nada no acervo", 0, 0, 0, "modelo-x", 0.015)

        linhas = conexao.execute(
            "SELECT veredito, custo_usd FROM consultas ORDER BY id").fetchall()
        assert [l["veredito"] for l in linhas] == ["confirmado", "sem_evidencia"]
        assert sum(l["custo_usd"] for l in linhas) == pytest.approx(0.043)
        conexao.close()

    def test_veredito_fora_do_dominio_e_recusado(self, tmp_path):
        """O CHECK repete validação que o Pydantic já faz na entrada. É defesa
        em profundidade: import ou correção manual não passam pelo Pydantic."""
        import sqlite3

        from src.storage import conecta, salva_consulta

        conexao = conecta(tmp_path / "t.db")
        with pytest.raises(sqlite3.IntegrityError):
            salva_consulta(conexao, "x", "provavelmente", "y", 1, 1, 1, "m", 0.0)
        conexao.close()


class TestCacheNasExtracoes:
    """Cache lido custa 0,1x da entrada e cache escrito custa 1,25x. Somados
    num campo só, o acervo registra o custo mas não sabe mais de onde veio."""

    def test_grava_a_reparticao(self, tmp_path):
        from src import llm
        from src.llm import Uso
        from src.storage import conecta, salva, salva_extracao

        conexao = conecta(tmp_path / "t.db")
        salva(conexao, artigo(url="https://x/1"))
        artigo_id = conexao.execute("SELECT id FROM artigos").fetchone()["id"]
        salva_extracao(conexao, artigo_id, [], llm.EXTRACAO.id, "v1", 1,
                       Uso(modelo=llm.EXTRACAO, entrada=200, saida=900,
                           cache_leitura=2400, cache_escrita=0))

        linha = conexao.execute(
            "SELECT tokens_entrada e, tokens_cache_leitura r, "
            "tokens_cache_escrita w FROM extracoes").fetchone()
        assert linha["e"] == 2600  # total, como sempre foi
        assert (linha["r"], linha["w"]) == (2400, 0)
        conexao.close()

    def test_migracao_e_idempotente(self, tmp_path):
        """`conecta` roda a migração toda vez. Rodar duas vezes não pode
        falhar — ADD COLUMN de coluna existente é erro no SQLite."""
        from src.storage import conecta

        caminho = tmp_path / "t.db"
        conecta(caminho).close()
        conexao = conecta(caminho)
        colunas = {l[1] for l in conexao.execute("PRAGMA table_info(extracoes)")}
        assert {"tokens_cache_leitura", "tokens_cache_escrita"} <= colunas
        conexao.close()

    def test_banco_antigo_ganha_as_colunas(self, tmp_path):
        """Linhas gravadas antes ficam NULL — que é a resposta honesta: para
        aquelas o dado não foi guardado.

        O banco "antigo" é o esquema REAL com as duas colunas novas removidas,
        e não um esboço à mão: assim o teste continua valendo quando o resto do
        esquema mudar.
        """
        import re
        import sqlite3

        from src.storage import ESQUEMA, conecta

        antes = re.sub(r"\s*tokens_cache_(leitura|escrita) INTEGER,", "",
                       ESQUEMA)
        assert "tokens_cache_leitura" not in antes

        caminho = tmp_path / "velho.db"
        antigo = sqlite3.connect(caminho)
        antigo.executescript(antes)
        antigo.execute(
            "INSERT INTO extracoes (artigo_id, modelo, prompt_versao, "
            "vocab_versao, tokens_entrada, tokens_saida, custo_usd, "
            "extraido_em) VALUES (1, 'm', 'v', 1, 5000, 1000, 0.1, 'x')")
        antigo.commit()
        antigo.close()

        conexao = conecta(caminho)
        linha = conexao.execute(
            "SELECT tokens_entrada e, tokens_cache_leitura r FROM extracoes"
        ).fetchone()
        assert linha["e"] == 5000 and linha["r"] is None
        conexao.close()
