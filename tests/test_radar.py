"""As partes do radar que não dependem de rede: prompt, parse e links.

A regra que mais importa aqui foi medida em 30/08/2026: o modelo NÃO vê o
filtro `allowed_x_handles` — só o prompt direciona. Um prompt que não nomeia
os handles devolve "quais handles?" e paga a chamada mesmo assim.
"""

from src.radar import (_corpo, _handles_de, _links_de, _posts_de, _prompt,
                       id_status, url_do_post)


class TestPrompt:
    def test_nomeia_todos_os_handles(self):
        texto = _prompt(("mentalhedgebr", "outro_perfil"), 2)
        assert "@mentalhedgebr" in texto
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

    def test_corpo_carrega_filtro_e_janela(self):
        from datetime import datetime, timedelta, timezone
        hoje = datetime.now(timezone.utc).date()
        corpo = _corpo(("mentalhedgebr",), 3)
        ferramenta = corpo["tools"][0]
        assert ferramenta["type"] == "x_search"
        assert ferramenta["allowed_x_handles"] == ["mentalhedgebr"]
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
        assert _handles_de("@mentalhedgebr, outro") == ("mentalhedgebr",
                                                        "outro")

    def test_arroba_sozinho_cai_fora(self):
        # '@' sobrevivia ao filtro antigo e disparava busca paga com
        # handle vazio — a normalização vem ANTES do filtro.
        assert _handles_de("@") == ()
        assert _handles_de("@,mentalhedgebr") == ("mentalhedgebr",)

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

    def test_bloco_sem_linhas_novas_passa_intacto(self):
        from src.radar import para_separacao
        bloco = "POST 1 (@x, data):\ntexto simples"
        assert para_separacao(bloco) == bloco


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
