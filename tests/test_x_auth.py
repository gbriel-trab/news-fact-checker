"""O guardião do refresh token, sem rede e sem credencial.

O que se prende aqui não é o fluxo OAuth em si — é o que acontece quando ele
falha no pior instante. O refresh da X é ROTATIVO: cada renovação mata o
anterior, e entre a X emitir o novo e este processo gravá-lo existe uma janela
em que um crash deixa o dono sem credencial nenhuma e a coleta parada até
alguém abrir o navegador à mão. `TestRotacaoDoRefresh`, `TestGravacaoAtomica`,
`TestExpiresInMalformado` e `TestLockDeRenovacao` existem por causa dessa
janela, e são os blocos que não podem ser afrouxados: o resto falha numa
rodada, esses falham até o humano voltar. Os dois últimos entraram em
04/09/2026, cada um por um jeito diferente de perder a credencial que a suíte
não pegava — resposta malformada (o par novo nunca chegava ao disco) e dois
processos gastando o MESMO refresh rotativo na mesma janela de renovação.

Nenhum teste toca a rede nem o disco do projeto. `requests.post`,
`webbrowser.open` e o `HTTPServer` do callback são substituídos, e
`ARQUIVO_TOKEN` aponta para `tmp_path` em TODOS — um teste que gravasse no
caminho real sobrescreveria o token do dono com lixo de fixture.

Nenhum teste conhece credencial: `X_CLIENT_ID` é fixado com valor de mentira,
inclusive para o dia em que o .env estiver preenchido. `config` carrega o .env
no import, então sem essa fixação o teste passaria a depender de uma credencial
real — e a ler uma.
"""

import base64
import hashlib
import io
import json
import os
import subprocess
import time
import types
import urllib.parse

import pytest
import requests

from src import config, x_auth
from src.x_auth import PrecisaAutorizar

AGORA = 1_000_000.0

ESTADO = "estado-legitimo-deste-processo"


def _forja(state: str) -> str:
    """Um `state` do MESMO tamanho do verdadeiro e diferente em toda posição.

    Tamanho igual é o ponto: forjar um state mais curto testaria o
    comprimento, não o segredo."""
    return "".join("y" if c == "z" else "z" for c in state)


# Capturado no import, ANTES de a fixture apontar tudo para tmp_path: é o
# caminho de verdade, e um teste abaixo o confere contra o .gitignore.
CAMINHO_PADRAO = x_auth.ARQUIVO_TOKEN


# ------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def sem_rede_sem_navegador_sem_disco(monkeypatch, tmp_path):
    """As três amarras, ligadas em todo teste deste arquivo.

    A do disco é a que mais importa: `_guarda` grava no `ARQUIVO_TOKEN` de
    módulo, sem receber caminho, então um teste que esquecesse de redirecionar
    escreveria em `data/x_token.json` — o arquivo do dono, com o refresh vivo
    dentro."""
    monkeypatch.setattr(x_auth, "ARQUIVO_TOKEN", tmp_path / "x_token.json")
    monkeypatch.setattr(x_auth.requests, "post", _proibido)
    monkeypatch.setattr(x_auth.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(x_auth.http.server, "HTTPServer", _PortaProibida)
    monkeypatch.setenv("X_CLIENT_ID", "client-id-de-mentira")
    monkeypatch.delenv("X_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("X_REDIRECT_URI", raising=False)


def _proibido(*args, **kwargs):
    raise AssertionError("o teste tentou falar com a rede")


class _PortaProibida:
    """Barra o `HTTPServer` em todo teste que não o substituir de propósito.

    Não é zelo: com o `_cliente()` quebrado numa mutação, `autoriza()` seguiu
    até aqui e a suíte ficou 300 segundos pendurada numa porta de verdade,
    esperando um humano que não existe. AssertionError e não OSError porque
    `_espera_callback` converte OSError em "porta ocupada" e engoliria o
    aviso."""

    def __init__(self, endereco, handler):
        raise AssertionError(f"o teste tentou abrir a porta {endereco}")


@pytest.fixture
def relogio(monkeypatch):
    """Congela o relógio. A folga é uma janela de 5 minutos, e um teste de
    fronteira que dependesse de quanto o próprio teste demorou mediria outra
    coisa. `x_auth.time` É o módulo `time`, então isto também congela o
    `time.time()` usado pelos ajudantes daqui."""
    monkeypatch.setattr(x_auth.time, "time", lambda: AGORA)
    return AGORA


@pytest.fixture
def cache(relogio):
    """O arquivo de token que o dono já teria em disco.

    Depende de `relogio` para fixar a ordem: se o arquivo fosse escrito com o
    relógio real e lido com o congelado, `expira_em` mediria de um a outro."""
    return escreve_cache(x_auth.ARQUIVO_TOKEN)


# -------------------------------------------------------------- dublês


class Resposta:
    """O mínimo de `requests.Response` que o módulo usa."""

    def __init__(self, status=200, corpo=None, texto=""):
        self.status_code = status
        self._corpo = {} if corpo is None else corpo
        self.text = texto or str(self._corpo)

    def json(self):
        if isinstance(self._corpo, Exception):
            raise self._corpo
        return self._corpo

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class Endpoint:
    """Fila de respostas do endpoint de token + registro do que foi enviado."""

    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.corpos: list[dict] = []
        self.cabecalhos: list[dict] = []

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.corpos.append(dict(data or {}))
        self.cabecalhos.append(dict(headers or {}))
        if not self.respostas:
            raise AssertionError(f"chamada a mais ao endpoint de token: {url}")
        item = self.respostas.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def corpo(self) -> dict:
        return self.corpos[-1]


def liga(monkeypatch, *respostas) -> Endpoint:
    endpoint = Endpoint(*respostas)
    monkeypatch.setattr(x_auth.requests, "post", endpoint)
    return endpoint


def par_novo(access="acesso-novo", refresh="refresh-novo", expira_em_s=7200,
             escopos=x_auth.ESCOPOS) -> Resposta:
    corpo = {"access_token": access, "expires_in": expira_em_s,
             "scope": escopos}
    if refresh is not None:
        corpo["refresh_token"] = refresh
    return Resposta(corpo=corpo)


def escreve_cache(caminho, access="acesso-velho", refresh="refresh-velho",
                  faltam=3600):
    dados = {"access_token": access, "refresh_token": refresh,
             "expira_em": time.time() + faltam, "escopos": x_auth.ESCOPOS,
             "obtido_em": "2026-09-04T10:00:00-0300"}
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(dados), encoding="utf-8")
    return caminho


def lido(caminho) -> dict:
    return json.loads(caminho.read_text(encoding="utf-8"))


def lock_atual():
    """Onde `_lock_renovacao` vai criar o arquivo, DERIVADO de ARQUIVO_TOKEN.

    Derivado e não escrito à mão porque é assim que o módulo o monta: o lock é
    irmão do token para o `.gitignore` cobri-lo com o mesmo padrão do `.tmp` e
    para a fixture que redireciona `ARQUIVO_TOKEN` levar o lock junto — sem
    isso a suíte criaria lock no `data/` de verdade do dono."""
    return x_auth.ARQUIVO_TOKEN.with_name(x_auth.ARQUIVO_TOKEN.name + ".lock")


def consente(monkeypatch, *respostas, volta=None) -> tuple[str, Endpoint]:
    """Roda `autoriza()` inteiro sem navegador, sem socket e sem rede.

    O callback é fabricado a partir da PRÓPRIA URL de autorização, porque o
    `state` nasce dentro de `autoriza` e a volta legítima é exatamente a que o
    devolve igual. `volta` sobrescreve campos — é assim que se forja o callback
    do teste de CSRF."""
    urls: list[str] = []
    monkeypatch.setattr(x_auth.webbrowser, "open", urls.append)
    endpoint = liga(monkeypatch, *respostas)

    def callback(destino, espera=x_auth.ESPERA_CONSENTIMENTO):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(urls[-1]).query)
        devolve = {"code": "code-de-mentira", "state": query["state"][0]}
        for chave, valor in (volta or {}).items():
            # Valor que é função recebe o legítimo e devolve o adulterado: é
            # como se forja um `state` do mesmo tamanho do que `autoriza`
            # acabou de sortear, sem o teste conhecê-lo de antemão.
            devolve[chave] = (valor(devolve.get(chave, ""))
                             if callable(valor) else valor)
        return devolve

    monkeypatch.setattr(x_auth, "_espera_callback", callback)
    x_auth.autoriza()
    return urls[-1], endpoint


def query_de(url: str) -> dict:
    return {k: v[0] for k, v in
            urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


class Volta(x_auth._Callback):
    """O handler sem socket.

    `BaseHTTPRequestHandler.__init__` já ATENDE a requisição — instanciá-lo de
    verdade exigiria uma conexão aberta. Aqui só o que `do_GET` toca existe."""

    def __init__(self, path):
        self.path = path
        self.server = types.SimpleNamespace(resultado=None)
        self.codigos: list[int] = []
        self.wfile = io.BytesIO()

    def send_response(self, codigo):
        self.codigos.append(codigo)

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass


# ---------------------------------------------------------------- PKCE


class TestPKCE:
    """O challenge é o que impede que um `code` interceptado vire token.

    Errar o encoding aqui não quebra nada visível: a X recusa o challenge e o
    erro aparece como "credencial recusada", longe da causa."""

    def test_o_desafio_e_o_sha256_do_verifier(self):
        """Vetor calculado aqui, por um caminho DIFERENTE do da implementação.

        Repetir `urlsafe_b64encode` no teste provaria apenas que a linha é
        igual a si mesma; o base64 padrão com a troca do alfabeto à mão é uma
        segunda derivação do mesmo valor."""
        verificador = "verificador-de-teste-com-mais-de-43-caracteres-por-rfc"
        resumo = hashlib.sha256(verificador.encode("ascii")).digest()
        esperado = (base64.b64encode(resumo).decode("ascii")
                    .replace("+", "-").replace("/", "_").rstrip("="))
        assert x_auth._desafio(verificador) == esperado

    def test_o_desafio_decodifica_de_volta_no_resumo(self):
        verificador = x_auth._verificador()
        desafio = x_auth._desafio(verificador)
        faltando = "=" * (-len(desafio) % 4)
        assert base64.urlsafe_b64decode(desafio + faltando) == (
            hashlib.sha256(verificador.encode("ascii")).digest())

    def test_sem_padding(self):
        """`=` é reservado em query string: cru a X recusa o challenge, e
        escapado muda o valor comparado do outro lado."""
        assert "=" not in x_auth._desafio(x_auth._verificador())
        # 32 bytes de SHA-256 viram 43 caracteres base64 sem o padding.
        assert len(x_auth._desafio("qualquer")) == 43

    def test_usa_o_alfabeto_urlsafe(self):
        """Procura um verifier cujo resumo caia nos dois caracteres que o
        base64 padrão usa e a URL não aceita crus.

        Sem essa busca o teste passaria por sorte com quase qualquer entrada —
        a maioria dos resumos não produz `+` nem `/`."""
        for n in range(500):
            candidato = f"verificador-{n}"
            resumo = hashlib.sha256(candidato.encode("ascii")).digest()
            if set("+/") & set(base64.b64encode(resumo).decode("ascii")):
                break
        else:
            pytest.fail("nenhum resumo com + ou / em 500 tentativas")
        desafio = x_auth._desafio(candidato)
        assert "+" not in desafio and "/" not in desafio

    def test_o_verifier_nao_se_repete(self):
        """Verifier previsível é PKCE de enfeite: quem intercepta o `code`
        deriva o verifier da saída anterior e troca o code por token."""
        assert x_auth._verificador() != x_auth._verificador()

    def test_o_verifier_cabe_no_que_a_rfc_7636_exige(self):
        verificador = x_auth._verificador()
        assert 43 <= len(verificador) <= 128
        permitido = set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789-._~")
        assert set(verificador) <= permitido

    def test_o_par_da_autorizacao_e_o_mesmo(self, monkeypatch):
        """O elo que os testes isolados não pegam: o challenge que foi para a
        URL tem de ser o do verifier que vai no corpo da troca.

        Uma implementação que gerasse dois verifiers independentes passaria em
        tudo acima e falharia na primeira troca real, com um erro da X que não
        diz isso."""
        url, endpoint = consente(monkeypatch, par_novo())
        assert x_auth._desafio(endpoint.corpo["code_verifier"]) == (
            query_de(url)["code_challenge"])
        assert query_de(url)["code_challenge_method"] == "S256"

    def test_o_verifier_nao_vai_na_url(self, monkeypatch):
        """Se o segredo viajasse junto com o challenge, o PKCE não protegeria
        de nada — quem lê a URL teria os dois lados."""
        url, endpoint = consente(monkeypatch, par_novo())
        assert endpoint.corpo["code_verifier"] not in url


# --------------------------------------------------------------- state


class TestState:
    """A barreira de CSRF. Sem ela um link forjado entrega a este servidor um
    `code` de OUTRA conta, e o processo grava feliz um token que não é do
    dono — a coleta passaria a ler a timeline de quem forjou."""

    def test_state_igual_devolve_o_code(self):
        assert x_auth._confere_callback(
            {"code": "abc", "state": ESTADO}, ESTADO) == "abc"

    def test_state_diferente_e_barrado(self):
        """O forjado tem o MESMO tamanho do verdadeiro.

        Com um state mais curto, uma comparação que olhasse só o comprimento
        passaria neste teste — e foi o que aconteceu na primeira versão dele,
        descoberto trocando `compare_digest` por `len(...) != len(...)` no
        módulo. O que se prende aqui é o CONTEÚDO."""
        with pytest.raises(PrecisaAutorizar) as erro:
            x_auth._confere_callback(
                {"code": "abc", "state": _forja(ESTADO)}, ESTADO)
        assert "forjada" in str(erro.value)
        assert "Nada foi gravado" in str(erro.value)

    def test_state_ausente_e_barrado(self):
        with pytest.raises(PrecisaAutorizar):
            x_auth._confere_callback({"code": "abc"}, ESTADO)

    def test_state_com_acento_e_barrado_e_nao_estoura(self):
        """`compare_digest` com `str` levanta TypeError diante de um caractere
        fora do ASCII, e o state recebido vem da REDE. O state legítimo é
        sempre `token_urlsafe`, então acento aqui só existe em callback
        forjado — e forjado tem de ser barrado, não derrubar o processo com um
        erro que não diz nada."""
        with pytest.raises(PrecisaAutorizar):
            x_auth._confere_callback(
                {"code": "abc", "state": "ç" + _forja(ESTADO)[1:]}, ESTADO)

    def test_erro_da_x_vem_antes_da_comparacao(self):
        erro_x = {"error": "access_denied",
                  "error_description": "o usuário recusou"}
        with pytest.raises(PrecisaAutorizar) as erro:
            x_auth._confere_callback(erro_x, ESTADO)
        assert "access_denied" in str(erro.value)
        assert "o usuário recusou" in str(erro.value)

    def test_callback_sem_code(self):
        with pytest.raises(PrecisaAutorizar):
            x_auth._confere_callback({"state": ESTADO}, ESTADO)

    def test_a_autorizacao_recusa_callback_de_outro_state(self, monkeypatch):
        """Fim a fim: com o state trocado, `autoriza` não chega a trocar o
        code por token (o endpoint sem respostas na fila estouraria) e não
        deixa arquivo nenhum para trás.

        `forja` mantém o tamanho do state real pelo mesmo motivo do teste
        acima."""
        with pytest.raises(PrecisaAutorizar):
            consente(monkeypatch, volta={"state": _forja})
        assert not x_auth.ARQUIVO_TOKEN.exists()

    def test_state_novo_a_cada_consentimento(self, monkeypatch):
        primeira, _ = consente(monkeypatch, par_novo())
        segunda, _ = consente(monkeypatch, par_novo())
        assert query_de(primeira)["state"] != query_de(segunda)["state"]


# ------------------------------------------------------ rotação do refresh


class TestRotacaoDoRefresh:
    """O bloco mais importante do arquivo.

    "Refresh tokens rotate - always save the newest one": no instante em que a
    X responde, o refresh que está no disco JÁ morreu do lado dela. Perder o
    novo custa reautorização manual — e num job agendado isso é a coleta parada
    até alguém perceber."""

    def test_grava_o_refresh_novo(self, monkeypatch, cache, relogio):
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, par_novo(refresh="refresh-2"))
        x_auth.token()
        assert lido(cache)["refresh_token"] == "refresh-2"

    def test_o_refresh_velho_some_do_disco(self, monkeypatch, cache, relogio):
        """Guardar os dois seria pior que guardar o errado: na renovação
        seguinte não haveria como saber qual está vivo."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, par_novo(refresh="refresh-2"))
        x_auth.token()
        assert "refresh-velho" not in cache.read_text(encoding="utf-8")

    def test_manda_o_refresh_do_disco_no_corpo(self, monkeypatch, cache,
                                               relogio):
        escreve_cache(cache, faltam=60)
        endpoint = liga(monkeypatch, par_novo())
        x_auth.token()
        assert endpoint.corpo["grant_type"] == "refresh_token"
        assert endpoint.corpo["refresh_token"] == "refresh-velho"

    def test_morrer_depois_de_gravar_deixa_o_refresh_valido(
            self, monkeypatch, cache, relogio):
        """O caso que justifica gravar ANTES de devolver.

        O processo morre entre o `os.replace` e o `return`. O access token
        daquela rodada se perde — aceito —, mas o refresh que ficou no disco é
        o NOVO, e a rodada seguinte renova com ele. Se a ordem fosse a inversa,
        o disco guardaria um refresh que a X já matou e a única saída seria o
        navegador."""
        escreve_cache(cache, faltam=60)

        class Morreu(Exception):
            pass

        grava = x_auth._grava

        def grava_e_morre(dados, caminho=None):
            grava(dados, caminho)
            raise Morreu("processo morto entre gravar e devolver")

        monkeypatch.setattr(x_auth, "_grava", grava_e_morre)
        liga(monkeypatch, par_novo(access="acesso-2", refresh="refresh-2",
                                   expira_em_s=60))
        with pytest.raises(Morreu):
            x_auth.token()
        assert lido(cache)["refresh_token"] == "refresh-2"

        # A rodada seguinte, com o processo de novo em pé, renova com o que
        # sobreviveu ao crash.
        monkeypatch.setattr(x_auth, "_grava", grava)
        endpoint = liga(monkeypatch, par_novo(access="acesso-3",
                                              refresh="refresh-3"))
        assert x_auth.token() == "acesso-3"
        assert endpoint.corpo["refresh_token"] == "refresh-2"

    def test_gravacao_que_falha_nao_devolve_token(self, monkeypatch, cache,
                                                  relogio):
        """Uma rodada perdida com aviso é melhor que uma rodada bem-sucedida
        que deixa o dia seguinte sem credencial nenhuma.

        O disco fica com um refresh que a X JÁ matou — a janela é irredutível
        num cliente sem transação distribuída, e o módulo diz isso. O que este
        teste prende é que a perda seja BARULHENTA: o OSError sobe, ninguém
        sai lendo timeline achando que está tudo certo."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, par_novo(refresh="refresh-2"))
        monkeypatch.setattr(x_auth.os, "replace", _explode)
        with pytest.raises(OSError):
            x_auth.token()
        assert lido(cache)["refresh_token"] == "refresh-velho"

    def test_resposta_sem_refresh_preserva_o_antigo(self, monkeypatch, cache,
                                                    relogio):
        """A doc diz que a rotação é a regra, mas se algum caminho devolver sem
        `refresh_token`, sobrescrever com vazio apagaria o único token de
        renovação que existe. Manter o antigo custa, no pior caso, um 4xx na
        renovação seguinte — que já é tratado."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, par_novo(refresh=None))
        x_auth.token()
        assert lido(cache)["refresh_token"] == "refresh-velho"

    def test_a_renovacao_devolve_o_access_novo(self, monkeypatch, cache,
                                               relogio):
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, par_novo(access="acesso-2"))
        assert x_auth.token() == "acesso-2"

    def test_guarda_a_expiracao_em_epoch_absoluto(self, monkeypatch, cache,
                                                  relogio):
        """`expires_in` é relativo ao instante da resposta, e esse instante
        some quando o processo morre."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, par_novo(expira_em_s=7200))
        x_auth.token()
        assert lido(cache)["expira_em"] == AGORA + 7200


# ------------------------------------------------------ expires_in malformado


# Cada um destes é `expires_in` fora do formato prometido, e os quatro últimos
# estouravam `float()` ANTES do `_grava` — o par novo se perdia inteiro.
# `None` e `{}` são falsos e já caíam no `or 7200`; ficam na lista porque a
# correção não pode ter quebrado o caminho que já funcionava.
EXPIRES_IN_TORTOS = [
    pytest.param("7200 seconds", id="numero-com-unidade"),
    pytest.param(None, id="nulo"),
    pytest.param({}, id="objeto-vazio"),
    pytest.param({"value": 7200}, id="objeto"),
    pytest.param([7200], id="lista"),
    pytest.param("abc", id="texto"),
    pytest.param(object(), id="objeto-qualquer"),
]


class TestExpiresInMalformado:
    """O pior caso do arquivo: perder a credencial por resposta malformada.

    No instante em que a X responde, o refresh que está no disco JÁ morreu do
    lado dela. Se o `float(expires_in)` estourar antes do `_grava`, o par NOVO
    nunca toca o disco e o VELHO já não vale — o dono acorda com a coleta
    parada e a única saída é o navegador. Um `expira_em` errado custa, no
    máximo, uma renovação cedo demais ou um 401 que já é tratado; a exceção
    custa a credencial inteira. É por isso que aqui se cai para o padrão em
    vez de estourar."""

    @pytest.mark.parametrize("bruto", EXPIRES_IN_TORTOS)
    def test_o_refresh_novo_chega_ao_disco_mesmo_assim(self, monkeypatch,
                                                       cache, relogio, bruto):
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, par_novo(access="acesso-2", refresh="refresh-2",
                                   expira_em_s=bruto))
        assert x_auth.token() == "acesso-2"
        guardado = lido(cache)
        assert guardado["refresh_token"] == "refresh-2"
        assert guardado["expira_em"] == AGORA + 7200

    @pytest.mark.parametrize("bruto", EXPIRES_IN_TORTOS)
    def test_guarda_nao_estoura_com_o_que_veio_da_rede(self, relogio, bruto):
        """O mesmo caso um nível abaixo, sem o `token()` em volta.

        `_guarda` é onde o defeito morava; prendê-lo aqui é o que impede que
        alguém reintroduza o `float` cru dentro do literal do dicionário e só
        descubra no dia em que a X responder torto."""
        novo = x_auth._guarda(
            {"access_token": "a", "refresh_token": "r", "expires_in": bruto},
            {})
        assert novo["refresh_token"] == "r"
        assert novo["expira_em"] == AGORA + 7200

    @pytest.mark.parametrize("bruto,segundos", [
        (60, 60), ("3600", 3600), (7200.5, 7200.5), ("120.5", 120.5)])
    def test_expires_in_legivel_nao_cai_no_padrao(self, monkeypatch, cache,
                                                  relogio, bruto, segundos):
        """O controle contra a sobrecorreção, que é o risco real deste
        conserto: um `except` largo demais engoliria TODO valor e faria o
        módulo achar que qualquer token dura duas horas. Número, string
        numérica e float têm de chegar como são."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, par_novo(expira_em_s=bruto))
        x_auth.token()
        assert lido(cache)["expira_em"] == AGORA + segundos

    def test_a_rodada_seguinte_renova_com_o_refresh_que_sobreviveu(
            self, monkeypatch, cache, relogio):
        """A consequência que o teste de disco não mostra sozinho: gravar o
        par novo só vale se ele for utilizável depois. Aqui a resposta torta
        vem primeiro e a rodada seguinte tem de renovar com o refresh que ela
        deixou — sem isso, "gravou" seria uma vitória de papel."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, par_novo(refresh="refresh-2",
                                   expira_em_s="7200 seconds"))
        x_auth.token()

        escreve_cache(cache, access=lido(cache)["access_token"],
                      refresh=lido(cache)["refresh_token"], faltam=60)
        endpoint = liga(monkeypatch, par_novo(access="acesso-3"))
        assert x_auth.token() == "acesso-3"
        assert endpoint.corpo["refresh_token"] == "refresh-2"


# --------------------------------------------------------- gravação atômica


def _explode(*args, **kwargs):
    raise OSError("disco cheio")


class TestGravacaoAtomica:
    """O arquivo final ou é o antigo inteiro ou o novo inteiro.

    Meio arquivo é indistinguível de arquivo ausente para `_le`, e ausente
    significa reautorizar à mão."""

    def test_falha_no_meio_da_escrita_nao_corrompe_o_antigo(
            self, monkeypatch, cache):
        def meia_escrita(dados, saida, **kwargs):
            saida.write('{"access_token": "aces')
            raise OSError("disco cheio no meio da escrita")

        monkeypatch.setattr(x_auth.json, "dump", meia_escrita)
        with pytest.raises(OSError):
            x_auth._grava({"access_token": "novo"}, cache)
        assert lido(cache)["access_token"] == "acesso-velho"

    def test_o_destino_so_muda_no_replace(self, monkeypatch, cache):
        """Enquanto o conteúdo novo está sendo escrito, o destino ainda é o
        antigo — é isso que o temporário no mesmo diretório compra."""
        dump = x_auth.json.dump
        visto = []

        def espia(dados, saida, **kwargs):
            visto.append(cache.read_text(encoding="utf-8"))
            dump(dados, saida, **kwargs)

        monkeypatch.setattr(x_auth.json, "dump", espia)
        x_auth._grava({"access_token": "acesso-2"}, cache)
        assert "acesso-velho" in visto[0]
        assert lido(cache)["access_token"] == "acesso-2"

    def test_replace_que_falha_deixa_o_antigo_intacto(self, monkeypatch,
                                                      cache):
        monkeypatch.setattr(x_auth.os, "replace", _explode)
        with pytest.raises(OSError):
            x_auth._grava({"access_token": "acesso-2"}, cache)
        assert lido(cache)["access_token"] == "acesso-velho"

    def test_o_temporario_fica_no_mesmo_diretorio(self, monkeypatch, cache):
        """`os.replace` só é atômico dentro do mesmo volume. Temporário em
        `%TEMP%` viraria uma cópia entre discos, que pode ser interrompida no
        meio — exatamente o que este arquivo existe para evitar."""
        onde = []
        monkeypatch.setattr(x_auth.os, "replace",
                            lambda origem, destino: onde.append(origem))
        x_auth._grava({"access_token": "acesso-2"}, cache)
        assert x_auth.Path(onde[0]).parent == cache.parent

    def test_temporario_orfao_nao_atrapalha(self, cache):
        """Uma gravação que morreu no meio deixa o `.tmp` para trás. O `O_TRUNC`
        é o que impede que a gravação seguinte grude conteúdo novo no resto do
        antigo e produza um JSON que não abre."""
        cache.with_name(cache.name + ".tmp").write_text(
            "lixo de uma tentativa anterior" * 50, encoding="utf-8")
        x_auth._grava({"access_token": "acesso-2"}, cache)
        assert lido(cache) == {"access_token": "acesso-2"}

    def test_roundtrip(self, tmp_path):
        alvo = tmp_path / "sub" / "x_token.json"
        dados = {"access_token": "a", "refresh_token": "r", "expira_em": 1.5}
        x_auth._grava(dados, alvo)
        assert x_auth._le(alvo) == dados

    def test_arquivo_corrompido_e_tratado_como_ausente(self, cache):
        """Estourar exceção crua dentro do job agendado seria pior: pedir
        reautorização é recuperável à mão."""
        cache.write_text("{isto não é json", encoding="utf-8")
        assert x_auth._le(cache) == {}

    def test_json_que_nao_e_objeto_e_tratado_como_ausente(self, cache):
        cache.write_text("[1, 2, 3]", encoding="utf-8")
        assert x_auth._le(cache) == {}

    def test_arquivo_inexistente(self, tmp_path):
        assert x_auth._le(tmp_path / "nao-existe.json") == {}


# ---------------------------------------------------------------- folga


class TestFolga:
    """Devolver um token que expira em 40 segundos é convidar o 401 no meio da
    paginação, com o job já tendo PAGO pelas páginas anteriores."""

    def test_token_de_uma_hora_nao_chama_a_rede(self, cache, relogio):
        escreve_cache(cache, faltam=3600)
        # `requests.post` continua sendo `_proibido`: qualquer chamada estoura.
        assert x_auth.token() == "acesso-velho"

    def test_token_que_expira_em_dois_minutos_e_renovado(self, monkeypatch,
                                                         cache, relogio):
        escreve_cache(cache, faltam=120)
        liga(monkeypatch, par_novo(access="acesso-2"))
        assert x_auth.token() == "acesso-2"

    def test_token_ja_expirado_e_renovado(self, monkeypatch, cache, relogio):
        escreve_cache(cache, faltam=-10)
        liga(monkeypatch, par_novo(access="acesso-2"))
        assert x_auth.token() == "acesso-2"

    def test_a_fronteira_da_folga(self, monkeypatch, cache, relogio):
        """Um segundo de cada lado de FOLGA_SEGUNDOS. Fronteira testada porque
        `<=` no lugar de `<` aqui não quebra nenhum outro teste."""
        escreve_cache(cache, faltam=x_auth.FOLGA_SEGUNDOS + 1)
        assert x_auth.token() == "acesso-velho"
        escreve_cache(cache, faltam=x_auth.FOLGA_SEGUNDOS - 1)
        liga(monkeypatch, par_novo(access="acesso-2"))
        assert x_auth.token() == "acesso-2"

    def test_expiracao_ilegivel_renova(self, monkeypatch, cache):
        """Cache escrito por uma versão anterior, ou editado à mão. Confiar num
        campo que não é número faria o job devolver um token morto."""
        cache.write_text(json.dumps(
            {"access_token": "a", "refresh_token": "r",
             "expira_em": "amanhã"}), encoding="utf-8")
        liga(monkeypatch, par_novo(access="acesso-2"))
        assert x_auth.token() == "acesso-2"

    def test_sem_arquivo_pede_o_consentimento_humano(self):
        with pytest.raises(PrecisaAutorizar) as erro:
            x_auth.token()
        assert "-m src.x_auth" in str(erro.value)

    def test_arquivo_sem_access_token_pede_consentimento(self, cache):
        cache.write_text(json.dumps({"refresh_token": "r"}), encoding="utf-8")
        with pytest.raises(PrecisaAutorizar):
            x_auth.token()


# ---------------------------------------------------- lock de renovação


class TestLockDeRenovacao:
    """Dois processos na mesma janela gastam UM refresh só.

    O refresh da X é ROTATIVO, e é isso que torna a corrida cara: dois
    processos que leem o MESMO refresh o gastam duas vezes, e o segundo grava
    por cima um par que a X já invalidou — o disco fica com credencial morta e
    a saída é o navegador. Não é hipótese: a tarefa `checador-coleta` roda de
    15 em 15 minutos e cai exatamente em cima do que o dono rodar à mão.

    A outra metade do bloco é o inverso: um lock que possa pendurar o job para
    sempre é PIOR que a corrida que ele evita. A corrida custa uma renovação
    perdida de vez em quando; o bloqueio custa a coleta inteira, calada, até
    alguém olhar."""

    def test_duas_rodadas_seguidas_gastam_uma_renovacao_so(
            self, monkeypatch, cache, relogio):
        """O caso do dia a dia: a segunda chamada acha em disco o par que a
        primeira gravou. A fila tem UMA resposta — uma segunda ida à rede
        estoura no dublê."""
        escreve_cache(cache, faltam=60)
        endpoint = liga(monkeypatch, par_novo(access="acesso-2",
                                              refresh="refresh-2"))
        assert x_auth.token() == "acesso-2"
        assert x_auth.token() == "acesso-2"
        assert len(endpoint.corpos) == 1

    def test_quem_espera_o_lock_nao_queima_o_refresh_de_novo(
            self, monkeypatch, cache, relogio):
        """O teste que importa, e o único que prende a RELEITURA.

        O lock já está na mão de outro processo. Enquanto se espera, ele
        termina: grava o par novo e solta. Quem estava esperando entra, relê o
        disco, encontra token válido e devolve SEM ir à rede — a fila do
        endpoint está vazia de propósito, então qualquer requisição estoura.

        O lock sozinho não resolveria nada aqui: sem a releitura ele apenas
        enfileiraria as duas renovações, e a segunda queimaria um refresh que
        a primeira acabou de aposentar."""
        escreve_cache(cache, faltam=60)
        endpoint = liga(monkeypatch)
        lock = lock_atual()
        lock.write_text("outro processo, vivo", encoding="utf-8")

        def o_outro_termina(_segundos):
            escreve_cache(cache, access="acesso-do-outro",
                          refresh="refresh-do-outro", faltam=7200)
            lock.unlink()

        # A espera do lock é o gancho: quando este processo dorme, o outro
        # conclui. É a única forma de simular a concorrência sem thread — e
        # thread aqui traria a flutuação que o `relogio` existe para tirar.
        monkeypatch.setattr(x_auth.time, "sleep", o_outro_termina)
        assert x_auth.token() == "acesso-do-outro"
        assert endpoint.corpos == []
        assert not lock.exists()

    def test_renovacao_normal_nao_deixa_lock_para_tras(self, monkeypatch,
                                                       cache, relogio):
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, par_novo(access="acesso-2"))
        assert x_auth.token() == "acesso-2"
        assert not lock_atual().exists()

    def test_renovacao_que_falha_solta_o_lock(self, monkeypatch, cache,
                                              relogio):
        """`finally`, e não "solta no fim": um erro tratado lá em cima viraria
        um órfão de dois minutos para a rodada seguinte."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, Resposta(400, texto="invalid_grant"))
        with pytest.raises(PrecisaAutorizar):
            x_auth.token()
        assert not lock_atual().exists()

    def test_lock_ocupado_por_processo_vivo_segue_sem_ele_e_avisa(
            self, monkeypatch, capsys):
        """FALHA PARA A FRENTE. Esgotado o prazo, segue SEM o lock em vez de
        bloquear, e diz isso no stderr — um `while` sem limite penduraria o
        job agendado para sempre, calado.

        E não apaga o lock alheio: o dono dele está vivo, e apagá-lo devolve
        exatamente a corrida que o lock existe para impedir.

        O teto de voltas existe para que a mutação "espera sem limite" caia
        VERMELHA em vez de pendurar a suíte: um teste que trava não é um teste
        que pega o defeito, é um teste que some do relatório."""
        lock = lock_atual()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("9999 processo vivo", encoding="utf-8")
        dorme = time.sleep
        voltas: list = []

        def conta(segundos):
            voltas.append(segundos)
            if len(voltas) > 20:
                raise AssertionError(
                    "a espera do lock não tem fim — o job agendado ficaria "
                    "pendurado, calado, até alguém olhar")
            dorme(segundos)

        monkeypatch.setattr(x_auth.time, "sleep", conta)
        inicio = time.monotonic()
        with x_auth._lock_renovacao(espera=0.5) as tem:
            assert tem is False
        gasto = time.monotonic() - inicio
        # Folgado de propósito: o que se prende é que TERMINA, não quanto
        # demora. Máquina carregada não pode virar teste vermelho.
        assert gasto < 10
        assert voltas and set(voltas) == {x_auth.LOCK_INTERVALO}
        assert lock.exists()
        assert "AVISO" in capsys.readouterr().err

    def test_lock_orfao_nao_trava_o_job_para_sempre(self, monkeypatch, cache,
                                                    relogio):
        """Processo morto (Ctrl+C, máquina desligada, tarefa cancelada no
        meio) deixa o arquivo para trás. Sem a regra de idade ele barraria a
        renovação para SEMPRE, e o conserto seria pior que o defeito."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, par_novo(access="acesso-2"))
        lock = lock_atual()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("1234 processo morto", encoding="utf-8")
        velho = AGORA - x_auth.LOCK_VELHO - 60
        os.utime(lock, (velho, velho))
        # Se dormisse, o órfão não teria sido reconhecido: o caminho certo é
        # apagar e tentar de novo na mesma volta, sem esperar nada.
        monkeypatch.setattr(x_auth.time, "sleep", _dormiu_a_toa)
        assert x_auth.token() == "acesso-2"
        assert not lock.exists()

    def test_lock_recem_criado_nao_e_orfao(self, relogio):
        """A fronteira do outro lado, e é a que custa caro se ceder: tratar
        lock novo como órfão apaga o de um processo VIVO e devolve a corrida
        inteira. Uma mutação para `idade > 0` passa em tudo acima e cai
        aqui."""
        lock = lock_atual()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("4321 processo vivo", encoding="utf-8")
        os.utime(lock, (AGORA, AGORA))
        assert x_auth._descarta_lock_velho(lock) is False
        assert lock.exists()

    def test_a_fronteira_da_idade_do_orfao(self, relogio):
        """Um segundo de cada lado de LOCK_VELHO. `<=` no lugar de `<` aqui
        não quebra nenhum outro teste."""
        lock = lock_atual()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("x", encoding="utf-8")
        novo = AGORA - x_auth.LOCK_VELHO + 1
        os.utime(lock, (novo, novo))
        assert x_auth._descarta_lock_velho(lock) is False
        antigo = AGORA - x_auth.LOCK_VELHO - 1
        os.utime(lock, (antigo, antigo))
        assert x_auth._descarta_lock_velho(lock) is True
        assert not lock.exists()

    def test_o_lock_nasce_irmao_do_token_e_dentro_do_tmp(self, tmp_path):
        """O lock é DERIVADO de `ARQUIVO_TOKEN`, não constante de módulo
        montada sobre `config.DIR_DADOS`. Se fosse constante, a fixture que
        redireciona só o token faria a suíte criar lock no `data/` de verdade
        do dono — e o invariante deste arquivo é não tocar o disco do
        projeto."""
        with x_auth._lock_renovacao() as tem:
            assert tem is True
            criados = sorted(p.name for p in tmp_path.iterdir())
        assert criados == ["x_token.json.lock"]
        assert sorted(p.name for p in tmp_path.iterdir()) == []

    def test_o_lock_diz_de_quem_e(self, tmp_path):
        """Um zero-byte não diz de qual processo sobrou, e é isso que o humano
        precisa saber quando encontra um órfão."""
        with x_auth._lock_renovacao():
            marca = lock_atual().read_text(encoding="utf-8")
        assert str(os.getpid()) in marca


def _dormiu_a_toa(_segundos):
    raise AssertionError("esperou em vez de reconhecer o lock órfão")


# ------------------------------------------------------- falha na renovação


class TestFalhaNaRenovacao:
    """A distinção que decide o que o dono faz: 4xx é credencial morta e só o
    humano resolve; 5xx e rede são passageiros. Confundir os dois faz refazer
    consentimento por causa de um timeout."""

    def test_4xx_levanta_precisa_autorizar_com_o_comando(self, monkeypatch,
                                                         cache, relogio):
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, Resposta(400, texto="invalid_grant"))
        with pytest.raises(PrecisaAutorizar) as erro:
            x_auth.token()
        msg = str(erro.value)
        assert "400" in msg and "invalid_grant" in msg
        assert "venv/Scripts/python.exe -m src.x_auth" in msg

    def test_a_mensagem_aponta_o_redirect_uri(self, monkeypatch, cache,
                                              relogio):
        """A doc da X se contradiz entre `localhost` e `127.0.0.1`, e o erro de
        redirect_uri chega como um 4xx genérico. Sem o valor em uso na
        mensagem, esse é o erro em que se perde a tarde."""
        monkeypatch.setenv("X_REDIRECT_URI", "http://localhost:9999/callback")
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, Resposta(401, texto="unauthorized"))
        with pytest.raises(PrecisaAutorizar) as erro:
            x_auth.token()
        assert "http://localhost:9999/callback" in str(erro.value)
        assert "barra final" in str(erro.value)

    def test_4xx_nao_apaga_o_que_esta_no_disco(self, monkeypatch, cache,
                                               relogio):
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, Resposta(403, texto="forbidden"))
        with pytest.raises(PrecisaAutorizar):
            x_auth.token()
        assert lido(cache)["refresh_token"] == "refresh-velho"

    def test_5xx_nao_vira_pedido_de_reautorizacao(self, monkeypatch, cache,
                                                  relogio):
        """Instabilidade do servidor da X não é credencial inválida."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, Resposta(503, texto="upstream"))
        with pytest.raises(requests.RequestException):
            x_auth.token()

    def test_falha_de_rede_nao_vira_pedido_de_reautorizacao(
            self, monkeypatch, cache, relogio):
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, requests.ConnectionError("cabo"))
        with pytest.raises(requests.RequestException):
            x_auth.token()

    def test_corpo_200_que_nao_e_objeto(self, monkeypatch, cache, relogio):
        """Corpo 200 fora do formato (proxy, portal de captura) escaparia limpo
        daqui e estouraria KeyError longe da causa."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, Resposta(corpo=["nada disso"]))
        with pytest.raises(requests.RequestException):
            x_auth.token()

    def test_sem_refresh_no_cache_nem_tenta(self, monkeypatch, cache, relogio):
        """Sem `offline.access` a X não emite refresh, e nenhuma chamada aqui
        vai inventar um. Gastar a requisição para ouvir isso do servidor seria
        pagar para descobrir o que já se sabe."""
        escreve_cache(cache, refresh="", faltam=60)
        with pytest.raises(PrecisaAutorizar) as erro:
            x_auth.token()
        assert "offline.access" in str(erro.value)


# -------------------------------------------------- passageiro dentro do 4xx


class TestPassageiroDentroDo4xx:
    """429 e 408 são 4xx e NÃO são credencial ruim.

    A faixa inteira virava `PrecisaAutorizar`, então um 429 do endpoint de
    token mandava o dono refazer o consentimento no navegador por causa de
    excesso de requisições — exatamente o "reautorize falso" que o próprio
    comentário do módulo diz não fazer para 5xx e timeout. Os dois códigos não
    afirmam nada sobre a credencial: ela continua válida na tentativa
    seguinte."""

    @pytest.mark.parametrize("status", [408, 429])
    def test_nao_pedem_reautorizacao(self, monkeypatch, cache, relogio,
                                     status):
        """`PrecisaAutorizar` NÃO é `RequestException`: se o módulo voltar a
        levantá-la, ela atravessa este `raises` e o teste fica vermelho com o
        nome do defeito na tela."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, Resposta(status, texto="slow down"))
        with pytest.raises(requests.RequestException) as erro:
            x_auth.token()
        assert not isinstance(erro.value, PrecisaAutorizar)

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 499])
    def test_o_resto_do_4xx_continua_pedindo(self, monkeypatch, cache, relogio,
                                             status):
        """O controle contra a sobrecorreção. Tirar a faixa 4xx inteira
        também faria os dois testes acima passarem — e aí refresh morto
        (`invalid_grant`, 400) viraria "instabilidade passageira", o job
        tentaria para sempre e ninguém saberia que precisa autorizar."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, Resposta(status, texto="invalid_grant"))
        with pytest.raises(PrecisaAutorizar):
            x_auth.token()

    @pytest.mark.parametrize("status", [408, 429])
    def test_nao_apagam_o_que_esta_no_disco(self, monkeypatch, cache, relogio,
                                            status):
        """Passageiro é para ser tentado de novo, e a próxima tentativa
        precisa do refresh que está lá."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, Resposta(status, texto="rate limit"))
        with pytest.raises(requests.RequestException):
            x_auth.token()
        assert lido(cache)["refresh_token"] == "refresh-velho"

    @pytest.mark.parametrize("status", [408, 429])
    def test_nao_deixam_lock_para_tras(self, monkeypatch, cache, relogio,
                                       status):
        """A renovação estourou DENTRO do lock. Sem o `finally` que solta, a
        rodada seguinte — 15 minutos depois — encontraria um órfão."""
        escreve_cache(cache, faltam=60)
        liga(monkeypatch, Resposta(status, texto="rate limit"))
        with pytest.raises(requests.RequestException):
            x_auth.token()
        assert not lock_atual().exists()

    def test_pede_token_classifica_sem_o_token_em_volta(self, monkeypatch):
        """O mesmo um nível abaixo, direto em `_pede_token`: aqui não há cache,
        lock nem renovação no caminho, então o que falha é a classificação e
        só ela."""
        liga(monkeypatch, Resposta(429, texto="Too Many Requests"))
        with pytest.raises(requests.HTTPError):
            x_auth._pede_token({}, "cid", "")
        liga(monkeypatch, Resposta(401, texto="unauthorized"))
        with pytest.raises(PrecisaAutorizar):
            x_auth._pede_token({}, "cid", "")


# ------------------------------------------------------------- credencial


class TestCredencial:
    """Nada aqui conhece credencial de verdade. O que se verifica é o FORMATO
    do que seria montado, com valor de mentira."""

    def test_client_id_ausente_levanta_antes_de_qualquer_chamada(
            self, monkeypatch, cache, relogio):
        monkeypatch.delenv("X_CLIENT_ID", raising=False)
        escreve_cache(cache, faltam=60)
        # `requests.post` é `_proibido`: se a ordem se invertesse e a chamada
        # viesse antes da checagem, o erro seria AssertionError, não este.
        with pytest.raises(PrecisaAutorizar) as erro:
            x_auth.token()
        assert "X_CLIENT_ID" in str(erro.value)
        assert ".env" in str(erro.value)

    def test_client_id_so_de_espaco_conta_como_ausente(self, monkeypatch):
        monkeypatch.setenv("X_CLIENT_ID", "   ")
        with pytest.raises(PrecisaAutorizar):
            x_auth._cliente()

    def test_a_autorizacao_para_antes_de_abrir_o_navegador(self, monkeypatch):
        """Abrir o navegador e só depois falhar faria o dono autorizar um app
        que nem foi identificado."""
        monkeypatch.delenv("X_CLIENT_ID", raising=False)
        aberturas = []
        monkeypatch.setattr(x_auth.webbrowser, "open", aberturas.append)
        with pytest.raises(PrecisaAutorizar):
            x_auth.autoriza()
        assert aberturas == []

    def test_app_publico_nao_manda_basic(self, monkeypatch, cache, relogio):
        """App do tipo Native/Public NÃO tem secret — o PKCE faz o papel dele.
        Mandar um Basic vazio seria credencial malformada."""
        escreve_cache(cache, faltam=60)
        endpoint = liga(monkeypatch, par_novo())
        x_auth.token()
        assert "Authorization" not in endpoint.cabecalhos[0]

    def test_app_confidencial_manda_basic(self, monkeypatch, cache, relogio):
        monkeypatch.setenv("X_CLIENT_ID", "cid-falso")
        monkeypatch.setenv("X_CLIENT_SECRET", "segredo-falso")
        escreve_cache(cache, faltam=60)
        endpoint = liga(monkeypatch, par_novo())
        x_auth.token()
        esperado = base64.b64encode(b"cid-falso:segredo-falso").decode("ascii")
        assert endpoint.cabecalhos[0]["Authorization"] == f"Basic {esperado}"

    def test_o_client_id_vai_no_corpo(self, monkeypatch, cache, relogio):
        escreve_cache(cache, faltam=60)
        endpoint = liga(monkeypatch, par_novo())
        x_auth.token()
        assert endpoint.corpo["client_id"] == "client-id-de-mentira"


# --------------------------------------------------------------- callback


class TestServidorDeCallback:
    """O servidor local que recebe a volta do navegador."""

    def test_favicon_nao_encerra_o_consentimento(self):
        """O navegador pede `/favicon.ico` sozinho. Se qualquer requisição
        encerrasse o servidor, o consentimento seria perdido para um pedido de
        ícone."""
        volta = Volta("/favicon.ico")
        volta.do_GET()
        assert volta.server.resultado is None
        assert volta.codigos == [404]

    def test_callback_com_code_guarda_a_query(self):
        volta = Volta("/callback?code=abc&state=xyz")
        volta.do_GET()
        assert volta.server.resultado == {"code": "abc", "state": "xyz"}
        assert volta.codigos == [200]

    def test_callback_com_erro_tambem_encerra(self):
        """Recusa é resposta: continuar esperando os 300s prenderia o processo
        depois de o humano já ter dito não."""
        volta = Volta("/callback?error=access_denied")
        volta.do_GET()
        assert volta.server.resultado == {"error": "access_denied"}

    def test_o_caminho_certo_sem_code_nem_error_nao_encerra(self):
        """A segunda barreira, que a checagem de caminho NÃO substitui: o
        navegador pode bater no próprio `/callback` sem trazer nada (recarga,
        pré-busca). Mutação que apague este `if` passa no teste do favicon,
        porque lá o caminho já é outro."""
        volta = Volta("/callback")
        volta.do_GET()
        assert volta.server.resultado is None
        assert volta.codigos == [404]

    def test_caminho_diferente_nao_encerra_o_consentimento(self):
        """Sem esta barreira, QUALQUER caminho servido nesta porta encerrava a
        espera. O `state` barra o `code` forjado, mas só DEPOIS de a janela já
        ter fechado — e um pedido qualquer com `?error=` bastava para derrubar
        a janela em que o dono ainda estava clicando em Authorize."""
        volta = Volta("/qualquer-outra-coisa?error=x&code=y")
        volta.do_GET()
        assert volta.server.resultado is None
        assert volta.codigos == [404]
        assert volta.wfile.getvalue() == b""

    def test_o_caminho_aceito_e_o_do_redirect_configurado(self, monkeypatch):
        """O caminho não é fixo em código: sai do X_REDIRECT_URI, como o host
        e a porta. Quem registrou outro caminho no console do X troca no .env
        e o callback continua sendo aceito — e o antigo deixa de ser."""
        monkeypatch.setenv("X_REDIRECT_URI", "http://127.0.0.1:8765/volta")
        aceita = Volta("/volta?code=abc&state=xyz")
        aceita.do_GET()
        assert aceita.server.resultado == {"code": "abc", "state": "xyz"}
        recusa = Volta("/callback?code=abc&state=xyz")
        recusa.do_GET()
        assert recusa.server.resultado is None

    def test_o_log_nao_imprime_a_url_do_callback(self, capsys):
        """A URL do callback CARREGA o authorization code. O log padrão do
        `http.server` a escreve no stderr, e daí ela vai para onde quer que o
        job esteja logando."""
        Volta("/callback?code=segredo").log_message(
            '"%s" %s', "GET /callback?code=segredo", "200")
        capturado = capsys.readouterr()
        assert "segredo" not in capturado.err + capturado.out

    def test_deriva_host_e_porta_do_redirect(self, monkeypatch):
        criados = _servidor_falso(monkeypatch, resultado={"code": "abc"})
        x_auth._espera_callback("http://127.0.0.1:8765/callback")
        assert criados[0].endereco == ("127.0.0.1", 8765)

    def test_segue_o_redirect_configurado(self, monkeypatch):
        """Host e porta saem do próprio X_REDIRECT_URI porque são os mesmos que
        a X vai usar para devolver o navegador — e porque a doc dela se
        contradiz entre `localhost` e `127.0.0.1`, então quem registrou um dos
        dois no console troca no .env, sem tocar em código."""
        criados = _servidor_falso(monkeypatch, resultado={"code": "abc"})
        x_auth._espera_callback("http://localhost:9999/cb")
        assert criados[0].endereco == ("localhost", 9999)

    def test_prazo_estourado_desliga_o_servidor(self, monkeypatch):
        """O prazo é real mesmo sem requisição nenhuma chegar, e a porta não
        pode ficar presa depois — a tentativa seguinte usa a mesma."""
        criados = _servidor_falso(monkeypatch)
        with pytest.raises(PrecisaAutorizar) as erro:
            x_auth._espera_callback(x_auth.REDIRECT_PADRAO, espera=0)
        assert "ninguém autorizou" in str(erro.value)
        assert criados[0].fechado

    def test_porta_ocupada_diz_qual_porta_e_por_que(self, monkeypatch):
        """Acontece de verdade quando uma tentativa anterior ficou pendurada. O
        erro cru do socket não diz que a porta é a do X_REDIRECT_URI, e é aí
        que se perde tempo."""
        _servidor_falso(monkeypatch, erro=OSError("endereço em uso"))
        with pytest.raises(PrecisaAutorizar) as erro:
            x_auth._espera_callback(x_auth.REDIRECT_PADRAO)
        assert "8765" in str(erro.value)
        assert "X_REDIRECT_URI" in str(erro.value)


def _servidor_falso(monkeypatch, resultado=None, erro=None) -> list:
    """Substitui o HTTPServer. Nenhum teste abre porta: porta ocupada na
    máquina de quem roda a suíte não pode virar teste vermelho."""
    criados: list = []

    class Falso:
        def __init__(self, endereco, handler):
            if erro is not None:
                raise erro
            self.endereco = endereco
            self.resultado = None
            self.timeout = None
            self.fechado = False
            criados.append(self)

        def handle_request(self):
            self.resultado = resultado

        def server_close(self):
            self.fechado = True

    monkeypatch.setattr(x_auth.http.server, "HTTPServer", Falso)
    return criados


# --------------------------------------------------- script na página local


def _corpo_do_erro(valor: str) -> str:
    """A página que o handler escreveria, para um `?error=` vindo da rede.

    Sem socket: `Volta` já é o handler com `wfile` de memória. Subir porta
    aqui trocaria um teste de escape por um teste de rede."""
    volta = Volta("/callback?error=" + urllib.parse.quote(valor, safe=""))
    volta.do_GET()
    assert volta.codigos == [200]
    return volta.wfile.getvalue().decode("utf-8")


class TestNaoExecutaScriptNaPaginaLocal:
    """O valor de `error` vem da REDE e ia cru para dentro do HTML.

    Um callback com `?error=<script>...</script>` executava script numa página
    servida por 127.0.0.1, no navegador do dono — que é o navegador logado na
    X. Não é hipótese remota: o link do consentimento é colado à mão, e trocar
    a query de um link é o ataque mais barato que existe."""

    def test_script_no_erro_nao_volta_cru(self):
        corpo = _corpo_do_erro("<script>alert(document.domain)</script>")
        assert "<script>" not in corpo
        assert "&lt;script&gt;" in corpo

    def test_o_escape_cobre_aspas_e_e_comercial(self):
        """`quote=True` (o padrão de `html.escape`) para o dia em que algum
        destes valores for parar dentro de um atributo — aí `"` fecha o
        atributo e o `<` nem é preciso."""
        corpo = _corpo_do_erro('a" onload="alert(1)" x=&y')
        assert 'onload="' not in corpo
        assert "&quot;" in corpo
        assert "&amp;y" in corpo

    def test_o_dado_guardado_continua_o_cru(self):
        """O escape é da APRESENTAÇÃO. `_confere_callback` compara e reporta o
        valor recebido, e escapá-lo antes disso esconderia o que a X mandou."""
        volta = Volta("/callback?error=" + urllib.parse.quote("<b>x</b>"))
        volta.do_GET()
        assert volta.server.resultado == {"error": "<b>x</b>"}

    def test_o_erro_legitimo_continua_legivel(self):
        """Não regressão: o escape não pode transformar a mensagem útil em
        entidade ilegível — é ela que diz ao dono o que a X recusou."""
        corpo = _corpo_do_erro("access_denied")
        assert "access_denied" in corpo

    def test_a_pagina_de_sucesso_nao_interpola_nada_da_rede(self):
        """O caminho do `code` não põe valor nenhum da query na página. Se
        alguém acrescentar um ali, este teste cai junto com o escape."""
        volta = Volta("/callback?code=" + urllib.parse.quote("<script>x"))
        volta.do_GET()
        corpo = volta.wfile.getvalue().decode("utf-8")
        assert "<script>" not in corpo
        # O `code` é segredo: não pode ser ecoado nem escapado.
        assert "script" not in corpo


# -------------------------------------------------------- consentimento


class TestAutoriza:
    def test_grava_o_token_e_monta_a_troca(self, monkeypatch, capsys):
        url, endpoint = consente(monkeypatch, par_novo(access="acesso-1",
                                                       refresh="refresh-1"))
        assert query_de(url)["response_type"] == "code"
        assert query_de(url)["scope"] == x_auth.ESCOPOS
        assert endpoint.corpo["grant_type"] == "authorization_code"
        assert endpoint.corpo["code"] == "code-de-mentira"
        guardado = lido(x_auth.ARQUIVO_TOKEN)
        assert guardado["access_token"] == "acesso-1"
        assert guardado["refresh_token"] == "refresh-1"

    def test_o_redirect_vai_identico_na_url_e_na_troca(self, monkeypatch):
        """O valor tem de bater EXATAMENTE com o registrado no console, barra
        final inclusive: `/callback` e `/callback/` são URIs diferentes, e o
        segundo dá uma tela de erro da X sem explicação útil. Duas passagens
        pelo mesmo valor é uma chance de ele ser normalizado no caminho."""
        destino = "http://localhost:9999/callback/"
        monkeypatch.setenv("X_REDIRECT_URI", destino)
        url, endpoint = consente(monkeypatch, par_novo())
        assert query_de(url)["redirect_uri"] == destino
        assert endpoint.corpo["redirect_uri"] == destino

    def test_pede_offline_access(self, monkeypatch):
        """Sem ele a X não emite refresh e o acesso morre em duas horas — cada
        rodada do job exigiria um humano no navegador."""
        url, _ = consente(monkeypatch, par_novo())
        assert "offline.access" in query_de(url)["scope"]

    def test_avisa_quando_o_escopo_nao_foi_concedido(self, monkeypatch,
                                                     capsys):
        """O consentimento pode voltar com menos do que se pediu. Sem o aviso,
        a falha apareceria no dia seguinte, no job, longe daqui."""
        consente(monkeypatch, par_novo(escopos="tweet.read users.read"))
        assert "AVISO" in capsys.readouterr().out

    def test_imprime_o_link_mesmo_com_o_navegador_disponivel(self, monkeypatch,
                                                             capsys):
        """`webbrowser.open` devolve True depois de abrir um handler que não é
        navegador. O link na tela é o caminho que não depende disso."""
        url, _ = consente(monkeypatch, par_novo())
        assert url in capsys.readouterr().out


# ------------------------------------------------------ segredo em disco


def _git_ignora(relativo: str) -> bool:
    """Pergunta ao PRÓPRIO git se o caminho está ignorado.

    Escolhido em vez de reimplementar a regra de glob aqui, e o motivo é
    concreto: uma reimplementação testa o que EU acho que o padrão faz, não o
    que o git faz. Precedência entre linhas, negação com `!`, âncora de barra e
    ordem de arquivo são do git, e o dia em que alguém acrescentar
    `!data/x_token.json.tmp` no fim do arquivo um matcher caseiro continuaria
    verde enquanto o segredo passaria a entrar no `git add`.

    `--no-index` para medir a REGRA e não o estado do índice: sem ele, um
    caminho que já tivesse sido rastreado por engano apareceria como "não
    ignorado" e o teste acusaria a linha errada."""
    try:
        saida = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "--", relativo],
            cwd=config.RAIZ, capture_output=True)
    except OSError:
        pytest.skip("git não está no PATH; a regra não pode ser consultada")
    if saida.returncode not in (0, 1):
        pytest.skip(f"git check-ignore não respondeu: {saida.stderr!r}")
    return saida.returncode == 0


def _sufixo_do_temporario(monkeypatch, alvo) -> str:
    """O sufixo que `_grava` põe no temporário, DERIVADO do próprio código.

    Escrito à mão (`".tmp"`) o teste passaria a concordar consigo mesmo: quem
    trocasse o sufixo no módulo trocaria o do teste junto e o `.gitignore`
    ficaria para trás, calado, com o refresh de fora."""
    visto: list = []
    replace = x_auth.os.replace

    def espia(origem, destino):
        visto.append(origem)
        replace(origem, destino)

    monkeypatch.setattr(x_auth.os, "replace", espia)
    x_auth._grava({"access_token": "valor-de-mentira"}, alvo)
    return x_auth.Path(visto[0]).name[len(alvo.name):]


class TestSegredoEmDisco:
    def test_o_caminho_default_e_o_que_o_gitignore_cobre(self):
        """A proteção começa numa linha literal no .gitignore, posta em
        04/09/2026. `data/` NÃO é ignorado inteiro — só `raw/`, `*.db`,
        `chroma/` e `boletins/`. Se o caminho mudar sem a linha mudar junto, o
        próximo `git add data/` leva o refresh token para um repositório
        público, e apagá-lo num commit posterior não o tira do histórico.

        Esta linha cobre o `.json`; os IRMÃOS que o módulo cria ao lado dele
        têm o mesmo refresh dentro e são cobertos por um segundo padrão — é o
        que os testes abaixo verificam, e por isso este não pode ser lido como
        "a proteção inteira"."""
        relativo = CAMINHO_PADRAO.relative_to(config.RAIZ).as_posix()
        linhas = (config.RAIZ / ".gitignore").read_text(
            encoding="utf-8").splitlines()
        assert relativo in [linha.strip() for linha in linhas]

    def test_o_git_realmente_ignora_o_token(self):
        """Controle do método: o resto do bloco só vale se `git check-ignore`
        souber dizer não. Um fonte rastreado tem de dar NÃO ignorado — sem
        isto, um `check-ignore` que respondesse 0 para tudo faria os testes
        abaixo passarem sem cobrir nada."""
        relativo = CAMINHO_PADRAO.relative_to(config.RAIZ).as_posix()
        assert _git_ignora(relativo)
        assert not _git_ignora("src/x_auth.py")

    def test_o_temporario_da_gravacao_atomica_tambem_e_ignorado(
            self, monkeypatch, tmp_path):
        """O defeito real, medido em 04/09/2026: só o `.json` casava. O
        temporário de `_grava` carrega o MESMO refresh token dentro e ficava
        exposto a um `git add data/` — a gravação atômica, que existe para
        proteger a credencial, era quem a deixava de fora do .gitignore.

        O nome sai do código (via `os.replace`), não de uma string escrita
        aqui."""
        sufixo = _sufixo_do_temporario(monkeypatch,
                                       tmp_path / CAMINHO_PADRAO.name)
        assert sufixo, "o temporário não é irmão com sufixo do arquivo final"
        relativo = CAMINHO_PADRAO.relative_to(config.RAIZ).as_posix()
        assert _git_ignora(relativo + sufixo)

    def test_o_lock_da_renovacao_tambem_e_ignorado(self, tmp_path):
        """O lock não guarda o token, mas nasce no mesmo diretório e pelo mesmo
        padrão. Deixá-lo de fora encheria o `git status` do dono de ruído
        justamente na hora em que ele está conferindo se o segredo ficou
        fora — e é assim que se aprende a ignorar o `git status`."""
        with x_auth._lock_renovacao():
            criados = [p.name for p in tmp_path.iterdir()]
        assert len(criados) == 1
        sufixo = criados[0][len(x_auth.ARQUIVO_TOKEN.name):]
        assert sufixo
        relativo = CAMINHO_PADRAO.relative_to(config.RAIZ).as_posix()
        assert _git_ignora(relativo + sufixo)

    def test_pede_os_tres_escopos_e_nada_mais(self):
        """Escopo a mais é acesso a mais concedido a um token que mora em
        arquivo. Os dois primeiros são o que o endpoint de timeline exige; o
        terceiro é o que transforma um consentimento único em coleta diária."""
        assert x_auth.ESCOPOS.split() == [
            "tweet.read", "users.read", "offline.access"]

    def test_o_que_a_doc_nao_confirma_segue_marcado(self):
        """Duas apostas do módulo em que o comentário é a única memória de que
        são apostas.

        (a) conta protegida: a doc do endpoint nunca afirma que ele devolve
        post de conta protegida; a única evidência é a descrição do escopo
        `tweet.read`, que fala do que a CONTA enxerga.
        (b) validade do refresh: a doc não diz se ele expira, e a FAQ manda
        assumir que o acesso cai a qualquer momento.

        Se alguém medir e confirmar, a marca sai — junto com este teste.
        Enquanto ninguém mediu, apagá-la seria promover inferência a fato."""
        fonte = (config.RAIZ / "src" / "x_auth.py").read_text(encoding="utf-8")
        # A docstring de ESCOPOS é string solta abaixo da atribuição: o
        # interpretador a descarta, então ela só existe no fonte.
        marca = fonte[fonte.index("NÃO CONFIRMADO"):][:200]
        assert "conta protegida" in marca
        assert "may become invalid at any time" in PrecisaAutorizar.__doc__
