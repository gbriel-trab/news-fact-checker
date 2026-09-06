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
        """O boletim de 02/09/2026: o post da charada (o C1 do
        gabarito local) saiu como UMA opinião, sem fato — "André"
        sem sobrenome foi lido como referente indeterminado, e o acervo
        tinha o encontro. Nome incompleto é referente e a reescrita não o
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

    def test_positivo_classe_mensuravel_passa(self):
        """C19 (post real do gabarito local), e a barreira nasce com o
        VEREDITO na mao: a afirmacao daquele post era rebaixada, e levada
        ao verificador voltou CONFIRMADO por 4 veiculos (CNN, Folha, G1,
        Agencia Brasil) em 03/09/2026. Regra que manda descartar o que o
        acervo sustenta esta errada. O texto abaixo reconstroi a forma do
        caso -- classe no sujeito, palavra de quantidade no trecho."""
        texto = ("POST 4 (@x, 31 Aug 2026):\nNinguem mais conta quantas "
                 "recuperacoes judiciais atingem marcas iconicas.")
        p_ = self._fato(
            texto, quem=("marcas iconicas", "marcas iconicas"),
            o_que=("recuperacoes judiciais", "recuperacoes judiciais"))
        assert p_.tipo == "fato", p_.roteado

    def test_negativo_classe_sem_medida_continua_rebaixada(self):
        """O pareado. Classe sozinha nao abre a porta: sem palavra de
        quantidade nao ha o que o acervo meca."""
        texto = ("POST 4 (@x, 31 Aug 2026):\nAs marcas iconicas estao "
                 "sofrendo com recuperacoes judiciais.")
        p_ = self._fato(
            texto, quem=("marcas iconicas", "marcas iconicas"),
            o_que=("recuperacoes judiciais", "recuperacoes judiciais"))
        assert p_.tipo == "nao_verificavel"

    def test_negativo_o_empresario_nao_entra_pela_porta_nova(self):
        """O C3 (post real do gabarito local) QUANTIFICA sem medir.
        "TODOS", "infindaveis" e "Praticamente" ficaram fora de
        _QUANTIDADE justamente por isto: e o caso que a regra 8 existe
        para fechar, e a porta nova nao pode reabri-lo. Cada linha abaixo
        carrega uma dessas tres palavras na mesma posicao do original."""
        base = "POST 7 (@x, 01 Sep 2026):\n"
        for trecho, quem, o_que in (
            ("TODOS os cofres alheios sob controle",
             ("os cofres alheios", "os cofres alheios"),
             ("sob controle", "sob controle")),
            ("Assento em infindaveis conselhos fechados",
             ("infindaveis conselhos", "infindaveis conselhos"),
             ("assento", "Assento")),
            ("Praticamente o setor inteiro deve favores a ele",
             ("o setor inteiro", "o setor inteiro"),
             ("favores", "favores")),
        ):
            p_ = self._fato(base + trecho, quem=quem, o_que=o_que)
            assert p_.tipo == "nao_verificavel", (trecho, p_.roteado)

    def test_negativo_o_encontro_continua_fora(self):
        """O caso que fundou a regra por slot (o C2 do gabarito
        local) nao pode voltar: sujeito ancorado mas
        indeterminado, objeto com nome proprio e sem numero."""
        texto = ("POST 3 (@x, 01 Sep 2026):\nO encontro pesa mais para o "
                 "Brasil que qualquer votacao.")
        p_ = self._fato(texto, quem=("O encontro que ocorreu", "O encontro"),
                        o_que=("o Brasil", "o Brasil"))
        assert p_.tipo == "nao_verificavel"
        # Prende a QUEDA NA REGRA POR SLOT: com "o Brasil" ancorado, o
        # freio tem de ser o sujeito indeterminado, não a âncora que
        # falta. A redação anterior caía antes disso, por acidente.
        assert "sem entidade nomeada" in p_.roteado

    def test_charada_passa(self):
        # A forma do C1 (a charada): nome proprio de uma
        # palavra no sujeito, ancorado, com o QUÊ nomeado ao lado.
        texto = ("POST 6 (@x, 01 Sep 2026):\nCharada: André almocou com "
                 "Trump e o jogo virou.")
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
        assert p_.tipo == "nao_verificavel" and "QUÊ" in p_.roteado

    def test_data_de_janela_nao_ancora(self):
        # "até 01/09/2026" não está no texto: não conta como quando.
        p_ = self._fato("O encontro que ocorreu muda o rumo do país.",
                        quem=("o encontro", "O encontro"),
                        quando=("até 01/09/2026", "até 01/09/2026"))
        assert p_.tipo == "nao_verificavel"

    def test_data_nao_substitui_o_que(self):
        """A data do POST está no texto que o modelo recebe, então
        deixá-la valer como segundo apoio era o freio vazando pelo
        cabeçalho: sujeito indeterminado do C3 do gabarito local mais a
        data do próprio post passava."""
        texto = ("POST 7 (@perfil_teste, 01 Sep 2026):\n"
                 "O cara manda em tudo.")
        p_ = self._fato(texto, quem=("o cara", "O cara"),
                        quando=("01/09/2026", "01 Sep 2026"))
        assert p_.tipo == "nao_verificavel"

    def test_cabecalho_do_post_nao_ancora(self):
        from src.premissas import texto_ancoravel
        texto = "POST 7 (@perfil_teste, 01 Sep 2026):\nO cara manda em tudo."
        assert "01 Sep 2026" not in texto_ancoravel(texto)
        assert "manda em tudo" in texto_ancoravel(texto)

    def test_so_a_primeira_linha_e_cabecalho(self):
        """O cabeçalho é tirado só do INÍCIO do texto: uma linha do autor
        que comece com "POST" é texto dele e continua ancorável."""
        from src.premissas import texto_ancoravel
        texto = ("POST (@perfil_teste, 2026-09-01 12:00 UTC):\n"
                 "primeira frase\nPOST 9 do autor, com Selic dentro")
        assert "Selic dentro" in texto_ancoravel(texto)
        assert "12:00 UTC" not in texto_ancoravel(texto)

    def test_fala_do_post_citado_nao_ancora(self):
        """Trecho copiado do post de um terceiro ancorava perfeitamente, e
        a âncora provava que o pedaço está no texto — não que o autor o
        afirmou (caso real do gabarito local). A regra 9 passa a existir
        em código."""
        from src.premissas import CONTEXTO_ALHEIO, texto_ancoravel
        texto = ("POST (@perfil_teste, 2026-09-01 12:00 UTC):\n"
                 f"({CONTEXTO_ALHEIO}: Esse André era vizinho seu?)\n"
                 "Almocei com ele.")
        ancoravel = texto_ancoravel(texto)
        assert "vizinho" not in ancoravel and "Almocei" in ancoravel
        p_ = self._fato(texto, quem=("André", "André"),
                        o_que=("vizinho", "vizinho"))
        assert p_.tipo == "nao_verificavel"

    def test_thread_propria_continua_ancorando(self):
        """Regra 9: o post anterior do próprio autor É texto do autor."""
        from src.premissas import CONTEXTO_PROPRIO, texto_ancoravel
        texto = ("POST (@perfil_teste, 2026-09-01 12:00 UTC):\n"
                 f"({CONTEXTO_PROPRIO}: A Selic está em 15%.)\n"
                 "E vai ficar assim até 2027.")
        assert "Selic" in texto_ancoravel(texto)
        p_ = self._fato(texto, quem=("A Selic", "A Selic"),
                        o_que=("15%", "em 15%"))
        assert p_.tipo == "fato"

    def test_o_que_pronome_ou_indefinido_rebaixa(self):
        p_ = self._fato("André foi lá e nada mudou.",
                        quem=("André", "André"), o_que=("lá", "lá"))
        assert p_.tipo == "nao_verificavel"
        # Indefinido não é pronome e passava com sujeito nomeado.
        p2 = self._fato("Esteves se encontrou com um empresário.",
                        quem=("Esteves", "Esteves"),
                        o_que=("um empresário", "com um empresário"))
        assert p2.tipo == "nao_verificavel" and "indefinido" in p2.roteado

    def test_sem_entidade_nem_numero_rebaixa(self):
        p_ = self._fato("O empresário tem um banco.",
                        quem=("o empresário", "O empresário"),
                        o_que=("um banco", "um banco"))
        assert p_.tipo == "nao_verificavel"

    def test_maiuscula_de_inicio_de_linha_nao_e_nome_proprio(self):
        """O incidente de US$ 0,36 (o C3 do gabarito local): um item
        de lista abre a linha depois dos dois-pontos, e a maiúscula de
        início de linha era lida como nome próprio."""
        texto = "POST 7 (@x, 01 Sep 2026):\nO cara junta:\n\nGrana curta"
        p_ = self._fato(texto, quem=("o cara", "O cara"),
                        o_que=("Grana curta", "Grana curta"))
        assert p_.tipo == "nao_verificavel"

    def test_caixa_alta_e_enfase_nao_sigla(self):
        texto = "POST 7 (@x, 01 Sep 2026):\nTODOS os vizinhos sabem disso."
        p_ = self._fato(texto, quem=("o cara", "TODOS"),
                        o_que=("TODOS os vizinhos", "TODOS os vizinhos"))
        assert p_.tipo == "nao_verificavel"

    def test_ancora_respeita_fronteira_de_palavra(self):
        """"ele" ancorava dentro de "eleição"."""
        p_ = self._fato("O encontro muda mais o rumo que eleição.",
                        quem=("ele", "ele"), o_que=("Trump", "eleição"))
        assert p_.tipo == "nao_verificavel"

    def test_tipografia_nao_derruba_a_ancora(self):
        """O modelo transcreve “medir” como "medir" — e a âncora falhava
        por causa de um caractere (a aspa curva de um post real do
        gabarito local)."""
        texto = 'POST 2 (@x, 01 Sep 2026):\nO relatorio serve para “medir” o risco.'
        p_ = self._fato(texto, quem=("O relatorio", "O relatorio"),
                        o_que=('Selic 15%', '"medir" o risco'))
        assert p_.roteado != "sem o QUÊ ancorado (data não substitui)"

    def test_numero_basta_como_segundo_apoio(self):
        p_ = self._fato("Com o desemprego em 5,3%, o Copom não tem escolha.",
                        quem=("desemprego", "o desemprego"),
                        o_que=("5,3%", "em 5,3%"))
        assert p_.tipo == "fato"

    def test_data_qualifica_mas_nao_sustenta(self):
        """Sujeito + data confirma qualquer jantar do Lula; o QUÊ é que
        sustenta o fato. Com ele, a data entra junto."""
        texto = ("POST 1 (@x, 01 Sep 2026):\n"
                 "Lula jantou ontem com 16 empresários em Brasília.")
        so_data = self._fato(texto, quem=("Lula", "Lula"),
                             quando=("31/08/2026", "ontem"))
        assert so_data.tipo == "nao_verificavel"
        completo = self._fato(texto, quem=("Lula", "Lula"),
                              o_que=("16 empresários", "16 empresários"),
                              quando=("31/08/2026", "ontem"))
        assert completo.tipo == "fato"

    def test_reescrita_de_nao_fato_vira_hipotese(self):
        """"[nao_verificavel] André Esteves tem um banco" saía no boletim
        como se fosse o post: o palpite que a regra 8 manda pôr em
        `hipotese` aparecia como texto."""
        p_ = Premissa(tipo="nao_verificavel", trecho="Banco dele",
                      afirmacao="André Esteves tem um banco")
        assert p_.afirmacao is None
        assert p_.hipotese == "André Esteves tem um banco"
        assert p_.texto == "Banco dele"

    def test_versao_do_prompt_inclui_o_roteador(self):
        """O roteador é código e não entra no prompt: sem ele no hash,
        consertá-lo não invalidava as separações em cache, e o freio
        corrigido não rodaria em nenhum post já separado."""
        from src.premissas import versao_roteador
        import json
        from src import llm
        from src.premissas import INSTRUCOES as I
        v = versao_roteador()
        material = I + json.dumps(
            {"schema": Analise.model_json_schema(), "modelo": llm.VERIFICACAO.id,
             "esforco": llm.VERIFICACAO.esforco, "roteador": v},
            sort_keys=True, ensure_ascii=False)
        import hashlib
        assert len(v) == 8
        assert hashlib.sha256(material.encode("utf-8")).hexdigest()[:12] ==             PROMPT_VERSAO

    def test_nao_fato_nao_e_tocado(self):
        from src.premissas import roteia
        a = Analise(premissas=[Premissa(tipo="opiniao", trecho="x")])
        assert roteia(a, "x").premissas[0].tipo == "opiniao"

    def test_roteado_fica_fora_do_schema(self):
        propriedades = Premissa.model_json_schema()["properties"]
        assert "roteado" not in propriedades
        assert {"quem", "o_que", "quando", "hipotese"} <= set(propriedades)

    def test_regra_9_e_a_ancora_da_regra_8(self):
        from src.premissas import CONTEXTO_ALHEIO, CONTEXTO_PROPRIO
        assert "LINHA DE CONTEXTO" in INSTRUCOES
        # Os dois prefixos que o radar escreve têm de estar descritos no
        # prompt: é a regra 9 que diz ao modelo o que cada um significa.
        assert "post anterior do próprio autor" in INSTRUCOES
        assert "post citado pelo autor" in INSTRUCOES
        assert CONTEXTO_PROPRIO.startswith("contexto — ")
        assert CONTEXTO_ALHEIO.startswith("contexto — ")
        assert "ancorado no texto" in INSTRUCOES
        assert "nao_verificavel" in INSTRUCOES
