"""Testes da separação de premissas.

O que importa aqui é a fronteira entre o que vai ser verificado e o que não
vai. Mandar previsão para o `check.py` gasta uma chamada para receber "sem
evidência" — e pior, sugere que previsão poderia ser desmentida por acervo.
O tipo `relato` existe pela mesma economia: "o autor afirma que opera assim"
custou 11 verificações inúteis no boletim de estreia (31/08/2026).
"""

from src.premissas import (Analise, INSTRUCOES, PROMPT_VERSAO, Premissa,
                           _grava_separacao, _hash_texto, _separacao_gravada,
                           versao_prompt)


def p(tipo, afirmacao="x", trecho="x"):
    return Premissa(tipo=tipo, afirmacao=afirmacao, trecho=trecho)


class TestSchema:
    def test_a_lista_de_tipos_e_fechada(self):
        """Fechada pelo schema, não pedida no prompt — é restrição da
        chamada, e o modelo não consegue devolver um quinto tipo."""
        import pytest
        from pydantic import ValidationError

        for tipo in ("fato", "previsao", "opiniao", "relato"):
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

    def test_relato_nao_vai_para_verificacao(self):
        """O caso do boletim nº 1: 'o autor fica fora na onda 4' não tem
        como ser confirmado por acervo de imprensa — a prova é o post."""
        analise = Analise(premissas=[
            p("relato", "o autor fica fora do mercado na onda 4"),
            p("fato", "o IPCA de julho de 2026 foi de 5,2%"),
        ])
        fatos = [x for x in analise.premissas if x.tipo == "fato"]
        assert len(fatos) == 1
        assert "IPCA" in fatos[0].afirmacao


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

    def test_relato_do_autor_tem_regra_e_desembrulho(self):
        """A regra que faltou no boletim nº 1 — e a sutileza que a impede
        de jogar fora fato do mundo embrulhado em primeira pessoa."""
        assert "RELATO DO PRÓPRIO AUTOR" in INSTRUCOES
        assert "DESEMBRULHE" in INSTRUCOES


class TestVersionamento:
    def test_hash_tem_forma_estavel(self):
        v = versao_prompt()
        assert v == PROMPT_VERSAO and len(v) == 12
        assert all(c in "0123456789abcdef" for c in v)

    def test_hash_do_texto_normaliza(self):
        assert _hash_texto("Compro  antes da 1") == _hash_texto(
            "compro antes da 1")


class TestSeparacoesGravadas:
    def test_roundtrip_e_reuso(self, tmp_path):
        from src.storage import conecta
        con = conecta(tmp_path / "t.db")
        analise = Analise(premissas=[p("fato", "o IPCA foi de 5,2%")])
        h = _hash_texto("texto qualquer")
        assert _separacao_gravada(con, h) is None
        _grava_separacao(con, h, analise, 0.03)
        gravada = _separacao_gravada(con, h)
        assert gravada is not None
        assert Analise.model_validate_json(
            gravada["premissas_json"]) == analise
        assert gravada["prompt_versao"] == PROMPT_VERSAO

    def test_gravar_duas_vezes_nao_quebra(self, tmp_path):
        from src.storage import conecta
        con = conecta(tmp_path / "t.db")
        analise = Analise(premissas=[p("opiniao")])
        _grava_separacao(con, "abc", analise, 0.01)
        _grava_separacao(con, "abc", analise, 0.01)
        assert con.execute(
            "SELECT COUNT(*) FROM separacoes").fetchone()[0] == 1
