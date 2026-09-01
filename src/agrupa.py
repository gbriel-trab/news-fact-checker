"""Agrupamento de matérias que cobrem a mesma história.

    python -m src.agrupa          # lista as histórias com mais veículos

É o filtro de cobertura múltipla do documento, e a seleção do que vale extrair.
Matéria de fonte única não pode ser corroborada por definição — extraí-la
produz triplas que o sistema nunca conseguirá confirmar.

E é pré-requisito da detecção de contradição: para saber que dois veículos
divergem, é preciso primeiro saber que eles falam da mesma coisa.

O agrupamento aqui é léxico — sobreposição de termos significativos no título.
É grosseiro e será substituído por similaridade de embedding, que compara
sentido em vez de palavra. Existe agora porque a decisão que ele sustenta
(quais matérias extrair) precisa ser tomada antes de o índice vetorial existir,
e porque errar aqui custa uma extração a mais, não uma conclusão errada.
"""

import re
import sqlite3
import sys
from dataclasses import dataclass

from . import config
from .storage import conecta

STOPWORDS = frozenset("""
para com uma que dos das nos nas por mais sobre como após até entre seus suas
seu sua pelo pela ser tem são foi era esta este essa esse isso contra desde
ainda onde quando quem qual porque diz dizem afirma sem mesmo apenas ter novo
nova pode podem deve devem vai vão fazer ver veja saiba confira leia
""".split())

MIN_TERMOS_COMUNS = 3
"""Termos significativos que dois títulos precisam compartilhar.

Três é o piso que separou sinal de ruído nesta base. Com dois, títulos sobre
assuntos diferentes que citam a mesma pessoa entram no mesmo grupo.
"""


@dataclass(frozen=True, slots=True)
class Historia:
    """Um conjunto de matérias que aparentam cobrir o mesmo fato."""

    materias: tuple[sqlite3.Row, ...]

    @property
    def veiculos(self) -> set[str]:
        """Redações distintas. É esta contagem que importa, não a de matérias:
        duas editorias do mesmo veículo não são fontes independentes."""
        return {m["veiculo"] for m in self.materias}

    @property
    def corroborada(self) -> bool:
        return len(self.veiculos) >= 2

    @property
    def titulo(self) -> str:
        return self.materias[0]["titulo"]


def _termos(titulo: str) -> frozenset[str]:
    palavras = re.findall(r"[a-zà-ú0-9]{4,}", titulo.lower())
    return frozenset(p for p in palavras if p not in STOPWORDS)


def agrupa(materias: list[sqlite3.Row]) -> list[Historia]:
    """Junta matérias com títulos que compartilham termos suficientes."""
    termos = [_termos(m["titulo"]) for m in materias]
    usadas: set[int] = set()
    historias: list[Historia] = []

    for i, base in enumerate(termos):
        if i in usadas or len(base) < MIN_TERMOS_COMUNS:
            continue
        grupo = [i]
        for j in range(i + 1, len(materias)):
            if j in usadas:
                continue
            if len(base & termos[j]) >= MIN_TERMOS_COMUNS:
                grupo.append(j)
                usadas.add(j)
        if len(grupo) > 1:
            usadas.add(i)
            historias.append(Historia(tuple(materias[k] for k in grupo)))

    return sorted(historias, key=lambda h: (-len(h.veiculos), -len(h.materias)))


JANELA_DIAS = 10
"""Só matéria publicada nos últimos N dias entra no agrupamento.

Sem janela, o agrupamento varre o acervo INTEIRO — e o acervo é permanente.
Evento recorrente ("Copom mantém Selic", "Quaest divulga pesquisa") formaria
par entre edições de meses diferentes: mesmos termos no título, fatos
distintos, extração paga de par falso. Não mordeu na primeira semana de
acervo; morderia com o primeiro Copom repetido. Apontado em revisão externa
de 01/09/2026.

Dez dias, e não menos, pelo custo do outro lado: a promessa "matéria
solitária espera o par" (ARCHITECTURE, filtro de custo) vale DENTRO da
janela — corroboração entre veículos acontece em horas ou dias, e o evento
recorrente mais frequente do acervo (reunião do Copom) tem ciclo de ~45
dias. Dez fica com folga dos dois lados. Matéria fora da janela continua no
acervo e na busca; só deixa de formar par novo."""


def carrega(conexao: sqlite3.Connection,
            janela_dias: int | None = JANELA_DIAS) -> list[sqlite3.Row]:
    filtro = ""
    parametros: tuple = ()
    if janela_dias is not None:
        filtro = ("WHERE data_publicacao >= "
                  "datetime('now', ?) ")
        parametros = (f"-{janela_dias} days",)
    return conexao.execute(
        f"""
        SELECT id, veiculo, editoria, titulo, resumo, conteudo,
               MAX(LENGTH(conteudo), LENGTH(resumo)) AS tamanho,
               (SELECT COUNT(*) FROM extracoes e WHERE e.artigo_id = artigos.id) AS extraida
        FROM artigos
        {filtro}
        ORDER BY data_publicacao DESC
        """,
        parametros,
    ).fetchall()


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    conexao = conecta(config.BANCO)
    materias = carrega(conexao)
    historias = [h for h in agrupa(materias) if h.corroborada]

    envolvidas = sum(len(h.materias) for h in historias)
    com_texto = sum(
        1 for h in historias for m in h.materias if m["tamanho"] > 1200)

    print(f"{len(historias)} histórias com 2+ veículos, "
          f"de {len(materias)} matérias")
    print(f"{envolvidas} matérias envolvidas · {com_texto} com texto "
          f"suficiente para extração\n")

    for h in historias[:15]:
        marca = "".join(
            "*" if m["extraida"] else ("." if m["tamanho"] > 1200 else "-")
            for m in h.materias)
        print(f"  {len(h.veiculos)} veículos {marca:<6} {h.titulo[:62]}")
        for m in h.materias:
            print(f"      id {m['id']:<5} {m['veiculo']:<16} "
                  f"{m['tamanho']:>5}c  {m['titulo'][:46]}")

    print("\n  * já extraída   . tem texto   - só manchete")
    conexao.close()


if __name__ == "__main__":
    main()
