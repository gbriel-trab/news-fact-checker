"""Extração de afirmações como triplas.

Primeira chamada de LLM do projeto. Recebe uma matéria segmentada em sentenças
e devolve as afirmações que ela faz, estruturadas.

    python -m src.extract --dry-run -n 5     # mostra o que seria enviado
    python -m src.extract -n 5               # roda de verdade (exige chave)

A relação vem da lista fechada de `vocabulario`, imposta como enum no schema.
A fase de relação livre (vocabulário zero) existiu só para derivar a lista de
dado real — o histórico está no docstring de `vocabulario`.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import agrupa, boilerplate, config, indice, llm, vocabulario
from .vocabulario import Relacao
from .segment import em_sentencas
from .storage import (
    conecta, estatisticas_triplas, orfas, salva_extracao)

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

    @model_validator(mode="after")
    def _objeto_e_canonico_andam_juntos(self) -> "Tripla":
        """Preenche `objeto_canonico` quando o modelo mandou só `objeto`.

        O schema não tem como impor os dois campos em conjunto, e o modelo às
        vezes preenche um só. `descarta_vazias` olha apenas o canônico, então
        a tripla com objeto e sem canônico morria em silêncio — medido em
        29/08/2026: o fato principal de duas matérias parceiras de par caiu
        como "vazia" exatamente assim. Completar preserva a afirmação paga;
        descartar jogava fora o que o sistema existe para guardar.

        Tensão assumida com "normalização é na leitura, nunca no registro"
        (ver `canonico`): isto grava no registro. A diferença que a torna
        aceitável é que nada é interpretado — o campo ausente recebe o TEXTO
        que o próprio modelo devolveu no campo irmão, na mesma resposta. A
        alternativa, medida, era perder o fato.
        """
        if self.objeto and not self.objeto_canonico:
            self.objeto_canonico = self.objeto
        return self


class Extracao(BaseModel):
    triplas: list[Tripla]


# ------------------------------------------------------------ modo história

ROTULOS_FONTE = "ABCDEFG"
MAX_FONTES = len(ROTULOS_FONTE)
"""Matérias por chamada no modo história. Sete é o máximo testado
(experimento de 01/09/2026, história do incêndio): atribuição graduada
saiu limpa. Acima disso, sem medição."""


class Origem(BaseModel):
    """De onde uma tripla saiu: qual matéria, qual sentença."""

    fonte: str = Field(
        description='A letra da matéria que afirma: "A", "B"...')
    sentenca: int = Field(
        description="Índice da sentença numerada DAQUELA matéria.")


class TriplaHistoria(BaseModel):
    """Tripla do modo história: os mesmos campos da `Tripla`, trocando o
    índice único de sentença por `origens` — uma entrada por matéria que
    afirma o fato. É o que torna a corroboração auditável: código barato
    confere que cada fonte apontada de fato contém a sentença.

    Definida por inteiro em vez de herdar: a `Tripla` exige `sentenca`, que
    aqui não existe — e schema explícito é mais honesto que herança com
    campo morto."""

    sujeito: str = Field(description="Entidade como apareceu no texto.")
    sujeito_canonico: str = Field(
        description=(
            "Nome canônico e completo da entidade, sem cargo nem artigo."
        )
    )
    relacao: Relacao = Field(
        description=(
            "A relação, da lista fechada. `outro` quando nenhuma servir."
        )
    )
    objeto: str | None = Field(
        description="Segunda entidade como apareceu, ou null em atributo.")
    objeto_canonico: str | None = Field(
        description="Nome canônico da segunda entidade. null junto de objeto.")
    tipo_relacao: Literal["evento", "estado"] = Field(
        description="'evento' = instante; 'estado' = intervalo.")
    origem: Literal["EXTRACTED", "INFERRED"] = Field(
        description="EXTRACTED explícito no texto; INFERRED deduzido.")
    valor_numero: float | None = Field(
        description="O número puro quando houver quantidade, senão null.")
    valor_unidade: str | None = Field(
        description="Unidade curta ('%', 'BRL', 'votos'), ou null.")
    valor_contexto: str | None = Field(
        description="O que o número mede, curto, ou null.")
    data_fato: str | None = Field(
        description="Quando o fato ocorreu (AAAA-MM-DD/AAAA-MM/AAAA) ou null.")
    origens: list[Origem] = Field(
        description=(
            "Uma entrada por matéria que AFIRMA este fato, com a sentença. "
            "Inclua uma matéria APENAS se ela de fato o afirma."
        )
    )

    @model_validator(mode="after")
    def _objeto_e_canonico_andam_juntos(self) -> "TriplaHistoria":
        if self.objeto and not self.objeto_canonico:
            self.objeto_canonico = self.objeto
        return self


class ExtracaoHistoria(BaseModel):
    mesma_historia: bool = Field(
        description=(
            "true se as matérias tratam do MESMO fato central. false se o "
            "agrupamento errou e elas falam de coisas distintas — nesse "
            "caso devolva triplas vazio."
        )
    )
    triplas: list[TriplaHistoria]


ADENDO_HISTORIA = """

MODO HISTÓRIA: você receberá VÁRIAS matérias sobre a mesma história,
etiquetadas [A], [B], [C]... Extraia as triplas seguindo TODAS as regras
acima, e mais três:

11. ORIGENS. Cada tripla lista `origens`: uma entrada {fonte, sentenca} por
    matéria que AFIRMA aquele fato, apontando a sentença exata daquela
    matéria. Inclua uma matéria SOMENTE se ela de fato afirma — atribuir a
    quem não disse é o erro mais grave deste modo.

12. UM NOME SÓ. Todas as matérias estão diante de você. A mesma entidade,
    o mesmo evento e o mesmo ato recebem EXATAMENTE o mesmo nome canônico
    em todas as triplas — "incêndio na casa de X" e "incêndio na residência
    de X" são o MESMO evento. Se várias matérias afirmam o mesmo fato,
    faça UMA tripla com várias origens, nunca cópias.

13. HISTÓRIA ERRADA SE DECLARA. Se as matérias NÃO tratam do mesmo fato
    central, devolva mesma_historia=false e triplas vazio — o agrupamento
    errou, e dizer isso vale mais que extrair um par falso.
"""


def monta_conteudo_historia(
        blocos: list[tuple[sqlite3.Row, list[str]]]) -> str:
    """A parte variável do modo história: cada matéria com seu rótulo e
    suas sentenças numeradas por fonte ([A0], [A1], [B0]...)."""
    partes = []
    for rotulo, (linha, sentencas) in zip(ROTULOS_FONTE, blocos):
        numeradas = "\n".join(f"[{rotulo}{i}] {s}"
                              for i, s in enumerate(sentencas))
        partes.append(
            f"MATÉRIA {rotulo} — Veículo: {linha['veiculo']} · "
            f"Data de publicação: {linha['data_publicacao'] or 'desconhecida'}"
            f" · Título: {linha['titulo']}\n{numeradas}")
    return "\n\n".join(partes)


def valida_origens(
        triplas: list[TriplaHistoria],
        n_sentencas: dict[str, int]) -> tuple[list[TriplaHistoria], int]:
    """Descarta origem que aponta fonte ou sentença inexistente, e tripla
    que ficar sem origem válida. É a trava contra corroboração fabricada:
    `origens` é o modelo AFIRMANDO que cada fonte disse — índice inválido é
    afirmação sem lastro e cai antes de virar registro. (A verificação
    semântica — a sentença sustenta o conteúdo? — fica registrada como
    evolução; esta é a estrutural.)"""
    boas: list[TriplaHistoria] = []
    descartadas = 0
    for t in triplas:
        validas = [o for o in t.origens
                   if o.fonte in n_sentencas
                   and 0 <= o.sentenca < n_sentencas[o.fonte]]
        if validas:
            t.origens[:] = validas
            boas.append(t)
        else:
            descartadas += 1
    return boas, descartadas


def extrai_historia(blocos: list[tuple[sqlite3.Row, list[str]]]
                    ) -> llm.Resposta:
    """Uma chamada para a história inteira. Ver o registro no ARCHITECTURE:
    com as matérias no mesmo contexto, o modelo nomeia o evento uma vez —
    a convergência que chamadas isoladas não têm como entregar."""
    return llm.gera(
        INSTRUCOES + ADENDO_HISTORIA,
        monta_conteudo_historia(blocos),
        ExtracaoHistoria,
        modelo=llm.EXTRACAO,
    )


def _tripla_da_fonte(t: TriplaHistoria, sentenca: int) -> Tripla:
    return Tripla(
        sujeito=t.sujeito, sujeito_canonico=t.sujeito_canonico,
        relacao=t.relacao, objeto=t.objeto,
        objeto_canonico=t.objeto_canonico, tipo_relacao=t.tipo_relacao,
        origem=t.origem, valor_numero=t.valor_numero,
        valor_unidade=t.valor_unidade, valor_contexto=t.valor_contexto,
        data_fato=t.data_fato, sentenca=sentenca,
    )


def salva_historia(conexao: sqlite3.Connection,
                   blocos: list[tuple[sqlite3.Row, list[str]]],
                   triplas: list[TriplaHistoria],
                   uso: llm.Uso, prompt_versao: str) -> None:
    """Explode a extração da história em linhas POR FONTE, no formato que o
    banco já conhece: cada origem vira uma tripla comum presa ao artigo e à
    sentença dela. Grafo, índice, digest e check não mudam uma linha — a
    corroboração aparece como triplas idênticas de artigos distintos, que
    agora casam porque nasceram na mesma chamada. O custo é rateado por
    igual entre os artigos: a soma bate com a fatura."""
    n = len(blocos)
    for i, (rotulo, (linha, _)) in enumerate(zip(ROTULOS_FONTE, blocos)):
        # História que ganhou membro novo é re-extraída inteira (é a
        # releitura que faz os nomes convergirem) — a linha anterior da
        # MESMA versão sai antes, senão a UNIQUE derruba a rodada. A
        # substituição é explícita e restrita à versão de história: o
        # UNIQUE continua protegendo contra pagamento duplo acidental.
        conexao.execute(
            "DELETE FROM extracoes WHERE artigo_id = ? AND modelo = ? "
            "AND prompt_versao = ?",
            (linha["id"], llm.EXTRACAO.id, prompt_versao))
        do_artigo = [
            _tripla_da_fonte(t, o.sentenca)
            for t in triplas for o in t.origens if o.fonte == rotulo
        ]
        # Último leva o resto da divisão: a soma dos rateios = fatura.
        rateio = llm.Uso(
            modelo=uso.modelo,
            entrada=uso.entrada // n + (uso.entrada % n if i == n - 1 else 0),
            saida=uso.saida // n + (uso.saida % n if i == n - 1 else 0),
            cache_leitura=uso.cache_leitura // n
            + (uso.cache_leitura % n if i == n - 1 else 0),
            cache_escrita=uso.cache_escrita // n
            + (uso.cache_escrita % n if i == n - 1 else 0),
        )
        salva_extracao(conexao, linha["id"], do_artigo,
                       llm.EXTRACAO.id, prompt_versao, VOCAB_VERSAO, rateio)


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

{vocabulario.resumo_para_prompt()}

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

   E VALOR CITADO DENTRO DE PERÍODO NARRADO recebe a data do período, nunca
   a da publicação. Caso real que virou divergência falsa no acervo:

   Matéria de 25/08: "os ETFs somaram captações entre 14 e 22 de julho...
                      o Bitcoin era negociado perto de US$ 65.500"
   Errado: valor 65500 · data_fato 2026-08-25   ← data da publicação
   Certo:  valor 65500 · data_fato 2026-07-22   ← o preço é DE JULHO

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

   E a recíproca fecha a regra: VALOR SEM OBJETO É SEMPRE `tem_atributo`.
   Nunca `outro`. Dois veículos publicaram o mesmo lucro da Caixa, um recebeu
   `tem_atributo` e o outro `outro`, e o fato deixou de ser o mesmo fato — a
   confirmação sumiu sem erro nenhum aparecer.

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


def versao_prompt(max_sentencas: int | None = MAX_SENTENCAS) -> str:
    """Identidade de tudo que determina o resultado, como hash curto.

    Serve para saber qual versão produziu cada tripla. Durante a calibração o
    prompt mudou várias vezes, e triplas de versões diferentes não são
    comparáveis — misturá-las no acervo sem marcação tornaria impossível saber
    se uma diferença veio da fonte ou da instrução.

    Calculado em vez de mantido à mão porque versão que depende de alguém
    lembrar de incrementar fica errada exatamente quando importa.

    O corte efetivo entra como PARÂMETRO porque o CLI pode mudá-lo por rodada
    (`--sentencas`). Hashear só a constante gravava corte diferente sob a
    mesma versão — exatamente a incomparabilidade silenciosa que este hash
    existe para impedir. O `main` recalcula a versão quando o argumento
    diverge do padrão.
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
                "max_sentencas": max_sentencas,
            },
            # O esforco vem do modelo de extracao. Trocar de modelo muda a
            # versao do prompt junto, e isso esta certo: a configuracao que
            # produziu as triplas de fato mudou.
            "esforco": llm.EXTRACAO.esforco,
        },
        sort_keys=True, ensure_ascii=False, default=list)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


PROMPT_VERSAO = versao_prompt()


def versao_prompt_historia(max_sentencas: int | None = MAX_SENTENCAS) -> str:
    """Versão do prompt do modo história — hash próprio, pelo mesmo motivo
    do `versao_prompt`: instruções e schema diferentes produzem triplas que
    não se comparam com as do modo matéria sem marca que as distinga."""
    material = INSTRUCOES + ADENDO_HISTORIA + json.dumps(
        {
            "schema": ExtracaoHistoria.model_json_schema(),
            "filtro": {
                "marcadores": boilerplate.MARCADORES,
                "min_ocorrencias": boilerplate.MIN_OCORRENCIAS,
                "min_dias": boilerplate.MIN_DIAS_DISTINTOS,
                "min_materias": boilerplate.MIN_MATERIAS,
                "max_sentencas": max_sentencas,
            },
            "esforco": llm.EXTRACAO.esforco,
        },
        sort_keys=True, ensure_ascii=False, default=list)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


PROMPT_VERSAO_HISTORIA = versao_prompt_historia()


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

def _roda_historias(conexao: sqlite3.Connection, grupos, args,
                    limite_lide, prompt_versao: str) -> list[llm.Uso]:
    """Executa o modo história: uma chamada por grupo, gravação explodida
    por fonte. Falha numa história não derruba o lote."""
    usos: list[llm.Uso] = []
    repetidas: dict[str, set[str]] = {}
    for n, grupo in enumerate(grupos, 1):
        blocos: list[tuple[sqlite3.Row, list[str]]] = []
        print(f"\n{'=' * 78}")
        print(f"HISTÓRIA {n}/{len(grupos)} — {len(grupo)} veículos")
        for linha in grupo:
            v = linha["veiculo"]
            if v not in repetidas:
                repetidas[v] = boilerplate.frases_repetidas(
                    conexao, v, em_sentencas)
            sentencas, removidas = boilerplate.filtra(
                em_sentencas(max(linha["conteudo"], linha["resumo"],
                                 key=len)),
                repetidas[v])
            sentencas = corta_lide(sentencas, limite_lide)
            rotulo = ROTULOS_FONTE[len(blocos)]
            blocos.append((linha, sentencas))
            print(f"  [{rotulo}] {v}: {len(sentencas)} sentenças · "
                  f"{linha['titulo'][:58]}")
            for r in removidas:
                print(f"      fora: {r[:90]}")

        if args.dry_run:
            conteudo = monta_conteudo_historia(blocos)
            print(f"  ~{(len(INSTRUCOES) + len(ADENDO_HISTORIA) + len(conteudo)) // 4} "
                  f"tokens de entrada")
            if n == 1:
                print(f"\n--- user (variável) ---\n{conteudo[:1200]}\n[...]")
            continue

        try:
            resultado = extrai_historia(blocos)
        except llm.FalhaNoModelo as erro:
            print(f"  FALHOU: {erro}")
            continue
        usos.append(resultado.uso)

        if not resultado.dados.mesma_historia:
            # O agrupamento errou e o modelo disse — grava linhas vazias
            # para a história não voltar, e o caso realimenta a calibração.
            print("  MESMA_HISTORIA=FALSE — o modelo recusou o grupo. "
                  "Gravado vazio; conferir o agrupamento.")
            salva_historia(conexao, blocos, [], resultado.uso, prompt_versao)
            continue

        n_sentencas = {ROTULOS_FONTE[i]: len(s)
                       for i, (_, s) in enumerate(blocos)}
        validas, invalidas = valida_origens(resultado.dados.triplas,
                                            n_sentencas)
        antes = len(validas)
        validas = [t for t in validas
                   if t.objeto_canonico or t.valor_numero is not None]
        # Contado e impresso como no modo matéria: filtro que descarta em
        # silêncio não pode ser conferido.
        vazias = antes - len(validas)
        salva_historia(conexao, blocos, validas, resultado.uso,
                       prompt_versao)

        aviso = ((f" · {invalidas} com origens inválidas fora"
                  if invalidas else "")
                 + (f" · {vazias} vazias descartadas" if vazias else ""))
        print(f"  {len(validas)} triplas{aviso} · {resultado.uso}")
        for t in sorted(validas, key=lambda x: -len(x.origens)):
            fontes = "".join(sorted(o.fonte for o in t.origens))
            alvo = t.objeto_canonico or "—"
            valor = (f" = {t.valor_numero:g} {t.valor_unidade or ''}"
                     if t.valor_numero is not None else "")
            print(f"    [{fontes:>7}] ({t.sujeito_canonico}, {t.relacao}, "
                  f"{alvo}){valor}")
    return usos


def extrai_grupo(conexao: sqlite3.Connection,
                 linhas: list[sqlite3.Row],
                 limite_lide: int | None = MAX_SENTENCAS,
                 ) -> tuple[int, float, bool]:
    """Extrai um grupo de matérias como UMA história e grava. Devolve
    (triplas válidas, custo em US$, recusada) — `recusada` é o
    mesma_historia=false, que quem chama pode querer tratar (a demanda
    re-tenta a melhor candidata sozinha; o lote só registra).

    É o caminho do modo história sem a cerimônia do CLI, para a extração
    sob demanda (`demanda`): o chamador já escolheu as matérias; aqui é
    montar os blocos, chamar o modelo uma vez e gravar explodido por
    fonte — mesmas regras, mesma validação e mesma versão de prompt do
    `_roda_historias`. Grupo de UMA matéria também passa por aqui: o
    formato de história com uma fonte só é válido, e manter um caminho
    único evita duas gravações com semânticas diferentes.

    `mesma_historia=false` grava os marcadores vazios e devolve 0 triplas
    — o grupo não volta a ser candidato, igual ao comportamento do lote.
    """
    repetidas: dict[str, set[str]] = {}
    blocos: list[tuple[sqlite3.Row, list[str]]] = []
    for linha in linhas[:MAX_FONTES]:
        v = linha["veiculo"]
        if v not in repetidas:
            repetidas[v] = boilerplate.frases_repetidas(
                conexao, v, em_sentencas)
        sentencas, _ = boilerplate.filtra(
            em_sentencas(max(linha["conteudo"], linha["resumo"], key=len)),
            repetidas[v])
        blocos.append((linha, corta_lide(sentencas, limite_lide)))

    resultado = extrai_historia(blocos)
    if not resultado.dados.mesma_historia:
        salva_historia(conexao, blocos, [], resultado.uso,
                       PROMPT_VERSAO_HISTORIA)
        return 0, resultado.uso.custo, True

    n_sentencas = {ROTULOS_FONTE[i]: len(s) for i, (_, s) in enumerate(blocos)}
    validas, _ = valida_origens(resultado.dados.triplas, n_sentencas)
    validas = [t for t in validas
               if t.objeto_canonico or t.valor_numero is not None]
    salva_historia(conexao, blocos, validas, resultado.uso,
                   PROMPT_VERSAO_HISTORIA)
    return len(validas), resultado.uso.custo, False


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


EDITORIAS_DURAS: frozenset[str] = frozenset(
    {"Política", "Economia", "Poder", "Mercado", "Mercados", "Comunicados"})
"""Editorias cujo material sustenta verificação, para `--editorias duras`.

A editoria é o único rótulo de assunto que o acervo já tem, e ele vem da
própria redação — melhor que qualquer heurística nossa. Medido nas 113
histórias corroboradas do acervo:

    sem filtro                    113
    só editoria dura               84  (74%)
    por palavra-chave no título    30  (27%)

O filtro por palavra-chave foi testado e DESCARTADO: derruba 73% e leva junto
o que interessa — "JHC e Renan Filho empatam em Alagoas", "MPT processa Uber
por R$ 321 milhões", "Meta paga US$ 18 bi". Título de notícia é feito de nome
próprio, não de palavra de categoria.

O que a editoria descarta é notícia mole de verdade: Dolly Parton, Neymar,
Harry e Meghan, Trump querendo renomear o Lago Ontário.

LIMITAÇÃO: `Mundo` e `Geral` são sacos de gato — carregam a guerra tarifária
junto com celebridade. Ficam de fora inteiras, e com elas cai notícia dura de
exterior. É corte grosseiro, e por isso opcional em vez de padrão."""

# A peneira de 0,70 sobre títulos morreu na v3, por medição (01/09/2026):
# nos 117 pares-ouro do acervo, 74 reprovavam nela. O papel dela — impedir
# par falso — passou a ser feito em três camadas melhores: o agrupamento já
# é semântico (agrupa.LIMIAR_SEMANTICO, calibrado no ouro), o modelo declara
# mesma_historia=false quando o grupo veio errado (regra 13), e as origens
# por fonte são validadas na volta.

MIN_SENTENCAS = 2
"""Sentenças mínimas para uma matéria sustentar extração.

ERA UM PISO DE 1200 CARACTERES, e ele ficou errado quando o corte no lide
entrou. O piso de caracteres foi escrito quando a matéria inteira ia para o
modelo: ali, texto curto significava pouco material. Depois do corte, só vão
5 sentenças de qualquer forma — uma matéria de 280 caracteres manda 2 em vez
de 5, gasta MENOS, e o que ela manda é justamente o lide.

E o lide é onde a corroboração vive: medimos que 84% das triplas úteis estão
nas sentenças 0 a 2. Um RSS que entrega só o lide não é a pior forma de
matéria para este sistema — é a ideal.

MEDIDO no acervo, matérias que o piso antigo excluía e este aceita:

    Folha           216 de 518    2,2 sentenças, 100% com número
    Exame            18 de 28
    BBC Brasil       13 de 55
    UOL               6 de 49
    Cointelegraph     3 de 30
    Carta Capital     3 de 20

A Folha é o destravamento: segundo maior veículo do acervo, e fazia par com
todo mundo sem poder virar evidência.

Duas sentenças e não uma: com uma só, metade das matérias do UOL e do
Cointelegraph entrariam trazendo um fato solto sem contexto de data ou
entidade, e tripla sem contexto não corrobora nem contradiz."""


def _historias_para_extrair(
        conexao: sqlite3.Connection, quantas: int,
        editorias: frozenset[str] | None = None,
        prompt_versao: str = PROMPT_VERSAO_HISTORIA
) -> list[list[sqlite3.Row]]:
    """Histórias inteiras para o modo história, uma chamada cada.

    A seleção que faz o dinheiro render, agora na unidade certa: a HISTÓRIA.
    Devolve grupos de matérias — um veículo por matéria, até MAX_FONTES —
    que vão juntas num prompt só. A corroboração nasce dentro da chamada
    (ver o registro de 01/09/2026 no ARCHITECTURE), então:

    * O piso de 2 sentenças vale para a HISTÓRIA (ao menos uma matéria com
      substância), não para cada membro — no modo história, a matéria de 1
      sentença contribui lida no contexto das outras. Medido: Folha e Exame
      de 1 sentença entraram com atribuição correta no teste das 7 fontes.
    * Membro já extraído em modo matéria ENTRA de novo: a releitura no
      contexto da história é o que faz os nomes convergirem, e a linha nova
      supera a antiga no grafo (que lê MAX(id) por artigo).
    * História onde TODOS os membros já passaram por esta versão do modo
      história fica de fora — inclusive as marcadas mesma_historia=false,
      que gravam linhas vazias exatamente para não voltarem.
    """
    ja_nesta_versao = {
        linha["artigo_id"] for linha in conexao.execute(
            "SELECT artigo_id FROM extracoes WHERE modelo = ? "
            "AND prompt_versao = ?", (llm.EXTRACAO.id, prompt_versao))
    }

    grupos: list[list[sqlite3.Row]] = []
    for historia in agrupa.agrupa(agrupa.carrega(conexao)):
        # Ao menos UMA matéria da editoria pedida: se a Folha cobriu em
        # Mercado e a CNN no Geral, é o mesmo assunto.
        if editorias and not any(m["editoria"] in editorias
                                 for m in historia.materias):
            continue

        # Um por veículo (o texto mais longo), com ao menos 1 sentença. O
        # boilerplate não é removido aqui — o filtro de verdade roda na
        # extração; errar para mais custa tokens, não conclusão.
        por_veiculo: dict[str, tuple[sqlite3.Row, int]] = {}
        for m in sorted(historia.materias, key=lambda x: -x["tamanho"]):
            n = len(em_sentencas(max(m["conteudo"], m["resumo"], key=len)))
            if n >= 1:
                por_veiculo.setdefault(m["veiculo"], (m, n))

        pares = list(por_veiculo.values())
        membros_n = pares[:MAX_FONTES]
        if len(membros_n) < 2:
            continue
        # Substância DEPOIS do corte (achado da revisão de 01/09/2026): o
        # corte por tamanho — caracteres, não sentenças — podia deixar a
        # única matéria de 2+ sentenças de fora, e a âncora checada no
        # grupo inteiro aprovava um time só de manchetes. Se a âncora
        # existe mas caiu no corte, ela troca com o último cortado.
        if not any(n >= 2 for _, n in membros_n):
            ancora = next(((m, n) for m, n in pares[MAX_FONTES:]
                           if n >= 2), None)
            if ancora is None:
                continue
            membros_n[-1] = ancora
        membros = [m for m, _ in membros_n]
        if all(m["id"] in ja_nesta_versao for m in membros):
            continue

        grupos.append(membros)
        if len(grupos) >= quantas:
            break

    return grupos


def _materias(conexao: sqlite3.Connection, limite: int,
              prompt_versao: str = PROMPT_VERSAO) -> list[sqlite3.Row]:
    """Pega matérias com texto suficiente para sustentar extração, por recência.

    Ver `_por_historia` para a seleção que rende mais por dólar.
    """
    return conexao.execute(
        """
        SELECT a.id, a.veiculo, a.editoria, a.titulo, a.resumo, a.conteudo,
               a.data_publicacao, a.url_norm
        FROM artigos a
        -- Prefiltro barato: corta manchete pura sem segmentar nada. O piso
        -- de sentenças é aplicado em `_por_historia`; aqui, com ORDER BY
        -- data e LIMIT, segmentar o acervo inteiro seria pior que o ganho.
        WHERE MAX(LENGTH(a.conteudo), LENGTH(a.resumo)) > ?
          -- Exclui também o que o MODO HISTÓRIA já leu (achado da revisão
          -- de 01/09/2026): sem isso, um -n rotineiro depois de --historias
          -- re-pagava os mesmos artigos E a extração isolada, com id maior,
          -- SUPERAVA a convergida no grafo — desfazendo o que a chamada
          -- conjunta pagou para criar.
          AND NOT EXISTS (
              SELECT 1 FROM extracoes e
              WHERE e.artigo_id = a.id
                AND e.modelo = ?
                AND e.prompt_versao IN (?, ?)
          )
        ORDER BY a.data_publicacao DESC
        LIMIT ?
        """,
        (150, llm.EXTRACAO.id, prompt_versao, PROMPT_VERSAO_HISTORIA,
         limite),
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
        "--editorias",
        metavar="LISTA",
        help="filtra por editoria: 'duras' usa "
             + ", ".join(sorted(EDITORIAS_DURAS))
             + "; ou passe a sua lista separada por virgula. Sem isto, nao "
               "filtra. So vale com --historias",
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
    if args.sentencas < 0:
        # `sentencas[:-1]` cortaria do FIM — exatamente o que corta_lide
        # existe para nunca fazer (achado da revisão de 01/09/2026).
        parser.error("--sentencas não aceita valor negativo")

    total_uso: list[llm.Uso] = []
    falhas: list[tuple[int, str]] = []

    # O corte efetivo desta rodada define a versão do prompt gravada. Com o
    # padrão, é a constante já calculada; com --sentencas, recalcula — gravar
    # corte diferente sob a mesma versão tornaria as triplas incomparáveis
    # sem nenhum sinal disso no banco.
    limite_lide = args.sentencas or None
    prompt_versao = (PROMPT_VERSAO if limite_lide == MAX_SENTENCAS
                     else versao_prompt(limite_lide))

    conexao = conecta(config.BANCO)
    if args.historias:
        # Modo história (v3): a unidade é o grupo, não a matéria. Fluxo
        # próprio, gravação explodida por fonte, e o resto do main não roda.
        if not args.editorias:
            editorias = None
        elif args.editorias == "duras":
            editorias = EDITORIAS_DURAS
        else:
            editorias = frozenset(e.strip() for e in
                                  args.editorias.split(",") if e.strip())
        versao_h = (PROMPT_VERSAO_HISTORIA
                    if limite_lide == MAX_SENTENCAS
                    else versao_prompt_historia(limite_lide))
        grupos = _historias_para_extrair(conexao, args.historias,
                                         editorias=editorias,
                                         prompt_versao=versao_h)
        if not grupos:
            print("Nenhuma história nova para extrair nesta janela.")
            sys.exit(0)
        if not args.dry_run:
            print(f"Provedor: {llm.descricao(llm.EXTRACAO)}\n")
        usos = _roda_historias(conexao, grupos, args, limite_lide, versao_h)
        if usos:
            total = sum(u.custo for u in usos)
            print(f"\n{'=' * 78}")
            print(f"{len(grupos)} histórias · US$ {total:.4f} nesta rodada "
                  f"· prompt {versao_h} · vocabulário v{VOCAB_VERSAO}")
        return
    if args.ids:
        linhas = _por_id(conexao, [int(x) for x in args.ids.split(",")])
    else:
        linhas = _materias(conexao, args.n, prompt_versao)
    if not linhas:
        print("Nenhuma matéria nova para extrair.")
        print(f"Tudo o que tem texto suficiente já foi processado por "
              f"{llm.EXTRACAO.id} com o prompt {prompt_versao}.")
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
        sentencas = corta_lide(sentencas, limite_lide)

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
            llm.EXTRACAO.id, prompt_versao, VOCAB_VERSAO, resultado.uso,
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

        n_orfas, custo_orfas = orfas(conexao, VOCAB_VERSAO)
        if n_orfas:
            print()
            print(f"ATENÇÃO: {n_orfas} matérias extraídas sob vocabulário "
                  f"antigo (US$ {custo_orfas:.2f} pagos).")
            print("O grafo as ignora — relação de vocabulários diferentes não "
                  "é comparável.")
            print("Elas voltaram para a fila: --historias e -n as oferecem de "
                  "novo.")

        t = estatisticas_triplas(conexao)
        print(f"\nAcervo de triplas: {t['triplas']} triplas de {t['materias']} "
              f"matérias · {t['relacoes']} relações distintas · "
              f"{t['entidades']} entidades")
        # "Gravado", nao "total": o banco so registra extracao que deu certo.
        # Chamada truncada e cobrada nao grava nada -- a do Braskem queimou
        # ~US$ 0,20 sozinha -- e este total soma so extracoes; o custo das
        # consultas fica na tabela `consultas`. Este numero e piso. A versao
        # impressa e a EFETIVA da rodada: com --sentencas, a constante do
        # modulo nao e a versao sob a qual nada aqui foi gravado.
        print(f"Custo gravado (so extracao): US$ {t['custo']:.4f} · "
              f"prompt {prompt_versao} · vocabulário v{VOCAB_VERSAO}")
        if no_vocab:
            print(f"Em 'outro': {fora} de {no_vocab} "
                  f"({100 * fora / no_vocab:.0f}%) — se subir, a lista precisa "
                  f"de relação nova")

    conexao.close()


if __name__ == "__main__":
    main()
