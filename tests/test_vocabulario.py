"""Testes do vocabulário controlado.

A lista fechada existe para que fontes diferentes cheguem ao mesmo valor. Erro
aqui não levanta exceção: produz triplas que não se encontram no grafo, e o
sintoma é "sem evidência" onde havia corroboração.
"""

import pytest
from pydantic import ValidationError

from src.extract import Tripla
from src.vocabulario import DEFINICOES, VERSAO, Relacao, resumo_para_prompt


class TestLista:
    def test_toda_relacao_tem_definicao(self):
        """Relação sem definição chega ao modelo como nome solto, e ele
        adivinha o que significa."""
        assert set(DEFINICOES) == set(Relacao)

    def test_valvula_de_escape_existe(self):
        """Sem `outro`, o que não couber some sem deixar rastro — e a lista
        nunca aprende o que está faltando."""
        assert Relacao.OUTRO in Relacao

    def test_versao_e_positiva(self):
        """Zero era a fase de relação livre; triplas daquela época não são
        comparáveis com estas."""
        assert VERSAO >= 1

    def test_nomes_sao_minusculos_sem_espaco(self):
        for r in Relacao:
            assert r.value == r.value.lower()
            assert " " not in r.value

    def test_resumo_lista_todas(self):
        texto = resumo_para_prompt()
        for r in Relacao:
            assert r.value in texto


class TestSchemaRecusaForaDaLista:
    def _tripla(self, relacao):
        return Tripla(
            sujeito="Vale", sujeito_canonico="Vale S.A.", relacao=relacao,
            objeto="Ferrous", objeto_canonico="Ferrous Resources",
            tipo_relacao="evento", origem="EXTRACTED",
            valor_numero=None, valor_unidade=None, valor_contexto=None,
            data_fato=None, sentenca=0,
        )

    def test_aceita_relacao_da_lista(self):
        assert self._tripla("afirmou").relacao is Relacao.AFIRMOU

    def test_recusa_relacao_inventada(self):
        """Era exatamente o que acontecia antes: `foi_filiado_a`,
        `filiou_se_a` e `integrou` para a mesma afirmação."""
        with pytest.raises(ValidationError):
            self._tripla("filiou_se_a")

    def test_recusa_variacao_de_grafia(self):
        with pytest.raises(ValidationError):
            self._tripla("AFIRMOU")

    def test_enum_aparece_no_schema_enviado_a_api(self):
        """É o schema que restringe a geração. Se o enum não estiver nele, a
        trava não existe — vira pedido no prompt."""
        schema = Tripla.model_json_schema()
        valores = schema["$defs"]["Relacao"]["enum"]
        assert set(valores) == {r.value for r in Relacao}
