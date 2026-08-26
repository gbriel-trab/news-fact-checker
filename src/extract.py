"""Extração de afirmações como triplas.

Primeira chamada de LLM do projeto. Recebe uma matéria segmentada em sentenças
e devolve as afirmações que ela faz, estruturadas.

    python -m src.extract --dry-run -n 5     # mostra o que seria enviado
    python -m src.extract -n 5               # roda de verdade (exige chave)

Nesta primeira passada a **relação é texto livre**, de propósito. O documento
manda derivar o vocabulário de dado real em vez de inventá-lo no papel: roda-se
solto, olha-se o que a realidade produziu, e só então a lista fechada é escrita
e imposta como enum. O resto do schema já é estrito.
"""

import argparse
import json
import sqlite3
import sys
from typing import Literal

from pydantic import BaseModel, Field

from . import config
from .segment import em_sentencas

VOCAB_VERSAO = 0
"""Versão do vocabulário de relações. Zero significa "ainda livre, não fechado"."""

MAX_TRIPLAS: int | None = None
"""Teto de triplas por matéria, ou None para não limitar.

None durante a fase de medição. O teto existe para conter o custo de saída —
que é a parte cara e a única sem cache —, mas aplicá-lo antes de olhar o dado
cortaria exatamente a cauda que precisa ser inspecionada: é ela que revela
quais relações a realidade produz e onde o corte deveria ficar.

Repor depois de medir a distribuição real de triplas por matéria.
"""

_LINHA_TETO = (
    f"- No máximo {MAX_TRIPLAS} por matéria, priorizando as centrais"
    if MAX_TRIPLAS
    else "- Todas as que a matéria fizer. Não limite a quantidade"
)


class Tripla(BaseModel):
    """Uma afirmação feita pela matéria."""

    sujeito: str = Field(description="Entidade como apareceu no texto.")
    sujeito_canonico: str = Field(
        description=(
            "Nome canônico e completo da entidade, sem cargo nem artigo. "
            "'o presidente Lula' e 'Luiz Inácio Lula da Silva' devem produzir "
            "o mesmo valor aqui."
        )
    )
    relacao: str = Field(
        description=(
            "Verbo da afirmação, no passado, minúsculo, sem espaços "
            "(use_underscore). Genérico o bastante para que fontes diferentes "
            "descrevendo o mesmo fato cheguem ao mesmo valor."
        )
    )
    objeto: str = Field(description="Segunda entidade como apareceu no texto.")
    objeto_canonico: str = Field(description="Nome canônico da segunda entidade.")
    tipo_relacao: Literal["evento", "estado"] = Field(
        description=(
            "'evento' se afirma algo ocorrido num instante (comprou, anunciou, "
            "votou) — permanece verdadeiro para sempre. 'estado' se afirma algo "
            "sobre um intervalo (possui, preside, integra) — pode deixar de valer."
        )
    )
    origem: Literal["EXTRACTED", "INFERRED"] = Field(
        description=(
            "EXTRACTED se a afirmação está explícita no texto. INFERRED se você "
            "a deduziu combinando informações. Na dúvida, INFERRED."
        )
    )
    data_fato: str | None = Field(
        description=(
            "Quando o fato ocorreu, em AAAA-MM-DD, ou AAAA-MM / AAAA se o texto "
            "só der o mês ou o ano. Resolva referências relativas ('ontem', "
            "'nesta terça') usando a data de publicação informada. null se o "
            "texto não permitir determinar."
        )
    )
    sentenca: int = Field(
        description="Índice da sentença numerada de onde a afirmação saiu."
    )


class Extracao(BaseModel):
    triplas: list[Tripla]


INSTRUCOES = f"""\
Você extrai afirmações verificáveis de matérias jornalísticas em português e as
estrutura como triplas (sujeito, relação, objeto).

O que extrair:
- Afirmações factuais que poderiam ser confirmadas ou desmentidas por outra fonte
{_LINHA_TETO}

O que NÃO extrair:
- Opinião, análise, previsão, hipótese e pergunta
- Afirmação sem as duas entidades identificáveis
- Detalhe circunstancial que ninguém contestaria

Regras que importam mais que as outras:

1. ORIGEM. EXTRACTED é o que o texto afirma explicitamente. INFERRED é o que
   você deduziu. Distinguir os dois é o ponto central deste sistema — marcar
   dedução como EXTRACTED corrompe o resultado em silêncio. Na dúvida, INFERRED.

2. ENTIDADE CANÔNICA. Fontes diferentes chamam a mesma entidade de formas
   diferentes. O campo canônico precisa convergir: se duas matérias falam da
   mesma pessoa ou instituição, os valores canônicos têm que ser idênticos,
   caractere por caractere. Use o nome completo e oficial, sem cargo e sem
   artigo. Não invente hierarquia: se o texto diz "Ministério da Saúde", o
   canônico é o ministério, nunca "governo federal".

3. RELAÇÃO GENÉRICA. Escolha o verbo mais comum que descreva o fato. Se três
   veículos noticiam a mesma compra usando "comprou", "adquiriu" e "fechou
   acordo", os três precisam chegar a comprou. Prefira o verbo simples.

4. DATA DO FATO. É quando o fato ocorreu, não quando a matéria foi publicada.
   Elas divergem quando a matéria trata de algo antigo, e essa divergência é
   justamente o que o sistema precisa enxergar.

5. ATRIBUIÇÃO. Para "Fulano afirmou que Z", extraia (Fulano, afirmou, <resumo
   curto de Z>) e marque EXTRACTED. Não trate Z como fato do mundo — o
   verificável ali é que Fulano disse, não que Z seja verdade.

Exemplo:

  Matéria publicada em 2026-08-20, sentenças numeradas:
    [0] A Vale confirmou nesta quarta-feira a compra da mineradora Ferrous por
        R$ 3 bilhões.
    [1] O negócio, negociado desde 2024, ainda depende de aval do Cade.
    [2] Para analistas, o preço foi salgado.

  Saída:
    (Vale S.A., comprou, Ferrous Resources) evento, EXTRACTED, 2026-08-19, sent 0
    (Vale S.A., aguarda_aprovacao_de, Conselho Administrativo de Defesa
     Econômica) estado, EXTRACTED, 2026-08-20, sent 1

  A sentença [2] não gerou tripla: é opinião de terceiros, não é verificável.
  Repare que "nesta quarta-feira" virou a data real, e que o Cade foi expandido
  para o nome oficial no campo canônico.
"""


def monta_conteudo(titulo: str, veiculo: str, data_pub: str | None,
                   sentencas: list[str]) -> str:
    """Monta a parte variável da requisição — a que não é cacheável."""
    numeradas = "\n".join(f"[{i}] {s}" for i, s in enumerate(sentencas))
    return (
        f"Veículo: {veiculo}\n"
        f"Data de publicação: {data_pub or 'desconhecida'}\n"
        f"Título: {titulo}\n\n"
        f"Sentenças:\n{numeradas}"
    )


def extrai(cliente, titulo: str, veiculo: str, data_pub: str | None,
           sentencas: list[str]) -> Extracao:
    """Chama o modelo e devolve as triplas validadas contra o schema."""
    resposta = cliente.messages.parse(
        model="claude-opus-5",
        max_tokens=8000,
        # O bloco de instruções é idêntico em toda chamada, então vai marcado
        # para cache. ATENÇÃO: o prefixo mínimo cacheável é ~1024 tokens, e
        # hoje este bloco tem ~625 — ou seja, a marcação ainda não faz efeito
        # e a falha é silenciosa. Ela passa a valer quando o vocabulário
        # fechado e mais exemplos entrarem aqui. Conferir em
        # `usage.cache_read_input_tokens` antes de contar com a economia.
        system=[{
            "type": "text",
            "text": INSTRUCOES,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": monta_conteudo(titulo, veiculo, data_pub, sentencas),
        }],
        output_format=Extracao,
    )
    return resposta.parsed_output


# ---------------------------------------------------------------- interface

def _materias(limite: int) -> list[sqlite3.Row]:
    """Pega matérias com texto suficiente para sustentar extração.

    Veículo que só publica manchete no RSS não entra: 200 caracteres não dão
    tripla, e a chamada seria desperdício.
    """
    conexao = sqlite3.connect(config.BANCO)
    conexao.row_factory = sqlite3.Row
    linhas = conexao.execute(
        """
        SELECT veiculo, editoria, titulo, resumo, conteudo, data_publicacao, url_norm
        FROM artigos
        WHERE MAX(LENGTH(conteudo), LENGTH(resumo)) > 1200
        ORDER BY data_publicacao DESC
        LIMIT ?
        """,
        (limite,),
    ).fetchall()
    conexao.close()
    return linhas


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Extrai triplas do acervo.")
    parser.add_argument("-n", type=int, default=5, help="quantas matérias")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="mostra a requisição que seria enviada, sem chamar a API",
    )
    args = parser.parse_args()

    linhas = _materias(args.n)
    if not linhas:
        print("Nenhuma matéria com texto suficiente. Rode a coleta primeiro.")
        sys.exit(1)

    cliente = None
    if not args.dry_run:
        import anthropic

        cliente = anthropic.Anthropic()

    for i, linha in enumerate(linhas, 1):
        texto = max(linha["conteudo"], linha["resumo"], key=len)
        sentencas = em_sentencas(texto)

        print(f"\n{'=' * 78}")
        print(f"[{i}/{len(linhas)}] {linha['veiculo']} / {linha['editoria']}")
        print(f"  {linha['titulo'][:70]}")
        print(f"  {len(texto)} caracteres → {len(sentencas)} sentenças")

        if args.dry_run:
            conteudo = monta_conteudo(
                linha["titulo"], linha["veiculo"],
                linha["data_publicacao"], sentencas,
            )
            entrada = len(INSTRUCOES) + len(conteudo)
            print(f"  ~{entrada // 4} tokens de entrada "
                  f"({len(INSTRUCOES) // 4} cacheáveis)")
            if i == 1:
                print(f"\n--- system (fixo, cacheado) ---\n{INSTRUCOES}")
                print(f"--- user (variável) ---\n{conteudo[:900]}\n[...]")
                print(f"\n--- schema exigido na resposta ---")
                print(json.dumps(Extracao.model_json_schema(), indent=2,
                                 ensure_ascii=False)[:1400])
            continue

        resultado = extrai(
            cliente, linha["titulo"], linha["veiculo"],
            linha["data_publicacao"], sentencas,
        )
        print(f"  {len(resultado.triplas)} triplas\n")
        for t in resultado.triplas:
            marca = " " if t.origem == "EXTRACTED" else "~"
            print(f"  {marca} ({t.sujeito_canonico}, {t.relacao}, {t.objeto_canonico})")
            print(f"      {t.tipo_relacao} · {t.origem} · fato: {t.data_fato}"
                  f" · sent [{t.sentenca}]")

    if args.dry_run:
        print(f"\n{'=' * 78}")
        print("Nada foi enviado. Para rodar de verdade, preencha "
              "ANTHROPIC_API_KEY no .env e remova --dry-run.")


if __name__ == "__main__":
    main()
