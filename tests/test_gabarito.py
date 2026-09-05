"""Testes do gabarito — do comparador e dos arquivos de casos.

Nada aqui chama modelo. O que se prende é (a) que os arquivos de casos
estão bem formados e batem com o que o boletim de fato envia ao separador,
e (b) que o comparador julga certo — porque um comparador frouxo faria a
bateria passar sempre, e aí ela protegeria de nada.
"""

import hashlib
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


def carrega_local(nome: str) -> list[dict]:
    """`carrega`, mas pulando o teste quando o gabarito é de máquina.

    `gabaritos/premissas.json` está no .gitignore: os casos reais carregam
    o TEXTO de posts do X, e o Developer Agreement deixa redistribuir o ID
    do post, não o conteúdo. Na máquina que tem o arquivo o teste roda
    inteiro; num clone do repositório público ele não existe, e o teste
    tem de PULAR dizendo por quê — quebrar com FileNotFoundError seria
    ruído, e passar em silêncio seria pior.
    """
    try:
        return carrega(nome)
    except FileNotFoundError:
        pytest.skip(f"gabaritos/{nome}.json não vem no repositório público: "
                    f"os casos reproduzem texto de post do X e o gabarito "
                    f"fica local. Rode na máquina que tem o arquivo.")


# Texto SINTÉTICO no formato exato que `radar.para_separacao` entrega ao
# separador. O texto é inventado de propósito — o repositório é público e o
# conteúdo do post não pode ser redistribuído. O caso REAL equivalente é o
# C1, que mora só em `gabaritos/premissas.json`; a estrutura (cabeçalho
# "POST (@handle, data):" e o corpo na linha seguinte) é o que importa aqui.
BLOCO = ("POST (@perfil_teste, 01 Sep 2026):\nVale registrar: André "
         "desembarca em Washington na sexta. Daqui a um mês ninguém vai "
         "lembrar disso.")

# sha256 do `texto` do C1 no gabarito local. Prende o caso real byte a byte
# — exatamente o que o antigo `c1["texto"] == CHARADA` prendia — sem trazer
# o texto do post para dentro do repositório.
SHA256_C1 = "8e38315de948108deac17f9313af270cede616b13fec9ee7a023e6d512e80f1b"


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
        caso = {"texto": BLOCO, "fatos": 1,
                "esperado": [{"tipo": "fato", "contem": "Washington"},
                             {"tipo": "opiniao", "contem": "lembrar"}],
                "proibido_em_fato": ["Esteves"]}
        premissas = [P("fato", "André desembarca em Washington na sexta",
                       "André desembarcou em Washington na sexta"),
                     P("opiniao", "Daqui a um mês ninguém vai lembrar disso")]
        assert confere_premissas(caso, premissas) == []

    def test_conta_fatos_exato(self):
        caso = {"fatos": 0, "esperado": []}
        assert confere_premissas(caso, [P("fato", "x")]) == [
            "esperava 0 fato(s), veio 1"]

    def test_acusa_esperado_ausente(self):
        caso = {"esperado": [{"tipo": "relato", "contem": "convivi"}]}
        falhas = confere_premissas(caso, [P("opiniao", "convivi com ele")])
        assert falhas and "faltou [relato]" in falhas[0]

    def test_tipo_pode_ser_lista_e_qualquer_um_serve(self):
        """O C13 (04/09/2026): "nada mudou" saiu nao_verificavel 2/2 com o
        esperado exigindo opinião, e a taxonomia ali não decide — o que o
        caso cobra é zero fatos. Uma lista aceita os dois; fora dela,
        continua falhando, com os dois nomes na mensagem."""
        caso = {"esperado": [{"tipo": ["opiniao", "nao_verificavel"],
                              "contem": "nada mudou"}]}
        assert confere_premissas(caso, [P("opiniao", "nada mudou")]) == []
        assert confere_premissas(
            caso, [P("nao_verificavel", "nada mudou")]) == []
        falhas = confere_premissas(caso, [P("relato", "nada mudou")])
        assert falhas and "faltou [opiniao/nao_verificavel]" in falhas[0]

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
        caso = {"texto": BLOCO, "esperado": []}
        assert confere_premissas(
            caso, [P("fato", "André desembarca em Washington")]) == []
        falhas = confere_premissas(
            caso, [P("fato", "André Esteves desembarca em Washington")])
        assert falhas and "literal" in falhas[0]
        # Quebra de linha e acento não contam como diferença.
        caso2 = {"texto": "POST (@x, 01 Sep 2026):\nA lista tem:\n\nquatro nomes"}
        assert confere_premissas(caso2, [P("opiniao", "A lista tem: quatro nomes")]) == []


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
            Resultado("C1", ["x"], "", 0.01),
            Resultado("C1", ["x"], "", 0.01),            # falhou nas DUAS
            Resultado("C7", ["x"], "", 0.01, fronteira=True),
        ]
        casos = [{"id": "C1", "texto": "t", "esperado": []}, {"id": "C7"}]
        assert resume(resultados, casos) == (1, 1, 2, [])

    def test_caso_que_passa_as_vezes_e_instavel_e_nao_regressao(self):
        """C13 e C25, 03/09/2026: apareceram como regressão numa bateria
        de uma passada e deram 2/3 e 3/3 quando repetidos. O prompt não
        tinha mudado. Chamar variância de regressão é falso positivo
        dentro da ferramenta que existe para evitar falso positivo."""
        resultados = [
            Resultado("C13", ["x"], "", 0.01),
            Resultado("C13", [], "", 0.01),
            Resultado("C13", [], "", 0.01),              # 2 de 3
        ]
        casos = [{"id": "C13", "texto": "t", "esperado": []}]
        regressoes, _, _, instaveis = resume(resultados, casos)
        assert regressoes == 0
        assert instaveis == ["C13 (2/3)"]

    def test_falhar_em_todas_continua_regressao(self):
        """O pareado: instável não pode virar porta dos fundos."""
        resultados = [Resultado("C13", ["x"], "", 0.01) for _ in range(3)]
        casos = [{"id": "C13", "texto": "t", "esperado": []}]
        regressoes, _, _, instaveis = resume(resultados, casos)
        assert regressoes == 1 and instaveis == []

    def test_uma_passada_que_falha_continua_regressao(self):
        """Com --vezes 1 o comportamento não muda: falhou na única vez é
        falhar em TODAS. A distinção só existe quando há repetição."""
        resultados = [Resultado("C13", ["x"], "", 0.01)]
        casos = [{"id": "C13", "texto": "t", "esperado": []}]
        assert resume(resultados, casos)[0] == 1

    def test_vezes_menor_que_um_e_erro(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError):
            _ao_menos_um("0")
        assert _ao_menos_um("2") == 2


class TestArquivosDeCasos:
    def test_premissas_bem_formado(self):
        casos = carrega_local("premissas")
        assert len(casos) >= 18
        for c in casos:
            assert {"id", "origem", "texto", "esperado", "nota",
                    "revisado_por"} <= set(c), c["id"]
            assert c["texto"].strip(), c["id"]
            fatos = c.get("fatos")
            assert fatos is None or (isinstance(fatos, int)
                                     and not isinstance(fatos, bool)), c["id"]
            for item in c["esperado"]:
                tipos = (item["tipo"] if isinstance(item["tipo"], list)
                         else [item["tipo"]])
                assert tipos and all(
                    t in ("fato", "previsao", "opiniao", "relato",
                          "nao_verificavel") for t in tipos), c["id"]
                assert item["contem"].strip(), c["id"]
            assert all(p.strip() for p in c.get("proibido_em_fato", [])), c["id"]
            if fatos == 0:
                # Lista com `fato` dentro também contaria como fato esperado.
                assert not any("fato" in (i["tipo"] if isinstance(
                    i["tipo"], list) else [i["tipo"]])
                    for i in c["esperado"]), c["id"]

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

    def test_todo_caso_carrega_o_registro_e_o_texto_e_derivado_dele(self):
        """Todo caso carrega o registro do post (`post`: autor, data, texto
        e o referenciado), e o `texto` tem de ser byte a byte o que
        `radar.para_separacao` produz dele — senão o gabarito mede um
        texto que o boletim nunca enviou."""
        from src import radar
        from src.gabarito import captura_de
        casos = carrega_local("premissas")
        assert len(casos) >= 25
        for c in casos:
            assert "post" in c and "bloco_radar" not in c, c["id"]
            assert c["texto"] == radar.para_separacao(
                captura_de(c["post"])), c["id"]
            assert c["texto"].startswith("POST (@"), c["id"]

    def test_c1_e_o_bloco_real_byte_a_byte(self):
        """O C1 é o bloco real que a v2 engoliu — o exemplo trabalhado da
        regra 8. O texto do post não pode morar no repositório, então o
        que se prende aqui é o digest: mudou um byte do caso, cai."""
        c1 = next(c for c in carrega_local("premissas") if c["id"] == "C1")
        assert hashlib.sha256(
            c1["texto"].encode("utf-8")).hexdigest() == SHA256_C1

    def test_fronteira_e_booleano_quando_presente(self):
        # "check" primeiro de propósito: ele está no repositório e continua
        # conferido mesmo quando o pulo do gabarito local vem em seguida.
        for nome in ("check", "premissas"):
            for c in carrega_local(nome):
                if "fronteira" in c:
                    assert c["fronteira"] is True, c["id"]

    def test_exemplo_literal_e_detectado(self):
        """C1 é o exemplo trabalhado da regra 8; C14 é a paráfrase que mede
        generalização. O relatório tem de distinguir os dois."""
        from src.premissas import INSTRUCOES
        casos = {c["id"]: c for c in carrega_local("premissas")}
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
