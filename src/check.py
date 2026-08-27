"""Verificação de uma afirmação contra o acervo.

    python -m src.check "o governo cancelou o programa X"

É o produto. Recebe uma afirmação que NÃO veio do acervo, procura evidência, e
devolve um veredito com as fontes que o sustentam.

O que este módulo é e o que não é:

    verifica contra o ACERVO, não contra a realidade

A resposta é sempre "os veículos que eu tenho sustentam isso", "contradizem" ou
"não falam do assunto". Nunca "isso é verdade". O acervo é catalogado, não
verificado: ele guarda o que cada veículo afirmou, e uma fonte errada entra
igual.

Duas chamadas de LLM, nenhum agente:

    1. a afirmação vira tripla, para poder ser procurada
    2. a evidência recuperada é julgada contra ela

Entre as duas, só código: busca vetorial por proximidade, grafo por identidade,
e a montagem da resposta. Nada decide, em tempo de execução, qual é o próximo
passo — por isso não há ciclo, e por isso não há agente.
"""

import sys
from typing import Literal

from pydantic import BaseModel, Field

from . import config, grafo, indice, llm
from .storage import conecta, salva_consulta
from .vocabulario import Relacao

MIN_PROXIMIDADE = 0.55
"""Piso de similaridade para uma afirmação do acervo virar evidência candidata.

Abaixo disso o texto trata de outro assunto, e incluí-lo faria o modelo julgar
contra material irrelevante — que é como se produz veredito confiante e errado.
"""

QUANTAS_CANDIDATAS = 10


class AfirmacaoRecebida(BaseModel):
    """A afirmação que chegou, estruturada para poder ser procurada."""

    sujeito_canonico: str = Field(
        description="Entidade principal, nome completo e oficial."
    )
    relacao: Relacao
    objeto_canonico: str | None = Field(
        description="Segunda entidade, ou null se a afirmação for sobre um valor."
    )
    valor_numero: float | None
    valor_unidade: str | None
    busca: str = Field(
        description=(
            "A afirmação reescrita como frase curta e neutra, para busca "
            "semântica. Sem negação, sem quem disse: o acervo guarda o fato, "
            "não a dúvida sobre ele."
        )
    )


class Julgamento(BaseModel):
    """O veredito sobre a afirmação, dada a evidência recuperada."""

    veredito: Literal["confirmado", "contradito", "sem_evidencia"]
    evidencias: list[int] = Field(
        description=(
            "Números das evidências que sustentam o veredito, da lista "
            "apresentada. Vazio quando o veredito for sem_evidencia."
        )
    )
    justificativa: str = Field(
        description=(
            "Uma frase explicando a decisão. É o raciocínio do sistema, "
            "exibido separado das fontes — não pode se passar por citação."
        )
    )


INSTRUCOES_ESTRUTURA = """\
Você converte uma afirmação em forma estruturada para ser procurada num acervo
de notícias.

Não julgue se a afirmação é verdadeira. Apenas estruture o que ela afirma.

Se a afirmação for negativa ("X não fez Y"), estruture o fato POSITIVO ("X fez
Y") — o acervo guarda o que aconteceu, e a negação é justamente o que a
verificação vai decidir. O campo `busca` também vai no positivo.

A relação vem de uma lista fechada. Use `outro` quando nenhuma servir.
"""


INSTRUCOES_JULGAMENTO = """\
Você julga se uma afirmação é sustentada pela evidência recuperada de um acervo
de notícias.

Três vereditos possíveis:

  confirmado      a evidência afirma o mesmo que a afirmação
  contradito      a evidência afirma algo incompatível com ela
  sem_evidencia   a evidência não trata do que a afirmação diz

Regras que importam mais que as outras:

1. SEM EVIDÊNCIA É RESPOSTA VÁLIDA, e é a correta sempre que a evidência não
   resolver a questão. Não preencha lacuna com plausibilidade: dizer "não sei"
   é o comportamento certo, e forçar um veredito é a falha que este sistema
   existe para evitar.

2. EVIDÊNCIA SOBRE O MESMO ASSUNTO NÃO É EVIDÊNCIA SOBRE A AFIRMAÇÃO. Uma
   matéria que fala da mesma empresa não confirma nada sobre uma dívida
   específica. Só conta o que trata do fato afirmado.

3. INCOMPATIBILIDADE É CONTRADIÇÃO. Se a afirmação diz que um programa foi
   cancelado e a evidência diz que foi ampliado, isso é `contradito`, não
   `sem_evidencia`.

4. VALOR DIFERENTE É CONTRADIÇÃO quando mede a mesma coisa na mesma unidade.
   Moedas diferentes para o mesmo fato não são divergência.

5. CITE APENAS AS EVIDÊNCIAS QUE USOU, pelo número. Veredito sem evidência
   citada não pode ser conferido, e este sistema só afirma o que pode mostrar.
"""


def estrutura(texto: str) -> tuple[AfirmacaoRecebida, llm.Uso]:
    r = llm.gera(INSTRUCOES_ESTRUTURA, f"Afirmação: {texto}",
                 AfirmacaoRecebida, modelo=llm.VERIFICACAO)
    return r.dados, r.uso


def recupera(afirmacao: AfirmacaoRecebida) -> list[indice.Achado]:
    """Junta candidatas por proximidade semântica e por identidade no grafo.

    As duas rotas são complementares e cobrem falhas uma da outra: a busca
    vetorial encontra o fato descrito com outras palavras, e a chave exata
    encontra o que a paráfrase não alcançaria por estar escrito de forma
    técnica demais.
    """
    achados = [
        a for a in indice.busca("afirmacoes", afirmacao.busca, QUANTAS_CANDIDATAS)
        if a.proximidade >= MIN_PROXIMIDADE
    ]
    vistos = {a.meta.get("sujeito", "") + a.texto for a in achados}

    por_entidade = indice.busca("afirmacoes", afirmacao.sujeito_canonico, 6)
    for a in por_entidade:
        chave = a.meta.get("sujeito", "") + a.texto
        if chave not in vistos and a.meta.get("relacao") == afirmacao.relacao.value:
            achados.append(a)
            vistos.add(chave)

    return achados


def julga(texto: str, evidencias: list[indice.Achado]) -> tuple[Julgamento, llm.Uso]:
    linhas = []
    for i, e in enumerate(evidencias, 1):
        m = e.meta
        linhas.append(
            f"[{i}] {e.texto}\n"
            f"    veículo: {m['veiculo']} · data do fato: {m['data_fato'] or 'não informada'}"
            f" · {m['origem']}\n"
            f"    matéria: {m['titulo']}"
        )
    corpo = (
        f"AFIRMAÇÃO A VERIFICAR:\n{texto}\n\n"
        f"EVIDÊNCIA RECUPERADA DO ACERVO:\n" + "\n".join(linhas)
    )
    r = llm.gera(INSTRUCOES_JULGAMENTO, corpo, Julgamento,
                 modelo=llm.VERIFICACAO)
    return r.dados, r.uso


def verifica(texto: str, verboso: bool = False,
             conexao=None) -> None:
    print(f'AFIRMAÇÃO\n  "{texto}"\n')

    afirmacao, uso1 = estrutura(texto)
    if verboso:
        print(f"  estruturada: ({afirmacao.sujeito_canonico}, "
              f"{afirmacao.relacao.value}, {afirmacao.objeto_canonico or '—'})")
        print(f"  busca: \"{afirmacao.busca}\"\n")

    evidencias = recupera(afirmacao)

    if not evidencias:
        print("VEREDITO\n  SEM EVIDÊNCIA · 0 veículos\n")
        print("  Nada no acervo trata desta afirmação. Isso não significa que "
              "ela seja falsa —\n  significa que os veículos coletados não "
              "falam do assunto.")
        print(f"\n  custo: US$ {uso1.custo:.4f}")
        # Gravado tambem quando nao ha evidencia: a consulta foi feita,
        # foi cobrada, e "o acervo nao cobre isto" e justamente o que
        # precisa ser contado para saber onde a coleta tem buraco.
        if conexao is not None:
            salva_consulta(conexao, texto, "sem_evidencia",
                           "Nenhuma candidata acima do piso de proximidade.",
                           0, 0, 0, llm.VERIFICACAO.id, uso1.custo)
        return

    julgamento, uso2 = julga(texto, evidencias)
    citadas = [evidencias[i - 1] for i in julgamento.evidencias
               if 1 <= i <= len(evidencias)]
    veiculos = {e.meta["veiculo"] for e in citadas}

    rotulo = {"confirmado": "CONFIRMADO", "contradito": "CONTRADITO",
              "sem_evidencia": "SEM EVIDÊNCIA"}[julgamento.veredito]
    print(f"VEREDITO\n  {rotulo} · {len(veiculos)} "
          f"{'veículo' if len(veiculos) == 1 else 'veículos'}\n")

    if citadas:
        print("EVIDÊNCIA")
        for e in citadas:
            m = e.meta
            valor = ""
            if m.get("valor") not in ("", None):
                valor = f" = {m['valor']:g} {m.get('unidade', '')}".rstrip()
            print(f"  [{m['veiculo']}] {m['titulo'][:66]}")
            print(f"    {e.texto[:88]}{valor}")
            print(f"    fato: {m['data_fato'] or 'não informada'} · "
                  f"{m['origem']} · {m['url'][:62]}")
        print()

    # Separado das fontes de propósito: é o sistema falando, não o veículo.
    print(f"POR QUE\n  {julgamento.justificativa}")

    if len(veiculos) == 1 and julgamento.veredito != "sem_evidencia":
        print("\n  ATENÇÃO: um veículo só. Sem confirmação independente.")

    print(f"\n  {len(evidencias)} candidatas recuperadas · "
          f"custo US$ {uso1.custo + uso2.custo:.4f}")

    if conexao is not None:
        salva_consulta(conexao, texto, julgamento.veredito,
                       julgamento.justificativa, len(evidencias),
                       len(citadas), len(veiculos), llm.VERIFICACAO.id,
                       uso1.custo + uso2.custo)


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    args = [a for a in sys.argv[1:] if a != "-v"]
    if not args:
        print('Uso: python -m src.check "afirmação a verificar" [-v]')
        sys.exit(1)

    conexao = conecta(config.BANCO)
    if not grafo.carrega(conexao):
        print("Acervo sem afirmações. Rode a coleta, a extração e o índice.")
        sys.exit(1)
    try:
        verifica(" ".join(args), verboso="-v" in sys.argv, conexao=conexao)
    finally:
        conexao.close()


if __name__ == "__main__":
    main()
