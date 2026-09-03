"""Canonicalização determinística de entidades, aplicada na LEITURA.

O prompt de extração pede nome canônico "idêntico caractere por caractere
entre matérias" (regra 2), e não tem como cumprir: cada chamada é isolada, o
modelo não vê o que gravou nas outras. "Braskem" e "Braskem S.A." são ambas
formas canônicas legítimas — e, medido no acervo em 29/08/2026, existiam como
duas entidades distintas somando 100 triplas, o caso mais denso do grafo. O
sintoma é o que o ARCHITECTURE chama de falha silenciosa e enganosa:
fragmentação de entidade produz o mesmo resultado que ausência de contradição.

A correção é código na leitura, não prompt melhor nem re-extração: o banco
continua registrando o que o modelo afirmou — acervo catalogado, não editado —
e só a COMPARAÇÃO passa pela chave. É o precedente de
`grafo.relacao_normalizada`, aplicado ao outro lado da tripla.

Medido antes de escrever, sobre as 269 formas canônicas do acervo:

    caixa + acento + sufixo societário  →  funde SÓ "Braskem" com
    "Braskem S.A." (100 triplas). Zero fusões indevidas.

    contenção de nome (85 pares tipo "Braskem" ⊂ "Braskem Idesa")  →  NÃO é
    decidível por regra: Braskem Idesa é subsidiária, não apelido. Fusão
    automática aqui fabricaria corroboração — o falso positivo que o
    princípio 5 chama de pior erro.

Por isso duas camadas, e nenhuma é embedding decidindo fusão sozinho:

1. `chave_canonica` — determinística, segura por construção.
2. `APELIDOS` — lista curada à mão e versionada, como o vocabulário de
   relações: o dado propõe (pares contidos ou próximos no índice), o dono do
   projeto promove. Mesmo ciclo do `outro`.

Hierarquia ("Ministério da Saúde" ⊂ "governo federal") continua fora de
escopo, como o ARCHITECTURE assume: não é normalização, é inferência.
"""

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

ARQUIVO_APELIDOS = Path(__file__).resolve().parent.parent / "apelidos.json"
"""Onde moram os apelidos PROMOVIDOS. Versionado no Git de propósito: é
decisão curada, e o diff mostra quando uma identidade nova passou a valer."""

SUFIXOS_SOCIETARIOS = re.compile(
    r"\s+(s\.\s?a\.?|s\s?/\s?a|sa\.|ltda\.?|inc\.?|corp\.?|holding)\s*$",
    re.IGNORECASE,
)
"""Forma societária no fim do nome. Duas exigências, ambas contra fusão
indevida:

* espaço antes — é um token próprio, nunca o fim de palavra ("Casa" fica);
* PONTUAÇÃO na forma curta ("S.A.", "S.A", "S/A", "SA.") — o token nu "sa"
  é proibido porque a normalização remove acentos antes do sufixo, e o
  sobrenome "Sá" vira "sa": aceitá-lo amputaria "Fernando Sá" em "Fernando",
  fundindo pessoas distintas. Achado da revisão de 29/08/2026.

Custo aceito: "Empresa SA" sem pontuação não funde com "Empresa". Perder uma
fusão legítima é o erro barato; fabricar uma é o caro (princípio 5)."""

APELIDOS: dict[str, str] = {
    # Só entra par cujas DUAS formas existem no acervo e nomeiam o MESMO
    # referente — critério conferido na consulta de contenção de 29/08/2026.
    # As chaves e os valores já estão na forma normalizada (saída de
    # `_normaliza`), porque o mapa é consultado depois dela.
    "estados unidos da america": "estados unidos",
    "presidencia da republica do brasil": "presidencia da republica",
}

CABECAS = frozenset(
    # Cargo e a nominalização dele
    "presidente presidencia ministro ministerio senador senadora deputado "
    "deputada governador governadora prefeito prefeita reitor reitoria "
    "prefeitura secretaria relator relatora diretor diretoria juiz juiza "
    "procurador procuradoria advogado advogados advogada defesa "
    "porta-voz representante "
    # Coletivo em torno de alguém
    "governo campanha equipe assessoria gabinete comissao conselho chapa "
    "base aliados entorno familia contas forcas etfs sede "
    # Parentesco e vínculo
    "pai mae filho filha filhos filhas irmao irma esposa marido amigo "
    "amiga socio socia herdeiro sucessor "
    # Obra ou registro SOBRE alguém
    "perfil pagina post publicacao declaracao entrevista discurso "
    "cinebiografia biografia documentario relatorio "
    # Ato, que não é a coisa
    "reuniao encontro sessao telefonema votacao julgamento manutencao "
    "renuncia nomeacao indicacao aprovacao rejeicao".split())
"""Palavra que, sobrando de um lado de uma contenção, muda o REFERENTE em
vez de encurtá-lo: o cargo não é o país, o pai não é o filho, a
cinebiografia não é o biografado, a renúncia não é o cargo.

Mora aqui, e não em quem usa, porque são DOIS que usam e eles divergiram:
o freio do juiz (`check.sujeito_casa`) e a promoção de apelido
(`apelidos.forma_de_apelido`). A revisão de 03/09/2026 executou os dois e
achou o par fundador passando pelo freio — `sujeito_casa("Estados Unidos",
"Presidente dos Estados Unidos")` era True — enquanto o minerador o
recusava, e a docstring afirmava que a guarda era a mesma. Lista
duplicada é lista que diverge.

Nenhuma lista escrita à mão fecha o problema sozinha: ela tinha
"presidente" e não "presidência", "senador" e não "senadora". Quem fecha,
do lado da promoção, é a peneira de DIREÇÃO; esta aqui é a segunda
guarda, para o que vai na direção certa e ainda assim troca o referente
("lula" ⊂ "campanha do presidente lula")."""


def _promovidos() -> dict[str, str]:
    """Apelidos minerados do acervo e promovidos à mão (`src/apelidos.py`).

    Ficam em arquivo, não no código, porque a lista cresce por promoção e o
    Git precisa mostrar quando uma identidade nova passou a valer. Arquivo
    ausente ou ilegível não derruba nada: o mapa fixo acima é o piso."""
    if not ARQUIVO_APELIDOS.exists():
        return {}
    try:
        with open(ARQUIVO_APELIDOS, encoding="utf-8-sig") as arquivo:
            return {str(k): str(v) for k, v in
                    json.load(arquivo).get("apelidos", {}).items()}
    except (json.JSONDecodeError, OSError, AttributeError):
        return {}


APELIDOS.update(_promovidos())

VERSAO_APELIDOS = 1
"""Versão da lista FIXA de apelidos. Cresce como o vocabulário: dado propõe,
humano promove, versão incrementa. Os promovidos em arquivo têm versão
própria — `assinatura_apelidos`, abaixo, que entra no hash do check."""


def assinatura_apelidos() -> str:
    """Identidade do mapa inteiro, fixo mais promovido.

    Entra na versão do prompt do check porque apelido muda o que a rota por
    chave recupera E o que o freio de alinhamento considera o mesmo sujeito:
    vereditos de mapas diferentes não são comparáveis, e o gabarito precisa
    saber sob qual deles cada rodada correu."""
    import hashlib

    material = json.dumps({"versao": VERSAO_APELIDOS,
                           "apelidos": dict(sorted(APELIDOS.items()))},
                          sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


def _normaliza(nome: str) -> str:
    n = unicodedata.normalize("NFD", nome)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    n = n.lower().strip()
    n = SUFIXOS_SOCIETARIOS.sub("", n)
    return re.sub(r"\s+", " ", n)


@lru_cache(maxsize=4096)
def chave_canonica(nome: str) -> str:
    """Chave de COMPARAÇÃO de uma entidade. Não é forma de exibição.

    Quem exibe continua usando o texto que veio do banco; quem compara —
    `grafo.Afirmacao.chave`, a rota por chave exata do check — usa isto.
    Assim a fusão acontece no agrupamento sem reescrever nada do acervo.
    """
    n = _normaliza(nome)
    return APELIDOS.get(n, n)


def chave_medida(propriedade: str | None, recorte: str | None) -> str:
    """A chave que decide se dois números medem a MESMA coisa.

    Nasceu em 03/09/2026 de uma medição: a mesma medida da Caixa saiu em
    SEIS redações diferentes ("alta do lucro recorrente do 2º trimestre
    de 2026 ante o 2º trimestre de 2025", "alta do lucro recorrente sobre
    o mesmo período", "alta do lucro líquido recorrente na base anual"…),
    e o mecanismo que existia — proximidade de embedding a 0,95 sobre a
    prosa — SEPAROU 9 dos 15 pares, com proximidades de 0,79 a 0,90.

    O efeito era falso negativo de corroboração, e silencioso: dois
    veículos publicavam o mesmo número e deixavam de se confirmar. É o
    avesso do princípio 5 e igualmente caro, porque o produto do sistema
    é justamente dizer que duas fontes independentes batem.

    Normaliza aqui, na LEITURA, pelo mesmo motivo de `chave_canonica`: o
    modelo propõe a forma, o código impõe a chave. Acento, caixa,
    pontuação e ordem de underscore não podem separar duas medidas."""
    def _limpa(x: str | None) -> str:
        n = _normaliza(x or "")
        n = re.sub(r"[^a-z0-9]+", "_", n).strip("_")
        return re.sub(r"_+", "_", n)

    return f"{_limpa(propriedade)}|{_limpa(recorte)}"
