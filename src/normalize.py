"""Normalização de URL e hash de conteúdo.

Estas duas funções são a base da deduplicação. Um erro aqui não levanta
exceção: ou o acervo incha com cópias da mesma matéria, ou — pior — duas
matérias distintas colapsam numa só e uma delas some. Por isso são as
primeiras coisas cobertas por teste.
"""

import hashlib
import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import PARAMS_RASTREIO

_TAG_HTML = re.compile(r"<[^>]+>")
_ESPACOS = re.compile(r"\s+")


def normaliza_url(url: str) -> str:
    """Devolve a forma canônica da URL, usada como chave de deduplicação.

    Remove o fragmento, rebaixa esquema e host para minúsculas, descarta os
    parâmetros de rastreamento conhecidos e tira a barra final. Os demais
    parâmetros de query são preservados e reordenados, porque muitos
    veículos identificam a matéria por eles.
    """
    partes = urlsplit(url.strip())

    esquema = partes.scheme.lower()
    host = partes.netloc.lower()

    # Porta padrão explícita é ruído: http://x:80/ e http://x/ são a mesma página.
    if (esquema == "http" and host.endswith(":80")) or (
        esquema == "https" and host.endswith(":443")
    ):
        host = host.rsplit(":", 1)[0]

    query = urlencode(
        sorted(
            (chave, valor)
            for chave, valor in parse_qsl(partes.query, keep_blank_values=True)
            if chave.lower() not in PARAMS_RASTREIO
        )
    )

    caminho = partes.path.rstrip("/") or "/"

    return urlunsplit((esquema, host, caminho, query, ""))


def limpa_html(texto: str | None) -> str:
    """Tira marcação e normaliza espaços do texto vindo do feed.

    Feeds entregam HTML dentro de campos de texto com frequência, e resíduo
    de marcação atrapalha tanto a extração de afirmações quanto o hash.
    """
    if not texto:
        return ""
    return _ESPACOS.sub(" ", html.unescape(_TAG_HTML.sub(" ", texto))).strip()


def hash_conteudo(titulo: str, resumo: str, conteudo: str) -> str:
    """SHA-256 do texto da matéria, para detectar edição na mesma URL.

    O separador nulo evita colisão entre campos: sem ele, ("ab", "c") e
    ("a", "bc") produziriam o mesmo hash.
    """
    bruto = "\x00".join((titulo, resumo, conteudo))
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()

PADROES_LIVE = ("aovivo", "ao-vivo", "ao_vivo", "tempo-real", "tempo_real",
                "liveblog", "live-blog", "minuto-a-minuto", "acompanhe-ao-vivo",
                "-ao-vivo-", "/live/")
"""Marcas de URL de página ROLANTE: liveblog, cobertura minuto a minuto.

Medido em 03/09/2026, o problema é triplo e nenhum é cosmético:

* o link NÃO prova o fato — o leitor tem de rolar a página até achar a
  entrada. O princípio 2 pede fonte rastreável, e liveblog é rastreável
  só de nome. Achado pelo usuário conferindo uma fonte da Folha citada
  num CONFIRMADO: a URL abria uma cobertura de mercado ao vivo;
* a mesma URL é recoletada sem parar e vira N "matérias" — o Ibovespa ao
  vivo do InfoMoney está 31 vezes no acervo, a Folha do caso 19;
* a `data_publicacao` é a de ABERTURA da cobertura, não a do fato, e
  alimenta a regra de data do juiz com data errada.

Fica em `normalize` porque quem precisa saber são três: o índice (para
não contar N vezes), o boletim (para avisar o leitor) e, se um dia for
decidido, a contagem de veículos independentes."""


def e_live(url: str) -> bool:
    """A URL é de página rolante? Compara em minúsculas, sem exigir
    domínio: o padrão está no CAMINHO, e cada veículo escreve o seu."""
    u = (url or "").lower()
    return any(p in u for p in PADROES_LIVE)
