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
import hashlib
import json
import sqlite3
import sys
from typing import Literal

from pydantic import BaseModel, Field

from . import agrupa, boilerplate, config, indice, llm, vocabulario
from .vocabulario import Relacao
from .segment import em_sentencas
from .storage import (
    conecta, estatisticas_triplas, salva_extracao)

VOCAB_VERSAO = vocabulario.VERSAO

MAX_SENTENCAS: int | None = 5
"""Quantas sentenças da matéria chegam ao modelo. `None` manda a matéria toda.

MEDIDO no acervo de 14 matérias, 282 triplas, 9 fatos confirmados:

    corte    confirmações mantidas    triplas pagas
      2          16 de 19  (84%)      103 de 382  (27%)
      4          17 de 19  (89%)      134 de 382  (35%)
     10          18 de 19  (95%)      210 de 382  (55%)
    sem          19 de 19 (100%)      382 de 382 (100%)

Só 7% das triplas pagas participam de alguma confirmação. Os outros 93% são
detalhe que um veículo só publicou — as 43 triplas sobre dívidas de
subsidiárias da Braskem nunca serão corroboradas, porque nenhum outro jornal
desceu àquele nível.

O motivo é a pirâmide invertida: o fato principal vai no primeiro parágrafo, e
é ele que dois veículos publicam igual. O corpo é exclusivo por natureza, e
exclusivo não corrobora.

É o princípio 6 do ARCHITECTURE — filtro barato antes de chamada cara.

O QUE SE PERDE: o acervo fica mais raso. Afirmação sobre detalhe do oitavo
parágrafo passa a receber "sem evidência", corretamente, porque o detalhe não
foi extraído. Troca deliberada de profundidade por confirmação  por dólar.

Amostra pequena. Este número tem que ser refeito quando o acervo crescer.
"""

MAX_TRIPLAS: int | None = None
"""Teto de triplas por matéria, ou None para não limitar.

None durante a medição. Cheguei a repor em 30 junto com a correção do
truncamento, e era conserto a mais: o que causou a falha foi o teto de TOKENS,
já corrigido. O teto de triplas resolve custo, não truncamento.

E ele custa caro aqui. Se uma matéria rende 45 fatos e o modelo entrega 30, os
15 restantes somem sem aviso — e se a divergência entre veículos estiver entre
eles, a medição conclui "não há contradição" quando a verdade é "não olhei".
Perda silenciosa no experimento que decide o projeto.

Repor quando o custo passar a mandar, isto é, quando a extração rodar sobre
centenas de matérias por dia em vez de dezenas escolhidas a dedo."""

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
    relacao: Relacao = Field(
        description=(
            "A relação que a afirmação estabelece. Escolha da lista fechada; "
            "não há outros valores possíveis. Use `outro` quando nenhuma "
            "servir — forçar uma relação errada é pior que admitir a lacuna."
        )
    )
    objeto: str | None = Field(
        description=(
            "Segunda entidade como apareceu no texto. null quando a afirmação é "
            "um ATRIBUTO do sujeito e não uma relação com outra entidade — "
            "margem de erro, custo, nível de confiança."
        )
    )
    objeto_canonico: str | None = Field(
        description="Nome canônico da segunda entidade. null junto com `objeto`."
    )
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
    valor_numero: float | None = Field(
        description=(
            "Quando a afirmação é sobre uma quantidade, o número puro aqui — "
            "38, não '38%'. null quando não houver quantidade."
        )
    )
    valor_unidade: str | None = Field(
        description=(
            "Unidade do número, curta e padronizada: '%', 'BRL', 'pessoas', "
            "'pontos percentuais', 'votos'. null se não houver."
        )
    )
    valor_contexto: str | None = Field(
        description=(
            "O que o número mede, curto: '1º turno', 'margem de erro', "
            "'2º cenário'. null se não houver."
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

   Resolver a quem um apelido se refere é DEDUÇÃO, mesmo quando é óbvio:

   Frase:  "Juliana tem 48%, contra 35% do emedebista."
   Errado: (Gabriel Souza, obteve_percentual_em, ...) EXTRACTED
   Certo:  (Gabriel Souza, obteve_percentual_em, ...) INFERRED

   O nome não está na frase. Você o recuperou do contexto — isso é INFERRED.
   Vale para "o emedebista", "o senador amapaense", "a ex-deputada", "ele".

2. ENTIDADE CANÔNICA. Fontes diferentes chamam a mesma entidade de formas
   diferentes. O campo canônico precisa convergir: se duas matérias falam da
   mesma pessoa ou instituição, os valores canônicos têm que ser idênticos,
   caractere por caractere. Use o nome completo e oficial, sem cargo e sem
   artigo. Não invente hierarquia: se o texto diz "Ministério da Saúde", o
   canônico é o ministério, nunca "governo federal".

   Use o nome COMPLETO, nunca só o sobrenome. O texto abrevia depois da
   primeira menção; o canônico não pode acompanhar essa abreviação, senão duas
   matérias sobre a mesma pessoa não se encontram.

   Errado: Zucco · Couto · Haddad
   Certo:  Luciano Zucco · Fernando Haddad

   Se o nome completo não estiver na matéria, use a forma mais completa que
   houver e marque a tripla como INFERRED.

3. RELAÇÃO. Escolha uma da lista fechada abaixo. Não existem outros
   valores: o schema recusa qualquer coisa fora dela.

  afirmou                declarou algo, sem valoração explícita
  criticou               declarou com valoração negativa, ou atacou alguém ou algo
  defendeu               declarou apoio a algo, ou propôs que algo seja feito
  integra                faz parte de partido, chapa, comissão ou organização
  exerce_cargo_em        ocupa cargo numa instituição, sem dirigi-la
  preside                dirige uma instituição, comissão ou órgão
  candidatou_se_a        é candidato a um cargo
  obteve_percentual_em   resultado em pesquisa ou votação; número no valor
  submeteu_a_votacao     pôs proposta em votação, ou ela foi votada num órgão
  preve                  projeto ou proposta prevê algo; nunca fato consumado
  abriu_processo_contra  iniciou processo, investigação ou ação contra
  participou_de          esteve em entrevista, sabatina, sessão ou evento
  divulgou               publicou ou tornou público um dado, estudo ou documento
  tem_atributo           propriedade com valor numérico — custo, margem de erro, amostra. Objeto null, e o nome da propriedade vai em valor_contexto
  outro                  afirmação verificável que não cabe em nenhuma acima. Use sem hesitar: forçar uma relação que não serve é pior

   Prefira a relação específica quando ela couber. `outro` existe para
   afirmação verificável que nenhuma descreve — usá-la é melhor que
   forçar uma relação que não serve, porque a lista aprende com o que
   cai lá, e não aprende com o que foi forçado.

4. DATA DO FATO. É quando o fato ocorreu, não quando a matéria foi
   publicada. Elas divergem quando a matéria trata de algo antigo, e essa
   divergência é justamente o que o sistema precisa enxergar.

   Para relação de ESTADO sem data explícita no texto, use null. Filiação
   partidária, cargo e propriedade duram anos; carimbá-los com a data da
   matéria inventa uma precisão que a fonte não deu, e faz duas matérias sobre
   o mesmo fato permanente parecerem separadas no tempo.

5. QUANTIDADE NÃO VAI NO OBJETO. Se a afirmação é sobre um número, o objeto é
   a ENTIDADE a que o número se refere, e o número vai nos campos de valor.

   Errado:  (Fulano, obteve, 38% das intenções de voto no 1º turno)
   Certo:   (Fulano, obteve_percentual_em, Pesquisa X)
            valor_numero 38 · valor_unidade "%" · valor_contexto "1º turno"

   O motivo é concreto: dois veículos noticiando a mesma pesquisa nunca
   escreveriam a mesma frase no objeto, e as triplas jamais se encontrariam no
   grafo. Como número, se encontram — e divergência entre eles é justamente a
   contradição que o sistema procura.

6. ATRIBUTO NÃO É RELAÇÃO. Quando a afirmação é uma PROPRIEDADE do sujeito e
   não um vínculo com outra entidade, `objeto` e `objeto_canonico` são null.

   Errado: (Pesquisa X, teve_margem_de_erro, margem de erro)
   Errado: (Pesquisa X, custou, Instituto Y)
   Certo:  (Pesquisa X, teve_margem_de_erro, null) valor 2 "pontos percentuais"
   Certo:  (Pesquisa X, teve_custo, null)          valor 24000 "BRL"

   Margem de erro, custo e nível de confiança são propriedades da pesquisa, não
   relações com algo. Inventar um objeto para preencher o campo produz tripla
   que não se conecta a nada no grafo.

   ISTO NÃO VALE PARA DECLARAÇÃO. Relação de fala — afirmou, criticou,
   defendeu, chamou, declarou — SEMPRE tem objeto: é o conteúdo do que foi
   dito. Objeto nulo ali apaga a afirmação inteira.

   Errado: (Ruas, afirmou, null)
   Certo:  (Ruas, afirmou, ADPF 635 transformou o Rio em resort para criminosos)

   Regra geral: toda tripla precisa carregar OU um objeto OU um valor
   numérico. Sem nenhum dos dois, ela não afirma nada e não deve existir.

7. PROPOSTA NÃO É FATO CONSUMADO. Projeto de lei, plano, promessa e proposta
   descrevem o que ACONTECERIA, não o que aconteceu.

   Frase:  "o PL 2.234/2022, que legaliza cassinos"
   Errado: (PL 2.234/2022, legalizou, Jogos de azar) EXTRACTED
   Certo:  (PL 2.234/2022, preve_legalizacao_de, Jogos de azar) EXTRACTED

   O projeto legalizaria. Ele está em tramitação. Registrar como consumado
   coloca no acervo um fato que não ocorreu.

8. RELAÇÃO PRECISA SIGNIFICAR ALGO. Nunca use verbos vazios como "foi", "teve"
   ou "esteve" sozinhos. (Jonathan Karter, foi, Poder360) não afirma nada.
   Prefira exercer_cargo_em, integrou, foi_transmitido_em.

9. IGNORE TEXTO INSTITUCIONAL DO VEÍCULO. Chamada de podcast, agregador,
   newsletter, canal no YouTube e descrição da própria redação não são
   notícia. Nada disso vira tripla.

10. ATRIBUIÇÃO. Para "Fulano afirmou que Z", o objeto é o CONTEÚDO de Z,
   resumido numa frase curta — nunca o assunto nem a pessoa citada.

   Errado:  (Girão, afirmou, Davi Alcolumbre)
   Errado:  (Girão, afirmou, Casas de apostas on-line)
   Certo:   (Girão, afirmou, Alcolumbre tem obsessão por jogos de azar)

   Marque EXTRACTED e não trate Z como fato do mundo: o verificável ali é que
   Fulano disse, não que Z seja verdade.

   QUEM FALOU É QUEM O TEXTO DIZ QUE FALOU. Antes de escolher o sujeito de uma
   relação de fala, ache o verbo de dizer e o sujeito DELE — "escreveu o
   magistrado", "disse a ministra", "segundo o relator", "afirmou o advogado".
   O falante é esse. Nunca é uma pessoa que aparece apenas DENTRO da citação.

   Frase:  "a prova amealhada nos autos não autoriza a condenação do
            recorrente", escreveu o magistrado.
   Errado: (Recorrente, afirmou, a prova não autoriza a condenação)
   Certo:  (Magistrado, afirmou, a prova não autoriza a condenação do recorrente)

   O recorrente é sobre quem se fala; o magistrado é quem fala. Trocar os dois
   põe na boca de alguém a frase que o condena, e sai do sistema com fonte
   citada ao lado. É o erro mais grave que esta extração pode cometer.

Exemplo:

  Matéria publicada em 2026-08-20, sentenças numeradas:
    [0] O Ibope divulgou nesta quarta-feira pesquisa que mostra o senador
        Carlos Lima (PSD) com 41% das intenções de voto ao governo paulista.
    [1] O levantamento custou R$ 30.000 e ouviu 2.000 eleitores.
    [2] Lima disse que "a segurança pública será prioridade absoluta".
    [3] Para analistas, o resultado surpreende.

  Saída:
    (Instituto Brasileiro de Opinião Pública e Estatística, divulgou,
     Pesquisa Ibope SP agosto 2026)
       evento · EXTRACTED · fato 2026-08-19 · sent 0
    (Carlos Lima, obteve_percentual_em, Pesquisa Ibope SP agosto 2026)
       evento · EXTRACTED · fato 2026-08-19 · sent 0
       valor_numero 41 · valor_unidade "%"
    (Carlos Lima, integra, Partido Social Democrático)
       estado · EXTRACTED · fato null · sent 0
    (Carlos Lima, candidatou_se_a, Governo do Estado de São Paulo)
       estado · EXTRACTED · fato null · sent 0
    (Pesquisa Ibope SP agosto 2026, tem_atributo, null)
       estado · EXTRACTED · fato null · sent 1
       valor_numero 30000 · valor_unidade "BRL" · valor_contexto "custo"
    (Pesquisa Ibope SP agosto 2026, tem_atributo, null)
       estado · EXTRACTED · fato null · sent 1
       valor_numero 2000 · valor_unidade "pessoas" · valor_contexto "amostra"
    (Carlos Lima, afirmou, a segurança pública será prioridade absoluta)
       evento · EXTRACTED · fato 2026-08-19 · sent 2

  Repare em cada decisão:
  - "nesta quarta-feira" virou data real, e o Ibope foi expandido no canônico
  - o percentual saiu do objeto e virou valor, para que outro veículo
    noticiando a mesma pesquisa chegue ao mesmo número
  - filiação e candidatura são estado, e o texto não as data: fato null
  - custo e amostra são atributos: objeto null, e o que eles medem vai em
    valor_contexto
  - a fala virou o CONTEÚDO dito, não o assunto
  - a sentença [3] é opinião de terceiros e não gerou tripla
"""


def versao_prompt() -> str:
    """Identidade de tudo que determina o resultado, como hash curto.

    Serve para saber qual versão produziu cada tripla. Durante a calibração o
    prompt mudou várias vezes, e triplas de versões diferentes não são
    comparáveis — misturá-las no acervo sem marcação tornaria impossível saber
    se uma diferença veio da fonte ou da instrução.

    Calculado em vez de mantido à mão porque versão que depende de alguém
    lembrar de incrementar fica errada exatamente quando importa.
    """
    material = INSTRUCOES + json.dumps(
        {
            "schema": Extracao.model_json_schema(),
            # O filtro de rodapé muda o texto que chega ao modelo, logo muda o
            # resultado. Fora do hash, ajustar o filtro sem mexer no prompt
            # deixaria extrações incomparáveis com a mesma versão.
            "filtro": {
                "marcadores": boilerplate.MARCADORES,
                "min_ocorrencias": boilerplate.MIN_OCORRENCIAS,
                "min_dias": boilerplate.MIN_DIAS_DISTINTOS,
                "min_materias": boilerplate.MIN_MATERIAS,
                # Muda quantas sentenças o modelo vê, logo muda o resultado.
                # Fora do hash, extrações com corte diferente ficariam
                # comparáveis entre si sem serem comparáveis de fato.
                "max_sentencas": MAX_SENTENCAS,
            },
            # O esforco vem do modelo de extracao. Trocar de modelo muda a
            # versao do prompt junto, e isso esta certo: a configuracao que
            # produziu as triplas de fato mudou.
            "esforco": llm.EXTRACAO.esforco,
        },
        sort_keys=True, ensure_ascii=False, default=list)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


PROMPT_VERSAO = versao_prompt()


def corta_lide(sentencas: list[str],
               limite: int | None = MAX_SENTENCAS) -> list[str]:
    """Fica com as primeiras `limite` sentenças. Ver `MAX_SENTENCAS`.

    Corta do FIM, nunca do meio: o índice de cada sentença é gravado junto da
    tripla e é como a evidência volta ao texto de origem. Remover do meio
    renumeraria tudo o que vem depois e faria cada tripla apontar para a frase
    errada — sem erro nenhum, só citação trocada.
    """
    if limite is None:
        return sentencas
    return sentencas[:limite]


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


def descarta_vazias(triplas: list[Tripla]) -> tuple[list[Tripla], int]:
    """Separa as triplas que não afirmam nada. Devolve as boas e quantas caíram.

    Uma tripla precisa carregar ou um objeto ou um valor numérico. Sem nenhum
    dos dois — `(Fulano, afirmou, null)` — ela ocupa espaço no grafo sem dizer
    nada, e pior: parece uma afirmação registrada quando a afirmação se perdeu.

    A regra está no prompt, mas fica repetida aqui porque instrução é pedido e
    isto é garantia. A primeira versão da regra de atributo nulo vazou para a
    atribuição e produziu seis dessas numa única matéria.
    """
    boas = [t for t in triplas if t.objeto_canonico or t.valor_numero is not None]
    return boas, len(triplas) - len(boas)


def extrai(titulo: str, veiculo: str, data_pub: str | None,
           sentencas: list[str]) -> llm.Resposta:
    """Chama o modelo e devolve as triplas validadas mais o consumo da chamada."""
    return llm.gera(
        INSTRUCOES,
        monta_conteudo(titulo, veiculo, data_pub, sentencas),
        Extracao,
        modelo=llm.EXTRACAO,
    )


# ---------------------------------------------------------------- interface

def _por_id(conexao: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    """Matérias escolhidas a dedo, para extrair uma história inteira.

    Sem isto só dá para pegar as mais recentes, e a detecção de contradição
    precisa do oposto: as matérias que cobrem o MESMO fato em veículos
    diferentes, que raramente são as últimas publicadas.
    """
    marcadores = ",".join("?" * len(ids))
    linhas = conexao.execute(
        f"""
        SELECT id, veiculo, editoria, titulo, resumo, conteudo,
               data_publicacao, url_norm
        FROM artigos WHERE id IN ({marcadores})
        """,
        ids,
    ).fetchall()
    por_id = {linha["id"]: linha for linha in linhas}
    return [por_id[i] for i in ids if i in por_id]


MIN_SIMILARIDADE = 0.70
"""Proximidade semântica mínima entre dois títulos para valer o par.

Ver a justificativa e a medição em `_por_historia`. Roda no modelo local de
embedding — não custa chamada."""

MIN_TEXTO = 1200
"""Caracteres mínimos para uma matéria sustentar extração.

Veículo que só publica manchete no RSS não entra: 200 caracteres não dão
tripla, e a chamada seria desperdício.
"""


def _por_historia(conexao: sqlite3.Connection, quantas: int,
                  por_historia: int = 2) -> list[sqlite3.Row]:
    """Matérias escolhidas aos PARES, um veículo diferente em cada.

    É a seleção que faz o dinheiro render. `_materias` pega as mais recentes,
    e recência não tem relação nenhuma com corroboração: matéria de fonte única
    nunca vira confirmação, por mais nova que seja. Extrair uma delas gasta o
    mesmo e não move o número de fatos confirmados.

    O que move é PAR — dois veículos distintos cobrindo o mesmo fato. Por isso
    a história só entra se sobrarem dois veículos com texto suficiente depois
    de todos os filtros; história que não forma par é descartada inteira, e não
    parcialmente, porque metade de um par não corrobora nada.

    Já extraídas pelo modelo ativo ficam de fora, em qualquer versão de prompt:
    elas já estão no acervo e o grafo já as lê. Reextrair com o prompt novo
    melhoraria a qualidade delas, mas gastaria onde não há confirmação nova a
    ganhar — e é justamente o que este seletor existe para evitar.
    """
    ja_extraidas = {
        linha["artigo_id"] for linha in conexao.execute(
            "SELECT artigo_id FROM extracoes WHERE modelo = ?",
            (llm.EXTRACAO.id,))
    }

    escolhidas: list[int] = []
    for historia in agrupa.agrupa(agrupa.carrega(conexao)):
        com_texto = [m for m in sorted(historia.materias,
                                       key=lambda x: -x["tamanho"])
                     if m["tamanho"] > MIN_TEXTO]

        # Um por veículo, o de texto mais longo. Duas matérias do mesmo veículo
        # na mesma história são a mesma redação publicando duas vezes — pagar
        # pelas duas compra zero corroboração.
        por_veiculo: dict[str, sqlite3.Row] = {}
        for m in com_texto:
            por_veiculo.setdefault(m["veiculo"], m)

        # A elegibilidade olha o que o ACERVO terá, não só o que falta extrair.
        # Contar apenas as pendentes descartava a história em que um veículo já
        # foi extraído e o outro não — justamente o par que falta uma metade
        # para fechar, e o mais barato de completar. Custava uma chamada e
        # rendia uma confirmação nova; pulá-lo é o oposto do que este seletor
        # existe para fazer.
        if len(por_veiculo) < 2:
            continue

        pendentes = [m for m in por_veiculo.values()
                     if m["id"] not in ja_extraidas]
        prontas = [m for m in por_veiculo.values() if m["id"] in ja_extraidas]
        if not pendentes:
            continue

        # Segunda peneira, semântica. O agrupamento de `agrupa` casa termos do
        # título, e termo em comum não é assunto em comum: "Flávio e Lula
        # empatam no RS" e "Quaest em SC: Flávio Bolsonaro, 45%" compartilham
        # três termos e são pesquisas em ESTADOS diferentes. Extrair esse par
        # gasta duas chamadas e produz zero corroboração, porque as triplas
        # falam de coisas distintas.
        #
        # Medido em 8 pares propostos pelo critério léxico: os verdadeiros
        # ficaram entre 0,81 e 0,96, os falsos entre 0,43 e 0,58. Amostra
        # pequena — o limiar vai ter que se mover quando houver mais dados.
        #
        # A peneira erra para o lado seguro: um par verdadeiro rejeitado só
        # deixa de ser extraído nesta rodada; um par falso aceito é dinheiro
        # gasto sem retorno possível.
        #
        # O par conferido é sempre a pendente contra a sua contraparte — outra
        # pendente, ou a que já está no acervo. Comparar uma pendente consigo
        # mesma daria 1,0 e a peneira não filtraria nada.
        referencia = (prontas or pendentes[1:])
        if not referencia:
            continue
        if indice.similaridade(pendentes[0]["titulo"],
                               referencia[0]["titulo"]) < MIN_SIMILARIDADE:
            continue

        escolhidas.extend(m["id"] for m in pendentes[:por_historia])
        if len(escolhidas) >= quantas * por_historia:
            break

    return _por_id(conexao, escolhidas)


def _materias(conexao: sqlite3.Connection, limite: int) -> list[sqlite3.Row]:
    """Pega matérias com texto suficiente para sustentar extração, por recência.

    Ver `_por_historia` para a seleção que rende mais por dólar.
    """
    return conexao.execute(
        """
        SELECT a.id, a.veiculo, a.editoria, a.titulo, a.resumo, a.conteudo,
               a.data_publicacao, a.url_norm
        FROM artigos a
        WHERE MAX(LENGTH(a.conteudo), LENGTH(a.resumo)) > ?
          AND NOT EXISTS (
              SELECT 1 FROM extracoes e
              WHERE e.artigo_id = a.id
                AND e.modelo = ?
                AND e.prompt_versao = ?
          )
        ORDER BY a.data_publicacao DESC
        LIMIT ?
        """,
        (MIN_TEXTO, llm.EXTRACAO.id, PROMPT_VERSAO, limite),
    ).fetchall()


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Extrai triplas do acervo.")
    parser.add_argument("-n", type=int, default=5, help="quantas matérias")
    parser.add_argument(
        "--ids",
        help="ids de matérias, separados por vírgula. Extrai exatamente essas, "
             "na ordem dada, ignorando o filtro de tamanho — para processar uma "
             "história inteira em vez das mais recentes",
    )
    parser.add_argument(
        "--sentencas",
        type=int,
        metavar="N",
        default=MAX_SENTENCAS,
        help=f"quantas sentencas de cada materia chegam ao modelo "
             f"(padrao: {MAX_SENTENCAS}; 0 manda a materia inteira). "
             f"O corte e o maior lever de custo que existe aqui -- ver "
             f"MAX_SENTENCAS no codigo para a medicao",
    )
    parser.add_argument(
        "--historias",
        type=int,
        metavar="N",
        help="extrai as N maiores historias AOS PARES, um veiculo diferente "
             "em cada. E a selecao que rende mais por dolar: materia de fonte "
             "unica nunca vira confirmacao. Consome 2*N chamadas",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="mostra a requisição que seria enviada, sem chamar a API",
    )
    args = parser.parse_args()

    total_uso: list[llm.Uso] = []
    falhas: list[tuple[int, str]] = []

    conexao = conecta(config.BANCO)
    if args.ids:
        linhas = _por_id(conexao, [int(x) for x in args.ids.split(",")])
    elif args.historias:
        linhas = _por_historia(conexao, args.historias)
    else:
        linhas = _materias(conexao, args.n)
    if not linhas:
        print("Nenhuma matéria nova para extrair.")
        print(f"Tudo o que tem texto suficiente já foi processado por "
              f"{llm.EXTRACAO.id} com o prompt {PROMPT_VERSAO}.")
        print("Colete mais, ou mude o prompt — a versão muda junto e libera "
              "reprocessamento.")
        sys.exit(0)

    if not args.dry_run:
        print(f"Provedor: {llm.descricao(llm.EXTRACAO)}\n")

    # Frases institucionais por veículo, calculadas uma vez e reaproveitadas.
    # Percorrer o acervo inteiro por matéria seria lento sem ganho nenhum.
    repetidas: dict[str, set[str]] = {}

    for i, linha in enumerate(linhas, 1):
        texto = max(linha["conteudo"], linha["resumo"], key=len)
        veiculo = linha["veiculo"]

        if veiculo not in repetidas:
            repetidas[veiculo] = boilerplate.frases_repetidas(
                conexao, veiculo, em_sentencas)

        sentencas, removidas = boilerplate.filtra(
            em_sentencas(texto), repetidas[veiculo])
        inteiro = len(sentencas)
        sentencas = corta_lide(sentencas, args.sentencas or None)

        print(f"\n{'=' * 78}")
        print(f"[{i}/{len(linhas)}] {linha['veiculo']} / {linha['editoria']}")
        print(f"  {linha['titulo'][:70]}")
        cortadas = inteiro - len(sentencas)
        print(f"  {len(texto)} caracteres → {len(sentencas)} sentenças"
              + (f" ({len(removidas)} institucionais fora)" if removidas else "")
              # Impresso porque o corte muda o que o modelo pode achar, e filtro
              # que corta em silêncio não pode ser conferido.
              + (f" · {cortadas} depois do lide não enviadas" if cortadas else ""))
        # Impresso porque filtro que corta em silêncio não pode ser conferido,
        # e este corta antes de o texto chegar ao modelo.
        for r in removidas:
            print(f"      fora: {r[:96]}")

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

        # Falha numa matéria não derruba o lote. As anteriores já estão
        # gravadas, e abortar deixaria as seguintes por extrair sem motivo.
        try:
            resultado = extrai(
                linha["titulo"], linha["veiculo"],
                linha["data_publicacao"], sentencas,
            )
        except llm.FalhaNoModelo as erro:
            falhas.append((linha["id"], str(erro)))
            print(f"  FALHOU: {erro}")
            continue
        boas, vazias = descarta_vazias(resultado.dados.triplas)
        resultado.dados.triplas[:] = boas

        aviso = f" · {vazias} vazias descartadas" if vazias else ""
        print(f"  {len(boas)} triplas{aviso} · {resultado.uso}")
        total_uso.append(resultado.uso)

        # Agrupado por sentenca de origem. O lide jornalistico brasileiro
        # empacota muitos fatos numa frase so -- a abertura de uma das materias
        # rendeu dez triplas --, e imprimir a frase sob cada uma repetia o mesmo
        # texto dez vezes. Agrupado, ve-se o que cada frase de fato produziu.
        #
        # A frase aparece so aqui, na avaliacao: o que vai para o banco continua
        # sendo o indice. Mas sem ela na tela nao ha como julgar se a tripla
        # esta certa, e julgar sem ler a fonte e o erro que o proprio campo
        # INFERRED existe para evitar.
        # Grava antes de imprimir: chamada paga que nao persiste e dinheiro perdido.
        salva_extracao(
            conexao, linha["id"], resultado.dados.triplas,
            llm.EXTRACAO.id, PROMPT_VERSAO, VOCAB_VERSAO, resultado.uso,
        )

        por_sentenca: dict[int, list[Tripla]] = {}
        for t in resultado.dados.triplas:
            por_sentenca.setdefault(t.sentenca, []).append(t)

        for idx in sorted(por_sentenca):
            frase = sentencas[idx] if 0 <= idx < len(sentencas) else "(fora da materia)"
            corte = frase[:165] + ("..." if len(frase) > 165 else "")
            print()
            print(f'    [{idx}] "{corte}"')

            for t in por_sentenca[idx]:
                marca = " " if t.origem == "EXTRACTED" else "~"
                alvo = t.objeto_canonico or "—"
                print(f"      {marca} ({t.sujeito_canonico}, {t.relacao}, {alvo})")

                meta = f"          {t.tipo_relacao} · {t.origem} · fato: {t.data_fato}"
                if t.valor_numero is not None:
                    valor = f"{t.valor_numero:g} {t.valor_unidade or ''}".strip()
                    if t.valor_contexto:
                        valor += f" ({t.valor_contexto})"
                    meta += f" · valor: {valor}"
                print(meta)


    print(f"\n{'=' * 78}")
    if args.dry_run:
        print("Nada foi enviado. Para rodar de verdade, preencha "
              "ANTHROPIC_API_KEY no .env e remova --dry-run.")
    elif total_uso:
        custo = sum(u.custo for u in total_uso)
        entrada = sum(u.entrada + u.cache_leitura + u.cache_escrita
                      for u in total_uso)
        saida = sum(u.saida for u in total_uso)
        print(f"{len(total_uso)} matérias · {entrada} tokens de entrada · "
              f"{saida} de saída")
        print(f"US$ {custo:.4f} nesta rodada · "
              f"US$ {custo / len(total_uso):.4f} por matéria")

        # A fracao em `outro` e o sinal de que o vocabulario precisa crescer.
        # Sem medir isso, a lista fechada congela no que alguem imaginou uma
        # vez, e o que nao coube desaparece sem deixar rastro.
        fora = conexao.execute(
            "SELECT COUNT(*) FROM triplas t JOIN extracoes e ON e.id = t.extracao_id "
            "WHERE e.vocab_versao = ? AND t.relacao = 'outro'", (VOCAB_VERSAO,)
        ).fetchone()[0]
        no_vocab = conexao.execute(
            "SELECT COUNT(*) FROM triplas t JOIN extracoes e ON e.id = t.extracao_id "
            "WHERE e.vocab_versao = ?", (VOCAB_VERSAO,)
        ).fetchone()[0]

        t = estatisticas_triplas(conexao)
        print(f"\nAcervo de triplas: {t['triplas']} triplas de {t['materias']} "
              f"matérias · {t['relacoes']} relações distintas · "
              f"{t['entidades']} entidades")
        # "Gravado", nao "total": o banco so registra extracao que deu certo.
        # Chamada truncada e cobrada e nao grava nada -- a do Braskem queimou
        # ~US$ 0,20 sozinha --, e o check.py nao persiste uso nenhum. Este
        # numero e piso. A fatura esta no console.
        print(f"Custo gravado (so extracao): US$ {t['custo']:.4f} · "
              f"prompt {PROMPT_VERSAO} · vocabulário v{VOCAB_VERSAO}")
        if no_vocab:
            print(f"Em 'outro': {fora} de {no_vocab} "
                  f"({100 * fora / no_vocab:.0f}%) — se subir, a lista precisa "
                  f"de relação nova")

    conexao.close()


if __name__ == "__main__":
    main()
