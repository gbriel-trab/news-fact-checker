"""Apelidos de entidade minerados do acervo — o veículo escreveu, não o modelo.

    python -m src.apelidos                 # lista os candidatos, ordenados
    python -m src.apelidos --ambiguos      # mostra o que o teste recusou
    python -m src.apelidos --promover      # grava os aprovados em apelidos.json
    python -m src.apelidos --promover --so lula,trump

O problema: o separador manda o nome COMO O TEXTO ESCREVE — "André", "Lula",
"Trump" —, e a rota por chave exata do check compara `chave_canonica` dos dois
lados. "lula" não é "luiz inacio lula da silva", então a rota morre e só a
busca semântica recupera. Completar o nome resolveria, e é justamente o que a
regra 8 proíbe: seria o modelo fornecendo um dado que nenhuma fonte disse.

A saída é a distinção entre FORNECER e PROPOR. Quando o G1 escreve "o
presidente Luiz Inácio Lula da Silva (Lula) disse", a equivalência está no
texto do veículo, e a extração já a registrou: a tripla guarda `sujeito` (como
apareceu) e `sujeito_canonico` (a forma canônica). Minerar esses pares não é
perguntar ao modelo quem é Lula — é ler o que o jornal escreveu.

Três peneiras, e a segunda foi de graça porque o projeto já a tinha:

1. Só `EXTRACTED`. A regra 1 do prompt de extração diz que resolver a quem um
   apelido se refere é DEDUÇÃO e tem de sair como `INFERRED`, "mesmo quando é
   óbvio". Então o próprio extrator já separou o que leu do que deduziu, e
   aqui só entra o que ele afirmou ter lido. Medido em 03/09/2026: 407 formas
   com par EXTRACTED contra 67 com par INFERRED.

2. UNICIDADE. Uma forma curta que aponta para mais de uma entidade não é
   apelido, é ambiguidade — e é exatamente onde uma fusão automática estragaria
   o acervo. O teste recusou 14 das 407 sem nenhuma regra escrita à mão: "a
   proposta" apontava para três projetos de lei distintos, "pesquisa quaest"
   para duas pesquisas, "a transação" para dois negócios.

3. DOIS VEÍCULOS. Mesmo critério de corroboração do resto do sistema: par que
   só uma redação escreve pode ser hábito de casa, não equivalência.

4. FORMA DE APELIDO. Sigla, ou tokens de um cabendo nos do outro sem que o
   que sobra troque o referente. As três primeiras peneiras deixaram passar,
   com SEIS veículos cada, "presidente dos estados unidos" → "estados unidos"
   e "o perfil de nicolas maduro" → "nicolas maduro" — corroboração não
   protege de erro de categoria, porque todos os veículos escrevem assim.

E o que passa nas três ainda não entra sozinho — vai para uma lista que o dono
do projeto promove, como o vocabulário de relações e pelo mesmo motivo (os
pares-ouro contaminados do agrupamento estão no ARCHITECTURE como a lição de
não deixar o sistema calibrar a si mesmo).

DOIS NÍVEIS, e a diferença importa:

* PROMOVIDO (apelidos.json, aprovado por humano) entra em `canonico.APELIDOS`,
  vale em todo lugar que compara entidade — grafo, digest, rota por chave, e o
  freio de alinhamento do juiz.
* FUNDADO MAS NÃO PROMOVIDO (`mapa_vivo`) só AMPLIA A RECUPERAÇÃO: o check
  procura também pelo nome equivalente, e o que encontra passa pelo juiz e
  pelo freio como qualquer outra candidata. Nunca muda um veredito por conta
  própria, nunca entra na comparação de sujeito. Propor mais para olhar é
  barato; decidir identidade é que exige assinatura.
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from . import config
from .canonico import ARQUIVO_APELIDOS, _normaliza

MIN_VEICULOS = 2
"""Veículos independentes que precisam ter escrito o par. Duas editorias da
mesma redação não são duas fontes — mesma regra do agrupamento."""

_GENERICAS = frozenset(
    "proposta transacao medida acordo decisao projeto lei pesquisa reuniao "
    "encontro sessao caso grupo empresa governo banco partido programa taxa "
    "operacao processo comissao conselho pais estado cidade texto documento "
    "relatorio balanco eleicoes candidato ministro presidente".split())
"""Forma curta feita só de substantivo comum não é apelido de ninguém. A
unicidade pega a maioria; isto pega a que sobrevive por acaso (uma única
entidade genérica no acervo hoje não a torna unívoca amanhã)."""


_CABECAS = frozenset(
    "presidente ministro senador deputado governador prefeito juiz relator "
    "perfil campanha governo gabinete equipe assessoria diretoria chapa "
    "conta contas publicacao declaracao manutencao renuncia nomeacao "
    "indicacao aprovacao rejeicao votacao julgamento reuniao encontro "
    "sessao telefonema entrevista discurso post pagina filho filha esposa "
    "familia advogado porta-voz sede".split())
"""Palavra que, sobrando de um lado, muda o REFERENTE em vez de encurtá-lo.

Sem esta peneira a mineração de 03/09/2026 promovia, com 6 veículos cada,
"presidente dos estados unidos" → "estados unidos" e "o perfil de nicolas
maduro" → "nicolas maduro". É a contenção que o `canonico.py` já recusava
resolver por regra ("Braskem" ⊂ "Braskem Idesa"): o cargo não é o país, o
perfil não é a pessoa, a renúncia não é o cargo."""

_VAZIAS = frozenset("de do da dos das e o a os as em no na para com por "
                    "ao aos pelo pela sob sobre".split())


def _so_generica(forma: str) -> bool:
    tokens = [t for t in forma.split() if len(t) > 2]
    return not tokens or all(t in _GENERICAS for t in tokens)


def _tokens(forma: str) -> list[str]:
    import re as _re
    limpo = _re.sub(r"[^\w\s]", " ", forma)
    return [t for t in limpo.split() if t not in _VAZIAS]


def _e_sigla(curta: str, longa: str) -> bool:
    """"mdb" para "movimento democratico brasileiro": uma palavra só, e as
    letras são as iniciais das palavras cheias da forma longa."""
    tc = _tokens(curta)
    if len(tc) != 1 or not (2 <= len(tc[0]) <= 6):
        return False
    return tc[0] == "".join(t[0] for t in _tokens(longa))


def forma_de_apelido(curta: str, longa: str) -> bool:
    """O par tem FORMA de apelido, ou é outra entidade com nome parecido?

    Duas formas aceitas, e nenhuma delas é "parece perto": ou uma é sigla da
    outra, ou os tokens de uma cabem nos da outra — e, nesse caso, o que
    sobra do lado maior não pode ser cabeça que troca o referente. É a mesma
    guarda do `check.sujeito_casa`, aqui aplicada na promoção em vez de na
    comparação: promover um par errado contamina o acervo inteiro, então a
    peneira mora nos dois lugares."""
    if _e_sigla(curta, longa) or _e_sigla(longa, curta):
        return True
    tc, tl = set(_tokens(curta)), set(_tokens(longa))
    if not tc or not tl or not (tc <= tl or tl <= tc):
        return False
    extras = (tl - tc) if tc <= tl else (tc - tl)
    return not (extras & _CABECAS)


def pares(conexao: sqlite3.Connection) -> dict[str, dict[str, set[str]]]:
    """{forma curta: {forma canônica: veículos que escreveram}}, cru.

    Sujeito e objeto entram: a mesma entidade aparece dos dois lados da
    tripla, e o par vale igual."""
    achados: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set))
    consulta = """
        SELECT t.{campo} AS forma, t.{campo}_canonico AS canonico, a.veiculo
        FROM triplas t
        JOIN extracoes e ON e.id = t.extracao_id
        JOIN artigos a ON a.id = e.artigo_id
        WHERE t.origem = 'EXTRACTED'
          AND t.{campo} IS NOT NULL AND t.{campo}_canonico IS NOT NULL
    """
    for campo in ("sujeito", "objeto"):
        for linha in conexao.execute(consulta.format(campo=campo)):
            curta = _normaliza(linha["forma"])
            longa = _normaliza(linha["canonico"])
            if curta and longa and curta != longa:
                achados[curta][longa].add(linha["veiculo"])
    return achados


def candidatos(conexao: sqlite3.Connection
               ) -> tuple[list[tuple[str, str, int, int]], list[tuple[str, dict]]]:
    """(aprovados, ambíguos). Aprovado é (curta, longa, veículos, ocorrências),
    ordenado por evidência: veículos primeiro, depois frequência."""
    aprovados, ambiguos = [], []
    for curta, alvos in pares(conexao).items():
        if len(alvos) > 1:
            ambiguos.append((curta, {k: len(v) for k, v in alvos.items()}))
            continue
        longa, veiculos = next(iter(alvos.items()))
        if (len(veiculos) < MIN_VEICULOS or _so_generica(curta)
                or not forma_de_apelido(curta, longa)):
            continue
        aprovados.append((curta, longa, len(veiculos), 0))
    ocorrencias = _conta_ocorrencias(conexao, {c for c, *_ in aprovados})
    aprovados = [(c, l, v, ocorrencias.get(c, 0)) for c, l, v, _ in aprovados]
    aprovados.sort(key=lambda x: (-x[2], -x[3], x[0]))
    return aprovados, sorted(ambiguos)


def autonomas(conexao: sqlite3.Connection) -> set[str]:
    """Formas que ALGUM veículo tratou como entidade inteira — isto é, que
    aparecem como `sujeito_canonico`/`objeto_canonico` delas mesmas.

    Serve de alerta na promoção: "justiça federal" é candidata a virar
    "justiça federal do distrito federal", mas outras matérias usam "justiça
    federal" como a entidade em si. Promover funde a instituição com uma
    seccional dela. O dado avisa; quem decide é o dono."""
    saida = set()
    for campo in ("sujeito_canonico", "objeto_canonico"):
        for (valor,) in conexao.execute(
                f"SELECT DISTINCT {campo} FROM triplas "
                f"WHERE {campo} IS NOT NULL"):
            saida.add(_normaliza(valor))
    return saida


def _conta_ocorrencias(conexao: sqlite3.Connection,
                       formas: set[str]) -> dict[str, int]:
    contagem: dict[str, int] = defaultdict(int)
    for campo in ("sujeito", "objeto"):
        for (valor,) in conexao.execute(
                f"SELECT {campo} FROM triplas WHERE {campo} IS NOT NULL"):
            n = _normaliza(valor)
            if n in formas:
                contagem[n] += 1
    return contagem


def _resolve_cadeias(mapa: dict[str, str]) -> dict[str, str]:
    """A → B e B → C viraria A → B, e A nunca casaria com C. Segue até o fim,
    e para em ciclo em vez de girar."""
    resolvido = {}
    for origem in mapa:
        visto, atual = {origem}, mapa[origem]
        while atual in mapa and atual not in visto:
            visto.add(atual)
            atual = mapa[atual]
        resolvido[origem] = atual
    return resolvido


@lru_cache(maxsize=1)
def _mapa_vivo_cache(caminho: str, marca: float) -> dict[str, str]:
    conexao = sqlite3.connect(caminho)
    conexao.row_factory = sqlite3.Row
    try:
        aprovados, _ = candidatos(conexao)
        return _resolve_cadeias({c: l for c, l, *_ in aprovados})
    finally:
        conexao.close()


def mapa_vivo(conexao: sqlite3.Connection) -> dict[str, str]:
    """Os apelidos FUNDADOS no acervo e ainda não promovidos.

    Só amplia recuperação (ver o cabeçalho do módulo). Em cache por processo,
    invalidado quando o arquivo do banco muda — uma extração nova pode fundar
    um apelido novo, e a rodada seguinte deve enxergá-lo."""
    caminho = str(config.BANCO)
    try:
        marca = Path(caminho).stat().st_mtime
    except OSError:
        marca = 0.0
    promovidos = carrega_promovidos()
    return {c: l for c, l in _mapa_vivo_cache(caminho, marca).items()
            if c not in promovidos}


def equivalentes(nome: str, mapa: dict[str, str]) -> set[str]:
    """As chaves por que procurar este nome: ele mesmo, o alvo do apelido, e
    quem aponta para ele. "lula" acha "luiz inacio lula da silva" e
    vice-versa."""
    n = _normaliza(nome)
    saida = {n}
    if n in mapa:
        saida.add(mapa[n])
    saida.update(c for c, l in mapa.items() if l == n)
    return saida


def carrega_promovidos() -> dict[str, str]:
    if not ARQUIVO_APELIDOS.exists():
        return {}
    with open(ARQUIVO_APELIDOS, encoding="utf-8-sig") as arquivo:
        return json.load(arquivo).get("apelidos", {})


def promove(novos: dict[str, str], por: str) -> int:
    """Grava os apelidos aprovados. Devolve quantos entraram agora."""
    atual = carrega_promovidos()
    entraram = {c: l for c, l in novos.items() if c not in atual}
    atual.update(entraram)
    with open(ARQUIVO_APELIDOS, "w", encoding="utf-8") as arquivo:
        json.dump({"promovido_por": por,
                   "apelidos": dict(sorted(_resolve_cadeias(atual).items()))},
                  arquivo, ensure_ascii=False, indent=2)
        arquivo.write("\n")
    return len(entraram)


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Apelidos de entidade minerados do acervo.")
    parser.add_argument("--ambiguos", action="store_true",
                        help="mostra o que o teste de unicidade recusou")
    parser.add_argument("--promover", action="store_true",
                        help="grava os candidatos em apelidos.json")
    parser.add_argument("--so", help="promove só estas formas curtas")
    parser.add_argument("--por", default="gabriel",
                        help="quem promoveu (vai no arquivo)")
    args = parser.parse_args()

    from .storage import conecta
    conexao = conecta(config.BANCO)
    try:
        aprovados, ambiguos = candidatos(conexao)
        sozinhas = autonomas(conexao)
    finally:
        conexao.close()
    promovidos = carrega_promovidos()

    if args.ambiguos:
        print(f"{len(ambiguos)} forma(s) recusada(s) pelo teste de unicidade "
              f"— apontam para mais de uma entidade:\n")
        for curta, alvos in ambiguos:
            destinos = " | ".join(f"{a} ({n}x)" for a, n in
                                  sorted(alvos.items(), key=lambda x: -x[1]))
            print(f'  "{curta}"\n      {destinos}')
        return

    novos = [(c, l, v, o) for c, l, v, o in aprovados if c not in promovidos]
    print(f"{len(aprovados)} candidato(s) passam nas quatro peneiras "
          f"(EXTRACTED, unívoco, {MIN_VEICULOS}+ veículos, forma de apelido) · "
          f"{len(promovidos)} já promovido(s) · {len(ambiguos)} ambíguo(s) "
          f"recusado(s)\n")
    marcados = 0
    for curta, longa, veiculos, ocorr in novos:
        alerta = "  ⚠ a forma curta também é entidade própria no acervo" \
            if curta in sozinhas else ""
        marcados += bool(alerta)
        print(f"  {veiculos} veíc · {ocorr:>3}x   \"{curta}\"  ->  "
              f"\"{longa}\"{alerta}")

    if marcados:
        print(f"\n{marcados} par(es) com ⚠: promover funde duas entidades que "
              f"o acervo trata como distintas. Olhe um a um.")
    if not args.promover:
        print("\nNada gravado. Reveja a lista e rode com --promover "
              "(ou --promover --so a,b) para entrar em apelidos.json.")
        return

    querido = ({s.strip() for s in args.so.split(",") if s.strip()}
               if args.so else None)
    escolhidos = {c: l for c, l, *_ in novos
                  if querido is None or c in querido}
    if querido:
        faltam = querido - {c for c, *_ in novos}
        if faltam:
            raise SystemExit(f"forma(s) não candidata(s): {sorted(faltam)}")
    n = promove(escolhidos, args.por)
    print(f"\n{n} apelido(s) promovido(s) em {ARQUIVO_APELIDOS}.")
    print("Eles passam a valer na comparação de entidade em todo o sistema — "
          "rode o gabarito do check antes de confiar no que mudou.")


if __name__ == "__main__":
    main()
