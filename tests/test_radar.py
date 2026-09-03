"""As partes do radar que não dependem de rede: prompt, parse e links.

A regra que mais importa aqui foi medida em 30/08/2026: o modelo NÃO vê o
filtro `allowed_x_handles` — só o prompt direciona. Um prompt que não nomeia
os handles devolve "quais handles?" e paga a chamada mesmo assim.
"""

from src.radar import (_corpo, _handles_de, _links_de, _posts_de, _prompt,
                       id_status, url_do_post)


class TestPrompt:
    def test_nomeia_todos_os_handles(self):
        texto = _prompt(("perfil_teste", "outro_perfil"), 2)
        assert "@perfil_teste" in texto
        assert "@outro_perfil" in texto

    def test_pede_transcricao_integral(self):
        assert "ÍNTEGRA" in _prompt(("a",), 2)

    def test_pede_url_e_contexto_de_resposta(self):
        # O pareamento link↔post e o contexto de thread vêm do MODELO —
        # as anotações da API chegam sem posição (01/09/2026), então não
        # há como parear do nosso lado depois.
        texto = _prompt(("a",), 2)
        assert "URL:" in texto
        assert "EM RESPOSTA A" in texto

    def test_quote_vem_transcrito(self):
        # Cobre o QUOTE SECO: autor comenta por cima de um embed sem
        # reescrever o conteúdo (caso real de 01/09/2026: a pergunta que
        # o post da confluência respondia ficava só no embed e se perdia).
        # Quando o autor reescreve o citado no corpo — caso RIOT — a
        # transcrição normal já basta. Mesma receita do EM RESPOSTA A.
        assert "CITANDO" in _prompt(("a",), 2)

    def test_prompt_pede_DADO_e_nao_filtragem(self):
        """O prompt ENCOLHEU em 03/09/2026, e essa é a correção.

        Ele mandava "NÃO TRANSCREVA respostas a outros usuários" desde
        01/09 e o modelo transcrevia assim mesmo — pior, apontava a RAIZ
        da thread como pai quando o pai era um terceiro. Um terço do
        prompt era regra desobedecida, e regra desobedecida é pior que
        ausente: dá a impressão de que a barreira existe. Agora o prompt
        pede o LINK do post respondido e manda trazer tudo; quem filtra
        é `filtra_respostas`, em código."""
        texto = _prompt(("a",), 2)
        assert "NÃO TRANSCREVA respostas" not in texto
        assert "link do post respondido" in texto
        assert "quem descarta é o programa" in texto
        assert "TIPO: post | thread | quote | resposta" in texto
        # Cresceu de 832 para ~1.070 com o TIPO obrigatorio, e cresceu
        # certo: e o campo que faz o filtro falhar FECHADO. O teto sobe
        # com o motivo escrito, nao some.
        assert len(texto) < 1200, f"o prompt voltou a crescer: {len(texto)}"

    def test_corpo_carrega_filtro_e_janela(self):
        from datetime import datetime, timedelta, timezone
        hoje = datetime.now(timezone.utc).date()
        corpo = _corpo(("perfil_teste",), 3)
        ferramenta = corpo["tools"][0]
        assert ferramenta["type"] == "x_search"
        assert ferramenta["allowed_x_handles"] == ["perfil_teste"]
        assert ferramenta["from_date"] == (hoje - timedelta(days=3)).isoformat()
        # to_date é AMANHÃ: o limite superior real é a meia-noite UTC do
        # to_date (medido em 31/08 e 01/09/2026) — com to_date=hoje, o
        # boletim nunca via os posts do próprio dia.
        assert ferramenta["to_date"] == (hoje + timedelta(days=1)).isoformat()


class TestParseDePosts:
    def test_separa_blocos_delimitados(self):
        texto = ("POST 1 (@a, 2026-08-29):\nprimeiro\n---\n"
                 "POST 2 (@a, 2026-08-30):\nsegundo\n---")
        posts, notas = _posts_de(texto)
        assert len(posts) == 2
        assert "primeiro" in posts[0] and "segundo" in posts[1]
        assert notas == ()

    def test_resposta_fora_do_formato_vira_post_unico(self):
        # Fora do formato não é descartada: é mostrada como veio.
        posts, notas = _posts_de("O handle não publicou nada relevante.")
        assert len(posts) == 1 and notas == ()

    def test_vazio_devolve_nada(self):
        assert _posts_de("   \n  ") == ((), ())

    def test_aviso_antes_do_primeiro_post_vira_nota(self):
        # O prompt pede "handle sem resultado, diga numa linha à parte" —
        # essa linha não pode sumir nem virar texto de autor.
        texto = ("O @foo não retornou posts na janela.\n"
                 "POST 1 (@bar, 2026-08-30):\ntexto do post\n---")
        posts, notas = _posts_de(texto)
        assert posts == ("POST 1 (@bar, 2026-08-30):\ntexto do post",)
        assert notas == ("O @foo não retornou posts na janela.",)

    def test_aviso_depois_do_ultimo_post_vira_nota(self):
        texto = ("POST 1 (@bar, 2026-08-30):\ntexto\n---\n"
                 "O @foo não retornou nada.")
        posts, notas = _posts_de(texto)
        assert len(posts) == 1
        assert "O @foo" not in posts[0]
        assert notas == ("O @foo não retornou nada.",)


class TestHandles:
    def test_normaliza_e_filtra(self):
        assert _handles_de("@perfil_teste, outro") == ("perfil_teste",
                                                        "outro")

    def test_arroba_sozinho_cai_fora(self):
        # '@' sobrevivia ao filtro antigo e disparava busca paga com
        # handle vazio — a normalização vem ANTES do filtro.
        assert _handles_de("@") == ()
        assert _handles_de("@,perfil_teste") == ("perfil_teste",)

    def test_vazio_devolve_nada(self):
        assert _handles_de(" , ") == ()


class TestLinks:
    def test_acha_status_no_json_bruto(self):
        bruto = ('{"annotations":[{"url":"https://x.com/i/status/123"},'
                 '{"url":"https://x.com/fulano/status/456"}]}')
        assert _links_de(bruto) == ("https://x.com/i/status/123",
                                    "https://x.com/fulano/status/456")

    def test_deduplica_preservando_ordem(self):
        bruto = ("https://x.com/i/status/9 https://x.com/i/status/8 "
                 "https://x.com/i/status/9")
        assert _links_de(bruto) == ("https://x.com/i/status/9",
                                    "https://x.com/i/status/8")

    def test_perfil_sem_status_nao_e_link_de_post(self):
        assert _links_de('{"url":"https://x.com/i/user/777"}') == ()


class TestCitacoes:
    def test_prefere_anotacoes_do_servidor(self):
        # URL no texto do modelo sem anotação correspondente fica FORA:
        # medido em 01/09/2026, o modelo escreveu duas URLs inventadas, e
        # o regex sobre o JSON inteiro as teria posto no conjunto que
        # valida a própria linha URL: — alucinação validando a si mesma.
        from src.radar import _citacoes_de
        dados = {"output": [
            {"content": [{"annotations": [
                {"type": "url_citation",
                 "url": "https://x.com/i/status/123"}]}]},
            {"text": "veja https://x.com/x/status/999"},
        ]}
        assert _citacoes_de(dados) == ("https://x.com/i/status/123",)

    def test_anotacao_que_nao_e_status_fica_fora(self):
        from src.radar import _citacoes_de
        dados = {"annotations": [
            {"type": "url_citation", "url": "https://x.com/i/user/7"}]}
        assert _citacoes_de(dados) == ()


class TestParaSeparacao:
    def test_reatribui_o_interlocutor_e_tira_a_url(self):
        from src.radar import para_separacao
        bloco = ("POST 1 (@x, 01 Sep 2026):\n"
                 "URL: https://x.com/x/status/123\n"
                 "EM RESPOSTA A (@grok): o índice subiu 40% no ano\n"
                 "Então falta muito?")
        saida = para_separacao(bloco)
        assert "URL:" not in saida
        assert "palavras do interlocutor, não do autor" in saida
        assert "(@grok): o índice subiu 40% no ano" in saida
        assert "Então falta muito?" in saida

    def test_thread_propria_nao_vira_interlocutor(self):
        # Com resposta a terceiros excluída da captura, EM RESPOSTA A
        # passa a apontar o post anterior da PRÓPRIA thread — palavras do
        # mesmo autor. Rotulá-las de "interlocutor" poria a premissa
        # legítima (ex.: update de posição em thread) sob suspeita.
        from src.radar import para_separacao
        bloco = ("POST 1 (@perfil_teste, 01 Sep 2026):\n"
                 "EM RESPOSTA A (@perfil_teste): MINERADORAS: tirando "
                 "meio hedge 20% abaixo do topo.\n"
                 "Update: retomando 25% da posição")
        saida = para_separacao(bloco)
        assert "post anterior do próprio autor na thread" in saida
        assert "interlocutor" not in saida
        assert "Update: retomando 25%" in saida

    def test_bloco_sem_linhas_novas_passa_intacto(self):
        from src.radar import para_separacao
        bloco = "POST 1 (@x, data):\ntexto simples"
        assert para_separacao(bloco) == bloco

    def test_post_citado_e_reatribuido_a_quem_o_escreveu(self):
        # Caso RIOT (01/09/2026): a tese com números era do analista
        # CITADO; o autor só comentou por cima. Sem reatribuir, os números
        # do citado virariam premissa do autor.
        from src.radar import para_separacao
        bloco = ("POST 1 (@x, data):\n"
                 "CITANDO (@sigel): RIOT assina contrato de US$ 9 bi\n"
                 "Empresas boas sabem divulgar por estrutura.")
        saida = para_separacao(bloco)
        assert "as afirmações são de quem ele cita" in saida
        assert "(@sigel): RIOT assina contrato" in saida
        assert "Empresas boas" in saida


class TestUrlDoPost:
    BLOCO = ("POST 1 (@x, 01 Sep 2026):\n"
             "URL: https://x.com/x/status/123\n"
             "texto do post")

    def test_url_que_confere_por_id_de_status(self):
        # As anotações usam x.com/i/status/N; o modelo escreve
        # x.com/handle/status/N — o ID é o que identifica.
        url, confere = url_do_post(self.BLOCO,
                                   ("https://x.com/i/status/123",))
        assert url == "https://x.com/x/status/123" and confere

    def test_url_fora_das_citacoes_e_alegacao_sem_lastro(self):
        url, confere = url_do_post(self.BLOCO,
                                   ("https://x.com/i/status/999",))
        assert url == "https://x.com/x/status/123" and not confere

    def test_bloco_sem_linha_url(self):
        assert url_do_post("POST 1 (@x):\ntexto", ()) == (None, False)

    def test_id_status(self):
        assert id_status("https://x.com/i/status/42") == "42"
        assert id_status("https://x.com/i/user/42") is None


class TestRepeticaoDaBusca:
    """03/09/2026: a xAI estourou os 180s às 12:00 e o boletim do dia
    morreu ali — uma tentativa só, e a próxima chance 24h depois. Timeout
    e 5xx repetem; 4xx não, porque não melhoram na segunda vez."""

    def _post(self, respostas):
        """Devolve um requests.post falso que consome `respostas` em ordem;
        cada item é uma exceção a levantar ou um (status, corpo)."""
        chamadas = []

        class Resp:
            def __init__(self, status, corpo):
                self.status_code, self._corpo = status, corpo
                self.text = str(corpo)

            def json(self):
                return self._corpo

        def falso(*a, **kw):
            item = respostas[len(chamadas)]
            chamadas.append(1)
            if isinstance(item, Exception):
                raise item
            return Resp(*item)
        return falso, chamadas

    def test_timeout_repete_e_a_segunda_vale(self, monkeypatch):
        import requests
        from src import radar
        falso, chamadas = self._post([
            requests.exceptions.ReadTimeout("read timed out"),
            (200, {"output": []}),
        ])
        monkeypatch.setattr(radar.requests, "post", falso)
        dormiu = []
        assert radar._pede("k", ("x",), 1, dormir=dormiu.append) == {"output": []}
        assert len(chamadas) == 2 and dormiu == [radar.ESPERA]

    def test_desiste_depois_de_todas_e_diz_quantas(self, monkeypatch):
        import pytest
        import requests
        from src import radar
        falso, chamadas = self._post(
            [requests.exceptions.ReadTimeout("t")] * radar.TENTATIVAS)
        monkeypatch.setattr(radar.requests, "post", falso)
        with pytest.raises(radar.FalhaNoRadar, match="após 3 tentativas"):
            radar._pede("k", ("x",), 1, dormir=lambda s: None)
        assert len(chamadas) == radar.TENTATIVAS

    def test_erro_de_requisicao_nao_repete(self, monkeypatch):
        import pytest
        from src import radar
        falso, chamadas = self._post([(400, "parâmetro inválido")])
        monkeypatch.setattr(radar.requests, "post", falso)
        with pytest.raises(radar.FalhaNoRadar, match="400"):
            radar._pede("k", ("x",), 1, dormir=lambda s: None)
        assert len(chamadas) == 1

    def test_servidor_e_limite_repetem(self, monkeypatch):
        from src import radar
        for status in (500, 429):
            falso, chamadas = self._post([(status, "x"), (200, {"ok": 1})])
            monkeypatch.setattr(radar.requests, "post", falso)
            assert radar._pede("k", ("x",), 1, dormir=lambda s: None) == {"ok": 1}
            assert len(chamadas) == 2

    def test_espera_cresce_a_cada_tentativa(self, monkeypatch):
        import requests
        from src import radar
        falso, _ = self._post([requests.exceptions.ReadTimeout("t")] * 2
                              + [(200, {"ok": 1})])
        monkeypatch.setattr(radar.requests, "post", falso)
        dormiu = []
        radar._pede("k", ("x",), 1, dormir=dormiu.append)
        assert dormiu == [radar.ESPERA, radar.ESPERA * 2]


class TestContratoDoCorpo:
    """Corpo 200 com JSON VÁLIDO mas fora do formato escapava limpo de
    `_pede` e estourava AttributeError lá na frente, onde o boletim só
    captura FalhaNoRadar — o log ficava com o cabeçalho do dia e nada
    mais (achado de 03/09/2026)."""

    def _resposta(self, corpo, status=200, headers=None):
        class Resp:
            def __init__(self):
                self.status_code, self.text = status, str(corpo)
                self.headers = headers or {}

            def json(self):
                return corpo
        return Resp()

    def test_corpo_que_nao_e_objeto_vira_tentativa(self, monkeypatch):
        from src import radar
        respostas = [self._resposta([]), self._resposta(None),
                     self._resposta({"output": []})]
        chamadas = []

        def falso(*a, **kw):
            chamadas.append(1)
            return respostas[len(chamadas) - 1]
        monkeypatch.setattr(radar.requests, "post", falso)
        assert radar._pede("k", ("x",), 1, dormir=lambda s: None) == {
            "output": []}
        assert len(chamadas) == 3

    def test_a_mensagem_final_avisa_do_custo_incerto(self, monkeypatch):
        import pytest
        import requests
        from src import radar
        monkeypatch.setattr(
            radar.requests, "post",
            lambda *a, **kw: (_ for _ in ()).throw(
                requests.exceptions.ReadTimeout("t")))
        with pytest.raises(radar.FalhaNoRadar, match="console da xAI"):
            radar._pede("k", ("x",), 1, dormir=lambda s: None)


class TestRetryAfter:
    def test_o_servidor_manda_quando_pede_mais(self):
        from src import radar

        class Resp:
            headers = {"Retry-After": "300"}
        assert radar._quanto_esperar(1, Resp()) == 300

    def test_nossa_espera_vale_quando_o_pedido_e_menor(self):
        from src import radar

        class Resp:
            headers = {"Retry-After": "5"}
        assert radar._quanto_esperar(2, Resp()) == radar.ESPERA * 2

    def test_teto_impede_pendurar_a_tarefa(self):
        from src import radar

        class Resp:
            headers = {"Retry-After": "3600"}
        assert radar._quanto_esperar(1, Resp()) == radar.TETO_ESPERA

    def test_sem_header_e_sem_resposta_nao_quebra(self):
        from src import radar

        class Resp:
            headers = {"Retry-After": "sexta-feira"}
        assert radar._quanto_esperar(1, None) == radar.ESPERA
        assert radar._quanto_esperar(1, Resp()) == radar.ESPERA


class TestRespostaATerceiro:
    """A barreira de 03/09/2026, e ela entra com os POSITIVOS pareados.

    O prompt manda ignorar resposta a terceiro desde 01/09; o modelo
    transcreveu assim mesmo — das 14 entradas de 31/08, 3 eram posts e
    11 eram respostas, quase todas ao @grok e varias sem uma palavra do
    autor. Prompt e pedido, codigo e barreira.

    O RISCO desta barreira, e por isso os tres primeiros testes: ela nao
    pode engolir a continuacao de thread propria (o C25 do gabarito
    depende dela), nem o quote, nem o post puro."""

    H = ("perfil_teste",)

    def _bloco(self, miolo):
        from src.radar import resposta_a_terceiro
        return resposta_a_terceiro(
            "POST 1 (@perfil_teste, 31 Aug 2026):\n" + miolo, self.H)

    def test_positivo_post_puro_fica(self):
        assert self._bloco("A Selic esta em 15%.") is None

    def test_positivo_thread_propria_fica(self):
        """O caso do C25: o autor respondendo a si mesmo. Descartar isto
        mataria a premissa que so faz sentido com o post anterior."""
        assert self._bloco(
            "EM RESPOSTA A (@perfil_teste): A Selic esta em 15%.\n"
            "E vai ficar assim ate 2027.") is None

    def test_positivo_quote_fica(self):
        assert self._bloco(
            "CITANDO (@outro): alguma coisa\nComentario do autor.") is None

    def test_negativo_resposta_a_terceiro_sai(self):
        assert self._bloco(
            "EM RESPOSTA A (@grok): explica ai\nE amigo, nem o grok deu "
            "conta.") == "@grok"

    def test_caixa_do_handle_nao_engana(self):
        assert self._bloco(
            "EM RESPOSTA A (@Perfil_Teste): anterior\ncontinuacao") is None


    def test_handle_truncado_sai(self):
        """`boletim_posts.resumo` guarda resumo[:120], entao o handle
        chega cortado. Exigir o ")" fazia a funcao falhar ABERTO — e
        falhar aberto aqui e deixar passar o que se quer barrar."""
        assert self._bloco("EM RESPOSTA A (@corte") == "@corte"

    def test_positivo_thread_propria_TRUNCADA_fica(self):
        """O pareado do anterior, e o que a igualdade crua quebraria: o
        proprio autor cortado no meio continua sendo o proprio autor."""
        assert self._bloco("EM RESPOSTA A (@perfil_t") is None
        assert self._bloco("EM RESPOSTA A (@perfil") is None

    def test_busca_descarta_e_CONTA_na_nota(self, monkeypatch):
        """Descarte silencioso e o que esconde defeito: se o modelo
        passar a obedecer, ou a marcar errado, a contagem muda e
        aparece."""
        from src import radar
        texto = (
            "POST 1 (@perfil_teste, 31 Aug 2026):\n"
            "URL: https://x.com/perfil_teste/status/1\n"
            "TIPO: post\n"
            "Tremenda absorcao do mercado.\n---\n"
            "POST 2 (@perfil_teste, 31 Aug 2026):\n"
            "URL: https://x.com/perfil_teste/status/2\n"
            "EM RESPOSTA A (@grok): explica\nNope\n---\n"
            "POST 3 (@perfil_teste, 31 Aug 2026):\n"
            "URL: https://x.com/perfil_teste/status/3\n"
            "TIPO: thread\n"
            "EM RESPOSTA A (@perfil_teste): anterior\nExpansao\n---")
        monkeypatch.setattr(radar, "_pede", lambda *a, **k: {
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": texto}]}],
            "usage": {"cost_in_usd_ticks": 0}})
        monkeypatch.setenv("XAI_API_KEY", "x")
        r = radar.busca(self.H, 1)
        assert len(r.posts) == 2, [p[:40] for p in r.posts]
        assert all("@grok" not in p for p in r.posts)
        assert any("Expansao" in p for p in r.posts), "thread propria sumiu"
        # O bloco do @grok agora cai ANTES, por nao declarar TIPO — a
        # barreira de falha fechado roda primeiro. As duas existem: esta
        # pega o que nao se declara, a de handle pega o que se declara
        # `thread` mentindo sobre o pai.
        assert any("TIPO" in n or "@grok" in n for n in r.notas), r.notas


class TestCadeiaDeRespostas:
    """Os sete blocos que o usuario classificou a mao em 03/09/2026,
    olhando a timeline. O veredito de cada um e dele, nao meu."""

    H = ("perfil_teste",)
    RAIZ = "https://x.com/perfil_teste/status/1000000000000000007"
    TERCEIRO = "https://x.com/streetmanwtf/status/1000000000000000008"

    def _b(self, n, sid, corpo, pai=None, quem="perfil_teste"):
        L = [f"POST {n} (@perfil_teste, 03 Sep 2026):",
             f"URL: https://x.com/perfil_teste/status/{sid}"]
        if pai:
            L.append(f"EM RESPOSTA A (@{quem}, {pai}): texto do pai")
        L.append(corpo)
        return chr(10).join(L)

    def test_o_caso_Vaza_cai_pelo_ID_do_pai(self):
        """"Vaza... furazoio" veio com EM RESPOSTA A (@perfil_teste) --
        o modelo apontou a RAIZ da thread. O pai REAL e o comentario do
        @streetmanwtf, e o ID entrega isso mesmo com o handle mentindo."""
        from src.radar import filtra_respostas
        posts = (self._b(1, "1000000000000000007", "Como eu gosto"),
                 self._b(2, "1000000000000000009", "Vaza... furazoio",
                         pai=self.TERCEIRO))
        ficam, fora = filtra_respostas(posts, self.H)
        assert len(ficam) == 1 and "Como eu gosto" in ficam[0]
        assert len(fora) == 1 and "Vaza" in fora[0][0]

    def test_o_17_thread_propria_FICA(self):
        """"Completando: expansao" responde ao proprio post. E o caso
        que nao pode ser derrubado -- o C25 do gabarito depende dele."""
        from src.radar import filtra_respostas
        posts = (self._b(1, "111", "Tremenda absorcao. Absorcao precede __?"),
                 self._b(2, "222", "Completando: expansao",
                         pai="https://x.com/perfil_teste/status/111"))
        ficam, fora = filtra_respostas(posts, self.H)
        assert len(ficam) == 2, [f[1] for f in fora]

    def test_o_6_cai_pela_CADEIA(self):
        """Ele responde a SI MESMO dentro de uma resposta a terceiro. O
        pai imediato e legitimo; quem entrega e o avo. Sem seguir a
        cadeia, este passava -- e o usuario disse que nao era para
        chegar onde chegou."""
        from src.radar import filtra_respostas
        posts = (self._b(1, "9001", "resposta a terceiro", pai=self.TERCEIRO),
                 self._b(2, "9002", "*vem depois...",
                         pai="https://x.com/perfil_teste/status/9001"))
        ficam, fora = filtra_respostas(posts, self.H)
        assert ficam == (), [f[1] for f in fora]
        assert any("cadeia" in m for _, m in fora)

    def test_post_puro_e_quote_ficam(self):
        from src.radar import filtra_respostas
        posts = (self._b(1, "111", "post puro"),
                 chr(10).join(["POST 2 (@perfil_teste, 03 Sep 2026):",
                           "URL: https://x.com/perfil_teste/status/222",
                           "CITANDO (@outro): pergunta dele",
                           "comentario proprio"]))
        ficam, _ = filtra_respostas(posts, self.H)
        assert len(ficam) == 2

    def test_sem_link_de_pai_cai_no_handle(self):
        """Terceira camada: modelo antigo, sem link. O handle decide."""
        from src.radar import filtra_respostas
        posts = (chr(10).join(["POST 1 (@perfil_teste, 03 Sep 2026):",
                           "URL: https://x.com/perfil_teste/status/1",
                           "EM RESPOSTA A (@grok): explica",
                           "nem o grok deu conta"]),)
        ficam, fora = filtra_respostas(posts, self.H)
        assert ficam == () and fora[0][1] == "@grok"


class TestDedupNaRodada:
    """134 blocos para 88 IDs numa janela de 9 dias (03/09/2026). O
    estado 'ja entregue' dedupe ENTRE rodadas e `--reenviar` o desliga;
    DENTRO da rodada nao havia nada."""

    def _b(self, n, sid, corpo):
        return chr(10).join([f"POST {n} (@perfil_teste, 03 Sep 2026):",
                             f"URL: https://x.com/perfil_teste/status/{sid}",
                             corpo])

    def test_mesmo_id_conta_uma_vez(self):
        from src.radar import dedup_por_status
        posts = (self._b(1, "111", "a"), self._b(7, "111", "a"),
                 self._b(2, "222", "b"))
        ficam, caidos = dedup_por_status(posts)
        assert len(ficam) == 2 and caidos == 1

    def test_bloco_sem_url_nao_e_descartado(self):
        """Sem URL nao ha identidade. Descartar perderia post por falta
        de metadado -- o erro caro e o falso negativo de cobertura."""
        from src.radar import dedup_por_status
        posts = ("POST 1 (@x, 03 Sep 2026):" + chr(10) + "sem url",
                 "POST 2 (@x, 03 Sep 2026):" + chr(10) + "tambem sem url")
        ficam, caidos = dedup_por_status(posts)
        assert len(ficam) == 2 and caidos == 0

    def test_ids_diferentes_todos_ficam(self):
        from src.radar import dedup_por_status
        posts = tuple(self._b(i, str(i), "x") for i in range(1, 6))
        ficam, caidos = dedup_por_status(posts)
        assert len(ficam) == 5 and caidos == 0


class TestFalhaFechado:
    """O primeiro post de um boletim ENTREGUE em 03/09/2026 era
    "emoji emoji emoji", resposta a um terceiro, e passou: o modelo nao
    emitiu a linha EM RESPOSTA A e o filtro nao tinha o que ler.

    Filtro que depende de rotulo OPCIONAL falha aberto. Agora o prompt
    exige TIPO em todo bloco e so passa quem declara post ou quote."""

    def _b(self, *linhas):
        return chr(10).join(["POST 1 (@perfil_teste, 03 Sep 2026):",
                             "URL: https://x.com/perfil_teste/status/1",
                             *linhas])

    def test_o_caso_do_emoji_cai(self):
        from src.radar import declara_post_proprio
        assert not declara_post_proprio(self._b("emoji emoji emoji"))

    def test_positivo_post_declarado_fica(self):
        from src.radar import declara_post_proprio
        assert declara_post_proprio(self._b("TIPO: post", "A Selic esta em 15%"))

    def test_positivo_quote_declarado_fica(self):
        from src.radar import declara_post_proprio
        assert declara_post_proprio(
            self._b("TIPO: quote", "CITANDO (@outro): x", "comentario"))

    def test_resposta_a_terceiro_declarada_cai(self):
        from src.radar import declara_post_proprio
        assert not declara_post_proprio(self._b("TIPO: resposta", "nope"))

    def test_positivo_thread_propria_declarada_FICA(self):
        """O caso vizinho que nao pode quebrar: o C25 do gabarito e uma
        continuacao de thread propria, e a premissa dele so faz sentido
        com o post anterior. Um TIPO de tres valores a derrubaria junto
        com a resposta a terceiro."""
        from src.radar import declara_post_proprio
        assert declara_post_proprio(
            self._b("TIPO: thread", "E vai ficar assim ate 2027."))

    def test_busca_CONTA_o_descarte_sem_rotulo(self, monkeypatch):
        """Some com aviso, nunca em silencio -- o custo assumido aqui e
        falso negativo de cobertura, e ele tem de ser visivel."""
        from src import radar
        texto = chr(10).join([
            "POST 1 (@perfil_teste, 03 Sep 2026):",
            "URL: https://x.com/perfil_teste/status/1",
            "TIPO: post", "A Selic esta em 15%", "---",
            "POST 2 (@perfil_teste, 03 Sep 2026):",
            "URL: https://x.com/perfil_teste/status/2",
            "emoji sem rotulo", "---"])
        monkeypatch.setattr(radar, "_pede", lambda *a, **k: {
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": texto}]}],
            "usage": {"cost_in_usd_ticks": 0}})
        monkeypatch.setenv("XAI_API_KEY", "x")
        r = radar.busca(("perfil_teste",), 1)
        assert len(r.posts) == 1
        assert "Selic" in r.posts[0]
        assert any("sem declaração TIPO" in n for n in r.notas)
