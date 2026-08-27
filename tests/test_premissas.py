"""Testes da separação de premissas.

O que importa aqui é a fronteira entre o que vai ser verificado e o que não
vai. Mandar previsão para o `check.py` gasta uma chamada para receber "sem
evidência" — e pior, sugere que previsão poderia ser desmentida por acervo.
"""

from src.premissas import Analise, INSTRUCOES, Premissa


def p(tipo, afirmacao="x", trecho="x"):
    return Premissa(tipo=tipo, afirmacao=afirmacao, trecho=trecho)


class TestSchema:
    def test_so_tres_tipos_existem(self):
        """A lista é fechada pelo schema, não pedida no prompt — é restrição
        da chamada, e o modelo não consegue devolver um quarto tipo."""
        import pytest
        from pydantic import ValidationError

        for tipo in ("fato", "previsao", "opiniao"):
            assert p(tipo).tipo == tipo
        with pytest.raises(ValidationError):
            p("talvez")

    def test_trecho_e_campo_obrigatorio(self):
        """O trecho literal é o que permite conferir que a separação não
        inventou afirmação que o texto não faz."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Premissa(tipo="fato", afirmacao="x")


class TestFiltro:
    def test_so_fato_vai_para_verificacao(self):
        analise = Analise(premissas=[
            p("fato", "o desemprego está em 5,3%"),
            p("previsao", "a Selic vai subir em setembro"),
            p("opiniao", "o Copom não tem escolha"),
        ])
        fatos = [x for x in analise.premissas if x.tipo == "fato"]
        assert len(fatos) == 1
        assert fatos[0].afirmacao == "o desemprego está em 5,3%"

    def test_texto_sem_fato_nao_gasta_verificacao(self):
        analise = Analise(premissas=[p("opiniao"), p("previsao")])
        assert [x for x in analise.premissas if x.tipo == "fato"] == []


class TestInstrucoes:
    def test_diz_que_numero_nao_garante_fato(self):
        """"o dólar está caro" tem a mesma forma de "o dólar está em R$ 5,80"
        e não é verificável. Sem a regra, o modelo separa por presença de
        número."""
        assert "NÚMERO NÃO GARANTE" in INSTRUCOES

    def test_exige_afirmacao_autonoma(self):
        """A premissa segue sozinha para o check.py, sem o texto ao lado."""
        assert "SE SUSTENTAR SOZINHA" in INSTRUCOES

    def test_proibe_corrigir_o_texto(self):
        """Corrigir na extração esconderia justamente o erro que o módulo
        existe para achar."""
        assert "NÃO CORRIJA" in INSTRUCOES
