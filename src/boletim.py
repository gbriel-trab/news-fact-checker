"""Boletim diário do radar: posts dos handles com as premissas conferidas.

    python -m src.boletim               # posts novos das últimas 24h
    python -m src.boletim --dias 3      # janela maior
    python -m src.boletim --sem-envio   # monta e grava, não envia

A terceira saída do sistema, e a que reconcilia o AC1 com o ARCHITECTURE:
é proativa (agente, não chatbot) e verifica população DISTINTA do acervo
(o que os handles alegaram, nunca a imprensa contra ela mesma). O post
indica onde olhar; a evidência vem do acervo — post não entra nele.

O fluxo por rodada:

    radar.busca(handles, janela)
      → descarta o que já foi entregue (tabela boletim_posts, por hash de
        conteúdo E por ID de status quando o bloco traz URL validada)
      → para cada post inédito: premissas.separa → check de cada fato
      → monta o texto → imprime → grava em data/boletins/ → envia

REGRA DE ENTREGA: o arquivo em data/boletins/ é o registro; um post só é
marcado como entregue DEPOIS de o arquivo do dia ser gravado. Post cuja
conferência falhou no meio (API fora do ar) não é marcado — volta inteiro
na próxima rodada, e os vereditos que chegaram a ser pagos são reusados
pela janela do check em vez de pagos de novo. Telegram é melhor-esforço:
falha de envio vira status legível apontando o arquivo, nunca perda.

Entrega por TELEGRAM quando TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID estiverem
no .env (bot gratuito via @BotFather; ver .env.example). WhatsApp fica como
camada futura — o montador é o mesmo, só troca o carteiro.

Custo por rodada: a busca na xAI (~US$ 0,03) + separação por post inédito
(~US$ 0,03) + uma verificação por premissa factual (~US$ 0,02-0,05). A soma
do rodapé vem do LIVRO-CAIXA, não de heurística: o custo de verificação é
lido das linhas que a rodada de fato gravou em `consultas` — veredito
reusado não grava linha e não soma.

O enquadramento é lei aqui, dobrado — e vai no CABEÇALHO além do rodapé,
para sobreviver a entrega parcial: a saída é conferência de premissas,
nunca placar do autor; o texto de cada post é transcrição de modelo,
conferível no link.
"""

import argparse
import contextlib
import hashlib
import io
import os
import re
import sys
from datetime import datetime, timezone

import requests

from . import config

DIR_BOLETINS = config.DIR_DADOS / "boletins"
LIMITE_TELEGRAM = 4096

ENQUADRAMENTO = ("Conferência de premissas contra o acervo — não avalia o "
                 "autor. Sem evidência = o acervo não cobre.")


_RE_CABECALHO = re.compile(r"^POST\s+\d+[^\n]*\n?")
_RE_LINHA_URL = re.compile(r"^\s*URL:[^\n]*$\n?", re.MULTILINE | re.IGNORECASE)
_RE_LINHA_RESPOSTA = re.compile(r"^\s*EM RESPOSTA A[^\n]*$\n?",
                                re.MULTILINE | re.IGNORECASE)


def _hash_post(texto: str) -> str:
    """Hash do CONTEÚDO do post: cabeçalho 'POST N (...)', linha URL: e
    linha EM RESPOSTA A ficam de fora. O N muda a cada rodada — com o
    cabeçalho no hash, o mesmo post voltava como inédito na rodada
    seguinte (defeito notado em 01/09/2026); as outras duas linhas variam
    conforme o modelo obedece ou não ao formato."""
    corpo = _RE_LINHA_RESPOSTA.sub(
        "", _RE_LINHA_URL.sub("", _RE_CABECALHO.sub("", texto)))
    normalizado = " ".join(corpo.lower().split())
    return hashlib.sha256(normalizado.encode("utf-8")).hexdigest()[:16]


def _chaves_do_post(post: str, links: tuple[str, ...]) -> set[str]:
    """As identidades de um post para dedup: hash do conteúdo sempre; e
    'url:<id do status>' quando o bloco traz URL validada contra as
    citações da busca. A URL é a identidade forte — sobrevive a variação
    de transcrição; o hash cobre bloco sem URL e o histórico anterior à
    linha URL:. Post editado no X ganha status novo, então a versão
    pré-edição continua contando como inédita, como decidido."""
    from . import radar
    chaves = {_hash_post(post)}
    url, confere = radar.url_do_post(post, links)
    if url and confere:
        chaves.add("url:" + radar.id_status(url))
    return chaves


def _ja_entregues(conexao) -> set[str]:
    return {linha["hash"] for linha in
            conexao.execute("SELECT hash FROM boletim_posts")}


def _marca_entregue(conexao, hash_: str, resumo: str) -> None:
    conexao.execute(
        "INSERT OR IGNORE INTO boletim_posts (hash, resumo, entregue_em) "
        "VALUES (?, ?, ?)",
        (hash_, resumo[:120], datetime.now(timezone.utc).isoformat()))
    conexao.commit()


_RE_EVIDENCIA = re.compile(
    r"^\s*\[([^\]]+)\][^\n]*\n\s+(https?://\S+)", re.MULTILINE)


def _confere_post(post: str, conexao, estado: dict) -> tuple[str, float, dict]:
    """Separa as premissas de um post e confere cada fato. Devolve
    (bloco de texto puro, custo Anthropic, dados estruturados).

    `estado` é MUTÁVEL de propósito — {"acervo": ..., "orcamento": ...} —
    e pertence à rodada, não ao post: acervo recarregado e orçamento de
    demanda debitado sobrevivem mesmo quando esta função morre no meio
    (a revisão de 01/09/2026 mostrou que devolver os dois por retorno
    deixava uma exceção restaurar o orçamento já gasto — TETO furado no
    caminho de falha, que é o caminho onde teto mais importa).

    Os dados estruturados alimentam a rendição HTML do Telegram — montada
    das linhas de `consultas`, nunca do stdout capturado, para o celular
    não herdar a verborragia do terminal. O texto puro continua sendo a
    trilha completa (arquivo e console).

    Antes de cada fato ir ao check, a EXTRAÇÃO SOB DEMANDA (`demanda`)
    tenta cobrir a premissa com matéria coletada e ainda não extraída,
    dentro do orçamento da rodada. Se extraiu, o check roda com
    `forcar=True` — sem isso a janela de reuso de 24h devolvia o
    "sem evidência" antigo e a extração recém-paga nunca era julgada.
    Demanda é otimização: falha nela vira linha do boletim e um débito
    PESSIMISTA no orçamento (não dá para saber se a chamada chegou a ser
    cobrada), nunca derruba o check.

    O custo vem do livro-caixa: soma das linhas de `consultas` gravadas
    DURANTE esta função (id > marco) mais o custo faturado das extrações
    de demanda; veredito reusado não grava e não soma.
    """
    from . import check, demanda, grafo, premissas, radar

    marco = conexao.execute(
        "SELECT COALESCE(MAX(id), 0) FROM consultas").fetchone()[0]

    # A linha EM RESPOSTA A entra REATRIBUÍDA ao interlocutor — as
    # palavras do outro não podem virar premissa do autor do post.
    analise, uso = premissas.separa(radar.para_separacao(post),
                                    conexao=conexao)
    partes: list[str] = []
    custo_demanda = 0.0
    dados: dict = {"nao_verificaveis": [], "checks": [],
                   "sem_premissas": not analise.premissas}

    resto = [p for p in analise.premissas if p.tipo != "fato"]
    fatos = [p for p in analise.premissas if p.tipo == "fato"]

    for p in resto:
        partes.append(f"  [{p.tipo}] {p.afirmacao} — nada a conferir")
        dados["nao_verificaveis"].append((p.tipo, p.afirmacao))

    for p in fatos:
        nota_demanda = None
        try:
            r = demanda.garante(conexao, p.afirmacao, estado["orcamento"])
        except Exception as erro:  # noqa: BLE001 — otimização não derruba
            r = None
            # Débito pessimista: a falha pode ter vindo DEPOIS de a
            # chamada ser cobrada (llm.py registra esse caso), e teto que
            # não desconta falha não é teto.
            estado["orcamento"] -= demanda.CUSTO_ESTIMADO
            partes.append(f"  demanda falhou ({type(erro).__name__}: "
                          f"{erro}) — verificando só com o acervo; "
                          f"orçamento debitado por precaução")
        if r is not None and r.motivo == "extraiu":
            estado["orcamento"] -= r.custo
            custo_demanda += r.custo
            estado["acervo"] = grafo.carrega(conexao)
            nota_demanda = (f"{r.materias} matéria(s) extraída(s) na hora, "
                            f"{r.triplas} triplas")
            partes.append(f"  [DEMANDA] {nota_demanda} · US$ {r.custo:.4f}")
        elif r is not None and r.motivo == "teto":
            partes.append("  [DEMANDA] teto da rodada atingido — "
                          "verificando só com o acervo")

        marco_fato = conexao.execute(
            "SELECT COALESCE(MAX(id), 0) FROM consultas").fetchone()[0]
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            check.verifica(p.afirmacao, conexao=conexao,
                           acervo=estado["acervo"],
                           forcar=(r is not None and r.motivo == "extraiu"))
        partes.append(f'  premissa: "{p.afirmacao}"')
        nova = conexao.execute(
            "SELECT * FROM consultas WHERE id > ? "
            "ORDER BY id DESC LIMIT 1", (marco_fato,)).fetchone()
        if nova is None:
            nova = check.consulta_recente(conexao, p.afirmacao)
        evidencias = _RE_EVIDENCIA.findall(saida.getvalue())
        dados["checks"].append({
            "afirmacao": p.afirmacao,
            "veredito": nova["veredito"] if nova else "sem_evidencia",
            "justificativa": nova["justificativa"] if nova else "",
            "veiculos": nova["veiculos"] if nova else 0,
            "custo": nova["custo_usd"] if nova else 0.0,
            "evidencias": evidencias,
            "demanda": nota_demanda,
        })
        # Sem evidência vira UMA linha: a enumeração do que foi olhado e
        # rejeitado é trilha de auditoria — mora em `consultas` e no
        # painel, não no bolso.
        if nova and nova["veredito"] == "sem_evidencia":
            partes.append(f"    → SEM EVIDÊNCIA — o acervo não cobre · "
                          f"US$ {nova['custo_usd']:.4f}")
        else:
            partes.append("    " + "\n    ".join(
                linha for linha in saida.getvalue().splitlines() if linha))

    if not analise.premissas:
        partes.append("  (nenhuma afirmação separável)")

    pago_em_checks = conexao.execute(
        "SELECT COALESCE(SUM(custo_usd), 0) FROM consultas WHERE id > ?",
        (marco,)).fetchone()[0]
    return ("\n".join(partes), uso.custo + pago_em_checks + custo_demanda,
            dados)


def monta(dias: int, reenviar: bool = False,
          ) -> tuple[str, float, list[tuple[set[str], str]], str]:
    """Roda a cadeia e devolve (texto, custo total, [(chaves, post)] dos
    posts contidos, HTML do Telegram). Quem marca entrega é o chamador,
    DEPOIS de gravar — e marca TODAS as chaves de cada post.

    Com `reenviar`, o estado 'já entregue' é ignorado e a janela inteira
    volta — para auditar formato novo sem apagar histórico. O dedup
    DENTRO da rodada continua valendo.

    Falha na conferência de um post não derruba a rodada nem o marca:
    o post volta inteiro na próxima, e o que já foi pago em vereditos é
    reusado pela janela do check.
    """
    from . import grafo, radar
    from .storage import conecta

    if not config.HANDLES_RADAR:
        raise SystemExit(
            "Nenhum handle no radar — defina HANDLES_RADAR no .env "
            "(ver .env.example) antes de rodar o boletim.")

    conexao = conecta(config.BANCO)
    try:
        acervo = grafo.carrega(conexao)
        if not acervo:
            raise SystemExit("Acervo vazio — rode coleta, extração e "
                             "índice antes do boletim.")

        try:
            rodada = radar.busca(config.HANDLES_RADAR, dias)
        except radar.FalhaNoRadar as erro:
            raise SystemExit(f"Busca do radar falhou: {erro}") from erro

        # `vistos` acumula as chaves da própria rodada: o modelo transcrever
        # o mesmo post duas vezes não pode virar entrega dupla.
        vistos = set() if reenviar else _ja_entregues(conexao)
        ineditos: list[tuple[str, set[str]]] = []
        for p in rodada.posts:
            chaves = _chaves_do_post(p, rodada.links)
            if chaves & vistos:
                continue
            vistos |= chaves
            ineditos.append((p, chaves))

        hoje = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        handles = ", ".join("@" + h for h in config.HANDLES_RADAR)
        linhas = [f"RADAR · {handles} · {hoje}",
                  "transcrição de modelo — o registro é o post, no link",
                  ENQUADRAMENTO, ""]
        custo = rodada.custo_usd
        contidos: list[tuple[set[str], str]] = []
        estruturados: list[tuple[int, str, dict, str | None]] = []

        if not ineditos:
            linhas.append(f"Nenhum post novo na janela de {dias} dia(s)."
                          if not rodada.posts else
                          f"{len(rodada.posts)} post(s) na janela, todos já "
                          f"entregues em boletins anteriores.")
        from . import demanda
        # O estado é da RODADA e mutável de propósito: exceção num post
        # não pode restaurar orçamento de demanda já gasto nem descartar
        # o acervo recarregado (revisão de 01/09/2026).
        estado = {"acervo": acervo, "orcamento": demanda.TETO_USD}
        for i, (post, chaves) in enumerate(ineditos, 1):
            linhas.append(f"[{i}] {post}")
            url, confere = radar.url_do_post(post, rodada.links)
            if url and not confere:
                linhas.append("  aviso: a URL que o modelo deu para este "
                              "post não está entre as citações da busca — "
                              "link omitido")
            # Falha num post não derruba o lote — padrão do extract.main.
            try:
                bloco, gasto, dados = _confere_post(post, conexao, estado)
            except Exception as erro:  # noqa: BLE001 — vira linha do boletim
                linhas.append(f"  CONFERÊNCIA FALHOU ({type(erro).__name__}: "
                              f"{erro}) — o post volta na próxima rodada")
                linhas.append("")
                continue
            custo += gasto
            linhas.append(bloco)
            linhas.append("")
            contidos.append((chaves, post))
            estruturados.append((i, post, dados, url if confere else None))

        for nota in rodada.notas:
            linhas.append(f"aviso da busca: {nota}")
        if rodada.links:
            linhas.append("lidos na busca (SEM ordem — não correspondem à "
                          "numeração): " + " · ".join(rodada.links))
        linhas.append("")
        linhas.append(ENQUADRAMENTO)
        # Duas carteiras, dois consoles: quem confere fatura precisa saber
        # de qual bolso saiu cada parte.
        linhas.append(f"custo da rodada: US$ {custo:.4f} "
                      f"(busca xAI US$ {rodada.custo_usd:.4f} + "
                      f"Anthropic US$ {custo - rodada.custo_usd:.4f})")
        html = _formata_telegram(handles, hoje, estruturados, rodada.notas,
                                 rodada.links, custo, rodada.custo_usd)
        return "\n".join(linhas), custo, contidos, html
    finally:
        conexao.close()


# ------------------------------------------------------------- rendição

def _esc(texto: str) -> str:
    return (texto.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


_TAG_TIPO = {"opiniao": "OPINIÃO", "previsao": "PREVISÃO",
             "relato": "RELATO"}
_TAG_VEREDITO = {"confirmado": "CONFIRMADO", "contradito": "CONTRADITO",
                 "sem_evidencia": "SEM EVIDÊNCIA"}


def _formata_telegram(handles: str, hoje: str, estruturados, notas,
                      links, custo: float, custo_xai: float) -> str:
    """A rendição HTML do Telegram: os MESMOS dados do texto puro, com
    hierarquia visual — negrito no cabeçalho, itálico no post, etiqueta
    monoespaçada no tipo e link clicável na evidência. Trilha completa
    continua no arquivo; aqui é o resumo para o bolso. Tudo que vem de
    modelo ou de post passa por `_esc` antes de virar HTML.

    Sem emoji, por pedido (01/09/2026): etiquetas textuais [RELATO],
    [CONFIRMADO] etc. O Telegram não aceita cor de texto — a paleta é
    negrito (veredito), itálico (texto de post) e `<code>` (etiquetas),
    que os clientes renderizam num tom próprio: é a "cor" possível."""
    from . import radar

    def tag(texto: str) -> str:
        return f"<code>[{texto}]</code>"

    p: list[str] = [f"<b>RADAR · {_esc(handles)} · {hoje}</b>",
                    f"<i>{_esc(ENQUADRAMENTO)}</i>", ""]
    if not estruturados:
        p.append("Nenhum post novo na janela.")
    pareados: set[str] = set()
    for i, post, dados, url in estruturados:
        # A linha-cabeçalho do modelo ("POST 4 (@x, 30 Aug):") sai — o
        # número duplica o [n] — mas o parêntese (handle, data) fica. O
        # CORPO vai na íntegra, sem truncar: post é conteúdo, não resumo.
        # A linha URL: vira a âncora do cabeçalho (só quando validada
        # contra as citações); EM RESPOSTA A vira a linha de contexto ↳.
        meta = ""
        corpo = post
        if post.startswith("POST"):
            cabecalho, _, resto = post.partition("\n")
            corpo = resto or post
            m = re.search(r"\(([^)]+)\)", cabecalho)
            if m:
                meta = f" <i>({_esc(m.group(1))})</i>"
        resposta = None
        corpo_linhas: list[str] = []
        for linha in corpo.splitlines():
            limpa = linha.strip()
            if limpa.upper().startswith("URL:"):
                continue
            if limpa.upper().startswith("EM RESPOSTA A"):
                resposta = limpa
                continue
            corpo_linhas.append(linha)
        corpo = "\n".join(corpo_linhas).strip()

        ver = ""
        if url:
            ver = f' — <a href="{_esc(url)}">ver no X</a>'
            pareados.add(radar.id_status(url))
        p.append(f"<b>[{i}]</b>{meta}{ver}")
        if resposta:
            p.append(f"{tag('CONTEXTO')} <i>{_esc(resposta)}</i>")
        p.append(f"<i>{_esc(corpo)}</i>")
        for tipo, afirmacao in dados["nao_verificaveis"]:
            p.append(f"{tag(_TAG_TIPO.get(tipo, tipo.upper()))} "
                     f"{_esc(afirmacao)}")
        for c in dados["checks"]:
            rotulo = _TAG_VEREDITO.get(c["veredito"], c["veredito"].upper())
            if c.get("demanda"):
                p.append(f"{tag('DEMANDA')} {_esc(c['demanda'])}")
            if c["veredito"] == "sem_evidencia":
                p.append(f"<b>[{rotulo}]</b> o acervo não cobre · "
                         f"<i>{_esc(c['afirmacao'])}</i>")
            else:
                fontes = " · ".join(
                    f'<a href="{_esc(url)}">{_esc(veiculo)}</a>'
                    for veiculo, url in c["evidencias"][:4])
                p.append(f"<b>[{rotulo}]</b> · {c['veiculos']} veículo(s) — "
                         f"<i>{_esc(c['afirmacao'])}</i>")
                p.append(f"    {_esc(c['justificativa'])}")
                if fontes:
                    p.append(f"    {tag('EVIDÊNCIA')} {fontes}")
        if dados["sem_premissas"]:
            p.append("(nenhuma afirmação separável)")
        p.append("")
    for nota in notas:
        p.append(f"{tag('AVISO')} {_esc(nota)}")
    # Só as SOBRAS: link já pareado a um post não repete aqui. O texto da
    # âncora é o fim do ID do status, nunca um número — numerar este
    # conjunto foi o que fez o boletim de 31/08 prometer correspondência
    # que a API não dá (as citações vêm sem ordem nem posição).
    sobras = [u for u in links if radar.id_status(u) not in pareados]
    if sobras:
        ancoras = " · ".join(
            f'<a href="{_esc(u)}">…{(radar.id_status(u) or u)[-5:]}</a>'
            for u in sobras)
        p.append(f"Também lidos na busca, sem par com os posts acima "
                 f"(sem ordem): {ancoras}")
    p.append("")
    p.append(f"<i>custo: US$ {custo:.2f} (xAI {custo_xai:.2f} + "
             f"Anthropic {custo - custo_xai:.2f})</i>")
    return "\n".join(p)


# ---------------------------------------------------------------- entrega

def _grava(texto: str) -> "os.PathLike":
    DIR_BOLETINS.mkdir(parents=True, exist_ok=True)
    caminho = DIR_BOLETINS / (
        datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".txt")
    # Append: duas rodadas no mesmo dia ficam no mesmo arquivo, separadas.
    with open(caminho, "a", encoding="utf-8") as arquivo:
        arquivo.write(texto + "\n\n" + "=" * 72 + "\n\n")
    return caminho


def _envia_telegram(texto: str, html: bool = False) -> str:
    """Envia se o .env tiver bot e chat. Devolve o status para o relatório —
    qualquer falha vira texto, nunca traceback: o arquivo já é o registro.

    Com `html`, o corte em pedaços respeita quebras de linha (cortar no
    meio de uma tag quebraria o parse do Telegram inteiro)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return ("não enviado — TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes "
                "no .env (ver .env.example)")
    if html:
        pedacos, atual = [], ""
        for linha in texto.split("\n"):
            if len(atual) + len(linha) + 1 > LIMITE_TELEGRAM:
                pedacos.append(atual)
                atual = linha
            else:
                atual = f"{atual}\n{linha}" if atual else linha
        if atual:
            pedacos.append(atual)
    else:
        pedacos = [texto[i:i + LIMITE_TELEGRAM]
                   for i in range(0, len(texto), LIMITE_TELEGRAM)]
    for n, pedaco in enumerate(pedacos, 1):
        corpo = {"chat_id": chat, "text": pedaco,
                 "disable_web_page_preview": True}
        if html:
            corpo["parse_mode"] = "HTML"
        try:
            resposta = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=corpo, timeout=30)
        except requests.RequestException as erro:
            return (f"FALHOU no Telegram (pedaço {n}/{len(pedacos)}, "
                    f"{type(erro).__name__}) — o boletim está no arquivo")
        if not resposta.ok:
            return (f"FALHOU no Telegram ({resposta.status_code}, pedaço "
                    f"{n}/{len(pedacos)}): {resposta.text[:200]} — o "
                    f"boletim está no arquivo")
    return f"enviado ao Telegram em {len(pedacos)} mensagem(ns)"


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Boletim do radar: posts com premissas conferidas.")
    parser.add_argument("--dias", type=int, default=1,
                        help="janela da busca (padrão: 1)")
    parser.add_argument("--sem-envio", action="store_true",
                        help="monta e grava o arquivo, não envia")
    args = parser.parse_args()

    texto, custo, contidos, html = monta(args.dias)
    print(texto)
    caminho = _grava(texto)
    print(f"\ngravado em {caminho}")

    # Marca DEPOIS de gravar: o arquivo é o registro de entrega. Se o
    # processo morrer antes desta linha, nada foi marcado e a próxima
    # rodada refaz — reusando os vereditos pagos, pela janela do check.
    from .storage import conecta
    conexao = conecta(config.BANCO)
    for chaves, post in contidos:
        for chave in chaves:
            _marca_entregue(conexao, chave, post)
    conexao.close()

    if contidos and not args.sem_envio:
        # O celular recebe a rendição HTML; o texto puro é a trilha,
        # gravada no arquivo acima.
        print(f"entrega: {_envia_telegram(html, html=True)}")
    elif not contidos:
        print("entrega: pulada — nada novo")


if __name__ == "__main__":
    main()
