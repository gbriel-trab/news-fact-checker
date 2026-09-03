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

        for tipo in ("fato", "previsao", "opiniao", "relato",
                     "nao_verificavel"):
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


class TestReescritaSoParaFato:
    """A evolução de 01/09/2026: a paráfrase é exclusiva do fato (é a
    consulta do verificador); opinião/previsão/relato exibem o trecho
    literal — a reescrita deles era o maior custo de saída da separação."""

    def test_nao_verificavel_sem_afirmacao_usa_o_trecho(self):
        premissa = Premissa(tipo="opiniao",
                            trecho="Confluência é macro.")
        assert premissa.afirmacao is None
        assert premissa.texto == "Confluência é macro."

    def test_fato_sem_reescrita_cai_no_trecho_e_nao_se_perde(self):
        premissa = Premissa(tipo="fato", trecho="o IPCA veio em 5,2%")
        assert premissa.afirmacao == "o IPCA veio em 5,2%"
        assert premissa.texto == "o IPCA veio em 5,2%"

    def test_afirmacao_saiu_dos_obrigatorios_do_schema(self):
        schema = Premissa.model_json_schema()
        assert "afirmacao" not in schema["required"]
        assert "trecho" in schema["required"]

    def test_regra_do_referente_indeterminado_esta_no_prompt(self):
        # O 'CONFIRMADO' absurdo de 01/09: "ocorreu um encontro" casou com
        # uma sessão de comissão qualquer. Fato exige referente.
        assert "REFERENTE DETERMINADO" in INSTRUCOES
        assert "NÃO adivinhe o referente" in INSTRUCOES

    def test_nome_incompleto_e_referente_e_nao_se_completa(self):
        """O boletim de 02/09/2026: "André se reune com Trump, todos os
        rumos mudam" saiu como UMA opinião, sem fato — "André" sem
        sobrenome foi lido como referente indeterminado, e o acervo tinha
        o encontro. Nome incompleto é referente e a reescrita não o
        completa; e o predicado precisa estar no texto tanto quanto o
        sujeito ("André foi lá" segue sem fato). A terceira âncora prende
        o exemplo trabalhado — se ele for trocado, ela muda junto."""
        assert "COMO O TEXTO ESCREVE" in INSTRUCOES
        assert "O nome resolve QUEM" in INSTRUCOES
        assert 'não "André Esteves"' in INSTRUCOES

    def test_reescrita_de_nao_verificavel_e_proibida_no_prompt(self):
        assert "OMITA `afirmacao`" in INSTRUCOES


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


class TestRoteador:
    """A regra 8 v4 vira campo + código: o fato só segue ao check se o
    sujeito e mais uma lacuna estiverem ANCORADOS no texto. Cada
    rebaixamento sai com motivo em `roteado`."""

    def _fato(self, texto, quem=None, o_que=None, quando=None):
        from src.premissas import Referente, roteia

        def ref(par):
            return Referente(valor=par[0], trecho=par[1]) if par else None

        analise = Analise(premissas=[Premissa(
            tipo="fato", afirmacao="x", trecho=texto.split("\n")[-1],
            quem=ref(quem), o_que=ref(o_que), quando=ref(quando))])
        return roteia(analise, texto).premissas[0]

    def test_charada_passa(self):
        texto = ("POST 6 (@x, 01 Sep 2026):\nCharada: André se reune com "
                 "Trump, todos os rumos mudam.")
        p_ = self._fato(texto, quem=("André", "André"),
                        o_que=("Trump", "com Trump"))
        assert p_.tipo == "fato" and p_.roteado is None

    def test_sujeito_sem_ancora_rebaixa(self):
        p_ = self._fato("O cara tem banco dele.",
                        quem=("André Esteves", "André Esteves"),
                        o_que=("banco", "banco dele"))
        assert p_.tipo == "nao_verificavel"
        assert "sem âncora" in p_.roteado and p_.afirmacao is None

    def test_so_sujeito_rebaixa(self):
        p_ = self._fato("O encontro que ocorreu muda o rumo do país.",
                        quem=("o encontro", "O encontro"))
        assert p_.tipo == "nao_verificavel" and "só o sujeito" in p_.roteado

    def test_data_de_janela_nao_ancora(self):
        # "até 01/09/2026" não está no texto: não conta como quando.
        p_ = self._fato("O encontro que ocorreu muda o rumo do país.",
                        quem=("o encontro", "O encontro"),
                        quando=("até 01/09/2026", "até 01/09/2026"))
        assert p_.tipo == "nao_verificavel"

    def test_o_que_pronome_rebaixa(self):
        p_ = self._fato("André foi lá e nada mudou.",
                        quem=("André", "André"), o_que=("lá", "lá"))
        assert p_.tipo == "nao_verificavel" and "pronome" in p_.roteado

    def test_sem_entidade_nem_numero_rebaixa(self):
        p_ = self._fato("O empresário tem um banco.",
                        quem=("o empresário", "O empresário"),
                        o_que=("um banco", "um banco"))
        assert p_.tipo == "nao_verificavel" and "entidade" in p_.roteado

    def test_numero_basta_como_segundo_apoio(self):
        p_ = self._fato("Com o desemprego em 5,3%, o Copom não tem escolha.",
                        quem=("desemprego", "o desemprego"),
                        o_que=("5,3%", "em 5,3%"))
        assert p_.tipo == "fato"

    def test_data_de_ocorrencia_ancorada_basta(self):
        texto = "POST 1 (@x, 01 Sep 2026):\nLula jantou ontem em Brasília."
        p_ = self._fato(texto, quem=("Lula", "Lula"),
                        quando=("31/08/2026", "ontem"))
        assert p_.tipo == "fato"

    def test_nao_fato_nao_e_tocado(self):
        from src.premissas import roteia
        a = Analise(premissas=[Premissa(tipo="opiniao", trecho="x")])
        assert roteia(a, "x").premissas[0].tipo == "opiniao"

    def test_roteado_fica_fora_do_schema(self):
        propriedades = Premissa.model_json_schema()["properties"]
        assert "roteado" not in propriedades
        assert {"quem", "o_que", "quando", "hipotese"} <= set(propriedades)

    def test_regra_9_e_a_ancora_da_regra_8(self):
        assert "LINHAS DE CONTEXTO" in INSTRUCOES
        assert "ancorado no texto" in INSTRUCOES
        assert "nao_verificavel" in INSTRUCOES
