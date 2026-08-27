"""Grafo das afirmações do acervo.

    python -m src.grafo              # números do grafo e o que ele encontra
    python -m src.grafo "Braskem"    # o que o acervo afirma sobre uma entidade

O índice vetorial acha o que é PARECIDO. O grafo responde o que é EXATO:
quantos veículos independentes afirmam a mesma coisa, e quando dois afirmam
coisas incompatíveis.

Os dois são necessários e fazem trabalhos opostos. Casar uma afirmação que
chega de fora exige proximidade; contar corroboração exige identidade — "quase
a mesma entidade" não corrobora nada.

A unidade de corroboração é o VEÍCULO. Duas editorias da mesma redação não são
fontes independentes, e contá-las como duas fabricaria confirmação.
"""

import collections
import sqlite3
import sys
from dataclasses import dataclass

from . import config
from .storage import conecta

TOLERANCIA_RELATIVA = 0.02
"""Diferença relativa abaixo da qual dois números são considerados o mesmo.

Existe porque veículos arredondam de formas diferentes — 10,9 bilhões e
10.900.000.000 são o mesmo fato. Sem tolerância, arredondamento vira
contradição; com tolerância grande demais, contradição vira arredondamento.
"""


@dataclass(frozen=True, slots=True)
class Afirmacao:
    """Uma tripla, com a proveniência que o veredito precisa citar."""

    sujeito: str
    relacao: str
    objeto: str | None
    valor: float | None
    unidade: str | None
    contexto: str | None
    data_fato: str | None
    origem: str
    veiculo: str
    titulo: str
    url: str

    @property
    def chave(self) -> tuple[str, str, str, str]:
        """O que precisa coincidir para duas afirmações serem 'a mesma'.

        O contexto entra sempre que há valor, mesmo havendo objeto. Sem ele,
        "Petrobras detém 47% do capital votante" e "Petrobras detém 36,1% do
        capital total" viram o mesmo fato — mesmo sujeito, mesma relação, mesmo
        objeto — e o sistema os reportaria como dois veículos divergindo sobre
        um número quando são duas medidas diferentes.

        O custo é perder o encontro quando dois veículos descrevem a mesma
        medida com palavras diferentes. Esse caso é do índice vetorial: chave
        exata aqui, proximidade lá.
        """
        return (self.sujeito, self.relacao, self.objeto or "",
                self.contexto or "" if self.valor is not None else "")


@dataclass(frozen=True, slots=True)
class Corroboracao:
    """Um fato e os veículos que o afirmam."""

    chave: tuple[str, str, str]
    afirmacoes: tuple[Afirmacao, ...]

    @property
    def veiculos(self) -> set[str]:
        return {a.veiculo for a in self.afirmacoes}

    @property
    def confirmada(self) -> bool:
        return len(self.veiculos) >= 2

    @property
    def por_unidade(self) -> dict[str, list["Afirmacao"]]:
        """Afirmações com número, separadas por unidade.

        Comparar entre unidades é erro de categoria: 10,9 bilhões de dólares e
        56 bilhões de reais são a MESMA dívida, e sem esta separação o sistema
        os reportava como dois veículos divergindo. Falso positivo, que é o
        erro que o projeto considera o pior.

        Converter moeda resolveria mais casos, mas exige a cotação da data do
        fato — e cotação errada transforma o mesmo número em divergência
        inventada. Sem unidade comum, o sistema não compara.
        """
        grupos: dict[str, list[Afirmacao]] = collections.defaultdict(list)
        for a in self.afirmacoes:
            if a.valor is not None:
                grupos[a.unidade or "?"].append(a)
        return dict(grupos)

    @property
    def divergencias(self) -> list[tuple[str, list["Afirmacao"]]]:
        """Unidades em que os números afirmados não batem entre si."""
        achadas = []
        for unidade, grupo in self.por_unidade.items():
            vs = [a.valor for a in grupo]
            if len(vs) < 2:
                continue
            maior, menor = max(vs), min(vs)
            # Diferença relativa: veículos arredondam diferente, e
            # arredondamento não é contradição.
            fora = (menor != 0) if maior == 0 else (
                abs(maior - menor) / abs(maior) > TOLERANCIA_RELATIVA)
            if fora:
                achadas.append((unidade, grupo))
        return achadas

    @property
    def diverge(self) -> bool:
        return bool(self.divergencias)


def carrega(conexao: sqlite3.Connection) -> list[Afirmacao]:
    """Lê as afirmações do vocabulário mais recente.

    Versões antigas ficam de fora: relações de vocabulários diferentes não são
    comparáveis, e misturá-las produziria corroboração inexistente entre nomes
    que só por acaso coincidem.
    """
    linhas = conexao.execute(
        """
        SELECT t.sujeito_canonico s, t.relacao r, t.objeto_canonico o,
               t.valor_numero vn, t.valor_unidade vu, t.valor_contexto vc,
               t.data_fato df, t.origem og,
               a.veiculo, a.titulo, a.url_norm
        FROM triplas t
        JOIN extracoes e ON e.id = t.extracao_id
        JOIN artigos   a ON a.id = e.artigo_id
        WHERE e.vocab_versao = (SELECT MAX(vocab_versao) FROM extracoes)
        """
    ).fetchall()
    return [
        Afirmacao(l["s"], l["r"], l["o"], l["vn"], l["vu"], l["vc"],
                  l["df"], l["og"], l["veiculo"], l["titulo"], l["url_norm"])
        for l in linhas
    ]


def constroi(afirmacoes: list[Afirmacao]):
    """Monta o grafo dirigido: entidades como nós, afirmações como arestas.

    NetworkX em vez de banco de grafo porque a lógica é a parte difícil, não a
    infraestrutura. Migrar depois é mecânico; um servidor a mais agora, não.
    """
    import networkx as nx

    g = nx.MultiDiGraph()
    for a in afirmacoes:
        g.add_node(a.sujeito)
        alvo = a.objeto or f"[{a.relacao}]"
        g.add_node(alvo)
        g.add_edge(a.sujeito, alvo, relacao=a.relacao, veiculo=a.veiculo,
                   valor=a.valor, data_fato=a.data_fato, origem=a.origem,
                   url=a.url, titulo=a.titulo)
    return g


def agrupa(afirmacoes: list[Afirmacao]) -> list[Corroboracao]:
    """Junta afirmações idênticas em fato, para contar quem afirma o quê."""
    por_chave: dict[tuple, list[Afirmacao]] = collections.defaultdict(list)
    for a in afirmacoes:
        por_chave[a.chave].append(a)
    return [Corroboracao(k, tuple(v)) for k, v in por_chave.items()]


def sobre(afirmacoes: list[Afirmacao], entidade: str) -> list[Afirmacao]:
    """Tudo que o acervo afirma sobre uma entidade, como sujeito ou objeto."""
    alvo = entidade.lower()
    return [
        a for a in afirmacoes
        if alvo in a.sujeito.lower() or (a.objeto and alvo in a.objeto.lower())
    ]


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    conexao = conecta(config.BANCO)
    afirmacoes = carrega(conexao)
    conexao.close()

    if not afirmacoes:
        print("Nenhuma afirmação no vocabulário atual. Rode a extração.")
        return

    if len(sys.argv) > 1:
        alvo = " ".join(sys.argv[1:])
        achadas = sobre(afirmacoes, alvo)
        print(f'{len(achadas)} afirmações sobre "{alvo}"\n')
        for a in sorted(achadas, key=lambda x: x.relacao):
            valor = ""
            if a.valor is not None:
                valor = f"  = {a.valor:g} {a.unidade or ''} ({a.contexto or ''})"
            marca = " " if a.origem == "EXTRACTED" else "~"
            print(f" {marca} ({a.sujeito}, {a.relacao}, {a.objeto or '—'}){valor}")
            print(f"      [{a.veiculo}] fato {a.data_fato}")
        return

    g = constroi(afirmacoes)
    grupos = agrupa(afirmacoes)
    confirmados = [c for c in grupos if c.confirmada]
    divergentes = [c for c in grupos if c.diverge]

    print(f"Grafo: {g.number_of_nodes()} entidades · {g.number_of_edges()} arestas")
    print(f"Fatos distintos: {len(grupos)}")
    print(f"Confirmados por 2+ veículos: {len(confirmados)}")
    print(f"Com divergência numérica: {len(divergentes)}\n")

    if confirmados:
        print("=== CONFIRMADOS POR FONTES INDEPENDENTES ===")
        for c in sorted(confirmados, key=lambda x: -len(x.veiculos))[:12]:
            suj, rel, obj, ctx = c.chave
            rotulo = f"({suj}, {rel}, {obj or '—'})" + (f" · {ctx}" if ctx else "")
            print(f"  {len(c.veiculos)} veículos · {rotulo}")
            for a in c.afirmacoes:
                valor = f" = {a.valor:g} {a.unidade or ''}" if a.valor is not None else ""
                print(f"      [{a.veiculo}]{valor}")

    if divergentes:
        print("\n=== NÚMEROS QUE NÃO BATEM ===")
        for c in divergentes:
            suj, rel, obj, ctx = c.chave
            print(f"  ({suj}, {rel}, {obj or '—'})" + (f" · {ctx}" if ctx else ""))
            for unidade, grupo in c.divergencias:
                veics = {a.veiculo for a in grupo}
                escopo = "entre veículos" if len(veics) > 1 else f"dentro de {list(veics)[0]}"
                print(f"      em {unidade} · {escopo}")
                for a in sorted(grupo, key=lambda x: x.valor):
                    print(f"        {a.valor:>18,.2f}  [{a.veiculo}] {a.titulo[:42]}")


if __name__ == "__main__":
    main()
