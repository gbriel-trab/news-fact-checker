"""Vocabulário controlado de relações.

Lista fechada, imposta como `Literal` no schema. A API não consegue gerar valor
fora dela — é restrição de geração, não pedido no prompt.

Por que fechar: com relação em texto livre, o mesmo fato recebe nomes
diferentes e as triplas não se encontram no grafo. Medido nas 98 primeiras
triplas desta base, `foi_filiado_a`, `filiou_se_a` e `integrou` apareceram como
três relações distintas para a mesma afirmação, somando 20 triplas que não
corroboravam nenhuma. Pior: extrair a MESMA matéria duas vezes produzia nomes
diferentes — `foi_candidato_a` numa rodada, `concorreu_a` na outra.

A lista foi derivada do dado, não projetada no papel: são os 47 valores que a
extração livre produziu, agrupados pelo que significam.

Como evoluir: rodar, medir a fração que caiu em `outro`, e promover o que for
frequente. Cada promoção incrementa VERSAO — sem isso é impossível distinguir
"não cabia em nenhuma" de "essa relação ainda não existia".

LIMITAÇÃO CONHECIDA DA v1: a amostra que gerou esta lista foram quatro matérias,
todas de política eleitoral brasileira. Economia, mercado e cobertura
internacional não estão representados. Uma matéria sobre decisão de banco
central — "elevou juros para 0,75%", "sinalizou corte" — cairia quase toda em
`outro`.

Isso é o mecanismo funcionando, não falha: `outro` alto num domínio é
exatamente o sinal de que faltam relações para ele. A correção é ampliar a
AMOSTRA antes de ampliar a lista. Inventar relações de macroeconomia sem ter
lido uma matéria de macroeconomia seria projetar no papel, que é o que este
desenho recusa.

Antes de fechar a v2: extrair matérias espalhadas por editoria — economia,
mundo, ciência — e deixar o `outro` dizer o que falta.
"""

from enum import StrEnum

VERSAO = 1
"""Versão do vocabulário. Zero era a fase de relação livre."""


class Relacao(StrEnum):
    # Declaração — o objeto é o CONTEÚDO da fala, nunca o assunto
    AFIRMOU = "afirmou"
    CRITICOU = "criticou"
    DEFENDEU = "defendeu"

    # Vínculo e cargo
    INTEGRA = "integra"
    EXERCE_CARGO_EM = "exerce_cargo_em"
    PRESIDE = "preside"

    # Disputa eleitoral
    CANDIDATOU_SE_A = "candidatou_se_a"
    OBTEVE_PERCENTUAL_EM = "obteve_percentual_em"

    # Ação institucional
    SUBMETEU_A_VOTACAO = "submeteu_a_votacao"
    PREVE = "preve"
    ABRIU_PROCESSO_CONTRA = "abriu_processo_contra"

    # Evento e publicação
    PARTICIPOU_DE = "participou_de"
    DIVULGOU = "divulgou"

    # Atributo — objeto é null, o número vai no valor
    TEM_ATRIBUTO = "tem_atributo"

    # Válvula de escape
    OUTRO = "outro"


DEFINICOES: dict[Relacao, str] = {
    Relacao.AFIRMOU: "declarou algo, sem valoração explícita",
    Relacao.CRITICOU: "declarou com valoração negativa, ou atacou alguém ou algo",
    Relacao.DEFENDEU: "declarou apoio a algo, ou propôs que algo seja feito",
    Relacao.INTEGRA: "faz parte de partido, chapa, comissão ou organização",
    Relacao.EXERCE_CARGO_EM: "ocupa cargo numa instituição, sem dirigi-la",
    Relacao.PRESIDE: "dirige uma instituição, comissão ou órgão",
    Relacao.CANDIDATOU_SE_A: "é candidato a um cargo",
    Relacao.OBTEVE_PERCENTUAL_EM: "resultado em pesquisa ou votação; número no valor",
    Relacao.SUBMETEU_A_VOTACAO: "pôs proposta em votação, ou ela foi votada num órgão",
    Relacao.PREVE: "projeto ou proposta prevê algo; nunca fato consumado",
    Relacao.ABRIU_PROCESSO_CONTRA: "iniciou processo, investigação ou ação contra",
    Relacao.PARTICIPOU_DE: "esteve em entrevista, sabatina, sessão ou evento",
    Relacao.DIVULGOU: "publicou ou tornou público um dado, estudo ou documento",
    Relacao.TEM_ATRIBUTO: (
        "propriedade com valor numérico — custo, margem de erro, amostra. "
        "Objeto null, e o nome da propriedade vai em valor_contexto"
    ),
    Relacao.OUTRO: (
        "afirmação verificável que não cabe em nenhuma acima. "
        "Use sem hesitar: forçar uma relação que não serve é pior"
    ),
}

assert set(DEFINICOES) == set(Relacao), "toda relação precisa de definição"


def resumo_para_prompt() -> str:
    """Lista formatada para as instruções, com a definição de cada relação."""
    return "\n".join(
        f"  {r.value:<22} {DEFINICOES[r]}" for r in Relacao
    )
