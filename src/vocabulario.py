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

A v1 veio de quatro matérias, todas de política eleitoral brasileira — e a
limitação prevista se confirmou: quando a extração alcançou economia
corporativa e cripto/regulação, `outro` chegou a 33% do acervo.

A v2 foi derivada como o desenho manda: inspeção das 121 triplas em `outro`
do lote vocab-1/Opus, em 29/08/2026. Os padrões dominantes, por frequência:
participação societária (Petrobras/Novonor/IG4 × Braskem, ~12 ocorrências),
pedido formal (recuperação extrajudicial — o fato mais corroborável do acervo
estava caindo em `outro`), listagem em bolsa, imposição de tarifa, envio para
análise de outra instância, e os verbos do noticiário de produto em cripto
(lançar, recomendar). Cada um virou relação; o que apareceu uma vez só —
"testou", "adiou" — ficou em `outro` esperando frequência.

DECISÃO JUDICIAL FICOU DE FORA, apesar de frequente (~5), e o motivo vai
registrado para a v3 não repetir o erro: um rótulo neutro de desfecho
("decidiu sobre o caso") faz "homologou" e "negou" caírem na MESMA chave —
duas decisões opostas virariam fato confirmado por dois veículos, corroboração
fabricada sem número para a divergência pegar. É o falso positivo do
princípio 5, dentro do enum. Desfecho judicial precisa ou de relações por
polaridade ou da reificação da atribuição; até lá, fica em `outro`.

O alvo original de convergência (10-15 relações) era uma previsão feita com a
amostra de um domínio só; com três domínios a lista foi a 23 e o alvo revisto
é ~20-25. O mecanismo continua o mesmo: `outro` alto num domínio novo é sinal
de relação faltando, e a correção é ampliar a amostra antes da lista.
"""

from enum import StrEnum

VERSAO = 2
"""Versão do vocabulário. Zero era a fase de relação livre; 1, a lista de
política eleitoral; 2 somou economia corporativa e cripto/regulação."""


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
    SUBMETEU_A = "submeteu_a"
    PREVE = "preve"
    ABRIU_PROCESSO_CONTRA = "abriu_processo_contra"
    SOLICITOU = "solicitou"
    IMPOS = "impos"
    RECOMENDOU = "recomendou"

    # Corporativo e mercado (v2)
    TEM_PARTICIPACAO_EM = "tem_participacao_em"
    NEGOCIADA_EM = "negociada_em"
    LANCOU = "lancou"

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
    Relacao.SUBMETEU_A: (
        "entregou formalmente a outra instância para ANÁLISE ou revisão — "
        "revisão de regra à Casa Branca. Se foi posto em votação, é "
        "submeteu_a_votacao; se pede algo para si, é solicitou"
    ),
    Relacao.PREVE: "projeto ou proposta prevê algo; nunca fato consumado",
    Relacao.ABRIU_PROCESSO_CONTRA: "iniciou processo, investigação ou ação contra",
    Relacao.SOLICITOU: (
        "pediu formalmente algo PARA SI a quem pode conceder — recuperação "
        "judicial, registro, isenção, moção"
    ),
    Relacao.IMPOS: "impôs tarifa, sanção, multa ou medida a alguém",
    Relacao.RECOMENDOU: (
        "emitiu recomendação formal ou alerta técnico — atualização de "
        "software, conduta, política. Apoio declarado em fala é defendeu; "
        "isto é ATO de órgão ou equipe técnica"
    ),
    Relacao.TEM_PARTICIPACAO_EM: (
        "é acionista, controladora ou dona de parte de empresa ou fundo. O "
        "DONO é sempre o sujeito: 'X, subsidiária de Y' vira (Y, "
        "tem_participacao_em, X). O percentual vai nos campos de valor"
    ),
    Relacao.NEGOCIADA_EM: "papel ou ativo listado ou negociado em bolsa ou índice",
    Relacao.LANCOU: "lançou produto, serviço, rede, ativo ou programa",
    Relacao.PARTICIPOU_DE: "esteve em entrevista, sabatina, sessão ou evento",
    Relacao.DIVULGOU: "publicou ou tornou público um dado, estudo ou documento",
    Relacao.TEM_ATRIBUTO: (
        "QUALQUER propriedade com valor numérico: lucro, receita, dívida, "
        "prazo, percentual, custo, margem de erro, amostra. Objeto null, e o "
        "nome da propriedade vai em valor_contexto. Toda tripla com número e "
        "sem objeto usa esta relação — nunca `outro`"
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
