"""Testes da seleção de matérias para extração.

O que importa aqui é dinheiro: cada matéria selecionada é uma chamada paga, e
a seleção errada gasta sem poder produzir confirmação nenhuma.
"""

from src import extract
from src.storage import conecta, salva, salva_extracao
from tests.test_storage import artigo


def _corpo(sentencas: int) -> str:
    """Texto com N sentencas de verdade.

    O piso passou de caracteres para SENTENCAS (ver `MIN_SENTENCAS`), entao
    encher de caractere nao basta: 2000 letras sem pontuacao sao uma sentenca
    so, e a materia seria descartada com razao.
    """
    return " ".join(
        f"O orgao divulgou o dado numero {i} nesta quarta-feira." 
        for i in range(sentencas)
    )


def _base(tmp_path, materias):
    """materias: lista de (veiculo, titulo, quantas_sentencas, ja_extraida)."""
    from src import llm
    from src.llm import Uso

    conexao = conecta(tmp_path / "t.db")
    uso = Uso(modelo=llm.EXTRACAO, entrada=1, saida=1,
              cache_leitura=0, cache_escrita=0)
    for i, (veiculo, titulo, sentencas, extraida) in enumerate(materias):
        salva(conexao, artigo(url=f"https://x/{i}", titulo=titulo,
                              veiculo=veiculo, conteudo=_corpo(sentencas)))
        if extraida:
            linha = conexao.execute(
                "SELECT id FROM artigos WHERE url_norm LIKE ?",
                (f"%/{i}",)).fetchone()
            # A versão corrente, não um número fixo: extração de vocabulário
            # antigo VOLTA para a fila por regra do seletor, e um fixture
            # preso à v1 testaria esse retorno em vez do "já extraída".
            salva_extracao(conexao, linha["id"], [], llm.EXTRACAO.id,
                           "v1", extract.VOCAB_VERSAO, uso)
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
            ("G1", TITULO_A, 3, True),
            ("Folha", TITULO_B, 3, False),
        ])
        escolhidas = extract._por_historia(conexao, 5)
        assert [m["veiculo"] for m in escolhidas] == ["Folha"]
        conexao.close()

    def test_nao_seleciona_quando_tudo_ja_foi_extraido(self, tmp_path):
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 3, True),
            ("Folha", TITULO_B, 3, True),
        ])
        assert extract._por_historia(conexao, 5) == []
        conexao.close()


class TestFonteUnica:
    def test_veiculo_sozinho_nao_entra(self, tmp_path):
        """Não pode ser corroborado por definição — a chamada seria gasto sem
        retorno possível. Continua no acervo; só não entra nesta fila."""
        conexao = _base(tmp_path, [("G1", TITULO_A, 3, False)])
        assert extract._por_historia(conexao, 5) == []
        conexao.close()

    def test_duas_materias_do_mesmo_veiculo_nao_formam_par(self, tmp_path):
        """Mesma redação publicando duas vezes não é confirmação independente."""
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 3, False),
            ("G1", TITULO_B, 3, False),
        ])
        assert extract._por_historia(conexao, 5) == []
        conexao.close()


class TestPeneiraSemantica:
    def test_titulos_de_assuntos_diferentes_nao_formam_par(self, tmp_path):
        """Termo em comum não é assunto em comum. Ver `_por_historia`."""
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 3, False),
            ("Folha", TITULO_OUTRO, 3, False),
        ])
        assert extract._por_historia(conexao, 5) == []
        conexao.close()


class TestTextoInsuficiente:
    def test_uma_sentenca_so_nao_entra(self, tmp_path):
        """Uma sentenca traz fato solto, sem data nem entidade em volta, e
        tripla sem contexto nao corrobora nem contradiz."""
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 3, False),
            ("Folha", TITULO_B, 1, False),
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
        entre si sem serem comparáveis de fato.

        O corte é PARÂMETRO de `versao_prompt` porque o CLI muda o corte por
        rodada (`--sentencas`): hashear só a constante gravava corte diferente
        sob a mesma versão — o main recalcula com o valor efetivo."""
        versoes = {extract.versao_prompt(valor) for valor in (3, 5, None)}
        assert len(versoes) == 3

    def test_sem_argumento_a_versao_e_a_do_padrao(self):
        assert extract.versao_prompt() == extract.versao_prompt(
            extract.MAX_SENTENCAS)
        assert extract.PROMPT_VERSAO == extract.versao_prompt()


class TestJanelaTemporal:
    """Número só disputa com número quando afirma o mesmo instante.

    A Medição 1 derrubou a janela em dias: cotação do Bitcoin em dias
    diferentes era a única 'divergência' do acervo, e era falsa — o preço
    subiu 20% na semana e cada veículo estava certo no seu dia."""

    @staticmethod
    def _cotacao(valor, veiculo, data_fato):
        from src.grafo import Afirmacao
        return Afirmacao(
            sujeito="Bitcoin", relacao="tem_atributo", objeto=None,
            valor=valor, unidade="USD", contexto="cotação",
            data_fato=data_fato, origem="EXTRACTED", veiculo=veiculo,
            titulo="t", url=f"https://x/{veiculo}/{data_fato}",
        )

    def test_datas_diferentes_nao_divergem(self):
        from src import grafo
        grupos = grafo.agrupa([
            self._cotacao(65500, "CriptoFácil", "2026-08-25"),
            self._cotacao(79589, "Portal do Bitcoin", "2026-08-27"),
        ])
        assert all(not g.diverge for g in grupos)

    def test_mesma_data_diverge(self):
        from src import grafo
        grupos = grafo.agrupa([
            self._cotacao(65500, "CriptoFácil", "2026-08-25"),
            self._cotacao(80000, "Folha", "2026-08-25"),
        ])
        assert any(g.diverge for g in grupos)

    def test_sem_data_compara_entre_si(self):
        # Estado presente sem data explícita é contemporâneo por construção.
        from src import grafo
        grupos = grafo.agrupa([
            self._cotacao(100, "A", None),
            self._cotacao(200, "B", None),
        ])
        assert any(g.diverge for g in grupos)

    def test_granularidade_diferente_nao_compara(self):
        # "2026-08" vs "2026-08-25": pode perder divergência real, nunca
        # inventa uma.
        from src import grafo
        grupos = grafo.agrupa([
            self._cotacao(100, "A", "2026-08"),
            self._cotacao(200, "B", "2026-08-25"),
        ])
        assert all(not g.diverge for g in grupos)

    def test_arredondamento_na_mesma_data_continua_nao_divergindo(self):
        from src import grafo
        grupos = grafo.agrupa([
            self._cotacao(80000, "A", "2026-08-25"),
            self._cotacao(79900, "B", "2026-08-25"),
        ])
        assert all(not g.diverge for g in grupos)


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
