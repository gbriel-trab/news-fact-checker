"""A quarta saída: o que o acervo tem sobre o assunto, quando não há
premissa para conferir.

Nasce do post das recuperações judiciais (03/09/2026). O separador acerta
ao não extrair fato — "marcas icônicas" não identifica quais, "quantas"
não é número —, mas o acervo cobria o assunto fartamente e o sistema não
tinha onde dizer isso. Não é `confirmado`, porque não há premissa
bem-formada; não é `sem evidência`, porque o acervo cobre.

CONTEXTO NÃO É VEREDITO, e a distinção é a mesma que separa o digest do
check: aponta o que o acervo tem sobre um assunto e mostra as fontes, sem
afirmar que isso sustenta a insinuação de ninguém.

O ASSUNTO VEM DA `hipotese`, NÃO DO TEXTO DO POST. Isto foi medido em
03/09/2026 e é a decisão que faz a saída funcionar, então fica escrita:

    busca pelo TEXTO do post          busca pela HIPOTESE
    C19  14 matérias, 5 veículos      15 matérias, 7 veículos
    C3  200 matérias, 13 veículos      0 matérias

Post longo casa com o acervo inteiro: buscando pelo texto, o C3 ("o cara
tem: banco dele...") trazia 200 matérias e 13 veículos, e os primeiros
achados eram sobre golpistas com IA e propaganda eleitoral — nada a ver
com o post. Publicar "o acervo registra 200 matérias" ali seria dar como
achado do acervo um número que a busca fabricou. A `hipotese` é o campo
que o separador já preenche em `nao_verificavel` dizendo de que o texto
fala, e ela é curta e específica; o C3, cuja hipótese nomeia uma PESSOA
("um empresário do setor financeiro não identificado"), devolve zero — e
tem de devolver, porque contexto por assunto não nomeia pessoa. Usar a
mesma máquina para responder "de quem ele está falando" seria o princípio
1 pela porta dos fundos.

Os três limiares foram medidos no acervo de 03/09/2026 (8.644 matérias
indexadas), com controle negativo, não escolhidos por gosto:

    LIMIAR 0.75   a 0.70 uma consulta sobre trigo no Cazaquistão trazia 34
                  matérias; a 0.75, a frase vaga "todos os rumos mudam
                  imediatamente" traz ZERO. É o piso onde ruído morre.
    MIN_MATERIAS  a segunda hipótese do C3 trazia UMA matéria, sobre um
    MIN_VEICULOS  empresário preso por homicídio. Um veículo não é acervo
                  cobrindo assunto, é coincidência de vocabulário — e
                  corroboração neste projeto sempre se conta por veículo.

O que esta saída NÃO faz, e o ARCHITECTURE assume que faria: contar
ENTIDADES ("7 empresas"). Nada no código extrai nome de empresa de título
de matéria, e das 32 matérias de recuperação judicial só 2 estavam
extraídas. Número de empresa aqui seria inventado; a saída conta matéria,
veículo e período, que são medidos.
"""

from dataclasses import dataclass, field

from src import indice

LIMIAR = 0.75
"""Proximidade mínima. Medido: ver o cabeçalho."""

MIN_MATERIAS = 3
MIN_VEICULOS = 2
"""Piso para a saída existir. Abaixo disso não é cobertura do acervo."""

QUANTOS = 200
"""Teto da busca. `indice.busca` devolve exatamente o que se pede e não
conta nada — quem corta é o LIMIAR, aqui embaixo."""

TETO_POR_RODADA = 8
"""Buscas de contexto por rodada do boletim. A busca é local e não custa
API, mas vetorizar não é grátis em tempo e um post com dez
`nao_verificavel` faria dez buscas — o ARCHITECTURE pede teto próprio."""


@dataclass
class Contexto:
    """O que o acervo tem sobre um assunto. Nunca um veredito."""

    assunto: str
    materias: int
    veiculos: list[str]
    de: str
    ate: str
    amostra: list[tuple[str, str]] = field(default_factory=list)
    """(veículo, título) dos mais próximos. A fonte vai junto porque
    princípio 2: nada é apresentado sem de onde veio."""


def do_assunto(assunto: str, buscar=None) -> Contexto | None:
    """O contexto de um assunto, ou None se o acervo não o cobre.

    `buscar` é injetável de propósito: `indice.DIR_INDICE` é global e
    aponta para a coleção de PRODUÇÃO, então teste que não injeta leria o
    acervo do dia e mudaria de resultado sozinho.
    """
    if not (assunto or "").strip():
        return None
    achados = [a for a in (buscar or indice.busca)("artigos", assunto,
                                                   QUANTOS)
               if a.proximidade >= LIMIAR]
    if not achados:
        return None

    # Matéria editada entra duas vezes no índice, com ids diferentes e o
    # mesmo assunto. Contar ACHADO em vez de matéria inflaria o número.
    por_materia: dict[object, object] = {}
    for a in achados:
        chave = a.meta.get("artigo_id", a.texto)
        if chave not in por_materia:
            por_materia[chave] = a
    unicos = list(por_materia.values())

    veiculos = sorted({a.meta.get("veiculo", "") for a in unicos} - {""})
    if len(unicos) < MIN_MATERIAS or len(veiculos) < MIN_VEICULOS:
        return None

    datas = sorted(d[:10] for d in
                   (str(a.meta.get("data") or "") for a in unicos) if d)
    melhores = sorted(unicos, key=lambda a: -a.proximidade)[:3]
    return Contexto(
        assunto=assunto.strip(),
        materias=len(unicos),
        veiculos=veiculos,
        de=datas[0] if datas else "",
        ate=datas[-1] if datas else "",
        amostra=[(str(a.meta.get("veiculo", "")),
                  str(a.meta.get("titulo", ""))) for a in melhores],
    )


def linha(c: Contexto) -> str:
    """Uma linha de texto puro. Descreve o ACERVO, nunca a premissa."""
    periodo = f" ({c.de} a {c.ate})" if c.de and c.ate else ""
    return (f"o acervo registra {c.materias} matérias em "
            f"{len(c.veiculos)} veículos{periodo}")
