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
      → descarta o que já foi entregue (tabela boletim_posts, por hash)
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


def _hash_post(texto: str) -> str:
    normalizado = " ".join(texto.lower().split())
    return hashlib.sha256(normalizado.encode("utf-8")).hexdigest()[:16]


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


def _confere_post(post: str, conexao, acervo) -> tuple[str, float, dict]:
    """Separa as premissas de um post e confere cada fato. Devolve
    (bloco de texto puro, custo Anthropic, dados estruturados).

    Os dados estruturados alimentam a rendição HTML do Telegram — montada
    das linhas de `consultas`, nunca do stdout capturado, para o celular
    não herdar a verborragia do terminal. O texto puro continua sendo a
    trilha completa (arquivo e console).

    O custo vem do livro-caixa: soma das linhas de `consultas` gravadas
    DURANTE esta função (id > marco); veredito reusado não grava e não
    soma — sinal estrutural, nunca busca de palavra no texto capturado.
    """
    from . import check, premissas

    marco = conexao.execute(
        "SELECT COALESCE(MAX(id), 0) FROM consultas").fetchone()[0]

    analise, uso = premissas.separa(post, conexao=conexao)
    partes: list[str] = []
    dados: dict = {"nao_verificaveis": [], "checks": [],
                   "sem_premissas": not analise.premissas}

    resto = [p for p in analise.premissas if p.tipo != "fato"]
    fatos = [p for p in analise.premissas if p.tipo == "fato"]

    for p in resto:
        partes.append(f"  [{p.tipo}] {p.afirmacao} — nada a conferir")
        dados["nao_verificaveis"].append((p.tipo, p.afirmacao))

    for p in fatos:
        marco_fato = conexao.execute(
            "SELECT COALESCE(MAX(id), 0) FROM consultas").fetchone()[0]
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            check.verifica(p.afirmacao, conexao=conexao, acervo=acervo)
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
    return "\n".join(partes), uso.custo + pago_em_checks, dados


def monta(dias: int) -> tuple[str, float, list[str]]:
    """Roda a cadeia e devolve (texto, custo total, hashes dos posts que o
    texto contém). Quem marca entrega é o chamador, DEPOIS de gravar.

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

        entregues = _ja_entregues(conexao)
        ineditos = [(p, _hash_post(p)) for p in rodada.posts
                    if _hash_post(p) not in entregues]

        hoje = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        handles = ", ".join("@" + h for h in config.HANDLES_RADAR)
        linhas = [f"📡 RADAR · {handles} · {hoje}",
                  "transcrição de modelo — o registro é o post, no link",
                  ENQUADRAMENTO, ""]
        custo = rodada.custo_usd
        contidos: list[tuple[str, str]] = []
        estruturados: list[tuple[int, str, dict]] = []

        if not ineditos:
            linhas.append(f"Nenhum post novo na janela de {dias} dia(s)."
                          if not rodada.posts else
                          f"{len(rodada.posts)} post(s) na janela, todos já "
                          f"entregues em boletins anteriores.")
        for i, (post, h) in enumerate(ineditos, 1):
            linhas.append(f"[{i}] {post}")
            # Falha num post não derruba o lote — padrão do extract.main.
            try:
                bloco, gasto, dados = _confere_post(post, conexao, acervo)
            except Exception as erro:  # noqa: BLE001 — vira linha do boletim
                linhas.append(f"  CONFERÊNCIA FALHOU ({type(erro).__name__}: "
                              f"{erro}) — o post volta na próxima rodada")
                linhas.append("")
                continue
            custo += gasto
            linhas.append(bloco)
            linhas.append("")
            contidos.append((h, post))
            estruturados.append((i, post, dados))

        for nota in rodada.notas:
            linhas.append(f"aviso da busca: {nota}")
        if rodada.links:
            linhas.append("links: " + " · ".join(rodada.links))
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


_EMOJI_TIPO = {"opiniao": "💬", "previsao": "🔮", "relato": "👤"}
_EMOJI_VEREDITO = {"confirmado": "✅", "contradito": "❌",
                   "sem_evidencia": "⚪"}


def _formata_telegram(handles: str, hoje: str, estruturados, notas,
                      links, custo: float, custo_xai: float) -> str:
    """A rendição HTML do Telegram: os MESMOS dados do texto puro, com
    hierarquia visual — negrito no cabeçalho, itálico no post, semáforo no
    veredito e link clicável na evidência. Trilha completa continua no
    arquivo; aqui é o resumo para o bolso. Tudo que vem de modelo ou de
    post passa por `_esc` antes de virar HTML."""
    p: list[str] = [f"📡 <b>Radar · {_esc(handles)} · {hoje}</b>",
                    f"<i>{_esc(ENQUADRAMENTO)}</i>", ""]
    if not estruturados:
        p.append("Nenhum post novo na janela.")
    for i, post, dados in estruturados:
        # A linha-cabeçalho do modelo ("POST 4 (@x, 30 Aug):") sai — o
        # número duplica o [n] — mas o parêntese (handle, data) fica. O
        # CORPO vai na íntegra, sem truncar: post é conteúdo, não resumo.
        meta = ""
        corpo = post
        if post.startswith("POST"):
            cabecalho, _, resto = post.partition("\n")
            corpo = resto or post
            m = re.search(r"\(([^)]+)\)", cabecalho)
            if m:
                meta = f" <i>({_esc(m.group(1))})</i>"
        p.append(f"<b>[{i}]</b>{meta}")
        p.append(f"<i>{_esc(corpo.strip())}</i>")
        for tipo, afirmacao in dados["nao_verificaveis"]:
            p.append(f"{_EMOJI_TIPO.get(tipo, '•')} {_esc(afirmacao)}")
        for c in dados["checks"]:
            emoji = _EMOJI_VEREDITO.get(c["veredito"], "•")
            if c["veredito"] == "sem_evidencia":
                p.append(f"{emoji} <b>sem evidência</b> — o acervo não "
                         f"cobre · <i>{_esc(c['afirmacao'])}</i>")
            else:
                rotulo = c["veredito"].replace("_", " ")
                fontes = " · ".join(
                    f'<a href="{_esc(url)}">{_esc(veiculo)}</a>'
                    for veiculo, url in c["evidencias"][:4])
                p.append(f"{emoji} <b>{rotulo}</b> · "
                         f"{c['veiculos']} veículo(s) — "
                         f"<i>{_esc(c['afirmacao'])}</i>")
                p.append(f"    {_esc(c['justificativa'])}")
                if fontes:
                    p.append(f"    fontes: {fontes}")
        if dados["sem_premissas"]:
            p.append("(nenhuma afirmação separável)")
        p.append("")
    for nota in notas:
        p.append(f"⚠️ {_esc(nota)}")
    if links:
        ancoras = " · ".join(f'<a href="{_esc(u)}">{n}</a>'
                             for n, u in enumerate(links, 1))
        p.append(f"🔗 posts no X: {ancoras}")
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
    for h, post in contidos:
        _marca_entregue(conexao, h, post)
    conexao.close()

    if contidos and not args.sem_envio:
        # O celular recebe a rendição HTML; o texto puro é a trilha,
        # gravada no arquivo acima.
        print(f"entrega: {_envia_telegram(html, html=True)}")
    elif not contidos:
        print("entrega: pulada — nada novo")


if __name__ == "__main__":
    main()
