"""Testes da persistência de triplas.

Extração é a única etapa paga do projeto. Falha aqui não aparece como erro:
aparece como dinheiro gasto sem acervo, ou como acervo que a varredura de
contradição lê errado.
"""

import pytest

from src.extract import Extracao, Tripla
from src import llm
from src.llm import Uso
from src.storage import (
    conecta, estatisticas_triplas, ja_extraido, salva, salva_extracao)
from tests.test_storage import artigo


def tripla(sujeito="Vale S.A.", relacao="outro", objeto="Ferrous",
           valor=None, unidade=None, data_fato="2026-08-19", sentenca=0):
    return Tripla(
        sujeito=sujeito, sujeito_canonico=sujeito,
        relacao=relacao,
        objeto=objeto, objeto_canonico=objeto,
        tipo_relacao="evento", origem="EXTRACTED",
        valor_numero=valor, valor_unidade=unidade, valor_contexto=None,
        data_fato=data_fato, sentenca=sentenca,
    )


USO = Uso(modelo=llm.EXTRACAO, entrada=1000, saida=2000,
          cache_leitura=0, cache_escrita=0)


@pytest.fixture
def conexao(tmp_path):
    con = conecta(tmp_path / "t.db")
    salva(con, artigo())
    yield con
    con.close()


def artigo_id(con):
    return con.execute("SELECT id FROM artigos LIMIT 1").fetchone()["id"]


class TestGravacao:
    def test_grava_triplas(self, conexao):
        salva_extracao(conexao, artigo_id(conexao),
                       [tripla(), tripla(relacao="divulgou")],
                       "claude-opus-5", "abc123", 0, USO)
        assert estatisticas_triplas(conexao)["triplas"] == 2

    def test_registra_custo_da_extracao(self, conexao):
        salva_extracao(conexao, artigo_id(conexao), [tripla()],
                       "claude-opus-5", "abc123", 0, USO)
        assert estatisticas_triplas(conexao)["custo"] == pytest.approx(USO.custo)

    def test_atributo_grava_objeto_nulo(self, conexao):
        """Margem de erro é propriedade da pesquisa, não relação com entidade."""
        t = tripla(relacao="tem_atributo", objeto=None,
                   valor=2, unidade="pontos percentuais")
        salva_extracao(conexao, artigo_id(conexao), [t],
                       "claude-opus-5", "abc123", 0, USO)
        linha = conexao.execute(
            "SELECT objeto, objeto_canonico, valor_numero FROM triplas").fetchone()
        assert linha["objeto"] is None
        assert linha["objeto_canonico"] is None
        assert linha["valor_numero"] == 2


class TestNaoPagarDuasVezes:
    def test_reconhece_extracao_ja_feita(self, conexao):
        aid = artigo_id(conexao)
        assert not ja_extraido(conexao, aid, "claude-opus-5", "abc123")
        salva_extracao(conexao, aid, [tripla()], "claude-opus-5", "abc123", 0, USO)
        assert ja_extraido(conexao, aid, "claude-opus-5", "abc123")

    def test_prompt_diferente_permite_extrair_de_novo(self, conexao):
        """Prompt novo produz resultado diferente: não é a mesma extração."""
        aid = artigo_id(conexao)
        salva_extracao(conexao, aid, [tripla()], "claude-opus-5", "abc123", 0, USO)
        assert not ja_extraido(conexao, aid, "claude-opus-5", "def456")

    def test_modelo_diferente_permite_extrair_de_novo(self, conexao):
        """Sustenta a comparação entre modelos sobre a mesma matéria."""
        aid = artigo_id(conexao)
        salva_extracao(conexao, aid, [tripla()], "claude-opus-5", "abc123", 0, USO)
        assert not ja_extraido(conexao, aid, "outro-modelo", "abc123")

    def test_as_duas_extracoes_convivem(self, conexao):
        aid = artigo_id(conexao)
        salva_extracao(conexao, aid, [tripla()], "claude-opus-5", "abc123", 0, USO)
        salva_extracao(conexao, aid, [tripla(), tripla()], "outro", "abc123", 0, USO)
        n = estatisticas_triplas(conexao)
        assert n["materias"] == 2 and n["triplas"] == 3


class TestIntegridade:
    def test_extracao_repetida_e_recusada(self, conexao):
        """A restrição do banco impede pagar duas vezes por engano."""
        import sqlite3
        aid = artigo_id(conexao)
        salva_extracao(conexao, aid, [tripla()], "claude-opus-5", "abc123", 0, USO)
        with pytest.raises(sqlite3.IntegrityError):
            salva_extracao(conexao, aid, [tripla()], "claude-opus-5", "abc123", 0, USO)

    def test_falha_no_meio_nao_deixa_extracao_sem_triplas(self, conexao):
        """Extração órfã seria lida como matéria sem afirmação nenhuma."""
        import sqlite3
        ruim = tripla()
        object.__setattr__(ruim, "origem", "TALVEZ")
        with pytest.raises(sqlite3.IntegrityError):
            salva_extracao(conexao, artigo_id(conexao), [tripla(), ruim],
                           "claude-opus-5", "abc123", 0, USO)
        assert estatisticas_triplas(conexao)["materias"] == 0
        assert estatisticas_triplas(conexao)["triplas"] == 0

    def test_banco_recusa_origem_fora_do_dominio(self, conexao):
        """O Pydantic já barra na entrada; o CHECK protege quem escrever direto."""
        import sqlite3
        ruim = tripla()
        object.__setattr__(ruim, "origem", "TALVEZ")
        with pytest.raises(sqlite3.IntegrityError):
            salva_extracao(conexao, artigo_id(conexao), [ruim],
                           "claude-opus-5", "abc123", 0, USO)

    def test_tipo_de_relacao_e_DERIVADO_nao_aceito(self, conexao):
        """Desde 03/09/2026 o tipo vem da RELAÇÃO, não do modelo: dez das
        39 relações saíam com os dois tipos (tem_atributo: 460 estado e
        79 evento). Lixo do modelo não chega mais ao CHECK do banco
        porque é corrigido antes — garantia mais forte que rejeitar."""
        ruim = tripla()
        object.__setattr__(ruim, "tipo_relacao", "processo")
        object.__setattr__(ruim, "relacao", "integra")
        salva_extracao(conexao, artigo_id(conexao), [ruim],
                       "claude-opus-5", "abc123", 0, USO)
        gravado = conexao.execute(
            "SELECT tipo_relacao FROM triplas").fetchone()[0]
        assert gravado == "estado", gravado

    def test_o_CHECK_do_banco_continua_de_pe(self, conexao):
        """A derivação não substitui a guarda do esquema: escrita por
        fora do salva_extracao ainda tem de ser recusada."""
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            conexao.execute(
                "INSERT INTO triplas (extracao_id, sentenca, sujeito, "
                "sujeito_canonico, relacao, tipo_relacao, origem) "
                "VALUES (1, 0, 'x', 'x', 'integra', 'processo', "
                "'EXTRACTED')")

    def test_propriedade_e_recorte_sao_gravados(self, conexao):
        """Patch 5: os campos que decidem se dois numeros medem a MESMA
        coisa. Sem persistir, a chave nao existe na leitura e o grafo cai
        no embedding, que separou 9 de 15 pares da mesma medida."""
        t = tripla()
        object.__setattr__(t, "valor_numero", 1741.0)
        object.__setattr__(t, "valor_propriedade", "salario_minimo")
        object.__setattr__(t, "valor_recorte", "2027")
        salva_extracao(conexao, artigo_id(conexao), [t],
                       "claude-opus-5", "abc123", 0, USO)
        r = conexao.execute(
            "SELECT valor_propriedade, valor_recorte FROM triplas").fetchone()
        assert (r[0], r[1]) == ("salario_minimo", "2027")

    def test_tripla_sem_os_campos_novos_continua_gravando(self, conexao):
        """A safra antiga e o modelo que omitir: nao pode quebrar."""
        salva_extracao(conexao, artigo_id(conexao), [tripla()],
                       "claude-opus-5", "abc123", 0, USO)
        r = conexao.execute(
            "SELECT valor_propriedade FROM triplas").fetchone()
        assert r[0] is None

    def test_outro_mantem_o_palpite_do_modelo(self, conexao):
        """`outro` é a válvula de escape — 397 evento e 225 estado no
        acervo — e não há o que derivar. É a única exceção."""
        t = tripla()
        object.__setattr__(t, "relacao", "outro")
        object.__setattr__(t, "tipo_relacao", "estado")
        salva_extracao(conexao, artigo_id(conexao), [t],
                       "claude-opus-5", "abc123", 0, USO)
        assert conexao.execute(
            "SELECT tipo_relacao FROM triplas").fetchone()[0] == "estado"


class TestEstatisticas:
    def test_acervo_vazio(self, conexao):
        n = estatisticas_triplas(conexao)
        assert n["triplas"] == 0 and n["relacoes"] == 0 and n["custo"] == 0

    def test_conta_relacoes_e_entidades_distintas(self, conexao):
        salva_extracao(
            conexao, artigo_id(conexao),
            [tripla(sujeito="A", relacao="afirmou"),
             tripla(sujeito="A", relacao="criticou"),
             tripla(sujeito="B", relacao="afirmou")],
            "claude-opus-5", "abc123", 0, USO)
        n = estatisticas_triplas(conexao)
        assert n["relacoes"] == 2
        assert n["entidades"] == 2


class TestDescartaVazias:
    """Tripla sem objeto e sem valor não afirma nada.

    Pior que ocupar espaço: ela parece uma afirmação registrada quando a
    afirmação se perdeu. Apareceu de verdade — a regra de atributo nulo vazou
    para a atribuição e produziu seis `(Fulano, afirmou, null)` numa matéria.
    """

    def test_mantem_tripla_com_objeto(self):
        from src.extract import descarta_vazias
        boas, vazias = descarta_vazias([tripla()])
        assert len(boas) == 1 and vazias == 0

    def test_mantem_atributo_com_valor_e_sem_objeto(self):
        from src.extract import descarta_vazias
        t = tripla(relacao="tem_atributo", objeto=None,
                   valor=2, unidade="pontos percentuais")
        boas, vazias = descarta_vazias([t])
        assert len(boas) == 1 and vazias == 0

    def test_descarta_declaracao_sem_conteudo(self):
        from src.extract import descarta_vazias
        t = tripla(relacao="afirmou", objeto=None)
        boas, vazias = descarta_vazias([t])
        assert boas == [] and vazias == 1

    def test_conta_quantas_cairam(self):
        from src.extract import descarta_vazias
        entrada = [tripla(), tripla(relacao="afirmou", objeto=None),
                   tripla(relacao="criticou", objeto=None), tripla()]
        boas, vazias = descarta_vazias(entrada)
        assert len(boas) == 2 and vazias == 2


class TestObjetoECanonicoAndamJuntos:
    """O schema não impõe os dois campos em conjunto, e o modelo às vezes
    preenche um só. `descarta_vazias` olha apenas o canônico — a tripla com
    objeto e sem canônico morria em silêncio, levando o fato principal da
    matéria junto (medido em 29/08/2026, nos pares de cripto)."""

    def test_canonico_vazio_herda_o_objeto(self):
        t = Tripla(
            sujeito="Core Lightning", sujeito_canonico="Core Lightning",
            relacao="recomendou",
            objeto="atualização de segurança", objeto_canonico=None,
            tipo_relacao="evento", origem="EXTRACTED",
            valor_numero=None, valor_unidade=None, valor_contexto=None,
            data_fato=None, sentenca=0,
        )
        assert t.objeto_canonico == "atualização de segurança"

    def test_atributo_sem_objeto_continua_sem_canonico(self):
        t = tripla(relacao="tem_atributo", objeto=None, valor=71,
                   unidade="investidores")
        assert t.objeto is None and t.objeto_canonico is None

    def test_meia_tripla_deixa_de_ser_descartada(self):
        from src.extract import descarta_vazias
        t = Tripla(
            sujeito="StarkWare", sujeito_canonico="StarkWare",
            relacao="lancou",
            objeto="transação resistente a ataque quântico",
            objeto_canonico=None,
            tipo_relacao="evento", origem="EXTRACTED",
            valor_numero=None, valor_unidade=None, valor_contexto=None,
            data_fato=None, sentenca=0,
        )
        boas, vazias = descarta_vazias([t])
        assert len(boas) == 1 and vazias == 0
