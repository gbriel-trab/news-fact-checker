"""A escada de freios da extração sob demanda, sem rede nem índice real.

A ordem dos freios é o contrato: quem pergunta "o acervo cobre?" é o
CHECK do chamador (a demanda pressupõe um "sem evidência" — o oráculo por
proximidade caiu no primeiro teste vivo); busca vazia não estima;
orçamento curto recusa ANTES de chamar a API; recusa de grupo re-tenta
UMA vez, só com a melhor candidata e só se o orçamento comportar; e só
extração com tripla nova reindexa — incremental.
"""

import pytest

from src import demanda


class TestGarante:
    def test_sem_candidata_nao_gasta(self, monkeypatch):
        monkeypatch.setattr(demanda, "candidatas", lambda c, t, r="": [])
        r = demanda.garante(None, "x")
        assert r.motivo == "sem_candidata" and r.custo == 0

    def test_teto_recusa_antes_da_api(self, monkeypatch):
        monkeypatch.setattr(demanda, "candidatas", lambda c, t, r="": ["m"])
        monkeypatch.setattr(demanda.extract, "extrai_grupo",
                            lambda *a: pytest.fail("o teto não segurou"))
        r = demanda.garante(None, "x",
                            orcamento=demanda.CUSTO_ESTIMADO - 0.01)
        assert r.motivo == "teto" and r.custo == 0

    def test_extraiu_reindexa_so_o_grupo_e_fatura_o_real(self, monkeypatch):
        m1, m2 = {"id": 11}, {"id": 22}
        monkeypatch.setattr(demanda, "candidatas", lambda c, t, r="": [m1, m2])
        monkeypatch.setattr(demanda.extract, "extrai_grupo",
                            lambda c, g: (7, 0.08, False))
        reindexados = []
        monkeypatch.setattr(
            demanda.indice, "indexa_afirmacoes",
            lambda c, so_artigos=None: reindexados.append(so_artigos))
        r = demanda.garante(None, "x")
        assert r.motivo == "extraiu"
        assert r.materias == 2 and r.triplas == 7 and r.custo == 0.08
        # Incremental: só as matérias do grupo, nunca o recorte inteiro.
        assert reindexados == [[11, 22]]

    def test_recusa_de_grupo_retenta_a_melhor_sozinha(self, monkeypatch):
        # mesma_historia=false num grupo montado por proximidade com a
        # premissa não pode queimar a matéria certa junto com o carona.
        m1, m2 = {"id": 11}, {"id": 22}
        monkeypatch.setattr(demanda, "candidatas", lambda c, t, r="": [m1, m2])
        chamadas = []

        def falso_extrai(c, grupo, *a):
            chamadas.append([l["id"] for l in grupo])
            if len(grupo) > 1:
                return (0, 0.05, True)   # recusou o grupo
            return (3, 0.04, False)      # a melhor sozinha rende

        monkeypatch.setattr(demanda.extract, "extrai_grupo", falso_extrai)
        monkeypatch.setattr(demanda.indice, "indexa_afirmacoes",
                            lambda c, so_artigos=None: None)
        r = demanda.garante(None, "x")
        assert chamadas == [[11, 22], [11]]
        assert r.triplas == 3
        assert r.custo == pytest.approx(0.09)

    def test_recusa_sem_orcamento_nao_retenta(self, monkeypatch):
        monkeypatch.setattr(demanda, "candidatas",
                            lambda c, t, r="": [{"id": 1}, {"id": 2}])
        chamadas = []
        monkeypatch.setattr(
            demanda.extract, "extrai_grupo",
            lambda c, g, *a: chamadas.append(1) or (0, 0.10, True))
        monkeypatch.setattr(demanda.indice, "indexa_afirmacoes",
                            lambda c, so_artigos=None:
                            pytest.fail("reindexou sem tripla"))
        r = demanda.garante(None, "x", orcamento=demanda.CUSTO_ESTIMADO)
        assert len(chamadas) == 1
        # "sem_tripla", não "extraiu": pagou e o acervo não mudou, então o
        # chamador não pode recarregar nem pagar o segundo check — ele não
        # teria como mudar de veredito (revisão de 03/09/2026).
        assert r.motivo == "sem_tripla" and r.triplas == 0
        assert r.custo == 0.10

    def test_falha_de_indice_nao_vira_falha_de_demanda(self, monkeypatch):
        # Extração PAGA precisa contar como extração mesmo se o Chroma
        # cair — a rota por chave do check segue enxergando o grafo.
        monkeypatch.setattr(demanda, "candidatas", lambda c, t, r="": [{"id": 1}])
        monkeypatch.setattr(demanda.extract, "extrai_grupo",
                            lambda c, g, *a: (5, 0.06, False))

        def explode(c, so_artigos=None):
            raise RuntimeError("chroma fora")

        monkeypatch.setattr(demanda.indice, "indexa_afirmacoes", explode)
        r = demanda.garante(None, "x")
        assert r.motivo == "extraiu" and r.triplas == 5


class _ConexaoFalsa:
    """Devolve marco 0 para MAX(id) e 'sem_evidencia' para a consulta."""

    def execute(self, sql, *a):
        class R:
            def __init__(self, valor):
                self._v = valor

            def fetchone(self):
                return self._v

        if "MAX(id)" in sql:
            return R([0])
        return R({"veredito": "sem_evidencia"})


class TestConferePostEstado:
    def test_orcamento_sobrevive_a_excecao_do_recheck(self, monkeypatch):
        # O bug que a revisão de 01/09/2026 confirmou: com o orçamento
        # devolvido por retorno, uma exceção depois do gasto restaurava o
        # dinheiro. Cenário: 1º check sem evidência → demanda extrai e
        # PAGA → re-check com forcar explode. O débito tem que ficar.
        from types import SimpleNamespace

        from src import boletim

        premissa = SimpleNamespace(tipo="fato", afirmacao="X fez Y",
                                   trecho="X fez Y", texto="X fez Y")
        analise = SimpleNamespace(premissas=[premissa])
        uso = SimpleNamespace(custo=0.0)
        monkeypatch.setattr("src.premissas.separa",
                            lambda texto, conexao=None: (analise, uso))
        monkeypatch.setattr(
            demanda, "garante",
            lambda c, t, o, referente="": demanda.Resultado(
                "extraiu", 1, 3, 0.20))
        monkeypatch.setattr("src.grafo.carrega", lambda c: ["novo"])

        def check_fake(*a, forcar=False, **k):
            if forcar:
                raise RuntimeError("API fora no re-check")

        monkeypatch.setattr("src.check.verifica", check_fake)

        from src.radar import Captura
        from src.x_api import Post
        captura = Captura(Post(id="1", autor="x", criado_em="",
                               texto="post", tipo="post"))
        estado = {"acervo": ["velho"], "orcamento": demanda.TETO_USD}
        with pytest.raises(RuntimeError):
            boletim._confere_post(captura, _ConexaoFalsa(), estado)
        assert estado["orcamento"] == pytest.approx(demanda.TETO_USD - 0.20)
        assert estado["acervo"] == ["novo"]


class TestJaExtraida:
    """Recusa de grupo (mesma_historia=false) deixa linha vazia para o grupo
    não voltar, mas a matéria não pode ficar invisível para a demanda:
    03/09/2026, a G1 'Joesley Batista se reuniu com Trump' entrou por
    engano no grupo da premissa 'o empresário' e sumiu do acervo."""

    def _extracao(self, con, artigo_id, versao, recusada=None, recusas=None):
        con.execute(
            "INSERT INTO extracoes (artigo_id, modelo, prompt_versao, "
            "vocab_versao, tokens_entrada, tokens_saida, custo_usd, "
            "extraido_em, recusada, recusas) "
            "VALUES (?, 'm', ?, 1, 0, 0, 0.0, 't', ?, ?)",
            (artigo_id, versao, recusada, recusas))
        con.commit()

    def test_recusada_nao_conta_como_extraida(self, tmp_path):
        from src.storage import conecta
        con = conecta(tmp_path / "t.db")
        self._extracao(con, 1, demanda.extract.PROMPT_VERSAO_HISTORIA,
                       recusada=1)
        assert not demanda.ja_extraida(con, 1)

    def test_extracao_normal_conta(self, tmp_path):
        from src.storage import conecta
        con = conecta(tmp_path / "t.db")
        self._extracao(con, 2, demanda.extract.PROMPT_VERSAO_HISTORIA)
        self._extracao(con, 3, demanda.extract.PROMPT_VERSAO, recusada=0)
        assert demanda.ja_extraida(con, 2)
        assert demanda.ja_extraida(con, 3)

    def test_versao_antiga_nao_conta(self, tmp_path):
        from src.storage import conecta
        con = conecta(tmp_path / "t.db")
        self._extracao(con, 4, "versao-antiga")
        assert not demanda.ja_extraida(con, 4)

    def test_positivo_recusas_abaixo_do_teto_seguem_recompraveis(self, tmp_path):
        """O lado que NÃO pode quebrar. Duas recusas ainda é acidente de
        agrupamento — a matéria continua elegível, que é a correção de
        03/09 que este teto não pode desfazer."""
        from src.storage import conecta
        con = conecta(tmp_path / "t.db")
        for n in (1, demanda.TETO_RECUSAS - 1):
            con.execute("DELETE FROM extracoes")
            self._extracao(con, 5, demanda.extract.PROMPT_VERSAO_HISTORIA,
                           recusada=1, recusas=n)
            assert not demanda.ja_extraida(con, 5), n

    def test_negativo_no_teto_a_materia_para_de_ser_recomprada(self, tmp_path):
        """O moto-perpétuo: TETO_USD é da RODADA e renasce a cada boletim,
        então sem contador a mesma matéria era recomprada e recusada uma
        vez por rodada, para sempre."""
        from src.storage import conecta
        con = conecta(tmp_path / "t.db")
        self._extracao(con, 6, demanda.extract.PROMPT_VERSAO_HISTORIA,
                       recusada=1, recusas=demanda.TETO_RECUSAS)
        assert demanda.ja_extraida(con, 6)

    def test_recusa_do_lote_nao_gasta_vida_do_teto(self, tmp_path):
        """O contador limita a RECOMPRA pela demanda. Recusa do lote
        gastando as três vidas faria a matéria ficar invisível para a
        demanda por culpa de um agrupamento que não foi dela (achado da
        revisão adversarial de 03/09/2026)."""
        from src.storage import conecta
        from src import extract, llm
        con = conecta(tmp_path / "lote.db")
        con.execute(
            "INSERT INTO artigos (id, url_norm, url_original, veiculo, "
            "editoria, titulo, resumo, conteudo, hash_conteudo, coletado_em) "
            "VALUES (11,'u','u','G1','x','t','r','c','h','hoje')")
        con.commit()
        linha = con.execute("SELECT * FROM artigos WHERE id = 11").fetchone()
        uso = llm.Uso(modelo=llm.EXTRACAO, entrada=0, saida=0,
                      cache_leitura=0, cache_escrita=0)
        for _ in range(5):
            extract.salva_historia(con, [(linha, [])], [], uso,
                                   extract.PROMPT_VERSAO_HISTORIA,
                                   recusada=True)
        n = con.execute(
            "SELECT recusas FROM extracoes WHERE artigo_id = 11").fetchone()[0]
        assert n == 0, f"o lote gastou {n} vida(s)"
        assert not demanda.ja_extraida(con, 11)

    def test_teto_nunca_pode_ser_um(self):
        """1 é exatamente o comportamento de antes da correção de 03/09."""
        assert demanda.TETO_RECUSAS >= 2

    def test_contador_sobrevive_ao_delete_de_salva_historia(self, tmp_path):
        """O único jeito plausível de errar o patch: incrementar DEPOIS do
        DELETE trava o contador em 1 e o teto nunca dispara."""
        from src.storage import conecta
        from src import extract, llm
        con = conecta(tmp_path / "t.db")
        con.execute(
            "INSERT INTO artigos (id, url_norm, url_original, veiculo, "
            "editoria, titulo, resumo, conteudo, hash_conteudo, coletado_em) "
            "VALUES (9,'u','u','G1','x','t','r','c','h','hoje')")
        con.commit()
        linha = con.execute("SELECT * FROM artigos WHERE id = 9").fetchone()
        uso = llm.Uso(modelo=llm.EXTRACAO, entrada=0, saida=0,
                      cache_leitura=0, cache_escrita=0)
        for esperado in (1, 2, 3):
            extract.salva_historia(
                con, [(linha, [])], [], uso,
                extract.PROMPT_VERSAO_HISTORIA, recusada=True,
                conta_recusa=True)
            n = con.execute(
                "SELECT recusas FROM extracoes WHERE artigo_id = 9"
            ).fetchone()[0]
            assert n == esperado, f"travou em {n}, esperado {esperado}"
        assert demanda.ja_extraida(con, 9)


class TestGuardaDeReferente:
    """06/09/2026: "as alts que postei andaram entre 40 e 50%" puxou três
    pesquisas eleitorais por proximidade de porcentagem. Candidata tem de
    MENCIONAR o referente da premissa; referente sem termo útil não filtra."""

    def _linha(self, titulo, resumo="", conteudo=""):
        return {"titulo": titulo, "resumo": resumo, "conteudo": conteudo}

    def test_termos_uteis_do_referente(self):
        assert demanda._termos("as alts que postei") == ["alts", "postei"]
        assert demanda._termos("o desemprego") == ["desemprego"]
        assert demanda._termos("o BC") == []

    def test_pontuacao_colada_nao_entra_no_termo(self):
        """Revisão de 06/09: 'Ibovespa, Nasdaq, Russell, SPX' é referente
        real do cache e virava 'ibovespa,' — nunca casava."""
        assert demanda._termos("Ibovespa, Nasdaq, Russell, SPX") == [
            "ibovespa", "nasdaq", "russell", "spx"]
        assert demanda._menciona(self._linha("Ibovespa sobe 2%"),
                                 "Ibovespa, Nasdaq, Russell, SPX")

    def test_cabeca_generica_sozinha_nao_casa(self):
        """'a taxa de juros' não pode ser satisfeita por 'Taxa de
        desocupação'; sobra 'juros', e é ele que tem de aparecer."""
        assert demanda._termos("a taxa de juros") == ["juros"]
        assert not demanda._menciona(
            self._linha("Taxa de desocupação cai a 5,6%"), "a taxa de juros")
        assert demanda._menciona(self._linha("Juros sobem no Brasil"),
                                 "a taxa de juros")
        assert demanda._menciona(self._linha("qualquer"), "o governo")

    def test_confere_o_mesmo_texto_que_o_ranking_viu(self):
        """Estadão chega com resumo vazio e o lead no corpo; a guarda lê
        título + começo do corpo, como o embedding."""
        linha = self._linha("Mercado reage à decisão", resumo="",
                            conteudo="A Selic foi mantida em 15% pelo Copom.")
        assert demanda._menciona(linha, "a Selic")

    def test_pesquisa_eleitoral_nao_menciona_as_alts(self):
        eleicao = self._linha("Ciro tem 50% contra 46,1% de Elmano no 2º "
                              "turno do CE, diz pesquisa")
        assert not demanda._menciona(eleicao, "as alts que postei")
        assert demanda._menciona(self._linha("Selic sobe para 15%"),
                                 "a Selic")
        assert demanda._menciona(
            self._linha("Taxa de desemprego cai", "no trimestre"),
            "o desemprego")
        assert not demanda._menciona(
            self._linha("Trump quer se reunir com Putin"), "Esteves")

    def test_referente_sem_termo_util_nao_filtra(self):
        assert demanda._menciona(self._linha("qualquer coisa"), "o BC")
        assert demanda._menciona(self._linha("qualquer coisa"), "")

    def test_garante_repassa_o_referente(self, monkeypatch):
        visto = {}

        def falso(c, t, r=""):
            visto["referente"] = r
            return []

        monkeypatch.setattr(demanda, "candidatas", falso)
        demanda.garante(None, "x", referente="a Selic")
        assert visto["referente"] == "a Selic"
