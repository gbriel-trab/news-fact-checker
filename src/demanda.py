"""Extração sob demanda: premissa sem cobertura busca no que foi coletado.

O caso que motivou (01/09/2026): um post pergunta "André se reúne com
Trump, quem manda no Brasil?" — a matéria do G1 sobre a reunião estava
COLETADA havia horas, mas o seletor de extração não a tinha priorizado,
e o check respondia "sem evidência" por cegueira, não por falta de
cobertura. O funil de extração prioriza corroboração (histórias com 2+
veículos); a demanda cobre o resto: extrai na hora exatamente o que a
premissa precisa, e só isso.

O CICLO MORA AQUI, e não no check — de propósito. O check promete no
próprio docstring que nada decide o próximo passo em tempo de execução;
essa promessa fica de pé. Quem decide é este módulo, com regra fixa,
UMA volta só e teto de gasto: é o princípio 7 do ARCHITECTURE em código
(o ciclo serve para tentar outra fonte, não para insistir até inventar).

A PERGUNTA "o acervo cobre?" é respondida pelo CHECK, nunca por proxy:
quem chama roda o check primeiro e só aciona a demanda sobre um veredito
"sem evidência" — e re-verifica com `forcar` depois de extrair. A versão
inicial usava proximidade vetorial como oráculo de cobertura e caiu no
primeiro teste vivo (01/09/2026): "Esteves integra BTG" no índice fingia
cobrir a premissa da REUNIÃO com Trump; proximidade casa entidade, não o
fato — que é literalmente a regra 2 do julgamento do check.

Freios, na ordem em que seguram:

1. Só entra matéria com título+lead a >= LIMIAR_CANDIDATA da premissa,
   dentro da janela de dias EM TORNO DA DATA DO POST (não de "agora": o
   boletim refeito de 26/08 rodou em 07/09, e as cinco matérias do G1 e
   da Folha sobre o diretor da CIA em Moscou, de 25 a 27/08, ficavam fora
   da janela "últimos 10 dias" — o dono achou a do G1 à mão), que MENCIONE
   o referente (o QUEM) da
   premissa, uma por veículo, no máximo MAX_MATERIAS, e coerente com a
   melhor candidata. A guarda de referente é de 06/09/2026: proximidade
   casa vocabulário, e "as alts que postei andaram entre 40 e 50%" puxou
   três pesquisas eleitorais (Ciro 50%, Quaest 66%, AtlasIntel 48%) —
   sete matérias e US$ 0,13 sobre eleição para dois fatos de cripto.
2. Matéria já extraída pelas versões ATIVAS de prompt fica fora: ela já
   teve a vez, e re-extração seria moto-perpétuo de gasto. Extração de
   versão antiga não conta — a órfã volta a ser elegível, exatamente
   como no funil de lote.
3. O chamador informa o orçamento restante da rodada; abaixo de
   CUSTO_ESTIMADO a demanda recusa ANTES de chamar a API.
"""

import re
import sqlite3
from dataclasses import dataclass

from . import extract, indice
from .premissas import _ARTIGOS, _FECHADAS, _normaliza

LIMIAR_CANDIDATA = 0.60
"""Piso premissa↔título+lead para uma matéria virar candidata.

Mais alto que o MIN_PROXIMIDADE do check (0,55) de propósito: candidata
custa extração PAGA, e falso positivo aqui custa dinheiro, não só posição
de ranking. Ainda sem calibração dedicada — 0,60 é chute honesto na
família dos limiares medidos do projeto, registrado como tal."""

MAX_MATERIAS = 4
"""Uma história basta para cobrir uma premissa; quatro veículos é mais
corroboração do que o check precisa para confirmar."""

TETO_USD = 0.50
"""Teto de gasto de demanda por RODADA do boletim (não por premissa).

A lição do estouro de 31/08 (US$ 4,56 contra teto combinado de 4): teto
que não é código não segura nada. Este nasce junto com a funcionalidade."""

CUSTO_ESTIMADO = 0.15
"""Pior caso medido de uma chamada de história (US$ 0,017-0,12 na estreia
do v3), arredondado para cima. Serve só para decidir se o orçamento
comporta MAIS UMA extração; o custo real vem da fatura da chamada."""


@dataclass(frozen=True, slots=True)
class Resultado:
    """O que uma volta do ciclo fez, e por quê."""

    motivo: str  # "sem_candidata" | "teto" | "teto_diario" | "extraiu" | "sem_tripla"
    materias: int
    triplas: int
    custo: float


_TAMANHO_MINIMO = 3
"""Token do referente com menos de três letras não decide nada ("BC" é
sigla legítima, mas "de", "os" não): sem token útil, a guarda não filtra."""

_CABECAS_GENERICAS = frozenset(
    "taxa taxas governo banco bancos bolsa mercado mercados empresa "
    "empresas programa projeto lei presidente ministro ministra setor "
    "numero numeros dados pesquisa relatorio indice valor preco precos "
    "acordo reuniao encontro".split())
"""Cabeça de sintagma que sozinha não identifica assunto: "a taxa de juros"
casaria "Taxa de desocupação" pelo "taxa" (revisão de 06/09/2026). Fica
fora dos termos; se só sobrar cabeça genérica, a guarda não filtra."""


def _termos(referente: str) -> list[str]:
    """Os tokens do referente que valem como assunto: só letras e dígitos
    (a pontuação colada cai — "Ibovespa, Nasdaq" era "ibovespa," e nunca
    casava), fora artigo, preposição, pronome, palavra curta e cabeça
    genérica. "as alts que postei" → ["alts", "postei"]; "a taxa de juros"
    → ["juros"]; "o BC" → [] (sem filtro).

    Quando o referente traz NOME PRÓPRIO — token com inicial maiúscula fora
    da primeira posição, ou @handle, ou $ticker —, só os nomes próprios
    valem: "O cartão da @ether_fi" é a ether.fi, não qualquer cartão. Sem
    isso, a demanda pagou uma matéria da Ethena por "cartão" e "cashback"
    (26/08 do segundo handle, US$ 0,08)."""
    crus = [c for c in re.findall(r"[@$]?\w+", referente)
            if c.lstrip("@$") and _normaliza(c.lstrip("@$")) not in _ARTIGOS]
    # A inicial maiúscula do PRIMEIRO token pode ser só começo de frase
    # ("Muitos terremotos"): ela conta como nome próprio quando os outros
    # tokens também são nomes ("Ibovespa, Nasdaq, Russell, SPX") ou quando
    # o token está sozinho ("Esteves").
    outros_sao_nomes = all(c[0] in "@$" or c.lstrip("@$")[0].isupper()
                           for c in crus[1:])
    proprios = []
    for i, cru in enumerate(crus):
        limpo = cru.lstrip("@$")
        if cru[0] in "@$" or (limpo[0].isupper()
                              and (i > 0 or outros_sao_nomes)):
            proprios.append(_normaliza(limpo))
    uteis = [t for t in re.findall(r"\w+", _normaliza(referente))
             if len(t) >= _TAMANHO_MINIMO
             and t not in _ARTIGOS and t not in _FECHADAS
             and t not in _CABECAS_GENERICAS]
    so_proprios = [t for t in uteis if t in proprios]
    return so_proprios or uteis


def _menciona(linha, referente: str) -> bool:
    """A matéria menciona o referente da premissa?

    Conferido sobre o MESMO texto que o ranking vetorial viu — título +
    começo do corpo (`agrupa.texto_de_agrupamento`), não título + resumo:
    Estadão e UOL chegam com resumo vazio e o lead está no corpo (revisão
    de 06/09/2026). Basta UM termo útil do referente, em fronteira de
    palavra: é a mesma exigência que `check.aplica_alinhamento` faz no
    veredito (o QUEM da evidência tem de conter o QUEM da afirmação),
    trazida para ANTES de pagar a extração. Referente sem termo útil não
    filtra — o erro caro aqui é o falso negativo de cobertura, e o roteador
    já barrou os pronomes antes de chegar aqui. Flexão não é tolerada
    ("marcas icônicas" não casa "marca icônica"): é limite conhecido, e o
    lado errado dele é o barato."""
    from . import agrupa

    termos = _termos(referente)
    if not termos:
        return True
    texto = _normaliza(agrupa.texto_de_agrupamento(linha))
    return any(re.search(rf"(?<!\w){re.escape(t)}(?!\w)", texto)
               for t in termos)


def inicio_do_acervo(conexao: sqlite3.Connection) -> str:
    """Data ISO da matéria mais antiga do acervo, ou "". Piso para a
    âncora da janela: data escrita no post anterior a isso não tem
    matéria possível, e a janela ficaria em torno do nada."""
    try:
        linha = conexao.execute(
            "SELECT MIN(data_publicacao) FROM artigos "
            "WHERE data_publicacao IS NOT NULL").fetchone()
    except sqlite3.Error:
        return ""
    return str(linha[0] or "") if linha else ""


def _janela(quando: str = "") -> tuple[str, str]:
    """(início, fim) ISO da janela de matérias: `agrupa.JANELA_DIAS` para
    cada lado da data do post — ou de agora, sem data. Notícia sobre o que
    o post afirma sai dias antes e dias depois dele; e num boletim refeito
    semanas depois, "últimos N dias" olhava para o lugar errado."""
    from datetime import datetime, timedelta, timezone

    from . import agrupa

    centro = None
    texto = str(quando or "").strip()
    if texto:
        try:
            centro = datetime.fromisoformat(texto.replace("Z", "+00:00"))
            if centro.tzinfo is None:
                centro = centro.replace(tzinfo=timezone.utc)
        except ValueError:
            centro = None
    if centro is None:
        centro = datetime.now(timezone.utc)
    raio = timedelta(days=agrupa.JANELA_DIAS)
    return (centro - raio).isoformat(), (centro + raio).isoformat()


def candidatas(conexao: sqlite3.Connection, texto: str,
               referente: str = "", quando: str = "") -> list[sqlite3.Row]:
    """Matérias coletadas, ainda sem extração ATUAL, próximas da premissa.

    Cinco peneiras, na ordem (três vieram da revisão de 01/09/2026; a
    do referente, de 06/09/2026):

    * Menção ao REFERENTE da premissa no título+lead (`_menciona`), quando
      o chamador o informa: proximidade vetorial casa porcentagem com
      porcentagem e "pesquisa" com "enquete", e pagou extração de pesquisa
      eleitoral para premissa sobre altcoins.

    * Uma por veículo — duas editorias do mesmo veículo não são fontes
      independentes (regra do agrupamento) — na ordem do ranking.
    * Janela de data NA RECUPERAÇÃO, não só na indexação: a coleção
      acumula e o upsert não poda, então sem este corte uma premissa de
      tema recorrente casaria com a matéria do trimestre passado e
      pagaria extração de notícia obsoleta.
    * "Já extraída" conta só extração das VERSÕES ATIVAS de prompt —
      igual ao funil de lote, que se autocura num bump de versão. Sem o
      filtro, matéria órfã de vocabulário antigo (invisível para o grafo)
      ficava bloqueada aqui para sempre: a cegueira que o módulo existe
      para curar.
    * Guarda de coesão ENTRE as candidatas, contra a melhor delas: o
      grupo nasce da proximidade com a PREMISSA, e duas matérias podem
      orbitar a mesma premissa cobrindo fatos diferentes — o lote tem o
      `agrupa` para isso; aqui o carona é expulso antes de pagar, porque
      mesma_historia=false gravaria marcador em TODAS.
    """
    from . import agrupa

    indice.indexa_artigos(conexao)
    achadas = indice.busca("artigos", texto, quantos=12)
    inicio, fim = _janela(quando)
    ids = [int(a.meta["artigo_id"]) for a in achadas
           if a.proximidade >= LIMIAR_CANDIDATA
           and inicio <= str(a.meta.get("data", "")) <= fim]
    if not ids:
        return []
    linhas = {l["id"]: l for l in extract._por_id(conexao, ids)}
    por_veiculo: dict[str, sqlite3.Row] = {}
    for i in ids:
        linha = linhas.get(i)
        if linha is None:
            continue
        if ja_extraida(conexao, i):
            continue
        if referente and not _menciona(linha, referente):
            continue
        por_veiculo.setdefault(linha["veiculo"], linha)
    grupo = list(por_veiculo.values())[:MAX_MATERIAS]
    if len(grupo) > 1:
        vetores = indice.vetoriza(
            [agrupa.texto_de_agrupamento(l) for l in grupo])
        base = vetores[0]
        grupo = [l for l, v in zip(grupo, vetores)
                 if float(v @ base) >= agrupa.LIMIAR_COESAO]
    return grupo


TETO_RECUSAS = 3
"""Quantas recusas de grupo uma matéria pode acumular antes de deixar de
ser recomprada.

Não pode ser 1: isso é literalmente o comportamento de antes de
03/09/2026, quando a primeira recusa tornava a matéria invisível e a G1
"Joesley Batista se reuniu com Trump" sumiu do acervo. O erro caro aqui
é o falso NEGATIVO de cobertura — deixar de ver matéria que existe —,
então o teto é folgado de propósito: três grupos ruins seguidos é
evidência de que a matéria atrai agrupamento errado, não acidente.

O teto é POR VERSÃO DE PROMPT, não vitalício: `ja_extraida` já filtra por
`prompt_versao`, e todo bump perdoa o histórico. Coerente com o resto (a
órfã de versão antiga também volta ao mercado), mas dito em voz alta
porque não é o que "teto" sugere."""


def ja_extraida(conexao: sqlite3.Connection, artigo_id: int) -> bool:
    """Extração ATUAL da matéria que conte como 'já teve a vez'.

    Linha marcada `recusada` não conta: o modelo disse que o GRUPO estava
    errado, não que a matéria não tinha nada. Até 03/09/2026 contava, e a
    G1 "Joesley Batista se reuniu com Trump", puxada por engano para o
    grupo da premissa "o empresário", ficou invisível para o acervo até um
    bump de versão.

    Mas a marca sozinha não fechava o ciclo, só o amortecia: `TETO_USD` é
    da RODADA e nasce de novo a cada boletim, então a mesma matéria podia
    ser recomprada e recusada indefinidamente, uma vez por rodada, para
    sempre. Desde 03/09/2026 conta-se QUANTAS vezes: passado
    `TETO_RECUSAS`, a matéria volta a valer como 'já teve a vez'."""
    return conexao.execute(
        "SELECT COUNT(*) FROM extracoes WHERE artigo_id = ? "
        "AND prompt_versao IN (?, ?) "
        "AND (COALESCE(recusada, 0) = 0 OR COALESCE(recusas, 0) >= ?)",
        (artigo_id, extract.PROMPT_VERSAO,
         extract.PROMPT_VERSAO_HISTORIA, TETO_RECUSAS)).fetchone()[0] > 0


def garante(conexao: sqlite3.Connection, texto: str,
            orcamento: float = TETO_USD, *, referente: str = "",
            quando: str = "") -> Resultado:
    """Uma volta do ciclo: cobre a premissa se der, dentro do orçamento.

    `referente` é o QUEM ancorado da premissa (o `quem.valor` do
    separador); com ele, só candidata que o mencione paga extração.
    `quando` é a data do post (ISO): a janela de matérias fica em torno
    dela, não de hoje.

    Pressupõe que o chamador JÁ verificou e recebeu "sem evidência" — a
    demanda não re-pergunta se o acervo cobre (ver o docstring do módulo).
    Nunca levanta a mão de novo: extraiu ou recusou, o chamador segue para
    o check com o acervo que houver. Falha de API sobe como exceção — quem
    chama decide se ela derruba a rodada (o boletim não deixa).
    """
    grupo = candidatas(conexao, texto, referente, quando)
    if not grupo:
        return Resultado("sem_candidata", 0, 0, 0.0)
    if orcamento < CUSTO_ESTIMADO:
        return Resultado("teto", 0, 0, 0.0)
    # O teto DIÁRIO de extração (extract.TETO_DIARIO_USD) vale também aqui:
    # é o mesmo livro-caixa, e a demanda foi quem estourou em 01/09.
    try:
        triplas, custo, recusada = extract.extrai_grupo(conexao, grupo)
    except extract.TetoDiario:
        return Resultado("teto_diario", 0, 0, 0.0)
    if recusada and len(grupo) > 1 and orcamento - custo >= CUSTO_ESTIMADO:
        # O modelo recusou o grupo — e pode ter razão: aqui o grupo nasce
        # da proximidade com a premissa, não da coesão do lote. A melhor
        # candidata ainda pode cobrir sozinha; UMA re-tentativa, só com
        # ela, dentro do orçamento. Sem isto, a matéria certa morria
        # queimada junto com o carona (revisão de 01/09/2026).
        t2, c2, _ = extract.extrai_grupo(conexao, grupo[:1])
        triplas += t2
        custo += c2
    if not triplas:
        # Extraiu e não rendeu: o chamador não pode tratar como sucesso —
        # recarregar o acervo e pagar um segundo check com `forcar` sobre
        # um acervo que não ganhou uma tripla sequer é gasto garantido
        # sem chance de mudar o veredito (revisão de 03/09/2026).
        return Resultado("sem_tripla", len(grupo), 0, custo)
    if triplas:
        # O check tem duas rotas: a por chave lê o grafo direto do banco,
        # mas a vetorial só enxerga o que o índice tem. Só as matérias do
        # grupo — reindexar o recorte inteiro custava minutos por rodada.
        try:
            indice.indexa_afirmacoes(conexao,
                                     so_artigos=[l["id"] for l in grupo])
        except Exception:  # noqa: BLE001
            # Falha de índice não pode transformar extração PAGA em
            # "demanda falhou": a rota por chave segue funcionando com o
            # acervo recarregado, e a vetorial se cura na reindexação do
            # CLI. Registrar e seguir.
            pass
    return Resultado("extraiu", len(grupo), triplas, custo)
