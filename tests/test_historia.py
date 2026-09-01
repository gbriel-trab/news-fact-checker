"""As peças puras do modo história (v3): validação de origens, explosão
por fonte e versionamento.

A trava central está em `valida_origens`: `origens` é o modelo AFIRMANDO
que cada fonte disse — origem inválida que passasse viraria corroboração
fabricada, o pior erro do projeto.
"""

from src import extract, llm
from src.extract import (Extracao, Tripla, TriplaHistoria,
                         _tripla_da_fonte, valida_origens)
from src.llm import Uso
from src.storage import conecta, salva, salva_extracao
from tests.test_storage import artigo


def th(origens, sujeito="Braskem", relacao="solicitou",
       objeto="Recuperação extrajudicial da Braskem"):
    return TriplaHistoria(
        sujeito=sujeito, sujeito_canonico=sujeito, relacao=relacao,
        objeto=objeto, objeto_canonico=objeto, tipo_relacao="evento",
        origem="e", data_fato="2026-08-26",
        origens=[f"{f}{s}" for f, s in origens],
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
        assert boas[0].origens == ["A0"]

    def test_sentenca_fora_do_texto_cai(self):
        boas, _ = valida_origens([th([("A", 0), ("B", 99)])],
                                 {"A": 3, "B": 3})
        assert boas[0].origens == ["A0"]

    def test_tripla_sem_origem_valida_morre(self):
        boas, fora = valida_origens([th([("C", 0), ("A", 99)])],
                                    {"A": 3, "B": 3})
        assert boas == [] and fora == 1

    def test_codigo_malformado_cai_pela_mesma_porta(self):
        t = TriplaHistoria(
            sujeito_canonico="X", relacao="afirmou", objeto="algo",
            tipo_relacao="evento", origem="e",
            origens=["3A", "A", "B1"])
        boas, _ = valida_origens([t], {"A": 3, "B": 3})
        assert boas[0].origens == ["B1"]


class TestFioMagro:
    """O contrato do schema magro (01/09/2026): aliases curtos no fio,
    opcionais omitidos, e os validadores preenchendo as formas irmãs."""

    def test_valida_pelo_fio_com_omissoes(self):
        t = Tripla.model_validate({
            "sc": "Braskem", "r": "solicitou",
            "ob": "Recuperação extrajudicial", "t": "evento",
            "og": "e", "n": 2})
        assert t.sujeito == "Braskem"            # preenchido de sc
        assert t.objeto_canonico == "Recuperação extrajudicial"
        assert t.valor_numero is None and t.data_fato is None
        assert t.origem == "e"

    def test_nomes_python_continuam_validos(self):
        # populate_by_name: o código interno constrói pelos nomes longos.
        t = Tripla(sujeito_canonico="X", relacao="afirmou", objeto="algo",
                   tipo_relacao="evento", origem="i", sentenca=0)
        assert t.objeto_canonico == "algo"

    def test_schema_do_fio_usa_aliases_e_solta_opcionais(self):
        schema = Extracao.model_json_schema()
        tripla = schema["$defs"]["Tripla"]
        assert "sc" in tripla["properties"]
        assert "sujeito_canonico" not in tripla["properties"]
        # Opcional fora de required = o modelo pode OMITIR (sem null).
        assert "v" not in tripla["required"]
        assert "d" not in tripla["required"]
        assert "og" in tripla["required"]

    def test_historia_origens_sao_codigos(self):
        schema = extract.ExtracaoHistoria.model_json_schema()
        th_ = schema["$defs"]["TriplaHistoria"]
        assert th_["properties"]["fs"]["items"]["type"] == "string"


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


class TestCompatibilidadeDeVocabulario:
    """A v3 é aditiva: dado v2 continua no grafo, e a primeira extração v3
    não pode escurecer o acervo anterior (o recorte era MAX(vocab_versao))."""

    def _sobe(self, conexao, artigo_id, vocab, sujeito, prompt):
        uso = Uso(modelo=llm.EXTRACAO, entrada=10, saida=10,
                  cache_leitura=0, cache_escrita=0)
        t = extract._tripla_da_fonte(th([("A", 0)], sujeito=sujeito), 0)
        salva_extracao(conexao, artigo_id, [t], llm.EXTRACAO.id,
                       prompt, vocab, uso)

    def test_v2_e_v3_convivem_no_grafo(self, tmp_path):
        from src import grafo
        conexao = conecta(tmp_path / "t.db")
        salva(conexao, artigo(url="https://a/1", veiculo="G1"))
        salva(conexao, artigo(url="https://b/2", veiculo="Folha"))
        a1, a2 = [l["id"] for l in conexao.execute(
            "SELECT id FROM artigos ORDER BY id")]
        self._sobe(conexao, a1, 2, "Braskem", "p2")
        self._sobe(conexao, a2, 3, "Petrobras", "p3")
        sujeitos = {a.sujeito for a in grafo.carrega(conexao)}
        assert sujeitos == {"Braskem", "Petrobras"}
        conexao.close()

    def test_no_mesmo_artigo_o_vocab_mais_novo_vence(self, tmp_path):
        from src import grafo
        conexao = conecta(tmp_path / "t.db")
        salva(conexao, artigo(url="https://a/1", veiculo="G1"))
        a1 = conexao.execute("SELECT id FROM artigos").fetchone()["id"]
        self._sobe(conexao, a1, 2, "LeituraAntiga", "p2")
        self._sobe(conexao, a1, 3, "LeituraNova", "p3")
        afirmacoes = grafo.carrega(conexao)
        assert [a.sujeito for a in afirmacoes] == ["LeituraNova"]
        conexao.close()

    def test_vocab_incompativel_fica_fora(self, tmp_path):
        from src import grafo
        conexao = conecta(tmp_path / "t.db")
        salva(conexao, artigo(url="https://a/1", veiculo="G1"))
        a1 = conexao.execute("SELECT id FROM artigos").fetchone()["id"]
        self._sobe(conexao, a1, 1, "VocabUm", "p1")
        assert grafo.carrega(conexao) == []
        conexao.close()

    def test_v3_e_aditiva_sobre_a_v2(self):
        # Nenhuma relação da v2 pode ter sumido ou mudado de valor — é a
        # premissa que torna COMPATIVEIS honesto.
        from src import vocabulario
        v2 = {"afirmou", "criticou", "defendeu", "integra",
              "exerce_cargo_em", "preside", "candidatou_se_a",
              "obteve_percentual_em", "submeteu_a_votacao", "submeteu_a",
              "preve", "abriu_processo_contra", "solicitou", "impos",
              "recomendou", "tem_participacao_em", "negociada_em",
              "lancou", "participou_de", "divulgou", "tem_atributo",
              "outro"}
        atuais = {r.value for r in vocabulario.Relacao}
        assert v2 <= atuais
        assert vocabulario.VERSAO == 3
        assert vocabulario.COMPATIVEIS == frozenset({2, 3})
        # E as caras novas da v3 estão lá.
        assert {"concedeu", "rejeitou", "suspendeu", "causou",
                "ocorreu_em", "tem_parentesco_com"} <= atuais


class TestVersionamento:
    def test_historia_e_materia_tem_versoes_distintas(self):
        """Triplas dos dois modos não são comparáveis sem marca — mesmo
        motivo do hash original."""
        assert extract.PROMPT_VERSAO_HISTORIA != extract.PROMPT_VERSAO

    def test_o_corte_entra_na_versao_de_historia(self):
        versoes = {extract.versao_prompt_historia(v) for v in (3, 5, None)}
        assert len(versoes) == 3
