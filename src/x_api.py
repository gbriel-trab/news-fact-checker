"""Cliente de dados da API oficial do X: ler os posts de um handle.

É o cliente que o radar usa (`radar.busca`, um handle por vez). Este módulo
não tem credencial nenhuma e não lê ambiente no import: o token vem de
`x_auth`, e o dono preenche o .env.

O que este módulo garante, e é o que justifica ler pela API e não pedir o
post a um modelo: o TIPO do post (post, thread, resposta, citação, retweet)
é CALCULADO do metadado do servidor (`conversation_id`, `referenced_*`,
`in_reply_to_user_id`), em função pura e testável: `classifica`. Rótulo
pedido a um modelo seria opinião; metadado é dado. É a diferença entre
pedir e derivar.

Preço, lido na doc oficial em 04/09/2026: US$ 0,005 por post devolvido
("charged per resource returned in the response"). O desconto "Owned Reads"
(US$ 0,001) NÃO se aplica — vale só quando o :id lido é o próprio usuário
autenticado E dono do app. A cobrança é deduplicada dentro da janela de 24h
UTC, o que torna toda estimativa deste módulo um TETO, nunca uma medição
(ver `custo_estimado_usd`).

Este módulo NÃO expande `referenced_*.id` na leitura da timeline. A
expansão traria o referenciado de TODO post devolvido, inclusive das
respostas a terceiros que o radar descarta antes de custar — e cada recurso
devolvido é cobrado. O que existe (06/09/2026) é `posts_por_id`: o radar
pede, numa segunda requisição, SÓ os referenciados que faltam às capturas
(o post citado de outra conta, o pai de thread fora da janela), e paga só
por esses. Medido no boletim refeito de 25/08 a 06/09: 16 das 19 citações
chegavam sem o texto citado, e o separador trabalhava só com o comentário.

SUPERFÍCIE DE EXCEÇÃO — o contrato com quem chamar. Da fachada pública
(`posts_de`, `id_do_handle`) sai `FalhaNaAPI` ou `PrecisaAutorizar`, DUAS e
só duas. A distinção é de AÇÃO, não de gravidade: `PrecisaAutorizar` exige um
humano no navegador e nada vai destravar sozinho; `FalhaNaAPI` é rodada
perdida que a próxima tentativa pode recuperar.

Manter em duas custa uma tradução, e ela não é decorativa: `x_auth.token()`
renova o access token por conta própria e, por decisão explícita de lá, deixa
falha PASSAGEIRA subir como `requests.RequestException` (5xx e queda de rede
na renovação não são credencial inválida, e chamá-las de "reautorize" faria o
dono refazer o consentimento à toa). Essa exceção é de um segundo cliente
HTTP, invisível para quem chama daqui. `_pede` a converte em `FalhaNaAPI` no
ponto em que pede o token, para o adaptador do radar não ter de capturar uma
terceira classe cuja origem ele não tem como adivinhar.
"""

import re
import time
from dataclasses import dataclass

import requests

from .x_auth import PrecisaAutorizar, token

URL_BASE = "https://api.x.com/2"

PRECO_POR_POST_USD = 0.005

TIMEOUT = 30
"""Leitura curta de propósito: é REST puro devolvendo no máximo 100
registros por página. Timeout largo num GET simples só atrasa o aviso de que
a rede caiu."""

ESPERA_5XX = 5
TETO_ESPERA = 300
"""Teto para a espera de um 429. A janela do limite é de 15 minutos, então o
servidor pode mandar esperar até 900s — pendurar por 15 minutos uma rodada que
é diária não vale a pena, e o erro que sobe diz em quantos segundos libera.
Com dezenas de posts por dia contra 900 requisições/15min, o 429 é
praticamente inalcançável: isto existe para a rodada não morrer em silêncio,
não porque se espere bater no teto."""

TETO_PAGINAS = 40
"""Máximo de páginas que uma leitura de timeline pede, aconteça o que
acontecer.

A janela do endpoint é de 3.200 posts a no máximo 100 por página: 32 páginas
esgotam tudo que existe para ler, e 40 cobre isso com folga. Quem passar
daqui não está paginando, está girando.

Existe porque as outras paradas do laço são todas CONDICIONAIS ao que o
servidor devolve, e por isso nenhuma delas é garantia. Uma página com `data`
vazio e um `next_token` NOVO a cada volta não faz a contagem de posts subir
nem repete o token: as duas paradas antigas nunca chegariam, e o laço pagaria
uma requisição por volta, indefinidamente, com o dinheiro do dono. Este é o
único limite que não depende da boa-fé do outro lado.

NÃO CONFIRMADO que o servidor faça isso — não há caso observado. É teto de
segurança contra um modo de falha barato de prevenir e caro de descobrir pela
fatura."""


class FalhaNaAPI(Exception):
    """A leitura não pôde ser feita ou a resposta não pôde ser lida."""


class _Erro400(FalhaNaAPI):
    """Requisição recusada. Privado porque só a sonda de nomenclatura o
    distingue — para quem chama de fora, continua sendo `FalhaNaAPI`."""


@dataclass(frozen=True, slots=True)
class Post:
    """Um post como este módulo o entrega. `tipo` é derivado, não pedido."""

    id: str
    autor: str
    criado_em: str
    texto: str
    tipo: str
    pai_id: str = ""
    url: str = ""


# --- O conflito de nomenclatura -------------------------------------------
#
# Duas páginas primárias da própria X discordam sobre os nomes dos campos
# (conferido em 04/09/2026): as páginas de fundamentos dizem `tweet.fields`,
# `note_tweet` e `referenced_tweets`; a referência DO ENDPOINT diz
# `post.fields`, `note_post` e `referenced_posts`. O openapi.json, que seria o
# árbitro, passou a responder 402 — não há terceiro a consultar.
#
# Por isso a sonda: tenta o conjunto novo e, diante de um 400, repete UMA vez
# com o antigo. Escolher um e torcer é apostar num empate documental.
#
# OBSERVADO em 05/09/2026, na primeira leitura real: o servidor aceitou o
# conjunto NOVO (`post.fields`) na primeira tentativa, sem 400. A sonda fica
# porque a doc segue contraditória e o custo dela no caminho feliz é zero.
#
# O CAMINHO não está em disputa: a doc confirma `/2/users/:id/tweets`, com
# `tweets` mesmo na página que renomeia os campos.


@dataclass(frozen=True, slots=True)
class _Nomes:
    parametro: str
    nota: str
    referencias: str


NOVO = _Nomes("post.fields", "note_post", "referenced_posts")
ANTIGO = _Nomes("tweet.fields", "note_tweet", "referenced_tweets")

_vencedor: _Nomes | None = None
"""Qual conjunto o servidor aceitou. Guardado no módulo para a sonda não
custar uma requisição recusada a cada página — a paginação de uma janela
grande faria a mesma descoberta dezenas de vezes. É preferência de ordem,
não trava: `_pagina` continua tentando o outro dialeto se o guardado falhar.

Só é gravado depois de um 200. Gravar na tentativa erraria feio: um 400 pode
vir de `start_time` malformado, e aí a sonda concluiria coisa sobre
nomenclatura a partir de um erro que não é dela."""


def nomenclatura_em_uso() -> str:
    """O conjunto que a sonda validou, ou "" antes da primeira leitura boa."""
    return _vencedor.parametro if _vencedor else ""


def _esquece_nomenclatura() -> None:
    """Zera a sonda. Existe para o teste, que precisa de estado limpo."""
    global _vencedor
    _vencedor = None


def custo_estimado_usd(posts_lidos: int) -> float:
    """ESTIMATIVA do custo, feita no CLIENTE — não é medição.

    Multiplica posts lidos por US$ 0,005, o preço por recurso devolvido lido
    na doc em 04/09/2026. A API não devolve preço na resposta: isto é conta
    nossa, e apresentá-la como medição seria mentir sobre a origem do
    número.

    É um TETO, não um valor esperado: a doc diz que os recursos são
    deduplicados dentro da janela de 24h UTC, então reler o mesmo post no
    mesmo dia tende a não cobrar de novo — e esta função conta de novo. O
    valor real só sai da fatura.
    """
    return max(0, posts_lidos) * PRECO_POR_POST_USD


# --- Derivação do tipo (o coração do módulo) ------------------------------

def classifica(post: dict) -> tuple[str, str]:
    """(tipo, pai_id) a partir do metadado do servidor.

    Função pura: não toca em rede, e é o coração do módulo. As regras, na
    ordem em que são aplicadas:

    * `referenced` do tipo `replied_to` + autor do referenciado IGUAL ao autor
      do post -> `thread` (o autor continuando a si mesmo)
    * `referenced` do tipo `replied_to` + autor diferente -> `resposta`
    * `referenced` do tipo `retweeted` OU `reposted` (o nome novo, que o
      servidor de fato manda — medido em 06/09/2026) -> `retweet`
    * `referenced` do tipo `quoted` -> `citacao`
    * sem `referenced` e texto começando por "RT @" -> `retweet` (a forma
      antiga, copiando o texto)
    * sem `referenced` e `conversation_id == id` -> `post` (raiz de conversa)

    `replied_to` é examinado ANTES de `quoted` porque os dois coexistem quando
    o autor cita alguém dentro de uma resposta, e o que o projeto não pode
    deixar passar é resposta a terceiro — o rótulo mais restritivo ganha.

    A comparação de autor é por ID, nunca por nome: `in_reply_to_user_id`
    contra `author_id`. Handle escrito muda, abrevia e chega truncado; o id
    de uma conta não muda.

    O autor do referenciado NÃO sai daqui: ele só é conhecido quando o
    referenciado foi lido — na mesma rodada, ou buscado à parte por
    `posts_por_id` — e é o radar que o resolve pelo índice da rodada. O id
    do pai está em `pai_id` para isso.

    Falha FECHADO: post sem `referenced` e sem `conversation_id` igual ao
    próprio id vira `resposta`. Sem metadado não dá para provar que é raiz, e
    o custo dos dois erros é assimétrico — o projeto já decidiu, em
    `radar.separa_por_tipo`, que prefere perder post legítimo a deixar
    entrar resposta a terceiro. O risco de perder é pequeno: o campo vem do
    servidor, e se ele sumir some para TODOS os posts de uma vez, o que
    aparece como uma rodada inteira virando `resposta` — barulhento, não
    silencioso.
    """
    autor_id = str(post.get("author_id") or "")

    # Os dois nomes são lidos SEMPRE, independentemente do que a sonda tenha
    # escolhido na requisição: ler é grátis e o empate documental não deve
    # vazar para cá.
    referencias = (post.get("referenced_posts")
                   or post.get("referenced_tweets") or ())
    por_tipo: dict[str, str] = {}
    for item in referencias:
        if isinstance(item, dict) and item.get("type"):
            por_tipo.setdefault(str(item["type"]), str(item.get("id") or ""))

    # OBSERVADO em 06/09/2026, na primeira leitura do segundo handle: o
    # servidor marca o repost como `{"type": "reposted"}` — o nome NOVO,
    # par de `post.fields`/`referenced_posts`; a doc de fundamentos só fala
    # em `retweeted`. Um "RT @OutsOficial: …" passou como `post` e o
    # separador tratou a palavra de terceiro como premissa do autor. Os dois
    # nomes são lidos, como nos demais campos. E texto começando por "RT @"
    # sem referência nenhuma (a forma antiga, copiando) é retweet também:
    # não traz palavra do autor.
    if "retweeted" in por_tipo or "reposted" in por_tipo:
        return "retweet", por_tipo.get("retweeted") or por_tipo.get("reposted")
    if not por_tipo and str(post.get("text") or "").startswith("RT @"):
        return "retweet", ""

    if "replied_to" in por_tipo:
        pai_id = por_tipo["replied_to"]
        pai_uid = str(post.get("in_reply_to_user_id") or "")
        if pai_uid and autor_id and pai_uid == autor_id:
            return "thread", pai_id
        # Sem `in_reply_to_user_id` não há como afirmar que o pai é o próprio
        # autor, e presumir que é seria falhar aberto.
        return "resposta", pai_id
    if "quoted" in por_tipo:
        return "citacao", por_tipo["quoted"]

    ident = str(post.get("id") or "")
    if ident and str(post.get("conversation_id") or "") == ident:
        return "post", ""
    return "resposta", ""


def texto_integral(post: dict) -> str:
    """O texto do post, preferindo `note_*` quando existir.

    `note_post`/`note_tweet` carrega o texto de post acima de 280 caracteres.
    A doc NÃO afirma que `text` volta truncado quando existe nota — a
    preferência aqui é PRECAUÇÃO, não fato documentado: se o campo longo
    existe e traz mais texto, usar o curto seria perder premissa por escolha
    nossa. Os dois nomes são aceitos pelo mesmo motivo do `classifica`.
    """
    for chave in ("note_post", "note_tweet"):
        nota = post.get(chave)
        if isinstance(nota, dict) and str(nota.get("text") or "").strip():
            return str(nota["text"])
    return str(post.get("text") or "")


# --- HTTP -----------------------------------------------------------------

def _espera_do_429(resposta) -> int:
    """Quantos segundos o servidor pediu num 429.

    Lê `Retry-After` e `x-rate-limit-reset` (epoch em segundos) e fica com o
    maior. O material que fundamenta este módulo confirma o limite de 900
    requisições/15min por usuário, mas NÃO confirma o nome desses cabeçalhos —
    são a convenção da v2, e aqui estão marcados como não confirmados. Se
    nenhum vier, devolve 0 e quem chama decide; inventar uma espera padrão
    seria fingir que o servidor disse algo.
    """
    cabecalhos = getattr(resposta, "headers", None) or {}
    def _inteiro(nome: str) -> int:
        try:
            return int(cabecalhos.get(nome, 0) or 0)
        except (AttributeError, TypeError, ValueError):
            return 0
    reset = _inteiro("x-rate-limit-reset")
    falta = max(0, reset - int(time.time())) if reset else 0
    return max(_inteiro("Retry-After"), falta)


def _pede(caminho: str, params: dict, dormir=time.sleep) -> dict:
    """GET autenticado, com o tratamento de erro que cada código merece.

    No máximo três idas: a original, uma espera de 429 e uma repetição curta
    de 5xx. `dormir` existe para o teste não esperar de verdade.
    """
    repetiu_5xx = False
    esperou_429 = False
    while True:
        # O cabeçalho é montado A CADA volta, nunca uma vez antes do laço, e o
        # motivo é aritmética: TETO_ESPERA (300) é o MESMO número de
        # x_auth.FOLGA_SEGUNDOS (300), e `token()` só promete que o token
        # cacheado tem MAIS de 300s de vida. Dormir os 300s do teto de um 429
        # consome essa promessa inteira; reaproveitar o cabeçalho depois disso
        # manda a repetição com um token que pode ter acabado de expirar, e o
        # 401 resultante sobe como PrecisaAutorizar — o dono reautorizaria no
        # navegador por causa de uma espera, não de uma credencial ruim.
        # Rechamar não custa requisição: `token()` só vai à rede se a folga
        # tiver acabado, e é exatamente esse o caso que precisa dela.
        try:
            cabecalho = {"Authorization": f"Bearer {token()}"}
        except requests.RequestException as erro:
            # `x_auth` deixa 5xx e queda de rede da RENOVAÇÃO subirem como
            # RequestException de propósito — não são credencial inválida.
            # Traduzir aqui é o que mantém a fachada em duas exceções (ver o
            # cabeçalho do módulo); sem isto, `posts_de` levantaria uma
            # terceira classe que o docstring não promete.
            raise FalhaNaAPI(
                f"não deu para obter o token do X para {caminho}: "
                f"{type(erro).__name__}: {erro}") from erro
        try:
            resposta = requests.get(URL_BASE + caminho, headers=cabecalho,
                                    params=params, timeout=TIMEOUT)
        except requests.RequestException as erro:
            # Falha de transporte é da mesma família do 5xx: passageira por
            # definição, e gasta a mesma repetição curta.
            if repetiu_5xx:
                raise FalhaNaAPI(
                    f"GET {caminho} falhou: {type(erro).__name__}: {erro}"
                ) from erro
            repetiu_5xx = True
            dormir(ESPERA_5XX)
            continue

        codigo = resposta.status_code
        corpo = (resposta.text or "")[:300]

        if codigo == 401:
            # Sobe como PrecisaAutorizar, e não como FalhaNaAPI, porque a ação
            # é outra: quem trata isto manda o dono reautorizar no navegador.
            # A FAQ oficial é explícita em assumir o pior — "assume a user's
            # access token may become invalid at any time" —, então token
            # recusado não é caso excepcional, é caso previsto.
            raise PrecisaAutorizar(
                f"o X recusou o token em {caminho} (401): {corpo}")
        if codigo == 403:
            # A doc NUNCA afirma que este endpoint devolve post de conta
            # protegida. A única evidência é a descrição do escopo tweet.read
            # ("View all posts you can see, including those from protected
            # accounts"), que fala do que o usuário enxerga, não do que o
            # endpoint entrega. NÃO CONFIRMADO — por isso a mensagem diz
            # "pode ser", e não afirma a causa.
            raise FalhaNaAPI(
                f"o X negou o acesso em {caminho} (403): {corpo}. Pode ser "
                "conta protegida sem permissão para esta autorização — a doc "
                "não confirma o comportamento do endpoint com conta "
                "protegida —, ou escopo faltando (tweet.read, users.read).")
        if codigo == 400:
            raise _Erro400(f"o X recusou a requisição em {caminho} (400): "
                           f"{corpo}")
        if codigo == 429:
            espera = _espera_do_429(resposta)
            if esperou_429 or espera > TETO_ESPERA:
                raise FalhaNaAPI(
                    f"limite de requisições do X em {caminho} (429); o "
                    f"servidor libera em {espera}s")
            esperou_429 = True
            dormir(espera)
            continue
        if codigo >= 500:
            if repetiu_5xx:
                raise FalhaNaAPI(f"o X respondeu {codigo} em {caminho} "
                                 f"duas vezes: {corpo}")
            repetiu_5xx = True
            dormir(ESPERA_5XX)
            continue
        if codigo >= 400:
            raise FalhaNaAPI(f"o X respondeu {codigo} em {caminho}: {corpo}")

        try:
            dados = resposta.json()
        except ValueError as erro:
            raise FalhaNaAPI(
                f"corpo 200 ilegível em {caminho}: {erro}") from erro
        # Corpo 200 com JSON válido fora do formato (uma lista, um null, um
        # erro de proxy) escaparia limpo daqui e estouraria AttributeError
        # lá adiante, onde ninguém espera. O contrato fecha aqui.
        if not isinstance(dados, dict):
            raise FalhaNaAPI(f"corpo 200 não é objeto JSON em {caminho}: "
                             f"{type(dados).__name__}")
        return dados


def _campos(nomes: _Nomes) -> dict:
    """Os campos pedidos, no dialeto que a sonda estiver testando."""
    return {nomes.parametro: ",".join((
        "id", "text", "created_at", "author_id", "conversation_id",
        "in_reply_to_user_id", nomes.referencias, nomes.nota))}


def _com_dialeto(caminho: str, params: dict, dormir=time.sleep) -> dict:
    """Um GET com os campos de post, resolvendo o conflito de nomenclatura.

    Serve à timeline e à busca por id: os dois pedem os mesmos campos, e a
    sonda é uma só — o vencedor descoberto num caminho vale para o outro."""
    global _vencedor
    # O vencedor guardado só reordena a fila, não a encurta: se a X mudar de
    # ideia sobre os nomes (o empate documental é o aviso de que ela ainda não
    # decidiu), o cliente cai no outro dialeto em vez de morrer repetindo o
    # que passou a ser recusado. O custo disso no caminho feliz é zero — a
    # primeira tentativa acerta e a segunda nem acontece.
    ordem = (ANTIGO, NOVO) if _vencedor is ANTIGO else (NOVO, ANTIGO)
    ultimo = ""
    for nomes in ordem:
        try:
            dados = _pede(caminho, {**params, **_campos(nomes)}, dormir)
        except _Erro400 as erro:
            ultimo = str(erro)
            continue
        _vencedor = nomes
        return dados
    # Os dois dialetos recusados. Dizer "a nomenclatura está errada" seria
    # concluir demais: 400 também é start_time malformado, max_results fora da
    # faixa, id inexistente. A mensagem entrega o corpo do servidor e diz o
    # que se tentou.
    raise FalhaNaAPI(
        f"o X recusou os dois conjuntos de nomes ({NOVO.parametro} e "
        f"{ANTIGO.parametro}) em {caminho}. O 400 pode não ser de "
        f"nomenclatura — start_time, max_results e ids caem aqui igual. "
        f"Último: {ultimo}")


def _pagina(id_usuario: str, params: dict, dormir=time.sleep) -> dict:
    """Uma página da timeline, resolvendo o conflito de nomenclatura."""
    return _com_dialeto(f"/users/{id_usuario}/tweets", params, dormir)


# --- Interface pública ----------------------------------------------------

_SO_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_ids: dict[str, str] = {}
"""handle minúsculo -> id numérico. O id de uma conta não muda quando o
username muda, então repetir a consulta a cada rodada é gasto de cota por
dado estável. O preço da consulta de usuário NÃO está no material confirmado
(a tabela citada cobre o post lido) — pendência, e mais um motivo para não
repeti-la."""


def _limpa_handle(handle: str) -> str:
    return str(handle or "").strip().lstrip("@").strip()


def _iso(quando: str) -> str:
    """Completa uma data solta para o ISO8601 que o endpoint pede.

    `2026-09-01` é o que se digita naturalmente e é o que o servidor recusa
    com 400 — e um 400 aqui seria lido pela sonda como conflito de
    nomenclatura, mandando o cliente repetir a chamada com o dialeto antigo e
    culpar o empate documental por um erro de digitação. Completar aqui evita
    o diagnóstico errado.
    """
    texto = str(quando or "").strip()
    return f"{texto}T00:00:00Z" if _SO_DATA.match(texto) else texto


def _quantos(restante: int) -> int:
    """`max_results` da próxima página: entre 5 e 100, conforme a doc.

    O piso de 5 é do servidor, não nosso: pedindo os últimos 2 posts de uma
    janela, chegam 5 e PAGAM-SE 5. O corte para o `limite` acontece depois, em
    memória, e não devolve dinheiro — quem chama com limite baixo deve saber
    que o mínimo cobrado é 5 posts por página.
    """
    return max(5, min(100, restante))


def id_do_handle(handle: str) -> str:
    """O id numérico de um handle. Cacheado no módulo."""
    nome = _limpa_handle(handle)
    if not nome:
        raise FalhaNaAPI("handle vazio")
    if nome.lower() in _ids:
        return _ids[nome.lower()]
    dados = _pede(f"/users/by/username/{nome}", {})
    ident = str((dados.get("data") or {}).get("id") or "")
    if not ident:
        raise FalhaNaAPI(f"o X não devolveu id para @{nome}: {dados}")
    _ids[nome.lower()] = ident
    return ident


def posts_de(handle: str, desde: str, ate: str = "",
             limite: int = 100) -> list[Post]:
    """Os posts de um handle na janela pedida, com o tipo já derivado.

    `desde` e `ate` em ISO8601 (data solta é completada por `_iso`). `limite`
    é teto de posts devolvidos, e cada página custa — ver `custo_estimado_usd`
    e o piso de 5 em `_quantos`.

    A janela do endpoint é de 3.200 posts recentes; para os handles do projeto
    (dezenas por dia) isso não é limite prático, mas é o que existe: pedir
    mais que isso não traz mais. A leitura para em `TETO_PAGINAS` páginas de
    qualquer jeito, mesmo que o servidor continue oferecendo token.

    Levanta `FalhaNaAPI` ou `PrecisaAutorizar`, e nada além disso — inclusive
    quando a falha nasce na renovação do token (ver o cabeçalho do módulo).
    """
    nome = _limpa_handle(handle)
    id_usuario = id_do_handle(nome)
    base: dict = {"start_time": _iso(desde)}
    if ate:
        base["end_time"] = _iso(ate)

    colhidos: list[Post] = []
    pagina = ""
    paginas = 0
    while len(colhidos) < limite and paginas < TETO_PAGINAS:
        paginas += 1
        params = dict(base)
        params["max_results"] = _quantos(limite - len(colhidos))
        if pagina:
            # A doc confirma que o token chega em `meta.next_token`; devolvê-lo
            # como `pagination_token` é a convenção da v2 e NÃO está confirmado
            # no material que fundamenta este módulo.
            params["pagination_token"] = pagina
        dados = _pagina(id_usuario, params)
        rendeu = 0
        for cru in (dados.get("data") or []):
            if isinstance(cru, dict):
                colhidos.append(_monta(cru, nome))
                rendeu += 1
        seguinte = str((dados.get("meta") or {}).get("next_token") or "")
        # Quatro paradas, e é preciso ter as quatro. Sem token, ou com o token
        # repetido, é o fim normal da janela. `rendeu == 0` é a página que
        # volta sem NENHUM post aproveitado: continuar dali é pagar por
        # requisição que não traz dado, e é o caso em que as duas primeiras
        # paradas falham juntas — `data` vazio com um `next_token` novo a cada
        # volta não faz `colhidos` crescer nem repete o token. O teto de
        # páginas, no `while`, é a rede embaixo das três: laço que paga por
        # volta não pode ter só paradas que o servidor controla.
        if not seguinte or seguinte == pagina or rendeu == 0:
            break
        pagina = seguinte
    return colhidos[:limite]


LOTE_IDS = 100
"""Quantos ids por requisição em `posts_por_id`: é o teto da doc para o
endpoint de lookup em lote."""


def posts_por_id(ids, autores: dict | None = None,
                 dormir=time.sleep) -> list[Post]:
    """Os posts pedidos por id, com o tipo derivado e o autor resolvido.

    É a busca À PARTE do referenciado (ver o cabeçalho do módulo): o radar
    chama com os ids que faltam às capturas, e paga só por eles. Caminho
    `/2/tweets` com `ids`, os mesmos campos da timeline (mesma sonda de
    nomenclatura), mais `expansions=author_id` e `user.fields=username`
    para o handle vir em `includes.users` — sem ele não há como dizer se o
    citado é o próprio autor ou terceiro, que é a atribuição que o
    separador precisa (`Captura.contexto_proprio`).

    `autores` (id da conta → handle) é o que o chamador já sabe: o radar
    conhece o id do handle que leu, sem custo. Quando nem `includes` nem
    `autores` resolvem o autor, o handle sai como "id:<número>": honesto e
    visível, em vez de um nome inventado — e sem URL, que seria link
    fabricado. Id inexistente, apagado ou de conta protegida vem em
    `errors` e simplesmente não volta — quem chama conta a diferença pelos
    IDS que pediu, não pelo número de posts que voltou.

    Ids chegam normalizados (espaço fora, duplicata fora, ordem mantida):
    id com espaço faz o servidor responder 400, e a sonda leria isso como
    conflito de nomenclatura. Cada lote traz também os objetos de USUÁRIO
    em `includes.users`: recurso devolvido, logo cobrado pela regra que
    este módulo assume — o radar os conta no teto.

    LOTE QUE FALHA DEPOIS DE OUTRO TER VOLTADO: os posts já devolvidos
    foram pagos e existem. A exceção sobe (é a superfície de duas classes),
    mas leva o que veio em `erro.parciais`, para o chamador usar e contar
    — descartá-los seria pagar duas vezes pelo mesmo post.

    OBSERVADO em 06/09/2026, na primeira leitura real: o servidor aceitou
    `post.fields` também neste caminho (sem 400) e devolveu `includes.users`
    com `username` — três referenciados pedidos, três voltaram, dois de
    outras contas e um do próprio autor.
    """
    pedidos: list[str] = []
    for i in ids:
        ident = str(i or "").strip()
        if ident and ident not in pedidos:
            pedidos.append(ident)
    if not pedidos:
        return []
    conhecidos = {str(k): str(v) for k, v in (autores or {}).items()}
    achados: list[Post] = []
    for inicio in range(0, len(pedidos), LOTE_IDS):
        lote = pedidos[inicio:inicio + LOTE_IDS]
        try:
            dados = _com_dialeto("/tweets", {
                "ids": ",".join(lote),
                "expansions": "author_id",
                "user.fields": "username",
            }, dormir)
        except (FalhaNaAPI, PrecisaAutorizar) as erro:
            erro.parciais = list(achados)
            raise
        # `_pede` só garante que o corpo é um objeto; as sub-formas são
        # conferidas aqui, uma a uma — `includes` que vem como lista não
        # pode virar AttributeError fora da superfície de exceção.
        includes = dados.get("includes")
        crus_usuarios = includes.get("users") if isinstance(includes, dict) else None
        usuarios: dict[str, str] = {}
        for u in (crus_usuarios if isinstance(crus_usuarios, list) else []):
            if isinstance(u, dict) and u.get("id") and u.get("username"):
                usuarios[str(u["id"])] = str(u["username"])
        crus = dados.get("data")
        for cru in (crus if isinstance(crus, list) else []):
            if not isinstance(cru, dict):
                continue
            autor_id = str(cru.get("author_id") or "")
            nome = (usuarios.get(autor_id) or conhecidos.get(autor_id)
                    or (f"id:{autor_id}" if autor_id else ""))
            achados.append(_monta(cru, nome))
    return achados


def _monta(cru: dict, nome: str) -> Post:
    tipo, pai_id = classifica(cru)
    ident = str(cru.get("id") or "")
    # O objeto Post da API não traz a URL do próprio post; ela se monta. Sem
    # id não há link, e link inventado é pior que link ausente — inclusive
    # quando o autor não foi resolvido ("id:<número>" não é handle).
    com_handle = bool(nome) and not nome.startswith("id:")
    return Post(
        id=ident,
        autor=nome,
        criado_em=str(cru.get("created_at") or ""),
        texto=texto_integral(cru),
        tipo=tipo,
        pai_id=pai_id,
        url=f"https://x.com/{nome}/status/{ident}" if ident and com_handle else "",
    )
