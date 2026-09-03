"""Testes do check — o que dá para prender sem modelo.

O juiz é chamada de modelo e vive no gabarito. O que se testa aqui é o
freio em CÓDIGO que retém confirmação sem sustentação, a identidade de
candidata, a diversidade de veículo no ranking e a versão do prompt.

Histórico que estes testes guardam: a consulta 82 confirmou "ocorreu um
encontro" com uma sessão de comissão qualquer (01/09/2026), e a primeira
versão do freio (03/09) conferia o sujeito contra o texto que o PRÓPRIO
juiz escrevia no alinhamento — parafrasear a evidência bastava para
passar. Agora a conferência é contra o metadado da evidência citada.
"""

import pytest

from src.check import (FATOR_BUSCA, INSTRUCOES_JULGAMENTO, PROMPT_VERSAO,
                       QUANTAS_CANDIDATAS, RESERVA_DIVERSIDADE, Alinhamento,
                       Julgamento, _chave_candidata, _diversifica,
                       _identifica, aplica_alinhamento, sujeito_casa,
                       versao_prompt)
from src.indice import Achado


def evidencia(texto, veiculo="G1", sujeito="", objeto="", valor="",
              distancia=0.1):
    return Achado(texto, distancia,
                  {"veiculo": veiculo, "sujeito": sujeito or texto.split()[0],
                   "objeto": objeto, "valor": valor})


def julgamento(veredito="confirmado", **lacunas):
    alinhamento = [Alinhamento(lacuna=l, na_afirmacao=v[0], na_evidencia=v[1])
                   for l, v in lacunas.items()]
    return Julgamento(alinhamento=alinhamento, veredito=veredito,
                      evidencias=[1] if veredito != "sem_evidencia" else [],
                      justificativa="juiz disse")


ESTEVES = evidencia("André Esteves participou de Reunião entre André Esteves "
                    "e Donald Trump",
                    sujeito="André Esteves",
                    objeto="Reunião entre André Esteves e Donald Trump")
COMISSAO = evidencia("Sessão da Comissão Mista da MP 1357/2026 de 28 de "
                     "agosto de 2026", veiculo="CNN Brasil",
                     sujeito="Comissão Mista da Medida Provisória 1357/2026",
                     objeto="Sessão da Comissão Mista da MP 1357/2026")


class TestSujeitoCasa:
    def test_nome_incompleto_e_contido(self):
        assert sujeito_casa("André", "André Esteves")
        assert sujeito_casa("André Esteves", "André")
        assert sujeito_casa("Esteves", "André Esteves")

    def test_chave_canonica_ignora_sufixo_e_acento(self):
        assert sujeito_casa("Braskem", "Braskem S.A.")
        assert sujeito_casa("desemprego", "Taxa de desemprego no Brasil")
        assert sujeito_casa("Selic", "Taxa Selic")

    def test_pontuacao_nao_atrapalha(self):
        assert sujeito_casa("André (e Trump)",
                            "André Esteves, do BTG, e Donald Trump")

    def test_generico_nao_casa(self):
        """Interseção só com substantivo comum não identifica ninguém —
        medido no acervo: 8 triplas de 'governo' e 28 de 'governo
        federal' são entidades diferentes."""
        assert not sujeito_casa("governo", "governo federal")
        assert not sujeito_casa("um encontro", "encontro de líderes")
        assert not sujeito_casa("a empresa", "a empresa Braskem")
        assert not sujeito_casa("um encontro",
                                "Sessão da Comissão Mista da MP 1357/2026")

    def test_cabeca_de_hierarquia_ou_evento_bloqueia(self):
        """A contenção não pode fabricar a corroboração que o canonico.py
        se recusa a fabricar. Sujeitos reais do acervo (03/09/2026)."""
        assert not sujeito_casa(
            "Luiz Inácio Lula da Silva",
            "governo do presidente Luiz Inácio Lula da Silva")
        assert not sujeito_casa("Donald Trump",
                                "Telefonema entre Lula e Donald Trump")
        assert not sujeito_casa("Flávio Bolsonaro",
                                "Encontro entre Flávio Bolsonaro e Villatoro")
        assert not sujeito_casa("Supremo Tribunal Federal",
                                "ministro do Supremo Tribunal Federal Toffoli")
        assert not sujeito_casa("Câmara", "juiz Diego Câmara")

    def test_falso_negativo_aceito_do_cargo(self):
        """Mesmo token que separa a corte do ministro dela separa o juiz
        da pessoa. Perder confirmação é o erro barato (princípio 5)."""
        assert not sujeito_casa("Diego Câmara", "juiz Diego Câmara")

    def test_entidade_diferente_nao_casa(self):
        assert not sujeito_casa("Petrobras", "Braskem S.A.")
        assert not sujeito_casa("", "Braskem")
        assert not sujeito_casa("o", "a")


class TestIdentifica:
    def test_nome_proprio_identifica_generico_nao(self):
        assert _identifica("André Esteves")
        assert _identifica("Taxa Selic")
        assert not _identifica("Encontro")
        assert not _identifica("o empresário")
        assert not _identifica("")
        assert not _identifica(None)


class TestAplicaAlinhamento:
    def test_confirmado_com_sujeito_e_apoio_na_evidencia_passa(self):
        j = julgamento(quem=("André", "André Esteves"))
        assert aplica_alinhamento(j, [ESTEVES], ["André"],
                                  ["reunião com Donald Trump"]) is j

    def test_sujeito_generico_retem_mesmo_com_juiz_caprichado(self):
        """A consulta 82 de novo: o juiz alinha tudo e parafraseia a
        evidência com a palavra da afirmação. O freio não lê o que ele
        escreveu — lê o sujeito do estruturador."""
        j = julgamento(
            quem=("um encontro", "encontro (sessão da Comissão Mista)"),
            quando=("até 01/09/2026", "28 de agosto de 2026"))
        retido = aplica_alinhamento(j, [COMISSAO], ["Encontro"], [])
        assert retido.veredito == "sem_evidencia" and retido.retida
        assert "não nomeia um sujeito determinado" in retido.justificativa
        assert "juiz disse" in retido.justificativa

    def test_sujeito_vazio_do_estruturador_retem(self):
        j = julgamento(quem=("um encontro", "um encontro"))
        assert aplica_alinhamento(j, [ESTEVES], [""], []).retida
        assert aplica_alinhamento(j, [ESTEVES], [], []).retida

    def test_apoio_ausente_na_evidencia_retem(self):
        """Data não fecha referente: toda tripla tem data, e o juiz a
        preenche sempre. O apoio tem de ser o que a afirmação afirma."""
        j = julgamento(quem=("Braskem", "Braskem S.A."),
                       quando=("em 2026", "12 de agosto de 2026"))
        outra = evidencia("Braskem S.A. divulgou balanço com prejuízo",
                          sujeito="Braskem S.A.", objeto="balanço")
        retido = aplica_alinhamento(j, [outra], ["Braskem"],
                                    ["recuperação extrajudicial"])
        assert retido.retida and "mais um apoio" in retido.justificativa

    def test_valor_como_apoio(self):
        selic = evidencia("Taxa Selic tem atributo 15 % ao ano",
                          sujeito="Taxa Selic", valor=15)
        j = julgamento(quem=("Selic", "Taxa Selic"))
        assert aplica_alinhamento(j, [selic], ["Taxa Selic"], ["15"]) is j

    def test_sem_apoio_estrutural_a_decisao_do_juiz_vale(self):
        """Freio é backstop, não segundo juiz: afirmação sem objeto nem
        valor não tem o que conferir estruturalmente."""
        j = julgamento(quem=("André", "André Esteves"))
        assert aplica_alinhamento(j, [ESTEVES], ["André"], []) is j

    def test_sujeito_diferente_retem(self):
        j = julgamento(quem=("Petrobras", "Braskem S.A."))
        braskem = evidencia("Braskem S.A. solicitou recuperação extrajudicial",
                            sujeito="Braskem S.A.",
                            objeto="recuperação extrajudicial")
        assert aplica_alinhamento(j, [braskem], ["Petrobras"],
                                  ["recuperação extrajudicial"]).retida

    def test_objeto_contra_objeto_nao_ancora(self):
        """O par que confirmaria "Petrobras pediu recuperação" com
        "Braskem pediu recuperação": mesmo predicado, sujeito errado."""
        braskem = evidencia("Braskem S.A. solicitou recuperação extrajudicial",
                            sujeito="Braskem S.A.",
                            objeto="recuperação extrajudicial")
        j = julgamento(quem=("Petrobras", "Braskem S.A."))
        retido = aplica_alinhamento(
            j, [braskem], ["Petrobras", "recuperação extrajudicial"],
            ["recuperação extrajudicial"])
        assert retido.retida

    def test_troca_de_lados_do_estruturador_ancora(self):
        """Mas o objeto da afirmação contra o SUJEITO da tripla vale: o
        estruturador escolhe um dos lados e oscila."""
        selic = evidencia("Taxa Selic tem atributo 15 % ao ano",
                          sujeito="Taxa Selic", valor=15)
        j = julgamento(quem=("Copom", "Taxa Selic"))
        assert aplica_alinhamento(
            j, [selic], ["Comitê de Política Monetária", "Taxa Selic"],
            ["15"]) is j

    def test_confirmado_sem_citar_retem(self):
        j = julgamento(quem=("André", "André Esteves"))
        assert aplica_alinhamento(j, [], ["André"], ["reunião"]).retida

    def test_objeto_da_afirmacao_tambem_serve_de_sujeito(self):
        """O estruturador oscila entre os dois lados da tripla ('Copom'
        ou 'Taxa Selic' para o mesmo fato) — aceitar os dois evita falso
        negativo por escolha de lado."""
        selic = evidencia("Taxa Selic tem atributo 15 % ao ano",
                          sujeito="Taxa Selic", valor=15)
        j = julgamento(quem=("Copom", "Taxa Selic"))
        assert aplica_alinhamento(
            j, [selic], ["Comitê de Política Monetária", "Taxa Selic"],
            ["15"]) is j

    def test_uma_evidencia_boa_entre_varias_basta(self):
        j = julgamento(quem=("André", "André Esteves"))
        j.evidencias = [1, 2]
        assert aplica_alinhamento(j, [COMISSAO, ESTEVES], ["André"],
                                  ["reunião com Donald Trump"]) is j

    def test_retencao_preserva_as_evidencias_citadas(self):
        """O veredito que mais precisa de auditoria não pode ser o que
        menos mostra: as fontes ficam, rotuladas."""
        j = julgamento(quem=("Petrobras", "Braskem S.A."))
        retido = aplica_alinhamento(j, [ESTEVES], ["Petrobras"], ["x"])
        assert retido.evidencias == [1]

    def test_contradito_e_sem_evidencia_nao_sao_tocados(self):
        for v in ("contradito", "sem_evidencia"):
            j = julgamento(v, quem=("x", None))
            assert aplica_alinhamento(j, [ESTEVES], ["x"], ["y"]) is j


class TestCandidatas:
    def test_identidade_inclui_o_veiculo_e_ignora_caixa(self):
        """Modo história grava a mesma tripla para cada veículo que a
        afirma; até 03/09 a cópia do segundo veículo era descartada como
        duplicata e a corroboração saía subcontada. E a re-extração do
        mesmo artigo difere só em caixa."""
        g1 = evidencia("André Esteves participou de Reunião", "G1",
                       sujeito="André Esteves")
        valor = evidencia("André Esteves participou de Reunião", "Valor",
                          sujeito="André Esteves")
        g1_v3 = evidencia("andré esteves participou de reunião", "G1",
                          sujeito="André Esteves")
        assert _chave_candidata(g1) != _chave_candidata(valor)
        assert _chave_candidata(g1) == _chave_candidata(g1_v3)

    def test_reserva_abre_vaga_para_o_segundo_veiculo(self):
        """Medido em 03/09: para a reunião Esteves–Trump o G1 ocupava 7
        das 10 vagas com triplas do mesmo evento, e um veículo com
        fraseado diferente ficaria fora do julgamento."""
        muitas = [evidencia(f"G1 tripla {i}", "G1", distancia=0.1 * i)
                  for i in range(8)]
        muitas.append(evidencia("Valor tripla", "Valor", distancia=1.0))
        escolhidas = _diversifica(muitas, quantas=4, reserva=1)
        veiculos = [a.meta["veiculo"] for a in escolhidas]
        # Sem a reserva as 4 vagas seriam do G1 e o Valor (o mais
        # distante) ficaria fora; as 3 melhores continuam entrando.
        assert veiculos == ["G1", "G1", "G1", "Valor"]

    def test_reserva_volta_ao_ranking_se_nao_ha_veiculo_novo(self):
        so_g1 = [evidencia(f"t{i}", "G1", distancia=0.1 * i) for i in range(5)]
        escolhidas = _diversifica(so_g1, quantas=4, reserva=2)
        assert [a.texto for a in escolhidas] == ["t0", "t1", "t2", "t3"]

    def test_ordem_final_e_a_de_proximidade(self):
        itens = [evidencia("a", "G1", distancia=0.5),
                 evidencia("b", "Valor", distancia=0.1)]
        assert [a.texto for a in _diversifica(itens, quantas=2, reserva=1)] \
            == ["b", "a"]

    def test_constantes_dao_vaga_para_mais_de_um_veiculo(self):
        assert 0 < RESERVA_DIVERSIDADE < QUANTAS_CANDIDATAS
        assert FATOR_BUSCA >= 2


class TestPrompt:
    def test_regras_de_alinhamento_e_nome_parcial(self):
        assert "ALINHE ANTES DE JULGAR" in INSTRUCOES_JULGAMENTO
        assert "NOME INCOMPLETO CASA COM NOME COMPLETO" in INSTRUCOES_JULGAMENTO

    def test_alinhamento_vem_antes_do_veredito_no_schema(self):
        campos = list(Julgamento.model_json_schema()["properties"])
        assert campos.index("alinhamento") < campos.index("veredito")

    def test_retida_fica_fora_do_schema(self):
        assert "retida" not in Julgamento.model_json_schema()["properties"]

    def test_versao_tem_forma_estavel(self):
        v = versao_prompt()
        assert v == PROMPT_VERSAO and len(v) == 12
        assert all(c in "0123456789abcdef" for c in v)


class TestStorage:
    def test_consulta_grava_versao_e_retida(self, tmp_path):
        from src.storage import conecta, salva_consulta
        con = conecta(tmp_path / "t.db")
        salva_consulta(con, "x", "sem_evidencia", "j", 1, 1, 1, "m", 0.0,
                       prompt_versao="abc123", retida=True)
        salva_consulta(con, "y", "confirmado", "j", 1, 1, 1, "m", 0.0)
        linhas = list(con.execute(
            "SELECT prompt_versao, retida FROM consultas ORDER BY id"))
        assert [l["prompt_versao"] for l in linhas] == ["abc123", None]
        assert [l["retida"] for l in linhas] == [1, 0]

    def test_boletim_nao_confunde_retida_com_acervo_vazio(self, tmp_path):
        from src.boletim import _retida
        from src.storage import conecta, salva_consulta
        con = conecta(tmp_path / "t.db")
        salva_consulta(con, "x", "sem_evidencia", "j", 1, 1, 1, "m", 0.0,
                       retida=True)
        salva_consulta(con, "y", "sem_evidencia", "j", 1, 0, 0, "m", 0.0)
        retida, normal = con.execute(
            "SELECT * FROM consultas ORDER BY id").fetchall()
        assert _retida(retida) and not _retida(normal)
