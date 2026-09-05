"""O cliente da API oficial do X, sem rede e sem credencial.

Duas coisas se testam aqui. A primeira: `classifica` deriva o tipo do post
do METADADO do servidor, e não de um rótulo pedido a um modelo.
`TestClassifica` prova a regra caso a caso, e `TestPontaAPonta` repete os
mesmos casos ponta a ponta, porque entre a regra certa e o objeto que o radar
vai consumir ainda existe `_monta`.

A segunda é o empate documental: duas páginas primárias da X discordam sobre
os nomes dos campos e o openapi.json responde 402. O cliente sonda os dois
dialetos em vez de apostar num.

Nenhum teste toca a rede: `requests.get` e `token` são substituídos. Nenhum
teste conhece credencial — `token` devolve uma string de mentira, e o que se
verifica é a IGUALDADE com essa mentira: só passa se o cabeçalho tiver saído
de `token()`. Conferir só o prefixo `Bearer ` não provava nada, e a mutação de
04/09/2026 mostrou por quê — um Bearer escrito no código passava incólume.
"""

import importlib.util
import itertools

import pytest

from src import x_api, x_auth
from src.x_api import (ANTIGO, NOVO, FalhaNaAPI, Post, classifica,
                       custo_estimado_usd, id_do_handle, posts_de,
                       texto_integral)
from src.x_auth import PrecisaAutorizar

AUTOR = "111"
TERCEIRO = "999"


def post_cru(**campos) -> dict:
    base = {"id": "1", "text": "corpo", "created_at": "2026-09-04T12:00:00Z",
            "author_id": AUTOR, "conversation_id": "1"}
    base.update(campos)
    return base


class Resposta:
    """O mínimo de `requests.Response` que o módulo usa."""

    def __init__(self, status=200, corpo=None, headers=None, texto=""):
        self.status_code = status
        self._corpo = {} if corpo is None else corpo
        self.headers = headers or {}
        self.text = texto or str(self._corpo)

    def json(self):
        if isinstance(self._corpo, Exception):
            raise self._corpo
        return self._corpo


@pytest.fixture(autouse=True)
def sem_estado(monkeypatch):
    """Cada teste começa sem sonda resolvida e sem id cacheado.

    Os dois caches são de módulo de propósito (não repetir uma requisição
    recusada a cada página, não repagar a busca de um id que não muda), e
    estado de módulo que sobrevive entre testes faz o segundo teste passar
    pelo trabalho do primeiro."""
    x_api._esquece_nomenclatura()
    monkeypatch.setattr(x_api, "_ids", {})
    monkeypatch.setattr(x_api, "token", lambda: "token-de-mentira")


class Servidor:
    """Fila de respostas + registro do que foi pedido."""

    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.chamadas: list[tuple[str, dict, dict]] = []

    def __call__(self, url, headers=None, params=None, timeout=None):
        self.chamadas.append((url, dict(params or {}), dict(headers or {})))
        if not self.respostas:
            raise AssertionError(f"chamada a mais: {url} {params}")
        item = self.respostas.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def params(self) -> dict:
        return self.chamadas[-1][1]


def liga(monkeypatch, *respostas) -> Servidor:
    servidor = Servidor(*respostas)
    monkeypatch.setattr(x_api.requests, "get", servidor)
    return servidor


def usuario(ident=AUTOR) -> Resposta:
    return Resposta(corpo={"data": {"id": ident, "username": "handle"}})


def timeline(*posts, next_token="") -> Resposta:
    corpo: dict = {"data": list(posts)}
    if next_token:
        corpo["meta"] = {"next_token": next_token}
    return Resposta(corpo=corpo)


class Torneira:
    """Servidor que NUNCA para de oferecer página nova.

    `Servidor` tem fila finita e, esgotada a fila, acusa "chamada a mais" —
    ótimo para contar requisições, inútil para provar que um laço PARA. Aqui
    cada volta devolve um `next_token` inédito, que é o modo de falha que as
    duas paradas antigas de `posts_de` não pegavam: token novo a cada página
    nunca repete, e página sem post não faz a contagem subir.

    `teto_do_harness` existe porque um teste que não para não falha, pendura a
    suíte inteira. É a trava do TESTE, deliberadamente acima de
    `x_api.TETO_PAGINAS` para não ser ela a produzir o número que se afirma.
    """

    def __init__(self, posts_por_pagina: int, teto_do_harness: int):
        self.posts_por_pagina = posts_por_pagina
        self.teto_do_harness = teto_do_harness
        self.paginas = 0
        self.chamadas: list[tuple[str, dict, dict]] = []

    def __call__(self, url, headers=None, params=None, timeout=None):
        self.chamadas.append((url, dict(params or {}), dict(headers or {})))
        if not url.endswith("/tweets"):
            return usuario()
        self.paginas += 1
        if self.paginas > self.teto_do_harness:
            raise AssertionError(
                f"o laço não parou: {self.paginas} páginas pagas com o "
                f"dinheiro do dono")
        posts = [post_cru(id=f"{self.paginas}-{n}",
                          conversation_id=f"{self.paginas}-{n}")
                 for n in range(self.posts_por_pagina)]
        return timeline(*posts, next_token=f"pagina-inedita-{self.paginas}")


class TestClassifica:
    """A derivação do tipo. Função pura — nenhum destes precisa de rede."""

    def test_raiz_de_conversa_e_post(self):
        tipo, pai, autor = classifica(post_cru(id="7", conversation_id="7"),
                                      "handle", {})
        assert (tipo, pai, autor) == ("post", "", "")

    def test_resposta_a_si_mesmo_e_thread(self):
        """O caso C25 do gabarito: o autor continuando a própria conversa.

        É o que `filtra_respostas` precisa preservar enquanto derruba
        resposta a terceiro. O rótulo sai de `in_reply_to_user_id ==
        author_id`, comparação de IDS."""
        cru = post_cru(id="8", conversation_id="7", in_reply_to_user_id=AUTOR,
                       referenced_posts=[{"type": "replied_to", "id": "7"}])
        assert classifica(cru, "handle", {AUTOR: "handle"}) == (
            "thread", "7", "handle")

    def test_resposta_a_terceiro(self):
        """Resposta a outra conta: o id do autor do pai vem do servidor, e a
        comparação é por id — não há handle escrito a confundir com a raiz
        da thread."""
        cru = post_cru(id="8", conversation_id="7",
                       in_reply_to_user_id=TERCEIRO,
                       referenced_posts=[{"type": "replied_to", "id": "7"}])
        tipo, pai, autor = classifica(cru, "handle", {AUTOR: "handle"})
        assert (tipo, pai) == ("resposta", "7")
        assert autor == ""

    def test_nomeia_o_terceiro_quando_o_username_e_conhecido(self):
        cru = post_cru(id="8", in_reply_to_user_id=TERCEIRO, conversation_id="7",
                       referenced_posts=[{"type": "replied_to", "id": "7"}])
        assert classifica(cru, "handle",
                          {AUTOR: "handle", TERCEIRO: "outro"})[2] == "outro"

    def test_citacao(self):
        cru = post_cru(id="9", conversation_id="9",
                       referenced_posts=[{"type": "quoted", "id": "5"}])
        assert classifica(cru, "handle", {}) == ("citacao", "5", "")

    def test_retweet(self):
        cru = post_cru(id="9", conversation_id="9",
                       referenced_posts=[{"type": "retweeted", "id": "5"}])
        assert classifica(cru, "handle", {}) == ("retweet", "5", "")

    def test_le_os_dois_dialetos_do_campo(self):
        """`referenced_tweets` e `referenced_posts` são o mesmo dado.

        Ler os dois é grátis; deixar o empate documental decidir o rótulo do
        post não é."""
        antigo = post_cru(id="9", conversation_id="9",
                          referenced_tweets=[{"type": "quoted", "id": "5"}])
        assert classifica(antigo, "handle", {})[0] == "citacao"

    def test_resposta_que_tambem_cita_e_resposta(self):
        """Os dois tipos coexistem, e o mais restritivo ganha.

        Se o rótulo virasse `citacao`, uma resposta a terceiro entraria no
        boletim pela porta do quote — que é exatamente o que o projeto não
        pode deixar passar."""
        cru = post_cru(id="8", conversation_id="7",
                       in_reply_to_user_id=TERCEIRO,
                       referenced_posts=[{"type": "quoted", "id": "3"},
                                         {"type": "replied_to", "id": "7"}])
        assert classifica(cru, "handle", {})[0] == "resposta"

    def test_sem_metadado_falha_fechado(self):
        """Sem `referenced` e sem `conversation_id == id`, vira `resposta`.

        Não dá para provar que é raiz, e o projeto já decidiu, em
        `radar.declara_post_proprio`, que prefere perder post legítimo a
        deixar entrar resposta a terceiro."""
        assert classifica({"id": "8", "author_id": AUTOR}, "handle", {})[0] == (
            "resposta")

    def test_replied_to_sem_autor_do_pai_nao_vira_thread(self):
        """Sem `in_reply_to_user_id` não se afirma que o pai é o próprio autor.

        Presumir que é seria falhar ABERTO: quem não traz o metadado viraria
        post próprio."""
        cru = post_cru(id="8", conversation_id="7",
                       referenced_posts=[{"type": "replied_to", "id": "7"}])
        assert classifica(cru, "handle", {AUTOR: "handle"})[0] == "resposta"

    def test_nao_toca_no_dicionario_de_autores(self):
        autores = {AUTOR: "handle"}
        classifica(post_cru(id="7", conversation_id="7"), "handle", autores)
        assert autores == {AUTOR: "handle"}


class TestPontaAPonta:
    """A derivação do tipo, ponta a ponta — não só a função pura.

    `TestClassifica` prova a regra; estes provam que ela CHEGA no objeto que o
    radar vai consumir. Entre uma coisa e outra está `_monta`, e é lá que uma
    derivação certa ainda pode sair errada do cliente."""

    def test_metadado_de_resposta_a_terceiro_nao_vira_post_proprio(
            self, monkeypatch):
        """Resposta a terceiro não pode sair como post próprio — é o que o
        radar mais precisa que o cliente acerte, antes de qualquer barreira
        dele (`filtra_respostas` trata o pai fora da rodada, não isto).
        Aqui não há a quem perguntar: `in_reply_to_user_id` é de outra
        pessoa, e o tipo cai sozinho."""
        liga(monkeypatch, usuario(),
             timeline(post_cru(id="8", conversation_id="7", text="concordo",
                               in_reply_to_user_id=TERCEIRO,
                               referenced_posts=[{"type": "replied_to",
                                                  "id": "7"}])))
        (post,) = posts_de("handle", "2026-09-01")
        assert post.tipo != "post"
        assert post.tipo == "resposta"
        assert post.pai_id == "7"
        # O id do terceiro é conhecido, o username não — e vazio aqui é "não
        # sei", não "é o dono da timeline". Foi essa substituição silenciosa
        # que o modelo fez.
        assert post.pai_autor == ""

    def test_thread_do_proprio_autor_sobrevive(self, monkeypatch):
        """O outro lado do mesmo filtro, e o que torna a regra útil.

        Barreira que derruba toda resposta acerta em resposta a terceiro e
        mata o C25 junto — a continuação de thread própria, que é o único
        tipo de resposta que o dono quer ver."""
        liga(monkeypatch, usuario(),
             timeline(post_cru(id="8", conversation_id="7", text="continuando",
                               in_reply_to_user_id=AUTOR,
                               referenced_posts=[{"type": "replied_to",
                                                  "id": "7"}])))
        (post,) = posts_de("handle", "2026-09-01")
        assert post.tipo == "thread"
        assert post.pai_autor == "handle"

    def test_os_cinco_tipos_saem_de_uma_pagina_so(self, monkeypatch):
        """A tabela inteira atravessando `_monta`, na ordem em que chegou.

        Uma página real mistura os cinco, e o cliente não pode acertar o tipo
        do primeiro e arrastar o rótulo para os outros."""
        liga(monkeypatch, usuario(), timeline(
            post_cru(id="1", conversation_id="1", text="uma opinião qualquer"),
            post_cru(id="2", conversation_id="1", text="e outra coisa",
                     in_reply_to_user_id=AUTOR,
                     referenced_posts=[{"type": "replied_to", "id": "1"}]),
            post_cru(id="3", conversation_id="90", text="discordo",
                     in_reply_to_user_id=TERCEIRO,
                     referenced_posts=[{"type": "replied_to", "id": "90"}]),
            post_cru(id="4", conversation_id="4", text="olha isto",
                     referenced_posts=[{"type": "quoted", "id": "50"}]),
            post_cru(id="5", conversation_id="5", text="",
                     referenced_posts=[{"type": "retweeted", "id": "60"}]),
        ))
        tipos = [p.tipo for p in posts_de("handle", "2026-09-01")]
        assert tipos == ["post", "thread", "resposta", "citacao", "retweet"]


class TestTextoIntegral:
    def test_prefere_a_nota_quando_existe(self):
        cru = post_cru(text="começo truncado",
                       note_post={"text": "o texto inteiro, acima de 280"})
        assert texto_integral(cru) == "o texto inteiro, acima de 280"

    def test_aceita_o_nome_antigo(self):
        assert texto_integral(post_cru(note_tweet={"text": "inteiro"})) == (
            "inteiro")

    def test_cai_para_text_quando_a_nota_esta_vazia(self):
        # Nota presente e vazia é pior que ausente: devolver "" apagaria o
        # post inteiro em silêncio.
        assert texto_integral(post_cru(text="curto", note_post={"text": ""})) == (
            "curto")

    def test_post_sem_texto_nenhum(self):
        assert texto_integral({"id": "1"}) == ""

    def test_a_nota_chega_ao_texto_do_post(self, monkeypatch):
        """A preferência tem de sobreviver a `_monta`.

        É premissa que se perde: post longo é justamente onde a afirmação
        verificável costuma estar, e `text` truncado passaria pelo resto do
        pipeline sem sinal nenhum de que faltava metade."""
        liga(monkeypatch, usuario(), timeline(post_cru(
            id="1", conversation_id="1", text="o começo do que foi dito",
            note_post={"text": "o começo do que foi dito, e o resto que só "
                               "cabe acima de 280 caracteres"})))
        (post,) = posts_de("handle", "2026-09-01")
        assert post.texto.endswith("acima de 280 caracteres")


class TestCusto:
    def test_estimativa_por_post(self):
        assert custo_estimado_usd(100) == pytest.approx(0.5)

    def test_nao_inventa_credito_com_numero_negativo(self):
        assert custo_estimado_usd(-3) == 0.0

    def test_o_nome_e_o_docstring_dizem_que_e_estimativa(self):
        """O dono tem regra explícita: estimativa não se apresenta como
        medição. O radar lê o custo do SERVIDOR; aqui a conta é nossa."""
        assert "estimado" in custo_estimado_usd.__name__
        assert "ESTIMATIVA" in custo_estimado_usd.__doc__
        assert "TETO" in custo_estimado_usd.__doc__

    def test_bate_com_o_preco_lido_na_doc(self):
        # US$ 0,005 por recurso devolvido, doc oficial em 04/09/2026. O
        # desconto "Owned Reads" (0,001) não entra: só vale quando o :id lido
        # é o próprio usuário autenticado E dono do app.
        assert x_api.PRECO_POR_POST_USD == 0.005
        assert custo_estimado_usd(7) == pytest.approx(0.035)

    def test_a_conta_e_sobre_os_posts_que_voltaram(self, monkeypatch):
        """Fecha o ciclo: o número que se multiplica é o da leitura real."""
        liga(monkeypatch, usuario(),
             timeline(post_cru(id="1", conversation_id="1"),
                      post_cru(id="2", conversation_id="2"), next_token="p2"),
             timeline(post_cru(id="3", conversation_id="3")))
        posts = posts_de("handle", "2026-09-01", limite=100)
        assert custo_estimado_usd(len(posts)) == pytest.approx(0.015)

    def test_o_piso_de_5_por_pagina_fica_de_fora_da_conta(self, monkeypatch):
        """Com limite abaixo de 5 a estimativa sobre a lista SUBESTIMA.

        `max_results` tem piso de 5 no servidor, então pedir 2 traz 5 e cobra
        5; o corte para o limite acontece em memória e não devolve dinheiro.
        Quem contar `len(posts)` paga por 5 e estima 2. Fica registrado como
        pendência, não como bug: o módulo chama o valor de TETO por causa da
        dedup de 24h UTC, e esta é a ponta onde ele é PISO. Só importa a quem
        chamar com limite baixo — a rodada diária pede dezenas."""
        cinco = [post_cru(id=str(n), conversation_id=str(n)) for n in range(5)]
        liga(monkeypatch, usuario(), timeline(*cinco))
        posts = posts_de("handle", "2026-09-01", limite=2)
        assert len(posts) == 2
        assert custo_estimado_usd(len(posts)) == pytest.approx(0.010)
        assert custo_estimado_usd(5) == pytest.approx(0.025)


class TestSondaDeNomenclatura:
    def test_tenta_primeiro_o_conjunto_novo(self, monkeypatch):
        servidor = liga(monkeypatch, usuario(), timeline(post_cru()))
        posts_de("handle", "2026-09-01")
        assert NOVO.parametro in servidor.params
        assert ANTIGO.parametro not in servidor.params

    def test_repete_uma_vez_com_o_antigo_diante_de_400(self, monkeypatch):
        servidor = liga(monkeypatch, usuario(),
                        Resposta(400, texto="Invalid field"),
                        timeline(post_cru()))
        assert len(posts_de("handle", "2026-09-01")) == 1
        assert NOVO.parametro in servidor.chamadas[1][1]
        assert ANTIGO.parametro in servidor.chamadas[2][1]

    def test_o_vencedor_e_guardado_e_a_sonda_nao_repete(self, monkeypatch):
        liga(monkeypatch, usuario(), Resposta(400, texto="Invalid field"),
             timeline(post_cru()))
        posts_de("handle", "2026-09-01")
        assert x_api.nomenclatura_em_uso() == ANTIGO.parametro
        # Segunda rodada: só uma chamada de timeline, sem o 400 de novo.
        servidor = liga(monkeypatch, timeline(post_cru()))
        posts_de("handle", "2026-09-01")
        assert ANTIGO.parametro in servidor.params
        assert len(servidor.chamadas) == 1

    def test_vencedor_guardado_nao_e_trava(self, monkeypatch):
        """Se o dialeto guardado passar a ser recusado, o cliente cai no outro.

        O empate entre as duas páginas da X é o aviso de que ela ainda não
        decidiu; travar no que funcionou ontem seria escolher um e torcer com
        um passo a mais."""
        liga(monkeypatch, usuario(), Resposta(400, texto="Invalid field"),
             timeline())
        posts_de("handle", "2026-09-01")
        assert x_api.nomenclatura_em_uso() == ANTIGO.parametro
        servidor = liga(monkeypatch, Resposta(400, texto="Invalid field"),
                        timeline(post_cru()))
        assert len(posts_de("handle", "2026-09-01")) == 1
        assert ANTIGO.parametro in servidor.chamadas[0][1]
        assert NOVO.parametro in servidor.chamadas[1][1]
        assert x_api.nomenclatura_em_uso() == NOVO.parametro

    def test_a_sonda_nao_se_repete_a_cada_pagina(self, monkeypatch):
        """O motivo de o vencedor morar no módulo, e ele é dinheiro.

        Sem a memória, uma janela de dez páginas pagaria dez requisições
        recusadas para redescobrir a mesma coisa. O 400 acontece uma vez; da
        segunda página em diante a primeira tentativa já é a certa."""
        servidor = liga(monkeypatch, usuario(),
                        Resposta(400, texto="Invalid field"),
                        timeline(post_cru(id="1", conversation_id="1"),
                                 next_token="p2"),
                        timeline(post_cru(id="2", conversation_id="2")))
        assert len(posts_de("handle", "2026-09-01", limite=100)) == 2
        assert len(servidor.chamadas) == 4
        segunda_pagina = servidor.chamadas[3][1]
        assert ANTIGO.parametro in segunda_pagina
        assert NOVO.parametro not in segunda_pagina

    def test_400_nos_dois_nao_grava_vencedor(self, monkeypatch):
        """Um 400 pode ser `start_time` malformado, não o empate documental.

        Gravar um vencedor a partir de um erro que não é dele faria o cliente
        carregar a conclusão errada pelo resto do processo."""
        liga(monkeypatch, usuario(), Resposta(400, texto="ruim"),
             Resposta(400, texto="ruim"))
        with pytest.raises(FalhaNaAPI) as erro:
            posts_de("handle", "2026-09-01")
        assert x_api.nomenclatura_em_uso() == ""
        assert "pode não ser de nomenclatura" in str(erro.value)


class TestPostsDe:
    def test_monta_o_post_completo(self, monkeypatch):
        liga(monkeypatch, usuario(),
             timeline(post_cru(id="42", text="oi", conversation_id="42")))
        (post,) = posts_de("@handle", "2026-09-01")
        assert isinstance(post, Post)
        assert (post.id, post.autor, post.tipo) == ("42", "handle", "post")
        assert post.criado_em == "2026-09-04T12:00:00Z"
        # A URL não vem no objeto Post da API; ela se monta.
        assert post.url == "https://x.com/handle/status/42"

    def test_pede_todos_os_campos_que_a_derivacao_usa(self, monkeypatch):
        servidor = liga(monkeypatch, usuario(), timeline())
        posts_de("handle", "2026-09-01")
        pedidos = servidor.params[NOVO.parametro].split(",")
        for campo in ("id", "text", "created_at", "author_id",
                      "conversation_id", "in_reply_to_user_id",
                      NOVO.referencias, NOVO.nota):
            assert campo in pedidos

    def test_nao_expande_referenciados(self, monkeypatch):
        """Expansão traz o post referenciado como recurso adicional — outra
        cobrança. O único ganho seria o username do autor de quote e retweet,
        e o módulo assume esse vazio."""
        servidor = liga(monkeypatch, usuario(), timeline())
        posts_de("handle", "2026-09-01")
        assert "expansions" not in servidor.params

    def test_data_solta_vira_iso(self, monkeypatch):
        """`2026-09-01` é o que se digita e é o que o servidor recusa — e esse
        400 seria lido pela sonda como conflito de nomenclatura."""
        servidor = liga(monkeypatch, usuario(), timeline())
        posts_de("handle", "2026-09-01")
        assert servidor.params["start_time"] == "2026-09-01T00:00:00Z"

    def test_iso_completo_passa_intacto(self, monkeypatch):
        servidor = liga(monkeypatch, usuario(), timeline())
        posts_de("handle", "2026-09-01T13:45:00Z", ate="2026-09-02")
        assert servidor.params["start_time"] == "2026-09-01T13:45:00Z"
        assert servidor.params["end_time"] == "2026-09-02T00:00:00Z"

    def test_sem_ate_nao_manda_end_time(self, monkeypatch):
        servidor = liga(monkeypatch, usuario(), timeline())
        posts_de("handle", "2026-09-01")
        assert "end_time" not in servidor.params

    def test_max_results_respeita_o_piso_de_5(self, monkeypatch):
        servidor = liga(monkeypatch, usuario(), timeline(post_cru()))
        posts_de("handle", "2026-09-01", limite=2)
        assert servidor.params["max_results"] == 5

    def test_max_results_respeita_o_teto_de_100(self, monkeypatch):
        servidor = liga(monkeypatch, usuario(), timeline())
        posts_de("handle", "2026-09-01", limite=5000)
        assert servidor.params["max_results"] == 100

    def test_pagina_ate_o_limite(self, monkeypatch):
        primeiros = [post_cru(id=str(n), conversation_id=str(n))
                     for n in range(100)]
        servidor = liga(monkeypatch, usuario(),
                        timeline(*primeiros, next_token="abc"),
                        timeline(post_cru(id="200", conversation_id="200")))
        assert len(posts_de("handle", "2026-09-01", limite=150)) == 101
        assert servidor.chamadas[2][1]["pagination_token"] == "abc"
        # A segunda página pede só o que falta.
        assert servidor.chamadas[2][1]["max_results"] == 50

    def test_dois_lotes_viram_uma_lista_so(self, monkeypatch):
        """Contar 101 não prova que o conteúdo das duas páginas chegou.

        Aqui o teste olha os ids, e na ordem: o consumidor lê a lista de cima
        para baixo e a paginação não pode embaralhar nem comer lote."""
        liga(monkeypatch, usuario(),
             timeline(post_cru(id="1", conversation_id="1"),
                      post_cru(id="2", conversation_id="2"), next_token="p2"),
             timeline(post_cru(id="3", conversation_id="3")))
        posts = posts_de("handle", "2026-09-01", limite=100)
        assert [p.id for p in posts] == ["1", "2", "3"]

    def test_post_sem_id_nao_ganha_url_inventada(self, monkeypatch):
        """A URL é montada por nós, e é aí que dá para inventar link.

        `https://x.com/handle/status/` levaria o leitor do boletim a uma
        página que não existe. Link ausente ele percebe; link quebrado que
        parece certo, não."""
        liga(monkeypatch, usuario(),
             timeline({"text": "sem id", "author_id": AUTOR,
                       "created_at": "2026-09-04T12:00:00Z"}))
        (post,) = posts_de("handle", "2026-09-01")
        assert post.url == ""

    def test_corta_no_limite_pedido(self, monkeypatch):
        muitos = [post_cru(id=str(n), conversation_id=str(n)) for n in range(10)]
        liga(monkeypatch, usuario(), timeline(*muitos))
        assert len(posts_de("handle", "2026-09-01", limite=6)) == 6

    def test_para_sem_next_token(self, monkeypatch):
        liga(monkeypatch, usuario(), timeline(post_cru()))
        assert len(posts_de("handle", "2026-09-01", limite=100)) == 1

    def test_token_repetido_nao_gira_para_sempre(self, monkeypatch):
        """Laço que paga por página não depende da boa-fé do servidor."""
        liga(monkeypatch, usuario(),
             timeline(post_cru(id="1", conversation_id="1"), next_token="x"),
             timeline(post_cru(id="2", conversation_id="2"), next_token="x"))
        assert len(posts_de("handle", "2026-09-01", limite=100)) == 2

    def test_pagina_vazia_com_token_novo_encerra(self, monkeypatch):
        """O buraco entre as duas paradas antigas, e ele custava por volta.

        `data` vazio não faz `colhidos` crescer, então o teto do `limite`
        nunca chega; `next_token` inédito a cada volta nunca repete, então a
        outra parada nunca chega. Sem `rendeu == 0` o laço gira e paga uma
        requisição por giro — medido em 500 antes do conserto, quando a trava
        do harness disparou.

        Afirma-se o NÚMERO de requisições, não só que a função retornou: um
        laço que só para no teto de páginas também "retorna", depois de pagar
        40 vezes por página que não trazia nada."""
        torneira = Torneira(posts_por_pagina=0,
                            teto_do_harness=x_api.TETO_PAGINAS + 5)
        monkeypatch.setattr(x_api.requests, "get", torneira)
        assert posts_de("handle", "2026-09-01", limite=100) == []
        assert torneira.paginas == 1
        # A do id do handle mais UMA de timeline. O teto é o limite superior
        # que sempre vale; a igualdade é o que este conserto promete a mais.
        assert len(torneira.chamadas) <= x_api.TETO_PAGINAS + 1
        assert len(torneira.chamadas) == 2

    def test_teto_de_paginas_para_o_laco_que_o_servidor_alimenta(
            self, monkeypatch):
        """A outra parada nova, e é a única que não depende do outro lado.

        Aqui cada página RENDE um post, então `rendeu == 0` não salva, e o
        token é sempre inédito, então a repetição não salva. Só o teto salva.
        Com `limite=100` e um post por página, sem o teto seriam 100 páginas;
        com ele, 40. Este caso e o da página vazia cobrem modos de falha
        diferentes — nenhum dos dois substitui o outro."""
        torneira = Torneira(posts_por_pagina=1,
                            teto_do_harness=x_api.TETO_PAGINAS + 10)
        monkeypatch.setattr(x_api.requests, "get", torneira)
        posts = posts_de("handle", "2026-09-01", limite=100)
        assert torneira.paginas == x_api.TETO_PAGINAS
        assert len(posts) == x_api.TETO_PAGINAS
        assert len(torneira.chamadas) <= x_api.TETO_PAGINAS + 1

    def test_resposta_sem_data_nao_quebra(self, monkeypatch):
        liga(monkeypatch, usuario(), Resposta(corpo={"meta": {"result_count": 0}}))
        assert posts_de("handle", "2026-09-01") == []


class TestIdDoHandle:
    def test_le_o_id(self, monkeypatch):
        liga(monkeypatch, usuario("12345"))
        assert id_do_handle("@handle") == "12345"

    def test_cacheia(self, monkeypatch):
        servidor = liga(monkeypatch, usuario("12345"))
        id_do_handle("handle")
        id_do_handle("HANDLE")
        assert len(servidor.chamadas) == 1

    def test_handle_vazio_nao_gasta_chamada(self, monkeypatch):
        servidor = liga(monkeypatch)
        with pytest.raises(FalhaNaAPI):
            id_do_handle("@")
        assert servidor.chamadas == []

    def test_resposta_sem_id(self, monkeypatch):
        liga(monkeypatch, Resposta(corpo={"errors": [{"title": "Not Found"}]}))
        with pytest.raises(FalhaNaAPI):
            id_do_handle("handle")


class TestErros:
    def test_401_sobe_como_precisa_autorizar(self, monkeypatch):
        """Não vira FalhaNaAPI porque a ação é outra: só o humano resolve, no
        navegador. A FAQ oficial manda assumir que o token pode virar inválido
        a qualquer momento."""
        liga(monkeypatch, Resposta(401, texto="Unauthorized"))
        with pytest.raises(PrecisaAutorizar):
            id_do_handle("handle")

    def test_403_fala_de_conta_protegida_sem_afirmar(self, monkeypatch):
        """A doc nunca confirma o comportamento do endpoint com conta
        protegida — a mensagem diz "pode ser", não a causa."""
        liga(monkeypatch, Resposta(403, texto="Forbidden"))
        with pytest.raises(FalhaNaAPI) as erro:
            id_do_handle("handle")
        assert "Pode ser" in str(erro.value)
        assert "não confirma" in str(erro.value)

    def test_403_lembra_do_escopo_como_outra_causa(self, monkeypatch):
        """Mensagem útil é a que lista o que conferir.

        As duas causas plausíveis são conta protegida (NÃO CONFIRMADA) e
        escopo faltando (verificável em segundos, no console). Dizer só a
        primeira mandaria o dono investigar a que ele não pode confirmar."""
        liga(monkeypatch, Resposta(403, texto="Forbidden"))
        with pytest.raises(FalhaNaAPI) as erro:
            id_do_handle("handle")
        assert "tweet.read" in str(erro.value)
        assert "users.read" in str(erro.value)

    def test_401_na_timeline_nao_e_engolido_pela_sonda(self, monkeypatch):
        """A sonda só captura 400. Token morto tem de atravessá-la.

        Se ela capturasse qualquer falha para tentar o outro dialeto, um token
        expirado viraria "o X recusou os dois conjuntos de nomes" — e o dono
        iria caçar empate documental quando o conserto é reautorizar."""
        servidor = liga(monkeypatch, usuario(), Resposta(401, texto="expired"))
        with pytest.raises(PrecisaAutorizar):
            posts_de("handle", "2026-09-01")
        assert len(servidor.chamadas) == 2

    def test_403_na_timeline_nao_vira_segunda_tentativa(self, monkeypatch):
        """Repetir um 403 com o outro dialeto seria pagar para ouvir não duas
        vezes: nome de campo não é o que decide permissão."""
        servidor = liga(monkeypatch, usuario(), Resposta(403, texto="Forbidden"))
        with pytest.raises(FalhaNaAPI):
            posts_de("handle", "2026-09-01")
        assert len(servidor.chamadas) == 2

    def test_429_espera_o_que_o_servidor_pediu(self, monkeypatch):
        esperas = []
        liga(monkeypatch, Resposta(429, headers={"Retry-After": "17"}),
             usuario("7"))
        assert x_api._pede("/users/by/username/x", {},
                           dormir=esperas.append)["data"]["id"] == "7"
        assert esperas == [17]

    def test_429_le_o_reset_em_epoch(self, monkeypatch):
        esperas = []
        monkeypatch.setattr(x_api.time, "time", lambda: 1_000)
        liga(monkeypatch, Resposta(429, headers={"x-rate-limit-reset": "1060"}),
             usuario("7"))
        x_api._pede("/x", {}, dormir=esperas.append)
        assert esperas == [60]

    def test_429_longo_demais_nao_pendura_a_rodada(self, monkeypatch):
        liga(monkeypatch, Resposta(429, headers={"Retry-After": "900"}))
        with pytest.raises(FalhaNaAPI) as erro:
            x_api._pede("/x", {}, dormir=lambda _: None)
        assert "900s" in str(erro.value)

    def test_429_nao_espera_duas_vezes(self, monkeypatch):
        liga(monkeypatch, Resposta(429, headers={"Retry-After": "5"}),
             Resposta(429, headers={"Retry-After": "5"}))
        with pytest.raises(FalhaNaAPI):
            x_api._pede("/x", {}, dormir=lambda _: None)

    def test_5xx_repete_uma_vez(self, monkeypatch):
        esperas = []
        liga(monkeypatch, Resposta(503, texto="upstream"), usuario("7"))
        x_api._pede("/x", {}, dormir=esperas.append)
        assert esperas == [x_api.ESPERA_5XX]

    def test_5xx_duas_vezes_desiste(self, monkeypatch):
        liga(monkeypatch, Resposta(500, texto="a"), Resposta(500, texto="b"))
        with pytest.raises(FalhaNaAPI) as erro:
            x_api._pede("/x", {}, dormir=lambda _: None)
        assert "duas vezes" in str(erro.value)

    def test_falha_de_rede_gasta_a_mesma_repeticao(self, monkeypatch):
        liga(monkeypatch, x_api.requests.ConnectionError("cabo"), usuario("7"))
        assert x_api._pede("/x", {}, dormir=lambda _: None)["data"]["id"] == "7"

    def test_falha_de_rede_persistente_vira_falha(self, monkeypatch):
        liga(monkeypatch, x_api.requests.ConnectionError("cabo"),
             x_api.requests.ConnectionError("cabo"))
        with pytest.raises(FalhaNaAPI):
            x_api._pede("/x", {}, dormir=lambda _: None)

    def test_outro_4xx_nao_repete(self, monkeypatch):
        servidor = liga(monkeypatch, Resposta(404, texto="Not Found"))
        with pytest.raises(FalhaNaAPI):
            x_api._pede("/x", {}, dormir=lambda _: None)
        assert len(servidor.chamadas) == 1

    def test_corpo_200_que_nao_e_objeto(self, monkeypatch):
        """JSON válido fora do formato escaparia limpo e estouraria
        AttributeError adiante, onde ninguém captura."""
        liga(monkeypatch, Resposta(corpo=[1, 2, 3]))
        with pytest.raises(FalhaNaAPI) as erro:
            x_api._pede("/x", {}, dormir=lambda _: None)
        assert "não é objeto JSON" in str(erro.value)

    def test_corpo_200_ilegivel(self, monkeypatch):
        liga(monkeypatch, Resposta(corpo=ValueError("sem json"), texto="<html>"))
        with pytest.raises(FalhaNaAPI):
            x_api._pede("/x", {}, dormir=lambda _: None)


class TestSuperficieDeExcecao:
    """Duas exceções saem da fachada, e só duas.

    A promessa está no cabeçalho do módulo e no docstring de `posts_de`, e a
    distinção é de AÇÃO: `PrecisaAutorizar` é humano no navegador,
    `FalhaNaAPI` é rodada perdida que a próxima recupera. O risco mora em
    `token()`, que é um SEGUNDO cliente HTTP — o `x_auth` deixa falha
    passageira da renovação subir como `requests.RequestException` de
    propósito, e essa classe o adaptador do radar não tem como adivinhar."""

    def test_falha_de_rede_na_renovacao_do_token_nao_vaza(self, monkeypatch):
        """A terceira exceção, que era a que escapava.

        `token()` era chamado FORA do `try` de `_pede`: uma queda de rede na
        renovação subia como `requests.ConnectionError` por cima de
        `posts_de`, que promete duas classes. A causa fica presa no
        `__cause__` — traduzir não pode virar apagar o rastro."""
        def token_sem_rede():
            raise x_api.requests.ConnectionError("cabo arrancado")

        monkeypatch.setattr(x_api, "token", token_sem_rede)
        servidor = liga(monkeypatch)
        with pytest.raises(FalhaNaAPI) as erro:
            posts_de("handle", "2026-09-01")
        assert "ConnectionError" in str(erro.value)
        assert isinstance(erro.value.__cause__, x_api.requests.ConnectionError)
        # Sem token não se bate na porta: a falha é antes do GET.
        assert servidor.chamadas == []

    def test_precisa_autorizar_vinda_do_token_continua_subindo(self,
                                                               monkeypatch):
        """A trava contra a sobrecorreção, e ela NÃO prova o conserto.

        Passa nas duas versões, antes e depois, de propósito: o que ela
        impede é o conserto ser feito com `except Exception`, que converteria
        "reautorize no navegador" em "tente de novo amanhã" e deixaria o dono
        esperando por uma rodada que nunca voltaria sozinha.
        `PrecisaAutorizar` não é subclasse de `RequestException`, e é isso que
        mantém o `except` estreito."""
        def token_morto():
            raise PrecisaAutorizar("nenhum token do X em disco")

        monkeypatch.setattr(x_api, "token", token_morto)
        liga(monkeypatch)
        with pytest.raises(PrecisaAutorizar):
            posts_de("handle", "2026-09-01")


class TestCredencial:
    def test_o_token_vai_no_cabecalho_e_nao_no_codigo(self, monkeypatch):
        """O valor vem de `x_auth.token()`, e nada mais pode satisfazer isto.

        O assert era `.startswith("Bearer ")`, que confere o FORMATO e nada
        mais: a mutação de 04/09/2026 escreveu um Bearer literal no código e
        passou incólume. Agora `token()` devolve um valor que só existe dentro
        deste teste, e a afirmação é de IGUALDADE — nenhum literal escrito em
        `src/x_api.py` pode acertá-lo, hoje nem depois de um refactor."""
        monkeypatch.setattr(x_api, "token", lambda: "so-token()-sabe-disto")
        servidor = liga(monkeypatch, usuario())
        id_do_handle("handle")
        assert servidor.chamadas[0][2]["Authorization"] == (
            "Bearer so-token()-sabe-disto")

    def test_o_429_pega_token_novo_depois_da_espera(self, monkeypatch):
        """Depois de dormir, o cabeçalho é remontado — e é aritmética.

        `TETO_ESPERA` é 300 e `x_auth.FOLGA_SEGUNDOS` é 300, o MESMO número.
        `token()` só promete que o token cacheado tem MAIS de 300s de vida;
        dormir os 300s do teto consome a promessa inteira, e a repetição iria
        com um token que pode ter acabado de expirar. O 401 resultante sobe
        como `PrecisaAutorizar` e mandaria o dono reautorizar no navegador por
        causa de uma ESPERA, não de uma credencial ruim.

        Contar chamadas a `token()` não bastaria: quem chamasse `token()` fora
        do laço só para descartar o valor passaria. O que se compara é o
        cabeçalho VISTO em cada requisição."""
        contador = itertools.count(1)
        monkeypatch.setattr(x_api, "token", lambda: f"token-{next(contador)}")
        # 300 é o teto, e `espera > TETO_ESPERA` é falso na igualdade: espera-se.
        servidor = liga(monkeypatch, Resposta(429, headers={"Retry-After": "300"}),
                        usuario("7"))
        dormiu: list[int] = []
        x_api._pede("/users/by/username/x", {}, dormir=dormiu.append)
        assert dormiu == [x_api.TETO_ESPERA]
        vistos = [chamada[2]["Authorization"] for chamada in servidor.chamadas]
        assert vistos == ["Bearer token-1", "Bearer token-2"]

    def test_o_modulo_nao_chama_nada_no_import(self, monkeypatch):
        """Import não pode autenticar nem consultar: `python -m src.qualquer`
        carrega o pacote inteiro, e um import que fala com a rede transforma
        `--help` em requisição paga.

        Este teste era `assert nomenclatura_em_uso() == ""` e não media nada:
        a fixture autouse `sem_estado` chama `_esquece_nomenclatura()` antes
        de CADA teste, então o "" vinha dela, não do import. Provado por
        mutação em 04/09/2026 — um `requests.get` em nível de módulo passava
        incólume. A medição agora é o próprio import: proíbe-se a rede e o
        `token()`, e executa-se o FONTE de novo.

        A cópia sai de `exec_module` e NÃO entra em `sys.modules`, em vez de
        `importlib.reload`: recarregar `src.x_api` no lugar trocaria `Post` e
        `FalhaNaAPI` por classes novas, e os testes que fazem
        `isinstance(post, Post)` ou `pytest.raises(FalhaNaAPI)` passariam a
        comparar com as classes velhas importadas no topo deste arquivo."""
        def proibido(*args, **kwargs):
            raise AssertionError("o import do módulo foi à rede")

        for nome in ("get", "post", "request", "Session"):
            monkeypatch.setattr(x_api.requests, nome, proibido)
        # O `from .x_auth import token` do módulo captura o valor de AGORA:
        # proibir aqui é o que prova que o import não autentica.
        monkeypatch.setattr(x_auth, "token", proibido)

        spec = importlib.util.spec_from_file_location(
            "src._x_api_medido_pelo_teste", x_api.__file__)
        copia = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(copia)

        # Agora o "" é do import: nenhuma fixture tocou nesta cópia.
        assert copia.nomenclatura_em_uso() == ""
        assert copia._vencedor is None
        assert copia._ids == {}
