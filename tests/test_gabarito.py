"""Testes do gabarito — do comparador e dos arquivos de casos.

Nada aqui chama modelo. O que se prende é (a) que os arquivos de casos
estão bem formados e batem com o que o boletim de fato envia ao separador,
e (b) que o comparador julga certo — porque um comparador frouxo faria a
bateria passar sempre, e aí ela protegeria de nada.
"""

from dataclasses import dataclass

import pytest

from src.gabarito import (Resultado, _ao_menos_um, _contem, _normaliza,
                          assinatura, carrega, confere_estrutura,
                          confere_julgamento, confere_premissas,
                          marca_revisao, reproduz_exemplo, resume, revisado)


@dataclass
class P:
    tipo: str
    trecho: str
    afirmacao: str | None = None

    @property
    def texto(self) -> str:
        return self.afirmacao or self.trecho


CHARADA = ("POST 6 (@perfil_teste, 01 Sep 2026):\nCharada: André se reune "
           "com Trump, todos os rumos mudam imediatamente. Quem manda no Brasil?")


class TestContem:
    def test_fronteira_de_palavra_e_de_numero(self):
        assert not _contem("o maior do ano", "maio")
        assert not _contem("em 2015", "15")
        assert not _contem("veio em 15,2%", "5,2")
        assert not _contem("modela bem", "dela")
        assert _contem("a Selic está em 15%", "15")
        assert _contem("o IPCA veio em 5,2%.", "5,2")
        assert _contem("se reuniu com Trump.", "Trump")

    def test_sem_acento_sem_caixa_e_espaco_colapsado(self):
        assert _normaliza("Reúne  com\nTrump") == "reune com trump"
        assert _contem("André se reune", "se reúne")


class TestComparadorDePremissas:
    def test_passa_quando_bate_tudo(self):
        caso = {"texto": CHARADA, "fatos": 1,
                "esperado": [{"tipo": "fato", "contem": "Trump"},
                             {"tipo": "opiniao", "contem": "rumos"}],
                "proibido_em_fato": ["Esteves"]}
        premissas = [P("fato", "André se reune com Trump",
                       "André se reuniu com Trump"),
                     P("opiniao", "todos os rumos mudam imediatamente")]
        assert confere_premissas(caso, premissas) == []

    def test_conta_fatos_exato(self):
        caso = {"fatos": 0, "esperado": []}
        assert confere_premissas(caso, [P("fato", "x")]) == [
            "esperava 0 fato(s), veio 1"]

    def test_acusa_esperado_ausente(self):
        caso = {"esperado": [{"tipo": "relato", "contem": "convivi"}]}
        falhas = confere_premissas(caso, [P("opiniao", "convivi com ele")])
        assert falhas and "faltou [relato]" in falhas[0]

    def test_em_fato_o_esperado_e_conferido_na_reescrita(self):
        """O trecho é literal do post e conteria o pedaço por construção;
        o que vai ao check é a reescrita — é ela que não pode perder o
        referente (revisão de 02/09/2026)."""
        caso = {"esperado": [{"tipo": "fato", "contem": "Trump"}]}
        perdeu = [P("fato", "André se reune com Trump",
                    "André se reuniu com o presidente dos EUA")]
        assert confere_premissas(caso, perdeu) != []
        manteve = [P("fato", "André se reune com Trump",
                     "André se reuniu com Trump")]
        assert confere_premissas(caso, manteve) == []

    def test_acusa_sobrenome_inventado(self):
        caso = {"proibido_em_fato": ["Esteves"]}
        falhas = confere_premissas(
            caso, [P("fato", "André se reune", "André Esteves se reuniu")])
        assert falhas and "inventado" in falhas[0]

    def test_proibido_so_vale_para_fato_e_casa_palavra_inteira(self):
        caso = {"proibido_em_fato": ["Esteves", "maio", "15"]}
        assert confere_premissas(
            caso, [P("opiniao", "André Esteves é genial")]) == []
        assert confere_premissas(
            caso, [P("fato", "x", "o IPCA teve a maior alta desde 2015")]) == []
        assert confere_premissas(
            caso, [P("fato", "x", "o IPCA de maio foi 0,3%")]) != []

    def test_nao_fato_com_reescrita_e_falha(self):
        """O −38% da v2: opinião não pode voltar a vir parafraseada."""
        caso = {"esperado": []}
        falhas = confere_premissas(
            caso, [P("opiniao", "o dólar está caro",
                     "O autor avalia que o dólar está caro")])
        assert falhas and "reescrita paga" in falhas[0]
        assert confere_premissas(caso, [P("opiniao", "o dólar está caro")]) == []

    def test_trecho_tem_de_ser_literal(self):
        """Regra 4, conferível sem modelo: o trecho é o que o boletim
        exibe como citação do post."""
        caso = {"texto": CHARADA, "esperado": []}
        assert confere_premissas(
            caso, [P("fato", "André se reune com Trump")]) == []
        falhas = confere_premissas(
            caso, [P("fato", "André Esteves se reune com Trump")])
        assert falhas and "literal" in falhas[0]
        # Quebra de linha e acento não contam como diferença.
        caso2 = {"texto": "POST 1 (@x, 01 Sep 2026):\nO cara tem:\n\nBanco dele"}
        assert confere_premissas(caso2, [P("opiniao", "O cara tem: Banco dele")]) == []


class TestComparadorDoCheck:
    def test_julgamento_veredito_citacao_e_indice(self):
        caso = {"esperado": "confirmado", "cita_minimo": 1}
        assert confere_julgamento(caso, "confirmado", [1], total=2) == []
        assert confere_julgamento(caso, "sem_evidencia", [], total=2) == [
            "esperava confirmado, veio sem_evidencia",
            "citou 0 evidência(s), mínimo 1"]
        # Índice fora da lista não conta: em produção é descartado e o
        # veredito sairia sem fonte.
        falhas = confere_julgamento(caso, "confirmado", [3], total=2)
        assert any("fora da lista" in f for f in falhas)
        assert any("mínimo" in f for f in falhas)

    def test_sem_evidencia_nao_pode_citar(self):
        caso = {"esperado": "sem_evidencia"}
        assert confere_julgamento(caso, "sem_evidencia", [1], total=1) == [
            "sem_evidencia citando [1]"]

    def test_estrutura_relacao_sujeito_e_negacao(self):
        caso = {"esperado": {"relacao": "participou_de",
                             "sujeito_contem": "andré",
                             "busca_sem": ["não"]}}
        assert confere_estrutura(caso, "participou_de", "André Esteves",
                                 "reunião de André com Trump") == []
        falhas = confere_estrutura(caso, "afirmou", "Trump",
                                   "André não se reuniu")
        assert len(falhas) == 3


class TestRevisao:
    def test_assinatura_muda_com_o_esperado_e_nao_com_a_nota(self):
        caso = {"id": "X", "texto": "t", "esperado": [], "nota": "a"}
        a = assinatura(caso)
        caso["nota"] = "outra prosa"
        assert assinatura(caso) == a
        caso["fatos"] = 1
        assert assinatura(caso) != a

    def test_revisado_so_vale_assinado_sobre_o_conteudo_atual(self):
        caso = {"id": "X", "texto": "t", "esperado": [],
                "revisado_por": "gabriel 2026-09-03 #" }
        caso["revisado_por"] += assinatura(caso)
        assert revisado(caso)
        caso["esperado"] = [{"tipo": "fato", "contem": "x"}]
        assert not revisado(caso)
        assert not revisado({"id": "Y", "revisado_por": None})

    def test_marca_revisao_regrava_o_arquivo(self, tmp_path, monkeypatch):
        from src import gabarito
        import json
        (tmp_path / "x.json").write_text(
            json.dumps([{"id": "A", "texto": "t", "esperado": [],
                         "revisado_por": None, "nota": ""}]),
            encoding="utf-8")
        monkeypatch.setattr(gabarito, "DIR_GABARITOS", tmp_path)
        assert marca_revisao("x", {"A"}, "gabriel") == ["A"]
        caso = carrega("x")[0]
        assert caso["revisado_por"].startswith("gabriel ")
        assert revisado(caso)
        with pytest.raises(SystemExit):
            marca_revisao("x", {"Z"}, "gabriel")


class TestResumo:
    def test_fronteira_nao_conta_como_regressao(self):
        resultados = [
            Resultado("C1", [], "", 0.01),
            Resultado("C1", ["x"], "", 0.01),            # falhou 1 de 2
            Resultado("C7", ["x"], "", 0.01, fronteira=True),
        ]
        casos = [{"id": "C1", "texto": "t", "esperado": []}, {"id": "C7"}]
        assert resume(resultados, casos) == (1, 1, 2)

    def test_vezes_menor_que_um_e_erro(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError):
            _ao_menos_um("0")
        assert _ao_menos_um("2") == 2


class TestArquivosDeCasos:
    def test_premissas_bem_formado(self):
        casos = carrega("premissas")
        assert len(casos) >= 18
        for c in casos:
            assert {"id", "origem", "texto", "esperado", "nota",
                    "revisado_por"} <= set(c), c["id"]
            assert c["texto"].strip(), c["id"]
            fatos = c.get("fatos")
            assert fatos is None or (isinstance(fatos, int)
                                     and not isinstance(fatos, bool)), c["id"]
            for item in c["esperado"]:
                assert item["tipo"] in ("fato", "previsao", "opiniao",
                                        "relato"), c["id"]
                assert item["contem"].strip(), c["id"]
            assert all(p.strip() for p in c.get("proibido_em_fato", [])), c["id"]
            if fatos == 0:
                assert not any(i["tipo"] == "fato" for i in c["esperado"]), c["id"]

    def test_check_bem_formado(self):
        from src.vocabulario import Relacao
        casos = carrega("check")
        for c in casos:
            assert {"id", "origem", "afirmacao", "esperado", "nota",
                    "revisado_por"} <= set(c), c["id"]
            assert c["tipo"] in ("julgamento", "estrutura"), c["id"]
            if c["tipo"] == "julgamento":
                assert c["esperado"] in ("confirmado", "contradito",
                                         "sem_evidencia"), c["id"]
                assert c["evidencias"], c["id"]
                for e in c["evidencias"]:
                    assert {"texto", "veiculo"} <= set(e), c["id"]
                if c["esperado"] == "sem_evidencia":
                    assert not c.get("cita_minimo"), c["id"]
            else:
                relacao = c["esperado"].get("relacao")
                assert relacao is None or Relacao(relacao)

    def test_caso_real_e_o_que_o_boletim_envia(self):
        """Todo caso real carrega o bloco como o radar o transcreveu; o
        texto tem de ser byte a byte o que `para_separacao` produz dele —
        senão o gabarito mede um texto que o boletim nunca enviou."""
        from src import radar
        reais = [c for c in carrega("premissas") if "bloco_radar" in c]
        assert len(reais) >= 8
        for c in reais:
            assert c["texto"] == radar.para_separacao(c["bloco_radar"]), c["id"]
            assert c["bloco_radar"].startswith("POST "), c["id"]

    def test_c1_e_a_charada_exata(self):
        c1 = next(c for c in carrega("premissas") if c["id"] == "C1")
        assert c1["texto"] == CHARADA

    def test_fronteira_e_booleano_quando_presente(self):
        for nome in ("premissas", "check"):
            for c in carrega(nome):
                if "fronteira" in c:
                    assert c["fronteira"] is True, c["id"]

    def test_exemplo_literal_e_detectado(self):
        """C1 é o exemplo trabalhado da regra 8; C14 é a paráfrase que mede
        generalização. O relatório tem de distinguir os dois."""
        from src.premissas import INSTRUCOES
        casos = {c["id"]: c for c in carrega("premissas")}
        assert reproduz_exemplo(casos["C1"], INSTRUCOES)
        assert reproduz_exemplo(casos["C2"], INSTRUCOES)
        assert not reproduz_exemplo(casos["C14"], INSTRUCOES)
        assert not reproduz_exemplo(casos["C21"], INSTRUCOES)

    def test_id_repetido_e_erro(self, tmp_path, monkeypatch):
        from src import gabarito
        (tmp_path / "x.json").write_text('[{"id":"A"},{"id":"A"}]',
                                         encoding="utf-8")
        monkeypatch.setattr(gabarito, "DIR_GABARITOS", tmp_path)
        with pytest.raises(ValueError, match="repetido"):
            carrega("x")
