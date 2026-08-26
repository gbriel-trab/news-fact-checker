"""Comparação entre duas extrações da mesma matéria.

    python -m src.compare              # lista o que dá para comparar
    python -m src.compare <artigo_id>  # compara as versões dessa matéria

Existe porque o prompt, o filtro e o esforço mudam, e a única forma honesta de
saber se uma mudança melhorou alguma coisa é olhar a mesma matéria antes e
depois. Sem isso a avaliação vira impressão — e impressão sobre extração de
triplas erra, como já errou nesta base.

Compara por chave semântica, não por texto: duas triplas são "a mesma" quando
sujeito canônico, relação e objeto canônico coincidem. Assim uma mudança de
data ou de marcação aparece como alteração, não como uma tripla que sumiu e
outra que nasceu.
"""

import sqlite3
import sys

from . import config
from .storage import conecta


def _chave(linha: sqlite3.Row) -> tuple[str, str, str]:
    return (linha["sujeito_canonico"], linha["relacao"],
            linha["objeto_canonico"] or "—")


def _resumo(linha: sqlite3.Row) -> str:
    partes = [f"{linha['tipo_relacao']}", linha["origem"],
              f"fato {linha['data_fato']}"]
    if linha["valor_numero"] is not None:
        valor = f"{linha['valor_numero']:g} {linha['valor_unidade'] or ''}".strip()
        partes.append(f"valor {valor}")
    return " · ".join(partes)


def extracoes_de(conexao: sqlite3.Connection, artigo_id: int) -> list[sqlite3.Row]:
    return conexao.execute(
        """
        SELECT e.*, (SELECT COUNT(*) FROM triplas t WHERE t.extracao_id = e.id) AS n
        FROM extracoes e WHERE e.artigo_id = ? ORDER BY e.extraido_em
        """,
        (artigo_id,),
    ).fetchall()


def triplas_de(conexao: sqlite3.Connection, extracao_id: int) -> list[sqlite3.Row]:
    return conexao.execute(
        "SELECT * FROM triplas WHERE extracao_id = ? ORDER BY sentenca",
        (extracao_id,),
    ).fetchall()


def _lista(conexao: sqlite3.Connection) -> None:
    linhas = conexao.execute(
        """
        SELECT a.id, a.veiculo, a.titulo, COUNT(e.id) AS versoes
        FROM artigos a JOIN extracoes e ON e.artigo_id = a.id
        GROUP BY a.id HAVING COUNT(e.id) > 1
        ORDER BY versoes DESC
        """
    ).fetchall()

    if not linhas:
        print("Nenhuma matéria foi extraída mais de uma vez.")
        print("Comparação exige duas versões — mude o prompt e extraia de novo.")
        return

    print("Matérias com mais de uma extração:\n")
    for linha in linhas:
        print(f"  id {linha['id']:<5} {linha['versoes']} versões  "
              f"[{linha['veiculo']}] {linha['titulo'][:56]}")
    print(f"\n  python -m src.compare {linhas[0]['id']}")


def compara(conexao: sqlite3.Connection, artigo_id: int) -> None:
    versoes = extracoes_de(conexao, artigo_id)
    if len(versoes) < 2:
        print(f"Matéria {artigo_id} tem {len(versoes)} extração. "
              "São necessárias duas.")
        return

    antes, depois = versoes[-2], versoes[-1]
    titulo = conexao.execute(
        "SELECT titulo FROM artigos WHERE id = ?", (artigo_id,)).fetchone()["titulo"]

    print(f"{titulo}\n")
    print(f"{'':<10} {'versão':<14} {'triplas':>8} {'saída':>8} {'custo':>10}")
    for rotulo, e in (("antes", antes), ("depois", depois)):
        print(f"{rotulo:<10} {e['prompt_versao']:<14} {e['n']:>8} "
              f"{e['tokens_saida']:>8} {'US$ ' + format(e['custo_usd'], '.4f'):>10}")

    delta_custo = depois["custo_usd"] - antes["custo_usd"]
    pct = 100 * delta_custo / antes["custo_usd"] if antes["custo_usd"] else 0
    print(f"{'variação':<10} {'':<14} {depois['n'] - antes['n']:>+8} "
          f"{depois['tokens_saida'] - antes['tokens_saida']:>+8} "
          f"{format(pct, '+.0f') + '%':>10}")

    a = {_chave(t): t for t in triplas_de(conexao, antes["id"])}
    d = {_chave(t): t for t in triplas_de(conexao, depois["id"])}

    for rotulo, chaves in (("SÓ NA ANTIGA", a.keys() - d.keys()),
                           ("SÓ NA NOVA", d.keys() - a.keys())):
        if not chaves:
            continue
        print(f"\n=== {rotulo} ({len(chaves)}) ===")
        origem = a if rotulo == "SÓ NA ANTIGA" else d
        for k in sorted(chaves):
            print(f"  ({k[0]}, {k[1]}, {k[2]})")
            print(f"      {_resumo(origem[k])}")

    mudadas = [
        k for k in a.keys() & d.keys()
        if _resumo(a[k]) != _resumo(d[k])
    ]
    if mudadas:
        print(f"\n=== MESMA TRIPLA, MARCAÇÃO DIFERENTE ({len(mudadas)}) ===")
        for k in sorted(mudadas):
            print(f"  ({k[0]}, {k[1]}, {k[2]})")
            print(f"      antes:  {_resumo(a[k])}")
            print(f"      depois: {_resumo(d[k])}")

    iguais = len(a.keys() & d.keys()) - len(mudadas)
    print(f"\n{iguais} triplas idênticas nas duas versões.")


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    conexao = conecta(config.BANCO)
    if len(sys.argv) > 1:
        compara(conexao, int(sys.argv[1]))
    else:
        _lista(conexao)
    conexao.close()


if __name__ == "__main__":
    main()
