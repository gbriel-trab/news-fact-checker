# Arquitetura

Documento de decisões técnicas do projeto: o que foi escolhido, por quê, e o
que ainda está em aberto.

## O problema

Verificar automaticamente se uma afirmação que circula na internet é sustentada
por fontes independentes — e, quando não for, dizer isso explicitamente em vez
de inventar uma resposta.

A abordagem ingênua seria perguntar a um LLM "isso é verdade?". Não funciona:
o modelo responde com confiança tanto quando sabe quanto quando não sabe, e a
resposta não carrega fonte alguma. O projeto existe para resolver exatamente
esse ponto.

## Pipeline

Orquestração em **LangGraph**, com estado compartilhado atravessando os nós:

```
Coleta → Segmentação → Classificação (factual vs opinião)
       → Extração de triplas → Busca de evidência → Verificação → Entrega
                                        ↑                 |
                                        └── evidência insuficiente ──┘
```

O **ciclo** é o motivo da escolha do framework. Se a evidência recuperada for
insuficiente, o grafo não desiste nem alucina: volta ao nó de busca e tenta
outra query, até um limite de tentativas. Orquestradores lineares e frameworks
baseados em conversa entre agentes não expressam isso de forma natural — um
grafo de estado com aresta condicional expressa.

## Coleta contínua

A fonte primária é **RSS de veículos de notícia**. Isso impõe uma restrição que
molda o resto do sistema: **RSS não oferece busca**. Um feed devolve apenas os
últimos N itens publicados, e não há como consultar o passado.

A consequência é que o acervo local **é** o índice de busca que o RSS não tem.
O que não for coletado enquanto esteve no feed é irrecuperável — não existe
backfill.

Por isso a coleta não é acumulação de notícia velha por nostalgia. Ela existe
por **cobertura**:

* **Corroboração cruzada.** Veículos publicam o mesmo fato em horários
  diferentes. Coleta intermitente captura uma fonte só, e uma fonte sozinha não
  confirma nada. Só a coleta contínua permite comparar relatos independentes.
* **Casos que exigem passado.** Afirmação recirculada (fato verdadeiro e antigo
  apresentado como atual), retratação posterior, e fato que mudou legitimamente
  ao longo do tempo. Nenhum é detectável sem acervo.

Consequência prática na ordem de construção: **o coletor é o primeiro
componente a entrar em operação**, mesmo rudimentar. Todo o resto do sistema é
recuperável — extração se refaz, classificador se retreina, grafo se
reconstrói. O acervo não.

### Deduplicação

Coletando um feed a cada 30 minutos, a grande maioria dos itens se repete. Sem
deduplicação, a mesma matéria é armazenada e — muito pior — reprocessada por
LLM dezenas de vezes por dia.

A chave é a **URL normalizada** (sem parâmetros de rastreamento como `utm_*`),
somada a um **hash do conteúdo**:

| Situação | Ação |
|-|-|
| URL nova | Armazena e processa |
| URL conhecida, hash igual | Descarta |
| URL conhecida, hash diferente | Matéria foi editada: reprocessa e versiona |

O terceiro caso não é detalhe. Retratação e correção normalmente acontecem por
edição da mesma página — deduplicar apenas por URL tornaria invisível um dos
casos que o projeto mais quer capturar.

## Camada de verificação

O diferencial do projeto. Em vez de perguntar ao modelo se algo é verdade:

1. A afirmação é extraída como tripla `(entidade, relação, entidade)`
2. Buscam-se fontes independentes sobre essa tripla
3. O resultado é classificado em **confirmado**, **contradito** ou
   **sem evidência**
4. Toda saída carrega a fonte que a sustenta

"Sem evidência" é uma resposta válida e esperada do sistema, não uma falha.

### Vocabulário controlado de relações

A relação da tripla vem de uma **lista fechada**, imposta como `enum` no
structured output — restrição técnica na chamada, não pedido no prompt.

O motivo é concreto: com verbo livre, "comprou", "adquiriu" e "fechou_compra"
viram três relações distintas, e três fontes que **confirmam o mesmo fato** não
se encontram no grafo. O resultado não é um erro visível — é um "sem evidência"
silencioso, que é o pior tipo de falha porque parece funcionamento normal.

Regras do vocabulário:

* Sempre existe um valor **`outro`** como válvula de escape. Sem ele, o que não
  couber na lista desaparece sem deixar rastro.
* A lista é **derivada de dado real**, não projetada no papel: começar com 5–8
  relações, rodar sobre notícia de verdade, inspecionar o que caiu em `outro` e
  promover o que for frequente. Alvo de convergência: 10–15 relações.
* Cada tripla grava a **versão do vocabulário** vigente na extração. Como a
  lista cresce com o tempo, sem isso é impossível distinguir "não cabia em
  nenhuma relação" de "essa relação ainda não existia".

### Evento e estado são relações diferentes

`comprou` é um evento datado. `possui` é um estado atual. Fundir os dois produz
falso positivo: "comprou em 2019" e "não possui mais em 2026" são ambas
verdadeiras, e um sistema que as unifica acusa contradição onde não há.

Num verificador de fatos, **falso positivo é o pior erro possível** — acusar
contradição inexistente destrói a confiança no sistema inteiro.

A distinção também governa a detecção de conflito, descrita adiante:

| Tipo | Semântica | Permanece verdadeiro? |
|-|-|-|
| **Evento** | Afirma algo sobre um instante | Sim, para sempre |
| **Estado** | Afirma algo sobre um intervalo | Não, pode deixar de valer |

### Questão em aberto: atribuição

O padrão mais comum em jornalismo é `Fulano afirmou que Z`, onde `Z` é ela
própria uma afirmação. A tripla plana modela isso como
`(Fulano, afirmou, "Z")`, transformando um conteúdo verificável em string
opaca.

São duas perguntas verificáveis distintas — *Fulano disse isso?* e *isso é
verdade?* — e o modelo atual só alcança a primeira. Alternativas conhecidas
envolvem reificação, com a tripla interna virando um nó. Ainda não decidido;
será avaliado sobre dados reais.

## Modelo da aresta

Cada relação armazenada carrega, além dos dois nós:

| Campo | Função |
|-|-|
| `fonte` | Veículo e URL de origem |
| `data_publicacao` | Quando a fonte publicou |
| `data_fato` | Quando o fato ocorreu, segundo o texto |
| `tipo` | `EXTRACTED` (explícito na fonte) ou `INFERRED` (deduzido pelo modelo) |
| `vocab_versao` | Versão do vocabulário de relações usada |

**As duas datas não são redundantes.** Elas divergem justamente no caso de
desinformação mais comum: matéria publicada hoje sobre fato de anos atrás,
apresentada como atual. Uma aresta que guarde apenas a data de publicação
registra esse fato com a data errada e torna o caso indetectável.

`EXTRACTED` e `INFERRED` nunca são exibidos com o mesmo peso. O que a fonte diz
e o que o modelo deduziu são coisas diferentes.

## Detecção de contradição

Duas triplas com as mesmas entidades e relações incompatíveis são candidatas a
contradição — mas só isso produz falso positivo em massa, porque fato evolui
legitimamente:

```
(X, possui, Y)      2019
(X, nao_possui, Y)  2026     → evolução, NÃO contradição
```

A regra depende do tipo da relação:

* **Estado** — compara-se pela janela temporal. Triplas conflitantes próximas
  no tempo (dias) são contradição suspeita; separadas por meses ou anos são
  evolução do fato.
* **Evento** — a janela **não se aplica**. Duas fontes que discordam sobre o
  que ocorreu em 2019 se contradizem, tenham sido publicadas com três dias ou
  três anos de diferença. A comparação usa a `data_fato`, nunca a
  `data_publicacao`.

Aplicar a janela uniformemente aos dois tipos produziria falso negativo em
evento — o segundo pior erro do sistema, atrás apenas do falso positivo.

## Dois índices, não um

| Índice | Função |
|-|-|
| **Vetorial** (embeddings) | Recuperar notícias semanticamente relacionadas à afirmação |
| **Grafo** | Detectar quando duas fontes afirmam relações incompatíveis sobre as mesmas entidades |

Busca vetorial sozinha não enxerga contradição: dois textos que se contradizem
são semanticamente *parecidos* e ficam próximos no espaço de embeddings. É
preciso comparar as relações afirmadas, não a similaridade dos textos.

Contradição entre fontes independentes é sinal forte de fato duvidoso — e é
justamente o caso que mais interessa detectar.

## Filtro de custo: classificador factual vs opinião

Classificador clássico (**scikit-learn**) separando **afirmação factual
verificável** de **opinião**, para que apenas a primeira consuma chamada de
LLM.

A justificativa é econômica: armazenar texto é barato, chamar LLM não é. Tudo o
que for coletado é guardado; só o que o classificador marcar como factual segue
para extração.

**A unidade de classificação é a sentença, não a matéria.** Notícia mistura
relato factual e opinião citada no mesmo texto. A segmentação em sentenças
ocorre antes, e tem custo desprezível.

**Ordem de construção.** O classificador depende de dataset rotulado à mão, que
por sua vez depende de dados já coletados. Ele não pode ser a primeira peça:

```
coletar → extrair sem filtro → rotular à mão o coletado
        → treinar → inserir o filtro no pipeline
```

É otimização introduzida depois de o pipeline funcionar, não componente do dia
um.

## Stack

| Camada | Escolha | Motivo |
|-|-|-|
| Orquestração | **LangGraph** | Ciclo com aresta condicional |
| Vector DB | **ChromaDB** | Local, sem servidor, persiste em disco |
| Embeddings | **sentence-transformers**, modelo multilíngue | Notícia em português; roda local, custo zero por documento |
| Grafo | **NetworkX** | Em processo, sem infraestrutura |
| Classificador | **scikit-learn** | Filtro barato antes da chamada cara |

Embeddings rodam localmente de propósito: o orçamento de chamada paga fica
reservado para extração e verificação, que são as etapas onde o LLM é
insubstituível.

**NetworkX antes de Neo4j.** A detecção de contradição é lógica, não
infraestrutura, e migrar depois é mecânico. Neo4j acrescenta servidor e
container a um projeto onde Docker já está na fila de corte.

### Armadilha do embedding

Indexação e consulta **têm** que usar o mesmo modelo. Modelos diferentes
produzem sistemas de coordenadas diferentes: a busca não falha nem avisa, só
devolve resultado sem sentido. Trocar de modelo obriga a reindexar tudo.

Defesa: o nome e a versão do modelo ficam gravados nos metadados do índice e
são conferidos na consulta. Isso converte uma falha silenciosa em erro
explícito — troca sempre vantajosa.

### Ordem de grandeza do armazenamento

Estimativa, não medição: cerca de 7 KB por notícia entre texto, embedding e
triplas. A 500 notícias por dia, algo como 3,5 MB/dia. Disco não é o gargalo.

O gargalo é **volume de chamada de LLM**, que cresce linearmente com a coleta —
daí o classificador existir como filtro, e não como enfeite acadêmico.

## Princípios de projeto

Estas são as decisões que definem o que o projeto é. Funcionalidade nova que
contrarie qualquer uma delas está errada, ou exige revisar o princípio de forma
explícita — nunca por acidente.

1. **Nunca perguntar ao modelo se algo é verdade.** Todo veredito nasce de
   evidência externa recuperada, não do conhecimento interno do LLM.

2. **Todo veredito carrega a fonte.** Afirmação sem fonte rastreável não é
   apresentada como verificada, em nenhuma circunstância.

3. **"Sem evidência" é uma resposta válida.** O sistema tem o direito de não
   saber. Preencher a lacuna com plausibilidade é o fracasso que o projeto
   existe para evitar.

4. **`EXTRACTED` e `INFERRED` nunca têm o mesmo peso.** O que a fonte diz e o
   que o modelo deduziu são exibidos como coisas distintas.

5. **Falso positivo é o pior erro.** Na dúvida entre acusar contradição
   inexistente e deixar passar, o sistema deixa passar.

6. **Filtro barato antes de chamada cara.** Classificador clássico e heurística
   rodam antes do LLM, nunca depois.

7. **O ciclo do grafo serve para tentar outra query, não para insistir até
   inventar.** Há limite de tentativas, e esgotá-lo leva ao princípio 3.

8. **Nenhuma credencial no código.**

Teste prático para funcionalidade nova: *ela consegue citar a fonte do que
afirma?* Se não conseguir, não entra no caminho de verificação — no máximo em
uma camada de apresentação claramente separada.

## Fonte de dados

**RSS de veículos de notícia é a fonte única do projeto.**

X/Twitter foi descartado por custo de API. Bluesky exige autenticação para
busca (retorna 403 sem credencial) e Reddit exige OAuth; ambos foram
descartados para manter o escopo fechado, e não como etapa futura.

O ponto fraco assumido: RSS de veículos grandes significa checar fonte
confiável contra fonte confiável, o que enfraquece o caso "afirmação duvidosa
circulando em rede social". O que permanece forte é **contradição entre
veículos** — números, atribuições e cronologias divergentes sobre o mesmo
evento — que é exatamente o que o índice em grafo foi desenhado para detectar.

### O que os feeds realmente entregam

Medido na primeira coleta real, e não presumido. Os veículos não usam os campos
do RSS de forma consistente:

| Veículo | Itens por chamada | Onde vem o texto | Tamanho médio |
|-|-|-|-|
| G1 | 100 | `summary` | ~3.400 caracteres |
| CNN Brasil | 60 | `content` | ~3.300 caracteres |
| BBC Brasil | 42 | só manchete e linha fina | ~230 caracteres |
| Agência Brasil | 10 | `summary` | ~3.200 caracteres |
| InfoMoney | 10 | `content` | ~10.000 caracteres |

Consequência de projeto: ler apenas `content` descartaria o corpo do G1 e da
Agência Brasil, que juntos são metade do volume. O texto usado para extração é
o mais longo entre os dois campos.

A BBC entrega texto curto demais para extração de triplas. Fica no acervo
porque manchete ainda serve como sinal de cobertura — vários veículos noticiando
o mesmo fato —, mas não como base de afirmação verificável.

## Convenções do repositório

* Nenhuma credencial no código. Tudo em `.env`, versionado apenas como
  `.env.example`
* Mensagens de commit descrevem a mudança e o motivo
* Testes ao menos na camada de verificação, que é onde erro é silencioso
