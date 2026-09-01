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
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field

from . import config, grafo, indice, llm, vocabulario
from .canonico import chave_canonica
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


INSTRUCOES_ESTRUTURA = f"""\
Você converte uma afirmação em forma estruturada para ser procurada num acervo
de notícias.

Não julgue se a afirmação é verdadeira. Apenas estruture o que ela afirma.

Se a afirmação for negativa ("X não fez Y"), estruture o fato POSITIVO ("X fez
Y") — o acervo guarda o que aconteceu, e a negação é justamente o que a
verificação vai decidir. O campo `busca` também vai no positivo.

A relação vem da lista fechada abaixo. Use `outro` quando nenhuma servir.
As definições são as MESMAS que a extração usa — a rota por chave exata só
encontra o acervo se os dois lados escolherem a mesma relação para o mesmo
fato:

{vocabulario.resumo_para_prompt()}
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


def _por_chave(afirmacao: AfirmacaoRecebida,
               acervo: list[grafo.Afirmacao]) -> list[indice.Achado]:
    """Tudo que o acervo afirma sobre (sujeito, relação), por identidade exata.

    Esta rota existia no docstring e não no código: a "chave exata" era, na
    verdade, uma segunda busca vetorial pelo nome da entidade. Vetorial de novo
    não cobre a falha da vetorial.

    E a falha é específica de afirmação NUMÉRICA. O número quase não pesa no
    embedding — "38 %" são dois tokens numa frase de vinte dominada pelo nome
    do instituto —, então a busca semântica vira casamento de entidade e o
    valor, que é justamente o que se quer verificar, fica invisível. Medido:
    ao checar "Juliana Brizola tem 38%", a tripla dos 38% saiu em 8º de 10,
    atrás de uma sobre o capital votante da Petrobras. Com um teto de 7
    candidatas o veredito teria saído errado.

    Aqui o casamento é por igualdade de string, sem ranking: se o acervo afirma
    algo sobre aquele par, entra. É barato — o grafo já está em memória — e é
    determinístico.

    A igualdade passa pela `chave_canonica` dos DOIS lados: o estruturador e a
    extração são chamadas isoladas e canonizam com variações ("Petrobras" ×
    "Petrobrás", "Braskem" × "Braskem S.A."). Igualdade crua fazia a rota que
    existe para cobrir a falha da vetorial falhar em silêncio sob variação de
    grafia — evidência que não chega, sem erro nenhum.
    """
    achados = []
    alvo = chave_canonica(afirmacao.sujeito_canonico)
    for a in acervo:
        if (chave_canonica(a.sujeito) == alvo
                and a.relacao == afirmacao.relacao.value):
            achados.append(indice.Achado(
                texto=indice.texto_da_tripla(a.sujeito, a.relacao, a.objeto,
                                             a.valor, a.unidade, a.contexto),
                # Identidade exata não tem distância semântica a reportar. O
                # 0.0 nunca é exibido como porcentagem: a rota vai no metadado
                # e a tela mostra "chave", para não parecer 100% de semelhança.
                distancia=0.0,
                meta={
                    "veiculo": a.veiculo, "titulo": a.titulo, "url": a.url,
                    "sujeito": a.sujeito, "relacao": a.relacao,
                    "objeto": a.objeto or "", "data_fato": a.data_fato or "",
                    "origem": a.origem, "sentenca": -1, "rota": "chave",
                    "valor": a.valor if a.valor is not None else "",
                    "unidade": a.unidade or "", "contexto": a.contexto or "",
                },
            ))
    return achados


def recupera(afirmacao: AfirmacaoRecebida,
             acervo: list[grafo.Afirmacao] | None = None) -> list[indice.Achado]:
    """Junta candidatas por proximidade semântica e por identidade exata.

    As duas rotas são complementares e cobrem falhas uma da outra: a vetorial
    encontra o fato descrito com outras palavras, e a chave exata garante que
    tudo que o acervo afirma sobre aquele par (sujeito, relação) chegue ao
    julgamento, independente de como ficou o ranking.

    A chave exata entra PRIMEIRO. A ordem importa porque o modelo lê a lista em
    ordem, e porque um teto de candidatas cortaria o fim — que era exatamente
    onde a evidência certa estava caindo.
    """
    achados = _por_chave(afirmacao, acervo or [])
    vistos = {a.meta.get("sujeito", "") + a.texto for a in achados}

    for a in indice.busca("afirmacoes", afirmacao.busca, QUANTAS_CANDIDATAS):
        chave = a.meta.get("sujeito", "") + a.texto
        if a.proximidade >= MIN_PROXIMIDADE and chave not in vistos:
            a.meta.setdefault("rota", "semantica")
            achados.append(a)
            vistos.add(chave)

    return achados


_ORIGEM_LEGIVEL = {"EXTRACTED": "explícita", "INFERRED": "inferida",
                   "e": "explícita", "i": "inferida"}
"""O enum de origem em português de tela. Cobre as duas gerações de
valores gravados (EXTRACTED/INFERRED até 01/09/2026; e/i do schema magro
em diante) — o cru vazava para o Telegram e para o julgamento."""


def _origem_legivel(valor: str) -> str:
    return _ORIGEM_LEGIVEL.get(valor, valor)


def julga(texto: str, evidencias: list[indice.Achado]) -> tuple[Julgamento, llm.Uso]:
    linhas = []
    for i, e in enumerate(evidencias, 1):
        m = e.meta
        linhas.append(
            f"[{i}] {e.texto}\n"
            f"    veículo: {m['veiculo']} · data do fato: {m['data_fato'] or 'não informada'}"
            f" · afirmação {_origem_legivel(m['origem'])}\n"
            f"    matéria: {m['titulo']}"
        )
    corpo = (
        f"AFIRMAÇÃO A VERIFICAR:\n{texto}\n\n"
        f"EVIDÊNCIA RECUPERADA DO ACERVO:\n" + "\n".join(linhas)
    )
    r = llm.gera(INSTRUCOES_JULGAMENTO, corpo, Julgamento,
                 modelo=llm.VERIFICACAO)
    return r.dados, r.uso


HORAS_REUSO = 24
"""Janela em que a mesma afirmação reusa o veredito gravado em vez de pagar.

Medido no livro-caixa em 31/08/2026: das 29 consultas gravadas, 7 eram
repetições da mesma afirmação — 29% do gasto de consulta pagando de novo
pela mesma resposta. A janela é curta de propósito: "sem evidência" muda
conforme o acervo cresce, e um dia depois a repetição volta a valer a pena.
`--forcar` ignora a janela.
"""


def consulta_recente(conexao, texto: str,
                     horas: int = HORAS_REUSO):
    """Veredito já gravado para esta afirmação dentro da janela, ou None.

    O casamento é por texto normalizado (minúsculas, espaços colapsados) em
    Python — o lower() do SQLite ignora acento e mentiria em "É falso que".
    """
    if conexao is None:
        return None
    alvo = " ".join(texto.lower().split())
    limite = (datetime.now(timezone.utc)
              - timedelta(hours=horas)).isoformat()
    for linha in conexao.execute(
            "SELECT * FROM consultas WHERE consultado_em >= ? "
            "ORDER BY id DESC", (limite,)):
        if " ".join(linha["afirmacao"].lower().split()) == alvo:
            return linha
    return None


def verifica(texto: str, verboso: bool = False,
             conexao=None, acervo=None, forcar: bool = False) -> None:
    print(f'AFIRMAÇÃO\n  "{texto}"\n')

    if not forcar:
        anterior = consulta_recente(conexao, texto)
        if anterior is not None:
            rotulo = {"confirmado": "CONFIRMADO", "contradito": "CONTRADITO",
                      "sem_evidencia": "SEM EVIDÊNCIA"}[anterior["veredito"]]
            quando = anterior["consultado_em"][:16].replace("T", " ")
            print(f"VEREDITO (reusado — verificada em {quando} UTC)\n"
                  f"  {rotulo} · {anterior['veiculos']} veículo(s)\n")
            print(f"POR QUE\n  {anterior['justificativa']}\n")
            print("  Sem custo: veredito gravado nas últimas "
                  f"{HORAS_REUSO}h. Use --forcar para re-verificar "
                  "(o acervo pode ter crescido desde então).")
            return

    afirmacao, uso1 = estrutura(texto)
    if verboso:
        print(f"  estruturada: ({afirmacao.sujeito_canonico}, "
              f"{afirmacao.relacao.value}, {afirmacao.objeto_canonico or '—'})")
        print(f"  busca: \"{afirmacao.busca}\"\n")

    evidencias = recupera(afirmacao, acervo)

    if verboso and evidencias:
        # As candidatas que o modelo VAI ver, antes de ele escolher.
        # Sem isto so da para conferir o que foi citado, e o defeito mais
        # provavel do sistema e o contrario: a evidencia certa nao subir no
        # ranking e nunca chegar ao julgamento. Erro que nao aparece em
        # lugar nenhum, porque o modelo julga bem o material errado.
        print(f"  {len(evidencias)} candidatas recuperadas:")
        for i, e in enumerate(evidencias, 1):
            rota = ("chave" if e.meta.get("rota") == "chave"
                    else f"{e.proximidade:.0%}")
            print(f"    [{i:>2}] {rota:>5}  {e.texto[:74]}")
            print(f"          [{e.meta['veiculo']}] {e.meta['titulo'][:60]}")
        print()

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
            # Nada truncado aqui, e nao e questao de estetica: o principio 2 diz
            # que todo veredito carrega a fonte, e fonte que o leitor nao
            # consegue abrir nao e fonte. A URL cortada em 62 caracteres
            # parecia citacao e nao servia para conferir nada.
            print(f"  [{m['veiculo']}] {m['titulo']}")
            print(f"    {e.texto}{valor}")
            print(f"    fato: {m['data_fato'] or 'não informada'} · "
                  f"afirmação {_origem_legivel(m['origem'])}")
            print(f"    {m['url']}")
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

    args = [a for a in sys.argv[1:] if a not in ("-v", "--forcar")]
    if not args:
        print('Uso: python -m src.check "afirmação" [-v] [--forcar]')
        sys.exit(1)

    conexao = conecta(config.BANCO)
    # Carregado uma vez e passado adiante: a rota por chave exata precisa do
    # acervo em memoria, e le-lo duas vezes so gastaria tempo.
    acervo = grafo.carrega(conexao)
    if not acervo:
        print("Acervo sem afirmações. Rode a coleta, a extração e o índice.")
        sys.exit(1)
    try:
        verifica(" ".join(args), verboso="-v" in sys.argv,
                 conexao=conexao, acervo=acervo,
                 forcar="--forcar" in sys.argv)
    finally:
        conexao.close()


if __name__ == "__main__":
    main()
