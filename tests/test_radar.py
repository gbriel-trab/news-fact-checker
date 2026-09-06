"""O radar sobre o `Post` estruturado: barreiras por campo, as duas saídas
em texto e a rodada — tudo sem rede.

O que estes testes protegem: que as barreiras decidam por METADADO (id,
tipo, pai_id) e nunca por texto; que o texto do separador saia com a
atribuição certa (palavra do autor × palavra de terceiro); e que a rodada
CONTE o que descartou, em vez de sumir com ele. `x_api.posts_de` é mockado
em todo teste que chega perto da rede.

Nenhum teste toca a rede, e isso é BARRADO e não combinado: ver
`sem_rede_sem_token_do_dono`.
"""

import socket

import pytest

from src import radar
from src.premissas import CONTEXTO_ALHEIO, CONTEXTO_PROPRIO, texto_ancoravel
from src.radar import Captura, _handles_de, quando
from src.x_api import Post


# ------------------------------------------------------------- fixtures


def _proibido(*args, **kwargs):
    raise AssertionError("o teste tentou falar com a rede")


class _SocketProibido:
    """Barra o socket cru, que é a rede que não passa por `requests`."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("o teste tentou abrir um socket")


@pytest.fixture(autouse=True)
def sem_rede_sem_token_do_dono(monkeypatch):
    """As amarras que `test_x_api.py` e `test_x_auth.py` já tinham.

    Não é zelo: MEDIDO em 04/09/2026, numa auditoria por mutação. Um teste
    desta suíte desceu até `requests.post` DE VERDADE e gastou três
    tentativas contra a internet — a mutação estava errada e a suíte ficou
    verde de todo modo, porque quem respondeu foi a rede e não o mock.
    Teste que depende do que há do outro lado do cabo não mede código.

    Patch-se `radar.x_api.requests` porque o radar não importa `requests`:
    a única porta de rede alcançável daqui é a do `x_api`, e o objeto de
    módulo é o MESMO que o `x_auth` importa — uma substituição cobre as
    duas. `token` entra junto porque `x_auth.token()` lê
    `data/x_token.json` — o arquivo do dono, com o refresh vivo dentro."""
    monkeypatch.setattr(radar.x_api.requests, "post", _proibido)
    monkeypatch.setattr(radar.x_api.requests, "get", _proibido)
    monkeypatch.setattr(socket, "socket", _SocketProibido)
    monkeypatch.setattr(radar.x_api, "token", lambda: "token-de-mentira")


H = ("perfil_teste",)


def _post(ident, texto, tipo="post", quando="2026-09-03T17:50:11Z",
          pai_id="", autor="perfil_teste"):
    """Um `x_api.Post` sintético, como o cliente da API o entregaria."""
    return Post(id=ident, autor=autor, criado_em=quando, texto=texto,
                tipo=tipo, pai_id=pai_id,
                url=f"https://x.com/{autor}/status/{ident}" if ident else "")


def _liga(monkeypatch, por_handle):
    """Mocka `x_api.posts_de`. Devolve a lista de pedidos feitos.

    `por_handle` mapeia handle -> lista de Post OU uma exceção a levantar."""
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


def _motivos(fora):
    return [m for _, m in fora]


# ------------------------------------------------------------ handles


class TestHandles:
    def test_normaliza_e_filtra(self):
        assert _handles_de("@perfil_teste, outro") == ("perfil_teste",
                                                        "outro")

    def test_arroba_sozinho_cai_fora(self):
        # '@' sobrevive ao filtro e vira handle vazio depois do lstrip: a
        # normalização vem ANTES do filtro.
        assert _handles_de("@") == ()
        assert _handles_de("@,perfil_teste") == ("perfil_teste",)

    def test_vazio_devolve_nada(self):
        assert _handles_de(" , ") == ()


class TestQuando:
    def test_iso_com_z_vira_utc_legivel(self):
        assert quando("2026-09-03T17:50:11Z") == "2026-09-03 17:50 UTC"

    def test_fuso_explicito_e_normalizado_para_utc(self):
        assert quando("2026-09-03T14:50:11-03:00") == "2026-09-03 17:50 UTC"

    def test_sem_fuso_assume_utc(self):
        # Ler como hora local mudaria a data conforme a máquina.
        assert quando("2026-09-03T17:50:11") == "2026-09-03 17:50 UTC"

    def test_ausente_ou_ilegivel_nao_apaga_o_parentese(self):
        assert quando("") == "sem data"
        assert quando("ontem à tarde") == "ontem à tarde"


# ---------------------------------------------------------- barreiras


class TestDedup:
    def test_mesmo_id_conta_uma_vez(self):
        ficam, fora = radar.dedup([_post("111", "a"), _post("111", "a"),
                                   _post("222", "b")])
        assert [p.id for p in ficam] == ["111", "222"]
        assert _motivos(fora) == [radar._REPETIDO]

    def test_post_sem_id_nao_e_descartado(self):
        """Sem id não há identidade. Descartar perderia post por falta de
        metadado — o erro caro é o falso negativo de cobertura."""
        ficam, fora = radar.dedup([_post("", "sem id"),
                                   _post("", "também sem id")])
        assert len(ficam) == 2 and fora == []

    def test_ids_diferentes_todos_ficam(self):
        posts = [_post(str(i), "x") for i in range(1, 6)]
        ficam, fora = radar.dedup(posts)
        assert ficam == posts and fora == []


class TestSeparaPorTipo:
    def test_post_thread_e_citacao_ficam(self):
        posts = [_post("1", "raiz"),
                 _post("2", "continuação", tipo="thread", pai_id="1"),
                 _post("3", "comentário", tipo="citacao", pai_id="1")]
        ficam, fora = radar.separa_por_tipo(posts)
        assert ficam == posts and fora == []

    def test_resposta_a_outra_conta_cai_com_a_causa(self):
        """A barreira que segura a resposta a terceiro. O veredito é do
        servidor: `x_api.classifica` só chama de `resposta` o que responde
        a outra conta, e o motivo diz isso — ele vai para o arquivo."""
        ficam, fora = radar.separa_por_tipo(
            [_post("9", "explica ai", tipo="resposta", pai_id="7")])
        assert ficam == []
        assert "in_reply_to_user_id" in fora[0][1]

    def test_retweet_cai(self):
        ficam, fora = radar.separa_por_tipo(
            [_post("9", "texto de terceiro", tipo="retweet", pai_id="5")])
        assert ficam == [] and "retweet" in fora[0][1]

    def test_tipo_fora_do_vocabulario_cai_com_o_nome(self):
        """Se `Post.tipo` crescer um dia, o valor novo aparece com o nome
        em vez de virar "retweet" em silêncio."""
        ficam, fora = radar.separa_por_tipo(
            [_post("9", "coisa nova", tipo="transmissao")])
        assert ficam == [] and "transmissao" in fora[0][1]


class TestCadeia:
    """O autor respondendo a si mesmo DENTRO de uma resposta a terceiro:
    o pai imediato é legítimo, quem entrega é o avô."""

    def test_thread_pendurada_em_resposta_cai(self):
        resposta = _post("9001", "resposta a terceiro", tipo="resposta",
                         pai_id="777")
        filha = _post("9002", "auto-resposta na cadeia", tipo="thread",
                      pai_id="9001")
        ficam, fora = radar.separa_por_tipo([resposta, filha])
        ficam, fora = radar.cadeia(ficam, fora)
        assert ficam == []
        assert dict((p.id, m) for p, m in fora)["9002"] == radar._CADEIA

    def test_neto_cai_tambem(self):
        resposta = _post("1", "r", tipo="resposta", pai_id="0")
        filha = _post("2", "f", tipo="thread", pai_id="1")
        neta = _post("3", "n", tipo="thread", pai_id="2")
        ficam, fora = radar.separa_por_tipo([resposta, filha, neta])
        ficam, fora = radar.cadeia(ficam, fora)
        assert ficam == [] and len(fora) == 3

    def test_thread_com_pai_fora_da_janela_fica(self):
        """Pai ausente não é pai descartado. Derrubar isto mataria thread
        própria em série (janela de 2 dias, thread começada há três)."""
        continuacao = _post("222", "continuação", tipo="thread",
                            pai_id="000-fora-da-janela")
        ficam, fora = radar.cadeia([continuacao], [])
        assert ficam == [continuacao] and fora == []

    def test_thread_com_pai_vivo_fica(self):
        """O caso do C25: o autor continuando a si mesmo."""
        raiz = _post("111", "raiz")
        continuacao = _post("222", "continuação", tipo="thread", pai_id="111")
        ficam, fora = radar.cadeia([raiz, continuacao], [])
        assert ficam == [raiz, continuacao]

    def test_citacao_de_post_descartado_fica(self):
        """Só `thread` segue a cadeia: citar um post descartado é
        comentário do autor sobre ele, e o comentário é do autor."""
        resposta = _post("1", "r", tipo="resposta", pai_id="0")
        citacao = _post("2", "comentário por cima", tipo="citacao",
                        pai_id="1")
        ficam, fora = radar.separa_por_tipo([resposta, citacao])
        ficam, fora = radar.cadeia(ficam, fora)
        assert ficam == [citacao]

    def test_duplicata_descartada_nao_derruba_a_filha(self):
        """Dedup descarta a CÓPIA, não o post: o pai continua vivo e a
        thread pendurada nele fica. É por isso que a cadeia olha os ids
        descartados MENOS os que ficaram."""
        raiz = _post("111", "raiz")
        filha = _post("222", "continuação", tipo="thread", pai_id="111")
        ficam, fora, _ = radar.barreiras([raiz, raiz, filha])
        assert ficam == [raiz, filha]
        assert _motivos(fora) == [radar._REPETIDO]


class TestNotasDasBarreiras:
    def test_cada_motivo_vira_nota_com_numero_e_causa(self):
        posts = [_post("1", "a"), _post("1", "a"),
                 _post("2", "r", tipo="resposta", pai_id="0"),
                 _post("3", "r2", tipo="resposta", pai_id="0"),
                 _post("4", "rt", tipo="retweet", pai_id="5"),
                 _post("6", "novo", tipo="transmissao"),
                 _post("7", "f", tipo="thread", pai_id="2")]
        ficam, fora, notas = radar.barreiras(posts)
        assert [p.id for p in ficam] == ["1"]
        texto = "\n".join(notas)
        assert "1 post(s) repetido(s)" in texto
        assert "2 resposta(s) a outra conta" in texto
        assert "in_reply_to_user_id" in texto
        assert "1 retweet(s)" in texto
        assert "transmissao" in texto
        assert "1 continuação(ões) de thread" in texto and "cadeia" in texto

    def test_sem_descarte_sem_nota(self):
        _, fora, notas = radar.barreiras([_post("1", "a"), _post("2", "b")])
        assert fora == [] and notas == []


# ------------------------------------------------- as duas saídas em texto


class TestParaSeparacao:
    """O texto que o separador recebe: contrato com a regra 9 do prompt e
    com `premissas.texto_ancoravel`, e por isso conferido aqui contra ela."""

    def test_cabecalho_sem_numero_e_sem_url(self):
        texto = radar.para_separacao(
            Captura(_post("111", "A Selic esta em 15%")))
        assert texto == ("POST (@perfil_teste, 2026-09-03 17:50 UTC):\n"
                         "A Selic esta em 15%")
        assert "URL" not in texto and "status/111" not in texto

    def test_thread_propria_vira_contexto_do_autor(self):
        raiz = _post("111", "A Selic esta em 15%")
        c = Captura(_post("222", "E vai ficar assim", tipo="thread",
                          pai_id="111"), referenciado=raiz)
        texto = radar.para_separacao(c)
        assert texto.split("\n")[1] == (
            f"({CONTEXTO_PROPRIO}: A Selic esta em 15%)")
        # E o separador pode ancorar nela: é texto do autor (regra 9).
        assert "Selic" in texto_ancoravel(texto)

    def test_citacao_de_outra_conta_vira_contexto_alheio(self):
        citado = _post("555", "RIOT assina contrato de US$ 9 bi",
                       autor="sigel")
        c = Captura(_post("333", "Empresas boas sabem divulgar",
                          tipo="citacao", pai_id="555"), referenciado=citado)
        texto = radar.para_separacao(c)
        assert texto.split("\n")[1] == (
            f"({CONTEXTO_ALHEIO}: RIOT assina contrato de US$ 9 bi)")
        # E o separador NÃO ancora nela: palavra de quem ele cita.
        ancoravel = texto_ancoravel(texto)
        assert "RIOT" not in ancoravel and "Empresas boas" in ancoravel

    def test_autocitacao_e_palavra_do_proprio_autor(self):
        """Citar a si mesmo é o caso dominante do acervo, e a atribuição
        vem da comparação de autor: as palavras são dele, podem virar
        premissa."""
        proprio = _post("111", "A Selic esta em 15%")
        c = Captura(_post("333", "Repito o que disse", tipo="citacao",
                          pai_id="111"), referenciado=proprio)
        assert c.contexto_proprio
        assert CONTEXTO_PROPRIO in radar.para_separacao(c)
        assert CONTEXTO_ALHEIO not in radar.para_separacao(c)

    def test_autor_compara_sem_caixa(self):
        proprio = _post("111", "raiz", autor="Perfil_Teste")
        c = Captura(_post("222", "x", tipo="thread", pai_id="111"),
                    referenciado=proprio)
        assert c.contexto_proprio

    def test_referenciado_ausente_nao_gera_linha(self):
        c = Captura(_post("222", "continuação TOKEN-C2", tipo="thread",
                          pai_id="000-fora-da-janela"))
        texto = radar.para_separacao(c)
        assert texto.count("\n") == 1 and "contexto" not in texto

    def test_referenciado_sem_texto_nao_gera_linha(self):
        c = Captura(_post("222", "x", tipo="thread", pai_id="111"),
                    referenciado=_post("111", "   \n  "))
        assert "contexto" not in radar.para_separacao(c)

    def test_referenciado_de_varias_linhas_vai_numa_linha_so(self):
        """`texto_ancoravel` exclui a linha do citado por prefixo: um
        citado de várias linhas deixaria a segunda ancorável."""
        citado = _post("555", "primeira linha do citado\nsegunda linha "
                              "ancoravel TOKEN-2", autor="sigel")
        c = Captura(_post("333", "comentário", tipo="citacao",
                          pai_id="555"), referenciado=citado)
        texto = radar.para_separacao(c)
        assert texto.count("\n") == 2
        assert "TOKEN-2" not in texto_ancoravel(texto)

    def test_texto_do_autor_sai_inteiro_e_literal(self):
        """O texto é literal do autor: traço, "POST 9", o que for. O radar
        não reparseia o que produz; o único leitor em código é
        `premissas.texto_ancoravel`, que tira só a PRIMEIRA linha."""
        armadilha = ("primeira metade TOKEN-D\n---\n"
                     "POST 9 (@fake, x):\nsegunda metade TOKEN-E")
        texto = radar.para_separacao(Captura(_post("111", armadilha)))
        assert texto.endswith(armadilha)
        # E o cabeçalho tirado por `texto_ancoravel` é só o PRIMEIRO: a
        # linha "POST 9" do autor continua ancorável.
        assert "POST 9 (@fake, x)" in texto_ancoravel(texto)


class TestComoTexto:
    def test_leva_numero_tipo_url_contexto_e_corpo(self):
        raiz = _post("111", "A Selic esta em 15%")
        c = Captura(_post("222", "E vai ficar assim", tipo="thread",
                          pai_id="111"), referenciado=raiz)
        texto = radar.como_texto(c, 4)
        linhas = texto.split("\n")
        assert linhas[0] == ("POST 4 (@perfil_teste, 2026-09-03 17:50 UTC) "
                             "· thread")
        assert linhas[1] == "URL: https://x.com/perfil_teste/status/222"
        assert linhas[2] == ("contexto (post anterior do próprio autor): "
                             "A Selic esta em 15%")
        assert linhas[3] == "E vai ficar assim"

    def test_citacao_alheia_diz_de_quem_e(self):
        citado = _post("555", "tese do analista", autor="sigel")
        c = Captura(_post("333", "comentário", tipo="citacao", pai_id="555"),
                    referenciado=citado)
        assert "contexto (post citado, de @sigel): tese do analista" in (
            radar.como_texto(c, 1))


# ------------------------------------------------------------- a rodada


class TestBusca:
    def test_retweet_e_lido_pago_descartado_e_contado(self, monkeypatch):
        """A API cobra por recurso devolvido: o retweet não vira captura,
        mas entra em `lidos` e no custo, e some com aviso."""
        _liga(monkeypatch, {"perfil_teste": (
            _post("111", "A Selic esta em 15% TOKEN-P"),
            _post("222", "outro post"),
            _post("777", "texto de terceiro", tipo="retweet", pai_id="555"))})
        r = radar.busca(H, 2)
        assert [c.post.id for c in r.capturas] == ["111", "222"]
        assert r.lidos == 3
        assert r.custo_estimado_usd == radar.x_api.custo_estimado_usd(3)
        assert any("1 retweet" in n for n in r.notas), r.notas
        assert [p.id for p, _ in r.descartados] == ["777"]

    def test_resposta_a_terceiro_nao_chega_e_a_nota_diz_a_causa(
            self, monkeypatch):
        _liga(monkeypatch, {"perfil_teste": (
            _post("111", "A Selic esta em 15% TOKEN-P"),
            _post("900", "explica ai TOKEN-R", tipo="resposta",
                  pai_id="777"))})
        r = radar.busca(H, 2)
        assert [c.post.id for c in r.capturas] == ["111"]
        assert any("resposta a terceiro" in n and "in_reply_to_user_id" in n
                   for n in r.notas), r.notas
        assert r.descartados[0][0].id == "900"

    def test_thread_com_o_pai_na_rodada_ganha_o_referenciado(
            self, monkeypatch):
        _liga(monkeypatch, {"perfil_teste": (
            _post("111", "raiz"),
            _post("222", "continuação", tipo="thread", pai_id="111"))})
        r = radar.busca(H, 2)
        assert r.capturas[1].referenciado is r.capturas[0].post
        assert not any("FORA da janela" in n for n in r.notas)

    def test_referenciado_fora_da_janela_e_contado_com_numero(
            self, monkeypatch):
        """A nota é conferida pelo TEXTO, não pela existência: número
        errado ali é o dono lendo "1" e não indo procurar os outros. Dois
        threads e uma citação fora, e a autocitação (citado lido nesta
        rodada) fica de fora da conta."""
        _liga(monkeypatch, {"perfil_teste": (
            _post("111", "raiz TOKEN-RAIZ"),
            _post("222", "continuação", tipo="thread", pai_id="555"),
            _post("333", "outra continuação", tipo="thread", pai_id="666"),
            _post("444", "comentário", tipo="citacao", pai_id="777"),
            _post("999", "autocitação", tipo="citacao", pai_id="111"))})
        r = radar.busca(H, 2)
        assert len(r.capturas) == 5
        notas = [n for n in r.notas if "FORA da janela" in n]
        assert len(notas) == 1 and notas[0].startswith("3 post(s)"), r.notas
        assert "cobrança" in notas[0]
        assert sum(1 for c in r.capturas if c.referenciado) == 1

    def test_citacao_sem_id_do_citado_some_com_aviso(self, monkeypatch):
        _liga(monkeypatch, {"perfil_teste": (
            _post("333", "comentário TOKEN-C", tipo="citacao", pai_id=""),)})
        r = radar.busca(H, 2)
        assert len(r.capturas) == 1 and r.capturas[0].referenciado is None
        notas = [n for n in r.notas if "sem o id do post citado" in n]
        assert len(notas) == 1 and notas[0].startswith("1 citação(ões)")
        assert not any("FORA da janela" in n for n in r.notas)

    def test_tipo_fora_do_vocabulario_sai_com_o_nome(self, monkeypatch):
        _liga(monkeypatch, {"perfil_teste": (
            _post("111", "raiz"), _post("888", "coisa nova",
                                        tipo="transmissao"))})
        r = radar.busca(H, 2)
        assert len(r.capturas) == 1
        assert any("transmissao" in n for n in r.notas), r.notas

    def test_handle_sem_posts_aparece_na_nota(self, monkeypatch):
        _liga(monkeypatch, {"perfil_teste": (_post("111", "x"),),
                            "outro": ()})
        r = radar.busca(("perfil_teste", "outro"), 2)
        assert any("@outro" in n and "não retornou nada" in n
                   for n in r.notas), r.notas

    def test_a_janela_e_um_instante_e_o_limite_e_teto_de_gasto(
            self, monkeypatch):
        pedidos = _liga(monkeypatch, {"perfil_teste": ()})
        radar.busca(H, 2)
        assert len(pedidos) == 1
        assert pedidos[0]["limite"] == radar.LIMITE_POR_HANDLE
        assert pedidos[0]["ate"] == ""
        assert pedidos[0]["desde"].endswith("Z")
        assert len(pedidos[0]["desde"]) == len("2026-09-02T17:50:11Z")

    def test_PrecisaAutorizar_vira_FalhaNoRadar_com_o_comando(
            self, monkeypatch):
        """`boletim.monta` captura `radar.FalhaNoRadar` e mais nada — outra
        classe subiria como traceback, fora do aviso do Telegram. E quem lê
        o aviso não está no terminal: a mensagem carrega o comando."""
        from src.x_auth import PrecisaAutorizar
        _liga(monkeypatch,
              {"perfil_teste": PrecisaAutorizar("token recusado (401)")})
        with pytest.raises(radar.FalhaNoRadar) as erro:
            radar.busca(H, 2)
        assert "src.x_auth" in str(erro.value)
        assert "token recusado (401)" in str(erro.value)

    def test_FalhaNaAPI_em_um_handle_vira_nota_e_a_rodada_segue(
            self, monkeypatch):
        from src.x_api import FalhaNaAPI
        _liga(monkeypatch, {
            "perfil_teste": (_post("111", "TOKEN-P"),),
            "outro": FalhaNaAPI("o X respondeu 503 duas vezes")})
        r = radar.busca(("perfil_teste", "outro"), 2)
        assert len(r.capturas) == 1
        assert any("@outro" in n and "503" in n for n in r.notas), r.notas

    def test_FalhaNaAPI_em_todos_sobe_erro(self, monkeypatch):
        """Rodada vazia entregue como sucesso esconderia uma queda total."""
        from src.x_api import FalhaNaAPI
        _liga(monkeypatch, {"perfil_teste": FalhaNaAPI("503"),
                            "outro": FalhaNaAPI("503")})
        with pytest.raises(radar.FalhaNoRadar, match="TODOS os handles"):
            radar.busca(("perfil_teste", "outro"), 2)

    def test_o_detalhe_do_custo_diz_estimado_e_teto(self, monkeypatch):
        """A regra do dono é custo estimado antes e REAL depois. Por esta
        via o real não existe — o X não devolve preço — e a palavra tem de
        chegar ao rodapé do boletim."""
        _liga(monkeypatch, {"perfil_teste": (_post("111", "x"),)})
        r = radar.busca(H, 2)
        assert "estimado" in r.detalhe_custo.lower()
        assert "teto" in r.detalhe_custo.lower()


# ------------------------------------------ o que o boletim faz com isto


class TestConsumidoresDoBoletim:
    def test_chave_de_dedup_e_o_id_e_so_sem_id_cai_no_texto(self):
        from src.boletim import _chaves_do_post, _hash_post
        c = Captura(_post("111", "A Selic esta em 15%"))
        assert _chaves_do_post(c) == {"url:111"}
        assert _chaves_do_post(Captura(_post("", "sem id"))) == {
            _hash_post("sem id")}

    def test_o_telegram_le_handle_data_link_e_contexto_dos_campos(self):
        from src.boletim import _formata_telegram
        raiz = _post("111", "A Selic esta em 15%")
        thread = Captura(_post("222", "E vai ficar assim", tipo="thread",
                               pai_id="111"), referenciado=raiz)
        citado = _post("555", "tese do analista", autor="sigel")
        citacao = Captura(_post("333", "comentário", tipo="citacao",
                                pai_id="555"), referenciado=citado)
        vazio = {"nao_verificaveis": [], "checks": [], "contextos": [],
                 "sem_premissas": False}
        html = _formata_telegram("@perfil_teste", "03/09",
                                 [(1, thread, dict(vazio)),
                                  (2, citacao, dict(vazio))],
                                 [], 0.10, 0.03)
        assert "<i>(@perfil_teste, 2026-09-03 17:50 UTC)</i>" in html
        assert 'href="https://x.com/perfil_teste/status/222"' in html
        assert ("<code>[CONTEXTO]</code> <i>@perfil_teste: A Selic esta "
                "em 15%</i>") in html
        assert "<code>[CITANDO]</code> <i>@sigel: tese do analista</i>" in html
        assert "URL:" not in html


# ------------------------------------------------------- janela explícita


class TestJanelaExplicita:
    """`busca(handles, desde=..., ate=...)` é como o boletim refaz um dia
    passado (06/09/2026): a janela vai ao cliente como veio, `dias` é
    ignorado, e sem `desde` a janela continua sendo "os últimos dias"."""

    def test_desde_e_ate_vao_ao_cliente_e_dias_e_ignorado(self, monkeypatch):
        pedidos = _liga(monkeypatch, {"perfil_teste": []})
        radar.busca(("perfil_teste",), 9,
                    desde="2026-08-25", ate="2026-08-26")
        assert pedidos == [{"handle": "perfil_teste", "desde": "2026-08-25",
                            "ate": "2026-08-26",
                            "limite": radar.LIMITE_POR_HANDLE}]

    def test_sem_desde_a_janela_e_os_ultimos_dias_ate_agora(self, monkeypatch):
        pedidos = _liga(monkeypatch, {"perfil_teste": []})
        radar.busca(("perfil_teste",), 2)
        assert pedidos[0]["ate"] == ""
        assert pedidos[0]["desde"].endswith("Z")
