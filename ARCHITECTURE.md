# Arquitetura

Documento de decisões técnicas: o que foi escolhido, por quê, o que ainda está
em aberto, e o que o sistema deliberadamente **não** faz.

## O problema

Verificar se uma afirmação que circula é sustentada por fontes independentes —
e, quando não for, dizer isso explicitamente em vez de inventar uma resposta.

A abordagem ingênua seria perguntar a um LLM "isso é verdade?". Não funciona: o
modelo responde com a mesma confiança quando sabe e quando não sabe, e a
resposta não carrega fonte alguma. O projeto existe para resolver esse ponto.

## O que o sistema é, e o que não é

Duas delimitações que governam o resto do documento.

**O acervo é catalogado, não verificado.** O banco guarda o que cada veículo
**afirmou**, não o que é verdade. Se uma fonte publicar algo errado, entra
igual, registrada como "este veículo afirmou X". Nada ali passa por checagem de
veracidade.

Por isso todo veredito é relativo às fontes: `confirmado` significa *"as fontes
que tenho sustentam isso"*, jamais *"isso é verdade"*. A saída mostra
"confirmado por 3 veículos" com os links, nunca um carimbo de verdade solto. Um
sistema que afirmasse verdade seria o oráculo que este projeto recusa.

**O sistema não gera as próprias perguntas.** Ele é um motor de verificação: a
afirmação a ser checada é entrada dele, não parte dele. Monitorar redes sociais
em busca de boatos é um produto separado, e é a parte que exige API paga. Dizer
que o sistema "detecta desinformação sozinho" seria falso.

## Duas metades, dois gatilhos

O sistema não é um pipeline só. São dois, com gatilhos diferentes.

```
┌── INGESTÃO ─────────────────── gatilho: relógio, a cada 15 min ───┐
│                                                                   │
│   Coleta RSS → Segmentação → Classificação → Extração de triplas  │
│                                                    ↓              │
│                                   índice vetorial + grafo         │
└───────────────────────────────────────────────────────────────────┘
                                                    │
                                                    │ consulta
                                                    ↓
┌── CONSULTA ─────────────────── gatilho: uma afirmação de fora ────┐
│                                                                   │
│   afirmação → vira tripla → busca no acervo → julga → veredito    │
│                                   ↑              │                │
│                                   └── insuficiente? ──┘           │
│                                      (ver "O ciclo")              │
└───────────────────────────────────────────────────────────────────┘
```

A **ingestão** roda sozinha, em intervalo fixo. Prepara o acervo. É trabalho
caro e estável: cada matéria é processada uma vez, e o resultado vira índice.

A **consulta** roda quando chega uma afirmação. Não coleta nem extrai matéria —
apenas consulta o que a ingestão preparou.

A regra que separa as duas, e vale para qualquer sistema RAG: **o que é caro e
não depende da pergunta vai para a ingestão; o que depende da pergunta fica na
consulta.** Extrair triplas na hora da consulta significaria reprocessar o
acervo inteiro a cada pergunta.

## De onde vem a afirmação

Esta é a decisão que define se o sistema verifica ou apenas agrega.

**A afirmação e o acervo precisam ser populações diferentes.** Checar imprensa
contra imprensa é redundante: o resultado seria "três veículos disseram o
mesmo". O valor aparece quando a afirmação vem de fora da imprensa — um boato,
uma mensagem encaminhada, um post — e o acervo serve de corpo de evidência.

| Origem | População distinta? | Autônoma? | Papel no projeto |
|-|-|-|-|
| Afirmação digitada na CLI | Sim | Não | Demonstração e uso real |
| RSS de agências de checagem | Sim — são boatos de rede social | Sim, mas já vêm com o veredito | **Gabarito de avaliação** |
| Rede social via API do X | Sim | Sim | Descartada: US$ 0,005 por **post lido** |
| Rede social via API da xAI | Sim | Sim | Viável, ver abaixo |

### Rede social pela API da xAI

Identificado e precificado, não implementado.

A API do X cobra **por post lido** (US$ 0,005), o que inviabiliza volume: 500
posts/dia dariam ~US$ 75/mês. A API da xAI expõe busca no X como ferramenta e
cobra **por chamada de busca**, ao mesmo preço unitário — e uma busca devolve
vários posts. Cinco buscas por dia ficam em torno de US$ 0,75/mês, cerca de
cem vezes menos.

A diferença não é de desconto, é de unidade de cobrança. Vale registrar porque
a conclusão anterior — "rede social está fora do orçamento" — era verdadeira
para a API do X e falsa como afirmação geral.

Condição para adotar: **a busca precisa devolver o post com autor e link.**
Se devolver apenas um resumo do modelo sobre o que está circulando, não serve
— afirmação sem fonte rastreável quebra o princípio 2 já na entrada, e o
sistema passaria a confiar na paráfrase de um modelo como se fosse registro.

Fica para depois de a extração estar validada.

A entrada é uma CLI:

```
python -m src.check "o governo cancelou o programa X"
```

WhatsApp foi descartado. Ele havia sido pensado como *saída* — o sistema
empurrando vereditos —, o que reforçava o problema: o sistema escolhendo sozinho
o que verificar, e verificando o que já estava confirmado. Como *entrada* ele
também não se justifica, porque exigiria integração para um caso de uso que a
CLI cobre.

O RSS das agências não é fonte de produto — elas já publicaram a resposta.
É **gabarito**: roda-se o sistema sobre afirmações que Lupa, Aos Fatos ou
Comprova já julgaram, sem mostrar o veredito delas, e compara-se. Ver
"Como medir se funciona".

## Duas saídas, dois gatilhos

O sistema entrega por dois caminhos, e eles resolvem problemas diferentes.

| | Gatilho | Entrada | O que produz |
|-|-|-|-|
| **Digest diário** (`digest.py`) | relógio | o acervo do dia | o que se sustenta e onde divergem |
| **Consulta** (`check.py`) | uma pessoa | afirmação vinda de fora | veredito sobre aquela afirmação |

### Digest diário

Roda sozinho, sem ninguém pedir. Percorre as histórias do dia cobertas por dois
ou mais veículos e reporta **onde eles não concordam**.

O produto é a divergência, não a confirmação. Vários veículos publicando a mesma
coisa é o estado normal do jornalismo — reportar isso seria agregação. O que
nenhum agregador entrega é *"o G1 diz 38% e a Folha diz 36% sobre a mesma
pesquisa"*, ou *"a CNN atribui a declaração ao ministro e o Poder360 ao
assessor"*.

Um item só entra no digest se estiver confirmado por dois veículos
independentes — a unidade sendo o veículo, nunca a editoria. Item sem
confirmação não é enviado com ressalva: não é enviado.

**Risco assumido:** o valor do digest depende de com que frequência os veículos
de fato divergem, e isso ainda não foi medido. Se a divergência for rara, o
digest degrada para uma lista de fatos corroborados — que é agregação. A
medição está em "Como medir se funciona" e precede a construção.

### Consulta

Recebe uma afirmação que não veio do acervo e a julga contra ele. É o caso
não-redundante, descrito em "De onde vem a afirmação".

Os dois compartilham a mesma máquina: coleta, extração, índices e detecção de
contradição. Só o gatilho e a apresentação mudam.

## Coleta contínua

A fonte é **RSS de veículos de notícia**, e isso impõe uma restrição que molda
o resto: **RSS não oferece busca**. Um feed devolve os últimos N itens, e não há
como consultar o passado.

O acervo local **é** o índice de busca que o RSS não tem. O que não for coletado
enquanto esteve no feed é irrecuperável — não existe backfill.

A coleta existe por **cobertura**, não por nostalgia:

* **Corroboração cruzada.** Veículos publicam o mesmo fato em horários
  diferentes. Coleta intermitente captura uma fonte só, e uma fonte não confirma
  nada.
* **Casos que exigem passado.** Afirmação recirculada, retratação posterior,
  fato que mudou legitimamente.

Consequência na ordem de construção: **o coletor é o primeiro componente a
entrar em operação.** Todo o resto é recuperável — extração se refaz,
classificador se retreina, grafo se reconstrói. O acervo não.

### Intervalo de coleta

Medido, não estimado. Cada feed guarda um número fixo de itens; a janela de
tempo é consequência do ritmo de publicação:

| Feed | Itens | Janela coberta |
|-|-|-|
| Poder360 | 10 | **24 minutos** |
| InfoMoney | 10 | 1 hora |
| CNN Brasil | 60 | 2,2 horas |
| G1 Política | 100 | 3,2 dias |
| Folha Mundo | 100 | 8,2 dias |

O intervalo é ditado pelo feed mais rápido, não pela média: **15 minutos**,
com margem sobre os 24 do Poder360.

O feed geral do G1 (janela de 97 minutos) foi descartado — ver "Fonte de dados".
Feeds por editoria cobrem dias, o que torna a coleta **tolerante a falha**: uma
noite com a máquina desligada não abre buraco. Isso importa porque a coleta é a
única etapa sem backfill.

### Deduplicação

Coletando a cada 15 minutos, a maioria dos itens se repete. Sem deduplicação, a
mesma matéria é armazenada e — pior — reprocessada por LLM dezenas de vezes.

A chave é a **URL normalizada** (sem parâmetros de rastreamento) somada a um
**hash do conteúdo**:

| Situação | Ação |
|-|-|
| URL nova | Armazena e processa |
| URL conhecida, hash igual | Descarta |
| URL conhecida, hash diferente | Matéria editada: nova versão, preservando a anterior |

O terceiro caso não é detalhe: retratação e correção acontecem por edição da
mesma página. Deduplicar só por URL tornaria invisível um dos casos que o
projeto mais quer capturar.

## Fonte de dados

**RSS de veículos de notícia é a fonte única.** X/Twitter foi descartado por
custo; Bluesky exige autenticação para busca e Reddit exige OAuth — ambos fora
do escopo, e não como etapa futura.

Os feeds são **por editoria**, não gerais. O feed geral do G1 é dominado por
conteúdo das afiliadas regionais — acidente municipal, evento local, grade de
programação. Esse material é **estruturalmente inverificável**: só um veículo
cobre, e afirmação de fonte única nunca pode ser corroborada.

O efeito da troca, medido com a mesma metodologia:

```
feed geral,   5 veículos  →   4 histórias com 2+ veículos, de 213 matérias
por editoria, 8 veículos  →  64 histórias com 2+ veículos, de 830 matérias
```

De ~2% para ~17% de matéria corroborável.

### Camadas de fonte

Nem toda fonte precisa ser corroborada, e tratar todas igual quebra o sistema
num caso concreto.

| Camada | Exemplos | Precisa de outra fonte? |
|-|-|-|
| **Primária** | BoJ, Federal Reserve, BCE, TSE, Senado | **Não** — é a fonte do próprio ato |
| **Imprensa** | G1, Folha, CNN, Poder360 | Sim: dois veículos independentes |

Exigir duas testemunhas para um banco central anunciando a própria decisão não
é rigor, é erro de categoria: o comunicado **é** o registro autoritativo.

O problema apareceu medindo o acervo. Das 830 matérias coletadas:

```
Federal Reserve      0 matérias
BoJ / Japão         13 matérias, todas do G1
BCE / Europa         9 matérias, 8 delas do G1
```

Macroeconomia internacional é coberta por um veículo só, ou por nenhum. O
filtro de cobertura múltipla, aplicado sem camadas, **apagaria esse domínio
inteiro** — justamente o que menos aparece na imprensa generalista brasileira e
mais importa para crédito e mercado.

Com camadas, o caso se resolve:

```
G1 afirma "BoJ elevou juros"  +  feed do BoJ registra comunicado no mesmo dia
                              =  corroborado
```

Um veículo somado à instituição é evidência mais forte que dois veículos.

### Ausência de registro primário é evidência

Fonte primária **não ter dito nada** é informação, e produz veredito:

> *"Circula que o BoJ elevou juros. O feed oficial do BoJ não registra
> comunicado de política monetária nas últimas 48 horas."*

Isso só vale para fonte primária sobre o próprio ato — silêncio da imprensa não
significa nada, silêncio de um banco central sobre a própria política significa.
E exige coleta contínua da fonte primária, senão a ausência é do acervo, não do
mundo.

Feeds primários testados entregam **apenas manchete**, sem corpo. Não sustentam
extração de triplas, mas sustentam os dois mecanismos acima, que dependem da
existência e da data do comunicado, não do texto dele.

### Rede social é radar, nunca evidência

O caso que motiva: um assunto ganha tração antes de a imprensa brasileira
cobrir, ou sem que ela vá cobrir.

```
rede social  →  assunto em alta
                    ↓
        busca a fonte primária sobre ele
                    ↓
   registro existe   →  confirmado, citando a instituição
   registro ausente  →  "circulando, sem registro na fonte oficial"
```

O post nunca entra como evidência. Ele indica **onde olhar**; a evidência vem
sempre da instituição ou da imprensa. Isso preserva o princípio de que todo
veredito carrega fonte rastreável — resumo de modelo sobre o que está
circulando não seria citável.

Depende da API da xAI, precificada em "De onde vem a afirmação". Fica para
depois de a extração e o grafo existirem.

### Veículo não é o mesmo que feed

Duas editorias da mesma redação **não são fontes independentes**. Contá-las como
duas inflaria toda medida de corroboração e produziria `confirmado` falso —
violando o princípio de que falso positivo é o pior erro.

A unidade de corroboração é o **veículo**. A editoria só organiza.

### O que os feeds entregam

Os veículos não usam os campos do RSS de forma consistente, e a causa é
comercial: site com paywall publica só a chamada, porque o corpo é o produto que
vende.

| Veículo | Onde vem o texto | Tamanho médio |
|-|-|-|
| G1 | `summary` | ~4.000 caracteres |
| CNN Brasil | `content` | ~3.300 |
| InfoMoney | `content` | ~10.000 |
| Agência Brasil | `summary` | ~3.200 |
| Folha, BBC, UOL | manchete e linha fina | ~150 a 300 |

Ler apenas `content` descartaria o corpo do G1 e da Agência Brasil, metade do
volume. O texto usado é o mais longo entre os dois campos.

Veículos que entregam só manchete não sustentam extração de triplas, mas
permanecem no acervo como **sinal de cobertura** — saber que a Folha noticiou o
mesmo fato conta para corroboração, mesmo sem o texto.

O projeto usa apenas o que o feed entrega. Não há raspagem de site nem contorno
de paywall.

## Camada de verificação

Em vez de perguntar ao modelo se algo é verdade:

1. A afirmação é extraída como tripla `(entidade, relação, entidade)`
2. Buscam-se fontes independentes sobre essa tripla
3. O resultado é classificado em **confirmado**, **contradito** ou
   **sem evidência**
4. Toda saída carrega a fonte

"Sem evidência" é resposta válida e esperada, não falha.

### Vocabulário controlado de relações

A relação vem de uma **lista fechada**, imposta como `enum` no structured output
— restrição técnica na chamada, não pedido no prompt.

Com verbo livre, "comprou", "adquiriu" e "fechou_compra" viram três relações
distintas, e três fontes que **confirmam o mesmo fato** não se encontram no
grafo. O resultado não é erro visível: é um "sem evidência" silencioso, que é o
pior tipo de falha porque parece funcionamento normal.

* Sempre existe o valor **`outro`** como válvula de escape. Sem ele, o que não
  couber desaparece sem rastro.
* A lista é **derivada de dado real**: começar com 5–8 relações, rodar sobre
  notícia de verdade, inspecionar o que caiu em `outro` e promover o frequente.
  Alvo de convergência: 10–15.
* Cada tripla grava a **versão do vocabulário**. Como a lista cresce, sem isso é
  impossível distinguir "não cabia" de "essa relação ainda não existia".

### Por que não uma biblioteca pronta de extração

Existem extratores de tripla gratuitos e locais. O mais próximo do que este
projeto precisa é o **mREBEL** (`Babelscape/mrebel-large`): multilíngue com
português, roda offline, custo zero por matéria.

Testado em 27/08/2026 sobre 5 matérias já extraídas, no mesmo lide, com beam
search de 3 sequências. **60 triplas, 30 relações distintas, nenhuma no
vocabulário** — com mapeamento manual generoso (`position held` →
`exerce_cargo_em`, `office contested` → `candidatou_se_a`), chegaria a ~12%.

A causa é estrutural, não de qualidade: o mREBEL foi treinado nas propriedades
do **Wikidata**, que modela fato permanente de entidade — *tem sede em*, *é
subsidiária de*, *é filiado a*. Notícia é feita de **evento e ato de fala**, e
o Wikidata não tem coluna para "negou recurso", "afirmou que" ou "submeteu a
votação".

O que o teste mostrou, além da cobertura:

* **Nenhum número, em nenhuma das cinco.** Não extraiu os R$ 1.741 do salário
  mínimo, os R$ 130,6 bilhões da Braskem, nem a multa de R$ 420 mil. A detecção
  de divergência roda em cima de número — só isso já encerra a questão.
* **Erro de sujeito da mesma família que o do Haiku, e pior**: devolveu
  `(José Antonio Encinas Manfré, member of political party, PRTB)`, filiando o
  magistrado ao partido do réu que ele julgou.
* Não distingue `EXTRACTED` de `INFERRED` (princípio 4), não carrega valor com
  unidade e contexto, e não mantém entidade canônica estável entre matérias.

Conclusão registrada para não ser refeita: **a extração paga não é preguiça de
procurar alternativa.** O vocabulário deste projeto é de corroboração
jornalística, e não existe pronto porque quase ninguém constrói isso.

O script do teste foi descartado de propósito — 2,3 GB de dependência para
rodar uma vez não pertence ao repositório. O resultado, sim.

### Canonicalização de entidade

A extração devolve a **entidade canônica**, não a forma de superfície que
apareceu no texto. Esta não é uma sutileza de prompt: é saída de primeira classe
do schema.

O motivo é o mesmo do enum, aplicado ao outro lado da tripla. Se o G1 extrai
`Ministério da Saúde` e a Folha extrai `governo federal` sobre o mesmo ato, o
grafo guarda duas entidades distintas, a comparação não acontece, e a
contradição real passa batida.

**O modo de falha é silencioso e enganoso.** Fragmentação de entidade produz
exatamente o mesmo sintoma que ausência de contradição — grafo sem conflitos
detectados. Concluir "contradição entre veículos é rara" quando a causa real é
normalização quebrada mataria a metade autônoma do projeto por um bug.

Dois problemas distintos, com dificuldades distintas:

| Caso | Exemplo | Situação |
|-|-|-|
| **Apelido** | `Lula` = `Luiz Inácio Lula da Silva` = `o presidente` | Resolvido na extração |
| **Hierarquia** | `Ministério da Saúde` ⊂ `governo federal` | **Em aberto** — não é normalização, é inferência |

O segundo caso fica assumido como limitação. Uma afirmação atribuída ao
"governo" pode não encontrar a matéria que atribui o ato a um ministério
específico.

### Evento e estado são relações diferentes

`comprou` é evento datado. `possui` é estado atual. Fundir os dois produz falso
positivo: "comprou em 2019" e "não possui mais em 2026" são ambas verdadeiras.

| Tipo | Semântica | Permanece verdadeiro? |
|-|-|-|
| **Evento** | Afirma algo sobre um instante | Sim, para sempre |
| **Estado** | Afirma algo sobre um intervalo | Não, pode deixar de valer |

### Questão em aberto: atribuição

O padrão mais comum em jornalismo é `Fulano afirmou que Z`, onde `Z` é ela
própria uma afirmação. A tripla plana modela isso como `(Fulano, afirmou, "Z")`,
transformando conteúdo verificável em string opaca.

São duas perguntas distintas — *Fulano disse isso?* e *isso é verdade?* — e o
modelo atual só alcança a primeira. Alternativas envolvem reificação, com a
tripla interna virando um nó. Será avaliado sobre dados reais.

## Modelo da aresta

| Campo | Função |
|-|-|
| `veiculo` | Redação de origem — a unidade de corroboração |
| `url` | Matéria específica |
| `data_publicacao` | Quando a fonte publicou |
| `data_fato` | Quando o fato ocorreu, segundo o texto |
| `tipo` | `EXTRACTED` (explícito na fonte) ou `INFERRED` (deduzido) |
| `vocab_versao` | Versão do vocabulário de relações |

**As duas datas não são redundantes.** Elas divergem no caso de desinformação
mais comum: matéria publicada hoje sobre fato de anos atrás, apresentada como
atual. Uma aresta com apenas a data de publicação registra o fato com a data
errada e torna o caso indetectável.

`EXTRACTED` e `INFERRED` nunca são exibidos com o mesmo peso.

## Detecção de contradição

Duas triplas com as mesmas entidades e relações incompatíveis são candidatas —
mas só isso produz falso positivo em massa, porque fato evolui:

```
(X, possui, Y)      2019
(X, nao_possui, Y)  2026     → evolução, NÃO contradição
```

A regra depende do tipo da relação:

* **Estado** — compara-se pela janela temporal. Triplas conflitantes próximas no
  tempo (dias) são contradição suspeita; separadas por meses ou anos são
  evolução.
* **Evento** — a janela **não se aplica**. Duas fontes que discordam sobre o que
  ocorreu em 2019 se contradizem, tenham publicado com três dias ou três anos de
  diferença. A comparação usa a `data_fato`, nunca a `data_publicacao`.

Aplicar a janela uniformemente produziria falso negativo em evento — o segundo
pior erro, atrás apenas do falso positivo.

Esta varredura é **código sobre dado normalizado**: espaço fechado e enumerável,
agrupado por entidade canônica. Não há estratégia de busca a adaptar, e portanto
nada aqui justifica um agente.

## Dois índices, não um

| Índice | Função |
|-|-|
| **Vetorial** (embeddings) | Recuperar matérias semanticamente relacionadas à afirmação |
| **Grafo** | Detectar quando duas fontes afirmam relações incompatíveis sobre as mesmas entidades |

Busca vetorial sozinha não enxerga contradição: dois textos que se contradizem
são semanticamente *parecidos* e ficam próximos no espaço de embeddings. É
preciso comparar as relações afirmadas, não a similaridade dos textos.

## Filtro de custo

Armazenar texto é barato; chamar LLM não é. Tudo o que for coletado é guardado;
só uma fração segue para extração. Dois filtros, em ordem de impacto.

### Cobertura múltipla

**O filtro principal.** Antes de qualquer chamada de LLM, as matérias do dia são
agrupadas por similaridade de embedding — local, custo zero — e só os grupos com
**dois ou mais veículos distintos** seguem para extração.

A justificativa não é só econômica: matéria de fonte única **não pode ser
corroborada por definição**. Extraí-la produz triplas que o sistema nunca
conseguirá confirmar. Matéria solitária permanece no acervo; se outro veículo
cobrir o assunto depois, ela entra no grupo e aí vale extrair.

### Classificador factual vs opinião

Classificador clássico (**scikit-learn**) separando afirmação factual
verificável de opinião, no nível da **sentença** — notícia mistura relato
factual e opinião citada no mesmo texto.

**A justificativa original mudou.** Quando a fonte prevista eram redes sociais,
o argumento era que conteúdo social é majoritariamente opinião. Com RSS de
veículos, isso não vale: jornalismo já é majoritariamente factual, e o ruído do
RSS não é opinião — é **irrelevância**. O classificador continua útil porque
notícia tem editorial, coluna, análise e opinião citada, mas deixou de ser o
filtro principal.

**Ordem de construção.** O classificador depende de dataset rotulado à mão, que
depende de dados já coletados. Ele não pode ser a primeira peça:

```
coletar → extrair sem filtro → rotular à mão → treinar → inserir o filtro
```

É otimização introduzida depois de o pipeline funcionar, não componente do dia
um. O gargalo dele não é volume de dados — é hora de rotulagem.

## LLM não é agente

Distinção que governa a decisão sobre o LangGraph.

* **Chamada de LLM** — uma requisição, uma resposta. Sem ciclo, sem decisão.
* **Agente** — um ciclo que decide, em tempo de execução, qual o próximo passo
  com base no que aconteceu.

Onde cada coisa entra:

| Etapa | Precisa LLM? | Precisa agente? |
|-|-|-|
| Extração de triplas + entidade canônica | Sim | Não |
| Varredura de contradição no grafo | Não | Não |
| Afirmação de terceiro → tripla | Sim | Não |
| Busca no acervo | Não | Não |
| Julgar se sustenta ou contradiz | Sim | Não |
| **Repetir a busca quando a primeira falha** | — | **Talvez** |

Três chamadas de LLM. **Zero agentes obrigatórios.** O sistema inteiro é
executável como pipeline linear.

### O ciclo

O ciclo tem exatamente um lugar candidato: refazer a busca quando a primeira não
resolve. E ele **não é neutro** — um sistema que insiste até achar algo está
estruturalmente inclinado a achar algo, o que o torna uma máquina de falso
positivo.

Se entrar, com duas travas obrigatórias:

* **Limite duro de tentativas**, e esgotá-lo leva a `sem evidência` — nunca a
  "aceita o que achou"
* **A régua de evidência não afrouxa entre tentativas.** Muda *onde* procura,
  nunca *o quanto aceita*

E entra apenas se ganhar uma comparação medida:

| | Cascata fixa | Adaptativo |
|-|-|-|
| Quem decide o próximo passo | o código, em ordem escrita antes | o modelo, diagnosticando a falha |
| Achou evidência quando existia? | medir | medir |
| **Emitiu veredito quando não devia?** | medir | medir |
| Chamadas de LLM por afirmação | medir | medir |

A segunda linha é obrigatória. O adaptativo tende a ganhar em encontrar — ele
insiste mais. Se ganhar em recall e piorar em precisão, não é melhor.

O resultado depende do modelo: um modelo fraco diagnosticando mal perde para uma
cascata bem escrita. A pergunta não é "agente é melhor?", é "agente com qual
modelo, e a diferença paga o custo?".

Se a cascata fixa vencer, ela fica — e a decisão vai documentada.

## Stack

| Camada | Escolha | Motivo |
|-|-|-|
| Orquestração | **LangGraph**, condicionado à medição acima | Ciclo com aresta condicional |
| Vector DB | **ChromaDB** | Local, sem servidor, persiste em disco |
| Embeddings | **sentence-transformers**, multilíngue | Notícia em português; local, custo zero |
| Grafo | **NetworkX** | Em processo, sem infraestrutura |
| Classificador | **scikit-learn** | Filtro barato antes da chamada cara |

Embeddings rodam localmente de propósito: o orçamento de chamada paga fica para
extração e verificação, onde o LLM é insubstituível.

**NetworkX antes de Neo4j.** A detecção de contradição é lógica, não
infraestrutura, e migrar depois é mecânico. Neo4j acrescentaria servidor e
container a um projeto onde Docker já está na fila de corte.

### Armadilha do embedding

Indexação e consulta **têm** que usar o mesmo modelo. Modelos diferentes
produzem sistemas de coordenadas diferentes: a busca não falha nem avisa, só
devolve resultado sem sentido.

Defesa: nome e versão do modelo gravados nos metadados do índice e conferidos na
consulta. Converte falha silenciosa em erro explícito.

### Custo

O único item pago do projeto é a API de LLM. RSS, SQLite, embeddings locais,
NetworkX, scikit-learn e GitHub custam zero.

Três reduções, todas previstas na arquitetura: processamento em lote (metade do
preço, e extração não tem pressa), cache do prefixo do prompt (o trecho de
instruções e few-shot é idêntico em toda chamada), e os dois filtros acima.

A saída é a parte cara e **só encolhe sendo projetada**: relação vinda do enum,
sem devolver o trecho original da matéria — que já está no banco —, teto de
triplas por matéria.

## Princípios de projeto

Funcionalidade nova que contrarie qualquer um destes está errada, ou exige
revisar o princípio de forma explícita — nunca por acidente.

1. **Nunca perguntar ao modelo se algo é verdade.** Todo veredito nasce de
   evidência externa recuperada.

2. **Todo veredito carrega a fonte.** Afirmação sem fonte rastreável não é
   apresentada como verificada.

3. **"Sem evidência" é resposta válida.** O sistema tem o direito de não saber.
   Preencher a lacuna com plausibilidade é o fracasso que o projeto existe para
   evitar.

4. **`EXTRACTED` e `INFERRED` nunca têm o mesmo peso.**

5. **Falso positivo é o pior erro.** Na dúvida entre acusar contradição
   inexistente e deixar passar, o sistema deixa passar.

6. **Filtro barato antes de chamada cara.** Aplicado em três lugares: o
   descarte de matéria de fonte única, a remoção de texto institucional, e o
   corte no lide. Este último é o de maior efeito — medido no acervo de 14
   matérias, só **7% das triplas pagas participam de alguma confirmação**, e
   as primeiras 5 sentenças guardam 89% delas por 35% do custo. É a pirâmide
   invertida: o fato principal vai no primeiro parágrafo e é ele que dois
   veículos publicam igual; o corpo é exclusivo por natureza, e exclusivo não
   corrobora.

7. **O ciclo serve para tentar outra query, não para insistir até inventar.**

8. **Nenhuma credencial no código.**

Teste prático para funcionalidade nova: *ela consegue citar a fonte do que
afirma?* Se não conseguir, não entra no caminho de verificação.

## Como medir se funciona

Sem estas medições, o projeto é uma promessa. Ambas dependem apenas da extração
e do acervo já coletado.

**1. Rendimento da metade autônoma.** Extrair triplas de ~50 histórias já
cobertas por dois ou mais veículos e contar em quantas há divergência real
entre eles.

Antes de concluir qualquer coisa, verificar à mão se as entidades ficaram
unificadas — resultado perto de zero pode significar "contradição é rara" ou
"minhas entidades fragmentaram", e as duas conclusões levam a decisões opostas.
Se o rendimento for real, o projeto tem uma metade que roda sem ninguém
perguntar. Se for perto de zero, o grafo não se justifica e sai.

**2. Acurácia contra checador profissional.** Rodar ~50 afirmações já julgadas
por Lupa, Aos Fatos ou Comprova, sem mostrar o veredito delas, e comparar.

Concordância com checador profissional é o único número que separa este projeto
de um agregador — e nenhum agregador consegue produzi-lo.

## Convenções do repositório

* Nenhuma credencial no código. Tudo em `.env`, versionado apenas como
  `.env.example`
* Mensagens de commit descrevem a mudança e o motivo
* Testes ao menos na camada de verificação, que é onde erro é silencioso
