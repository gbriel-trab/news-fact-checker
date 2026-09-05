"""Radar de rede social: o que os handles acompanhados estão alegando.

    python -m src.radar                     # posts recentes dos handles
    python -m src.radar --dias 5            # janela maior
    python -m src.radar --conferir 2        # captura e confere o post 2
    python -m src.radar --dry-run           # mostra o que seria enviado

O papel está fixado no ARCHITECTURE.md: rede social é RADAR, nunca evidência.
O post indica onde olhar; a evidência vem sempre da imprensa ou da
instituição. Nada do que este módulo captura entra no acervo.

FONTE ÚNICA: a API OFICIAL do X, um handle por vez (`x_api.posts_de`). O
texto do post é o registro do servidor — literal do autor, não transcrição
— e o tipo (post/thread/quote/resposta/retweet) é CALCULADO de metadado em
`x_api.classifica`, comparando `in_reply_to_user_id` contra `author_id`.
Nada aqui é rótulo pedido a um modelo: rótulo pedido seria opinião,
metadado de servidor é dado. As barreiras de `_barreiras` leem só o
CABEÇALHO do bloco, porque o corpo é do autor; cada uma leva escrito o
que a faz disparar e por quê.

Honestidade que a saída carrega sempre: conferir premissas de um post é
CONFERÊNCIA, nunca placar do autor. Premissa sem evidência = o acervo não
cobre, não "o autor errou".

Custo: ESTIMATIVA feita no cliente (`x_api.custo_estimado_usd`), e um
TETO. A regra do dono é custo estimado antes e REAL depois; por esta via o
real só existe na fatura do X — ver `busca`.
"""

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from . import config, x_api
from .x_auth import PrecisaAutorizar

LIMITE_POR_HANDLE = 100
"""Teto de posts lidos por handle numa rodada. É teto de GASTO, não de
janela: a API do X cobra por recurso devolvido (US$ 0,005), então 100
posts são US$ 0,50 por handle no pior caso — e o pior caso é uma janela
larga num handle prolífico. A rodada típica (2 dias, ~15 posts/dia) fica
perto de US$ 0,15. Ver `x_api.custo_estimado_usd`: o número é teto, porque
a cobrança é deduplicada dentro da janela de 24h UTC."""

_TIPO_NO_BLOCO = {"post": "post", "thread": "thread",
                  "citacao": "quote", "resposta": "resposta"}
"""`Post.tipo` (vocabulário do `x_api`) -> o rótulo do bloco.

`retweet` NÃO está no mapa e é descartado antes de virar bloco: não traz
palavra do autor, e o formato do bloco — congelado, quatro consumidores o
reparseiam — só admite post|thread|quote|resposta. Inventar um quinto valor
para acomodá-lo mudaria o formato, e o formato não muda."""


class FalhaNoRadar(Exception):
    """A busca não pôde ser feita ou a resposta não pôde ser lida."""


@dataclass(frozen=True, slots=True)
class Rodada:
    """O que uma busca devolveu: posts, avisos da rodada, links e custo."""

    posts: tuple[str, ...]
    notas: tuple[str, ...]
    links: tuple[str, ...]
    custo_usd: float
    bruto: str
    detalhe_custo: str = ""
    """De onde o custo veio (posts devolvidos × preço unitário), legível.

    Só o total não diz qual alavanca puxar: janela mais curta, menos
    handles, ou o teto por handle."""


def _handles_de(argumento: str) -> tuple[str, ...]:
    """Normaliza ANTES de filtrar: '@' sozinho vira vazio e cai fora.

    Na ordem inversa, '@' sobrevive ao filtro e vira handle vazio depois
    do lstrip — o guard de lista vazia vê um tuple de um elemento e não
    protege nada, e o handle vazio só morre em `x_api.id_do_handle`, como
    falha de leitura em vez de entrada ignorada.
    """
    return tuple(x for x in
                 (h.strip().lstrip("@").strip()
                  for h in argumento.split(","))
                 if x)


_RE_URL_BLOCO = re.compile(
    r"^\s*URL:\s*(https://x\.com/[\w./]*status/(\d+))", re.MULTILINE)
_RE_LINHA_URL = re.compile(r"^\s*URL:[^\n]*\n?", re.MULTILINE | re.IGNORECASE)
_RE_RESPOSTA_CAPT = re.compile(r"^\s*EM RESPOSTA A\s*([^\n]*)$",
                               re.MULTILINE | re.IGNORECASE)
_RE_CITANDO_CAPT = re.compile(r"^\s*CITANDO\s*([^\n]*)$",
                              re.MULTILINE | re.IGNORECASE)

_RE_LINHA_DE_CONTEXTO = re.compile(r"\s*(URL:|TIPO:|EM RESPOSTA A|CITANDO)",
                                   re.IGNORECASE)


def _cabecalho(bloco: str) -> tuple[str, str]:
    """Parte o bloco em (cabeçalho, corpo). `cab + corpo == bloco`, sempre.

    O cabeçalho é a linha `POST N (@handle, data):` mais a SEQUÊNCIA de
    linhas de contexto logo abaixo dela — URL:, TIPO:, EM RESPOSTA A,
    CITANDO. Acaba na primeira linha que não é de contexto; o resto é corpo.

    As quatro marcas são escritas pelo RADAR (`_bloco`), nunca pelo autor
    — mas o texto do post é LITERAL do autor, e varrer o bloco INTEIRO
    atrás delas seria perigoso: um post cujo corpo tenha uma linha
    começando com "EM RESPOSTA A" faria `resposta_a_terceiro` achar um
    interlocutor que não existe e DESCARTAR o post próprio antes do
    boletim. Marca no corpo é texto do autor; só a do cabeçalho é metadado.

    O risco que SOBRA, e que não dá para tirar sem mexer no formato (ele
    está congelado, quatro consumidores o reparseiam): se a PRIMEIRA linha
    do corpo começar com uma das quatro marcas, ela fica colada ao
    cabeçalho. Ali ela é indistinguível de uma linha escrita pelo radar.
    """
    corte = 0
    pos = 0
    for i, linha in enumerate(bloco.split("\n")):
        if i and not _RE_LINHA_DE_CONTEXTO.match(linha):
            break
        pos += len(linha) + 1  # +1 pelo "\n" que o split consumiu
        corte = min(pos, len(bloco))
    return bloco[:corte], bloco[corte:]


def para_separacao(bloco: str) -> str:
    """O bloco como o separador de premissas deve vê-lo.

    A linha URL: sai (ruído de tokens); as linhas EM RESPOSTA A e CITANDO
    viram contexto com a ATRIBUIÇÃO certa. A captura exclui resposta a
    terceiro (neste domínio a substância vive em post, quote e thread
    própria), então EM RESPOSTA A aponta o post ANTERIOR DA PRÓPRIA THREAD
    — palavras do mesmo autor, e rotulá-las de "interlocutor" poria
    premissa legítima sob suspeita. A comparação de handle decide: mesmo
    handle do cabeçalho → contexto do próprio autor; outro handle →
    reatribuído a quem falou. Pelo caminho normal esse "outro handle" não
    chega a aparecer aqui (`_bloco` só escreve a linha para thread, e em
    thread o pai é o próprio autor por `x_api.classifica`); o ramo fica
    porque a função é o que `boletim._confere_post` chama, e ele não pode
    depender dessa cadeia. (Quando o autor reescreve o citado no próprio
    corpo — caso RIOT — a linha nem aparece; a atribuição das aspas é
    problema do separador.)

    Só o CABEÇALHO é reescrito: o corpo é literal do autor, e uma linha
    dele que comece com "EM RESPOSTA A" viraria contexto atribuído a um
    interlocutor inventado — dentro do texto que vai para o separador
    pago. Ver `_cabecalho`.
    """
    cab, corpo = _cabecalho(bloco)
    m_cab = re.match(r"^POST\s+\d+\s*\((@\w+)", cab)
    handle_autor = (m_cab.group(1).lower() if m_cab else "")

    def _rotula_resposta(m: re.Match) -> str:
        conteudo = m.group(1).strip()
        m_quem = re.match(r"\((@\w+)", conteudo)
        quem = m_quem.group(1).lower() if m_quem else ""
        if quem and quem == handle_autor:
            return ("(contexto — post anterior do próprio autor na "
                    f"thread: {conteudo})")
        return ("(contexto — palavras do interlocutor, não do autor "
                f"do post: {conteudo})")

    sem_url = _RE_LINHA_URL.sub("", cab)
    com_resposta = _RE_RESPOSTA_CAPT.sub(_rotula_resposta, sem_url)
    return _RE_CITANDO_CAPT.sub(
        lambda m: ("(contexto — post citado pelo autor; as afirmações são "
                   f"de quem ele cita: {m.group(1).strip()})"),
        com_resposta) + corpo


def id_status(url: str) -> str | None:
    """O número do status numa URL do X, ou None se não houver."""
    m = re.search(r"status/(\d+)", url)
    return m.group(1) if m else None


def url_do_post(bloco: str, citados: tuple[str, ...]) -> tuple[str | None, bool]:
    """(URL que o cabeçalho do bloco traz, se o status dela está entre os
    links da rodada).

    As duas pontas saem do mesmo `Post.url`, montado do id do servidor,
    então `confere` só é falso para bloco que não veio de `busca`. A função
    fica porque `boletim._chaves_do_post` tira dela a chave forte de dedup
    ENTRE rodadas. A comparação é por ID do status, e não por URL inteira,
    porque o mesmo post aparece como x.com/i/status/N (link montado sem
    autor conhecido, ver `_url_de_status`) e x.com/handle/status/N.

    Lê só o CABEÇALHO: um `URL:` escrito pelo autor no corpo de um bloco
    sem linha URL: daria ao boletim uma âncora que aponta para outro post.
    Ver `_cabecalho`.
    """
    m = _RE_URL_BLOCO.search(_cabecalho(bloco)[0])
    if not m:
        return None, False
    ids_citados = {id_status(u) for u in citados}
    return m.group(1), m.group(2) in ids_citados


def resposta_a_terceiro(bloco: str, handles: tuple[str, ...]) -> str | None:
    """O handle a quem este bloco responde, se NÃO for um dos monitorados.

    Devolve None quando o bloco é post próprio, quote, ou continuação de
    thread do próprio autor — os três que o projeto quer.

    O QUE PODE DISPARAR AQUI, e é preciso ser exato: pelo caminho normal a
    comparação de handle é letra morta. Só chega a `filtra_respostas` bloco
    post/thread/quote (o resto morre em `declara_post_proprio`), e em
    thread o pai é o próprio autor por construção — `x_api.classifica` só
    devolve `thread` quando `in_reply_to_user_id == author_id`, e é dali
    que sai o handle que `_bloco` escreve. Sobra UM caso vivo, e é o risco
    residual que `_cabecalho` documenta: post próprio cujo TEXTO comece,
    na primeira linha, com "EM RESPOSTA A (@fulano" — ali a marca cola no
    cabeçalho e é indistinguível de metadado. Fica pelo custo assimétrico:
    manter é uma comparação de string por bloco; tirar é apostar que a
    cadeia `classifica` → `_bloco` → `declara_post_proprio` nunca vai ser
    reordenada.

    E lê SÓ O CABEÇALHO: o corpo é do autor, e um post que escrevesse uma
    linha começando com "EM RESPOSTA A" seria descartado por responder a
    um interlocutor que ele mesmo citou no texto. Barreira que descarta
    produto tem de ler metadado, e metadado aqui é o cabeçalho (ver
    `_cabecalho`)."""
    cab = _cabecalho(bloco)[0]
    m_resp = _RE_RESPOSTA_CAPT.search(cab)
    if not m_resp:
        return None
    # Sem exigir o parentese de fechamento: `boletim_posts.resumo` guarda
    # `resumo[:120]`, entao o handle pode chegar CORTADO ("@corte",
    # "@vuvie"). Exigir o ")" fazia a funcao falhar ABERTO justamente no
    # dado truncado — e falhar aberto aqui e deixar passar o que se quer
    # barrar.
    m_quem = re.search(r"\(@(\w+)", m_resp.group(0))
    if not m_quem:
        return None
    quem = m_quem.group(1).lower()
    proprios = {h.lower().lstrip("@") for h in handles}
    m_cab = re.match(r"^POST\s+\d+\s*\(@(\w+)", cab)
    if m_cab:
        proprios.add(m_cab.group(1).lower())
    # Prefixo nos DOIS sentidos, por causa do corte: "@perfil_t" e o
    # proprio autor truncado e tem de FICAR; "@terceiro" nao e prefixo de
    # ninguem monitorado e SAI. Comparar por igualdade crua descartaria a
    # thread propria truncada — o caso vizinho que o C25 depende.
    if any(quem.startswith(p) or p.startswith(quem) for p in proprios):
        return None
    return "@" + quem


def _id_proprio(bloco: str) -> str | None:
    """O status ID do post transcrito NESTE bloco (a linha URL).

    Cabeçalho só (ver `_cabecalho`): um `URL:` no corpo daria a este bloco
    a identidade de OUTRO post, e `dedup_por_status` jogaria
    fora o post legítimo que carregasse aquele id."""
    m = _RE_LINHA_URL.search(_cabecalho(bloco)[0])
    return id_status(m.group(0)) if m else None


def _id_do_pai(bloco: str) -> str | None:
    """O status ID do post RESPONDIDO, se o link tiver vindo.

    Cabeçalho só (ver `_cabecalho`): `filtra_respostas` camada 1 descarta
    por este id, e link de status colado no corpo pelo autor
    não é o pai do post — é texto dele."""
    m = _RE_RESPOSTA_CAPT.search(_cabecalho(bloco)[0])
    return id_status(m.group(0)) if m else None


_RE_TIPO = re.compile(r"^\s*TIPO:\s*(post|thread|quote|resposta)\b",
                      re.MULTILINE | re.IGNORECASE)


def declara_post_proprio(bloco: str) -> bool:
    """O bloco DECLARA ser post ou quote próprio? Falha FECHADO.

    Passa `post`, `thread` (o autor respondendo a si mesmo) e `quote`;
    cai `resposta` e cai quem não declara.

    É A BARREIRA QUE SEGURA A RESPOSTA A TERCEIRO, e o ramo vivo é o
    `TIPO: resposta`, que é o veredito do servidor: `x_api.classifica`
    compara `in_reply_to_user_id` com `author_id` e só chama de `resposta`
    o que responde a outra conta. `_bloco` transcreve esse tipo para a
    linha `TIPO:`, e aqui ele cai, antes de custar separação e check.

    O OUTRO RAMO — bloco que não declara nada — é letra morta pelo caminho
    normal: quem escreve a linha `TIPO:` é `_bloco`, e ela sai em todo
    bloco, sempre dentro do cabeçalho. Fica como PADRÃO da função, porque
    a função é pública e falhar fechado é a decisão do projeto: prefere-se
    perder post legítimo a deixar entrar resposta a terceiro. Filtro que
    depende de rótulo opcional falha aberto.

    Lê só o CABEÇALHO (ver `_cabecalho`). O ganho é o inverso do de
    `resposta_a_terceiro`: lá o corpo derrubaria post legítimo, aqui um
    `TIPO: post` escrito no corpo pelo autor abriria a barreira que falha
    fechado."""
    m = _RE_TIPO.search(_cabecalho(bloco)[0])
    if not m:
        return False
    # `thread` FICA: e o autor respondendo a si mesmo, e o C25 do
    # gabarito depende disso — a premissa so faz sentido com o post
    # anterior. Um TIPO de tres valores derrubaria a thread junto com a
    # resposta a terceiro, que e o caso vizinho que nao pode quebrar.
    return m.group(1).lower() in ("post", "thread", "quote")


def dedup_por_status(posts) -> tuple[tuple, int]:
    """Um post por status ID na rodada. Devolve (posts, quantos caíram).

    Pela API cada handle é uma leitura paginada própria e a paginação para
    quando o token repete, então a mesma leitura não devolve o mesmo post
    duas vezes. O que faz o filtro disparar é outra coisa, e é por DESENHO,
    não por defeito — `HANDLES_RADAR` vem de uma lista solta do `.env` (ver
    `config.py`) e `--handles` de uma lista solta da linha de comando;
    nenhuma das duas deduplica. O mesmo handle repetido ali é lido duas
    vezes (o id é o mesmo, `x_api._ids` casa por handle minúsculo) e rende
    dois blocos com o mesmo status. A leitura duplicada já foi paga; o que
    este filtro evita é pagar separação DUAS vezes pelo mesmo post e
    mandá-lo duas vezes ao Telegram.

    Fica ANTES do filtro de resposta, para não gastar nem o filtro com
    repetido. Bloco sem URL não tem identidade e passa — descartá-lo
    perderia post por falta de metadado, que é o erro caro."""
    vistos, ficam, caidos = set(), [], 0
    for bloco in posts:
        sid = _id_proprio(bloco)
        if sid and sid in vistos:
            caidos += 1
            continue
        if sid:
            vistos.add(sid)
        ficam.append(bloco)
    return tuple(ficam), caidos


def filtra_respostas(posts, handles) -> tuple[tuple, list]:
    """Descarta resposta a terceiro, SEGUINDO A CADEIA. (fica, descartado)

    Três camadas, e o que interessa a quem for mexer é por que cada uma
    dispara:

    1. ID DO PAI. Se o link do post respondido aponta para um status que
       NÃO está entre os posts vivos desta rodada, é resposta a terceiro.
       `meus` é montado depois de `declara_post_proprio`, logo o pai que
       era `TIPO: resposta` já não está lá — é esta camada que derruba a
       thread própria pendurada numa resposta a terceiro.
    2. CADEIA. Filho de bloco descartado cai junto. É o degrau seguinte do
       mesmo caso: neto de uma resposta a terceiro tem pai imediato ainda
       presente em `posts`, então a camada 1 não o pega. Itera até
       estabilizar.
    3. HANDLE. Última linha, e letra morta pelo caminho normal — só chega
       aqui bloco post/thread/quote, e em thread o handle do pai é o do
       próprio autor por construção (`x_api.classifica`). Fica pelo caso
       residual descrito em `resposta_a_terceiro`: texto do autor cuja
       PRIMEIRA linha imita a marca e cola no cabeçalho. Custa uma
       comparação de string.

    O que FICA: post próprio, quote, e continuação de thread própria — o
    autor respondendo a si mesmo, que é o caso do C25 e o único tipo de
    resposta que o dono do projeto quer ver."""
    meus = {i for i in (_id_proprio(b) for b in posts) if i}
    fora: dict[int, str] = {}
    for k, bloco in enumerate(posts):
        alvo = resposta_a_terceiro(bloco, handles)
        pai = _id_do_pai(bloco)
        if alvo:
            fora[k] = alvo
        elif pai and pai not in meus:
            # Tem link de pai e o pai não está entre os blocos vivos: ou é
            # de outra conta, ou é um `TIPO: resposta` que já caiu na
            # barreira anterior. Nos dois casos o que pende dele sai junto.
            fora[k] = "pai fora da conta do autor"
    # Cadeia: enquanto alguém novo cair, quem responde a ele cai também.
    mudou = True
    while mudou:
        mudou = False
        ids_fora = {_id_proprio(posts[k]) for k in fora}
        for k, bloco in enumerate(posts):
            if k in fora:
                continue
            pai = _id_do_pai(bloco)
            if pai and pai in ids_fora:
                fora[k] = "responde a bloco já descartado (cadeia)"
                mudou = True
    ficam = tuple(b for k, b in enumerate(posts) if k not in fora)
    return ficam, [(posts[k], m) for k, m in sorted(fora.items())]


def _barreiras(posts: tuple[str, ...], notas: tuple[str, ...],
               handles: tuple[str, ...]) -> tuple[tuple[str, ...],
                                                  tuple[str, ...]]:
    """As três barreiras, na ordem em que a saída de uma alimenta a
    seguinte: dedup primeiro, para as outras duas não gastarem comparação
    com bloco repetido; `declara_post_proprio` antes de `filtra_respostas`,
    porque é ele que tira da lista o pai `TIPO: resposta` de que a camada 1
    do outro depende (ver a camada 1 em `filtra_respostas`). A ordem NÃO é
    arbitrária: trocar as duas últimas desliga o descarte da thread própria
    pendurada numa resposta a terceiro.

    O comentário de cada bloco diz o que faz a barreira disparar, e a
    docstring da função correspondente diz o que nela é letra morta pelo
    caminho normal e por que fica assim mesmo. Barreira tirada por parecer
    redundante é resposta a terceiro de volta no boletim.
    """
    # DEDUP POR STATUS. Pela API cada handle é uma leitura paginada própria;
    # o que dispara aqui é o mesmo handle repetido em HANDLES_RADAR ou em
    # --handles — nenhuma das duas listas deduplica. Ver `dedup_por_status`.
    posts, repetidos = dedup_por_status(posts)
    if repetidos:
        notas = tuple(notas) + (
            f"{repetidos} bloco(s) repetido(s) da mesma busca descartado(s) "
            f"antes de custar",)
    # DECLARA POST PRÓPRIO. É a barreira que segura a resposta a terceiro, e
    # o ramo vivo é `TIPO: resposta` — comparação de user id feita pelo
    # servidor (`x_api.classifica`). O ramo "bloco sem TIPO cai" é letra
    # morta pelo caminho normal: quem escreve a linha é `_bloco`, e ela sai
    # sempre.
    sem_declaracao = [b for b in posts if not declara_post_proprio(b)]
    if sem_declaracao:
        posts = tuple(b for b in posts if declara_post_proprio(b))
        # A nota diz a CAUSA: ela vai para o Telegram, e causa errada na
        # nota é o dono decidindo com base em ficção.
        notas = tuple(notas) + (
            f"{len(sem_declaracao)} bloco(s) descartado(s) antes de custar "
            f"por TIPO: resposta — o servidor classificou como resposta a "
            f"terceiro (in_reply_to_user_id ≠ author_id)",)
    # FILTRA RESPOSTAS. Sobra o que a barreira anterior não pega: a camada 1
    # (ID do pai fora dos blocos vivos) derruba a thread própria pendurada
    # numa resposta a terceiro, e a 2 segue a cadeia dali para baixo. A 3
    # (handle) é letra morta pelo caminho normal e fica pelo caso residual
    # de `_cabecalho`. Ver `_bloco`: é por causa da camada 1 que o link do
    # pai de uma thread só entra quando o pai foi lido nesta rodada.
    posts, descartadas = filtra_respostas(posts, handles)
    if descartadas:
        motivos = ", ".join(sorted({m for _, m in descartadas}))
        notas = tuple(notas) + (
            f"{len(descartadas)} resposta(s) a terceiro descartada(s) "
            f"antes de custar: {motivos}",)
    return posts, tuple(notas)


def _data_do_cabecalho(criado_em: str) -> str:
    """`created_at` ISO8601 -> a data como o cabeçalho do bloco a escreve.

    O formato do cabeçalho é a data RFC 2822 em GMT — "Thu, 03 Sep 2026
    17:50:11 GMT". `format_datetime(usegmt=True)` produz exatamente essa
    forma e, ao contrário de `strftime("%a, %d %b ...")`, não depende do
    locale da máquina: dia e mês em inglês são tabela fixa do
    `email.utils`. Num Windows com locale pt-BR o strftime escreveria
    "qui, 03 set" e o cabeçalho mudaria de formato sem ninguém pedir.

    Data ilegível volta como veio, e data ausente vira "sem data": o
    cabeçalho tem de manter o parêntese (handle, data) porque
    `boletim._formata_telegram` extrai esse parêntese com `\\(([^)]+)\\)`
    para montar a linha do Telegram.
    """
    texto = str(criado_em or "").strip()
    if not texto:
        return "sem data"
    try:
        quando = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return texto
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    return format_datetime(quando.astimezone(timezone.utc), usegmt=True)


def _url_de_status(ident: str) -> str:
    """O link canônico de um status cujo autor não se conhece.

    Serve para o post PAI, que não foi lido: montar `x.com/<handle>/status/N`
    com o handle errado seria link inventado, e `url_do_post` compara por
    ID e não por URL inteira justamente porque as duas formas coexistem.

    A garantia desta função é o segmento `/i/`: é ele que dispensa o handle
    do autor no caminho. Sem ele, `x.com/status/N` põe a palavra `status` na
    posição do perfil e deixa de abrir post nenhum — e montar link sem saber
    o autor é o único motivo de a função existir. NÃO conferido contra o
    servidor do X: a forma sem `/i/` nunca foi aberta.

    A linha CITANDO não leva este link (sem o texto do citado ela não sai),
    e quem o põe no bloco é o ramo `resposta`. Duas consequências, e nenhuma
    é confortável: o único leitor do link é `_id_do_pai`, que casa por
    `status/(\\d+)` e não olha o resto do caminho — logo o `/i/` não é
    conferível por nenhum consumidor, só por quem abrir o link; e o bloco
    `TIPO: resposta` morre em
    `declara_post_proprio` ANTES de `filtra_respostas` camada 1, então o
    link é montado e nunca lido. Fica assim porque a alternativa é `_bloco`
    saber a ordem das barreiras para decidir o que escrever, e o formato do
    bloco não pode depender de quem o consome depois.
    """
    return f"https://x.com/i/status/{ident}" if ident else ""


def _bloco(numero: int, post, lidos: dict) -> str:
    """Um post da API no bloco de texto que o resto do projeto já sabe ler.

    O formato está congelado e é reparseado por quatro consumidores
    (`boletim._chaves_do_post`, `boletim._confere_post`,
    `boletim._formata_telegram` e `tests/test_gabarito.py`):

        POST N (@handle, data):
        URL: <link do próprio post>
        TIPO: post | thread | quote | resposta
        EM RESPOSTA A (@autor, <link>): <texto do post respondido>
        CITANDO (@autor): <texto do post citado>
        <texto do post>

    `lidos` é o índice {id -> Post} de tudo que ESTA rodada leu, e é o que
    permite preencher o texto do pai e do citado sem pagar expansão: no
    caso dominante do acervo eles são posts do próprio handle, dentro da
    mesma janela. Expandir o referenciado é outra cobrança (a API só o
    entrega como recurso extra — ver o cabeçalho de `x_api`), então com ele
    fora da janela a linha de contexto NÃO SAI, nos dois tipos: sem o texto
    do referenciado ela não carrega nada, e na thread o link do pai também
    fica de fora de propósito (ver o `else` abaixo). O que falta é CONTADO
    nas notas de `busca` — é lá que o dono lê o que não veio.

    O bloco montado aqui vai DIRETO para a `Rodada`: não existe, e não pode
    voltar a existir, um passo que o reparseie para "garantir o formato".
    Texto de post é literal do autor, que pode escrever `---` ou uma linha
    começando com `POST 9` — um parser que partisse nisso perderia a
    segunda metade de um post por causa de um traço dele, em silêncio.
    """
    data = _data_do_cabecalho(post.criado_em)
    linhas = [f"POST {numero} (@{post.autor}, {data}):"]
    if post.url:
        # Sem URL não há identidade: `dedup_por_status` deixa passar bloco
        # sem ela de propósito, e é melhor bloco sem link que link montado
        # com id vazio.
        linhas.append(f"URL: {post.url}")
    linhas.append(f"TIPO: {_TIPO_NO_BLOCO[post.tipo]}")

    pai = lidos.get(post.pai_id) if post.pai_id else None
    if post.tipo in ("thread", "resposta"):
        quem = post.pai_autor or (pai.autor if pai else "")
        if pai:
            link = pai.url
        elif post.tipo == "resposta":
            link = _url_de_status(post.pai_id)
        else:
            # THREAD com o pai fora da janela: o link FICA DE FORA, e isto
            # é decisão, não esquecimento. `filtra_respostas` camada 1 lê
            # "ID de pai que não está entre os posts desta rodada" como
            # "resposta a terceiro". O servidor já respondeu essa pergunta
            # por user id: `thread` é o autor continuando a si mesmo. Pôr o
            # link aqui faria a camada 1 contradizer o servidor e descartar
            # thread própria em série (janela de 2 dias, thread começada há
            # três). O link some, a contagem vai para as notas.
            link = ""
        dentro = ", ".join(p for p in (f"@{quem}" if quem else "", link) if p)
        texto_pai = pai.texto if pai else ""
        # A linha só entra se CARREGAR algo — texto do pai ou link para ele.
        # `EM RESPOSTA A (@handle):` seco é o caso da thread com pai fora da
        # janela, e não informa nada que `TIPO: thread` já não diga:
        # `para_separacao` a transformaria em "(contexto — post anterior do
        # próprio autor na thread: (@handle):)" e mandaria isso para um
        # separador que se paga por token. O que falta está CONTADO nas notas.
        if dentro and (texto_pai or link):
            linhas.append(f"EM RESPOSTA A ({dentro}): {texto_pai}".rstrip())
    elif post.tipo == "citacao":
        # `x_api.classifica` devolve `pai_autor` VAZIO para TODA citação — o
        # autor do citado só viria expandindo `referenced_*.id`, que é outra
        # cobrança —, então com o citado fora da janela não sobra handle nem
        # texto.
        quem = post.pai_autor or (pai.autor if pai else "")
        dentro = f"@{quem}" if quem else _url_de_status(post.pai_id)
        texto_citado = pai.texto if pai else ""
        # A GUARDA. O ramo irmão só põe a linha se ela CARREGAR algo; este
        # também. Sem a guarda o que sairia era `CITANDO (<link ou "post
        # citado">):` — marca, parêntese, dois-pontos e nada. Iria para o
        # Telegram e para o separador, que se paga por token, como
        # "(contexto — post citado pelo autor; as afirmações são de quem
        # ele cita: (...):)".
        #
        # O que a linha tem de carregar aqui é o TEXTO do citado, e só ele:
        # o formato está congelado em `CITANDO (@autor): <texto>` e não tem
        # casa para link — é nisto que a guarda difere da do irmão, onde o
        # link do pai vai DENTRO do parêntese e sozinho já informa. Quando a
        # linha não sai, o caso é CONTADO nas notas de `busca`.
        if dentro and texto_citado.strip():
            linhas.append(f"CITANDO ({dentro}): {texto_citado}")

    linhas.append(post.texto)
    return "\n".join(linhas).strip()


def busca(handles: tuple[str, ...], dias: int = 2) -> Rodada:
    """Uma rodada do radar: a API oficial do X, um handle por vez.

    ASSINATURA CONGELADA — `boletim.monta` e `painel.rodar_radar` chamam
    `busca(handles, dias)` e esperam uma `Rodada`. É o que mantém a fonte
    fora dos chamadores.

    O TIPO do post vem de `Post.tipo`, derivado em `x_api.classifica` a
    partir de `referenced_*`, `conversation_id` e `in_reply_to_user_id`.
    Não há rótulo pedido a modelo nenhum.

    JANELA. `desde` é um instante, não uma data: `start_time` é ISO8601 com
    hora e o fim é "agora" por omissão, então a rodada vê o próprio dia.

    FALHA POR HANDLE. `PrecisaAutorizar` aborta a rodada inteira na hora:
    sem consentimento humano nada vai destravar, e continuar tentando os
    outros handles só empilharia o mesmo erro. `FalhaNaAPI` num handle
    deixa os outros seguirem, com o handle faltante NAS NOTAS — perder a
    rodada toda por causa de um handle seria trocar uma lacuna anunciada
    por um dia inteiro sem boletim. Se TODOS falharem, sobe erro: rodada
    vazia entregue como sucesso esconderia uma queda total.
    """
    desde = (datetime.now(timezone.utc)
             - timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M:%SZ")

    lidos: list = []
    notas: list[str] = []
    falhas: list[str] = []
    for handle in handles:
        try:
            achados = x_api.posts_de(handle, desde=desde,
                                     limite=LIMITE_POR_HANDLE)
        except PrecisaAutorizar as erro:
            # A fronteira. `boletim.py:370` captura `radar.FalhaNoRadar` e
            # mais nada, e é por ele que o aviso de falha chega ao Telegram
            # — uma exceção de outra classe subiria como traceback, fora do
            # aviso. A mensagem carrega o comando porque quem lê o aviso não
            # está no terminal, e o único desfecho possível é um humano no
            # navegador.
            raise FalhaNoRadar(
                f"a API do X exige autorização ({erro}). Rode à mão, no "
                f"computador do dono: venv/Scripts/python.exe -m src.x_auth "
                f"— nada aqui destrava sozinho.") from erro
        except x_api.FalhaNaAPI as erro:
            falhas.append(f"@{handle}: {erro}")
            continue
        if not achados:
            # Contagem nossa, não pedido: handle que não retornou nada é
            # dito por nome.
            notas.append(f"@{handle} não retornou nada na janela.")
        lidos.extend(achados)

    if falhas and len(falhas) == len(handles):
        raise FalhaNoRadar("a leitura falhou em TODOS os handles — "
                           + " · ".join(falhas))
    for falha in falhas:
        notas.append(f"handle sem leitura nesta rodada — {falha}")

    # `lidos` conta para o CUSTO mesmo o que não vira bloco: a API cobra
    # por recurso devolvido, e retweet descartado já foi pago.
    devolvidos = len(lidos)
    uteis = [p for p in lidos if p.tipo in _TIPO_NO_BLOCO]
    if (retweets := sum(1 for p in lidos if p.tipo == "retweet")):
        notas.append(
            f"{retweets} retweet(s) descartado(s): o texto é de terceiro, "
            f"não é afirmação do handle, e o formato do bloco não tem rótulo "
            f"para eles")
    # Contado à parte, e não somado aos retweets, de propósito: se o
    # vocabulário de `Post.tipo` crescer um dia, o valor novo aparece com o
    # nome dele em vez de ser silenciosamente chamado de retweet. Some com
    # aviso, nunca em silêncio.
    if (estranhos := sorted({p.tipo for p in lidos
                             if p.tipo not in _TIPO_NO_BLOCO
                             and p.tipo != "retweet"})):
        notas.append(f"post(s) com tipo fora do formato do bloco "
                     f"descartado(s): {', '.join(estranhos)}")

    indice = {p.id: p for p in uteis if p.id}
    if (sem_referenciado := sum(
            1 for p in uteis
            if p.tipo in ("thread", "citacao") and p.pai_id
            and p.pai_id not in indice)):
        # Nos dois tipos a linha NÃO SAI: sem o texto do referenciado ela
        # não carrega nada, e na thread o link do pai também fica de fora
        # de propósito (ver `_bloco`: com ele, `filtra_respostas` leria
        # thread própria como resposta a terceiro). Esta nota vai para o
        # Telegram: descrever errado o que aconteceu é informação errada
        # para quem decide se vai atrás do post que falta.
        notas.append(
            f"{sem_referenciado} bloco(s) com o post referenciado FORA da "
            f"janela lida: o bloco sai SEM a linha de contexto — sem o texto "
            f"do referenciado ela não carregaria nada —, e expandir o "
            f"referenciado é outra cobrança na API do X")
    # Citação sem id do citado não sobra nem link, e a linha também não sai.
    # Ramo DEFENSIVO: não há caso medido, e a doc do X não foi conferida
    # quanto a `referenced_*` sem `id` — aqui não se afirma que seja
    # impossível. Contado à parte porque a causa é outra (metadado
    # incompleto, não janela curta), e descarte silencioso é o que esconde
    # defeito.
    if (citacoes_sem_id := sum(1 for p in uteis
                               if p.tipo == "citacao" and not p.pai_id)):
        notas.append(
            f"{citacoes_sem_id} citação(ões) sem o id do post citado no "
            f"metadado: o bloco sai SEM linha CITANDO — não há handle, texto "
            f"nem link para pôr nela")

    posts = tuple(_bloco(i, p, indice) for i, p in enumerate(uteis, 1))
    posts, notas_finais = _barreiras(posts, tuple(notas), handles)

    return Rodada(
        posts=posts,
        notas=notas_finais,
        # `links` e a linha URL: de cada bloco saem do mesmo `Post.url`,
        # montado a partir do id do servidor, então a conferência em
        # `url_do_post` é tautologia pelo caminho normal. O campo existe
        # porque `boletim._chaves_do_post` tira dele a chave forte de dedup
        # entre rodadas.
        links=tuple(dict.fromkeys(p.url for p in lidos if p.url)),
        custo_usd=x_api.custo_estimado_usd(devolvidos),
        bruto=json.dumps([asdict(p) for p in lidos], ensure_ascii=False),
        # A regra do dono é informar o custo ESTIMADO antes e o REAL depois.
        # Por esta via o real não existe: o X não devolve preço nenhum. O
        # que fica é uma multiplicação nossa, e ainda por cima um TETO: a
        # cobrança é deduplicada dentro da janela de 24h UTC, então reler o
        # mesmo post no mesmo dia tende a não cobrar de novo e esta conta
        # cobra. O valor real só sai da fatura, e por isso a palavra
        # "estimado" está no texto que chega ao rodapé do boletim, não só
        # neste comentário.
        detalhe_custo=(
            f"custo ESTIMADO no cliente: {devolvidos} post(s) devolvido(s) × "
            f"US$ {x_api.PRECO_POR_POST_USD:.3f} — teto, não medição; o real "
            f"só na fatura do X"),
    )


def _confere(post: str, custo_busca: float) -> None:
    """Separa as premissas do post e julga cada uma, no rito do premissas.

    O rito importa tanto quanto o resultado: acervo vazio aborta ANTES
    de pagar verificação;
    previsão e opinião saem nomeadas pelo que são, nunca como descarte; o
    trecho literal aparece antes de cada veredito (é o elo auditável entre
    o que o autor escreveu e o que foi conferido); e o fecho impede a
    leitura de placar.
    """
    from . import check, grafo, premissas
    from .storage import conecta

    conexao = conecta(config.BANCO)
    acervo = grafo.carrega(conexao)
    if not acervo:
        print("Acervo vazio. Rode a coleta, a extração e o índice antes "
              "de conferir — verificar contra o nada só gasta.")
        conexao.close()
        sys.exit(1)

    analise, uso = premissas.separa(para_separacao(post), conexao=conexao)
    fatos = [p for p in analise.premissas if p.tipo == "fato"]
    resto = [p for p in analise.premissas if p.tipo != "fato"]

    if resto:
        print("NÃO VERIFICÁVEL — e não deve ser")
        for p in resto:
            print(f"  [{p.tipo}] {p.texto}{premissas.anotacao(p)}")
        print()

    if not fatos:
        print("Nenhuma premissa verificável no post.")
    else:
        for i, p in enumerate(fatos, 1):
            print(f"[{i}/{len(fatos)}] no post: \"{p.trecho[:110]}\"")
            check.verifica(p.texto, conexao=conexao, acervo=acervo)
    conexao.close()

    print("\nIsto confere premissas contra o acervo, não avalia o autor.")
    print("Premissa sem evidência significa que os veículos coletados não")
    print("cobrem o assunto — não que a afirmação seja falsa.")
    print(f"\n  separação: US$ {uso.custo:.4f} · mais uma verificação por "
          f"premissa · busca: US$ {custo_busca:.4f}")


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Radar: o que os handles acompanhados estão alegando.")
    parser.add_argument("--handles",
                        help="lista separada por vírgula; sem isto, usa "
                             "config.HANDLES_RADAR")
    parser.add_argument("--dias", type=int, default=2,
                        help="janela da busca (padrão: 2)")
    parser.add_argument("--conferir", type=int, metavar="N",
                        help="separa as premissas do post N e confere cada "
                             "uma contra o acervo (mais chamadas pagas)")
    parser.add_argument("--dry-run", action="store_true",
                        help="mostra a requisição, sem chamar a API")
    args = parser.parse_args()

    handles = (_handles_de(args.handles) if args.handles
               else config.HANDLES_RADAR)
    if not handles:
        print("Nenhum handle válido. Ver HANDLES_RADAR em config.py.")
        sys.exit(1)

    if args.dry_run:
        # Não há "corpo da requisição" único para imprimir: é um GET por
        # handle, paginado. O que interessa ver antes de gastar é a janela,
        # o teto por handle e o teto de custo.
        desde = (datetime.now(timezone.utc) - timedelta(
            days=args.dias)).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"GET /2/users/:id/tweets · start_time={desde} · "
              f"max_results até {LIMITE_POR_HANDLE} por handle")
        for handle in handles:
            print(f"  @{handle}")
        teto = x_api.custo_estimado_usd(LIMITE_POR_HANDLE * len(handles))
        print(f"\n  teto de gasto: US$ {teto:.4f} "
              f"(estimativa do cliente, não medição)")
        print("\nNada foi enviado. Remova --dry-run para rodar.")
        return

    try:
        rodada = busca(handles, args.dias)
    except FalhaNoRadar as erro:
        print(f"FALHOU: {erro}")
        sys.exit(1)

    print(f"RADAR · {', '.join('@' + h for h in handles)} · "
          f"últimos {args.dias} dias")
    # A linha de procedência diz de onde vêm texto e tipo — e o que ela
    # afirma é verificável no link.
    print("  API oficial do X — texto e tipo vêm do servidor\n")

    if not rodada.posts:
        print("Nenhum post na janela.")
    for i, post in enumerate(rodada.posts, 1):
        print(f"[{i}] {post}\n")
    for nota in rodada.notas:
        print(f"  aviso da busca: {nota}")
    if rodada.links:
        print("Links citados:")
        for link in rodada.links:
            print(f"  {link}")
    print(f"\n  busca: US$ {rodada.custo_usd:.4f}"
          + (f" ({rodada.detalhe_custo})" if rodada.detalhe_custo else ""))

    if args.conferir is not None:
        if not (1 <= args.conferir <= len(rodada.posts)):
            print(f"\nNão existe post {args.conferir} nesta rodada.")
            sys.exit(1)
        print("\n" + "=" * 78)
        print(f"CONFERINDO O POST {args.conferir}")
        print("=" * 78)
        _confere(rodada.posts[args.conferir - 1], rodada.custo_usd)


if __name__ == "__main__":
    main()
