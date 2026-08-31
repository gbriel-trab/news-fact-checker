"""Confere as premissas de um texto que argumenta.

    python -m src.premissas "cole o texto aqui"
    python -m src.premissas < post.txt

O `check.py` recebe UMA afirmação e responde. Este módulo recebe um ARGUMENTO
— análise, comentário, previsão — e separa o que dá para conferir do que não
dá, antes de gastar.

    "O Copom não tem escolha. Com o desemprego em 5,3% e a dívida pública
     acima de R$ 9,2 trilhões, manter a Selic é insustentável. Vai subir."

    premissa   desemprego está em 5,3%              → verificável
    premissa   dívida pública acima de R$ 9,2 tri   → verificável
    previsão   a Selic vai subir                    → não, e nem deve ser
    opinião    o Copom não tem escolha              → não

O `extract.py` descartaria a frase inteira, e com razão: a regra dele diz para
não extrair opinião nem previsão. Correto para notícia, errado para análise —
joga fora os números junto com o palpite.

O QUE ESTE MÓDULO NÃO É, e a distinção decide se ele presta:

    ✗  nota de credibilidade do autor
    ✓  conferência dos números em que o argumento se apoia

Premissa sem evidência significa que o ACERVO não cobre, nunca que o autor
errou. A saída é escrita para não permitir a outra leitura, e mudar isso
transforma a ferramenta em máquina de acusar — que é outro produto.

E previsão não é erro. Um argumento pode ter todas as premissas confirmadas e
a conclusão errada; é assim que análise funciona. O que este módulo detecta é
o contrário: raciocínio impecável partindo de um número que não bate.
"""

import argparse
import sys
from typing import Literal

from pydantic import BaseModel, Field

from . import check, config, grafo, llm
from .storage import conecta


class Premissa(BaseModel):
    """Uma afirmação isolada extraída de um texto argumentativo."""

    tipo: Literal["fato", "previsao", "opiniao"] = Field(
        description=(
            "fato: afirma algo já ocorrido ou um estado presente, que outra "
            "fonte poderia confirmar ou desmentir. "
            "previsao: afirma sobre o futuro. "
            "opiniao: juízo, avaliação ou recomendação."
        )
    )
    afirmacao: str = Field(
        description=(
            "A afirmação reescrita como frase completa e autônoma, que faça "
            "sentido sozinha. Quem lê não terá o texto original ao lado."
        )
    )
    trecho: str = Field(
        description="O pedaço LITERAL do texto de onde ela saiu, sem reescrever."
    )


class Analise(BaseModel):
    premissas: list[Premissa]


INSTRUCOES = """\
Você separa as afirmações de um texto que argumenta — análise, comentário,
opinião — em três tipos, para que só o verificável seja conferido depois.

  fato       algo já ocorrido, ou um estado presente. Outra fonte poderia
             confirmar ou desmentir. É o único tipo que será verificado.
  previsao   afirma sobre o futuro
  opiniao    juízo, avaliação, recomendação, valoração

Regras que importam mais que as outras:

1. NÚMERO NÃO GARANTE QUE É FATO. "o dólar está em R$ 5,80" é fato; "o dólar
   está caro" é opinião mesmo falando da mesma coisa. O que separa é existir
   uma fonte capaz de dizer que está errado.

2. SEPARE A PREMISSA DA CONCLUSÃO, mesmo na mesma frase.

   Texto:  "Com o desemprego em 5,3%, o Copom não tem escolha."
   fato:      o desemprego está em 5,3%
   opiniao:   o Copom não tem escolha

3. A AFIRMAÇÃO PRECISA SE SUSTENTAR SOZINHA. Quem vai lê-la não tem o texto
   original ao lado. Resolva pronome, apelido e referência implícita.

   Errado: "ela subiu 5,9%"
   Certo:  "o lucro da Caixa subiu 5,9% no 2º trimestre de 2026"

4. O TRECHO É LITERAL. Copie do texto, não reescreva. É o que permite conferir
   que a separação não inventou nada.

5. NÃO CORRIJA E NÃO JULGUE. Se o texto afirma um número que você acredita
   estar errado, extraia como está. Verificar é o passo seguinte, e é feito
   contra fonte, não contra o seu conhecimento.

6. O QUE NÃO É AFIRMAÇÃO FICA DE FORA. Pergunta retórica, saudação, chamada
   para seguir o perfil, emoji solto.
"""


def separa(texto: str) -> tuple[Analise, llm.Uso]:
    r = llm.gera(INSTRUCOES, f"Texto:\n{texto}", Analise,
                 modelo=llm.VERIFICACAO)
    return r.dados, r.uso


def main() -> None:
    # stdin entra na lista porque este módulo LÊ da entrada padrão: sem o
    # reconfigure, arquivo UTF-8 redirecionado no Windows chega em cp1252 e
    # o texto vai mojibake para o modelo ("cÃºpula") — visto em 30/08/2026.
    for fluxo in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Confere as premissas de um texto que argumenta.")
    parser.add_argument("texto", nargs="*",
                        help="o texto; se omitido, lê da entrada padrão")
    parser.add_argument("--dry-run", action="store_true",
                        help="mostra o que seria enviado, sem chamar a API")
    parser.add_argument("-v", action="store_true", help="mostra as candidatas")
    args = parser.parse_args()

    texto = " ".join(args.texto).strip() or sys.stdin.read().strip()
    if not texto:
        print('Uso: python -m src.premissas "texto"  ou  ... < arquivo.txt')
        sys.exit(1)

    if args.dry_run:
        print(f"--- system (fixo, cacheado) ---\n{INSTRUCOES}")
        print(f"--- user ---\nTexto:\n{texto}\n")
        print(f"~{(len(INSTRUCOES) + len(texto)) // 4} tokens de entrada")
        print("\nNada foi enviado. Remova --dry-run para rodar.")
        return

    print(f"TEXTO\n  {texto[:300]}{'...' if len(texto) > 300 else ''}\n")

    analise, uso = separa(texto)
    fatos = [p for p in analise.premissas if p.tipo == "fato"]
    resto = [p for p in analise.premissas if p.tipo != "fato"]

    print(f"{len(analise.premissas)} afirmações · {len(fatos)} verificáveis\n")

    if resto:
        # Impresso ANTES, e nomeado pelo que é. Previsão e opinião não são
        # defeito do texto — são o texto. Mostrá-las como descarte sugeriria
        # que o autor deveria tê-las evitado.
        print("=" * 78)
        print("NÃO VERIFICÁVEL — e não deve ser")
        print("=" * 78)
        for p in resto:
            print(f"  [{p.tipo}] {p.afirmacao}")
        print()

    if not fatos:
        print("Nenhuma afirmação factual. Nada a conferir.")
        print(f"\n  custo: US$ {uso.custo:.4f}")
        return

    conexao = conecta(config.BANCO)
    acervo = grafo.carrega(conexao)
    if not acervo:
        print("Acervo vazio. Rode a coleta, a extração e o índice.")
        conexao.close()
        sys.exit(1)

    print("=" * 78)
    print("PREMISSAS, CONFERIDAS CONTRA O ACERVO")
    print("=" * 78)
    for i, p in enumerate(fatos, 1):
        print(f"\n[{i}/{len(fatos)}] no texto: \"{p.trecho[:110]}\"")
        check.verifica(p.afirmacao, verboso=args.v,
                       conexao=conexao, acervo=acervo)
    conexao.close()

    print("\n" + "=" * 78)
    # O aviso fecha a saída de propósito: é a última coisa lida, e é a que
    # impede a leitura errada. Ver o cabeçalho do módulo.
    print("Isto confere NÚMEROS contra o acervo, não avalia o autor.")
    print("Premissa sem evidência significa que os veículos coletados não")
    print("cobrem o assunto — não que a afirmação seja falsa.")
    print(f"\n  separação das premissas: US$ {uso.custo:.4f}"
          f" · mais uma verificação por premissa")


if __name__ == "__main__":
    main()
