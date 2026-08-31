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


def _confere_post(post: str, conexao, acervo) -> tuple[str, float]:
    """Separa as premissas de um post e confere cada fato. Devolve
    (bloco de texto do boletim, custo em US$ das chamadas Anthropic).

    O custo de verificação vem do livro-caixa: soma-se `custo_usd` das
    linhas de `consultas` gravadas DURANTE esta função (id > marco).
    Veredito reusado não grava linha, logo não soma — sinal estrutural,
    nunca busca de palavra no texto capturado (a premissa "plástico
    reusado cresceu 20%" derrubaria qualquer heurística textual).
    """
    from . import check, premissas

    marco = conexao.execute(
        "SELECT COALESCE(MAX(id), 0) FROM consultas").fetchone()[0]

    analise, uso = premissas.separa(post)
    partes: list[str] = []

    resto = [p for p in analise.premissas if p.tipo != "fato"]
    fatos = [p for p in analise.premissas if p.tipo == "fato"]

    for p in resto:
        partes.append(f"  [{p.tipo}] {p.afirmacao} — nada a conferir")

    for p in fatos:
        marco_fato = conexao.execute(
            "SELECT COALESCE(MAX(id), 0) FROM consultas").fetchone()[0]
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            check.verifica(p.afirmacao, conexao=conexao, acervo=acervo)
        partes.append(f'  premissa: "{p.afirmacao}"')
        nova = conexao.execute(
            "SELECT veredito, custo_usd FROM consultas WHERE id > ? "
            "ORDER BY id DESC LIMIT 1", (marco_fato,)).fetchone()
        # Sem evidência vira UMA linha no boletim: a enumeração do que foi
        # olhado e rejeitado é trilha de auditoria — fica gravada em
        # `consultas` e visível no painel/CLI, não no celular. Exibir menos
        # não economiza nada (o texto já foi pago ao ser gerado); isto é
        # legibilidade, e a economia real é a premissa nem existir.
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
    return "\n".join(partes), uso.custo + pago_em_checks


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

        if not ineditos:
            linhas.append(f"Nenhum post novo na janela de {dias} dia(s)."
                          if not rodada.posts else
                          f"{len(rodada.posts)} post(s) na janela, todos já "
                          f"entregues em boletins anteriores.")
        for i, (post, h) in enumerate(ineditos, 1):
            linhas.append(f"[{i}] {post}")
            # Falha num post não derruba o lote — padrão do extract.main.
            try:
                bloco, gasto = _confere_post(post, conexao, acervo)
            except Exception as erro:  # noqa: BLE001 — vira linha do boletim
                linhas.append(f"  CONFERÊNCIA FALHOU ({type(erro).__name__}: "
                              f"{erro}) — o post volta na próxima rodada")
                linhas.append("")
                continue
            custo += gasto
            linhas.append(bloco)
            linhas.append("")
            contidos.append((h, post))

        for nota in rodada.notas:
            linhas.append(f"aviso da busca: {nota}")
        if rodada.links:
            linhas.append("links: " + " · ".join(rodada.links))
        linhas.append("")
        linhas.append(ENQUADRAMENTO)
        linhas.append(f"custo da rodada: US$ {custo:.4f}")
        return "\n".join(linhas), custo, contidos
    finally:
        conexao.close()


# ---------------------------------------------------------------- entrega

def _grava(texto: str) -> "os.PathLike":
    DIR_BOLETINS.mkdir(parents=True, exist_ok=True)
    caminho = DIR_BOLETINS / (
        datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".txt")
    # Append: duas rodadas no mesmo dia ficam no mesmo arquivo, separadas.
    with open(caminho, "a", encoding="utf-8") as arquivo:
        arquivo.write(texto + "\n\n" + "=" * 72 + "\n\n")
    return caminho


def _envia_telegram(texto: str) -> str:
    """Envia se o .env tiver bot e chat. Devolve o status para o relatório —
    qualquer falha vira texto, nunca traceback: o arquivo já é o registro."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return ("não enviado — TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes "
                "no .env (ver .env.example)")
    pedacos = [texto[i:i + LIMITE_TELEGRAM]
               for i in range(0, len(texto), LIMITE_TELEGRAM)]
    for n, pedaco in enumerate(pedacos, 1):
        try:
            resposta = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": pedaco,
                      "disable_web_page_preview": True},
                timeout=30)
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

    texto, custo, contidos = monta(args.dias)
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
        print(f"entrega: {_envia_telegram(texto)}")
    elif not contidos:
        print("entrega: pulada — nada novo")


if __name__ == "__main__":
    main()
