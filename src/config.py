"""Configuração da coleta: feeds, caminhos e parâmetros de rede."""

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
DIR_DADOS = RAIZ / "data"
BANCO = DIR_DADOS / "noticias.db"

# Carrega o .env para o ambiente do processo. Sem isto, `os.getenv` e o SDK da
# Anthropic não enxergam nada do arquivo — a chave ficaria lá parada e a falha
# seria "credencial ausente", sem indicar o motivo real.
#
# Fica aqui porque config é importado por todos os pontos de entrada, e
# variável de ambiente já definida de fora tem precedência sobre o arquivo.
load_dotenv(RAIZ / ".env")


@dataclass(frozen=True, slots=True)
class Feed:
    """Um feed RSS acompanhado.

    `veiculo` e `editoria` são campos separados de propósito. Duas editorias do
    mesmo veículo NÃO são fontes independentes: se a mesma matéria aparece em
    "G1 Política" e "G1 Economia", isso é uma redação publicando uma vez, não
    dois jornais concordando. Tratá-las como fontes distintas inflaria toda
    contagem de corroboração e produziria "confirmado" falso — exatamente o
    erro que o projeto considera o pior.

    A unidade de corroboração é o `veiculo`. A `editoria` só organiza.
    """

    veiculo: str
    editoria: str
    url: str


# Feeds acompanhados, medidos antes de entrar aqui.
#
# A separação abaixo importa para a etapa de extração: feed que só publica
# manchete não sustenta extração de triplas, mas continua valendo como sinal
# de que o veículo cobriu o assunto — que é o que a corroboração precisa.
FEEDS: tuple[Feed, ...] = (
    # --- Texto completo (~2.000 a 10.000 caracteres): servem para extração ---
    Feed("G1", "Política", "https://g1.globo.com/rss/g1/politica/"),
    Feed("G1", "Economia", "https://g1.globo.com/rss/g1/economia/"),
    Feed("G1", "Mundo", "https://g1.globo.com/rss/g1/mundo/"),
    Feed("G1", "Ciência e Saúde", "https://g1.globo.com/rss/g1/ciencia-e-saude/"),
    Feed("CNN Brasil", "Geral", "https://www.cnnbrasil.com.br/feed/"),
    Feed("Agência Brasil", "Política", "https://agenciabrasil.ebc.com.br/rss/politica/feed.xml"),
    Feed("Agência Brasil", "Economia", "https://agenciabrasil.ebc.com.br/rss/economia/feed.xml"),
    Feed("Poder360", "Geral", "https://www.poder360.com.br/feed/"),
    Feed("InfoMoney", "Mercados", "https://www.infomoney.com.br/feed/"),
    # --- Só manchete e linha fina (~150 a 300 caracteres): sinal de cobertura ---
    Feed("Folha", "Poder", "https://feeds.folha.uol.com.br/poder/rss091.xml"),
    Feed("Folha", "Mercado", "https://feeds.folha.uol.com.br/mercado/rss091.xml"),
    Feed("Folha", "Mundo", "https://feeds.folha.uol.com.br/mundo/rss091.xml"),
    Feed("BBC Brasil", "Geral", "https://feeds.bbci.co.uk/portuguese/rss.xml"),
    Feed("UOL", "Notícias", "https://rss.uol.com.br/feed/noticias.xml"),
)

# O feed geral do G1 (g1.globo.com/rss/g1/) foi descartado deliberadamente.
# Ele é dominado por conteúdo das afiliadas regionais — acidente de trânsito
# municipal, evento local, grade de programação da TV. Esse material não é
# ruído por ser irrelevante para o leitor: é estruturalmente inverificável,
# porque só um veículo cobre, e afirmação de fonte única nunca pode ser
# corroborada por fonte independente.

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
        "cmpid",
        "xtor",
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
    }
)
