"""Ponto de entrada da coleta.

    python -m src.collect

Percorre todos os feeds configurados, grava o que for inédito e imprime um
relatório. Pensado para rodar em intervalo curto (a cada 30 minutos), já que
feed RSS não guarda histórico: o que não for coletado enquanto está no feed
não pode ser recuperado depois.
"""

import sys

from . import config
from .collectors import rss
from .collectors.rss import FalhaNoFeed
from .models import ResultadoGravacao
from .storage import conecta, estatisticas, salva


def _prepara_saida() -> None:
    """Força UTF-8 na saída: o console do Windows corrompe acento sem isso."""
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")


def coleta_tudo() -> int:
    """Roda a coleta. Devolve o código de saída do processo."""
    conexao = conecta(config.BANCO)
    falhas: list[str] = []
    total = dict.fromkeys(ResultadoGravacao, 0)

    print(f"Acervo: {config.BANCO}\n")

    for fonte, url_feed in config.FEEDS.items():
        try:
            artigos = rss.busca(fonte, url_feed)
        except FalhaNoFeed as erro:
            falhas.append(fonte)
            print(f"  {fonte:<16} FALHOU  {erro}")
            continue

        contagem = dict.fromkeys(ResultadoGravacao, 0)
        for artigo in artigos:
            resultado = salva(conexao, artigo)
            contagem[resultado] += 1
            total[resultado] += 1

        print(
            f"  {fonte:<16} {len(artigos):>3} itens"
            f"  |  {contagem[ResultadoGravacao.NOVO]:>3} novos"
            f"  {contagem[ResultadoGravacao.ATUALIZADO]:>3} atualizados"
            f"  {contagem[ResultadoGravacao.DUPLICADO]:>3} repetidos"
        )

    numeros = estatisticas(conexao)
    conexao.close()

    print(
        f"\nNesta rodada: {total[ResultadoGravacao.NOVO]} novos, "
        f"{total[ResultadoGravacao.ATUALIZADO]} atualizados, "
        f"{total[ResultadoGravacao.DUPLICADO]} repetidos"
    )
    print(
        f"Acervo total: {numeros['materias']} matérias, "
        f"{numeros['registros']} registros, "
        f"{numeros['fontes']} fontes, "
        f"{numeros['bytes_conteudo'] / 1_048_576:.1f} MB de texto"
    )

    # Falha de feed vira código de saída diferente de zero para que a coleta
    # agendada possa alertar. Feed quieto por semanas é buraco no acervo, e
    # buraco no acervo não se recupera.
    if falhas:
        print(f"\nFeeds com falha: {', '.join(falhas)}")
        return 1
    return 0


def main() -> None:
    _prepara_saida()
    sys.exit(coleta_tudo())


if __name__ == "__main__":
    main()
