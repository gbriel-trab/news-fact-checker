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
Coleta → Extração de afirmações → Classificação (factual vs opinião)
       → Busca de evidência → Verificação → Entrega
                    ↑                 |
                    └── evidência insuficiente ──┘
```

O **ciclo** é o motivo da escolha do framework. Se a evidência recuperada for
insuficiente, o grafo não desiste nem alucina: volta ao nó de busca e tenta
outra query, até um limite de tentativas. Orquestradores lineares e frameworks
baseados em conversa entre agentes não expressam isso de forma natural — um
grafo de estado com aresta condicional expressa.

## Camada de verificação

O diferencial do projeto. Em vez de perguntar ao modelo se algo é verdade:

1. A afirmação é extraída como tripla `(entidade, relação, entidade)`
2. Buscam-se fontes independentes sobre essa tripla
3. O resultado é classificado em **confirmado**, **contradito** ou
   **sem evidência**
4. Toda saída carrega a fonte que a sustenta

A distinção que importa é entre **o que a fonte diz** e **o que o modelo
deduziu**. Cada relação extraída é marcada como `EXTRACTED` (explícita no
texto) ou `INFERRED` (inferida pelo modelo). São coisas diferentes e não podem
ser apresentadas com o mesmo peso ao usuário.

"Sem evidência" é uma resposta válida e esperada do sistema, não uma falha.

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

Classificador clássico (**scikit-learn**) treinado sobre dataset rotulado
manualmente, separando **afirmação factual verificável** de **opinião**.

A justificativa é econômica, não acadêmica: conteúdo de rede social é
majoritariamente opinião, e opinião não é verificável. Mandar tudo para o LLM
desperdiça chamada paga em texto que nunca produziria um veredito. O
classificador roda antes, é ordens de magnitude mais barato, e derruba boa
parte do volume.

## Decisão pendente: fonte de dados

A API do X/Twitter é paga e cara o suficiente para inviabilizar a fonte.
Alternativas em avaliação:

| Fonte | Custo | Observação |
|-|-|-|
| RSS de veículos | Grátis | Estruturado e confiável; texto limpo, bom para extração de triplas |
| Bluesky | Grátis (API aberta) | Texto curto e ruidoso; representa o caso "afirmação circulando na rede" |
| Reddit | Grátis (OAuth) | Volume alto, qualidade variável |

A escolha afeta o pipeline inteiro — formato do texto, ruído, e a proporção
factual/opinião que o classificador precisa lidar. Nenhum código de coleta será
escrito antes da definição.

## Convenções do repositório

* Nenhuma credencial no código. Tudo em `.env`, versionado apenas como
  `.env.example`
* Mensagens de commit descrevem a mudança e o motivo
* Testes ao menos na camada de verificação, que é onde erro é silencioso
