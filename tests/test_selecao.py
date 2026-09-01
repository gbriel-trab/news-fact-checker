"""Testes da seleção de matérias para extração.

O que importa aqui é dinheiro: cada matéria selecionada é uma chamada paga, e
a seleção errada gasta sem poder produzir confirmação nenhuma.
"""

from src import extract
from src.storage import conecta, salva, salva_extracao
from tests.test_storage import artigo


def _corpo(sentencas: int, titulo: str = "O órgão divulgou o dado") -> str:
    """Texto com N sentencas cujo assunto ecoa o título.

    O agrupamento é semântico sobre título+lead — lead genérico idêntico
    entre matérias de assuntos diferentes as aproximaria artificialmente.
    """
    return " ".join(
        f"{titulo}, segundo o levantamento de número {i} desta quarta."
        for i in range(sentencas)
    )


def _base(tmp_path, materias):
    """materias: lista de (veiculo, titulo, quantas_sentencas, ja_extraida).

    `ja_extraida` marca sob a versão do MODO HISTÓRIA — é ela que o seletor
    de histórias usa para não repetir."""
    from src import llm
    from src.llm import Uso

    conexao = conecta(tmp_path / "t.db")
    uso = Uso(modelo=llm.EXTRACAO, entrada=1, saida=1,
              cache_leitura=0, cache_escrita=0)
    for i, (veiculo, titulo, sentencas, extraida) in enumerate(materias):
        salva(conexao, artigo(url=f"https://x/{i}", titulo=titulo,
                              veiculo=veiculo,
                              conteudo=_corpo(sentencas, titulo)))
        if extraida:
            linha = conexao.execute(
                "SELECT id FROM artigos WHERE url_norm LIKE ?",
                (f"%/{i}",)).fetchone()
            salva_extracao(conexao, linha["id"], [], llm.EXTRACAO.id,
                           extract.PROMPT_VERSAO_HISTORIA,
                           extract.VOCAB_VERSAO, uso)
    return conexao


TITULO_A = "Caixa tem lucro de R$ 3,9 bilhões no segundo trimestre"
TITULO_B = "Lucro da Caixa cresce 5,9% e chega a R$ 3,9 bilhões"
TITULO_OUTRO = "Eclipse lunar quase total será visível no Brasil hoje"


class TestSelecaoDeHistorias:
    def test_historia_com_metade_extraida_entra_inteira(self, tmp_path):
        """No modo história a releitura é o ponto: o membro já extraído em
        modo matéria volta JUNTO, para os nomes convergirem na mesma
        chamada — e a linha nova supera a antiga no grafo."""
        from src import llm
        from src.llm import Uso
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 3, False),
            ("Folha", TITULO_B, 3, False),
        ])
        # G1 extraída em modo MATÉRIA (versão diferente da de história)
        linha = conexao.execute(
            "SELECT id FROM artigos WHERE url_norm LIKE '%/0'").fetchone()
        salva_extracao(conexao, linha["id"], [], llm.EXTRACAO.id,
                       extract.PROMPT_VERSAO, extract.VOCAB_VERSAO,
                       Uso(modelo=llm.EXTRACAO, entrada=1, saida=1,
                           cache_leitura=0, cache_escrita=0))
        grupos = extract._historias_para_extrair(conexao, 5)
        assert len(grupos) == 1
        assert {m["veiculo"] for m in grupos[0]} == {"G1", "Folha"}
        conexao.close()

    def test_historia_toda_sob_a_versao_de_historia_nao_volta(self, tmp_path):
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 3, True),
            ("Folha", TITULO_B, 3, True),
        ])
        assert extract._historias_para_extrair(conexao, 5) == []
        conexao.close()


class TestFonteUnica:
    def test_veiculo_sozinho_nao_entra(self, tmp_path):
        """Não pode ser corroborado por definição — a chamada seria gasto sem
        retorno possível. Continua no acervo; só não entra nesta fila."""
        conexao = _base(tmp_path, [("G1", TITULO_A, 3, False)])
        assert extract._historias_para_extrair(conexao, 5) == []
        conexao.close()

    def test_duas_materias_do_mesmo_veiculo_nao_formam_par(self, tmp_path):
        """Mesma redação publicando duas vezes não é confirmação independente."""
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 3, False),
            ("G1", TITULO_B, 3, False),
        ])
        assert extract._historias_para_extrair(conexao, 5) == []
        conexao.close()


class TestAgrupamento:
    def test_assuntos_diferentes_nao_formam_historia(self, tmp_path):
        """Caixa e eclipse lunar não compartilham termos — o léxico separa
        na entrada."""
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 3, False),
            ("Folha", TITULO_OUTRO, 3, False),
        ])
        assert extract._historias_para_extrair(conexao, 5) == []
        conexao.close()

    def test_mesmo_fato_agrupado_passa_pela_coesao(self, tmp_path):
        """Dois títulos do mesmo fato: o léxico junta (compartilham
        'Caixa/lucro/bilhões') e a guarda de coesão — calibrada no ouro
        refinado, p10 0,62 — não pode expulsar par verdadeiro."""
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 3, False),
            ("Folha", TITULO_B, 3, False),
        ])
        grupos = extract._historias_para_extrair(conexao, 5)
        assert len(grupos) == 1
        conexao.close()


class TestMedoide:
    def test_carona_mais_recente_nao_expulsa_o_par_verdadeiro(
            self, tmp_path, monkeypatch):
        """O achado grave da revisão: a referência da coesão era o membro
        mais recente (sim 1,0 consigo, inexpulsável) — quando ELE era o
        carona, expulsava o par verdadeiro e a história boa morria. A
        referência agora é o MEDOIDE, que o carona nunca é."""
        import numpy as np
        from src import agrupa as mod_agrupa
        from src import indice

        conexao = _base(tmp_path, [
            # mesmo conjunto léxico ("pesquisa/quaest/governo"), carona 1º
            ("CNN", "Pesquisa Quaest para governo mostra líder em SC",
             3, False),
            ("G1", "Pesquisa Quaest para governo tem empate no RS",
             3, False),
            ("Folha", "Pesquisa Quaest para governo: empate no RS",
             3, False),
        ])
        materias = mod_agrupa.carrega(conexao)
        # vetores controlados POR VEÍCULO (a ordem do carrega não é
        # garantida com datas iguais): CNN é o carona ortogonal.
        fake = {"CNN": [1.0, 0.0], "G1": [0.0, 1.0], "Folha": [0.0, 1.0]}

        def vetoriza_fake(textos):
            # ordem dos textos segue a ordem das matérias do grupo
            por_texto = {mod_agrupa.texto_de_agrupamento(m): fake[m["veiculo"]]
                         for m in materias}
            return [por_texto[t] for t in textos]

        monkeypatch.setattr(indice, "vetoriza", vetoriza_fake)
        historias = mod_agrupa.agrupa(materias)
        assert len(historias) == 1
        veiculos = {m["veiculo"] for m in historias[0].materias}
        assert veiculos == {"G1", "Folha"}  # carona CNN expulso
        conexao.close()


class TestAncoraPosCorte:
    def test_ancora_sobrevive_ao_corte_de_max_fontes(self, tmp_path):
        """Com 8 veículos e a única matéria de 2+ sentenças sendo a MENOR
        em caracteres, o corte por tamanho a derrubava e a âncora era
        checada no grupo inteiro — time só de manchetes ia ao modelo."""
        frase_longa = ("Este é um levantamento extenso sobre a pesquisa "
                       "Quaest para governo com muitos detalhes " + "x" * 400)
        materias = [(f"V{i}", "Pesquisa Quaest para governo no RS",
                     0, False) for i in range(7)]
        conexao = _base(tmp_path, materias)
        # os 7 grandes: 1 sentença longa cada (corpo custom)
        for i in range(7):
            conexao.execute(
                "UPDATE artigos SET conteudo = ? WHERE veiculo = ?",
                (frase_longa + ".", f"V{i}"))
        # a âncora: 2 sentenças curtas
        from src.storage import salva
        from tests.test_storage import artigo
        salva(conexao, artigo(
            url="https://ancora/1", veiculo="Ancora",
            titulo="Pesquisa Quaest para governo no RS",
            conteudo="A Quaest mediu o governo no RS. O empate segue firme."))
        conexao.commit()
        grupos = extract._historias_para_extrair(conexao, 5)
        assert len(grupos) == 1
        assert len(grupos[0]) == extract.MAX_FONTES
        assert any(m["veiculo"] == "Ancora" for m in grupos[0])
        conexao.close()


class TestModoMateriaNaoSuperaHistoria:
    def test_materias_exclui_lidas_pelo_modo_historia(self, tmp_path):
        """-n depois de --historias re-pagava os artigos e a extração
        isolada (id maior) SUPERAVA a convergida no grafo."""
        from src import llm
        from src.llm import Uso
        conexao = _base(tmp_path, [("G1", TITULO_A, 3, False)])
        linha = conexao.execute("SELECT id FROM artigos").fetchone()
        salva_extracao(conexao, linha["id"], [], llm.EXTRACAO.id,
                       extract.PROMPT_VERSAO_HISTORIA,
                       extract.VOCAB_VERSAO,
                       Uso(modelo=llm.EXTRACAO, entrada=1, saida=1,
                           cache_leitura=0, cache_escrita=0))
        assert extract._materias(conexao, 10) == []
        conexao.close()


class TestTextoInsuficiente:
    def test_uma_sentenca_entra_se_a_historia_tem_ancora(self, tmp_path):
        """A mudança medida do modo história (7 fontes, 01/09/2026): a
        matéria de 1 sentença contribui lida no contexto das outras — o
        piso vale para a HISTÓRIA, não para o membro."""
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 3, False),
            ("Folha", TITULO_B, 1, False),
        ])
        grupos = extract._historias_para_extrair(conexao, 5)
        assert len(grupos) == 1
        assert {m["veiculo"] for m in grupos[0]} == {"G1", "Folha"}
        conexao.close()

    def test_historia_so_de_manchetes_nao_entra(self, tmp_path):
        """Sem nenhuma matéria de 2+ sentenças não há âncora — só manchete
        não sustenta extração nem em conjunto."""
        conexao = _base(tmp_path, [
            ("G1", TITULO_A, 1, False),
            ("Folha", TITULO_B, 1, False),
        ])
        assert extract._historias_para_extrair(conexao, 5) == []
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


class TestJanelaDoAgrupamento:
    """Evento recorrente não pode formar par entre edições de meses
    diferentes — 'Copom mantém Selic' de julho e de setembro são fatos
    distintos com o mesmo título."""

    def test_materia_velha_fica_fora_do_agrupamento(self, tmp_path):
        from datetime import datetime, timedelta, timezone
        from src import agrupa
        from src.storage import conecta, salva
        from tests.test_storage import artigo

        con = conecta(tmp_path / "t.db")
        velha = (datetime.now(timezone.utc)
                 - timedelta(days=agrupa.JANELA_DIAS + 5)).isoformat()
        salva(con, artigo(url="https://a/1", titulo="Copom mantém Selic",
                          veiculo="A"))
        salva(con, artigo(url="https://b/2", titulo="Copom mantém Selic",
                          veiculo="B", data_publicacao=velha))
        ids = {m["id"] for m in agrupa.carrega(con)}
        assert len(ids) == 1  # só a recente

    def test_sem_janela_carrega_tudo(self, tmp_path):
        from datetime import datetime, timedelta, timezone
        from src import agrupa
        from src.storage import conecta, salva
        from tests.test_storage import artigo

        con = conecta(tmp_path / "t.db")
        velha = (datetime.now(timezone.utc)
                 - timedelta(days=99)).isoformat()
        salva(con, artigo(url="https://a/1", veiculo="A"))
        salva(con, artigo(url="https://b/2", veiculo="B",
                          data_publicacao=velha))
        assert len(agrupa.carrega(con, janela_dias=None)) == 2


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

    def test_conflito_dentro_do_mesmo_veiculo_nao_e_divergencia(self):
        # Medição 1 de 01/09/2026: metade das acusações era mono-veículo
        # (série de ETF sem data, quina/quadra) — inconsistência interna
        # de matéria, não dois veículos disputando. Divergência é ENTRE
        # veículos, e falso positivo é o pior erro do projeto.
        from src import grafo
        grupos = grafo.agrupa([
            self._cotacao(139_000_000, "CriptoFácil", None),
            self._cotacao(33_000_000, "CriptoFácil", None),
        ])
        assert all(not g.diverge for g in grupos)

    def test_terceiro_veiculo_reativa_a_disputa(self):
        # Dois do mesmo veículo + um de outro: há disputa entre veículos.
        from src import grafo
        grupos = grafo.agrupa([
            self._cotacao(100, "A", "2026-08-25"),
            self._cotacao(105, "A", "2026-08-25"),
            self._cotacao(200, "B", "2026-08-25"),
        ])
        assert any(g.diverge for g in grupos)


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
