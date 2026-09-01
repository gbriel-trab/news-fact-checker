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
        monkeypatch.setattr(demanda, "candidatas", lambda c, t: [])
        r = demanda.garante(None, "x")
        assert r.motivo == "sem_candidata" and r.custo == 0

    def test_teto_recusa_antes_da_api(self, monkeypatch):
        monkeypatch.setattr(demanda, "candidatas", lambda c, t: ["m"])
        monkeypatch.setattr(demanda.extract, "extrai_grupo",
                            lambda *a: pytest.fail("o teto não segurou"))
        r = demanda.garante(None, "x",
                            orcamento=demanda.CUSTO_ESTIMADO - 0.01)
        assert r.motivo == "teto" and r.custo == 0

    def test_extraiu_reindexa_so_o_grupo_e_fatura_o_real(self, monkeypatch):
        m1, m2 = {"id": 11}, {"id": 22}
        monkeypatch.setattr(demanda, "candidatas", lambda c, t: [m1, m2])
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
        monkeypatch.setattr(demanda, "candidatas", lambda c, t: [m1, m2])
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
                            lambda c, t: [{"id": 1}, {"id": 2}])
        chamadas = []
        monkeypatch.setattr(
            demanda.extract, "extrai_grupo",
            lambda c, g, *a: chamadas.append(1) or (0, 0.10, True))
        monkeypatch.setattr(demanda.indice, "indexa_afirmacoes",
                            lambda c, so_artigos=None:
                            pytest.fail("reindexou sem tripla"))
        r = demanda.garante(None, "x", orcamento=demanda.CUSTO_ESTIMADO)
        assert len(chamadas) == 1
        assert r.motivo == "extraiu" and r.triplas == 0

    def test_falha_de_indice_nao_vira_falha_de_demanda(self, monkeypatch):
        # Extração PAGA precisa contar como extração mesmo se o Chroma
        # cair — a rota por chave do check segue enxergando o grafo.
        monkeypatch.setattr(demanda, "candidatas", lambda c, t: [{"id": 1}])
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
                                   trecho="X fez Y")
        analise = SimpleNamespace(premissas=[premissa])
        uso = SimpleNamespace(custo=0.0)
        monkeypatch.setattr("src.premissas.separa",
                            lambda texto, conexao=None: (analise, uso))
        monkeypatch.setattr("src.radar.para_separacao", lambda p: p)
        monkeypatch.setattr(
            demanda, "garante",
            lambda c, t, o: demanda.Resultado("extraiu", 1, 3, 0.20))
        monkeypatch.setattr("src.grafo.carrega", lambda c: ["novo"])

        def check_fake(*a, forcar=False, **k):
            if forcar:
                raise RuntimeError("API fora no re-check")

        monkeypatch.setattr("src.check.verifica", check_fake)

        estado = {"acervo": ["velho"], "orcamento": demanda.TETO_USD}
        with pytest.raises(RuntimeError):
            boletim._confere_post("post", _ConexaoFalsa(), estado)
        assert estado["orcamento"] == pytest.approx(demanda.TETO_USD - 0.20)
        assert estado["acervo"] == ["novo"]
