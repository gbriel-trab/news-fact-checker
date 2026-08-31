"""As partes do radar que não dependem de rede: prompt, parse e links.

A regra que mais importa aqui foi medida em 30/08/2026: o modelo NÃO vê o
filtro `allowed_x_handles` — só o prompt direciona. Um prompt que não nomeia
os handles devolve "quais handles?" e paga a chamada mesmo assim.
"""

from src.radar import _corpo, _handles_de, _links_de, _posts_de, _prompt


class TestPrompt:
    def test_nomeia_todos_os_handles(self):
        texto = _prompt(("mentalhedgebr", "outro_perfil"), 2)
        assert "@mentalhedgebr" in texto
        assert "@outro_perfil" in texto

    def test_pede_transcricao_integral(self):
        assert "ÍNTEGRA" in _prompt(("a",), 2)

    def test_corpo_carrega_filtro_e_janela(self):
        corpo = _corpo(("mentalhedgebr",), 3)
        ferramenta = corpo["tools"][0]
        assert ferramenta["type"] == "x_search"
        assert ferramenta["allowed_x_handles"] == ["mentalhedgebr"]
        assert ferramenta["from_date"] < ferramenta["to_date"]


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
