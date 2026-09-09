"""Coletor de feeds RSS.

O download é feito com `requests` e só depois entregue ao `feedparser`. Poderia
ser feito pelo próprio feedparser, mas assim a verificação TLS usa o pacote de
certificados do `certifi`, que se mantém atualizado — o bundle embutido no
Python da máquina estava vencido e derrubava dois dos cinco feeds.
"""

import calendar
import re
from datetime import datetime, timezone

import feedparser
import requests

from ..config import TIMEOUT_SEGUNDOS, USER_AGENT, Feed
from ..models import Artigo
from ..normalize import hash_conteudo, limpa_html, normaliza_url


class FalhaNoFeed(Exception):
    """Feed não pôde ser baixado ou lido."""


_MESES_PT = {"jan": "Jan", "fev": "Feb", "mar": "Mar", "abr": "Apr",
             "mai": "May", "jun": "Jun", "jul": "Jul", "ago": "Aug",
             "set": "Sep", "out": "Oct", "nov": "Nov", "dez": "Dec"}
_RE_DIA_PT = re.compile(r"^\s*[A-Za-zÀ-ÿ]{3},\s*")
_RE_MES_PT = re.compile(r"\b([A-Za-zÀ-ÿ]{3})\b")


def _data_em_portugues(texto: str) -> str | None:
    """RFC 822 com nomes de dia e mês em PORTUGUÊS, em ISO — ou None.

    O feed do UOL entrega "Ter, 08 Set 2026 23:42:58 -0300". O feedparser
    só conhece os nomes em inglês, devolve `published_parsed` vazio e a
    matéria era gravada SEM data. Custo medido em 09/09/2026: 3.219
    matérias, 100% do UOL e 16% do acervo, invisíveis para tudo que
    trabalha com janela — o índice de matérias, a demanda e o
    agrupamento. Achado quando uma premissa sobre o drone de Leipzig saiu
    "o acervo não cobre" com a matéria do UOL no banco.

    O dia da semana é descartado (a data já o determina) e só o mês é
    traduzido, o que evita casar palavra de três letras dentro do resto
    da string.
    """
    from email.utils import parsedate_to_datetime

    limpo = _RE_DIA_PT.sub("", str(texto or "").strip())
    if not limpo:
        return None

    def traduz(m):
        return _MESES_PT.get(m.group(1).casefold(), m.group(1))

    try:
        data = parsedate_to_datetime(_RE_MES_PT.sub(traduz, limpo, count=1))
    except (TypeError, ValueError):
        return None
    if data.tzinfo is None:
        data = data.replace(tzinfo=timezone.utc)
    return data.astimezone(timezone.utc).isoformat()


def _para_iso(struct_time) -> str | None:
    """Converte a data do feedparser (UTC) para ISO 8601, ou None."""
    if not struct_time:
        return None
    instante = datetime.fromtimestamp(calendar.timegm(struct_time), tz=timezone.utc)
    return instante.isoformat()


def _corpo(entrada) -> str:
    """Extrai o corpo da matéria, quando o feed o oferece.

    Nem todo veículo publica o texto completo no RSS: alguns entregam só
    manchete e linha fina. Quando houver mais de um bloco de conteúdo, o mais
    longo é o que interessa.
    """
    blocos = entrada.get("content") or []
    textos = [limpa_html(bloco.get("value")) for bloco in blocos]
    return max(textos, key=len) if textos else ""


def _para_artigo(feed: Feed, entrada) -> Artigo | None:
    """Converte uma entrada do feed em Artigo, ou None se for inaproveitável."""
    url = (entrada.get("link") or "").strip()
    titulo = limpa_html(entrada.get("title"))

    # Sem URL não há como deduplicar nem citar a fonte, e o princípio de que
    # todo veredito carrega fonte torna o item inútil. Sem título, não há
    # afirmação a extrair.
    if not url or not titulo:
        return None

    resumo = limpa_html(entrada.get("summary"))
    conteudo = _corpo(entrada)

    return Artigo(
        veiculo=feed.veiculo,
        editoria=feed.editoria,
        titulo=titulo,
        url_original=url,
        url_norm=normaliza_url(url),
        resumo=resumo,
        conteudo=conteudo,
        # `_data_em_portugues` é a reserva: feed em português entrega
        # "Ter, 08 Set 2026" e o feedparser não o parseia.
        data_publicacao=(
            _para_iso(entrada.get("published_parsed")
                      or entrada.get("updated_parsed"))
            or _data_em_portugues(entrada.get("published")
                                  or entrada.get("updated") or "")
        ),
        hash_conteudo=hash_conteudo(titulo, resumo, conteudo),
    )


def busca(feed: Feed) -> list[Artigo]:
    """Baixa e interpreta um feed, devolvendo os artigos aproveitáveis.

    Levanta FalhaNoFeed se o download falhar. Entradas individuais defeituosas
    são descartadas em silêncio: uma matéria sem link não deve derrubar a
    coleta das outras cinquenta.
    """
    try:
        resposta = requests.get(
            feed.url,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SEGUNDOS,
        )
        resposta.raise_for_status()
    except requests.RequestException as erro:
        raise FalhaNoFeed(f"{type(erro).__name__}: {erro}") from erro

    analisado = feedparser.parse(resposta.content)

    artigos = (_para_artigo(feed, entrada) for entrada in analisado.entries)
    return [artigo for artigo in artigos if artigo is not None]
