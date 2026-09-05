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
                  cobrindo assunto, é coincidência de vocabulário.

                  Subiu de 3/2 para 5/3 em 03/09/2026, com dois pontos
                  medidos. O caso ruim é o post de uma frase do status
                  1000000000000000006 (boletim de 03/09/2026, item [3]):
                  gerou a hipótese "uma ferramenta de IA não conseguiu
                  resolver algo... o texto não identifica a tarefa", e o
                  contexto saiu com 3 matérias em 2 veículos sobre
                  engenharia de prompt e IA em recrutamento — nada a ver,
                  e no piso exato. O C19, que é o caso bom, dá 10 matérias
                  em 7 veículos. 5/3 separa os dois com folga dos dois
                  lados.

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

MIN_MATERIAS = 5
MIN_VEICULOS = 3
"""Piso para a saída existir. Abaixo disso não é cobertura do acervo."""

QUANTOS = 200
"""Quantos achados pedir por vez. `indice.busca` devolve EXATAMENTE o que
se pede, então quando todos os pedidos passam do limiar quem cortou foi
esta constante, não o limiar — e aí o número publicado seria a constante
disfarçada de contagem do acervo. Medido em 03/09/2026: "a economia
brasileira" tem 653 matérias acima do limiar, e o mesmo assunto publicava
"50", "200" ou "400" matérias conforme o valor daqui. Por isso
`do_assunto` dobra o pedido enquanto saturar — a busca é local e não custa
API."""

TETO_BUSCA = 4096
"""Onde a expansão para. Acima disso a saída diz "mais de N", que é
verdade, em vez de um número que não foi medido."""

AMOSTRA = 5
"""Quantos títulos mostrar. Ver a nota em `do_assunto`: com três, o post
das recuperações judiciais não exibia marca nenhuma."""

LIGADO = False
"""A quarta saída está DESLIGADA desde 03/09/2026, e o motivo é uma
medição, não desânimo.

Ela saiu em 4 dos 5 posts de um boletim entregue e errou em 3:

  "pessoas não identificadas teriam encontrado um erro de CEP em
   processos judiciais"          -> 11 matérias em 5 veículos
  "analistas macroeconômicos não identificados"
                                 -> 64 matérias em 11 veículos
  "o autor parece dizer que uma ferramenta de IA não conseguiu
   resolver algo"                -> 3 matérias em 2 veículos

Nenhuma é assunto. E o gate que eu tinha — piso de matérias e veículos —
NÃO PODE funcionar, porque o caso bom e os ruins são indistinguíveis por
contagem:

  C19, o caso bom .......... 10 matérias ·  7 veículos · 0,10% do acervo
  erro de CEP, ruim ........ 11 matérias ·  5 veículos · 0,11%
  anunciaram topo, ruim .....  9 matérias ·  4 veículos · 0,09%

Subir o piso mata o caso bom junto; baixar deixa tudo passar. A
diferença é SEMÂNTICA — "recuperações judiciais em marcas conhecidas" é
assunto, "pessoas não identificadas acharam erro de CEP" não é — e
contagem não vê semântica.

Para voltar, precisa de um gate que julgue se a hipótese NOMEIA um
assunto. Isso é chamada de modelo, com custo e com caso no gabarito. O
código, os testes e os limiares medidos ficam aqui inteiros: o que falta
é o gate, não a máquina."""

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
    saturou: bool = False
    """A busca bateu no teto: `materias` é piso, não contagem, e a linha
    diz "mais de N". Publicar o teto como medição foi o defeito que a
    revisão adversarial de 03/09/2026 achou nesta saída."""
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
    procurar = buscar or indice.busca
    pedido, saturou = QUANTOS, False
    while True:
        bruto = procurar("artigos", assunto, pedido)
        achados = [a for a in bruto if a.proximidade >= LIMIAR]
        # Saturou: TODO achado devolvido passou do limiar, ou seja o corte
        # foi o tamanho do pedido. Dobra e pergunta de novo — senão o
        # número publicado é a constante, não o acervo.
        if len(achados) < len(bruto) or len(bruto) < pedido:
            break
        if pedido >= TETO_BUSCA:
            saturou = True
            break
        pedido *= 2
    if not achados:
        return None

    # Dedup por URL, não por artigo_id: na coleção "artigos" o id do
    # documento É o artigo_id, então dois achados nunca compartilham
    # artigo_id e deduplicar por ele não faz nada. A duplicata real é a
    # matéria RECOLETADA, que vira linha nova com id novo — 17% do índice
    # medido em 03/09/2026, com um caso de 31 versões da mesma página.
    por_materia: dict[object, object] = {}
    for a in achados:
        chave = (a.meta.get("url_norm")
                 or a.meta.get("artigo_id", a.texto))
        if chave not in por_materia:
            por_materia[chave] = a
    unicos = list(por_materia.values())

    veiculos = sorted({a.meta.get("veiculo", "") for a in unicos} - {""})
    if len(unicos) < MIN_MATERIAS or len(veiculos) < MIN_VEICULOS:
        return None

    datas = sorted(d[:10] for d in
                   (str(a.meta.get("data") or "") for a in unicos) if d)
    # A amostra é o que o leitor de fato lê, e ela precisa MOSTRAR o
    # assunto, não só provar que ele existe. Duas regras, e as duas
    # nasceram do post das recuperações judiciais (03/09/2026):
    #
    # 1. Dedup por TÍTULO. G1 e BBC publicam "Do Habib's às Casas Bahia"
    #    com o mesmo título, e Agência Brasil e G1 idem com "Dona de
    #    Habib's e Ragazzo" — sindicação comia 2 das 3 vagas com uma
    #    matéria só. Isso NÃO muda a contagem: veículo distinto publicando
    #    a mesma matéria continua sendo corroboração e continua contando.
    #    Muda só o que se mostra.
    # 2. Cinco, não três. Com três, a amostra saía "explicador genérico do
    #    InfoMoney + o mesmo título duas vezes" e nenhuma marca aparecia,
    #    embora as marcas estivessem nos títulos das outras sete.
    por_titulo: dict[str, object] = {}
    for a in sorted(unicos, key=lambda a: -a.proximidade):
        titulo = str(a.meta.get("titulo", "")).strip().casefold()
        if titulo not in por_titulo:
            por_titulo[titulo] = a
    melhores = list(por_titulo.values())[:AMOSTRA]
    return Contexto(
        assunto=assunto.strip(),
        materias=len(unicos),
        veiculos=veiculos,
        de=datas[0] if datas else "",
        ate=datas[-1] if datas else "",
        saturou=saturou,
        amostra=[(str(a.meta.get("veiculo", "")),
                  str(a.meta.get("titulo", ""))) for a in melhores],
    )


def linha(c: Contexto) -> str:
    """Uma linha de texto puro. Descreve o ACERVO, nunca a premissa."""
    periodo = f" ({c.de} a {c.ate})" if c.de and c.ate else ""
    quanto = f"mais de {c.materias}" if c.saturou else str(c.materias)
    return (f"o acervo registra {quanto} matérias em "
            f"{len(c.veiculos)} veículos{periodo}")
