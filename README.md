# Verificador de notícias por corroboração

Motor de verificação de fatos que responde a uma pergunta difícil sem fingir
onisciência: **uma afirmação que circula é sustentada por fontes
independentes?**

A abordagem ingênua — perguntar a um LLM "isso é verdade?" — não funciona: o
modelo responde com a mesma confiança quando sabe e quando não sabe, e a
resposta não carrega fonte. Aqui o LLM nunca julga verdade. Ele estrutura; a
evidência vem de um acervo próprio de notícias, e todo veredito cita quem
afirmou o quê, com link.

Saída real de uma consulta ao acervo (agosto/2026):

```
$ python -m src.check "juliana brizola tem 38% no primeiro turno no RS"

VEREDITO   CONFIRMADO · 1 veículo
POR QUE    A pesquisa Real Time Big Data de agosto de 2026 registra
           Juliana Brizola com 38% no primeiro turno no RS, exatamente
           como afirmado; o valor distinto de 23% vem de outro instituto
           (Quaest), não sendo medida da mesma pesquisa.
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
| **`check`** | uma afirmação de fora (boato, post, mensagem) | veredito `confirmado / contradito / sem evidência`, com fontes |
| **`digest`** | o acervo do dia | o que 2+ veículos sustentam, e onde os números deles não batem |

Duas frentes auxiliares completam o ciclo: **`premissas`** recebe um texto
argumentativo (análise de mercado, post de rede social) e separa o que é
previsão/opinião — que não se verifica, e não deve ser — das premissas
factuais, conferindo cada uma; **`radar`** captura os posts de perfis
públicos acompanhados no X e alimenta essa conferência.

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

CONSULTA (quando chega uma afirmação)
  afirmação → tripla → busca em duas rotas (chave exata + vetorial)
  → LLM julga contra a evidência recuperada → veredito com fontes
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
  cache de prompt, extração a ~US$ 0,05/matéria. Cada centavo gravado no
  banco, por chamada.

## Números atuais (medidos, não estimados)

* Acervo: ~3.100 matérias de 20 veículos, coleta a cada 15 min
* 880 afirmações extraídas de 99 matérias sob o vocabulário v2
* **71 fatos confirmados por 2+ veículos independentes**
* 160 testes; a camada de verificação — onde erro é silencioso — é a mais
  coberta

## Rodando

```bash
python -m venv venv && venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # e preencha a ANTHROPIC_API_KEY

python -m src.collect                  # coleta (grátis, agende a cada 15min)
python -m src.extract --historias 10   # extração aos pares (paga, ~US$0,05/matéria)
python -m src.indice                   # reindexa a busca semântica (grátis)
python -m src.digest --horas 24        # o que se sustenta hoje (grátis)
python -m src.check "afirmação"        # verifica (paga, ~US$0,03/consulta)
```

Todo comando pago tem `--dry-run` para inspecionar o que seria enviado.

## O que este projeto não é

* **Não é um oráculo.** `confirmado` significa "as fontes que tenho
  sustentam", jamais "é verdade". O acervo cataloga o que cada veículo
  afirmou — inclusive quando erram.
* **Não raspa sites nem contorna paywall.** Usa o que o RSS entrega.
* **Não detecta desinformação sozinho.** A afirmação a verificar é entrada,
  não descoberta.

## Roadmap honesto

Medição de acurácia contra checadores profissionais (Lupa, Aos Fatos) —
o número que separa isto de um agregador; contradição não-numérica
("aprovado" vs "rejeitado"), que espera relações com polaridade no
vocabulário; classificador factual×opinião como filtro de custo; e a
avaliação medida de orquestração com ciclo (LangGraph) contra a cascata
fixa — que só entra se vencer em precisão, não só em recall.

---

Projeto acadêmico (IBMEC) construído como produto real. As decisões
técnicas, com as medições que as sustentam, estão no
[ARCHITECTURE.md](ARCHITECTURE.md).
