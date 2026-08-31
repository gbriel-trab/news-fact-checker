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
    Feed("Estadão", "Política", "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/politica/?outputType=xml"),
    Feed("Estadão", "Economia", "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/economia/?outputType=xml"),
    Feed("Valor", "Brasil", "https://pox.globo.com/rss/valor/brasil/"),
    Feed("Valor", "Política", "https://pox.globo.com/rss/valor/politica/"),
    Feed("Metrópoles", "Política", "https://www.metropoles.com/feed"),
    # --- Só manchete e linha fina (~150 a 300 caracteres): sinal de cobertura ---
    Feed("Exame", "Economia", "https://exame.com/feed/"),
    Feed("Carta Capital", "Geral", "https://www.cartacapital.com.br/feed/"),
    # Cripto. O endereco antigo (br.cointelegraph.com) responde 410 Gone -- a
    # edicao brasileira mudou de dominio. Vale registrar porque 410 nao e
    # falha temporaria: e o servidor dizendo que o recurso saiu de proposito,
    # e insistir nele nunca voltaria a funcionar.
    # Cripto precisa de mais de um veiculo pelo mesmo motivo que economia:
    # confirmacao exige duas redacoes independentes. Com o Cointelegraph
    # sozinho, TODA afirmacao sobre cripto ficava presa em "um veiculo so".
    # Medido antes de adicionar: 22 materias de cripto no acervo, 22 dele.
    #
    # Os tres abaixo entregam texto cheio (3.200 a 4.900 caracteres, 21 a 36
    # sentencas) e sustentam extracao -- ao contrario do Cointelegraph, que
    # publica uma sentenca por item e fica so como sinal de cobertura.
    Feed("Cointelegraph", "Cripto", "https://cointelegraph.com.br/rss"),
    Feed("Portal do Bitcoin", "Cripto", "https://portaldobitcoin.uol.com.br/feed/"),
    Feed("Livecoins", "Cripto", "https://livecoins.com.br/feed/"),
    Feed("CriptoFácil", "Cripto", "https://www.criptofacil.com/feed/"),
    Feed("Folha", "Poder", "https://feeds.folha.uol.com.br/poder/rss091.xml"),
    Feed("Folha", "Mercado", "https://feeds.folha.uol.com.br/mercado/rss091.xml"),
    Feed("Folha", "Mundo", "https://feeds.folha.uol.com.br/mundo/rss091.xml"),
    Feed("BBC Brasil", "Geral", "https://feeds.bbci.co.uk/portuguese/rss.xml"),
    Feed("UOL", "Notícias", "https://rss.uol.com.br/feed/noticias.xml"),
    # --- Fontes primárias: a instituição publicando sobre o próprio ato ---
    #
    # Não são imprensa e não entram na contagem de corroboração como se
    # fossem: quando o Banco do Japão afirma algo sobre o Banco do Japão, isso
    # não é uma segunda fonte concordando com o G1 — é a fonte do fato.
    #
    # Existem para dois usos que a imprensa brasileira não cobre:
    #
    #   1. assunto estrangeiro que só aparece aqui de segunda mão
    #   2. AUSÊNCIA de registro, que é evidência: se circula que o BoJ mexeu
    #      nos juros e o feed oficial não registra comunicado na data, isso
    #      diz algo — não prova, mas pesa
    #
    # LIMITAÇÃO MEDIDA (27/08/2026): os três publicam só título, sem resumo
    # nem corpo (BoJ 0c, BCE 0c, Fed ~80c). Não sustentam extração de triplas
    # pelo caminho atual, que exige texto. Entram como sinal de cobertura até
    # existir extração a partir do título. E publicam em inglês, contra um
    # acervo em português — o casamento depende do modelo de embedding ser
    # multilíngue, o que ele é, mas isso não foi medido ainda.
    Feed("Banco do Japão", "Comunicados", "https://www.boj.or.jp/en/rss/whatsnew.xml"),
    Feed("Federal Reserve", "Comunicados", "https://www.federalreserve.gov/feeds/press_all.xml"),
    Feed("BCE", "Comunicados", "https://www.ecb.europa.eu/rss/press.html"),
)

# O Banco Central do Brasil, o TSE, o STF, o Senado e a Câmara foram testados
# na mesma data e ficaram de fora: TSE e STF respondem 403 a requisição
# programática, Senado e Câmara não devolvem RSS válido, e o feed do BCB
# declara `encoding="pt-br"`, que não é codificação existente e derruba o
# parser. Nenhum é impossível — todos exigem trabalho que não é ler RSS.

# ADIÇÃO DE VEÍCULO É O QUE MOVE CORROBORAÇÃO. Com 8 veículos, uma história
# precisa que 2 dos 8 a cubram; com 13, a chance de qualquer história ter par
# sobe muito — e a taxa de confirmação do acervo era 5%, com o gargalo do lado
# da cobertura, não da extração.
#
# Estadão, Valor e Metrópoles entram por cobrirem as MESMAS histórias que os
# demais. É isso que corrobora: veículo que cobre outro assunto amplia o
# acervo e não confirma nada.
#
# Testados na mesma rodada e deixados de fora, por motivo:
#
#   O Globo       RSS responde 200 e vem vazio
#   Reuters BR    404
#   Nexo          análise e explicador, não notícia direta — outro gênero
#   Intercept     investigação, exclusiva por natureza. É o caso do "furo":
#                 publicaria sozinho e nunca teria par, então entraria no
#                 acervo permanentemente como não confirmado. Não é defeito
#                 dele nem nosso — é o que este sistema não consegue verificar

# O feed geral do G1 (g1.globo.com/rss/g1/) foi descartado deliberadamente.
# Ele é dominado por conteúdo das afiliadas regionais — acidente de trânsito
# municipal, evento local, grade de programação da TV. Esse material não é
# ruído por ser irrelevante para o leitor: é estruturalmente inverificável,
# porque só um veículo cobre, e afirmação de fonte única nunca pode ser
# corroborada por fonte independente.

# ---- Radar de rede social (ver src/radar.py e a seção da xAI no
# ARCHITECTURE.md) ----
#
# O critério de seleção é o INVERSO do dos feeds: aqui entram os perfis que
# PRODUZEM alegação, nunca os veículos em que se confia — @g1 na lista
# devolveria o acervo conversando consigo mesmo. Post não entra no acervo.
#
# Duas condições por handle, ambas medidas em 30/08/2026:
#   1. conta PÚBLICA — post protegido é invisível a qualquer busca, por
#      desenho do X (@OutsiderPapini, que motivou o radar, é privado e fica
#      no fluxo manual: copiar o post e colar no premissas)
#   2. TESTADO no x_search antes de entrar (~US$ 0,03 a chamada) — o índice
#      não cobre tudo, e handle cego aqui falharia em silêncio
HANDLES_RADAR: tuple[str, ...] = (
    "mentalhedgebr",   # testado 30/08/2026: visível, transcrição íntegra
)

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
