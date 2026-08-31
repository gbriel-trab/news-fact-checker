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
`grafo._relacao_normalizada`, aplicado ao outro lado da tripla.

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

import re
import unicodedata
from functools import lru_cache

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

VERSAO_APELIDOS = 1
"""Versão da lista de apelidos. Cresce como o vocabulário: dado propõe,
humano promove, versão incrementa."""


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
