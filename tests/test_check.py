"""Testes do check — o que dá para prender sem modelo.

O juiz é chamada de modelo e vive no gabarito. O que se testa aqui é o
freio em CÓDIGO que retém confirmação sem sustentação (03/09/2026: a
consulta 82 confirmou 'ocorreu um encontro' com uma sessão de comissão
qualquer, e prosa no prompt não bastou — J2 falhou três vezes), a
identidade de candidata por veículo, e a versão do prompt.
"""

import pytest

from src.check import (INSTRUCOES_JULGAMENTO, PROMPT_VERSAO, Alinhamento,
                       Julgamento, _chave_candidata, aplica_alinhamento,
                       sujeito_casa, versao_prompt)
from src.indice import Achado


def julgamento(veredito="confirmado", **lacunas):
    alinhamento = [Alinhamento(lacuna=l, na_afirmacao=v[0], na_evidencia=v[1])
                   for l, v in lacunas.items()]
    return Julgamento(alinhamento=alinhamento, veredito=veredito,
                      evidencias=[1] if veredito != "sem_evidencia" else [],
                      justificativa="juiz disse")


class TestSujeitoCasa:
    def test_nome_incompleto_e_contido(self):
        assert sujeito_casa("André", "André Esteves")
        assert sujeito_casa("André Esteves", "André")
        assert sujeito_casa("Esteves", "André Esteves")

    def test_chave_canonica_ignora_sufixo_e_acento(self):
        assert sujeito_casa("Braskem", "Braskem S.A.")
        assert sujeito_casa("desemprego", "Taxa de desemprego no Brasil")
        assert sujeito_casa("Selic", "Taxa Selic")

    def test_pontuacao_do_juiz_nao_atrapalha(self):
        """Primeira rodada do gabarito v4: o juiz escreveu os dois lados
        com parênteses e nada casava."""
        assert sujeito_casa("André (e Trump)",
                            "André Esteves, do BTG, e Donald Trump")
        assert sujeito_casa("o desemprego (taxa de desemprego no Brasil)",
                            "Taxa de desemprego no Brasil")

    def test_generico_nao_casa_com_evento_qualquer(self):
        assert not sujeito_casa("um encontro",
                                "Sessão da Comissão Mista da MP 1357/2026")
        assert not sujeito_casa("Petrobras", "Braskem S.A.")
        assert not sujeito_casa("", "Braskem")
        assert not sujeito_casa("o", "a")


class TestAplicaAlinhamento:
    def test_confirmado_com_sujeito_e_outra_lacuna_passa(self):
        j = julgamento(quem=("André", "André Esteves"),
                       o_que=("reunião com Trump", "reunião com Donald Trump"))
        assert aplica_alinhamento(j) is j

    def test_sujeito_do_estruturador_prevalece(self):
        j = julgamento(quem=("Encontro", "Sessão da Comissão Mista"),
                       quando=("até 01/09/2026", "28 de agosto de 2026"))
        retido = aplica_alinhamento(j, sujeito_afirmacao="Encontro não especificado")
        assert retido.veredito == "sem_evidencia"
        assert retido.evidencias == []
        assert "juiz disse" in retido.justificativa

    def test_sujeito_sem_contraparte_retem(self):
        j = julgamento(quem=("um encontro", None),
                       quando=("até 01/09/2026", "28 de agosto de 2026"))
        assert aplica_alinhamento(j).veredito == "sem_evidencia"

    def test_so_sujeito_alinhado_retem(self):
        j = julgamento(quem=("André", "André Esteves"),
                       o_que=("reunião com Trump", None))
        retido = aplica_alinhamento(j)
        assert retido.veredito == "sem_evidencia"
        assert "outra lacuna" in retido.justificativa

    def test_sujeito_diferente_retem(self):
        j = julgamento(quem=("Petrobras", "Braskem S.A."),
                       o_que=("recuperação extrajudicial",
                              "recuperação extrajudicial"))
        assert aplica_alinhamento(j).veredito == "sem_evidencia"

    def test_contradito_e_sem_evidencia_nao_sao_tocados(self):
        for v in ("contradito", "sem_evidencia"):
            j = julgamento(v, quem=("x", None))
            assert aplica_alinhamento(j) is j


class TestCandidatas:
    def test_identidade_inclui_o_veiculo(self):
        """Modo história grava a mesma tripla para cada veículo que a
        afirma; até 03/09 a cópia do segundo veículo era descartada como
        duplicata e a corroboração saía subcontada."""
        g1 = Achado("André Esteves participou de Reunião", 0.1,
                    {"veiculo": "G1", "sujeito": "André Esteves"})
        valor = Achado("André Esteves participou de Reunião", 0.1,
                       {"veiculo": "Valor", "sujeito": "André Esteves"})
        g1_de_novo = Achado("André Esteves participou de Reunião", 0.2,
                            {"veiculo": "G1", "sujeito": "André Esteves"})
        assert _chave_candidata(g1) != _chave_candidata(valor)
        assert _chave_candidata(g1) == _chave_candidata(g1_de_novo)


class TestPrompt:
    def test_regras_de_alinhamento_e_nome_parcial(self):
        assert "ALINHE ANTES DE JULGAR" in INSTRUCOES_JULGAMENTO
        assert "NOME INCOMPLETO CASA COM NOME COMPLETO" in INSTRUCOES_JULGAMENTO

    def test_alinhamento_vem_antes_do_veredito_no_schema(self):
        campos = list(Julgamento.model_json_schema()["properties"])
        assert campos.index("alinhamento") < campos.index("veredito")

    def test_versao_tem_forma_estavel(self):
        v = versao_prompt()
        assert v == PROMPT_VERSAO and len(v) == 12
        assert all(c in "0123456789abcdef" for c in v)


class TestStorage:
    def test_consulta_grava_versao_do_prompt(self, tmp_path):
        from src.storage import conecta, salva_consulta
        con = conecta(tmp_path / "t.db")
        salva_consulta(con, "x", "confirmado", "j", 1, 1, 1, "m", 0.0,
                       prompt_versao="abc123")
        salva_consulta(con, "y", "confirmado", "j", 1, 1, 1, "m", 0.0)
        versoes = [r["prompt_versao"] for r in
                   con.execute("SELECT prompt_versao FROM consultas ORDER BY id")]
        assert versoes == ["abc123", None]
