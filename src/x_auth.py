"""Autenticação OAuth 2.0 (PKCE) para a API oficial do X.

    venv/Scripts/python.exe -m src.x_auth    # primeiro consentimento, à mão

A API devolve `conversation_id` e os posts referenciados, e desses dois o
tipo do post (thread/quote/resposta) é DERIVADO em código, em
`x_api.classifica` — dado do servidor, não rótulo pedido a um modelo.

Este módulo só entrega o token. Quem lê a timeline é `x_api`.

Interface pública:

    token()      -> str   o job chama; renova sozinho se precisar
    autoriza()   -> None  o humano chama; abre o navegador
    PrecisaAutorizar      só o humano resolve

SEGREDO EM DISCO. O cache fica em `data/x_token.json` e guarda access token
e refresh token em texto puro: quem tiver o arquivo lê a timeline da conta,
e o refresh a mantém lendo depois de o access token expirar. O `data/` NÃO
é ignorado inteiro — só `raw/`, `*.db`, `chroma/` e `boletins/` —, então o
arquivo depende de linhas próprias no .gitignore (`data/x_token.json` e
`data/x_token.json.*`). São DUAS porque o temporário
da gravação atômica (`data/x_token.json.tmp`) carrega o mesmo refresh
dentro e o padrão literal não o cobria. Se essas linhas sumirem num merge,
o segredo vai junto no próximo `git add data/`, e apagá-lo num commit
posterior não o tira do histórico.
"""

import base64
import contextlib
import hashlib
import html
import http.server
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

from . import config

URL_AUTORIZA = "https://x.com/i/oauth2/authorize"
URL_TOKEN = "https://api.x.com/2/oauth2/token"

ESCOPOS = "tweet.read users.read offline.access"
"""Os três escopos, e por que cada um.

`tweet.read` + `users.read` são o que a página do endpoint de timeline
(GET /2/users/:id/tweets) exige do OAuth2UserToken.

`offline.access` é o que decide se este módulo serve para um job agendado:
SEM ele a X não emite refresh token, e o acesso morre em duas horas — cada
rodada exigiria um humano no navegador. Ele é o escopo que transforma um
consentimento único em coleta diária.

NÃO CONFIRMADO: se este conjunto alcança post de conta protegida. A doc do
endpoint nunca afirma isso; a única evidência é a descrição do próprio
escopo `tweet.read` ("View all posts you can see, including those from
protected accounts"), que fala do que a CONTA enxerga, não do que o
endpoint devolve. Tratar handle protegido como não coletável até medir.
"""

REDIRECT_PADRAO = "http://127.0.0.1:8765/callback"
"""Para onde a X devolve o navegador depois do consentimento.

Configurável por `X_REDIRECT_URI` porque o valor TEM de bater EXATAMENTE
com o registrado no console do app — a doc é explícita sobre match exato, e
isso inclui barra final: `/callback` e `/callback/` são URIs diferentes e o
segundo dá erro de redirect_uri inválido, que se manifesta como uma tela de
erro da X sem explicação útil.

A própria doc da X se contradiz entre `localhost` e `127.0.0.1` no callback
de desenvolvimento — páginas diferentes mostram uma e outra. Por isso o
default é um valor só e a variável existe: quem registrou `localhost` no
console troca no .env, sem tocar em código.
"""

FOLGA_SEGUNDOS = 300
"""Quanto antes de expirar o access token já é renovado.

O token dura duas horas (doc). Devolver um que expira em 40 segundos é
convidar o 401 no meio da paginação, com o job já tendo pago pelas páginas
anteriores. Cinco minutos cobrem uma rodada inteira de leitura de timeline
sem risco de o token morrer no meio dela."""

ESPERA_CONSENTIMENTO = 300
"""Quanto o servidor de callback espera o humano clicar em "Authorize".

Existe para o processo não ficar pendurado com uma porta aberta se a pessoa
fechar o navegador."""

TIMEOUT = 30

LOCK_ESPERA = TIMEOUT + 10
"""Quanto esperar pelo lock de renovação antes de seguir SEM ele.

Derivado de `TIMEOUT` de propósito: quem tem o lock na mão está, no pior
caso legítimo, pendurado no POST do endpoint de token — que só desiste em
`TIMEOUT` segundos — e ainda tem de gravar. Esperar menos que isso
transformaria uma renovação lenta e NORMAL em "processo travado"."""

LOCK_VELHO = 4 * TIMEOUT
"""A partir de que idade um lock é tratado como órfão e removido.

Um processo morto (Ctrl+C, máquina desligada, tarefa agendada cancelada
no meio) deixa o arquivo para trás, e sem esta regra ele barraria a
renovação para sempre — o conserto seria pior que o defeito.

O valor é folgado por escolha, não por medida: apagar o lock de um
processo VIVO devolve exatamente a corrida que o lock existe para
impedir, então o limite fica em 4x a duração máxima legítima da posse
(`TIMEOUT` mais a gravação). É maior que `LOCK_ESPERA` de propósito:
diante de um órfão, a primeira rodada prefere seguir sem lock a apagar o
que talvez seja de alguém."""

LOCK_INTERVALO = 0.25
"""De quanto em quanto tempo tentar de novo enquanto o lock está ocupado."""

ARQUIVO_TOKEN = config.DIR_DADOS / "x_token.json"


class PrecisaAutorizar(Exception):
    """Só o humano no navegador resolve. Nada aqui vai destravar sozinho.

    É a tradução direta do que a FAQ oficial manda assumir: "Assume a
    user's access token may become invalid at any time. If this happens,
    prompt the user to re-authorize the application." O job agendado não
    tem como pedir nada a ninguém — então ele levanta isto, e a mensagem
    carrega o comando exato a rodar."""


# ---------------------------------------------------------------- PKCE


def _verificador() -> str:
    """O `code_verifier`: segredo aleatório que fica NESTE processo.

    `secrets`, não `random`: o verifier é o que impede que quem intercepte
    o `code` no callback o troque por um token. `random` é previsível a
    partir de saídas anteriores, e aqui isso é a diferença entre PKCE
    funcionar e ser enfeite.

    64 bytes viram ~86 caracteres, dentro dos 43-128 que a RFC 7636 exige,
    e `token_urlsafe` só produz caractere do alfabeto permitido."""
    return secrets.token_urlsafe(64)


def _desafio(verificador: str) -> str:
    """O `code_challenge` S256: SHA-256 do verifier, base64url sem padding.

    Sem padding porque o `=` é reservado em query string; mandá-lo cru faz
    a X recusar o challenge, e mandá-lo escapado muda o valor comparado."""
    resumo = hashlib.sha256(verificador.encode("ascii")).digest()
    return base64.urlsafe_b64encode(resumo).decode("ascii").rstrip("=")


# ------------------------------------------------------------ ambiente


def _cliente() -> tuple[str, str]:
    """(client_id, client_secret). O secret é "" quando o app é público."""
    cid = os.environ.get("X_CLIENT_ID", "").strip()
    if not cid:
        raise PrecisaAutorizar(
            "X_CLIENT_ID ausente no .env — é o Client ID do app OAuth 2.0 "
            "criado em https://developer.x.com/en/portal/dashboard "
            "(aba Keys and tokens do app). Ver .env.example.")
    # App do tipo "Native / Public client" NÃO tem secret: o PKCE é que faz
    # o papel dele. Só o tipo "Confidential" tem, e aí o refresh e a troca
    # do code exigem o header Basic. Suportar os dois é mais barato que
    # descobrir o tipo do app na hora do erro.
    return cid, os.environ.get("X_CLIENT_SECRET", "").strip()


def _redirect() -> str:
    return os.environ.get("X_REDIRECT_URI", "").strip() or REDIRECT_PADRAO


def _cabecalhos(cid: str, secret: str) -> dict[str, str]:
    cab = {"Content-Type": "application/x-www-form-urlencoded"}
    if secret:
        par = base64.b64encode(f"{cid}:{secret}".encode()).decode("ascii")
        cab["Authorization"] = f"Basic {par}"
    return cab


# --------------------------------------------------------- cache em disco


def _le(caminho: Path | None = None) -> dict:
    """O cache, ou {} se não existir nem for legível.

    Arquivo corrompido é tratado como ausente de propósito: o efeito é
    pedir reautorização, que é recuperável à mão, contra estourar uma
    exceção crua dentro do job."""
    caminho = caminho or ARQUIVO_TOKEN
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dados if isinstance(dados, dict) else {}


def _grava(dados: dict, caminho: Path | None = None) -> None:
    """Grava o cache de forma ATÔMICA. É o ponto mais importante do arquivo.

    O refresh token da X é ROTATIVO: cada renovação devolve um novo e MATA
    o anterior ("Refresh tokens rotate - always save the newest one"). Isso
    cria uma janela em que o token que está no disco já não vale mais e o
    que vale ainda só existe em memória. Um crash — ou um `write` que
    truncou o arquivo e morreu no meio — dentro dessa janela deixa o dono
    sem nenhum refresh válido, e a única saída é reautorizar à mão, no
    navegador. Num job agendado isso significa a coleta parada até alguém
    perceber.

    Por isso: escreve num temporário no MESMO diretório, força o fsync, e
    só então `os.replace`. `replace` é atômico dentro do mesmo volume (no
    Windows também), então o arquivo final ou é o antigo inteiro ou o novo
    inteiro — nunca meio arquivo. O fsync antes é o que impede o rename de
    chegar ao disco antes do conteúdo.

    A janela não fecha por completo: entre a X rotacionar o refresh e este
    processo gravar existem milissegundos em que só a memória tem o token
    novo. Isso é irredutível num cliente sem transação distribuída — o que
    dá para fazer é encurtar ao máximo, que é gravar antes de qualquer
    outra coisa.

    Modo 0o600 porque o arquivo é credencial. No Windows o modo do
    `os.open` é praticamente ignorado (o CRT só distingue somente-leitura),
    então lá a proteção real é a ACL do perfil do usuário — NÃO CONFIRMADO
    que isso baste num diretório compartilhado."""
    caminho = caminho or ARQUIVO_TOKEN
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(caminho.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as saida:
        json.dump(dados, saida, ensure_ascii=False, indent=2)
        saida.flush()
        os.fsync(saida.fileno())
    os.replace(tmp, caminho)


def _guarda(resposta: dict, anterior: dict) -> dict:
    """Monta e grava o cache novo a partir da resposta do endpoint de token.

    `expira_em` é absoluto (epoch) e não relativo: guardar `expires_in`
    obrigaria a saber QUANDO a resposta chegou, e essa informação some
    quando o processo morre.

    O refresh anterior é mantido se a resposta vier sem um novo. A doc diz
    que a rotação é a regra, mas se algum caminho devolver sem
    `refresh_token`, sobrescrever com vazio apagaria o único token de
    renovação que existe — o erro caro. Manter o antigo, no pior caso,
    custa um 4xx na renovação seguinte, que já é tratado."""
    # A duração é calculada ANTES de montar o dicionário, e não dentro dele,
    # porque `float` de um valor da REDE pode estourar: `expires_in` fora do
    # formato ("7200 seconds", um objeto, um nulo esquisito) levantaria
    # ValueError/TypeError ANTES do `_grava` abaixo — e neste ponto o refresh
    # que está no disco JÁ morreu do lado da X, porque a rotação já aconteceu
    # lá. O par novo se perderia sem nunca tocar o disco, e a saída seria
    # reautorizar à mão, no navegador, num job agendado.
    #
    # Por isso cair para o padrão em vez de estourar: um `expira_em` errado
    # custa, no MÁXIMO, uma renovação cedo demais ou um 401 que já é tratado
    # como credencial recusada — enquanto a exceção custa a credencial
    # inteira. É a mesma escolha que `token()` já faz com o valor lido do
    # DISCO; a assimetria de deixar cru justo o que vem da rede, que é a
    # fonte menos confiável das duas, era descuido.
    try:
        dura = float(resposta.get("expires_in") or 7200)
    except (TypeError, ValueError):
        dura = 7200.0
    novo = {
        "access_token": resposta.get("access_token", ""),
        "refresh_token": (resposta.get("refresh_token")
                          or anterior.get("refresh_token", "")),
        "expira_em": time.time() + dura,
        "escopos": resposta.get("scope", ""),
        "obtido_em": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _grava(novo)
    return novo


# --------------------------------------------------- endpoint de token


def _pede_token(corpo: dict, cid: str, secret: str) -> dict:
    """POST no endpoint de token. Devolve o JSON; classifica a falha.

    A distinção que importa está no `status_code`. 4xx é o servidor da X
    dizendo que a CREDENCIAL não serve (refresh morto, redirect_uri
    diferente do registrado, app errado) — repetir não muda nada e o
    humano precisa saber. 5xx e falha de rede são passageiros e sobem como
    `requests.RequestException`, para não virarem um "reautorize" falso:
    timeout de cliente não é credencial inválida, e confundir os dois faz
    o dono refazer o consentimento sem necessidade.

    429 e 408 são a EXCEÇÃO dentro da faixa 4xx, e saem dela: são
    passageiros pela definição do próprio código — "excesso de requisições"
    e "o cliente demorou" não dizem nada sobre a credencial, que continua
    válida e vai funcionar na tentativa seguinte. Mandavam o dono refazer o
    consentimento à toa, que é exatamente o que o parágrafo acima diz não
    fazer. Descem para `raise_for_status`, junto com os 5xx, e chegam a
    quem chama como `requests.RequestException` — o mesmo tratamento que
    `x_api._pede` já dá ao 429 do endpoint de leitura."""
    resposta = requests.post(URL_TOKEN, data=corpo,
                             headers=_cabecalhos(cid, secret), timeout=TIMEOUT)
    if 400 <= resposta.status_code < 500 and resposta.status_code not in (
            408, 429):
        raise PrecisaAutorizar(
            f"a X recusou a credencial (HTTP {resposta.status_code}): "
            f"{resposta.text[:300]}\n"
            f"  Rode: venv/Scripts/python.exe -m src.x_auth\n"
            f"  Se o erro citar redirect_uri, confira que X_REDIRECT_URI "
            f"({_redirect()}) é IDÊNTICO ao registrado no console, barra "
            f"final inclusive.")
    resposta.raise_for_status()
    dados = resposta.json()
    if not isinstance(dados, dict):
        # Corpo 200 fora do formato (proxy, portal de captura) escaparia
        # limpo daqui e estouraria KeyError longe da causa.
        raise requests.exceptions.InvalidJSONError(
            f"resposta do token não é objeto JSON: {type(dados).__name__}")
    return dados


def _renova(cache: dict) -> str:
    """Troca o refresh token por um par novo e devolve o access token."""
    refresh = cache.get("refresh_token", "")
    if not refresh:
        raise PrecisaAutorizar(
            "não há refresh token no cache — o consentimento foi dado sem o "
            "escopo offline.access, e sem ele a X não emite renovação. "
            "Rode: venv/Scripts/python.exe -m src.x_auth")
    cid, secret = _cliente()
    dados = _pede_token(
        {"grant_type": "refresh_token", "refresh_token": refresh,
         "client_id": cid}, cid, secret)
    # GRAVA ANTES DE DEVOLVER. Neste ponto o refresh que está no disco JÁ
    # morreu do lado da X. Devolver o access token primeiro e gravar depois
    # inverteria a ordem do risco: o chamador sairia lendo a timeline com um
    # token bom enquanto o disco guarda um refresh morto, e qualquer falha
    # daí em diante custa reautorização à mão.
    #
    # Se a gravação falhar (disco cheio, permissão), o OSError sobe e o
    # token NÃO é devolvido — de propósito. Uma rodada perdida com aviso é
    # melhor que uma rodada bem-sucedida que deixa o próximo dia sem
    # credencial nenhuma.
    return _guarda(dados, cache)["access_token"]


def _expira_em(cache: dict) -> float:
    """Quando o access token do cache morre, em epoch. 0 se ilegível.

    Cache escrito por uma versão anterior, ou editado à mão, pode trazer
    qualquer coisa nesse campo. Tratar o ilegível como JÁ EXPIRADO renova
    cedo demais no pior caso; confiar nele devolveria um token morto."""
    try:
        return float(cache.get("expira_em", 0))
    except (TypeError, ValueError):
        return 0.0


def _vale(cache: dict) -> bool:
    return bool(cache.get("access_token")) and (
        time.time() < _expira_em(cache) - FOLGA_SEGUNDOS)


def _descarta_lock_velho(caminho: Path) -> bool:
    """Remove o lock se ele for velho demais para ser de alguém vivo."""
    try:
        idade = time.time() - caminho.stat().st_mtime
    except OSError:
        # Sumiu entre o `open` e o `stat`, ou não dá para inspecionar. Não
        # afirmar que é órfão: quem chama só volta a esperar.
        return False
    if idade < LOCK_VELHO:
        return False
    try:
        os.unlink(caminho)
    except OSError:
        # No Windows, apagar arquivo que outro processo mantém aberto falha.
        # Isso é INFORMAÇÃO, não erro: o dono está vivo.
        return False
    return True


@contextlib.contextmanager
def _lock_renovacao(espera: float = LOCK_ESPERA):
    """Serializa a renovação entre processos. Cede a passagem no prazo.

    O refresh da X é ROTATIVO, e é isso que torna a corrida cara: dois
    processos que leem o MESMO refresh o gastam duas vezes, e o segundo
    grava por cima um par que a X já invalidou — o disco fica com uma
    credencial morta e a saída é o navegador. Não é hipótese: a tarefa
    `checador-coleta` roda de 15 em 15 minutos e cai exatamente em cima de
    qualquer coisa que o dono rode à mão.

    O lock mora ao lado do token (`x_token.json.lock`) e é DERIVADO de
    `ARQUIVO_TOKEN` em vez de ser constante de módulo. Dois motivos: o
    `.gitignore` cobre os irmãos do token com um padrão só, e quem
    redireciona `ARQUIVO_TOKEN` — a suíte redireciona — leva o lock junto,
    em vez de a suíte cutucar o `data/` de verdade.

    FALHA PARA A FRENTE, e essa é a decisão que importa aqui: esgotado o
    prazo, segue SEM o lock em vez de bloquear. Um lock que possa pendurar
    o job para sempre é pior que a corrida que ele evita — a corrida custa
    uma renovação perdida de vez em quando, o bloqueio custa a coleta
    inteira, calada, até alguém olhar. `os.O_CREAT | os.O_EXCL` porque a
    criação exclusiva é atômica nos dois sistemas, sem depender de fcntl
    (que não existe no Windows) nem de msvcrt (que não existe fora dele)."""
    caminho = ARQUIVO_TOKEN.with_name(ARQUIVO_TOKEN.name + ".lock")
    with contextlib.suppress(OSError):
        caminho.parent.mkdir(parents=True, exist_ok=True)
    limite = time.monotonic() + espera
    fd = None
    while fd is None:
        try:
            fd = os.open(caminho, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if _descarta_lock_velho(caminho):
                continue
            if time.monotonic() >= limite:
                print(f"AVISO: {caminho} ocupado há mais de {espera:.0f}s; "
                      "renovando o token do X sem o lock. Se isto se repetir, "
                      "veja se sobrou um processo pendurado.", file=sys.stderr)
                break
            # `monotonic` e não `time()`: relógio de parede pode andar para
            # trás (NTP, horário de verão) e o prazo nunca chegaria.
            time.sleep(LOCK_INTERVALO)
        except OSError:
            # Diretório somente-leitura, caminho inválido. Seguir sem lock é
            # a mesma escolha do prazo esgotado: renovar é mais importante.
            break
    if fd is not None:
        # Quem e quando, para o humano que encontrar um órfão: sem isso o
        # arquivo é um zero-byte que não diz de qual processo sobrou.
        marca = f"{os.getpid()} {time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
        with contextlib.suppress(OSError):
            os.write(fd, marca.encode("utf-8"))
    try:
        yield fd is not None
    finally:
        # Solta SEMPRE, inclusive quando a renovação estourou no meio: o
        # `finally` é o que impede que um erro tratado lá em cima vire um
        # órfão de dois minutos para a rodada seguinte.
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(caminho)


def token() -> str:
    """Devolve um access token válido, renovando se preciso.

    Levanta PrecisaAutorizar quando só o humano resolve."""
    cache = _le()
    if not cache.get("access_token"):
        raise PrecisaAutorizar(
            "nenhum token do X em disco — o primeiro consentimento é "
            "obrigatoriamente humano, no navegador. "
            "Rode: venv/Scripts/python.exe -m src.x_auth")
    if _vale(cache):
        return cache["access_token"]
    with _lock_renovacao():
        # RELÊ com o lock na mão, e é aqui que a corrida se resolve. Quem
        # ficou esperando chega neste ponto DEPOIS de o outro já ter
        # gravado o par novo: encontra um token válido em disco, devolve, e
        # não gasta refresh nenhum. Sem esta releitura o lock só enfileiraria
        # as duas renovações — a segunda continuaria queimando um refresh
        # que a primeira acabou de aposentar.
        recente = _le()
        if _vale(recente):
            return recente["access_token"]
        # O de disco tem precedência: se alguém renovou enquanto se
        # esperava, o refresh que ainda vale é o dele, não o que este
        # processo leu antes do lock.
        return _renova(recente if recente.get("refresh_token") else cache)


# ------------------------------------------------------------ callback


_PAGINA = ("<html><meta charset='utf-8'><body style='font-family:sans-serif'>"
           "<h3>{titulo}</h3><p>{texto}</p></body></html>")


class _Callback(http.server.BaseHTTPRequestHandler):
    """Atende a UMA volta do navegador e guarda a query no servidor."""

    def do_GET(self):  # noqa: N802 (nome imposto pelo http.server)
        partes = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(partes.query)
        # O caminho tem de ser o do X_REDIRECT_URI, e não só a query trazer
        # `code`/`error`. Sem isto, QUALQUER caminho servido nesta porta
        # encerra o consentimento — o `state` barra o code forjado logo
        # depois, mas só depois de a espera já ter acabado, e um pedido
        # qualquer com `?error=` bastaria para derrubar a janela em que o
        # dono ainda estava clicando em Authorize. Duas barreiras custam uma
        # comparação de string.
        esperado = urllib.parse.urlparse(_redirect()).path or "/"
        if (partes.path or "/") != esperado:
            self.send_response(404)
            self.end_headers()
            return
        if "code" not in query and "error" not in query:
            # O navegador pede /favicon.ico sozinho, e essa requisição
            # chega ANTES ou DEPOIS do callback. Se qualquer requisição
            # encerrasse o servidor, o consentimento seria perdido para um
            # pedido de ícone. Só encerra quem traz code ou error.
            self.send_response(404)
            self.end_headers()
            return
        self.server.resultado = {k: v[0] for k, v in query.items()}
        # `html.escape` porque este valor vem da REDE e vai para dentro de
        # uma página. Sem ele, um callback com
        # `?error=<script>...</script>` executa script no navegador do dono,
        # numa página servida por 127.0.0.1 — e é o navegador dele que está
        # logado na X. Não é hipótese remota: o link do consentimento é
        # colado à mão, e trocar a query de um link é o ataque mais barato
        # que existe. `quote=True` (o padrão) também cobre o dia em que
        # algum destes valores for parar dentro de um atributo.
        corpo = _PAGINA.format(
            titulo="Pode fechar esta aba.",
            texto="O token foi gravado em data/x_token.json."
            if "code" in query else
            "A X recusou: " + html.escape(
                self.server.resultado.get("error") or ""))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(corpo.encode("utf-8"))

    def log_message(self, *args):
        """Silencia o log padrão: ele imprime a URL do callback no stderr,
        e a URL do callback CARREGA o authorization code."""


def _confere_callback(resultado: dict, state: str) -> str:
    """Valida a volta do navegador e devolve o `code`.

    O `state` é a barreira de CSRF: sem ele, um link forjado poderia
    entregar a este servidor um `code` de OUTRA conta, e o processo
    gravaria feliz um token que não é do dono. `compare_digest` porque a
    comparação é de segredo."""
    if "error" in resultado:
        raise PrecisaAutorizar(
            f"a X não autorizou: {resultado.get('error')} "
            f"{resultado.get('error_description', '')}".strip())
    # Compara em BYTES: `compare_digest` com `str` estoura TypeError se
    # houver um caractere fora do ASCII, e o `state` recebido vem da rede —
    # um callback forjado com acento derrubaria o processo com um erro que
    # não diz nada, em vez de ser barrado aqui.
    if not secrets.compare_digest(
            resultado.get("state", "").encode("utf-8", "replace"),
            state.encode("utf-8")):
        raise PrecisaAutorizar(
            "o `state` do callback não é o que este processo gerou — "
            "requisição forjada ou consentimento de outra janela. "
            "Nada foi gravado; rode de novo.")
    code = resultado.get("code", "")
    if not code:
        raise PrecisaAutorizar("o callback voltou sem `code`.")
    return code


def _espera_callback(destino: str, espera: int = ESPERA_CONSENTIMENTO) -> dict:
    """Sobe um HTTP server local efêmero, pega uma volta e desliga.

    Host e porta saem do próprio X_REDIRECT_URI: são os mesmos que a X vai
    usar para devolver o navegador, e derivá-los evita a configuração
    duplicada que fatalmente sai de sincronia.

    `timeout` de 1s no servidor com laço até o prazo, em vez de um
    `handle_request()` bloqueante: assim o Ctrl+C responde, e o prazo é
    real mesmo que nenhuma requisição chegue."""
    partes = urllib.parse.urlparse(destino)
    host = partes.hostname or "127.0.0.1"
    porta = partes.port or (443 if partes.scheme == "https" else 80)
    try:
        servidor = http.server.HTTPServer((host, porta), _Callback)
    except OSError as erro:
        # Acontece de verdade quando uma tentativa anterior ficou pendurada:
        # o erro cru do socket não diz que a porta é a do X_REDIRECT_URI, e
        # é aí que se perde tempo.
        raise PrecisaAutorizar(
            f"não deu para escutar em {host}:{porta} ({erro}). É a porta do "
            f"X_REDIRECT_URI — feche o processo que a ocupa, ou troque o "
            f"valor no .env E no console do X (os dois têm de bater).")
    servidor.resultado = None
    servidor.timeout = 1
    limite = time.monotonic() + espera
    try:
        while servidor.resultado is None and time.monotonic() < limite:
            servidor.handle_request()
    finally:
        servidor.server_close()
    if servidor.resultado is None:
        raise PrecisaAutorizar(
            f"ninguém autorizou em {espera}s. O consentimento é manual: "
            f"abra o link impresso acima e clique em Authorize.")
    return servidor.resultado


def autoriza() -> None:
    """Fluxo de primeiro consentimento: abre o navegador, recebe o
    callback, grava o token. Roda a MÃO, nunca no job agendado."""
    cid, secret = _cliente()
    destino = _redirect()
    verificador = _verificador()
    state = secrets.token_urlsafe(32)
    url = URL_AUTORIZA + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": destino,
        "scope": ESCOPOS,
        "state": state,
        "code_challenge": _desafio(verificador),
        "code_challenge_method": "S256",
    })
    # Imprime SEMPRE, e só então tenta abrir. `webbrowser.open` devolve
    # False em máquina sem navegador padrão e, pior, devolve True depois de
    # abrir um handler que não é navegador — o link na tela é o caminho que
    # não depende disso.
    print(f"Abra e autorize:\n\n  {url}\n")
    webbrowser.open(url)
    resultado = _espera_callback(destino)
    code = _confere_callback(resultado, state)
    dados = _pede_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": destino,
        "code_verifier": verificador,
        "client_id": cid,
    }, cid, secret)
    cache = _guarda(dados, {})
    print(f"Token gravado em {ARQUIVO_TOKEN}")
    print(f"Escopos concedidos: {cache['escopos'] or '(não informados)'}")
    if "offline.access" not in (cache["escopos"] or ""):
        # Sem offline.access o cache vale duas horas e o job agendado
        # falha no dia seguinte, longe daqui. Avisar agora é a única
        # chance de o dono ligar uma coisa à outra.
        print("AVISO: sem offline.access não há refresh — este token morre "
              "em 2h e o consentimento terá de ser refeito à mão.")
    print("Confira que data/x_token.json está fora do Git antes de commitar.")


if __name__ == "__main__":
    try:
        autoriza()
    except PrecisaAutorizar as erro:
        print(f"ERRO: {erro}", file=sys.stderr)
        sys.exit(1)
