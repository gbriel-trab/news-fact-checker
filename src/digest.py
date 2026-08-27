"""Digest do acervo: o que a imprensa publicou na janela, e o que se sustenta.

    python -m src.digest                          # últimas 24 horas
    python -m src.digest --horas 72
    python -m src.digest --topicos "Braskem,eleições,juros"

É a saída PROATIVA do sistema, e a contraparte do `check.py`. Os dois respondem
perguntas diferentes:

    check.py   você traz uma afirmação   →  o acervo sustenta?
    digest.py  você não traz nada        →  o que o acervo sustenta hoje?

Não chama modelo nenhum. Tudo que ele reporta já foi extraído e pago antes; o
digest é leitura, agrupamento e contagem. Custo zero por execução, de propósito
— saída que roda sozinha várias vezes ao dia não pode ter custo por rodada.

A REGRA QUE ORGANIZA A SAÍDA: um fato de veículo único nunca aparece junto dos
confirmados. Ele existe numa seção própria, marcada, e a separação é estrutural
em vez de textual — não há como um item não corroborado ser lido como
corroborado por descuido de formatação.

LIMITAÇÃO: sem estado entre execuções. Rodar duas vezes na mesma janela mostra
o mesmo conteúdo — ele reporta a janela, não "o que mudou desde a última vez".
Guardar o que já foi entregue é o passo seguinte, e é pré-requisito do envio
automático: entrega repetida é o jeito mais rápido de tornar um digest
ignorável.
"""

import argparse
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import config, grafo
from .storage import conecta


def _sem_acento(texto: str) -> str:
    """Compara "eleições" com "eleicoes". O filtro de tópicos é digitado à mão,
    e exigir acento certo transformaria erro de digitação em resultado vazio."""
    decomposto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in decomposto if unicodedata.category(c) != "Mn")


def filtra_topicos(afirmacoes: list[grafo.Afirmacao],
                   topicos: list[str]) -> list[grafo.Afirmacao]:
    """Mantém só as afirmações que mencionam algum dos tópicos.

    O casamento é por substring em sujeito, objeto e título — grosseiro e
    escolhido assim. O índice vetorial casaria por sentido, mas aqui isso é
    defeito: quem digita "juros" quer juros, e ampliar para o que é
    semanticamente próximo devolveria inflação e câmbio junto, sem pedir.
    """
    alvos = [_sem_acento(t.strip()) for t in topicos if t.strip()]
    if not alvos:
        return afirmacoes
    return [
        a for a in afirmacoes
        if any(t in _sem_acento(f"{a.sujeito} {a.objeto or ''} {a.titulo}")
               for t in alvos)
    ]


@dataclass(frozen=True, slots=True)
class Recorte:
    """O acervo dentro da janela, já separado pelo que sustenta o quê."""

    afirmacoes: tuple[grafo.Afirmacao, ...]
    confirmados: tuple[grafo.Corroboracao, ...]
    unicos: tuple[grafo.Corroboracao, ...]
    divergentes: tuple[grafo.Corroboracao, ...]

    @property
    def veiculos(self) -> set[str]:
        return {a.veiculo for a in self.afirmacoes}

    @property
    def materias(self) -> set[str]:
        return {a.url for a in self.afirmacoes}

    @property
    def taxa_confirmacao(self) -> float:
        """Fração dos fatos distintos que dois veículos independentes afirmam.

        É o critério 3 do AC1 — que pede 95% — medido em vez de alegado. O
        número real hoje é muito menor, e imprimi-lo é o ponto: critério que
        não é medido não é critério, é intenção.
        """
        total = len(self.confirmados) + len(self.unicos)
        return len(self.confirmados) / total if total else 0.0


def recorta(afirmacoes: list[grafo.Afirmacao]) -> Recorte:
    grupos = grafo.agrupa(afirmacoes)
    confirmados = [g for g in grupos if g.confirmada]
    return Recorte(
        afirmacoes=tuple(afirmacoes),
        # Ordenado por número de veículos: corroboração é o critério de
        # relevância aqui, não recência. Fato que quatro redações publicaram
        # importa mais que o que uma publicou há dez minutos.
        confirmados=tuple(sorted(confirmados, key=lambda c: -len(c.veiculos))),
        unicos=tuple(g for g in grupos if not g.confirmada),
        divergentes=tuple(g for g in grupos if g.diverge),
    )


def _rotulo(c: grafo.Corroboracao) -> str:
    sujeito, relacao, objeto, contexto = c.chave
    texto = f"({sujeito}, {relacao.replace('_', ' ')}, {objeto or '—'})"
    return texto + (f" · {contexto}" if contexto else "")


def _valor(a: grafo.Afirmacao) -> str:
    if a.valor is None:
        return ""
    return f" = {a.valor:g} {a.unidade or ''}".rstrip()


def imprime(recorte: Recorte, horas: int, topicos: list[str]) -> None:
    escopo = f"últimas {horas}h"
    if topicos:
        escopo += f" · tópicos: {', '.join(topicos)}"

    print(f"DIGEST · {escopo}")
    print(f"  {len(recorte.materias)} matérias de {len(recorte.veiculos)} "
          f"veículos · {len(recorte.afirmacoes)} afirmações")

    if not recorte.afirmacoes:
        print("\n  Nada no acervo nesta janela. Rode a coleta e a extração,\n"
              "  ou amplie com --horas.")
        return

    total = len(recorte.confirmados) + len(recorte.unicos)
    print(f"  {total} fatos distintos · {len(recorte.confirmados)} confirmados "
          f"por 2+ veículos ({recorte.taxa_confirmacao:.0%})")

    if recorte.confirmados:
        print("\n" + "=" * 78)
        print("CONFIRMADO POR FONTES INDEPENDENTES")
        print("=" * 78)
        for c in recorte.confirmados[:20]:
            print(f"\n  {len(c.veiculos)} veículos · {_rotulo(c)}")
            for a in sorted(c.afirmacoes, key=lambda x: x.veiculo):
                print(f"      [{a.veiculo}]{_valor(a)}  {a.titulo[:52]}")

    if recorte.divergentes:
        print("\n" + "=" * 78)
        print("NÚMEROS QUE NÃO BATEM")
        print("=" * 78)
        print("  Nenhum destes é 'a versão certa'. São afirmações incompatíveis\n"
              "  sobre a mesma medida, e o sistema não tem como arbitrar.")
        for c in recorte.divergentes:
            print(f"\n  {_rotulo(c)}")
            for unidade, grupo in c.divergencias:
                veics = {a.veiculo for a in grupo}
                onde = ("entre veículos" if len(veics) > 1
                        else f"dentro de {list(veics)[0]}")
                print(f"      em {unidade} · {onde}")
                for a in sorted(grupo, key=lambda x: x.valor):
                    print(f"        {a.valor:>18,.2f}  [{a.veiculo}] "
                          f"{a.titulo[:40]}")

    # Seção separada de propósito, e nunca misturada à de cima. Ver a regra no
    # topo do módulo: o critério do AC1 é zero itens entregues sem confirmação,
    # e a separação precisa ser de estrutura, não de aviso em texto.
    if recorte.unicos:
        print("\n" + "=" * 78)
        print("APURADO POR UM VEÍCULO SÓ — NÃO CONFIRMADO")
        print("=" * 78)
        print("  Sem confirmação independente. Pode estar certo; o acervo não\n"
              "  tem como dizer.")
        for c in recorte.unicos[:12]:
            veiculo = c.afirmacoes[0].veiculo
            print(f"    [{veiculo}] {_rotulo(c)}{_valor(c.afirmacoes[0])}")
        if len(recorte.unicos) > 12:
            print(f"    ... e mais {len(recorte.unicos) - 12}")

    print("\n" + "=" * 78)
    print("Sem custo: nenhuma chamada de modelo. Tudo já estava extraído.")


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Digest do acervo: o que se sustenta na janela.")
    parser.add_argument("--horas", type=int, default=24,
                        help="janela, em horas de publicação (padrão: 24)")
    parser.add_argument("--topicos", default="",
                        help="lista separada por vírgula; filtra por sujeito, "
                             "objeto e título")
    args = parser.parse_args()

    desde = (datetime.now(timezone.utc)
             - timedelta(hours=args.horas)).isoformat()
    topicos = [t.strip() for t in args.topicos.split(",") if t.strip()]

    conexao = conecta(config.BANCO)
    afirmacoes = grafo.carrega(conexao, desde=desde)
    conexao.close()

    imprime(recorta(filtra_topicos(afirmacoes, topicos)), args.horas, topicos)


if __name__ == "__main__":
    main()
