"""As partes do radar que não dependem de rede: bloco, barreiras e custo.

A fonte é a API oficial do X, e `x_api.posts_de`
é mockado em todo teste que chega perto dela. O que estes testes protegem não
é a fonte: é o FORMATO DO BLOCO, que quatro consumidores reparseiam.

Nenhum teste toca a rede, e isso é BARRADO e não combinado: ver
`sem_rede_sem_token_do_dono` abaixo.
"""

import socket

import pytest

from src import radar
from src.radar import _handles_de, id_status, url_do_post


# ------------------------------------------------------------- fixtures


def _proibido(*args, **kwargs):
    raise AssertionError("o teste tentou falar com a rede")


class _SocketProibido:
    """Barra o socket cru, que é a rede que não passa por `requests`."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("o teste tentou abrir um socket")


@pytest.fixture(autouse=True)
def sem_rede_sem_token_do_dono(monkeypatch):
    """As amarras que `test_x_api.py:58` e `test_x_auth.py:63` já tinham e
    este arquivo não.

    Não é zelo: MEDIDO em 04/09/2026, numa auditoria por mutação. Um teste
    desta suíte desceu até `requests.post` DE VERDADE e gastou três
    tentativas contra a internet — a mutação estava errada e a suíte ficou
    verde de todo modo, porque quem respondeu foi a rede e não o mock.
    Teste que depende do que há do outro lado do cabo não mede código.

    Patch-se `radar.x_api.requests` porque o radar não importa `requests`:
    a única porta de rede alcançável daqui é a do `x_api`, e o objeto de
    módulo é o MESMO que o `x_auth` importa — uma substituição cobre as
    duas.

    `token` entra junto porque `x_auth.token()` lê `data/x_token.json` — o
    arquivo do dono, com o refresh vivo dentro. Nenhum teste daqui precisa
    dele (todos mockam `x_api.posts_de`), e uma mutação que chame `x_api`
    por baixo do mock não pode acabar lendo credencial de verdade."""
    monkeypatch.setattr(radar.x_api.requests, "post", _proibido)
    monkeypatch.setattr(radar.x_api.requests, "get", _proibido)
    monkeypatch.setattr(socket, "socket", _SocketProibido)
    monkeypatch.setattr(radar.x_api, "token", lambda: "token-de-mentira")


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


class TestParaSeparacao:
    def test_reatribui_o_interlocutor_e_tira_a_url(self):
        from src.radar import para_separacao
        bloco = ("POST 1 (@x, 01 Sep 2026):\n"
                 "URL: https://x.com/x/status/123\n"
                 "EM RESPOSTA A (@terceiro): o índice subiu 40% no ano\n"
                 "Então falta muito?")
        saida = para_separacao(bloco)
        assert "URL:" not in saida
        assert "palavras do interlocutor, não do autor" in saida
        assert "(@terceiro): o índice subiu 40% no ano" in saida
        assert "Então falta muito?" in saida

    def test_thread_propria_nao_vira_interlocutor(self):
        # Com resposta a terceiros excluída da captura, EM RESPOSTA A
        # passa a apontar o post anterior da PRÓPRIA thread — palavras do
        # mesmo autor. Rotulá-las de "interlocutor" poria a premissa
        # legítima (ex.: update de posição em thread) sob suspeita.
        from src.radar import para_separacao
        bloco = ("POST 1 (@perfil_teste, 01 Sep 2026):\n"
                 "EM RESPOSTA A (@perfil_teste): texto do post anterior "
                 "da própria thread\n"
                 "corpo do autor neste bloco CORPO-T77")
        saida = para_separacao(bloco)
        assert "post anterior do próprio autor na thread" in saida
        assert "interlocutor" not in saida
        assert "CORPO-T77" in saida

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
        # x.com/i/status/N e x.com/handle/status/N são o mesmo status — o
        # ID é o que identifica.
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


class TestRespostaATerceiro:
    """A barreira do handle, e ela entra com os POSITIVOS pareados.

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
            "EM RESPOSTA A (@terceiro): explica ai\ncorpo do autor neste "
            "bloco") == "@terceiro"

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


class TestCadeiaDeRespostas:
    """Casos de cadeia classificados a mao pelo dono do projeto, olhando a
    timeline. O veredito de cada um e dele."""

    H = ("perfil_teste",)
    TERCEIRO = "https://x.com/terceiro/status/9000"

    def _b(self, n, sid, corpo, pai=None, quem="perfil_teste"):
        L = [f"POST {n} (@perfil_teste, 03 Sep 2026):",
             f"URL: https://x.com/perfil_teste/status/{sid}"]
        if pai:
            L.append(f"EM RESPOSTA A (@{quem}, {pai}): texto do pai")
        L.append(corpo)
        return chr(10).join(L)

    def test_pai_de_outra_conta_cai_pelo_ID_do_pai(self):
        """Bloco cujo EM RESPOSTA A traz o handle do proprio autor, mas cujo
        LINK de pai aponta para o status de outra conta: o ID entrega o que
        o handle esconde."""
        from src.radar import filtra_respostas
        posts = (self._b(1, "111", "corpo da raiz TOKEN-R1"),
                 self._b(2, "222", "corpo filho TOKEN-R2",
                         pai=self.TERCEIRO))
        ficam, fora = filtra_respostas(posts, self.H)
        assert len(ficam) == 1 and "TOKEN-R1" in ficam[0]
        assert len(fora) == 1 and "TOKEN-R2" in fora[0][0]

    def test_thread_propria_FICA(self):
        """O autor respondendo ao proprio post. E o caso que nao pode ser
        derrubado -- o C25 do gabarito depende dele."""
        from src.radar import filtra_respostas
        posts = (self._b(1, "111", "corpo do post raiz da thread"),
                 self._b(2, "222", "corpo da continuacao da thread",
                         pai="https://x.com/perfil_teste/status/111"))
        ficam, fora = filtra_respostas(posts, self.H)
        assert len(ficam) == 2, [f[1] for f in fora]

    def test_auto_resposta_pendurada_em_resposta_a_terceiro_cai_pela_CADEIA(
            self):
        """Ele responde a SI MESMO dentro de uma resposta a terceiro. O
        pai imediato e legitimo; quem entrega e o avo. Sem seguir a
        cadeia, este passaria."""
        from src.radar import filtra_respostas
        posts = (self._b(1, "9001", "resposta a terceiro", pai=self.TERCEIRO),
                 self._b(2, "9002", "corpo da auto-resposta na cadeia",
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
        """Terceira camada: bloco sem link de pai. O handle decide."""
        from src.radar import filtra_respostas
        posts = (chr(10).join(["POST 1 (@perfil_teste, 03 Sep 2026):",
                           "URL: https://x.com/perfil_teste/status/1",
                           "EM RESPOSTA A (@terceiro): explica",
                           "corpo do autor neste bloco"]),)
        ficam, fora = filtra_respostas(posts, self.H)
        assert ficam == () and fora[0][1] == "@terceiro"


class TestDedupNaRodada:
    """Um post por status ID DENTRO da rodada. O estado 'ja entregue'
    dedupe ENTRE rodadas e `--reenviar` o desliga; dentro da rodada e
    este filtro."""

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
    """`declara_post_proprio` falha FECHADO: bloco que nao declara TIPO
    cai, e so passa quem declara post, thread ou quote.

    Filtro que depende de rotulo OPCIONAL falha aberto — um bloco sem a
    linha EM RESPOSTA A passaria como proprio sem ninguem ter afirmado
    isso."""

    def _b(self, *linhas):
        return chr(10).join(["POST 1 (@perfil_teste, 03 Sep 2026):",
                             "URL: https://x.com/perfil_teste/status/1",
                             *linhas])

    def test_bloco_sem_TIPO_cai(self):
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


# --- O formato do bloco é o contrato dos quatro consumidores --------------
#
# O que esta bateria protege NÃO é a fonte: é o FORMATO DO BLOCO. Ele vazou
# do radar e quatro consumidores o reparseiam — `boletim._chaves_do_post`
# (-> `radar.url_do_post`), `boletim._confere_post`
# (-> `radar.para_separacao`), `boletim._formata_telegram` (o parêntese do
# cabeçalho e os prefixos URL:/EM RESPOSTA A/CITANDO) e
# `tests/test_gabarito.py`. A forma vai DECLARADA linha a linha em `_forma`
# — contrato escrito, que é preciso manter à mão.
#
# Nada aqui abre socket: `x_api.posts_de` é mockado.

def _post_x(ident, texto, tipo="post", quando="2026-09-03T17:50:11Z",
            pai_id="", pai_autor="", autor="perfil_teste"):
    """Um `x_api.Post` sintético, como o cliente da API o entregaria."""
    from src.x_api import Post
    return Post(id=ident, autor=autor, criado_em=quando, texto=texto,
                tipo=tipo, pai_id=pai_id, pai_autor=pai_autor,
                url=f"https://x.com/{autor}/status/{ident}" if ident else "")


def _forma(bloco):
    """A ESTRUTURA do bloco, sem o conteúdo.

    Cada linha vira o rótulo que os consumidores reparseiam, na ordem em que
    aparece. Comparar formas, e não textos, é o que faz o teste falar sobre
    FORMATO: conteúdo igual entre duas fontes é coincidência do caso montado;
    forma igual é o contrato."""
    import re as _re
    forma = []
    for i, linha in enumerate(bloco.splitlines()):
        alta = linha.upper()
        if i == 0:
            forma.append("CABEÇALHO" if _re.match(
                r"^POST \d+ \(@\w+, .+\):$", linha) else "CABEÇALHO QUEBRADO")
        elif alta.startswith("URL:"):
            forma.append("URL")
        elif alta.startswith("TIPO:"):
            forma.append("TIPO")
        elif alta.startswith("EM RESPOSTA A"):
            forma.append("EM RESPOSTA A")
        elif alta.startswith("CITANDO"):
            forma.append("CITANDO")
        else:
            forma.append("corpo")
    return tuple(forma)


class TestApiOficialDoX:
    """A fonte, e o motivo dela: o TIPO não é rótulo pedido a um modelo, é
    metadado calculado em `x_api.classifica`. Estes testes exigem que isso
    chegue ao bloco SEM mexer no formato dele."""

    H = ("perfil_teste",)

    # Raiz, continuação de thread e citação do próprio post: é o caso
    # dominante do acervo.
    POSTS_API = (
        _post_x("111", "A Selic esta em 15% TOKEN-A",
                quando="2026-09-03T17:50:11Z"),
        _post_x("222", "E vai ficar assim ate 2027 TOKEN-B", tipo="thread",
                quando="2026-09-03T18:00:00Z", pai_id="111",
                pai_autor="perfil_teste"),
        _post_x("333", "Comentario por cima TOKEN-C", tipo="citacao",
                quando="2026-09-03T19:00:00Z", pai_id="111"),
    )

    def _liga(self, monkeypatch, por_handle):
        """Mocka `x_api.posts_de`. Devolve a lista de pedidos feitos.

        `por_handle` mapeia handle -> lista de Post OU uma exceção a
        levantar."""
        from src import radar
        pedidos = []

        def falso(handle, desde, ate="", limite=100):
            pedidos.append({"handle": handle, "desde": desde, "ate": ate,
                            "limite": limite})
            achado = por_handle.get(handle, [])
            if isinstance(achado, Exception):
                raise achado
            return list(achado)

        monkeypatch.setattr(radar.x_api, "posts_de", falso)
        return pedidos

    # --- 1. O formato do bloco ---------------------------------------------

    def test_a_FORMA_do_bloco_e_o_contrato_dos_quatro_consumidores(
            self, monkeypatch):
        """Cada linha, na ordem, com o rótulo que os consumidores
        reparseiam.

        A forma vai DECLARADA aqui — é preciso mantê-la à mão —, e é dela
        que quatro arquivos dependem."""
        from src import radar
        self._liga(monkeypatch, {"perfil_teste": self.POSTS_API})
        nova = radar.busca(self.H, 2).posts

        assert _forma(nova[0]) == ("CABEÇALHO", "URL", "TIPO", "corpo")
        assert _forma(nova[1]) == ("CABEÇALHO", "URL", "TIPO",
                                   "EM RESPOSTA A", "corpo")
        assert _forma(nova[2]) == ("CABEÇALHO", "URL", "TIPO",
                                   "CITANDO", "corpo")

    def test_para_separacao_le_o_bloco_da_api(self, monkeypatch):
        """Consumidor de `boletim.py:183`. A reatribuição de autoria tem de
        continuar funcionando: thread própria é contexto do PRÓPRIO autor, e o
        citado é de quem ele cita."""
        from src import radar
        self._liga(monkeypatch, {"perfil_teste": self.POSTS_API})
        posts = radar.busca(self.H, 2).posts

        thread = radar.para_separacao(posts[1])
        assert "URL:" not in thread
        assert "post anterior do próprio autor na thread" in thread
        assert "interlocutor" not in thread
        assert "TOKEN-B" in thread

        citacao = radar.para_separacao(posts[2])
        assert "as afirmações são de quem ele cita" in citacao
        assert "TOKEN-C" in citacao

    def test_chaves_do_post_do_boletim_acham_a_url(self, monkeypatch):
        """Consumidor de `boletim.py:95`. A chave forte de dedup ENTRE rodadas
        é 'url:<id do status>', e ela só existe se a linha URL: do bloco
        conferir com os links da rodada."""
        from src import radar
        from src.boletim import _chaves_do_post
        self._liga(monkeypatch, {"perfil_teste": self.POSTS_API})
        rodada = radar.busca(self.H, 2)

        assert "url:111" in _chaves_do_post(rodada.posts[0], rodada.links)
        assert "url:333" in _chaves_do_post(rodada.posts[2], rodada.links)

    def test_o_telegram_extrai_handle_e_data_do_cabecalho(self, monkeypatch):
        """Consumidor de `boletim.py:529-548`, o mais frágil dos quatro: ele
        joga fora a linha-cabeçalho e guarda só o parêntese, por
        `re.search(r"\\(([^)]+)\\)", cabecalho)`. Cabeçalho sem parêntese
        apagaria handle e data do que chega ao bolso do dono."""
        from src import radar
        from src.boletim import _formata_telegram
        self._liga(monkeypatch, {"perfil_teste": self.POSTS_API})
        rodada = radar.busca(self.H, 2)

        vazio = {"nao_verificaveis": [], "checks": [], "contextos": [],
                 "sem_premissas": False}
        html = _formata_telegram(
            "@perfil_teste", "03/09",
            [(i, p, dict(vazio), None)
             for i, p in enumerate(rodada.posts, 1)],
            [], [], 0.10, 0.03)

        assert ("<i>(@perfil_teste, Thu, 03 Sep 2026 17:50:11 GMT)</i>"
                in html)
        # URL:, EM RESPOSTA A e CITANDO são reconhecidos pelo prefixo e saem
        # do corpo — a linha de contexto vira [CONTEXTO] / [CITANDO].
        assert "URL:" not in html
        assert "<code>[CONTEXTO]</code>" in html
        assert "<code>[CITANDO]</code>" in html

    def test_texto_do_autor_com_separador_sai_INTEIRO(self, monkeypatch):
        """Por que o bloco NÃO volta por um parser do formato.

        Um parser que partisse no primeiro `---` de QUALQUER lugar do bloco
        e cortasse em `^POST \\d+` perderia a segunda metade de um post do
        autor, em silêncio — o texto é literal dele, e ele pode escrever os
        dois. Reparsear o bloco para "garantir o formato" criaria exatamente
        esse caminho de perda, e é por isso que esse parser não existe."""
        from src import radar
        armadilha = ("primeira metade TOKEN-D\n---\n"
                     "POST 9 (@fake, x):\nsegunda metade TOKEN-E")
        self._liga(monkeypatch,
                   {"perfil_teste": (_post_x("111", armadilha),)})
        posts = radar.busca(self.H, 2).posts

        assert len(posts) == 1
        assert "TOKEN-D" in posts[0] and "TOKEN-E" in posts[0]

    # --- 2. O TIPO vem do metadado, não de rótulo pedido -------------------

    def test_o_tipo_vem_do_METADADO(self):
        """Em dois blocos.

        `Post.tipo` é calculado por `x_api.classifica` a partir de
        `in_reply_to_user_id` contra `author_id` — comparação de id feita no
        servidor. Um `tipo="resposta"` produz bloco que as barreiras
        descartam; um `tipo="thread"` produz bloco que SOBREVIVE."""
        from src.radar import _bloco, declara_post_proprio, filtra_respostas
        raiz = _post_x("111", "A Selic esta em 15%")
        thread = _post_x("222", "E vai ficar assim TOKEN-T", tipo="thread",
                         pai_id="111", pai_autor="perfil_teste")
        resposta = _post_x("900", "explica ai TOKEN-R", tipo="resposta",
                           pai_id="777", pai_autor="terceiro")
        indice = {"111": raiz, "222": thread}

        b_raiz = _bloco(1, raiz, indice)
        b_thread = _bloco(2, thread, indice)
        b_resposta = _bloco(3, resposta, indice)

        assert "TIPO: thread" in b_thread
        assert "TIPO: resposta" in b_resposta

        # Barreira do rótulo (falha FECHADO): `resposta` cai, `thread` fica.
        assert declara_post_proprio(b_thread)
        assert not declara_post_proprio(b_resposta)

        # E `filtra_respostas` chega ao mesmo veredito pelo ID do pai: o pai
        # da thread é post desta rodada; o da resposta, não.
        ficam, fora = filtra_respostas((b_raiz, b_thread, b_resposta), self.H)
        assert len(ficam) == 2 and "TOKEN-T" in ficam[1]
        assert len(fora) == 1 and "TOKEN-R" in fora[0][0]

    def test_resposta_a_terceiro_nao_chega_ao_boletim(self, monkeypatch):
        """O mesmo, ponta a ponta, e com a contagem: o descarte aparece."""
        from src import radar
        self._liga(monkeypatch, {"perfil_teste": (
            _post_x("111", "A Selic esta em 15% TOKEN-P"),
            _post_x("900", "explica ai TOKEN-R", tipo="resposta",
                    pai_id="777", pai_autor="terceiro"))})
        r = radar.busca(self.H, 2)

        assert len(r.posts) == 1 and "TOKEN-P" in r.posts[0]
        assert any("TIPO: resposta" in n for n in r.notas), r.notas

    def test_thread_com_o_pai_FORA_da_janela_sobrevive(self, monkeypatch):
        """O caso vizinho que não pode quebrar, e a razão de o link do pai
        ficar de fora quando ele não foi lido: `filtra_respostas` camada 1 lê
        "pai que não está nesta rodada" como resposta a terceiro. O servidor
        já respondeu por user id que thread é o autor continuando a si
        mesmo. Com o link, a camada 1 contradiria o servidor e derrubaria
        thread própria em série (janela de 2 dias, thread começada há
        três)."""
        from src import radar
        self._liga(monkeypatch, {"perfil_teste": (
            _post_x("222", "continuacao TOKEN-C2", tipo="thread",
                    pai_id="000-fora-da-janela", pai_autor="perfil_teste"),)})
        r = radar.busca(self.H, 2)

        assert len(r.posts) == 1 and "TOKEN-C2" in r.posts[0]
        assert "EM RESPOSTA A" not in r.posts[0]
        assert any("FORA da janela" in n for n in r.notas), r.notas

    # --- 3. Retweet: descartado e CONTADO ----------------------------------

    def test_retweet_cai_e_a_nota_CONTA_o_descarte(self, monkeypatch):
        """Descarte silencioso é o que esconde defeito. O retweet não traz
        palavra do autor e o formato do bloco não tem rótulo para ele — mas
        ele foi LIDO e PAGO, então some com aviso."""
        from src import radar
        self._liga(monkeypatch, {"perfil_teste": (
            _post_x("111", "A Selic esta em 15% TOKEN-P"),
            _post_x("777", "texto de terceiro", tipo="retweet",
                    pai_id="555"))})
        r = radar.busca(self.H, 2)

        assert len(r.posts) == 1 and "TOKEN-P" in r.posts[0]
        assert any("retweet" in n for n in r.notas), r.notas
        assert any("1 retweet" in n for n in r.notas), r.notas

    def test_tipo_fora_do_vocabulario_sai_com_o_NOME_dele(self, monkeypatch):
        """Contado à parte, e não somado aos retweets: se `Post.tipo` crescer
        um valor, ele aparece com o nome em vez de virar "retweet"."""
        from src import radar
        self._liga(monkeypatch, {"perfil_teste": (
            _post_x("111", "A Selic esta em 15% TOKEN-P"),
            _post_x("888", "coisa nova", tipo="transmissao"))})
        r = radar.busca(self.H, 2)

        assert len(r.posts) == 1
        assert any("transmissao" in n for n in r.notas), r.notas

    def test_a_janela_e_um_INSTANTE_nao_uma_data(self, monkeypatch):
        """`start_time` tem hora e o fim é "agora" por omissão: a rodada vê o
        próprio dia. O `limite` é teto de GASTO, não de janela."""
        from src import radar
        pedidos = self._liga(monkeypatch, {"perfil_teste": ()})
        radar.busca(self.H, 2)

        assert len(pedidos) == 1
        assert pedidos[0]["limite"] == radar.LIMITE_POR_HANDLE
        assert pedidos[0]["ate"] == ""
        assert pedidos[0]["desde"].endswith("Z")
        assert len(pedidos[0]["desde"]) == len("2026-09-02T17:50:11Z")

    # --- 5. A fronteira de exceções ----------------------------------------

    def test_PrecisaAutorizar_vira_FalhaNoRadar_COM_o_comando(self,
                                                              monkeypatch):
        """`boletim.py:370` captura `radar.FalhaNoRadar` e mais nada — outra
        classe subiria como traceback, fora do aviso que chega ao Telegram. E
        quem lê o aviso não está no terminal: o único desfecho possível é um
        humano no navegador, então a mensagem carrega o comando."""
        import pytest
        from src import radar
        from src.x_auth import PrecisaAutorizar
        self._liga(monkeypatch,
                   {"perfil_teste": PrecisaAutorizar("token recusado (401)")})
        with pytest.raises(radar.FalhaNoRadar) as erro:
            radar.busca(self.H, 2)
        assert "src.x_auth" in str(erro.value)
        assert "token recusado (401)" in str(erro.value)

    def test_FalhaNaAPI_em_UM_handle_vira_nota_e_a_rodada_segue(
            self, monkeypatch):
        """Perder a rodada toda por causa de um handle seria trocar uma lacuna
        anunciada por um dia inteiro sem boletim."""
        from src import radar
        from src.x_api import FalhaNaAPI
        self._liga(monkeypatch, {
            "perfil_teste": (_post_x("111", "A Selic esta em 15% TOKEN-P"),),
            "outro": FalhaNaAPI("o X respondeu 503 duas vezes")})
        r = radar.busca(("perfil_teste", "outro"), 2)

        assert len(r.posts) == 1 and "TOKEN-P" in r.posts[0]
        assert any("@outro" in n and "503" in n for n in r.notas), r.notas

    def test_FalhaNaAPI_em_TODOS_sobe_erro(self, monkeypatch):
        """Rodada vazia entregue como sucesso esconderia uma queda total."""
        import pytest
        from src import radar
        from src.x_api import FalhaNaAPI
        self._liga(monkeypatch, {
            "perfil_teste": FalhaNaAPI("503"), "outro": FalhaNaAPI("503")})
        with pytest.raises(radar.FalhaNoRadar, match="TODOS os handles"):
            radar.busca(("perfil_teste", "outro"), 2)

    def test_handle_sem_posts_aparece_na_nota(self, monkeypatch):
        """Contagem nossa, não pedido: handle que não retornou nada é dito
        por nome."""
        from src import radar
        self._liga(monkeypatch, {
            "perfil_teste": (_post_x("111", "A Selic esta em 15%"),),
            "outro": ()})
        r = radar.busca(("perfil_teste", "outro"), 2)
        assert any("@outro" in n and "não retornou nada" in n
                   for n in r.notas), r.notas

    # --- 6. Custo ----------------------------------------------------------

    def test_o_custo_bate_com_x_api_e_CONTA_o_retweet_descartado(
            self, monkeypatch):
        """O retweet não vira bloco, mas já foi PAGO: a API cobra por recurso
        devolvido. Contar só o que sobrou subestimaria a fatura."""
        from src import radar, x_api
        self._liga(monkeypatch, {"perfil_teste": (
            _post_x("111", "A Selic esta em 15%"),
            _post_x("222", "outro post"),
            _post_x("777", "texto de terceiro", tipo="retweet",
                    pai_id="555"))})
        r = radar.busca(self.H, 2)

        assert len(r.posts) == 2
        assert r.custo_usd == x_api.custo_estimado_usd(3)

    def test_o_detalhe_do_custo_diz_ESTIMADO(self, monkeypatch):
        """A regra do dono é custo estimado antes e REAL depois. Por esta via
        o real não existe — o X não devolve preço nenhum. A palavra tem de
        chegar ao rodapé do boletim, não ficar só no comentário do código."""
        from src import radar
        self._liga(monkeypatch,
                   {"perfil_teste": (_post_x("111", "A Selic esta em 15%"),)})
        r = radar.busca(self.H, 2)

        assert "estimado" in r.detalhe_custo.lower()
        assert "teto" in r.detalhe_custo.lower()

    # --- 7. A data do cabeçalho --------------------------------------------

    def test_a_data_sai_em_RFC_2822_GMT_sem_depender_do_locale(self):
        """"Thu, 03 Sep 2026 17:50:11 GMT" é a data RFC 2822 em GMT.
        `format_datetime(usegmt=True)` produz essa forma e, ao contrário de
        `strftime`, não depende do locale: num Windows pt-BR o strftime
        escreveria "qui, 03 set" e o cabeçalho mudaria de formato sem
        ninguém pedir."""
        from src.radar import _data_do_cabecalho
        assert (_data_do_cabecalho("2026-09-03T17:50:11Z")
                == "Thu, 03 Sep 2026 17:50:11 GMT")
        # Com fuso explícito, normalizado para UTC.
        assert (_data_do_cabecalho("2026-09-03T14:50:11-03:00")
                == "Thu, 03 Sep 2026 17:50:11 GMT")
        # Sem fuso, assumido UTC: a API manda com Z, mas ler isto como hora
        # local mudaria a data do cabeçalho conforme a máquina.
        assert (_data_do_cabecalho("2026-09-03T17:50:11")
                == "Thu, 03 Sep 2026 17:50:11 GMT")

    def test_data_ausente_ou_ilegivel_NAO_apaga_o_parentese(self):
        """O cabeçalho tem de manter o parêntese (handle, data) porque
        `boletim._formata_telegram` extrai esse parêntese para montar a linha
        do Telegram. Data ilegível volta como veio; ausente vira "sem data"."""
        from src.radar import _bloco, _data_do_cabecalho
        assert _data_do_cabecalho("") == "sem data"
        assert _data_do_cabecalho("ontem à tarde") == "ontem à tarde"
        bloco = _bloco(1, _post_x("111", "corpo", quando=""), {})
        assert bloco.startswith("POST 1 (@perfil_teste, sem data):")
        assert _forma(bloco)[0] == "CABEÇALHO"

    def test_o_parser_que_ja_existe_le_a_data_do_bloco_da_api(self,
                                                              monkeypatch):
        """A prova pedida: a data não é conferida contra uma descrição do
        formato, e sim passando o bloco pelo parser que o boletim já usa."""
        from src import radar
        from src.boletim import _formata_telegram
        self._liga(monkeypatch, {"perfil_teste": (
            _post_x("111", "A Selic esta em 15%",
                    quando="2026-09-02T18:16:54Z"),)})
        rodada = radar.busca(self.H, 2)

        html = _formata_telegram(
            "@perfil_teste", "03/09",
            [(1, rodada.posts[0], {"nao_verificaveis": [], "checks": [],
                                   "contextos": [], "sem_premissas": False},
              None)], [], [], 0.10, 0.03)
        assert ("<i>(@perfil_teste, Wed, 02 Sep 2026 18:16:54 GMT)</i>"
                in html)

    # --- 8. O referenciado fora da janela: nada de linha seca --------------

    def test_CITANDO_com_o_citado_FORA_da_janela_nao_sai_seca(self):
        """`CITANDO (@sigel):` e acabou a linha não informa nada.

        A linha existe para carregar o TEXTO do citado. Com o citado fora da
        janela lida `lidos` não tem esse texto, e a linha vazia não é só
        ruído: `para_separacao` a transforma em "(contexto — post citado
        pelo autor; as afirmações são de quem ele cita: (...):)" e manda
        isso para um separador que se paga por token. O ramo `thread` já
        decidia ("a linha só entra se CARREGAR algo"); o ramo `citacao` dava
        append sempre.

        Como a API entrega, e é por isso que não sobra nem handle:
        `x_api.classifica` devolve `pai_autor` VAZIO para toda citação — o
        autor do citado só viria expandindo `referenced_*.id`, que é outra
        cobrança. Fora da janela restam o id e um link montado dele, e o
        formato congelado (`CITANDO (@autor): <texto>`) não tem casa para
        link nenhum.

        O teste proíbe a forma vazia — marca, parêntese, dois-pontos e nada
        — e não escolhe COMO: a linha some, ou passa a carregar algo. O que
        ele fixa junto é que o POST não some com ela."""
        import re as _re
        from src.radar import _bloco
        bloco = _bloco(1, _post_x("333", "Comentario por cima TOKEN-C",
                                  tipo="citacao", pai_id="999"), {})

        secas = [linha for linha in bloco.splitlines()
                 if _re.match(r"^\s*(CITANDO|EM RESPOSTA A)\s*\([^)]*\)\s*:"
                              r"\s*$", linha, _re.IGNORECASE)]
        assert not secas, f"linha seca no bloco: {secas}"
        # Some a linha, não o bloco: o texto do autor é o que o boletim quer.
        assert "TOKEN-C" in bloco
        assert _forma(bloco) == ("CABEÇALHO", "URL", "TIPO", "corpo")

    def test_CITANDO_com_o_citado_DENTRO_da_janela_continua_saindo(self):
        """O controle do par: no caso dominante do acervo (autocitação) o
        citado está na mesma rodada, a linha carrega o texto dele e tem de
        continuar saindo. Sem este controle, a guarda passaria apagando a
        linha sempre."""
        from src.radar import _bloco
        raiz = _post_x("111", "A Selic esta em 15% TOKEN-RAIZ")
        bloco = _bloco(1, _post_x("333", "Comentario por cima TOKEN-C",
                                  tipo="citacao", pai_id="111"),
                       {"111": raiz})

        assert "CITANDO (@perfil_teste): A Selic esta em 15% TOKEN-RAIZ" \
            in bloco, bloco
        assert _forma(bloco) == ("CABEÇALHO", "URL", "TIPO", "CITANDO",
                                 "corpo")

    def test_CITANDO_sem_id_do_citado_NAO_deixa_a_marca_no_bloco(self):
        """Sem id não há nem link: a linha não tem nada para carregar.

        Ramo defensivo — não há caso medido de `referenced_*` sem `id`, e a
        doc do X não confirma que seja impossível. O que o teste fixa é que
        o bloco sai limpo em vez de sair com meia marca, que os quatro
        consumidores do formato reparseiam."""
        from src.radar import _bloco
        bloco = _bloco(1, _post_x("333", "Comentario por cima TOKEN-C",
                                  tipo="citacao", pai_id=""), {})

        assert "CITANDO" not in bloco.upper(), bloco
        assert _forma(bloco) == ("CABEÇALHO", "URL", "TIPO", "corpo")

    def test_a_citacao_sem_id_do_citado_SOME_COM_AVISO(self, monkeypatch):
        """E a nota dela é contada à PARTE da nota da janela: a causa é
        outra (metadado incompleto, não janela curta), e o dono que lê o
        Telegram decide coisas diferentes nos dois casos — num vale
        alargar a janela, no outro não adianta."""
        from src import radar
        self._liga(monkeypatch, {"perfil_teste": (
            _post_x("333", "Comentario por cima TOKEN-C", tipo="citacao",
                    pai_id=""),)})
        r = radar.busca(self.H, 2)

        assert len(r.posts) == 1 and "TOKEN-C" in r.posts[0]
        notas = [n for n in r.notas if "sem o id do post citado" in n]
        assert len(notas) == 1, r.notas
        assert notas[0].startswith("1 citação(ões)"), notas[0]
        assert "SEM linha CITANDO" in notas[0], notas[0]
        # E NÃO entra na conta da janela: sem id, o `pai_id not in indice`
        # nem chega a ser perguntado.
        assert not any("FORA da janela" in n for n in r.notas), r.notas

    def test_a_nota_do_referenciado_fora_da_janela_diz_QUANTOS_e_POR_QUE(
            self, monkeypatch):
        """A nota é conferida pelo TEXTO, não pela existência.

        Medido em 04/09/2026, por mutação: trocar o NÚMERO desta nota não
        derrubava teste nenhum, porque todo mundo só perguntava
        `any("FORA da janela" in n)`. A nota vai para o Telegram — número
        errado ali é o dono lendo "1 bloco" e não indo procurar os outros.

        Os três blocos fora da janela são DOIS de thread e UM de citação, de
        propósito: 3 não é 2 nem 1, então um contador que passe a olhar só
        um dos tipos cai aqui. E a autocitação, com o citado lido nesta
        rodada, fica de fora da conta."""
        from src import radar
        self._liga(monkeypatch, {"perfil_teste": (
            _post_x("111", "A Selic esta em 15% TOKEN-RAIZ"),
            _post_x("222", "continuacao TOKEN-T1", tipo="thread",
                    pai_id="555", pai_autor="perfil_teste"),
            _post_x("333", "outra continuacao TOKEN-T2", tipo="thread",
                    pai_id="666", pai_autor="perfil_teste"),
            _post_x("444", "comentario por cima TOKEN-CIT", tipo="citacao",
                    pai_id="777"),
            _post_x("999", "autocitacao TOKEN-AUTO", tipo="citacao",
                    pai_id="111"))})
        r = radar.busca(self.H, 2)

        assert len(r.posts) == 5, r.posts
        notas = [n for n in r.notas if "FORA da janela" in n]
        assert len(notas) == 1, r.notas
        # O NÚMERO, que é o que a mutação trocava impunemente.
        assert notas[0].startswith("3 bloco(s)"), notas[0]
        # E o TEXTO. A nota dizia "a linha de contexto sai sem o texto
        # citado", e isso era falso: nos dois tipos a linha NÃO SAI. Nota
        # que descreve errado o que aconteceu é informação errada para o
        # dono, que é quem a lê no Telegram.
        assert "post referenciado FORA da janela lida" in notas[0]
        assert "SEM a linha de contexto" in notas[0], notas[0]
        assert "cobrança" in notas[0], notas[0]
        # E o que a nota descreve tem de bater com os blocos: nenhum dos
        # três traz marca de contexto, e a autocitação traz.
        assert sum(1 for p in r.posts
                   if "CITANDO" in p or "EM RESPOSTA A" in p) == 1, r.posts

    # --- 9. O link do post que não foi lido --------------------------------

    def test_o_link_do_referenciado_tem_o_i_que_dispensa_o_handle(self):
        """`_url_de_status` tem uma garantia só, e é o segmento `/i/`.

        Ele é o que permite montar o link sem saber o handle do autor —
        `x.com/status/N`, sem ele, põe a palavra `status` na posição do
        perfil e não abre post nenhum. Importa porque este link SAI DAQUI
        para o bloco, e o bloco vai para o Telegram: link quebrado ali é o
        dono clicando em nada.

        Não conferido contra o servidor do X: a forma sem `/i/` nunca foi
        aberta."""
        from src.radar import _url_de_status
        url = _url_de_status("999")

        assert url == "https://x.com/i/status/999"
        assert "/i/status/" in url, url
        assert "x.com/status/" not in url, url

    def test_sem_id_nao_se_inventa_link(self):
        """String vazia, e não `https://x.com/i/status/`: o link truncado
        passaria pelo `if link` de `_bloco` e entraria no bloco como se
        fosse endereço."""
        from src.radar import _url_de_status
        assert _url_de_status("") == ""

    def test_o_link_montado_aqui_chega_ao_bloco_e_volta_como_id_do_pai(self):
        """A garantia conferida no DESTINO, e não só na função.

        A linha CITANDO não carrega link (sem o texto do citado ela não
        sai), então quem leva este link ao bloco é o ramo `resposta` — e é
        de lá que `filtra_respostas` camada 1 lê o id do pai para decidir o
        descarte. Se o formato do link mudar, quem quebra é a barreira."""
        from src.radar import _bloco, _id_do_pai, _url_de_status
        bloco = _bloco(1, _post_x("900", "explica ai TOKEN-R",
                                  tipo="resposta", pai_id="777"), {})

        assert _url_de_status("777") in bloco, bloco
        assert _id_do_pai(bloco) == "777"

class TestMarcaDeFormatoNoTEXTODoAutor:
    """As marcas do formato no TEXTO do autor, e o controle que impede a
    sobrecorreção.

    As quatro marcas do formato (URL:, TIPO:, EM RESPOSTA A, CITANDO) são
    escritas por `_bloco`; o texto do post é LITERAL do autor, e o autor
    escreve o que quiser — colar um print de conversa é uma linha começando
    com "EM RESPOSTA A"; comentar um post alheio é uma linha começando com
    "CITANDO". Varrer o bloco INTEIRO atrás das marcas leria texto como
    metadado.

    Cada caso vem em PAR, e a mesma linha muda só de lugar: no cabeçalho ela
    é metadado e tem de continuar valendo; no corpo é texto do autor e não
    pode valer nada. O par é o teste — sozinho, o falso positivo se
    "conserta" desligando a barreira, e aí resposta a terceiro volta ao
    boletim."""

    H = ("perfil_teste",)

    CABECA = ["POST 1 (@perfil_teste, Thu, 03 Sep 2026 17:50:11 GMT):",
              "TIPO: post"]
    MARCA_RESPOSTA = ("EM RESPOSTA A "
                      "(@terceiro, https://x.com/i/status/777): "
                      "a inflacao vai cair")
    MARCA_CITANDO = "CITANDO (@sigel): RIOT assina contrato de US$ 9 bi"
    MARCA_URL = "URL: https://x.com/terceiro/status/999"

    def _no_cabecalho(self, marca):
        return "\n".join(self.CABECA
                         + [marca, "o numero esta errado TOKEN-FIM"])

    def _no_corpo(self, marca):
        # A linha do autor ANTES da marca é o que a põe no corpo: o cabeçalho
        # acaba na primeira linha que não é de contexto.
        return "\n".join(self.CABECA
                         + ["olha o print do que responderam pra ele:", marca,
                            "o numero esta errado TOKEN-FIM"])

    # --- EM RESPOSTA A: a barreira que descarta resposta a terceiro --------

    def test_EM_RESPOSTA_A_no_CORPO_nao_derruba_o_post_proprio(self):
        """O falso positivo. Post PRÓPRIO, `TIPO: post`, e no corpo o autor
        colou o que alguém respondeu a um terceiro. Uma varredura do bloco
        inteiro acharia "@terceiro" e o status 777 — um interlocutor e um
        pai que não existem — e o post do autor sumiria do boletim antes de
        ser lido."""
        from src.radar import (_id_do_pai, filtra_respostas,
                               resposta_a_terceiro)
        bloco = self._no_corpo(self.MARCA_RESPOSTA)

        assert resposta_a_terceiro(bloco, self.H) is None
        assert _id_do_pai(bloco) is None
        ficam, fora = filtra_respostas((bloco,), self.H)
        assert ficam == (bloco,) and fora == []

    def test_EM_RESPOSTA_A_no_CABECALHO_continua_derrubando_o_post(self):
        """O controle, com a MESMA linha uma posição acima. Ali ela é
        metadado — escrita por `_bloco` — e descartá-la é a barreira
        funcionando."""
        from src.radar import (_id_do_pai, filtra_respostas,
                               resposta_a_terceiro)
        bloco = self._no_cabecalho(self.MARCA_RESPOSTA)

        assert resposta_a_terceiro(bloco, self.H) == "@terceiro"
        assert _id_do_pai(bloco) == "777"
        ficam, fora = filtra_respostas((bloco,), self.H)
        assert ficam == () and len(fora) == 1

    def test_o_post_cujo_TEXTO_traz_a_marca_chega_ao_boletim(self, monkeypatch):
        """O mesmo falso positivo pela porta da frente: `busca` com o texto
        literal do autor carregando a marca. É o caminho real — `_bloco`
        copia `Post.texto` sem tocar nele."""
        from src import radar

        def falso(handle, desde, ate="", limite=100):
            return [_post_x("111",
                            "olha o print do que responderam pra ele:\n"
                            + self.MARCA_RESPOSTA
                            + "\no numero esta errado TOKEN-FIM")]

        monkeypatch.setattr(radar.x_api, "posts_de", falso)
        r = radar.busca(self.H, 2)

        assert len(r.posts) == 1, r.notas
        assert "TOKEN-FIM" in r.posts[0]
        assert not any("resposta(s) a terceiro" in n for n in r.notas), r.notas

    # --- A mesma varredura, no separador que se paga por token -------------

    def test_para_separacao_deixa_a_marca_do_CORPO_como_o_autor_escreveu(self):
        """Reescrever a linha do autor não é só ruído: ela chega ao separador
        como "(contexto — palavras do interlocutor)", e o que o autor
        afirmou passa a ser atribuído a alguém que ele só citou de passagem."""
        from src.radar import para_separacao
        saida = para_separacao(self._no_corpo(self.MARCA_RESPOSTA))

        assert self.MARCA_RESPOSTA in saida
        assert "interlocutor" not in saida
        assert "contexto —" not in saida

    def test_para_separacao_continua_reescrevendo_a_marca_do_CABECALHO(self):
        from src.radar import para_separacao
        saida = para_separacao(self._no_cabecalho(self.MARCA_RESPOSTA))

        assert "palavras do interlocutor, não do autor" in saida
        assert ("(@terceiro, https://x.com/i/status/777): a inflacao vai cair"
                in saida)

    def test_CITANDO_no_CORPO_nao_vira_contexto_do_separador(self):
        """`filtra_respostas` não olha CITANDO, mas `para_separacao` olha — é
        a mesma varredura e o mesmo estrago: a frase do autor sai atribuída
        a "quem ele cita"."""
        from src.radar import para_separacao
        saida = para_separacao(self._no_corpo(self.MARCA_CITANDO))

        assert self.MARCA_CITANDO in saida
        assert "as afirmações são de quem ele cita" not in saida

    def test_CITANDO_no_CABECALHO_continua_virando_contexto_do_separador(self):
        """O controle do par: o caso RIOT (01/09/2026), em que os números
        eram do analista CITADO e não do autor, continua reatribuído."""
        from src.radar import para_separacao
        saida = para_separacao(self._no_cabecalho(self.MARCA_CITANDO))

        assert "as afirmações são de quem ele cita" in saida
        assert "(@sigel): RIOT assina contrato" in saida

    # --- TIPO: a barreira que falha FECHADO --------------------------------

    def test_TIPO_no_CORPO_nao_faz_um_bloco_sem_declaracao_passar(self):
        """Aqui o falso positivo vira falso NEGATIVO, e é o pior dos dois.

        `declara_post_proprio` falha FECHADO: bloco que não declara `TIPO:`
        cai. Se a varredura ler o corpo, basta o autor escrever "TIPO: post"
        numa linha para um bloco sem declaração nenhuma passar — a barreira
        deixa de falhar fechado, e resposta a terceiro volta ao boletim."""
        from src.radar import declara_post_proprio
        bloco = "\n".join([
            "POST 1 (@perfil_teste, Thu, 03 Sep 2026 17:50:11 GMT):",
            "URL: https://x.com/perfil_teste/status/111",
            "explicando o formato pra quem perguntou:",
            "TIPO: post"])

        assert not declara_post_proprio(bloco)

    def test_TIPO_do_CABECALHO_e_o_que_vale_mesmo_com_outro_no_corpo(self):
        """O controle nas duas pontas: o `TIPO:` do cabeçalho decide, e o do
        corpo não o contradiz nem para deixar passar nem para derrubar."""
        from src.radar import declara_post_proprio
        cabeca = ["POST 1 (@perfil_teste, Thu, 03 Sep 2026 17:50:11 GMT):",
                  "URL: https://x.com/perfil_teste/status/111"]

        proprio = "\n".join(cabeca + ["TIPO: post", "corpo:", "TIPO: resposta"])
        assert declara_post_proprio(proprio)

        alheio = "\n".join(cabeca + ["TIPO: resposta", "corpo:", "TIPO: post"])
        assert not declara_post_proprio(alheio)

    # --- URL: a âncora e a identidade do bloco -----------------------------

    def test_URL_no_CORPO_nao_vira_a_ancora_do_post(self):
        """Bloco sem linha URL: própria e com um `URL:` no texto do autor. A
        varredura antiga entregava ao boletim o link de OUTRO post como se
        fosse o deste — âncora que abre a página errada."""
        from src.radar import url_do_post
        bloco = self._no_corpo(self.MARCA_URL)

        assert url_do_post(bloco,
                           ("https://x.com/i/status/999",)) == (None, False)

    def test_URL_no_CABECALHO_continua_sendo_a_ancora_do_post(self):
        from src.radar import url_do_post
        bloco = self._no_cabecalho(self.MARCA_URL)

        url, confere = url_do_post(bloco, ("https://x.com/i/status/999",))
        assert url == "https://x.com/terceiro/status/999" and confere

    def test_URL_no_CORPO_nao_rouba_a_identidade_do_bloco_no_dedup(self):
        """`dedup_por_status` guarda um post por status ID na rodada. Com a
        identidade vinda do corpo, dois posts diferentes que citem o MESMO
        link viram um só — e o segundo some sem aparecer em nota nenhuma."""
        from src.radar import dedup_por_status
        um = self._no_corpo(self.MARCA_URL)
        outro = um.replace("TOKEN-FIM", "TOKEN-OUTRO")

        ficam, caidos = dedup_por_status((um, outro))
        assert len(ficam) == 2 and caidos == 0

    def test_URL_no_CABECALHO_continua_sendo_a_identidade_do_dedup(self):
        """O controle: o mesmo status lido duas vezes na rodada continua
        caindo uma vez."""
        from src.radar import dedup_por_status
        um = self._no_cabecalho(self.MARCA_URL)
        outro = um.replace("TOKEN-FIM", "TOKEN-OUTRO")

        ficam, caidos = dedup_por_status((um, outro))
        assert len(ficam) == 1 and caidos == 1
