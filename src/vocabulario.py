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

A v3 veio da mineração de 01/09/2026: 462 triplas em `outro` (21% do recorte
v2), lidas por dois mineradores independentes com lentes distintas (verbo e
domínio) e arbitradas contra as regras da v2. Dezessete relações entraram —
judiciário/normativo (concedeu, rejeitou, suspendeu, adiou, editou, indicou,
relata, renunciou_a, tramita_em), força do Estado (prendeu, detido_em),
nexo e lugar de evento (causou, ocorreu_em), dinheiro e posse (pagou_a,
adquiriu), família e obra (tem_parentesco_com, retrata). Arbitragens que
importam registrar:

* `rejeitou` entrou ABAIXO do piso de frequência, de propósito: com
  `concedeu` no enum e o polo negativo fora, o modelo seria tentado a
  forçar indeferimento na chave da concessão — a corrupção de polaridade
  DENTRO de uma relação, pior que a fusão que derrubou `decidiu_sobre`.
* `realizou`/`organizou` foi REJEITADO (verbo vazio que aceita qualquer
  objeto e legitima o evento sintético — a fragmentação já diagnosticada),
  e `fica_em`/`localiza_se_em` também (estado que ninguém disputa não gera
  corroboração nem contradição útil).
* Rejeições registradas esperando frequência ou decisão de schema:
  esporte inteiro, atingiu_marca (é tem_atributo mal extraído),
  entra_em_vigor (é carimbo de data, não relação), condenou/absolveu
  (par polar futuro), prorrogou (é impos com prazo no valor), morreu_em,
  negociou_com/firmou_acordo, investiga (vizinho de abriu_processo_contra),
  fato negativo (pede campo de negação, não relação).

O alvo de ~20-25 foi revisto de novo, e o motivo vai dito: ele foi escrito
com três domínios no acervo. A v3 cobre judiciário, força do Estado e
família/obra — seis domínios pedem mais chaves, e 39 relações bem
fronteiriças custam menos que 462 fatos amontoados em `outro`. A régua que
fica: enum só cresce por mineração medida, nunca por projeção.
"""

from enum import StrEnum

VERSAO = 3
"""Versão do vocabulário. Zero era a fase de relação livre; 1, a lista de
política eleitoral; 2 somou economia corporativa e cripto/regulação; 3 somou
judiciário/normativo, força do Estado, nexo causal e família/obra."""

COMPATIVEIS: frozenset[int] = frozenset({2, 3})
"""Versões cujo dado convive no MESMO grafo.

A v3 é ADITIVA: nenhuma relação da v2 mudou de nome, direção ou definição,
então tripla v2 continua válida sob v3 — grafo e índice leem as duas. Sem
isto, a primeira extração v3 escureceria o acervo inteiro (o recorte antigo
era MAX(vocab_versao)) e o digest colapsaria para meia dúzia de matérias
até uma re-extração geral que ninguém orçou. Quebra de compatibilidade real
(renomear, redefinir, inverter direção) deve REMOVER a versão antiga deste
conjunto — e aí sim pagar a re-extração."""


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

    # Judiciário e normativo (v3)
    CONCEDEU = "concedeu"
    REJEITOU = "rejeitou"
    SUSPENDEU = "suspendeu"
    ADIOU = "adiou"
    EDITOU = "editou"
    INDICOU = "indicou"
    RELATA = "relata"
    RENUNCIOU_A = "renunciou_a"
    TRAMITA_EM = "tramita_em"

    # Força do Estado (v3)
    PRENDEU = "prendeu"
    DETIDO_EM = "detido_em"

    # Nexo e lugar de evento (v3)
    CAUSOU = "causou"
    OCORREU_EM = "ocorreu_em"

    # Dinheiro e posse (v3)
    PAGOU_A = "pagou_a"
    ADQUIRIU = "adquiriu"

    # Família e obra (v3)
    TEM_PARENTESCO_COM = "tem_parentesco_com"
    RETRATA = "retrata"

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
    Relacao.CONCEDEU: (
        "autoridade deferiu pedido ou concedeu medida — liminar, recuperação "
        "judicial, registro, habeas corpus. Quem CONCEDE é o sujeito; quem "
        "pede é solicitou. Indeferir é rejeitou, NUNCA aqui"
    ),
    Relacao.REJEITOU: (
        "autoridade indeferiu ou negou formalmente pedido, emenda ou "
        "proposta. Ato formal — desaprovação em fala é criticou"
    ),
    Relacao.SUSPENDEU: (
        "fez cessar a vigência do que já valia — suspendeu, revogou, "
        "derrubou, cassou. Quem suspende é o sujeito; a coisa cessada é o "
        "objeto. Adiar evento futuro é adiou; criar medida é impos"
    ),
    Relacao.ADIOU: (
        "empurrou para data futura evento já marcado — votação, depoimento, "
        "julgamento, prazo. Mantém a validade; só desloca no tempo"
    ),
    Relacao.EDITOU: (
        "editou, assinou ou baixou ato normativo — medida provisória, "
        "decreto, ordem executiva, portaria. O ATO é o objeto; o efeito "
        "dele sobre um alvo é impos; o conteúdo projetado é preve"
    ),
    Relacao.INDICOU: (
        "indicou ou nomeou PESSOA para cargo, vaga ou função — a pessoa é "
        "o objeto, o cargo vai em valor_contexto. A própria pessoa "
        "disputando é candidatou_se_a"
    ),
    Relacao.RELATA: (
        "é o relator designado de proposição ou processo em órgão "
        "colegiado. NUNCA para fala relatada — declaração é afirmou"
    ),
    Relacao.RENUNCIOU_A: (
        "abriu mão formalmente de cargo, mandato ou função — encerra o "
        "estado de exerce_cargo_em/preside"
    ),
    Relacao.TRAMITA_EM: (
        "proposição, processo ou pedido está em análise num órgão — a "
        "MATÉRIA é o sujeito, mesmo quando o texto diz 'órgão analisa X'. "
        "O ato de entregar é submeteu_a; este é o ESTADO de onde está"
    ),
    Relacao.PRENDEU: (
        "prendeu, deteve ou capturou pessoa. A força ou agente estatal é o "
        "sujeito; a pessoa presa é o objeto — mesmo quando o texto inverte"
    ),
    Relacao.DETIDO_EM: (
        "está preso ou sob custódia em prisão ou instalação (estado). A "
        "pessoa é o sujeito; prendeu é o evento que inicia este estado"
    ),
    Relacao.CAUSOU: (
        "evento ou condição provocou outro evento CONSUMADO. A CAUSA é "
        "sempre o sujeito, mesmo quando o texto diz 'Y foi provocado por "
        "X'. Consequência projetada é preve"
    ),
    Relacao.OCORREU_EM: (
        "evento aconteceu em local ou território — o EVENTO é o sujeito, "
        "o lugar é o objeto; a data vai no campo de data. Pessoa em evento "
        "é participou_de"
    ),
    Relacao.PAGOU_A: (
        "transferiu dinheiro ao objeto — pagamento, repasse, aporte, "
        "doação, empréstimo, ajuda; o montante vai nos campos de valor. "
        "Com ativo recebido em troca é adquiriu; posse societária é "
        "tem_participacao_em"
    ),
    Relacao.ADQUIRIU: (
        "comprou ou assumiu controle de ativo, empresa ou propriedade; o "
        "preço vai nos campos de valor. É o EVENTO da compra; o estado de "
        "posse resultante é tem_participacao_em"
    ),
    Relacao.TEM_PARENTESCO_COM: (
        "vínculo familiar ou conjugal; o TIPO ('pai', 'casada com', "
        "'filho') é obrigatório em valor_contexto. No vínculo vertical o "
        "ascendente é o sujeito"
    ),
    Relacao.RETRATA: (
        "obra — filme, livro, série, documentário — retrata ou tem como "
        "tema pessoa ou evento. A OBRA é o sujeito; lançá-la é lancou"
    ),
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
