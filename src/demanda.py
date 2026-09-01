"""Extração sob demanda: premissa sem cobertura busca no que foi coletado.

    python -m src.demanda "afirmação"            # ciclo completo (paga API)
    python -m src.demanda "afirmação" --dry-run  # só mostra as candidatas

O caso que motivou (01/09/2026): um post pergunta "André se reúne com
Trump, quem manda no Brasil?" — a matéria do G1 sobre a reunião estava
COLETADA havia horas, mas o seletor de extração não a tinha priorizado,
e o check respondia "sem evidência" por cegueira, não por falta de
cobertura. O funil de extração prioriza corroboração (histórias com 2+
veículos); a demanda cobre o resto: extrai na hora exatamente o que a
premissa precisa, e só isso.

O CICLO MORA AQUI, e não no check — de propósito. O check promete no
próprio docstring que nada decide o próximo passo em tempo de execução;
essa promessa fica de pé. Quem decide é este módulo, com regra fixa,
UMA volta só e teto de gasto: é o princípio 7 do ARCHITECTURE em código
(o ciclo serve para tentar outra fonte, não para insistir até inventar).

A PERGUNTA "o acervo cobre?" é respondida pelo CHECK, nunca por proxy:
quem chama roda o check primeiro e só aciona a demanda sobre um veredito
"sem evidência" — e re-verifica com `forcar` depois de extrair. A versão
inicial usava proximidade vetorial como oráculo de cobertura e caiu no
primeiro teste vivo (01/09/2026): "Esteves integra BTG" no índice fingia
cobrir a premissa da REUNIÃO com Trump; proximidade casa entidade, não o
fato — que é literalmente a regra 2 do julgamento do check.

Freios, na ordem em que seguram:

1. Só entra matéria com título+lead a >= LIMIAR_CANDIDATA da premissa,
   dentro da janela de dias, uma por veículo, no máximo MAX_MATERIAS,
   e coerente com a melhor candidata.
2. Matéria já extraída pelas versões ATIVAS de prompt fica fora: ela já
   teve a vez, e re-extração seria moto-perpétuo de gasto. Extração de
   versão antiga não conta — a órfã volta a ser elegível, exatamente
   como no funil de lote.
3. O chamador informa o orçamento restante da rodada; abaixo de
   CUSTO_ESTIMADO a demanda recusa ANTES de chamar a API.
"""

import argparse
import sqlite3
import sys
from dataclasses import dataclass

from . import check, config, extract, indice
from .storage import conecta

LIMIAR_CANDIDATA = 0.60
"""Piso premissa↔título+lead para uma matéria virar candidata.

Mais alto que o MIN_PROXIMIDADE do check (0,55) de propósito: candidata
custa extração PAGA, e falso positivo aqui custa dinheiro, não só posição
de ranking. Ainda sem calibração dedicada — 0,60 é chute honesto na
família dos limiares medidos do projeto, registrado como tal."""

MAX_MATERIAS = 4
"""Uma história basta para cobrir uma premissa; quatro veículos é mais
corroboração do que o check precisa para confirmar."""

TETO_USD = 0.50
"""Teto de gasto de demanda por RODADA do boletim (não por premissa).

A lição do estouro de 31/08 (US$ 4,56 contra teto combinado de 4): teto
que não é código não segura nada. Este nasce junto com a funcionalidade."""

CUSTO_ESTIMADO = 0.15
"""Pior caso medido de uma chamada de história (US$ 0,017-0,12 na estreia
do v3), arredondado para cima. Serve só para decidir se o orçamento
comporta MAIS UMA extração; o custo real vem da fatura da chamada."""


@dataclass(frozen=True, slots=True)
class Resultado:
    """O que uma volta do ciclo fez, e por quê."""

    motivo: str  # "sem_candidata" | "teto" | "extraiu"
    materias: int
    triplas: int
    custo: float


def candidatas(conexao: sqlite3.Connection, texto: str) -> list[sqlite3.Row]:
    """Matérias coletadas, ainda sem extração ATUAL, próximas da premissa.

    Quatro peneiras, na ordem (as três últimas vieram da revisão de
    01/09/2026):

    * Uma por veículo — duas editorias do mesmo veículo não são fontes
      independentes (regra do agrupamento) — na ordem do ranking.
    * Janela de data NA RECUPERAÇÃO, não só na indexação: a coleção
      acumula e o upsert não poda, então sem este corte uma premissa de
      tema recorrente casaria com a matéria do trimestre passado e
      pagaria extração de notícia obsoleta.
    * "Já extraída" conta só extração das VERSÕES ATIVAS de prompt —
      igual ao funil de lote, que se autocura num bump de versão. Sem o
      filtro, matéria órfã de vocabulário antigo (invisível para o grafo)
      ficava bloqueada aqui para sempre: a cegueira que o módulo existe
      para curar.
    * Guarda de coesão ENTRE as candidatas, contra a melhor delas: o
      grupo nasce da proximidade com a PREMISSA, e duas matérias podem
      orbitar a mesma premissa cobrindo fatos diferentes — o lote tem o
      `agrupa` para isso; aqui o carona é expulso antes de pagar, porque
      mesma_historia=false gravaria marcador em TODAS.
    """
    from datetime import datetime, timedelta, timezone

    from . import agrupa

    indice.indexa_artigos(conexao)
    achadas = indice.busca("artigos", texto, quantos=12)
    corte = (datetime.now(timezone.utc)
             - timedelta(days=agrupa.JANELA_DIAS)).isoformat()
    ids = [int(a.meta["artigo_id"]) for a in achadas
           if a.proximidade >= LIMIAR_CANDIDATA
           and str(a.meta.get("data", "")) >= corte]
    if not ids:
        return []
    linhas = {l["id"]: l for l in extract._por_id(conexao, ids)}
    por_veiculo: dict[str, sqlite3.Row] = {}
    for i in ids:
        linha = linhas.get(i)
        if linha is None:
            continue
        extraida = conexao.execute(
            "SELECT COUNT(*) FROM extracoes WHERE artigo_id = ? "
            "AND prompt_versao IN (?, ?)",
            (i, extract.PROMPT_VERSAO,
             extract.PROMPT_VERSAO_HISTORIA)).fetchone()[0]
        if extraida:
            continue
        por_veiculo.setdefault(linha["veiculo"], linha)
    grupo = list(por_veiculo.values())[:MAX_MATERIAS]
    if len(grupo) > 1:
        vetores = indice.vetoriza(
            [agrupa.texto_de_agrupamento(l) for l in grupo])
        base = vetores[0]
        grupo = [l for l, v in zip(grupo, vetores)
                 if float(v @ base) >= agrupa.LIMIAR_COESAO]
    return grupo


def garante(conexao: sqlite3.Connection, texto: str,
            orcamento: float = TETO_USD) -> Resultado:
    """Uma volta do ciclo: cobre a premissa se der, dentro do orçamento.

    Pressupõe que o chamador JÁ verificou e recebeu "sem evidência" — a
    demanda não re-pergunta se o acervo cobre (ver o docstring do módulo).
    Nunca levanta a mão de novo: extraiu ou recusou, o chamador segue para
    o check com o acervo que houver. Falha de API sobe como exceção — quem
    chama decide se ela derruba a rodada (o boletim não deixa).
    """
    grupo = candidatas(conexao, texto)
    if not grupo:
        return Resultado("sem_candidata", 0, 0, 0.0)
    if orcamento < CUSTO_ESTIMADO:
        return Resultado("teto", 0, 0, 0.0)
    triplas, custo, recusada = extract.extrai_grupo(conexao, grupo)
    if recusada and len(grupo) > 1 and orcamento - custo >= CUSTO_ESTIMADO:
        # O modelo recusou o grupo — e pode ter razão: aqui o grupo nasce
        # da proximidade com a premissa, não da coesão do lote. A melhor
        # candidata ainda pode cobrir sozinha; UMA re-tentativa, só com
        # ela, dentro do orçamento. Sem isto, a matéria certa morria
        # queimada junto com o carona (revisão de 01/09/2026).
        t2, c2, _ = extract.extrai_grupo(conexao, grupo[:1])
        triplas += t2
        custo += c2
    if triplas:
        # O check tem duas rotas: a por chave lê o grafo direto do banco,
        # mas a vetorial só enxerga o que o índice tem. Só as matérias do
        # grupo — reindexar o recorte inteiro custava minutos por rodada.
        try:
            indice.indexa_afirmacoes(conexao,
                                     so_artigos=[l["id"] for l in grupo])
        except Exception:  # noqa: BLE001
            # Falha de índice não pode transformar extração PAGA em
            # "demanda falhou": a rota por chave segue funcionando com o
            # acervo recarregado, e a vetorial se cura na reindexação do
            # CLI. Registrar e seguir.
            pass
    return Resultado("extraiu", len(grupo), triplas, custo)


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Extração sob demanda: cobre uma premissa e verifica.")
    parser.add_argument("afirmacao")
    parser.add_argument("--dry-run", action="store_true",
                        help="mostra as candidatas, sem extrair nem verificar")
    args = parser.parse_args()

    conexao = conecta(config.BANCO)
    try:
        if args.dry_run:
            indice.indexa_artigos(conexao)
            achadas = indice.busca("artigos", args.afirmacao, quantos=8)
            print(f'candidatas para: "{args.afirmacao}" '
                  f"(piso {LIMIAR_CANDIDATA:.0%})\n")
            for a in achadas:
                marca = "✓" if a.proximidade >= LIMIAR_CANDIDATA else " "
                print(f"  {marca} {a.proximidade:.0%}  "
                      f"[{a.meta['veiculo']}] {a.meta['titulo'][:64]}")
            print("\nNada foi extraído. Remova --dry-run para rodar.")
            return

        from . import grafo
        acervo = grafo.carrega(conexao)
        if not acervo:
            print("Acervo vazio — rode coleta e extração antes.")
            sys.exit(1)

        # O rito é o do boletim: check primeiro; demanda só sobre
        # "sem evidência"; re-check com forcar depois de extrair.
        check.verifica(args.afirmacao, conexao=conexao, acervo=acervo)
        ultima = check.consulta_recente(conexao, args.afirmacao)
        if ultima is None or ultima["veredito"] != "sem_evidencia":
            return

        r = garante(conexao, args.afirmacao)
        rotulos = {
            "sem_candidata": "nenhuma matéria coletada passa do piso",
            "teto": "orçamento insuficiente para extrair",
            "extraiu": (f"{r.materias} matéria(s) extraída(s), "
                        f"{r.triplas} triplas · US$ {r.custo:.4f}"),
        }
        print(f"\ndemanda: {rotulos[r.motivo]}\n")
        if r.motivo != "extraiu":
            return

        acervo = grafo.carrega(conexao)
        # forcar: sem isso, a janela de reuso de 24h devolvia o
        # "sem evidência" que acabou de motivar a extração.
        check.verifica(args.afirmacao, conexao=conexao, acervo=acervo,
                       forcar=True)
    finally:
        conexao.close()


if __name__ == "__main__":
    main()
