"""Configuração da coleta: feeds, caminhos e parâmetros de rede."""

from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DIR_DADOS = RAIZ / "data"
BANCO = DIR_DADOS / "noticias.db"

# Feeds RSS acompanhados. A chave é o nome do veículo, gravado em cada artigo.
# Feed que passar a falhar de forma persistente deve sair daqui, não ser
# silenciado: coleta que falha calada vira buraco invisível no acervo.
FEEDS: dict[str, str] = {
    "G1": "https://g1.globo.com/rss/g1/",
    "Agência Brasil": "https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml",
    "BBC Brasil": "https://feeds.bbci.co.uk/portuguese/rss.xml",
    "CNN Brasil": "https://www.cnnbrasil.com.br/feed/",
    "InfoMoney": "https://www.infomoney.com.br/feed/",
}

TIMEOUT_SEGUNDOS = 15

USER_AGENT = (
    "news-fact-checker/0.1 "
    "(+https://github.com/gbriel-trab/news-fact-checker)"
)

# Parâmetros de query descartados antes de deduplicar. São de rastreamento:
# não mudam o conteúdo da página, mas mudam a URL, e sem removê-los a mesma
# matéria entraria várias vezes no acervo.
#
# A remoção é por lista fechada, e não "descartar toda query string", porque
# muitos veículos identificam a matéria por parâmetro (?id=123). Descartar
# tudo colapsaria matérias diferentes numa só.
PARAMS_RASTREIO: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_referrer",
        "fbclid",
        "gclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ref",
        "ref_src",
        "src",
        # Rastreamento da BBC. Descobertos na primeira coleta real: sem eles,
        # a mesma matéria reaparecia como registro distinto.
        "at_campaign",
        "at_campaign_type",
        "at_medium",
        "at_format",
        "at_link_id",
        "at_link_origin",
        "at_link_type",
        "at_ptr_name",
        "at_bbc_team",
        "cmpid",
        "xtor",
    }
)
