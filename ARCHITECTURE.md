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
afirmação a ser checada é escrita por outra pessoa — o autor do post que o radar
captura, ou o post de canal que esse autor escolheu citar (tipo `citado`, desde
06/09/2026; a escolha do que citar é do autor, e a premissa sai com o nome de
quem a fez) —, nunca pelo sistema. O que ele escolhe é QUAIS perfis ler, e a
lista vem do `.env`, não dele. Dizer que o sistema "detecta desinformação
sozinho" seria falso.

## Duas metades, dois gatilhos

O sistema não é um pipeline só. São dois, com gatilhos diferentes.

```
┌── INGESTÃO ─────────────────── gatilho: relógio, a cada 15 min ───┐
│                                                                   │
│   Coleta RSS → Segmentação → Seleção aos pares → Extração        │
│                                                    ↓              │
│                                   índice vetorial + grafo         │
└───────────────────────────────────────────────────────────────────┘
                                                    │
                                                    │ consulta
                                                    ↓
┌── CONSULTA ─────────────────── gatilho: um post do radar ─────────┐
│                                                                   │
│   afirmação → vira tripla → busca no acervo → julga → veredito    │
│                                   ↑              │                │
│                                   └── insuficiente? ──┘           │
│                                      (ver "O ciclo")              │
└───────────────────────────────────────────────────────────────────┘
```

A **ingestão** roda sozinha, em intervalo fixo. Prepara o acervo. É trabalho
caro e estável: cada matéria é processada uma vez, e o resultado vira índice.

A **consulta** roda quando o radar traz um post. Não coleta nem extrai matéria —
apenas consulta o que a ingestão preparou.

A regra que separa as duas, e vale para qualquer sistema RAG: **o que é caro e
não depende da pergunta vai para a ingestão; o que depende da pergunta fica na
consulta.** Extrair triplas na hora da consulta significaria reprocessar o
acervo inteiro a cada pergunta.

## De onde vem a afirmação

Esta é a decisão que define se o sistema verifica ou apenas agrega.

**A afirmação e o acervo precisam ser populações diferentes.** Checar imprensa
contra imprensa é redundante: o resultado seria "três veículos disseram o
mesmo". O valor aparece quando a afirmação vem de fora da imprensa — um post de
rede social — e o acervo serve de corpo de evidência.

| Origem | População distinta? | Autônoma? | Papel no projeto |
|-|-|-|-|
| RSS de agências de checagem | Sim — são boatos de rede social | Sim, mas já vêm com o veredito | **Gabarito de avaliação** |
| Rede social via API oficial do X | Sim | Sim | **A origem: é por onde a afirmação entra** |
| Análise econômica (premissas) | Sim | Sim | Implementada, ver abaixo |

### Rede social pela API oficial do X

Implementado em `radar.py` (a rodada, as barreiras e as duas saídas em
texto), `x_api.py` (o cliente de dados) e `x_auth.py` (OAuth 2.0): é a única
porta de entrada do sistema.

**A afirmação chega como registro do servidor, não como transcrição.** O
texto do post é o que a API devolve, literal do autor, e o tipo — post,
thread própria, citação, resposta a terceiro, retweet — é CALCULADO em código
(`x_api.classifica`) a partir de `conversation_id`, dos posts referenciados e
de `in_reply_to_user_id` comparado com `author_id`. É a diferença entre pedir
um rótulo e derivá-lo: rótulo pedido a um modelo seria opinião, metadado é
dado. Sem metadado a derivação falha FECHADO — vira `resposta`, que o radar
descarta —, porque o projeto prefere perder post legítimo a deixar entrar
resposta a terceiro. Afirmação sem fonte rastreável quebraria o princípio 2
já na entrada; aqui cada captura carrega a URL do próprio status.

**O post atravessa o radar como objeto, não como texto.** As três barreiras
— um post por id, só `post`/`thread`/`citacao` ficam, e thread pendurada em
post descartado cai junto (cadeia, até estabilizar) — leem campos do
`Post`, nunca o texto: o texto é do autor, e o autor escreve o que quiser.
Texto só nasce nas duas saídas, o que o separador recebe
(`radar.para_separacao`) e o que console e arquivo mostram
(`radar.como_texto`). A segunda ninguém lê de volta; a primeira é lida pelo
modelo e, em código, por `premissas.texto_ancoravel` — que tira o cabeçalho
e a linha do post citado antes de ancorar referentes — e esse é o único
reparse que resta, contratado nas duas constantes de `premissas`. O que
foi lido e não virou captura sai com o motivo, no arquivo do boletim e no
painel, e contado nas notas do Telegram: descarte silencioso é o que esconde
defeito.

O texto do separador é contrato com o prompt (regra 9) e com
`premissas.texto_ancoravel`, e os dois prefixos de contexto moram em
`premissas`: `contexto — post anterior do próprio autor` (a thread, ou o
autor citando a si mesmo — texto dele: resolve o que o post referencia e
ancora referente, mas a premissa dele sai no post dele, não aqui) e
`contexto — post citado pelo autor; as afirmações são de quem ele cita`
(texto de terceiro, nunca resolve nem ancora). A atribuição é comparação de
autor entre o post e o referenciado, não de texto. O cabeçalho é `POST (@handle, data):`, sem
número de rodada — o número entrava no hash da separação em cache e o mesmo
post pagava separação de novo noutra rodada — e sem URL, que é ruído de
tokens.

**Autenticação.** OAuth 2.0 com PKCE, em app do tipo Native/Public — sem
segredo, porque segredo não tem onde ficar num script que roda na máquina do
dono. Escopos `tweet.read users.read offline.access`; sem o terceiro não há
refresh token e o acesso morre em duas horas. O primeiro consentimento é
humano, no navegador (`python -m src.x_auth`); daí em diante o refresh renova
sozinho. Ele é ROTATIVO — cada uso invalida o anterior —, por isso é gravado
atomicamente em `data/x_token.json`, fora do Git, e a renovação é
serializada por lock entre processos: duas rodadas na mesma janela gastariam
o mesmo refresh e a segunda gravaria um par já morto.

**Custo.** US$ 0,005 por post devolvido, lido na documentação em 04/09/2026;
o desconto de "Owned Reads" não se aplica (vale só para a conta dona do app).
A cobrança é deduplicada numa janela de 24h UTC, então a estimativa feita no
cliente é um TETO, não uma medição — a API não devolve preço, e o rodapé do
boletim rotula essa metade como "estimado" e a da Anthropic como "medido". O
teto por handle e por rodada é de 100 posts (`radar.LIMITE_POR_HANDLE`,
US$ 0,50 no pior caso só de timeline; até US$ 1,50 com a busca à parte
descrita a seguir e os objetos de autor que ela traz). O post referenciado
NÃO é expandido na leitura da
timeline: a expansão traria o referenciado de todo post devolvido,
inclusive das respostas a terceiros que o radar descarta antes de custar.
Desde 06/09/2026 ele é BUSCADO À PARTE (`x_api.posts_por_id`, caminho
`/2/tweets?ids=`, lotes de 100), só para as capturas que precisam — o
post citado de outra conta, o pai de thread fora da janela — e pago como
leitura, contado em `lidos` e no estimado. Antes disso, 16 das 19 citações
do boletim refeito de 25/08 a 06/09 chegavam sem o texto citado. Primeira
leitura real com a busca, em 06/09: 14 posts da timeline, 3 referenciados
buscados (dois de outras contas, um do próprio autor fora da janela), o
servidor aceitou `post.fields` também nesse caminho e `includes.users`
trouxe o username; US$ 0,10 estimado (14 + 3 + os 3 objetos de autor,
contados como recurso para o estimado seguir sendo teto — se o X cobra o
usuário, e quanto, só a fatura diz). Referenciado apagado, protegido ou
inexistente não volta, e a nota conta a diferença pelos ids pedidos. Lote
que falha depois de outro ter voltado entrega o que veio (`erro.parciais`)
e a nota diz o que faltou; thread cujo pai buscado é resposta a terceiro
cai pela mesma cadeia que vale para a timeline. Os limites vieram de uma
revisão adversária do diff (22 agentes, 19 achados, 16 confirmados). Retweet é lido, pago,
descartado — não traz palavra do autor — e contado.

**Duas indefinições da própria documentação, tratadas em código.** As
páginas de fundamentos e a referência do endpoint discordam sobre os nomes
dos campos (`tweet.fields`/`referenced_tweets` contra
`post.fields`/`referenced_posts`); o cliente sonda os dois dialetos e guarda
o que o servidor aceitou. E a doc nunca afirma que o endpoint devolve post de
conta protegida — a única evidência é a descrição do escopo, que fala do que
a conta enxerga, não do que o endpoint entrega. MEDIDO em 06/09/2026, na
abertura da versão 0.2: o segundo handle do dono é conta protegida
(`protected: true` no lookup de usuário) que ele segue, e a timeline veio
inteira pelo endpoint — 6 posts num dia, 2 respostas descartadas, 2
referenciados buscados à parte, US$ 0,05 estimado. Conta protegida que o
dono NÃO segue continua sem medição, e continua fora da lista.

**Estado (05/09/2026):** o app foi criado no console do X (Native App,
escopo Read), o consentimento OAuth foi feito uma vez no navegador do dono
(`data/x_token.json`, fora do Git, com os três escopos), e a **primeira
leitura real** rodou no mesmo dia: um handle público, janela de um dia, 11
posts devolvidos. O servidor aceitou o dialeto NOVO (`post.fields`) na
primeira tentativa — a sonda continua no código porque a doc segue
contraditória. Os tipos vieram do metadado como desenhado: 6 capturas
(post, thread e citação), 5 respostas a outra conta descartadas antes de
custar, e a thread cujo pai estava na mesma janela saiu com a linha de
contexto preenchida. Custo ESTIMADO US$ 0,055 (11 × US$ 0,005); o real só
na fatura do X. Uma coisa segue não confirmada: o custo real contra a
estimativa (a de conta protegida foi medida em 06/09, ver "Duas
indefinições"). Observação de uso na época, fechada em 06/09: a
citação de post de OUTRA conta não trazia o texto citado, porque só a
timeline do handle era lida — hoje o referenciado que falta é buscado à
parte (ver "Custo").

No mesmo dia o **boletim completo** rodou sobre essas 6 capturas com
`--sem-envio`: separação dos 6 posts, zero premissas factuais (o conteúdo
era comentário de mercado — opinião, relato, previsão), logo nenhum check
nem demanda; US$ 0,21 na rodada (0,055 estimado no X + 0,15 medido na
Anthropic). O que a rodada real mostrou: quando o pai de uma thread está
na mesma janela, ele é capturado como post próprio E entra como linha de
contexto do filho — e a regra 9 de então mandava tratar o contexto do
próprio autor como texto dele, então o separador extraía as mesmas
premissas duas vezes (no pai, e de novo no filho, pagando o texto inteiro
do pai na segunda). Por construção, toda linha de contexto vem de um post
que a própria rodada capturou e confere por conta própria. Decisão do dono,
no mesmo dia: a linha de contexto serve para RESOLVER o que o post
referencia (pronome, "isso", "o encontro", nome que só está lá) e para
ancorar referente quando é do próprio autor; premissa sai só do texto do
post. A regra 9 mudou nesse sentido, com exemplo, e os dois casos do
gabarito que mediam o contrário viraram: C25 (Selic) passa a exigir ZERO
fato — a previsão fica, "a Selic está em 15%" é do pai; C26 passa a medir
resolução via contexto sem duplicata — "o encontro durou três horas" com
o nome de Esteves ancorado na linha do próprio autor, e EXATAMENTE um
fato. Bateria paga no mesmo dia, 27 casos × 2 passadas, US$ 0,57 real:
zero regressões, zero fronteiras falhando; C25 saiu só com a previsão 2/2
e C26 resolveu "o encontro" para "o encontro entre André Esteves e Trump"
com um único fato, 2/2. Ressalva honesta: o exemplo novo da regra 9 é o
texto do C25 palavra por palavra, e o detector de exemplo literal do
gabarito só marca `[repr]` a partir de 8 palavras seguidas — o post tem 6.
C25 passa a medir reprodução, não regra, até o exemplo do prompt ou o caso
mudar. Em 06/09 o caso real entrou como C28: o filho da thread, com os
2.220 caracteres do pai na linha de contexto, cobrando zero fato e — chave
nova do comparador, `proibido`, que vale para qualquer tipo, porque o
vazamento de 05/09 era opinião, relato e previsão do pai, não fato —
nenhuma premissa com seis pedaços que só existem no pai. Passou 2/2, US$
0,07: duas premissas próprias, contra as 20 do boletim de 05/09. Desde 06/09
à noite, uma thread cujo pai está FORA da janela também ganha a linha de
contexto, pela busca à parte do referenciado (ver "Custo").

**Teto diário de extração (06/09/2026).** A extração era 70% do gasto
operacional medido (US$ 13,80 de 19,24 entre 26/08 e 06/09) e o único
caminho pago sem freio: o radar tem teto por handle e a demanda tem teto
por rodada, mas `extract --historias N` e a soma das demandas de um dia
não tinham limite. Decisão do dono: "faz a média de gasto e põe o teto
como o dobro". Média por dia corrido no livro-caixa, 12 dias: US$ 1,15;
`extract.TETO_DIARIO_USD = 2,30`, por dia UTC (o carimbo do livro-caixa;
em Brasília o dia vira às 21h). Uma porta só, `confere_teto_diario`, antes
de cada chamada paga nos três caminhos — lote por história, lote por
matéria e demanda, que devolve `teto_diario` e o boletim imprime. É teto
de partida: a chamada que cruza a linha ainda acontece, e o pior caso é o
teto mais uma história.

**Post citado: só post de alguém entra no separador (06/09/2026).** Com
o segundo handle, a pergunta "o que fazer com o post citado" mudou de
forma: ele cita canais de informação; o primeiro citava comentários nos
próprios posts. Decisão do dono: o citado só vai ao separador se for post
de alguém, não resposta. Critério por metadado
(`Captura.citado_e_comentario`): raiz de conversa é canal, resposta é
comentarista. O leitor continua vendo o comentário citado no arquivo e no
Telegram, marcado "resposta, fora da separação"; o modelo não o recebe.
**Tipo `citado` (0.3, 06/09/2026, na mesma noite).** O post citado de
canal é de OUTRA conta: as afirmações factuais dele saem como `citado`,
com reescrita conferível como o fato, ancoradas na linha do post citado
(regra 10 do separador; `_roteia_citado` em código, mesmas quatro
condições do fato mais a regra 7 para quem cita), CONFERIDAS no check
como qualquer fato — "fonte, não autor", que é o título da regra 10, fala
só da atribuição, nunca de pular a conferência — e exibidas com o nome de
quem afirmou — "[CITADO de @AnaliseGeopol]" no arquivo e no
Telegram —, nunca misturadas às premissas do autor. `citado` que não
ancora, sem linha do citado ou acima do teto perde a reescrita e fica como
"nada a conferir", ainda `citado`: não vira não_verificável do autor. A
letra de "o sistema não gera as próprias perguntas" foi corrigida (a
escolha do que citar é do autor). Ordem de 03/09: C32 primeiro — o POST 4
de 26/08 do segundo handle (Rússia, 500 mil soldados; Ratcliffe em
Moscou), cujo baseline em cache falhava em 2 pontos —, C31 como negativo
(relato do citado não sai), depois a regra, depois a bateria. Revisão
adversária do diff antes de pagar (27 agentes, 24 achados, 11 confirmados
antes de o limite de uso cortar os outros): o pior era o validador
reconstruindo a reescrita de um `citado` barrado quando o cache era
relido, e ele voltando ao check; também a regra 3 e o primeiro parágrafo
da 9 ainda diziam "só fato"; `_do_autor` não valia para o citado ("minha
enquete" do citado passava); o `--conferir` do radar e o painel não
conheciam o tipo. Tudo isso corrigido com teste, menos o painel, que segue
mostrando a afirmação do citado sem o dono — limite registrado. Bateria
na madrugada de 07/09: 32 casos × 2, US$ 0,89, zero regressões — depois
de o C32 ser reescrito para cobrar a regra e não a escolha: o post citado
tem quatro afirmações conferíveis e o modelo escolhe três, variando entre
passadas (Ratcliffe/OTAN/bálticos numa, Ratcliffe/OTAN/Rússia na outra);
o caso passou a cobrar "ao menos 1 citado conferível" (`citados_min`, chave
opcional do comparador). Limite visto na bateria: "a OTAN" cai no roteador
porque sigla de quatro letras é lida como ênfase. **O teto de 3 citados
por post saiu em 07/09** ("tira o teto, vamos pagar pra ver"): deixava de
fora uma afirmação conferível, fato do autor nunca teve teto, e o custo é
segurado pela demanda por rodada e pela extração por dia. Bateria de
07/09 sem o teto: 32 casos × 2, US$ 0,91, zero regressões; o C32 rendeu 7
citados numa passada (4 conferíveis: a mobilização de 500 mil, Ratcliffe
diretor da CIA, as reuniões dele em Moscou, os bálticos na fronteira; 3
barrados pelo roteador, entre eles "a OTAN" pela sigla e "há relatos
locais" sem entidade) e 4 na outra (2 conferíveis) — a variação entre
chamadas é do modelo, não do teto, e é o motivo de o caso cobrar "ao menos
1" e não uma lista.

**Sigla não é ênfase (07/09/2026).** Os barrados do C32 abriram dois
defeitos, e só um era defeito. "A OTAN" e "WSJ" caíam porque a exclusão
de ênfase de 03/09 ("TODOS os outros empresários") dizia "caixa alta com
mais de duas letras é grito" — e o docstring prometia que IPCA e RIOT
passavam; passavam só quando o número no predicado salvava. Agora sigla é
caixa alta curta (até oito letras) que não é palavra comum (`_ENFASE`);
"TODOS", "NADA", "MINERADORAS" seguem fora. Casos antes da regra: C33
("A OTAN cercou Kaliningrado em 18 de agosto", sem número, caía no
roteador) e C34 ("TODOS os empresários estão no bolso dele", que tem de
continuar fora). "Os russos" com "há relatos locais de preparativos" NÃO
é defeito: classe sem medida cai pela regra 8, e consertar isso — gentílico
como entidade, ou nome próprio só no predicado — reabriria "o encontro que
ocorreu" + "o Brasil" de 01/09 e deixaria passar "os brasileiros estão
pessimistas". Decisão do dono: fica como está. Bateria de 07/09 com a
sigla: 34 casos × 2, US$ 0,91, zero regressões; C33 e C34 2/2, e no C32 a
OTAN passou a sair conferível. Instabilidade que fica registrada: numa das
duas passadas do C32 o modelo ABREVIOU um trecho longo do citado com
reticências ("esse foi o tema das reuniões ... avisando as autoridades"),
e a regra 4 (trecho literal) acusou — é comportamento do modelo em trecho
de 200 caracteres, não a regra do citado; entra como pauta para a próxima
mudança de prompt ("copie o trecho inteiro, sem reticências").

**Primeiro boletim com `citado`, segundo handle, 25 a 30/08 (07/09/2026,
US$ 1,77 nas seis rodadas).** O tipo pagou no primeiro dia com conteúdo:
o post citado sobre a enchente no Nepal dizia 291 turistas estrangeiros
desaparecidos, e o check devolveu CONTRADITO pelo G1 — 341 estrangeiros,
com o total de 384 batendo. Em 30/08, "o AfD lidera as intenções de voto
na Saxônia-Anhalt", citado de um canal, saiu CONFIRMADO por 2 veículos
depois de a demanda extrair a matéria. O resto foi "sem evidência" —
Fed, Ratcliffe em Moscou, drone em Leipzig, mobilização russa —, o
esperado para geopolítica estrangeira num acervo brasileiro. Observação
que fica: para "a Rússia estaria preparando uma mobilização de 500 mil",
a demanda trouxe as matérias certas (Putin nega plano de alistamento em
massa, G1 e Metrópoles) e o juiz manteve "sem evidência" — negação do
próprio acusado não conta como evidência independente. É a questão de
atribuição em aberto ("Fulano disse X" contra "X é verdade") aparecendo
na saída, não defeito. O dia mais denso (26/08) custou US$ 0,83, o dobro
de sem o tipo: seis citados conferidos e três demandas.

**Refazer dias passados (06/09/2026).** `radar.busca` e `boletim.monta`
aceitam `desde`/`ate` (só por nome; a assinatura antiga segue valendo), e
o boletim ganhou `--desde`/`--ate`: janela explícita em UTC, fim
EXCLUSIVO no endpoint, `--dias` ignorado quando `--desde` existe. O
cabeçalho mostra a janela em vez de hoje; o arquivo continua sendo o do
dia em que rodou, em append. Com `--reenviar`, o já entregue volta. Foi
assim que o boletim foi refeito de 25/08 a 06/09, um dia por rodada, de
baixo para cima: 13 rodadas, 103 posts lidos, 71 descartados antes de
custar (69%: resposta a outra conta, no metadado) e 32 no boletim; 9
checks, 2 confirmados (2 e 4 veículos) e 7 sem evidência; US$ 1,63 no
total (X US$ 0,52 estimado + Anthropic US$ 1,11 medido), ou US$ 0,125 por
dia — a estimativa feita antes, de US$ 6 a 10, usava os posts que a
fonte antiga entregava como se fossem capturas, e a fonte antiga
transcrevia as respostas a terceiros como posts. Nada foi reaproveitado
do cache: a versão do prompt tinha mudado na véspera (regra 9) e o
veredito só é reusado por 24h. Medição que fica: neste handle, o boletim
diário custa na casa de US$ 4 por mês.

**Fechado em 06/09/2026, visto na leitura manual do boletim refeito —
dois defeitos, um em cada ponta.** (1) O separador emitiu FATO para
referente genérico do próprio autor: "Na enquete do autor sobre cripto,
55% votaram…" (a prova é o post dele — relato, regra 7) e "As altcoins que
o autor postou andaram entre 40% e 50%" (não diz quais). O roteador deixou
passar porque "a enquete" e "as altcoins" ancoram e trazem número. (2) A
demanda escolheu as candidatas por proximidade semântica sem exigir o
referente da premissa: para esses dois fatos extraiu 7 matérias de
pesquisa eleitoral (Ciro, Elmano, Datafolha, escala 6x1) e 7 triplas sobre
eleição — casou por porcentagem e pela palavra "pesquisa". Custo dos dois
juntos: US$ 0,18 por dois fatos que não existiam, o padrão do C3 (US$ 0,36
em 01/09).

O que mudou, na ordem de 03/09 (caso positivo primeiro, regra depois):

* **Gabarito**: C29 é o post real de 25/08 (56 premissas); C30 é a forma
  curta, sintética. Baseline grátis, pela separação em cache da rodada
  real: o C29 falhava em 3 pontos. C29 cobra os dois relatos e PROÍBE fato
  com "enquete", "alts", "altcoins" ou "votaram" — o número de fatos ficou
  livre depois de a primeira bateria tirar do post um fato legítimo ("a
  queda do BTC foi uma correção de 50% a 61%"): caso de post longo mede a
  regra, não o post inteiro.
* **Regra 7 do separador** ganhou a cláusula "coisa do próprio autor não é
  referente do mundo" (minha enquete, as alts que postei, o post que fiz),
  com exemplo diferente dos casos.
* **Roteador, condição 5** (`_do_autor`): rebaixa a RELATO, não a
  não_verificável, o fato cujo sujeito é coisa do autor — possessivo de
  primeira pessoa seguido de palavra minúscula (sem casefold: "Minha Casa
  Minha Vida" e "Meu INSS" são nomes próprios), verbo em primeira pessoa
  ("que postei", "que rodei"), reescrita atribuindo ao dono ("do autor",
  "que o autor postou"; não "autor de Torto Arado", "autor intelectual",
  "pelo autor", "autor dos ataques") ou ao handle do cabeçalho ("na enquete
  de @handle", que é a resolução que a regra 3 induz). Residual conhecido:
  "a obra do autor vendeu 1 milhão" ainda casa. As regexes entram na versão
  do roteador; mudá-las é bump de prompt.
* **Demanda, guarda de referente** (`_menciona`): candidata só paga
  extração se título + começo do corpo — o MESMO texto que o ranking
  vetorial viu; Estadão e UOL chegam com resumo vazio — mencionar, em
  fronteira de palavra, um termo útil do QUEM da premissa: sem pontuação
  colada ("Ibovespa, Nasdaq" era "ibovespa," e nunca casava), sem artigo,
  pronome ou cabeça genérica ("taxa", "governo", "banco", "pesquisa": "a
  taxa de juros" só casa por "juros"). Sem termo útil, não filtra — o erro
  caro é o falso negativo de cobertura. Flexão não é tolerada ("marcas
  icônicas" não casa "marca icônica"): limite registrado. Quando o
  referente traz nome próprio (@handle, $ticker, inicial maiúscula), só o
  nome vale: "O cartão da @ether_fi" pagou uma matéria da Ethena por
  "cartão" e "cashback" (segundo handle, 26/08, US$ 0,08) antes disso.
* **Exibição**: quando o post referenciado está na mesma rodada, a linha
  de contexto aponta ("é o post 1 desta rodada", no arquivo, no console e
  no Telegram — no [CITANDO] com o handle) em vez de repetir o texto; o
  separador continua recebendo o texto. E as capturas saem do mais velho
  para o mais novo, por instante de publicação: a API devolve o inverso, e
  o filho da thread saía antes do pai (31/08, apontado pelo dono).

Os limites das regexes vieram de uma revisão adversária do diff por 18
agentes sem contexto (15 achados, 9 confirmados por reprodução offline),
antes de pagar a bateria. Bateria de 06/09: 30 casos × 2 passadas, zero
regressões, US$ 0,87 — contando uma rerrodada de C29/C30 porque a API
devolveu 529 no meio da primeira. C29 e C30 aguardam assinatura.

**Fechado em 08/09/2026, achado pelo dono lendo o boletim refeito de
26/08 do segundo handle.** O citado "o WSJ noticiou que o diretor da CIA,
Ratcliffe, esteve em Moscou" saiu SEM EVIDÊNCIA, e o G1 tinha a matéria
("Em visita surpresa a Moscou, chefe da CIA pediu que Rússia não ataque
países da OTAN, diz jornal", 26/08). O acervo tinha CINCO: G1, Folha e
mais três, de 25 a 27/08, ranqueadas pelo índice a 0,87–0,90 da premissa e
todas mencionando o referente — e nenhuma extraída. A demanda filtrava
candidata por "data >= hoje − 10 dias", e o boletim de 26/08 foi refeito
em 07/09: as matérias da época caíam fora, e o que sobrava eram três
matérias recentes, irrelevantes, que o juiz recusou certo. O check em si
não tem janela de data — se a tripla existir, ele acha. A janela da
demanda agora fica em torno da DATA DO POST (`demanda._janela`: 10 dias
para cada lado, "agora" só quando o post não tem data); o boletim passa
`criado_em`. Nada de prompt, sem bateria; o boletim ao vivo não muda de
comportamento, porque para post de hoje a janela é a mesma. Refazer dia
passado é feature registrada acima, e o freio de data era o único ponto
que ainda contava "de hoje".

Três limites medidos no mesmo dia, sem gastar — a seleção de candidatas da
demanda roda offline, e toda proposta abaixo foi simulada nas 47 premissas
reais de fato/citado já separadas antes de virar decisão:

* **Referente que é cargo sem nome não casa com a imprensa.** "O Ministro
  das Relações Exteriores do Nepal informou que um terremoto provocou a
  avalanche": o `quem` veio como o texto escreve, "Ministro das Relações
  Exteriores", e a guarda exige isso no título+lead. Nenhuma das 12 mais
  próximas traz; a que cita o ministro pelo cargo (G1, "o que se sabe até
  agora") está em 25º, com título genérico; a que contradiz o terremoto
  (G1, "USGS corrige e diz que não houve terremoto") é a 7ª, mas nunca
  cita o ministro — e a vaga do G1 vai para "Terremoto atinge o Tibete",
  outro tremor. Aceitar termos do `o_que` na guarda ("terremoto",
  "avalanche") rende, nas 47 premissas, +6 matérias (US$ 0,15), metade
  ruído: Bessent e "a curva de juros" puxam "Ibovespa hoje ao vivo" por
  juros+títulos+tesouro, o padrão de 06/09. Raridade do termo no acervo
  NÃO separa os dois casos — "juros" está em 1,3% dos 18.818 documentos,
  "terremoto" em 0,3%, "avalanche" em 0,7%: três semanas de coleta em que
  o Nepal foi a maior história. Exigir um termo raro só explode (52 → 83
  matérias por "abaixo", "rússia", "putin"). Fica como limite: referente
  de cargo, e referente em inglês que não é nome próprio ("Moscow"), não
  chegam à demanda. Mesma família: a premissa relata o que o ministro
  DISSE, e contradizer o terremoto não contradiz o relato — a questão de
  atribuição, em aberto.

* **Post sobre notícia mais velha que a janela.** Distância diferente da
  que o conserto acima fechou: ali era rodada→post; aqui é post→evento.
  Post de hoje sobre o cerco a Kaliningrado de 18/08 procura matérias em
  torno de hoje. O conserto pronto é centrar a janela no `quando` da
  premissa quando ele vem como data cheia — e NÃO foi implementado, por
  três razões medidas e uma de princípio. Medidas: das 47 premissas, 5 têm
  `quando`, 2 com data cheia, 0 a mais de 10 dias do post (quem posta
  comenta a notícia do dia); das 28 com zero candidatas, quase nenhuma é
  janela — são TOTAL3 e altcoins que a imprensa não cobre, premissas em
  inglês, a enquete do autor. De princípio: hoje TODA âncora temporal do
  funil é fato (data do post pela API, data da matéria pelo feed,
  `data_fato` da extração); `quando` seria a primeira saída de modelo a
  decidir ONDE a demanda gasta. Já há saída de modelo dirigindo a demanda
  (`quem`, na guarda), mas com assimetria: `quem` errado dá zero
  candidatas e custa nada; `quando` errado dá candidatas do período
  errado, paga extração, e as triplas entram no índice para sempre — a
  "Selic de janeiro contra agosto" autoinfligida, sem filtro de data no
  check. Se um caso real aparecer, entra com três guardas: só quando o
  `trecho` traz dia, mês e ano literais (resolução de "ontem" pelo
  cabeçalho não vale como âncora de gasto); data nunca posterior ao post
  nem anterior ao acervo; e o boletim imprime a âncora usada. E nunca uma
  regra de prompt "resolva sempre a data" — seria pedir ao modelo para
  preencher lacuna com plausibilidade (princípio 3) num campo que passa a
  mover dinheiro.

* **O juiz não sabe quando a afirmação foi feita.** Recebe a data do fato
  de cada evidência e alinha a lacuna "quando" (regra 6), mas só quando a
  afirmação traz data; "a Selic está em 15%" chega sem referência
  temporal, e uma tripla de outro mês pode render CONTRADITO pela regra 4.
  Exposição hoje pequena (1.218 triplas de ago/2026, 216 de set, 175 de
  jan–jul, 90 anteriores a 2026 — e 1.342 sem `data_fato`, sem defesa
  alguma); cresce a cada mês de coleta. Conserto: data do post como
  referência no prompt do juiz, com regra de que evidência distante no
  tempo, quando a afirmação não data, não sustenta nem contradiz. É
  mudança de prompt: entra só com a bateria do check.

**Decisão da noite de 08/09/2026: os três consertos acima e a fala de
terceiro entram ANTES da v0.2.0, com caso sintético primeiro** (o
gabarito aceita sintético — o C30 é um — e a regra de 03/09 é caso antes
da regra, não caso real antes da regra). O que ficou para a v0.3: a
recuperação do caso do ministro (guarda de referente), a retratação
("mesmo veículo, versão mais recente vence") e a negação na extração.

* **Tempo no juiz (A).** A data do post vai ao check como `referencia`
  (fato da API; a afirmação avulsa é de hoje). Antes do juiz, e ANTES da
  escolha de candidatas, em código (`check.por_referencia`, chamada de
  dentro de `recupera`): **sai o estado cuja MATÉRIA foi publicada depois
  da referência** — quem afirmou não podia saber, e a Selic de dezembro
  não contradiz um post de outubro; e **entre estados da mesma coisa fica
  o vigente**, sendo "mesma coisa" (veículo, sujeito canônico, relação,
  OBJETO canônico, unidade). Evento nunca é tocado, porque não expira;
  veículos diferentes não se substituem — se discordam no mesmo instante,
  quem diz é o juiz. Empate no instante mantém todos os empatados, e o
  desempate é a hora de publicação da matéria. O prompt do juiz recebe
  "DATA DE REFERÊNCIA" e a regra 8 diz o mesmo em prosa, para o que o
  código não alcança (a afirmação sem data descreve o estado na
  referência). Casos J19–J24, todos sintéticos: agosto contra dezembro em
  outubro (confirmado sem citar dezembro) e em janeiro (contradito sem
  citar agosto), junho e agosto do mesmo veículo, setembro que discorda
  (controle contra o juiz tímido), estado sem data, e a visita da CIA
  três meses depois (evento não expira).

  **Cinco escolhas desta barreira vieram da revisão adversária da mesma
  noite, e todas as cinco primeiras versões estavam erradas** — três
  lentes independentes sobre o diff, 35 achados, 20 confirmados por
  reprodução offline:
  * **O eixo é a publicação, não o fato.** `data_fato` posterior é
    legítimo em projeção, meta e orçamento: no acervo real são 40 triplas
    de estado ("salário mínimo deverá subir para R$ 1.741 em 2027",
    publicada em 25/08/2026), e cortar por ela as tornava invisíveis para
    qualquer check. Projeção agora fica, e não substitui o valor vigente.
  * **O objeto entra na chave.** Sem ele, "Esteves integra o BTG" e
    "Esteves integra o conselho da B3" eram a mesma medida e a mais antiga
    sumia — dois fatos, não um valor que mudou. É a mesma distinção que
    `grafo.Afirmacao.chave` já fazia.
  * **Empate mantém todos os empatados.** Guardando um índice só, o
    empatado não saía nem virava vigente, e um terceiro mais novo o
    deixava vivo ao lado do vigente: estado superado chegando ao juiz como
    matéria-prima de contradição falsa. Empate é o caso comum, não a
    exceção: entrada indexada antes de 08/09 não tem hora de publicação.
  * **`outro` precisa do tipo que o modelo deu.** É a válvula de escape do
    vocabulário (622 triplas, 225 delas estado): sem o palpite,
    `vocabulario.tipo_de` devolve "evento" e a barreira inteira ficava
    desligada ali. `tipo_relacao` passou ao metadado das duas rotas.
  * **A barreira roda antes da reserva de diversidade.** Filtrando depois,
    a vaga reservada ao segundo veículo era gasta numa evidência que a
    barreira ia remover em seguida, a vaga não voltava ao ranking, e a
    lista chegava ao juiz com um veículo só — o oposto do que a reserva
    existe para garantir, e do critério de duas fontes do AC1.
  E **lista esvaziada pela barreira não é "o acervo não cobre"**: o check
  passou a dizer "evidência fora da janela temporal" e a gravar `retida`,
  senão o boletim dispararia extração paga para cobrir o que já está
  coberto — o gasto que a marca existe para evitar desde 03/09.

  Dois efeitos colaterais consertados junto: a janela de reuso de 24h
  casava só pelo TEXTO da afirmação, e passaria a devolver o veredito de
  outra data de referência quando dois dias são refeitos na mesma sessão
  (a referência virou coluna de `consultas` e entra no casamento; linha
  antiga vale como veredito de hoje); e o rótulo `[segundo X]` só sai onde
  existe reescrita (fato e citado) — em opinião e previsão o que se exibe
  é o trecho literal, que já traz o "o ministro disse que", e o rótulo
  repetia a fonte na mesma linha (o eco de 02/09 voltando por outra
  porta). O campo `fonte` continua gravado nos outros tipos, como
  diagnóstico, do mesmo jeito que `hipotese`.

* **Janela da demanda na data escrita (B).** `premissas.data_literal`
  lê o TRECHO do `quando` — nunca o `valor`, que é resolução do modelo —,
  exige que ele ancore no texto do autor (a mesma âncora do `quem`, que
  exclui o cabeçalho e a linha do post citado) e traga dia, mês e ano
  ("18/08/2026", "18 de agosto de 2026", "August 18, 2026"). O boletim
  centra a janela nela (`_ancora_da_demanda`) com dois freios, nunca
  depois do dia do post nem antes da matéria mais antiga do acervo, e
  imprime a âncora quando a usa. Data numérica é dia/mês/ano: "08/18/2026"
  cai na validação, "05/08/2026" num post em inglês é 5 de agosto —
  limite registrado. Chave `quando_literal` no gabarito, conferida pela
  MESMA função. Casos C35–C39: numérica, por extenso, inglês, controle
  relativo ("semana passada"), e duas datas no texto com só uma de
  ocorrência.

* **Veredito `dividido` (C).** Quarto veredito, para veículos DIFERENTES
  incompatíveis entre si sobre o mesmo instante e o mesmo fato — um
  sustenta, outro contradiz. Nunca conta como confirmação (AC1), mostra
  os dois lados, e um veículo de cada lado já divide: o sistema não
  escolhe lado (regra 9). Em código, `aplica_alinhamento` retém divisão
  com menos de dois veículos citados (vira sem_evidencia retida, sem
  disparar demanda). Não é divisão: evolução no tempo (regra 8, e o
  código já deixou só o vigente), arredondamento, o mesmo veículo em
  versões, negativa de parte interessada. `consultas.veredito` tinha
  CHECK com três valores e o SQLite não altera CHECK: `_migra_veredito`
  recria a tabela copiando todas as colunas, numa transação, depois de
  backup (`data/backups/`, 08/09). Casos J25–J29: Folha 291 contra G1
  341 (dividido), evolução do mesmo veículo (confirmado), manhã e noite
  do mesmo dia (contradito pela hora), dois veículos contra o post
  (contradito), arredondamento (confirmado por dois). O J27 nasceu com a
  hora numa chave que `_achados_de` não lê, e o desempate — a única razão
  do caso — nunca rodava; o teste de forma do gabarito passou a rejeitar
  chave desconhecida dentro de `evidencias`, para um caso não voltar a
  medir campo inerte. E o J26 pedia "mais de 1.000" contra uma evidência
  de 1.050: o freio de alinhamento retinha SEMPRE, e o caso mediria o
  freio numérico em vez da regra 8.

* **Fala citada é evidência de que alguém disse (regra 10 do juiz).**
  "X afirmou/negou que Z" sustenta que X disse Z. Negativa de PARTE
  INTERESSADA não contradiz o que outro veículo relata na própria voz
  (decisão do caso Putin, 07/09, agora regra): J30, Kremlin nega a visita
  da CIA → confirmado por um veículo, com a negativa na justificativa.
  A outra ponta, sem a qual a regra apertaria só para um lado: J31,
  constatação de AUTORIDADE sobre o fato (USGS: não houve terremoto),
  relatada pelo veículo, contradiz "um terremoto provocou a avalanche".
  É o primeiro caso positivo do Nepal no gabarito — o lado do juiz; a
  recuperação segue na v0.3. O precedente que a regra abre, dito em voz
  alta: o juiz passa a classificar quem fala em relação ao fato, parte
  ou autoridade — classificação, como o alinhamento de lacunas, não
  "perguntar se é verdade" (princípio 1); a fronteira é difusa, e por
  isso entra medida nas duas pontas.

* **Fala de terceiro dentro do post (D, regra 7 do separador).** "O
  ministro informou que Y", "segundo a PF, Y": quem disse não é a
  premissa; Y é, no tipo que Y tem por si (fato, opinião, previsão), e
  quem disse vai em `fonte`, exibido como "[segundo X]" no radar, no
  arquivo e no Telegram. Negativa não desembrulha ("X negou que Y" é o
  fato da negativa; Y não vira fato por ter sido negado). Terceiro citado
  nunca é `relato`. É a terceira forma da mesma lógica: a regra 7 já
  desembrulhava o "eu disse" do autor, e a regra 10 confere o conteúdo
  do post citado; faltava a fala citada DENTRO do texto, que saía com o
  embrulho e fazia o check conferir "que o ministro informou". Chave
  `fonte_min` no gabarito. Casos C40–C44: o ministro do Nepal (o caso
  cobra o desembrulho e a fonte, não o roteamento — "terremoto" é sujeito
  de classe e pode cair no roteador), opinião dita por terceiro, previsão
  dita por terceiro, "segundo a PF" com número, e a negativa como
  controle. Exemplos do prompt (IBGE, presidente) diferentes dos casos de
  propósito: exemplo dentro do prompt só prova reprodução. O J32 fecha o
  par que faltava entre as duas regras: premissa desembrulhada cuja única
  evidência no acervo é a tripla de atribuição do próprio falante
  ("ministro afirmou que houve terremoto") tem de dar `sem_evidencia` —
  sem ele, o par positivo/negativo ficava aberto justamente na direção
  que custa, "CONFIRMADO · 1 veículo" para o que ninguém verificou.

Bateria de 08/09/2026, as duas: **zero regressões, US$ 3,80** (separador
44 casos, check 37, 3 passadas cada). O check passou inteiro de primeira,
incluindo os catorze casos novos. O separador achou duas coisas, e as duas
eram do gabarito, não do modelo:

* **C7, e é o achado que importa.** "Galípolo disse que não vai cortar os
  juros em setembro" passava desde 03/09 como FATO sobre o dizer, e a regra
  7 nova o transformou em previsão sobre o conteúdo — 3 passadas de 3, o
  modelo obedecendo exatamente ao que escrevi. O caso já registrava a lacuna
  ("o prompt não tem regra para 'X disse Y' de terceiro; duas leituras
  honestas"), e a regra escolheu o lado errado sem que eu percebesse: o
  DIZER é conferível no acervo (relação `afirmou`), e alegar declaração que
  não houve é justamente o que precisa ser checado. A regra 7 passou a ter
  duas metades: desembrulha quando o conteúdo se confere sozinho; quando não
  se confere (juízo, futuro), a premissa é "X disse Y", tipo fato, com
  `quem` = X. C41 e C42 mudaram de expectativa junto, e o C7 voltou a passar.
  Segunda rodada do separador, só ele: US$ 0,61.
* **C38 cobrava tradução, não regra.** O post é em inglês e o `contem` pedia
  "Kaliningrad"; a reescrita sai em português ("Kaliningrado"), e o modelo
  acertou o conteúdo nas três passadas. O caso existe para a data literal em
  inglês, que passou — a expectativa virou o ano mais o referente ancorado
  ("NATO", como o texto escreve), que sobrevivem à tradução.

Instável conhecido: C19 passa 2 de 3 ("recuperação judicial" no singular
contra "recuperações judiciais" do caso), que é o limite de flexão do
comparador já registrado acima. Não barra: regressão é falhar em todas.

A bateria em duas etapas nasceu desta rodada, por causa do custo: uma
passada em todos (US$ 0,53) e três só no que falhar, em vez de três em todos
(US$ 1,58). Mesmo poder de detecção — uma falha isolada nunca foi regressão
—, um terço do preço. O gabarito já respondia por 37% do gasto do projeto
(US$ 13 de US$ 35 até 08/09), atrás só da extração.

#### Não existe "o que está em alta"

O radar lê a timeline dos handles escolhidos, numa janela de data, e só
isso. Não há pergunta "quais assuntos estão em alta": o sistema não escolhe
assunto — ele confere o que os perfis acompanhados afirmaram (ver "O que o
sistema é, e o que não é"). Trending é outro produto, e devolveria a
impressão de alguém sobre o que circula, que tem forma de dado e não é.

#### A lista de handles é o RSS do radar

`HANDLES_RADAR` (no `.env`) funciona como a lista de feeds: fonte curada,
coleta periódica, janela de data. Mesmo mecanismo, **papel oposto**:

```
RSS       →  ACERVO   →  é a evidência      →  o que os jornais afirmam
handles   →  RADAR    →  é o que se checa   →  o que alguém alegou
```

Post nunca entra no acervo. Confirmar boato com boato quebra o critério do
AC1, que exige duas fontes jornalísticas independentes.

E por isso o critério de seleção é o INVERSO do dos feeds: no RSS entram os
veículos em que se confia; nos handles entram os que **produzem alegação**.
Colocar `@g1` e `@folha` na lista devolveria o acervo conversando consigo
mesmo.

### Análise não é notícia, e a premissa dela é verificável

Implementado: `premissas.py` separa, `check.py` julga, `boletim.py` entrega
o radar conferido. O desenho atual e as lições que o produziram estão em
"Separar, rotear, julgar — e o gabarito", abaixo.

Comentário econômico — o material dos perfis que se acompanha por interesse
real — é opinião e previsão, que o `extract.py` descarta de propósito. Mas
opinião se apoia em fato:

```
"o BC vai ter que subir juros, a inflação de julho veio em 5,2%"
  │
  ├─ previsão:  BC vai subir juros     ← não verificável, e nem deve ser
  └─ premissa:  IPCA de julho = 5,2%   ← verificável
```

Descartar a frase inteira joga fora o número junto com o palpite.

**O modo de falha do comentário não é mentir, é raciocinar bem a partir de um
número errado** — citado de memória, de dado velho, ou arredondado torto. É o
que este acervo pega bem, porque guarda valor com unidade, contexto e data. O
próprio acervo já mostrou o risco existindo: o G1 publicou 56 bi e 59 bi para
a mesma dívida da Braskem no mesmo dia, e um analista que pegasse o número
errado herdaria o erro no argumento inteiro.

Custa pouco: um prompt que extrai PREMISSAS de um texto argumentativo, e o
`check.py` inalterado julgando cada uma. Índice, grafo e digest reaproveitados.

**A armadilha de enquadramento**, e é a séria. A saída não pode virar placar:

```
✗ "@fulano: 2 premissas confirmadas, 1 sem evidência"   ← nota de credibilidade
✓ "o número citado bate com G1 e Agência Brasil"        ← conferência
```

Premissa sem evidência não significa que o analista errou — significa que o
acervo não cobre. Confundir os dois transforma a ferramenta em máquina de
acusar, que é outro produto e não é este.

### Separar, rotear, julgar — e o gabarito

Quatro incidentes em quatro dias desenharam esta parte, e vale registrar a
sequência porque o erro de método foi tão caro quanto os erros de prompt:

| Dia | Incidente | O que se aprendeu |
|-|-|-|
| 31/08 | "o autor opera assim" foi 11 vezes ao check | relato não é fato: tipo `relato` |
| 01/09 | "o empresário" gerou 4 fatos sem sujeito, US$ 0,36 de demanda inútil | falha de ROTEAMENTO: extraiu certo, mandou ao check errado |
| 01/09 | "ocorreu um encontro" saiu CONFIRMADO por uma sessão de comissão qualquer | falha do JUIZ: evidência compatível não é evidência que sustenta |
| 02/09 | a regra escrita na véspera para os dois casos engoliu "André se reune com Trump" | uma regra de prosa, com critério subjetivo e sem caso positivo, só pode "melhorar" apertando |

A correção de 02/09 (regra 8 v3) acertou o caso e errou o método: prosa no
prompt, sem saída observável, validada só contra casos negativos. O desenho
de 03/09 troca comportamento por estrutura, em três lugares:

**1. O separador emite referente como campo, não como impressão.** Todo
`fato` traz `quem` e, quando o texto dá, `o_que` e `quando`, cada um como
`{valor, trecho}`: o valor COMO O TEXTO ESCREVE (nome pela metade fica pela
metade) e o trecho literal onde aparece. Quinto tipo, `nao_verificavel`:
afirma algo sobre o mundo, mas o texto não identifica o referente — "Banco
dele" não é opinião, e chamá-lo de opinião era mentira de taxonomia. O que o
modelo desconfia vai em `hipotese`, sem âncora, e nunca é verificado.
`quando` é data de OCORRÊNCIA ancorada num trecho ("ontem" → 31/08/2026);
data de janela carimbada da data do post não conta — medido: sob a v3, zero
carimbos em 50 separações; sob a v1, os dois únicos carimbos estavam
justamente nas afirmações vazias.

**2. O roteador é código.** `premissas.roteia` confere as âncoras contra o
texto que o modelo recebeu e rebaixa a `nao_verificavel`, com motivo, o fato
que não passa por quatro condições: sujeito ancorado; `o_que` ou `quando`
ancorado; `o_que` não é pronome ou advérbio ("André foi lá"); entidade
nomeada, número ou data de ocorrência em algum lugar ("o empresário" + "um
banco" não; "desemprego" + "5,3%" sim). É o princípio 6 em código: filtro
barato antes da chamada cara. Na primeira bateria (50 separações) o modelo
classificou tudo certo sozinho e o roteador não interveio — ele é o backstop
que torna a decisão observável, não a barreira principal.

**3. O juiz alinha antes de julgar, e o código retém.** `Julgamento` ganha
`alinhamento` (quem, o quê, quando, onde, quanto) preenchido ANTES do
veredito, e a regra 6: lacuna afirmada sem contraparte não confirma. Prosa não
bastou três vezes, então `check.aplica_alinhamento` retém em código — e a
primeira versão desse freio, que conferia o sujeito contra o texto que o
PRÓPRIO juiz escrevia no alinhamento, durou algumas horas: o campo é livre, o
juiz tende a parafrasear a evidência com a palavra da afirmação, e a consulta
82 passaria de novo. O freio conta o que o código já tem na mão:

* a afirmação precisa nomear um sujeito determinado — `_identifica` recusa
  substantivo comum solto ("Encontro", "o empresário"), e o sujeito vem do
  ESTRUTURADOR, não do juiz;
* alguma evidência CITADA precisa ter sujeito ou objeto que case
  (`sujeito_casa`) E conter um apoio da afirmação (o objeto ou o valor
  estruturados). Data não fecha referente: toda tripla tem data e o juiz a
  preenche sempre, então ela nunca conta como o segundo apoio;
* sem apoio estrutural (afirmação sem objeto nem valor), a decisão semântica
  do juiz vale — o freio é backstop, não segundo juiz.

`sujeito_casa` é contenção de tokens pela chave canônica com duas guardas
medidas contra sujeitos reais do acervo: a interseção precisa de um token
não-genérico ("governo" ⊄ "governo federal") e os tokens extras do lado maior
não podem ser cabeça de hierarquia ou evento ("Lula" ⊄ "governo do presidente
Lula", "Trump" ⊄ "Telefonema entre Lula e Trump") — sem elas, o freio
fabricaria exatamente a corroboração que `canonico.py` se recusa a fabricar.
Falso negativo aceito, pelo princípio 5: o mesmo token que separa a corte do
ministro dela separa o juiz da pessoa. Retido mantém as evidências visíveis,
rotuladas, e marca `retida` — "achei isto e não conferi" não é "o acervo não
cobre", e o boletim usa a distinção para NÃO disparar extração paga sobre o
que já está coberto.

Três achados laterais da mesma semana, todos com teste. `recupera()`
deduplicava candidata por sujeito+texto sem veículo, e o modo história grava
a mesma tripla para cada veículo que a afirma — a cópia do segundo veículo era
descartada e a corroboração saía subcontada ("1 veículo" com dois no acervo);
além disso um veículo enchia 7 das 10 vagas com triplas do mesmo evento, então
duas vagas ficam RESERVADAS para veículos ainda não representados (reserva no
fim, não teto por veículo: o teto testado trocava a confirmação do BTG ao g1
por "Trump exerce cargo nos Estados Unidos"). O índice vetorial nunca podava:
2.594 ids para 2.157 do recorte, 478 órfãos — entre eles a cotação do Bitcoin
pré-regra 4 que este documento dá por curada — e 41 triplas ativas ausentes; o
grafo lia a versão nova e a rota vetorial servia a antiga, sem nada acusar.
E recusa de grupo na extração (`mesma_historia=false`) contava como "matéria
já extraída" para a demanda, deixando invisível uma matéria que entrou num
grupo errado; agora leva a marca `recusada` e volta a ser elegível.

### A terceira saída: contexto, quando não há premissa para conferir

**DESLIGADA em 03/09/2026, no mesmo dia em que entrou** (`contexto.LIGADO = False`). O código, os testes e os limiares medidos ficam; o que falta é o gate.

Ela saiu em 4 dos 5 posts de um boletim entregue e errou em 3: "pessoas não identificadas teriam encontrado um erro de CEP" (11 matérias, 5 veículos), "analistas macroeconômicos não identificados" (64 matérias, 11 veículos), "uma ferramenta de IA não conseguiu resolver algo" (3 matérias, 2 veículos). Nenhuma é assunto.

E o gate por CONTAGEM não pode funcionar — medido:

|hipótese|matérias|veículos|% do acervo|
|-|-|-|-|
|C19, o caso BOM|10|7|0,10%|
|erro de CEP, ruim|11|5|0,11%|
|anunciaram topo, ruim|9|4|0,09%|

Subir o piso mata o caso bom junto; baixar deixa tudo passar. A diferença é semântica, e contagem não vê semântica. Voltar exige um gate que julgue se a hipótese NOMEIA um assunto — chamada de modelo, com custo e com caso no gabarito.

Implementada em 03/09/2026 (`src/contexto.py`). Nasce de um post real:

> "Alguém consegue ainda manter as contas de quantas recuperações judiciais
> estão acontecendo em marcas icônicas? Devem ser só as icônicas né? As que
> não tem marca devem estar bem! (contém ironia)."

O separador acerta ao não extrair fato: "marcas icônicas" não identifica
quais, "quantas" não é número, e a pergunta é retórica. Sob a v1, que
extraía, a premissa vaga voltou **sem evidência duas vezes** e custou
US$ 0,077 — não por falta de acervo, mas porque afirmação vaga não casa com
tripla nenhuma.

Só que o acervo cobre o assunto fartamente. Em 03/09 ele tinha 31 matérias
sobre recuperação judicial em onze dias: Braskem (R$ 56 bi), Habib's
(R$ 265,2 mi), Casas Bahia, OSX, Lupatech, Novonor, Grupo Gennius — e uma
manchete do Estadão dizendo que o estoque de empresas em recuperação é
**recorde**. A insinuação do post é corroborada por dois veículos, e o
sistema não tem onde dizer isso.

O buraco não é de prompt, é de produto. Faltam as duas coisas ao mesmo
tempo: não é `confirmado`, porque não há premissa bem-formada para
confirmar; e não é `sem evidência`, porque o acervo cobre. É uma terceira
resposta:

```
[ACERVO] o autor sugere que há um número alto de recuperações judiciais em
         curso, mas não identifica quais empresas ou marcas
         — o acervo registra 15 matérias em 7 veículos (26/08 a 02/09)
         · InfoMoney · G1 · Folha
```

Duas coisas mudaram do rascunho acima para o que foi construído, e as duas
são correções, não simplificações:

**O assunto vem da `hipotese`, não do texto do post.** Medido em 03/09/2026
sobre o acervo de 8.644 matérias:

|busca|C19 (recuperações)|C3 ("o cara tem: banco dele")|
|-|-|-|
|pelo TEXTO do post|14 matérias, 5 veículos|**200 matérias, 13 veículos**|
|pela HIPOTESE|15 matérias, 7 veículos|**0 matérias**|

Post longo casa com o acervo inteiro. Buscando pelo texto, o C3 trazia 200
matérias e os primeiros achados eram sobre golpistas com IA e propaganda
eleitoral — nada a ver com o post. Publicar "o acervo registra 200 matérias"
ali seria dar como achado do acervo um número que a busca fabricou. A
`hipotese` é o campo que o separador já preenche em `nao_verificavel`
dizendo de que o texto fala; a do C19 é literalmente a linha do exemplo. E o
C3, cuja hipótese nomeia uma PESSOA, devolve zero — como tem de devolver,
pelo parágrafo final desta seção.

**Não conta entidades.** "7 empresas" não tem de onde sair: nada no código
extrai nome de empresa de título, e das 32 matérias de recuperação judicial
só 2 estavam extraídas. A saída conta matéria, veículo e período, que são
medidos, e mostra os títulos com o veículo. Número de empresa seria
inventado — o erro que esta saída existe para não cometer.

Os limiares foram medidos com controle negativo, não escolhidos: a 0,70 uma
consulta sobre trigo no Cazaquistão trazia 34 matérias; a **0,75** a frase
vaga "todos os rumos mudam imediatamente" traz zero. O mínimo de **3
matérias em 2 veículos** existe porque a segunda hipótese do C3 trazia uma
matéria só, sobre um empresário preso por homicídio: um veículo não é acervo
cobrindo assunto, é coincidência de vocabulário.

O rótulo é `[ACERVO]`, não `[CONTEXTO]` — esse já significa o post anterior
do próprio autor (a thread dele) no Telegram.

Isso é CONTEXTO, não veredito, e a distinção é a mesma que separa o digest
do check: aponta o que o acervo tem sobre um assunto, sem afirmar que
sustenta a insinuação de ninguém. O motor já existe — o índice acha por
assunto, o digest já sabe agrupar por história e contar veículos.

Três exigências, para não virar a porta dos fundos do que as regras 8 e 9
fecharam:

1. **Contexto nunca é veredito.** Nada de `confirmado`, nada de contagem de
   veículos apresentada como corroboração de uma premissa que não existe. A
   saída nomeia o ASSUNTO e mostra o que há, com fontes.
2. **Só para `nao_verificavel`.** Premissa bem-formada segue para o check
   como hoje; contexto é o que se oferece quando não há o que conferir.
3. **Teto próprio.** É uma busca no índice por rodada, não uma chamada de
   modelo por premissa — se precisar de LLM para resumir, entra com teto e
   com o custo no rodapé, como todo o resto.

O que isso NÃO resolve, e vale dizer: o mesmo post do "empresário" (C3)
continua sem sujeito. Contexto por assunto não nomeia pessoa, e não deve —
a tentação de usar a mesma máquina para responder "de quem ele está
falando" é exatamente o princípio 1 pela porta dos fundos.

**O gabarito** (`src/gabarito.py`, `gabaritos/*.json`) é o que impede a
próxima regra de reabrir a anterior: casos fixos com resposta esperada
escrita à mão (revisão assinada pelo conteúdo — editar o esperado invalida a
revisão), posts reais com o registro do post — autor, data, texto e o
referenciado —, de que o texto do separador é derivado pelo mesmo caminho
da produção, `fronteira` para lacuna
conhecida, `[repr]` para caso que é exemplo literal do prompt (passar prova
reprodução, não regra), `--vezes N` porque `temperature` não existe no Opus 5
e a variância se mede repetindo — e, desde 03/09/2026, a distinção entre
**regressão** (falhou em TODAS as vezes) e **instável** (falhou em algumas):
C13 e C25 apareceram como regressão numa bateria de uma passada e deram 2/3
e 3/3 quando repetidos, sem o prompt ter mudado. Chamar variância de
regressão é falso positivo dentro da ferramenta que existe para evitar falso
positivo, e é caro nas duas pontas — bloqueia mudança boa e ensina a ignorar
a bateria. Instável não é aprovação: entra no relatório com a contagem, e `--historico` que aplica o esperado de hoje
às separações de produção gravadas, de graça — foi assim que o comparador foi
validado antes de custar um centavo. A bateria não toca `separacoes`: grava
em `gabarito_rodadas`. Regra de processo, decidida em 03/09: **prompt do
separador ou do check não muda sem a bateria, e toda barreira nova entra com
caso positivo pareado no mesmo commit.**

Riscos aceitos e registrados: a heurística do roteador tem residual
conhecido ("o cara tem Banco dele" passa pela maiúscula se o modelo o chamar
de fato — o prompt é a primeira barreira e o caso C3 vigia); completar nome
não some, muda de módulo — o schema do estruturador pede "nome completo e
oficial", embora medido ele tenha mantido "André" e só expandido "Trump";
com nome incompleto a rota por chave exata não casa e só a semântica
recupera; o índice guarda triplas de todas as versões de prompt, e a
duplicata de re-extração compete no ranking (a dedup por veículo mitiga, não
resolve).

WhatsApp foi descartado. Ele havia sido pensado como *saída* — o sistema
empurrando vereditos sobre o que já estava confirmado, que é justamente o que o
digest faz sem exigir integração nova. A entrega do boletim já sai pelo Telegram.

O RSS das agências não é fonte de produto — elas já publicaram a resposta.
É **gabarito**: roda-se o sistema sobre afirmações que Lupa, Aos Fatos ou
Comprova já julgaram, sem mostrar o veredito delas, e compara-se. Ver
"Como medir se funciona".

## Duas saídas, dois gatilhos

O sistema entrega por dois caminhos, e eles resolvem problemas diferentes.

| | Gatilho | Entrada | O que produz |
|-|-|-|-|
| **Digest diário** (`digest.py`) | relógio | o acervo do dia | o que se sustenta e onde divergem |
| **Boletim** (`boletim.py`) | post novo no radar | afirmação do autor do post | veredito sobre aquela afirmação |

`check.py` é o motor da segunda linha: recebe a premissa que o separador
tirou do post e devolve o veredito com as fontes. Não tem entrada própria.

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

Recebe do radar uma afirmação que não veio do acervo e a julga contra ele. É o
caso não-redundante, descrito em "De onde vem a afirmação".

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

**RSS de veículos de notícia é a fonte única de evidência.** O X entra como
radar — é de onde vem a afirmação a conferir, nunca a evidência (ver "Rede
social é radar, nunca evidência"). Bluesky exige autenticação para busca e
Reddit exige OAuth — ambos fora do escopo, e não como etapa futura.

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

O post de um handle acompanhado é a AFIRMAÇÃO a conferir, nunca a
evidência:

```
post do handle  →  separador tira as premissas factuais
                          ↓
              cada premissa é buscada no acervo
                          ↓
   acervo cobre      →  confirmado ou contradito, citando os veículos
   acervo não cobre  →  "sem evidência" — não "o autor errou"
```

O post nunca entra no acervo nem conta como veículo. Ele indica **onde
olhar**; a evidência vem sempre da imprensa ou da instituição. Isso preserva
o princípio de que todo veredito carrega fonte rastreável.

Implementado em `radar.py` sobre a API oficial do X — ver "Rede social pela
API oficial do X", em "De onde vem a afirmação".

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
  O alvo original de convergência (10–15) valia para um domínio; a v2 —
  derivada das 121 triplas em `outro` quando a extração alcançou economia
  corporativa e cripto/regulação, em 29/08/2026 — levou a lista a 22, e o
  alvo revisto é ~20–25 com três domínios cobertos. Nem tudo que é frequente
  promove: rótulo que funde desfechos opostos ("decidiu sobre") confirmaria
  decisões contrárias entre si e ficou de fora — o porquê está registrado no
  docstring de `vocabulario`.
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

Três problemas distintos, com dificuldades distintas:

| Caso | Exemplo | Situação |
|-|-|-|
| **Apelido** | `Lula` = `Luiz Inácio Lula da Silva` = `o presidente` | Resolvido na extração |
| **Variação de grafia** | `Braskem` = `Braskem S.A.` · `Petrobras` = `Petrobrás` | Resolvido na **leitura** — ver abaixo |
| **Hierarquia** | `Ministério da Saúde` ⊂ `governo federal` | **Em aberto** — não é normalização, é inferência |

O prompt pede canônico "idêntico caractere por caractere entre matérias" e não
tem como cumprir: cada chamada é isolada. Medido no acervo em 29/08/2026,
`Braskem` e `Braskem S.A.` somavam 100 triplas como duas entidades — o caso
mais denso do grafo, invisível para a corroboração.

A correção é `chave_canonica` (`src/canonico.py`), aplicada na COMPARAÇÃO —
chave do grafo e rota por chave exata do check — nunca no registro. Mesmo
precedente da normalização de relação na leitura. Duas camadas:

1. **Determinística** (caixa, acento, sufixo societário) — medida antes de
   entrar: sobre 269 formas do acervo, funde só o caso Braskem, zero fusões
   indevidas.
2. **Apelidos curados** (`APELIDOS`, versionada) — só entra par cujas duas
   formas existem no acervo e nomeiam o mesmo referente. Embedding apenas
   PROPÕE candidatos; fusão automática por similaridade fundiria `Braskem`
   com `Braskem Idesa` (subsidiária) e fabricaria corroboração — o falso
   positivo do princípio 5.

O caso da hierarquia fica assumido como limitação. Uma afirmação atribuída ao
"governo" pode não encontrar a matéria que atribui o ato a um ministério
específico.

### Evento e estado são relações diferentes

`comprou` é evento datado. `possui` é estado atual. Fundir os dois produz falso
positivo: "comprou em 2019" e "não possui mais em 2026" são ambas verdadeiras.

| Tipo | Semântica | Permanece verdadeiro? |
|-|-|-|
| **Evento** | Afirma algo sobre um instante | Sim, para sempre |
| **Estado** | Afirma algo sobre um intervalo | Não, pode deixar de valer |

### Extração por história: o experimento que redesenha a v3

Medido em 01/09/2026 (US$ 0,18, três pares): a fragmentação de evento
sintético — "incêndio na *residência*" vs "incêndio na *casa*", direções
invertidas, zero corroboração quando cada matéria é extraída numa chamada
isolada — **desaparece quando as duas matérias vão no MESMO prompt**,
etiquetadas [A]/[B], com um campo `fonte ∈ {A, B, AB}` por tripla. Com os
dois textos diante de si, o modelo nomeia o evento uma vez só — a
convergência que a regra 2 pede e que chamadas isoladas não têm como
entregar, sai por construção. Nos três pares que haviam fracassado
(incêndio Nunes Marques, SEC→Casa Branca, Core Lightning), o fato principal
saiu `AB` — corroborado dentro da própria chamada — com atribuição limpa do
que era de uma fonte só, ~40% mais barato por história (um prefixo em vez
de dois), e com a matéria de 1 sentença do Cointelegraph contribuindo
(em chamada isolada ela rendia zero).

Condições registradas antes de virar o desenho padrão:

* **`AB` é corroboração afirmada pelo modelo** — generosidade aqui fabrica
  confirmação, o pior erro. Produção exige `sentenca_a`/`sentenca_b` por
  tripla AB e validação local de que cada fonte sustenta o afirmado.
* A ideia veio de revisão externa (outra instância, 01/09/2026); a
  validação contra os fracassos medidos é deste acervo.

**A segunda trava caiu no mesmo dia**: a história do incêndio com os SETE
veículos do acervo num prompt só ([A]…[G], campo `fontes` com as letras)
produziu 14 triplas com atribuição graduada — o evento e o "preside o TSE"
saíram `ABCDEFG` com um nome único; bombeiros `ABDEF`; "não houve feridos"
`DEG`; os pronunciamentos exclusivos da CNN, só `A`. US$ 0,12 pela história
inteira — **US$ 0,017 por matéria, ~65% mais barato** que sete chamadas — e
as matérias de 1 sentença (Folha, Exame) CONTRIBUÍRAM: no modo história, o
piso de sentenças deixa de existir como problema, porque a matéria curta é
lida no contexto das longas.

### Medições do funil (01/09/2026, custo zero, propostas em revisão externa)

Três medições sobre o funil de seleção, e a terceira muda a fila:

1. **Sindicação**: 8 de 539 pares cross-veículo com Jaccard de texto > 50%
   (InfoMoney×Estadão via Estadão Conteúdo, G1×Valor). ~1,5% — corroboração
   falsa existe, é pequena, e o modo história a neutraliza barato (texto
   quase idêntico vira uma leitura com as duas fontes anotadas).
2. **Vazamento do léxico**: das matérias que NÃO formaram par, 189 de 400
   têm vizinho semântico ≥ 0,70 em OUTRO veículo (título+lead) — pares que
   o agrupamento por termos de título nunca viu (Quaest-SE na CNN e no G1;
   PLOA na CNN e no Poder360). Parte é mesmo-assunto e não mesma-história,
   mas os espécimes inequívocos abundam.
3. **Calibração com pares-ouro** (117 pares de matérias cujas triplas v2
   compartilham chave): pela similaridade de TÍTULO — o input real da
   peneira — a mediana do ouro é **0,37**, e **74 de 117 ficam abaixo do
   limiar 0,70**. Com título+lead, mediana 0,55 contra 0,13 do aleatório.

Leitura conjunta da época: título é sinal fraco, migrar o funil para
embedding puro. **CORRIGIDA NO MESMO DIA — e a correção é a lição.** A
migração foi construída, e duas defesas baratas a derrubaram antes de
gastar um centavo: o dry-run mostrou uma "história" de 900 matérias
(Ibovespa + Argentina + sabatina do Lula no mesmo grupo), e a recalibração
achou o defeito do gabarito: **os pares-ouro estavam contaminados por
tripla BIOGRÁFICA** — (X, preside, Y) aparece em histórias diferentes e
ligava pares que nunca foram a mesma história. Com o ouro refinado (43
pares de tripla específica): mediana de similaridade **0,84**, e o léxico
co-agrupa **70%** — enquanto o embedding puro, em qualquer limiar testado,
fazia blobs e co-agrupava MENOS.

Decisão final, medida: **cada sinal no que provou fazer bem.** O léxico
agrupa; a semântica vira GUARDA DE COESÃO dentro do grupo
(`agrupa.LIMIAR_COESAO` = 0,55, com p10 do ouro em 0,62 — expulsa o carona
léxico sem tocar par verdadeiro, generalizando a antiga peneira de par); a
janela de dias limita o passado; a regra 13 do modo história é a rede
final. Verificado em operação: ouro preservado (30/43, idêntico ao léxico
puro), maior grupo 58, e o dry-run com histórias limpas de 7 veículos.

Duas morais registradas para as próximas medições: gabarito derivado de
triplas EXCLUI as biográficas/recorrentes, senão liga histórias distintas;
e conclusão de medição só vira decisão depois do dry-run — as duas juntas
custaram zero e salvaram o funil de uma troca para pior.

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

A regra original previa janela temporal em dias para estado. **Revisada em
30/08/2026, por medição**: a única divergência entre veículos que a Medição 1
encontrou era a cotação do Bitcoin em `data_fato` distintas (25 vs 27/08) — o
preço subiu 20% na semana e os dois veículos estavam certos, cada um no seu
dia. A janela em dias rotularia exatamente isso como contradição.

A regra implementada (`Corroboracao.divergencias`): **número só disputa com
número quando afirma o mesmo instante.** Dentro de (chave, unidade), as
afirmações se separam por `data_fato` antes da comparação; ausente forma o
grupo "sem data" (estado presente, contemporâneo por construção — o acervo
cobre dias). Granularidades diferentes ("2026-08" vs "2026-08-25") não se
comparam: pode perder divergência real, nunca inventa uma — a direção do
princípio 5. Vale para evento e estado; disputa de DATA entre veículos
("foi no dia 19" vs "foi no dia 20") fica registrada como caso não
detectado, pela mesma assimetria.

**Contradição não-numérica continua não implementada, e o bloqueio é de
vocabulário**: "aprovado" vs "rejeitado" exige relações com polaridade
(aprovou/rejeitou como pares declarados incompatíveis), e a v2 deliberadamente
recusou rótulo de desfecho neutro (caso `decidiu_sobre`). É a pergunta da v3:
promover pares de desfecho com a incompatibilidade declarada no vocabulário,
ou esperar a reificação da atribuição.

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

**ADIADO em 05/09/2026, por decisão do dono.** Dois motivos. Para o post do
radar, o classificador que importa já existe e não é clássico: o separador
(`premissas.py`, Opus com saída estruturada) mais o roteador em código — e o
que separa fato de opinião ali é referente, âncora e reescrita, que rótulo
de sentença não faz. Para a matéria de imprensa, com o corte no lide e a
seleção aos pares já cortando a maior parte do custo, o que sobra para um
filtro de sentença economizar não paga as horas de rotulagem. Fica a porta:
a tabela `separacoes` acumula, a cada boletim, o par post → separação, e em
alguns meses é um dataset rotulado sem trabalho extra — se o volume crescer
a ponto de o separador pesar, um modelo pequeno destilado dali é o caminho.

## LLM não é agente

Distinção que governa a decisão sobre orquestração — e que, em 05/09/2026,
tirou o LangGraph do projeto: o único ciclo que existe é regra fixa em
código (ver "O ciclo"), e um framework de agentes embrulhando uma cascata
fixa seria dependência sem função.

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
| Orquestração | **Código**, sem framework | O único ciclo (extração sob demanda) tem regra fixa, uma volta e teto; LangGraph descartado em 05/09/2026 |
| Vector DB | **ChromaDB** | Local, sem servidor, persiste em disco |
| Embeddings | **sentence-transformers**, multilíngue | Notícia em português; local, custo zero |
| Grafo | **NetworkX** | Em processo, sem infraestrutura |
| Classificador | **adiado** (05/09/2026) | O separador (Opus + roteador) já classifica o post; filtro de sentença para imprensa não paga a rotulagem hoje |

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
NetworkX e GitHub custam zero.

Três reduções, todas previstas na arquitetura: processamento em lote (metade do
preço, e extração não tem pressa), cache do prefixo do prompt (o trecho de
instruções e few-shot é idêntico em toda chamada), e os dois filtros acima.

A saída é a parte cara e **só encolhe sendo projetada**: relação vinda do enum,
sem devolver o trecho original da matéria — que já está no banco —, teto de
triplas por matéria.

### O que é observável não se verifica, se consulta

Preço de bitcoin, cotação do dólar, valor da Selic hoje: são **observáveis
diretamente**, em tempo real, na fonte autoritativa. Checar isso contra um
acervo de notícia é pior que inútil — o acervo tem a versão de ontem, e a
resposta certa está a um clique no gráfico ou no site do Banco Central.

O que precisa de verificação é o que **não dá para olhar**: decisão, evento,
atribuição, ato oficial.

```
✗ "o bitcoin caiu 40%"                    abre o gráfico
✓ "a SEC aprovou novas regras de custódia" precisa de fonte
✗ "a Selic está em 15%"                    site do BC
✓ "o Copom decidiu por unanimidade"        precisa de fonte
```

Confirmado pelos dados: das histórias de cripto que reuniram dois veículos no
acervo, nenhuma é preço. São regra da SEC, lançamento de stablecoin,
recomendação de atualização de software, transação resistente a ataque
quântico. Preço não vira notícia corroborada porque não é notícia — é leitura
de instrumento.

**Consequência para a checagem de premissa de análise:** premissa que cita
estatística oficial (IPCA, Selic, PIB) deveria ser conferida contra a
INSTITUIÇÃO, não contra a cobertura de imprensa. O jornal é intermediário, e
intermediário arredonda. Isso ainda não existe e é o argumento mais forte a
favor dos feeds de fonte primária.

### O que o acervo NÃO prova sozinho

Três correções de 03/09/2026, todas achadas conferindo a saída, não o código:

**Liveblog.** 153 dos 12.282 artigos são páginas rolantes — cobertura ao vivo
de mercado, minuto a minuto. O problema é triplo: o link não mostra o fato (o
leitor tem de rolar até a entrada); a mesma URL é recoletada sem parar e vira
N "matérias" (o Ibovespa ao vivo do InfoMoney está 31 vezes no acervo); e a
`data_publicacao` é a de ABERTURA da cobertura, não a do fato.

`normalize.e_live` detecta pelo caminho da URL. A fonte sai marcada
**liveblog** no boletim. A CONTAGEM de veículos não muda — decisão explícita: o
fato é real e o veículo realmente o publicou. Mas quando são EXATAMENTE dois
veículos e um é liveblog, o critério do AC1 passa a se apoiar num link que
não sustenta o fato sozinho, e aí `check.apoio_fragil` avisa. Com três ou
mais sobra corroboração; com um, o aviso antigo já cobria. Medido: 7
confirmações têm dois veículos e nenhuma tem liveblog hoje — mas nove
veículos publicam liveblog no acervo.

**Veredito sem fonte.** `consultas` guardava `citadas` como CONTAGEM, e o
boletim raspava as evidências do STDOUT do check. No reuso o check não
imprime nada disso, então um CONFIRMADO de 24h atrás voltava com "4 veículos"
e nenhum link. O princípio 2 valia na hora de imprimir, não no arquivo.
Coluna `evidencias` com (veículo, título, url, data) do que o JUIZ citou,
deduplicada por URL — uma matéria rende várias triplas e o juiz cita mais de
uma, e sem isso o boletim mostrava "2 veículos" com quatro linhas.

**Resposta a terceiro.** O tipo do post vem do metadado do servidor
(`x_api.classifica`, que falha FECHADO: sem metadado que prove raiz, é
`resposta`); `radar.separa_por_tipo` descarta `resposta` e `retweet` antes
de custar separação, check e demanda, e `radar.cadeia` derruba junto a
thread própria pendurada num post descartado, pelo id do pai, até
estabilizar. Cada descarte sai com o motivo no arquivo e contado nas notas
da rodada. O que fica: post próprio, quote, e continuação de thread própria
(o C25 depende dela).

### A medida como chave, não como prosa

Patch de 03/09/2026, e a medição que o justifica é o tipo de coisa que só
aparece quando se olha o acervo em vez do código.

`valor_contexto` sempre foi prosa livre, e o grafo comparava dois contextos
por EMBEDDING a 0,95. Medido sobre as 2.984 triplas: a mesma medida da Caixa
saiu em SEIS redações — "alta do lucro recorrente do 2º trimestre de 2026
ante o 2º trimestre de 2025", "alta do lucro recorrente sobre o mesmo
período", "alta do lucro líquido recorrente na base anual" — e o embedding
**separou 9 dos 15 pares**, com proximidades de 0,79 a 0,90.

O efeito é falso negativo de CORROBORAÇÃO, e é silencioso: dois veículos
publicam o mesmo número e deixam de se confirmar. É o avesso do princípio 5 e
custa igual, porque o produto do sistema é justamente dizer que duas fontes
independentes batem.

A medida passa a ter dois campos que são CHAVE, não descrição:
`valor_propriedade` (o que o número é: `lucro_recorrente`, `margem_de_erro`)
e `valor_recorte` (a fatia: `2t2026_vs_2t2025`, `1o_turno`). Ambos em
snake_case, e `canonico.chave_medida` normaliza na LEITURA — mesmo padrão de
`chave_canonica`: o modelo propõe a forma, o código impõe a chave.

`valor_contexto` fica, para a tela. E o embedding fica como reserva: as 2.984
triplas anteriores não têm os campos novos e continuam comparadas como antes,
sem migração e sem reextração.

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

9. **Barreira nova entra com caso positivo pareado.** Regra de prompt,
   roteador ou filtro que só tem caso negativo na bateria só pode "melhorar"
   apertando, e aperto engole o caso vizinho — foi assim que a regra escrita
   para "o empresário" engoliu "André se reune com Trump" (02/09/2026). Prompt
   do separador ou do check não muda sem rodar o gabarito.

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

**RODADA EM 30/08/2026**, sobre 880 afirmações de 99 matérias (vocab v2,
acervo de ~5 dias, US$ 3,12 de extração):

* **Corroboração validada**: 726 fatos distintos, **71 confirmados por 2+
  veículos** (~10%). A metade autônoma tem conteúdo real para o digest.
* **Entidades unificadas**: 598 formas → 593 chaves; os 5 grupos fundidos
  pela `chave_canonica` são todos legítimos (EUA, variação de caixa). A
  explicação "fragmentou" está afastada — o número de divergência abaixo
  é real, não artefato.
* **Divergência entre veículos: 1 — e é falsa.** Cotação do Bitcoin em
  `data_fato` distintas (25 vs 27/08): preço em dias diferentes, não
  contradição, e preço é observável, fora do escopo de verificação.

A leitura honesta: isto é um PISO, não o rendimento. O detector atual só
enxerga divergência NUMÉRICA na mesma chave — as duas regras já escritas
neste documento e ainda não implementadas (janela temporal por `data_fato`
e contradição evento/estado não-numérica, "aprovado" vs "rejeitado") são
exatamente as que pegariam os casos do AC1. Decisão registrada: o grafo NÃO
sai ainda; a régua é implementar as duas regras (custo zero — o dado está
pago e gravado) e repetir esta medição com acervo maior. Se continuar perto
de zero com o detector completo, aí vale a sentença acima e o digest assume
o papel de corroboração, não de divergência.

**REPETIÇÃO EM 01/09/2026**, sob o v3 (extração por história), sobre 2.101
afirmações de acervo com 6.317 matérias:

* **Corroboração**: 1.258 fatos distintos, **291 confirmados por 2+
  veículos (23%)** — mais que o dobro da taxa de 30/08 (10%). A extração
  por história cumpre a promessa de corroboração que motivou o v3.
* **Entidades**: 1.013 formas → 1.006 chaves; 7 fusões, todas variação de
  caixa, zero fusão indevida. A leitura "divergência baixa = fragmentação"
  segue afastada.
* **Divergências ENTRE VEÍCULOS: 2 — uma real, uma falsa.**
  * FALSA, e conhecida: Bitcoin a 65.500 (CriptoFácil) vs 80.000 (Folha)
    em 25/08 — é o próprio caso que gerou a regra 4, extraído em 30/08
    **antes** de a regra existir (prompt `9d4ed6ef`). A cura é re-extrair
    a matéria sob o prompt atual; enquanto isso, ela serve de teste vivo:
    se a regra 4 funciona, a divergência some.
  * **REAL, a primeira do projeto**: desaparecidos nas enchentes do
    Himalaia — G1 diz 3.044, Carta Capital diz 2.502, mesmo `data_fato`
    (30/08), 22% de diferença. Dois veículos publicando números
    materialmente distintos para o mesmo fato no mesmo dia. Ressalva de
    método: número de desastre evolui hora a hora, e a granularidade
    diária do carimbo pode estar fundindo momentos diferentes do mesmo
    dia — mas como publicado, é exatamente o que o radar de contradição
    existe para apontar.
* O detector também acusou 2 conflitos MONO-veículo (não contam como
  divergência entre veículos): série de captações diárias de ETF sem
  `data_fato` colapsada no grupo "sem data", e dois níveis de prêmio da
  Mega-Sena (quina/quadra) fundidos no mesmo contexto. Dois consertos
  candidatos que eles pedem: exigir 2+ veículos no conflito antes de
  chamar de divergência no digest (alinha com "falso positivo é o pior
  erro"), e tratar série temporal sem data como incomparável.

Veredito da régua de 30/08: apareceu divergência real com acervo maior —
**o grafo fica.**

**FECHAMENTO NO MESMO DIA (01/09/2026, após o pacote v3+schema magro)**:
os três artefatos morreram pela causa, não pelo sintoma — o Bitcoin pela
regra 4 com a matéria inteira (65.500 re-extraído saiu `2026-07`,
"preço negociado ao fim da sequência"; a lição operacional: a âncora
temporal estava DEPOIS do corte de 5 sentenças, e regra de prompt não
data o que o modelo não vê), e os dois mono-veículo pelo detector que
passou a exigir 2+ veículos no conflito. Estado final do acervo: 1.282
fatos, 295 confirmados por 2+ veículos, **1 divergência — a real**
(Himalaia). Validação do fio magro na mesma leva (3 chamadas, US$ 0,27):
131 tokens de saída por tripla (era ~178, −26%), cache lido em 9.592
tokens na 2ª chamada do lote, modo história explodindo códigos "fs" por
fonte com nomes convergentes, e o v3 em campo (`ocorreu_em` no Trump
National Golf Club, `tem_participacao_em` BlackRock→iShares).

**2. Acurácia contra checador profissional.** Rodar ~50 afirmações já julgadas
por Lupa, Aos Fatos ou Comprova, sem mostrar o veredito delas, e comparar.
Essas afirmações não são posts, e a via nunca foi construída: exigiria coletar
o RSS das checadoras e um modo de avaliação que chame `verifica` de ponta a
ponta — o gabarito não serve, porque fixa a evidência e mede o juiz, não a
recuperação.

Concordância com checador profissional é o único número que separa este projeto
de um agregador — e nenhum agregador consegue produzi-lo.

**Em aberto:** essas afirmações não são posts, e a via de execução desta
medição não está definida.

**3. Regressão de prompt.** Não mede rendimento nem acurácia: mede se o que
já funcionava continua funcionando quando o prompt muda. É o gabarito
(`python -m src.gabarito premissas|check`), e a pergunta é binária por caso.
Diferente das duas anteriores, roda a cada mudança de prompt, não a cada
acervo novo.

**PRIMEIRA RODADA EM 03/09/2026**, sobre os prompts v4 do separador e do
juiz: separador 25 casos × 2 vezes, 50/50, US$ 0,45 (8 posts reais, 17
sintéticos, 4 exemplos literais do prompt marcados como reprodução); juiz e
estruturador 23/23, US$ 0,34 — a primeira passada acusou duas regressões que
eram do freio em código (pontuação no casamento de sujeito), corrigidas antes
de qualquer prompt mudar. Aplicado retroativamente às separações de produção
gravadas (`--historico`, custo zero), o mesmo gabarito acusa todos os
incidentes de 31/08 a 02/09. Ressalva de método: nenhum dos 48 esperados foi
revisado pelo dono do projeto ainda; até lá a bateria cobra do modelo a
leitura de quem a escreveu.

**RODADA EM 05/09/2026**, depois de o texto do separador mudar de contrato
(cabeçalho sem número de rodada, contexto sem handle nem link — ver "Rede
social pela API oficial do X"): 27 casos × 2 vezes, US$ 0,53. Uma
regressão, e ela era do gabarito, não do prompt: o C13 ("André foi lá e
nada mudou") exigia `opiniao` para "nada mudou" e o modelo devolveu
`nao_verificavel` nas duas vezes — a nota do próprio caso já dizia que os
dois servem e que o que se cobra é zero fatos. O comparador passou a
aceitar lista de tipos, o caso passou a aceitar os dois, e repetido deu
2/2 (US$ 0,05). Os três casos reais que eram resposta a terceiro (C4, C5,
C15) viraram contexto de post citado — resposta a outra conta não chega
mais ao separador, e a regra 9 é a mesma para as duas formas. As
assinaturas de revisão caíram com o texto; 20 casos em que só o cabeçalho
mudou foram re-assinados na hora; C4, C5, C15, C25 e C26 (a linha de
contexto mudou) aguardam o dono, e C14 e C19 seguem em disputa. O
`--historico` deixa de casar as separações de produção gravadas antes desta
data: o hash é do texto, e o texto mudou.

## Convenções do repositório

* Nenhuma credencial no código. Tudo em `.env`, versionado apenas como
  `.env.example`
* Mensagens de commit descrevem a mudança e o motivo
* Testes ao menos na camada de verificação, que é onde erro é silencioso
