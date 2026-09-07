# Verificador de notícias por corroboração

[![testes](https://github.com/gbriel-trab/news-fact-checker/actions/workflows/testes.yml/badge.svg)](https://github.com/gbriel-trab/news-fact-checker/actions/workflows/testes.yml)

Motor de verificação de fatos que responde a uma pergunta difícil sem fingir
onisciência: **uma afirmação que circula é sustentada por fontes
independentes?**

A abordagem ingênua — perguntar a um LLM "isso é verdade?" — não funciona: o
modelo responde com a mesma confiança quando sabe e quando não sabe, e a
resposta não carrega fonte. Aqui o LLM nunca julga verdade. Ele estrutura; a
evidência vem de um acervo próprio de notícias, e todo veredito cita quem
afirmou o quê, com link.

Como o boletim diário apresenta um post: o separador tira dele a premissa
factual e o veredito sai do acervo. O caso abaixo é uma reconstrução
abreviada de uma verificação de agosto/2026, com o perfil omitido e sem o
bloco EVIDÊNCIA (veículo, manchete e link de cada fonte) que o boletim
imprime antes do POR QUE.

```
$ python -m src.boletim

RADAR · @perfil · 25/08/2026
texto literal do post, lido pela API oficial do X — o registro é o post, no link
Conferência de premissas contra o acervo — não avalia o autor.

POST 1 (@perfil, 2026-08-25 14:02 UTC) · post
URL: https://x.com/perfil/status/…
juliana brizola tem 38% no primeiro turno no RS, ganha no primeiro
  [previsao] ganha no primeiro — nada a conferir
  premissa: "Juliana Brizola tem 38% no primeiro turno no RS"
    VEREDITO
      CONFIRMADO · 1 veículo
    POR QUE
      A pesquisa Real Time Big Data de agosto de 2026 registra Juliana
      Brizola com 38% no primeiro turno no RS, exatamente como afirmado;
      o valor distinto de 23% vem de outro instituto (Quaest), não sendo
      medida da mesma pesquisa.

      ATENÇÃO: um veículo só. Sem confirmação independente.
```

Repare no que o sistema **não** fez: havia um "23%" no acervo que
contradiria a afirmação, e ele distinguiu sozinho que era outra pesquisa —
em vez de acusar contradição inexistente. E o digest, que roda sem ninguém
pedir, entrega o outro lado da mesma máquina:

```
CONFIRMADO POR FONTES INDEPENDENTES
  3 veículos · (Davi Alcolumbre, preside, Senado Federal)
      [CNN Brasil] · [G1] · [Poder360]
NÚMEROS QUE NÃO BATEM
  (Bitcoin, tem atributo, —) · cotação · em USD · entre veículos
      65.500 [CriptoFácil] · 80.000 [Folha]
```

"Sem evidência" é resposta válida e esperada — o sistema tem o direito de não
saber, e preencher a lacuna com plausibilidade é o defeito que ele existe
para evitar.

## As duas saídas

| | Gatilho | O que entrega |
|-|-|-|
| **`boletim`** | os posts do dia nos perfis acompanhados no X | cada premissa factual com veredito `confirmado / contradito / sem evidência`, e as fontes |
| **`digest`** | o acervo do dia | o que 2+ veículos sustentam, e onde os números deles não batem |

Duas peças montam o boletim: **`radar`** captura os posts dos perfis
públicos acompanhados no X, e **`premissas`** recebe o texto do post e
separa o que é previsão/opinião/relato — que não se verifica, e não deve
ser — e o que é afirmação sem referente identificável — que não dá para
verificar — das premissas factuais, cada uma com sujeito, objeto e data
**ancorados no trecho literal** e conferidos em código antes de custar uma
chamada. Quando o autor cita o post de um canal, as afirmações factuais do
citado entram à parte, com o nome de quem as fez: o autor não é avaliado
pelo que cita, e o que ele amplifica também é conferido. O resultado é entregue diariamente pelo Telegram.

Os prompts do separador e do juiz têm **gabarito de regressão**
(`gabaritos/`, `python -m src.gabarito`): casos fixos com resposta esperada
escrita à mão, posts reais com o registro do post, e a regra de que nenhum prompt
muda sem a bateria passar — porque uma regra escrita para um caso engoliu o
vizinho uma vez, e foi o bastante.

## Como funciona

```
INGESTÃO (a cada 15 min, sem LLM no caminho crítico)
  RSS de 20 veículos → dedup por URL+hash (edição vira versão, preservando
  a anterior — retratação é detectável) → seleção AOS PARES (só matéria
  coberta por 2+ veículos vale extração) → LLM extrai triplas
  (sujeito, relação, objeto) com vocabulário FECHADO de relações,
  entidade canônica, valor numérico com unidade e data do fato
       ↓
  SQLite (acervo) · ChromaDB (busca semântica) · grafo de corroboração

CONFERÊNCIA (diária, sobre os posts que o radar capturou)
  post → separador tira dele as premissas factuais → tripla → busca em
  duas rotas (chave exata + vetorial) → LLM julga contra a evidência
  recuperada → veredito com fontes
```

Decisões que fazem diferença, todas documentadas com medição no
[ARCHITECTURE.md](ARCHITECTURE.md):

* **Vocabulário fechado de relações**, imposto como `enum` no structured
  output e derivado de dado real: com verbo livre, "comprou" e "adquiriu"
  viram relações distintas e três fontes que confirmam o mesmo fato não se
  encontram. A lista evolui inspecionando o que cai em `outro`.
* **Canonicalização de entidade na leitura**: "Braskem" e "Braskem S.A."
  somavam 100 triplas como duas entidades. A fusão é determinística e
  conservadora — fundir "Braskem" com "Braskem Idesa" (subsidiária)
  fabricaria confirmação, e falso positivo é o pior erro do sistema.
* **Número só disputa com número no mesmo instante**: cotação de dias
  diferentes não é contradição, é o preço se movendo.
* **Corte no lide**: só as primeiras sentenças vão ao modelo. Medido: 89%
  das confirmações por 35% do custo — o fato principal mora no primeiro
  parágrafo, e é ele que dois veículos publicam igual.
* **Custo é projetado, não sofrido**: filtro de pares antes da chamada cara,
  cache de prompt, extração a ~US$ 0,04/matéria, e três tetos em código:
  por handle no radar, por rodada na demanda, e diário na extração (o
  dobro da média medida, US$ 2,30). Cada centavo gravado no banco, por
  chamada.

## Números atuais (medidos, não estimados)

Medidos no banco em 05/09/2026:

* Acervo: 12.464 matérias de 20 veículos, coleta a cada 15 min
* 3.056 afirmações extraídas de 326 matérias
* **301 fatos confirmados por 2+ veículos independentes**, de 1.395
  fatos distintos
* 706 testes, todos sem rede; a camada de verificação — onde erro é
  silencioso — é a mais coberta
* Gabarito de regressão dos prompts: 34 casos do separador (ficam fora do
  repositório, porque reproduzem texto de post) e 23 do juiz. Última
  rodada completa do separador em 07/09/2026, 2 passadas, US$ 0,91: zero
  regressões, depois de o roteador passar a ler sigla em caixa alta (OTAN,
  WSJ, IPCA) como nome e não como ênfase
* Boletim refeito dia a dia de 25/08 a 06/09/2026: 13 rodadas, 103 posts
  lidos, 32 no boletim (o resto era resposta a outra conta, descartada
  antes de custar), US$ 1,63 no total — US$ 0,125 por dia

## Rodando

```bash
python -m venv venv && venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # ANTHROPIC_API_KEY sempre; o boletim pede também
                         # X_CLIENT_ID e HANDLES_RADAR. As duas do Telegram
                         # são opcionais: sem elas o boletim só grava o arquivo

python -m src.collect                  # coleta (grátis, agende a cada 15min)
python -m src.extract --historias 10   # extração aos pares (paga, ~US$0,05/matéria)
python -m src.indice                   # reindexa a busca semântica (grátis)
python -m src.digest --horas 24        # o que se sustenta hoje (grátis)
python -m src.x_auth                   # consentimento no navegador, uma vez (grava
                                       # data/x_token.json; feche o painel antes:
                                       # o callback usa a mesma porta 8765)
python -m src.boletim                  # posts do dia → premissas → vereditos (paga)
python -m src.boletim --desde 2026-08-25 --ate 2026-08-26   # refaz um dia passado
                                       # (janela em UTC, fim exclusivo)
```

A extração tem `--dry-run` para inspecionar o que seria enviado antes de
gastar; o boletim tem `--sem-envio`, que monta e grava sem entregar.

## O que este projeto não é

* **Não é um oráculo.** `confirmado` significa "as fontes que tenho
  sustentam", jamais "é verdade". O acervo cataloga o que cada veículo
  afirmou — inclusive quando erram.
* **Não raspa sites nem contorna paywall.** Usa o que o RSS entrega.
* **Não varre a internet atrás de desinformação.** As afirmações vêm dos
  perfis que você escolheu acompanhar, não do que circula em geral.

## Roadmap honesto

Medição de acurácia contra checadores profissionais (Lupa, Aos Fatos) —
o número que separa isto de um agregador; contradição não-numérica
("aprovado" vs "rejeitado"), que espera relações com polaridade no
vocabulário; e a avaliação medida de orquestração com ciclo adaptativo
contra a cascata fixa — que só entra se vencer em precisão, não só em
recall. O classificador clássico factual×opinião ficou adiado: para o
post, o separador já faz esse papel; para a imprensa, o corte no lide e a
seleção aos pares já cortam a maior parte do custo.

---

Projeto acadêmico (IBMEC) construído como produto real. As decisões
técnicas, com as medições que as sustentam, estão no
[ARCHITECTURE.md](ARCHITECTURE.md).
