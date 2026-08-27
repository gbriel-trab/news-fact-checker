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


class TestCortaLide:
    """O corte é o maior lever de custo do sistema. Ver `MAX_SENTENCAS`."""

    def test_mantem_as_primeiras(self):
        assert extract.corta_lide(list("abcdefg"), 3) == ["a", "b", "c"]

    def test_materia_curta_passa_inteira(self):
        assert extract.corta_lide(["a", "b"], 5) == ["a", "b"]

    def test_none_desliga_o_corte(self):
        assert extract.corta_lide(list("abcdefg"), None) == list("abcdefg")

    def test_corta_do_fim_para_o_indice_continuar_valendo(self):
        """O índice da sentença é gravado junto da tripla e é como a evidência
        volta ao texto de origem. Cortar do meio renumeraria o que vem depois e
        faria cada tripla apontar para a frase errada — sem erro nenhum, só
        citação trocada."""
        sentencas = ["lide", "segunda", "terceira", "detalhe", "rodapé"]
        cortadas = extract.corta_lide(sentencas, 3)
        for i, frase in enumerate(cortadas):
            assert sentencas[i] == frase

    def test_o_corte_entra_na_versao_do_prompt(self):
        """Fora do hash, extrações com cortes diferentes ficariam comparáveis
        entre si sem serem comparáveis de fato."""
        original = extract.MAX_SENTENCAS
        try:
            versoes = set()
            for valor in (3, 5, None):
                extract.MAX_SENTENCAS = valor
                versoes.add(extract.versao_prompt())
            assert len(versoes) == 3
        finally:
            extract.MAX_SENTENCAS = original


class TestAgrupamentoPorMedida:
    """A correção do defeito que impedia qualquer número de corroborar.

    Antes, `contexto` entrava na chave por igualdade exata. Dois veículos
    escreveram "lucro recorrente NO 2º trimestre" e "...DO 2º trimestre" e o
    fato deixou de ser o mesmo. Medido no acervo: 126 fatos com número, ZERO
    confirmados.
    """

    def af(self, veiculo, valor, contexto, relacao="tem_atributo",
           unidade="BRL", objeto=None):
        from src.grafo import Afirmacao

        return Afirmacao(
            sujeito="Caixa", relacao=relacao, objeto=objeto, valor=valor,
            unidade=unidade, contexto=contexto, data_fato="2026-08-26",
            origem="EXTRACTED", veiculo=veiculo, titulo="t",
            url=f"https://x/{veiculo}",
        )

    def test_mesma_medida_escrita_diferente_confirma(self):
        from src import grafo

        grupos = grafo.agrupa([
            self.af("Poder360", 3.9e9, "lucro líquido recorrente do 2º trimestre de 2026"),
            self.af("Agência Brasil", 3.9e9, "lucro líquido recorrente no 2º trimestre de 2026"),
        ])
        assert len(grupos) == 1 and grupos[0].confirmada

    def test_periodos_diferentes_nao_se_fundem(self):
        """A trava de dígitos. A semântica sozinha dá 0,93 para estes dois, e
        fundi-los inventaria uma divergência entre dois fatos corretos."""
        from src import grafo

        grupos = grafo.agrupa([
            self.af("Poder360", 7.4e9, "lucro do 1º semestre de 2026"),
            self.af("Agência Brasil", 3.9e9, "lucro do 2º trimestre de 2026"),
        ])
        assert len(grupos) == 2
        assert not any(g.confirmada for g in grupos)

    def test_medidas_diferentes_sem_numero_no_texto_nao_se_fundem(self):
        """A trava semântica. Aqui não há dígito que separe — "capital votante"
        e "capital total" são duas medidas e 47% contra 36,1% não é divergência.
        """
        from src import grafo

        grupos = grafo.agrupa([
            self.af("G1", 47.0, "participação no capital votante", unidade="%"),
            self.af("Folha", 36.1, "participação no capital total", unidade="%"),
        ])
        assert len(grupos) == 2

    def test_mesma_medida_numeros_diferentes_e_divergencia(self):
        from src import grafo

        grupos = grafo.agrupa([
            self.af("G1", 56e9, "dívidas a reestruturar"),
            self.af("Folha", 59e9, "dívidas a reestruturar"),
        ])
        assert len(grupos) == 1 and grupos[0].diverge

    def test_valor_sem_objeto_vira_tem_atributo_na_leitura(self):
        """`outro` com número e sem objeto é o modelo deixando de aplicar a
        regra 6, não uma distinção real."""
        from src.grafo import _relacao_normalizada

        assert _relacao_normalizada("outro", None, 3.9e9) == "tem_atributo"
        assert _relacao_normalizada("outro", "Braskem", None) == "outro"
        assert _relacao_normalizada("afirmou", "algo", None) == "afirmou"
