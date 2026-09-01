"""As peças puras do modo história (v3): validação de origens, explosão
por fonte e versionamento.

A trava central está em `valida_origens`: `origens` é o modelo AFIRMANDO
que cada fonte disse — origem inválida que passasse viraria corroboração
fabricada, o pior erro do projeto.
"""

from src import extract, llm
from src.extract import (Origem, TriplaHistoria, _tripla_da_fonte,
                         valida_origens)
from src.llm import Uso
from src.storage import conecta, salva, salva_extracao
from tests.test_storage import artigo


def th(origens, sujeito="Braskem", relacao="solicitou",
       objeto="Recuperação extrajudicial da Braskem"):
    return TriplaHistoria(
        sujeito=sujeito, sujeito_canonico=sujeito, relacao=relacao,
        objeto=objeto, objeto_canonico=objeto, tipo_relacao="evento",
        origem="EXTRACTED", valor_numero=None, valor_unidade=None,
        valor_contexto=None, data_fato="2026-08-26",
        origens=[Origem(fonte=f, sentenca=s) for f, s in origens],
    )


class TestValidaOrigens:
    def test_origem_valida_passa(self):
        boas, fora = valida_origens([th([("A", 0), ("B", 2)])],
                                    {"A": 3, "B": 3})
        assert len(boas) == 1 and fora == 0

    def test_fonte_inexistente_cai(self):
        # O modelo afirmou que a fonte C disse — mas não há fonte C.
        boas, _ = valida_origens([th([("A", 0), ("C", 0)])],
                                 {"A": 3, "B": 3})
        assert [o.fonte for o in boas[0].origens] == ["A"]

    def test_sentenca_fora_do_texto_cai(self):
        boas, _ = valida_origens([th([("A", 0), ("B", 99)])],
                                 {"A": 3, "B": 3})
        assert [o.fonte for o in boas[0].origens] == ["A"]

    def test_tripla_sem_origem_valida_morre(self):
        boas, fora = valida_origens([th([("C", 0), ("A", 99)])],
                                    {"A": 3, "B": 3})
        assert boas == [] and fora == 1


class TestExplosao:
    def test_tripla_da_fonte_carrega_a_sentenca_certa(self):
        t = th([("A", 1), ("B", 4)])
        comum = _tripla_da_fonte(t, 4)
        assert comum.sentenca == 4
        assert comum.sujeito_canonico == t.sujeito_canonico
        assert comum.relacao == t.relacao

    def test_salva_historia_explode_por_artigo(self, tmp_path):
        conexao = conecta(tmp_path / "t.db")
        salva(conexao, artigo(url="https://a/1", veiculo="G1"))
        salva(conexao, artigo(url="https://b/2", veiculo="Folha"))
        linhas = conexao.execute(
            "SELECT * FROM artigos ORDER BY id").fetchall()
        blocos = [(linhas[0], ["s0", "s1"]), (linhas[1], ["s0"])]
        uso = Uso(modelo=llm.EXTRACAO, entrada=1000, saida=500,
                  cache_leitura=0, cache_escrita=0)
        extract.salva_historia(
            conexao, blocos, [th([("A", 1), ("B", 0)])], uso, "vh1")

        # Cada artigo ganhou SUA linha de extração e SUA tripla, com a
        # sentença da própria fonte — e as strings canônicas idênticas são
        # a corroboração pronta para o grafo.
        por_artigo = {
            linha["artigo_id"]: linha for linha in conexao.execute(
                "SELECT e.artigo_id, t.sentenca, t.sujeito_canonico s "
                "FROM triplas t JOIN extracoes e ON e.id = t.extracao_id")
        }
        assert por_artigo[linhas[0]["id"]]["sentenca"] == 1
        assert por_artigo[linhas[1]["id"]]["sentenca"] == 0
        assert (por_artigo[linhas[0]["id"]]["s"]
                == por_artigo[linhas[1]["id"]]["s"])

        # O rateio soma a fatura, sem perder o resto da divisão.
        total = conexao.execute(
            "SELECT SUM(tokens_entrada), SUM(tokens_saida) "
            "FROM extracoes").fetchone()
        assert (total[0], total[1]) == (1000, 500)
        conexao.close()


class TestSubstituicaoEmHistoriaCrescida:
    def test_re_salvar_mesma_versao_substitui_sem_erro(self, tmp_path):
        """História que ganha membro novo é re-extraída inteira — a linha
        anterior da MESMA versão sai antes, senão a UNIQUE derrubava a
        rodada (achado da revisão)."""
        conexao = conecta(tmp_path / "t.db")
        salva(conexao, artigo(url="https://a/1", veiculo="G1"))
        linha = conexao.execute("SELECT * FROM artigos").fetchone()
        uso = Uso(modelo=llm.EXTRACAO, entrada=10, saida=10,
                  cache_leitura=0, cache_escrita=0)
        blocos = [(linha, ["s0"])]
        extract.salva_historia(conexao, blocos, [th([("A", 0)])], uso, "vh")
        extract.salva_historia(conexao, blocos, [th([("A", 0)])], uso, "vh")
        n = conexao.execute(
            "SELECT COUNT(*) FROM extracoes WHERE prompt_versao='vh'"
        ).fetchone()[0]
        assert n == 1
        conexao.close()


class TestVazioNaoSuperaTripla:
    def test_grafo_prefere_extracao_com_tripla(self, tmp_path):
        """O marcador vazio do modo história (mesma_historia=false, fonte
        sem origem) tem id maior — e um MAX(id) cru o deixava APAGAR
        triplas boas do grafo. Vazio só vale quando é tudo que há."""
        from src import grafo
        conexao = conecta(tmp_path / "t.db")
        salva(conexao, artigo(url="https://a/1", veiculo="G1"))
        linha = conexao.execute("SELECT * FROM artigos").fetchone()
        uso = Uso(modelo=llm.EXTRACAO, entrada=10, saida=10,
                  cache_leitura=0, cache_escrita=0)
        boa = extract._tripla_da_fonte(th([("A", 0)]), 0)
        salva_extracao(conexao, linha["id"], [boa], llm.EXTRACAO.id,
                       "v-materia", extract.VOCAB_VERSAO, uso)
        # marcador vazio, mais novo, mesma geração
        salva_extracao(conexao, linha["id"], [], llm.EXTRACAO.id,
                       "v-historia", extract.VOCAB_VERSAO, uso)
        afirmacoes = grafo.carrega(conexao)
        assert len(afirmacoes) == 1
        assert afirmacoes[0].sujeito == "Braskem"
        conexao.close()


class TestVersionamento:
    def test_historia_e_materia_tem_versoes_distintas(self):
        """Triplas dos dois modos não são comparáveis sem marca — mesmo
        motivo do hash original."""
        assert extract.PROMPT_VERSAO_HISTORIA != extract.PROMPT_VERSAO

    def test_o_corte_entra_na_versao_de_historia(self):
        versoes = {extract.versao_prompt_historia(v) for v in (3, 5, None)}
        assert len(versoes) == 3
