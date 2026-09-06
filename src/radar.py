"""Radar de rede social: o que os handles acompanhados estão alegando.

    python -m src.radar                     # posts recentes dos handles
    python -m src.radar --dias 5            # janela maior
    python -m src.radar --conferir 2        # separa e confere o post 2
    python -m src.radar --dry-run           # mostra o que seria pedido

O papel está fixado no ARCHITECTURE.md: rede social é RADAR, nunca evidência.
O post indica onde olhar; a evidência vem sempre da imprensa ou da
instituição. Nada do que este módulo captura entra no acervo.

A unidade que atravessa o módulo é o `x_api.Post`: texto literal do autor
(registro do servidor) e tipo CALCULADO de metadado em `x_api.classifica`.
As barreiras operam nos CAMPOS do post — id, tipo, pai_id —, nunca no
texto: o texto é do autor, e o autor escreve o que quiser. Texto só nasce
nas duas fronteiras de saída: `como_texto` (o que console e arquivo
mostram), que ninguém lê de volta, e `para_separacao` (o que o separador
de premissas recebe), que o modelo lê e que `premissas.texto_ancoravel`
lê em código, por dois prefixos contratados lá — é o único reparse que
resta, e está dito onde acontece.

Honestidade que a saída carrega sempre: conferir premissas de um post é
CONFERÊNCIA, nunca placar do autor. Premissa sem evidência = o acervo não
cobre, não "o autor errou".

Custo: ESTIMATIVA feita no cliente (`x_api.custo_estimado_usd`), e um
TETO. A regra do dono é custo estimado antes e REAL depois; por esta via o
real só existe na fatura do X.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import config, x_api
from .premissas import CONTEXTO_ALHEIO, CONTEXTO_PROPRIO
from .x_api import Post
from .x_auth import PrecisaAutorizar

LIMITE_POR_HANDLE = 100
"""Teto de posts lidos por handle numa rodada. É teto de GASTO, não de
janela: a API do X cobra por recurso devolvido (US$ 0,005), então 100
posts são US$ 0,50 por handle no pior caso — e o pior caso é uma janela
larga num handle prolífico. Ver `x_api.custo_estimado_usd`: o número é
teto, porque a cobrança é deduplicada dentro da janela de 24h UTC."""

TIPOS_QUE_FICAM = frozenset({"post", "thread", "citacao"})
"""O que vira captura: post raiz, o autor continuando a si mesmo, e o
autor comentando um post citado. Cai `resposta` — o autor respondendo a
OUTRA conta, por `in_reply_to_user_id ≠ author_id` no servidor —, porque
neste domínio a substância vive em post, quote e thread própria, e
resposta a terceiro não chega ao boletim (decisão do dono). Cai
`retweet`, que não traz palavra do autor. Tipo fora daqui cai com o nome
dele na nota: some com aviso, nunca em silêncio."""

_REPETIDO = "repetido na rodada (mesmo handle lido duas vezes)"
_RESPOSTA = ("resposta a outra conta (in_reply_to_user_id ≠ author_id, "
             "no servidor)")
_RETWEET = "retweet: o texto é de terceiro"
_CADEIA = "thread pendurada em post descartado (cadeia)"
_ESTRANHO = "tipo fora do vocabulário: "


class FalhaNoRadar(Exception):
    """A busca não pôde ser feita ou a resposta não pôde ser lida."""


@dataclass(frozen=True, slots=True)
class Captura:
    """Um post que passou pelas barreiras, com o post que ele referencia
    quando este foi lido na mesma rodada.

    `referenciado` é o pai da thread ou o post citado. Vem do índice da
    rodada, não de expansão: expandir o referenciado é outra cobrança na
    API (ver o cabeçalho de `x_api`), e no caso dominante do acervo o
    referenciado é do próprio handle, dentro da mesma janela. Fora dela
    fica None, e `busca` conta a falta nas notas."""

    post: Post
    referenciado: Post | None = None

    @property
    def contexto_proprio(self) -> bool:
        """O referenciado é palavra do PRÓPRIO autor — a thread, ou o autor
        citando a si mesmo? Decide a atribuição no texto do separador:
        palavra do autor resolve referência e ancora referente; palavra de
        terceiro, não. A premissa sai só do texto do post (regra 9). É
        comparação de autor, nunca de texto."""
        return (self.referenciado is not None
                and self.referenciado.autor.lower() == self.post.autor.lower())


@dataclass(frozen=True, slots=True)
class Rodada:
    """O que uma busca devolveu."""

    capturas: tuple[Captura, ...]
    descartados: tuple[tuple[Post, str], ...]
    """(post, motivo) do que foi lido e não virou captura. Vai para o
    arquivo do boletim e para o painel: descarte silencioso é o que
    esconde defeito."""
    notas: tuple[str, ...]
    """Avisos da rodada, legíveis, para o Telegram."""
    lidos: int
    """Posts devolvidos pela API — o que foi PAGO, capturado ou não."""
    custo_estimado_usd: float
    detalhe_custo: str = ""


def _handles_de(argumento: str) -> tuple[str, ...]:
    """Normaliza ANTES de filtrar: '@' sozinho vira vazio e cai fora.

    Na ordem inversa, '@' sobrevive ao filtro e vira handle vazio depois
    do lstrip — o guard de lista vazia vê um tuple de um elemento e não
    protege nada, e o handle vazio só morre em `x_api.id_do_handle`, como
    falha de leitura em vez de entrada ignorada.
    """
    return tuple(x for x in
                 (h.strip().lstrip("@").strip()
                  for h in argumento.split(","))
                 if x)


def quando(criado_em: str) -> str:
    """`created_at` ISO8601 -> "2026-09-03 17:50 UTC".

    É a data do cabeçalho, no texto do separador e no arquivo. UTC
    explícito porque o separador resolve "ontem" pelo cabeçalho (regra 9)
    e a máquina do dono está em outro fuso. Data ilegível volta como veio;
    ausente vira "sem data" — o cabeçalho sempre tem o parêntese."""
    texto = str(criado_em or "").strip()
    if not texto:
        return "sem data"
    try:
        instante = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return texto
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=timezone.utc)
    return instante.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --- barreiras -------------------------------------------------------------
#
# Três, na ordem em que a saída de uma alimenta a seguinte. Operam nos
# campos do Post e devolvem (ficam, descartados com motivo). O motivo vai
# para o arquivo do boletim e para o painel; a contagem vira nota.

def dedup(posts) -> tuple[list[Post], list[tuple[Post, str]]]:
    """Um post por id na rodada.

    Pela API cada handle é uma leitura paginada própria, então a mesma
    leitura não devolve o mesmo post duas vezes. O que dispara aqui é o
    mesmo handle repetido em `HANDLES_RADAR` ou em `--handles` — nenhuma
    das duas listas deduplica, e o handle repetido é lido duas vezes. A
    leitura duplicada já foi paga; o que se evita é pagar separação DUAS
    vezes pelo mesmo post e mandá-lo duas vezes ao Telegram. Post sem id
    passa: descartá-lo perderia post por falta de metadado, o erro caro."""
    vistos: set[str] = set()
    ficam: list[Post] = []
    fora: list[tuple[Post, str]] = []
    for p in posts:
        if p.id and p.id in vistos:
            fora.append((p, _REPETIDO))
            continue
        if p.id:
            vistos.add(p.id)
        ficam.append(p)
    return ficam, fora


def separa_por_tipo(posts) -> tuple[list[Post], list[tuple[Post, str]]]:
    """Fica `TIPOS_QUE_FICAM`; o resto cai com o motivo.

    É a barreira que segura a resposta a terceiro, e o veredito é do
    servidor: `x_api.classifica` só chama de `resposta` o que responde a
    outra conta, e falha FECHADO — post sem metadado que prove ser raiz
    vira `resposta`. O projeto prefere perder post legítimo a deixar
    entrar resposta a terceiro."""
    ficam: list[Post] = []
    fora: list[tuple[Post, str]] = []
    for p in posts:
        if p.tipo in TIPOS_QUE_FICAM:
            ficam.append(p)
        elif p.tipo == "resposta":
            fora.append((p, _RESPOSTA))
        elif p.tipo == "retweet":
            fora.append((p, _RETWEET))
        else:
            fora.append((p, _ESTRANHO + str(p.tipo)))
    return ficam, fora


def cadeia(ficam, fora) -> tuple[list[Post], list[tuple[Post, str]]]:
    """Thread pendurada em post descartado cai junto, até estabilizar.

    O autor respondendo a si mesmo DENTRO de uma resposta a terceiro é
    `thread` pelo metadado (o pai é do próprio autor), e o pai imediato é
    legítimo; quem entrega é o avô. Sem seguir a cadeia o neto passa, e o
    dono decidiu que não era para chegar onde chegou. Só `thread` segue a
    cadeia: citação de post descartado é comentário do autor sobre ele, e
    fica. Thread cujo pai não foi LIDO (fora da janela) fica: pai ausente
    não é pai descartado."""
    ficam = list(ficam)
    fora = list(fora)
    mudou = True
    while mudou:
        mudou = False
        ids_fora = {p.id for p, _ in fora if p.id} - {p.id for p in ficam}
        for p in list(ficam):
            if p.tipo == "thread" and p.pai_id and p.pai_id in ids_fora:
                ficam.remove(p)
                fora.append((p, _CADEIA))
                mudou = True
    return ficam, fora


def barreiras(posts) -> tuple[list[Post], list[tuple[Post, str]], list[str]]:
    """As três barreiras na ordem certa, e a nota de cada uma.

    Dedup primeiro, para as outras não gastarem comparação com repetido;
    tipo antes de cadeia, porque é o descarte da `resposta` que a cadeia
    segue. As notas vão para o Telegram e dizem a CAUSA de cada descarte:
    causa errada na nota é o dono decidindo com base em ficção."""
    ficam, fora = dedup(posts)
    ficam, fora_tipo = separa_por_tipo(ficam)
    fora += fora_tipo
    ficam, fora = cadeia(ficam, fora)

    conta: dict[str, int] = {}
    for _, motivo in fora:
        conta[motivo] = conta.get(motivo, 0) + 1
    # O nome do tipo desconhecido vem do CAMPO do post, não de fatiar a
    # string do motivo.
    estranhos = {p.tipo for p, _ in fora
                 if p.tipo not in TIPOS_QUE_FICAM
                 and p.tipo not in ("resposta", "retweet")}

    notas: list[str] = []
    if conta.get(_REPETIDO):
        notas.append(f"{conta[_REPETIDO]} post(s) repetido(s) na rodada "
                     f"descartado(s) antes de custar: mesmo handle lido "
                     f"duas vezes")
    if conta.get(_RESPOSTA):
        notas.append(f"{conta[_RESPOSTA]} resposta(s) a outra conta "
                     f"descartada(s) antes de custar: o servidor classificou "
                     f"como resposta a terceiro (in_reply_to_user_id ≠ "
                     f"author_id)")
    if conta.get(_RETWEET):
        notas.append(f"{conta[_RETWEET]} retweet(s) descartado(s): o texto "
                     f"é de terceiro, não é afirmação do handle")
    if estranhos:
        # Contado à parte, com o nome: se o vocabulário de `Post.tipo`
        # crescer um dia, o valor novo aparece em vez de ser chamado de
        # retweet em silêncio.
        notas.append(f"post(s) com tipo fora do vocabulário descartado(s): "
                     f"{', '.join(sorted(estranhos))}")
    if conta.get(_CADEIA):
        notas.append(f"{conta[_CADEIA]} continuação(ões) de thread "
                     f"pendurada(s) em post descartado, descartada(s) junto "
                     f"(cadeia)")
    return ficam, fora, notas


def captura(post: Post, indice: dict) -> Captura:
    """O post com o referenciado, se ele foi lido nesta rodada."""
    referenciado = None
    if post.tipo in ("thread", "citacao") and post.pai_id:
        referenciado = indice.get(post.pai_id)
    return Captura(post=post, referenciado=referenciado)


# --- as duas saídas em texto ---------------------------------------------

def _uma_linha(texto: str) -> str:
    return " ".join(str(texto or "").split())


def para_separacao(c: Captura) -> str:
    """O texto que o separador de premissas recebe.

    É contrato com o prompt (`premissas.INSTRUCOES`, regra 9) e com
    `premissas.texto_ancoravel`, e por isso os dois prefixos de contexto
    moram em `premissas`:

        POST (@handle, 2026-09-03 17:50 UTC):
        (contexto — post anterior do próprio autor: <texto>)
        (contexto — post citado pelo autor; as afirmações são de quem ele
         cita: <texto>)
        <texto do post>

    Sem número de post: o N é da rodada, e entraria no hash da separação
    em cache — o mesmo post separado noutra rodada pagaria de novo. Sem
    URL: ruído de tokens que o modelo não usa. A linha de contexto só sai
    quando o referenciado foi LIDO nesta rodada e tem texto, e vai numa
    linha só (espaço colapsado): `texto_ancoravel` exclui a linha do
    citado por prefixo, e um citado de várias linhas deixaria a segunda
    ancorável. A atribuição — próprio autor ou terceiro — é
    `Captura.contexto_proprio`, comparação de autor, não de texto."""
    linhas = [f"POST (@{c.post.autor}, {quando(c.post.criado_em)}):"]
    ref = c.referenciado
    if ref is not None and _uma_linha(ref.texto):
        prefixo = CONTEXTO_PROPRIO if c.contexto_proprio else CONTEXTO_ALHEIO
        linhas.append(f"({prefixo}: {_uma_linha(ref.texto)})")
    linhas.append(c.post.texto)
    return "\n".join(linhas)


def como_texto(c: Captura, numero: int) -> str:
    """Para console e arquivo do boletim. Ninguém lê isto de volta, então
    leva o que o humano quer ver: número, tipo, URL e o contexto."""
    p = c.post
    linhas = [f"POST {numero} (@{p.autor}, {quando(p.criado_em)}) · {p.tipo}"]
    if p.url:
        linhas.append(f"URL: {p.url}")
    ref = c.referenciado
    if ref is not None and _uma_linha(ref.texto):
        quem = ("post anterior do próprio autor" if c.contexto_proprio
                else f"post citado, de @{ref.autor}")
        linhas.append(f"contexto ({quem}): {_uma_linha(ref.texto)}")
    linhas.append(p.texto)
    return "\n".join(linhas)


# --- a rodada ----------------------------------------------------------------

def busca(handles: tuple[str, ...], dias: int = 2, *,
          desde: str = "", ate: str = "") -> Rodada:
    """Uma rodada do radar: a API oficial do X, um handle por vez.

    ASSINATURA CONGELADA — `boletim.monta` e `painel.rodar_radar` chamam
    `busca(handles, dias)` e esperam uma `Rodada`. É o que mantém a fonte
    fora dos chamadores. `desde`/`ate` são opcionais e só por nome.

    JANELA. `desde` é um instante, não uma data: `start_time` é ISO8601 com
    hora e o fim é "agora" por omissão, então a rodada vê o próprio dia.
    Com `desde` (e opcionalmente `ate`) explícitos — ISO8601, ou data solta
    que `x_api._iso` completa —, a janela é a pedida, e `dias` é ignorado:
    é como o boletim refaz um dia passado, um por vez (06/09/2026). O fim
    é EXCLUSIVO no endpoint: `desde=2026-08-25, ate=2026-08-26` é o dia 25
    inteiro, em UTC.

    FALHA POR HANDLE. `PrecisaAutorizar` aborta a rodada inteira na hora:
    sem consentimento humano nada vai destravar, e continuar tentando os
    outros handles só empilharia o mesmo erro. `FalhaNaAPI` num handle
    deixa os outros seguirem, com o handle faltante NAS NOTAS — perder a
    rodada toda por causa de um handle seria trocar uma lacuna anunciada
    por um dia inteiro sem boletim. Se TODOS falharem, sobe erro: rodada
    vazia entregue como sucesso esconderia uma queda total.
    """
    desde = desde or (datetime.now(timezone.utc)
                      - timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M:%SZ")

    lidos: list[Post] = []
    notas: list[str] = []
    falhas: list[str] = []
    for handle in handles:
        try:
            achados = x_api.posts_de(handle, desde=desde, ate=ate,
                                     limite=LIMITE_POR_HANDLE)
        except PrecisaAutorizar as erro:
            # A fronteira. `boletim.monta` captura `radar.FalhaNoRadar` e
            # mais nada, e é por ele que o aviso de falha chega ao
            # Telegram — outra classe subiria como traceback, fora do
            # aviso. A mensagem carrega o comando porque quem lê o aviso
            # não está no terminal, e o único desfecho possível é um humano
            # no navegador.
            raise FalhaNoRadar(
                f"a API do X exige autorização ({erro}). Rode à mão, no "
                f"computador do dono: venv/Scripts/python.exe -m src.x_auth "
                f"— nada aqui destrava sozinho.") from erro
        except x_api.FalhaNaAPI as erro:
            falhas.append(f"@{handle}: {erro}")
            continue
        if not achados:
            # Contagem nossa, não pedido: handle que não retornou nada é
            # dito por nome.
            notas.append(f"@{handle} não retornou nada na janela.")
        lidos.extend(achados)

    if falhas and len(falhas) == len(handles):
        raise FalhaNoRadar("a leitura falhou em TODOS os handles — "
                           + " · ".join(falhas))
    for falha in falhas:
        notas.append(f"handle sem leitura nesta rodada — {falha}")

    ficam, descartados, notas_barreiras = barreiras(lidos)
    notas.extend(notas_barreiras)
    # O índice é sobre TUDO que foi lido, descartado ou não: o pai de uma
    # thread própria pode ter caído na cadeia sem que a thread caísse — e
    # aí não há nada a mostrar — mas o citado de uma citação pode ser um
    # post descartado por outro motivo, e o texto dele ainda é contexto.
    indice = {p.id: p for p in lidos if p.id}
    capturas = tuple(captura(p, indice) for p in ficam)

    if (sem_referenciado := sum(
            1 for c in capturas
            if c.post.tipo in ("thread", "citacao") and c.post.pai_id
            and c.referenciado is None)):
        # Esta nota vai para o Telegram: descrever errado o que aconteceu
        # é informação errada para quem decide se vai atrás do post que
        # falta.
        notas.append(
            f"{sem_referenciado} post(s) com o post referenciado FORA da "
            f"janela lida: vão sem a linha de contexto — expandir o "
            f"referenciado é outra cobrança na API do X")
    # Ramo DEFENSIVO: não há caso medido de `referenced_*` sem `id`, e a
    # doc do X não foi conferida quanto a isso. Contado à parte porque a
    # causa é outra (metadado incompleto, não janela curta), e descarte
    # silencioso é o que esconde defeito.
    if (citacoes_sem_id := sum(1 for c in capturas
                               if c.post.tipo == "citacao"
                               and not c.post.pai_id)):
        notas.append(
            f"{citacoes_sem_id} citação(ões) sem o id do post citado no "
            f"metadado: vão sem contexto")

    # `lidos` conta para o CUSTO mesmo o que não virou captura: a API
    # cobra por recurso devolvido, e retweet descartado já foi pago.
    return Rodada(
        capturas=capturas,
        descartados=tuple(descartados),
        notas=tuple(notas),
        lidos=len(lidos),
        custo_estimado_usd=x_api.custo_estimado_usd(len(lidos)),
        # A regra do dono é informar o custo ESTIMADO antes e o REAL depois.
        # Por esta via o real não existe: o X não devolve preço nenhum. O
        # que fica é uma multiplicação nossa, e ainda por cima um TETO
        # (cobrança deduplicada em 24h UTC). Este detalhe vai só para o
        # console do radar; o rodapé do boletim rotula a metade da busca
        # como estimada por conta própria.
        detalhe_custo=(
            f"custo ESTIMADO no cliente: {len(lidos)} post(s) devolvido(s) "
            f"× US$ {x_api.PRECO_POR_POST_USD:.3f} — teto, não medição; o "
            f"real só na fatura do X"),
    )


def _confere(c: Captura, custo_busca: float) -> None:
    """Separa as premissas do post e julga cada uma, no rito do premissas.

    O rito importa tanto quanto o resultado: acervo vazio aborta ANTES
    de pagar verificação; previsão e opinião saem nomeadas pelo que são,
    nunca como descarte; o trecho literal aparece antes de cada veredito
    (é o elo auditável entre o que o autor escreveu e o que foi
    conferido); e o fecho impede a leitura de placar.
    """
    from . import check, grafo, premissas
    from .storage import conecta

    conexao = conecta(config.BANCO)
    acervo = grafo.carrega(conexao)
    if not acervo:
        print("Acervo vazio. Rode a coleta, a extração e o índice antes "
              "de conferir — verificar contra o nada só gasta.")
        conexao.close()
        sys.exit(1)

    analise, uso = premissas.separa(para_separacao(c), conexao=conexao)
    fatos = [p for p in analise.premissas if p.tipo == "fato"]
    resto = [p for p in analise.premissas if p.tipo != "fato"]

    if resto:
        print("NÃO VERIFICÁVEL — e não deve ser")
        for p in resto:
            print(f"  [{p.tipo}] {p.texto}{premissas.anotacao(p)}")
        print()

    if not fatos:
        print("Nenhuma premissa verificável no post.")
    else:
        for i, p in enumerate(fatos, 1):
            print(f"[{i}/{len(fatos)}] no post: \"{p.trecho[:110]}\"")
            check.verifica(p.texto, conexao=conexao, acervo=acervo)
    conexao.close()

    print("\nIsto confere premissas contra o acervo, não avalia o autor.")
    print("Premissa sem evidência significa que os veículos coletados não")
    print("cobrem o assunto — não que a afirmação seja falsa.")
    print(f"\n  separação: US$ {uso.custo:.4f} · mais uma verificação por "
          f"premissa · busca: US$ {custo_busca:.4f} (estimado)")


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Radar: o que os handles acompanhados estão alegando.")
    parser.add_argument("--handles",
                        help="lista separada por vírgula; sem isto, usa "
                             "config.HANDLES_RADAR")
    parser.add_argument("--dias", type=int, default=2,
                        help="janela da busca (padrão: 2)")
    parser.add_argument("--conferir", type=int, metavar="N",
                        help="separa as premissas do post N e confere cada "
                             "uma contra o acervo (mais chamadas pagas)")
    parser.add_argument("--dry-run", action="store_true",
                        help="mostra a requisição, sem chamar a API")
    args = parser.parse_args()

    handles = (_handles_de(args.handles) if args.handles
               else config.HANDLES_RADAR)
    if not handles:
        print("Nenhum handle válido. Ver HANDLES_RADAR em config.py.")
        sys.exit(1)

    if args.dry_run:
        # Não há "corpo da requisição" único para imprimir: é um GET por
        # handle, paginado. O que interessa ver antes de gastar é a janela,
        # o teto por handle e o teto de custo.
        desde = (datetime.now(timezone.utc) - timedelta(
            days=args.dias)).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"GET /2/users/:id/tweets · start_time={desde} · "
              f"max_results até {LIMITE_POR_HANDLE} por handle")
        for handle in handles:
            print(f"  @{handle}")
        teto = x_api.custo_estimado_usd(LIMITE_POR_HANDLE * len(handles))
        print(f"\n  teto de gasto: US$ {teto:.4f} "
              f"(estimativa do cliente, não medição)")
        print("\nNada foi enviado. Remova --dry-run para rodar.")
        return

    try:
        rodada = busca(handles, args.dias)
    except FalhaNoRadar as erro:
        print(f"FALHOU: {erro}")
        sys.exit(1)

    print(f"RADAR · {', '.join('@' + h for h in handles)} · "
          f"últimos {args.dias} dias")
    # A linha de procedência diz de onde vêm texto e tipo — e o que ela
    # afirma é verificável no link.
    print("  API oficial do X — texto e tipo vêm do servidor\n")

    if not rodada.capturas:
        print("Nenhum post na janela.")
    for i, c in enumerate(rodada.capturas, 1):
        print(f"{como_texto(c, i)}\n")
    for p, motivo in rodada.descartados:
        print(f"  descartado: @{p.autor} {p.url or p.id or '(sem id)'} "
              f"— {motivo}")
    for nota in rodada.notas:
        print(f"  aviso da busca: {nota}")
    print(f"\n  {rodada.lidos} post(s) lido(s) · busca: "
          f"US$ {rodada.custo_estimado_usd:.4f}"
          + (f" ({rodada.detalhe_custo})" if rodada.detalhe_custo else ""))

    if args.conferir is not None:
        if not (1 <= args.conferir <= len(rodada.capturas)):
            print(f"\nNão existe post {args.conferir} nesta rodada.")
            sys.exit(1)
        print("\n" + "=" * 78)
        print(f"CONFERINDO O POST {args.conferir}")
        print("=" * 78)
        _confere(rodada.capturas[args.conferir - 1],
                 rodada.custo_estimado_usd)


if __name__ == "__main__":
    main()
