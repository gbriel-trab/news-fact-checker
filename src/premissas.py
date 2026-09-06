"""Confere as premissas de um texto que argumenta.

O `check.py` recebe UMA afirmação e responde. Este módulo recebe um ARGUMENTO
— análise, comentário, previsão — e separa o que dá para conferir do que não
dá, antes de gastar.

    "O Copom não tem escolha. Com o desemprego em 5,3% e a dívida pública
     acima de R$ 9,2 trilhões, manter a Selic é insustentável. Vai subir."

    premissa   desemprego está em 5,3%              → verificável
    premissa   dívida pública acima de R$ 9,2 tri   → verificável
    previsão   a Selic vai subir                    → não, e nem deve ser
    opinião    o Copom não tem escolha              → não

O `extract.py` descartaria a frase inteira, e com razão: a regra dele diz para
não extrair opinião nem previsão. Correto para notícia, errado para análise —
joga fora os números junto com o palpite.

O QUE ESTE MÓDULO NÃO É, e a distinção decide se ele presta:

    ✗  nota de credibilidade do autor
    ✓  conferência dos números em que o argumento se apoia

Premissa sem evidência significa que o ACERVO não cobre, nunca que o autor
errou. A saída é escrita para não permitir a outra leitura, e mudar isso
transforma a ferramenta em máquina de acusar — que é outro produto.

E previsão não é erro. Um argumento pode ter todas as premissas confirmadas e
a conclusão errada; é assim que análise funciona. O que este módulo detecta é
o contrário: raciocínio impecável partindo de um número que não bate.
"""

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from . import llm

CONTEXTO_PROPRIO = "contexto — post anterior do próprio autor"
CONTEXTO_ALHEIO = ("contexto — post citado pelo autor; as afirmações são de "
                   "quem ele cita")
"""Os dois prefixos de linha de contexto que o radar escreve no texto que
este módulo recebe (`radar.para_separacao`). Moram aqui porque são contrato
DESTE lado: a regra 9 do prompt os descreve e `texto_ancoravel` decide por
eles o que é texto do autor. O radar importa daqui — mudar um sem o outro
é o separador lendo palavra de terceiro como palavra do autor."""


class Referente(BaseModel):
    """Um pedaço da afirmação preso ao texto. O `valor` é como o texto
    escreve; o `trecho` é onde aparece, literalmente — é o que o roteador
    confere em código. Nome pela metade fica pela metade."""

    valor: str = Field(
        description="COMO O TEXTO ESCREVE — nunca completado nem corrigido."
    )
    trecho: str = Field(
        description="Pedaço LITERAL do texto onde isto aparece."
    )


class Premissa(BaseModel):
    """Uma afirmação isolada extraída de um texto argumentativo.

    Desde 01/09/2026 a reescrita (`afirmacao`) é EXCLUSIVA do tipo fato:
    é a consulta que o verificador consome, e só aí ela trabalha. Desde
    03/09/2026 o fato traz o referente como CAMPO ancorado (`quem`,
    `o_que`, `quando`), não como impressão: o roteador confere as âncoras
    contra o texto e rebaixa a `nao_verificavel` o que não ancora — a
    regra 8 v2, escrita em prosa, engoliu a charada Esteves–Trump por
    não ter saída observável."""

    tipo: Literal["fato", "previsao", "opiniao", "relato",
                  "nao_verificavel"] = Field(
        description=(
            "fato: afirma algo já ocorrido ou um estado presente NO MUNDO, "
            "com referente que o texto identifica. "
            "previsao: afirma sobre o futuro. "
            "opiniao: juízo, avaliação ou recomendação. "
            "relato: o assunto é o próprio autor do texto — o que ele diz, "
            "fez, costuma fazer ou postou; a prova é o próprio texto. "
            "nao_verificavel: afirma algo sobre o mundo, mas o texto não "
            "identifica de quem ou do que fala — não há o que conferir."
        )
    )
    afirmacao: str | None = Field(
        None,
        description=(
            "APENAS quando tipo=fato: a afirmação reescrita como frase "
            "completa e autônoma, que faça sentido sozinha — é o que será "
            "verificado. Nos demais tipos, OMITA: o trecho literal basta."
        )
    )
    trecho: str = Field(
        description="O pedaço LITERAL do texto de onde ela saiu, sem reescrever."
    )
    quem: Referente | None = Field(
        None, description="Só em fato: o sujeito da afirmação, ancorado."
    )
    o_que: Referente | None = Field(
        None,
        description=(
            "Só em fato: a outra entidade, o número ou o objeto, ancorado. "
            "Omita se o texto não dá."
        )
    )
    quando: Referente | None = Field(
        None,
        description=(
            "Só em fato: data de OCORRÊNCIA que o texto dá — valor resolvido "
            "('31/08/2026'), trecho literal ('ontem'). Omita se o texto não "
            "dá. A data do post NÃO é data de ocorrência."
        )
    )
    hipotese: str | None = Field(
        None,
        description=(
            "Só em nao_verificavel: de quem ou do que você desconfia que o "
            "texto fala, sem âncora. Nunca vai à verificação."
        )
    )
    roteado: SkipJsonSchema[str | None] = None
    """Preenchido em CÓDIGO por `roteia`, nunca pelo modelo: por que um
    fato foi rebaixado a nao_verificavel. Fora do schema enviado."""

    @property
    def texto(self) -> str:
        """O que exibir/verificar: a reescrita quando existe, senão o
        trecho literal. Fato sempre tem reescrita (o validador garante)."""
        return self.afirmacao or self.trecho

    @model_validator(mode="after")
    def _reescrita_e_do_fato(self) -> "Premissa":
        # Garantia, não pedido: se o modelo esquecer a reescrita num fato,
        # o trecho literal vira a consulta — perder a premissa paga seria
        # pior que verificar a frase crua. E se ele reescrever um NÃO-fato,
        # a paráfrase cai: era o que fazia "[nao_verificavel] André Esteves
        # tem um banco" aparecer no boletim como se fosse o post, com o
        # palpite que a regra 8 manda pôr em `hipotese`.
        if self.tipo == "fato" and not self.afirmacao:
            self.afirmacao = self.trecho
        if self.tipo != "fato" and self.afirmacao:
            self.hipotese = self.hipotese or self.afirmacao
            self.afirmacao = None
        return self


class Analise(BaseModel):
    premissas: list[Premissa]


# --------------------------------------------------------------- roteador

_EQUIVALENTES = str.maketrans({
    "“": '"', "”": '"', "„": '"', "‟": '"', "‘": "'", "’": "'", "‛": "'",
    "—": "-", "–": "-", "‒": "-", "°": "o",
})
"""Aspa curva, travessão e sinal de grau viram a forma reta antes de
comparar: o modelo transcreve o trecho normalizando a tipografia, e sem
isto a âncora falhava por causa de um caractere — falso negativo
silencioso, com motivo apontando para a lacuna errada."""


def _normaliza(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto.translate(_EQUIVALENTES))
    limpo = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return " ".join(limpo.casefold().split())


_ARTIGOS = frozenset(
    "o a os as um uma uns umas e com de do da dos das em no na nos nas que "
    "mas se por para ao aos pelo pela sobre ate até".split())
_FECHADAS = frozenset(
    "la ali aqui isso isto aquilo nisso disso ele ela eles elas o a "
    "alguem algo tudo nada assim".split())
_INDEFINIDOS = frozenset(
    "um uma uns umas algum alguma alguns algumas certo certa varios varias "
    "outro outra outros outras qualquer".split())
"""Determinante indefinido no início do valor: "um empresário", "uma
reunião", "algum lugar". Não identifica nada, e a barreira de pronome
sozinha não os pegava — passavam com sujeito nomeado."""

_RE_CABECALHO = re.compile(r"\APOST\b[^\n]*\n?")
_RE_CONTEXTO_ALHEIO = re.compile(
    r"^\(" + re.escape(CONTEXTO_ALHEIO) + r"[^\n]*\n?", re.MULTILINE)


def texto_ancoravel(texto: str) -> str:
    """O que conta como TEXTO DO AUTOR para ancorar um referente.

    Fora: a linha de cabeçalho "POST (@handle, data):" — só a PRIMEIRA
    linha, ancorada no início do texto; senão a data do post ancora como
    data de ocorrência, e a proibição fica só na prosa da regra 8 — e a
    linha de contexto que o `radar` rotula como palavra de OUTRA pessoa
    (`CONTEXTO_ALHEIO`, o post citado): trecho copiado da fala do terceiro
    ancorava perfeitamente, e a âncora provava que o pedaço está no texto,
    não que o autor o afirmou. Dentro: a linha de contexto do próprio
    autor (`CONTEXTO_PROPRIO`), que é texto dele (regra 9)."""
    return _RE_CONTEXTO_ALHEIO.sub("", _RE_CABECALHO.sub("", texto))


def _ancorado(ref: Referente | None, texto_norm: str) -> bool:
    """O trecho aparece literalmente no texto do autor, em fronteira de
    palavra? Substring crua fazia "ele" ancorar dentro de "eleição", e
    trecho de uma letra ancorar em qualquer texto."""
    if ref is None or not ref.trecho.strip():
        return False
    alvo = _normaliza(ref.trecho).strip(" .,;:!?\"'()")
    if len(alvo) < 3:
        return False
    return re.search(rf"(?<!\w){re.escape(alvo)}(?!\w)", texto_norm) is not None


def _tem_entidade_ou_numero(ref: Referente) -> bool:
    """Nome próprio, sigla ou número no valor do referente.

    Duas exclusões medidas na revisão de 03/09/2026: token que ABRE o
    trecho não conta como nome próprio (maiúscula de início de linha —
    "Banco dele", "Participação societária" vinham do post do
    "empresário"), e caixa alta com mais de duas letras é ênfase, não
    sigla ("TODOS os outros empresários"); sigla curta (BC, IPCA, RIOT)
    continua valendo."""
    tokens = ref.valor.split()
    # A posição só é evidência quando há mais de um token: um referente de
    # uma palavra ("André", "Esteves") não oferece contraste nenhum, e
    # descartá-lo por abrir o trecho matava justamente a charada.
    abre_o_trecho = (len(tokens) > 1
                     and ref.trecho.strip().startswith(tokens[0]))
    for i, token in enumerate(tokens):
        limpo = token.strip("\"'(),.;:!?«»")
        if not limpo:
            continue
        if any(c.isdigit() for c in limpo):
            return True
        if i == 0 and abre_o_trecho:
            continue
        if limpo.isupper() and len(limpo) > 2:
            continue
        if limpo[0].isupper() and limpo.casefold() not in _ARTIGOS:
            return True
    return False


def _tem_numero(ref: Referente) -> bool:
    return any(c.isdigit() for c in ref.valor)


_QUANTIDADE = frozenset(
    "quantas quantos muitas muitos varias varios tantas tantos diversas "
    "diversos inumeras inumeros recorde onda alta baixa aumento aumentou "
    "cresceu subiu caiu despencou disparou dobrou triplicou explodiu "
    "maioria minoria".split())
"""Palavra que torna o predicado MENSURÁVEL: o acervo consegue medir o
fenômeno sem nomear nenhum membro da classe.

Fora daqui de propósito: "todos", "todo", "infindáveis", "praticamente".
Elas quantificam mas não são mensuráveis pelo acervo, e todas as três
aparecem no post do "empresário" (C3) — "TODOS os outros empresários no
bolso", "Participação societária em infindáveis empresas". Incluí-las
abriria exatamente o caso que a regra 8 existe para fechar."""


def _e_classe(ref: Referente) -> bool:
    """O sujeito é uma CLASSE, não um indivíduo não identificado.

    Classe é substantivo comum no PLURAL — "marcas icônicas", "empresas",
    "bancos". Indivíduo é singular — "o cara", "o empresário", "o
    encontro". A diferença importa porque decide se falta informação para
    conferir: para "há muitas recuperações judiciais em marcas conhecidas"
    NÃO preciso saber quais marcas; para "o cara tem banco dele" preciso
    saber quem é o cara."""
    uteis = [t for t in _normaliza(ref.valor).split() if t not in _ARTIGOS]
    return bool(uteis) and all(t.endswith("s") for t in uteis[:1])


def _classe_mensuravel(p: "Premissa") -> bool:
    """A terceira porta do roteador: classe no sujeito E quantidade no
    que o texto escreveu.

    Exige as DUAS, e a segunda olha o TRECHO — o texto do autor —, não o
    valor resolvido: é lá que "quantas" aparece. Uma só não basta:
    "marcas icônicas estão em crise" tem classe e não tem medida;
    "TODOS os outros empresários" tem quantificador e não tem classe
    mensurável.

    Fundada em 03/09/2026 pelo C19, com o veredito na mão: a afirmação
    que o roteador rebaixava voltou CONFIRMADO por 4 veículos. Regra que
    manda descartar o que o acervo sustenta está errada."""
    if not (p.quem and _e_classe(p.quem)):
        return False
    texto = _normaliza(" ".join(
        filter(None, [p.trecho, p.quem.trecho if p.quem else "",
                      p.o_que.trecho if p.o_que else "",
                      p.quem.valor if p.quem else "",
                      p.o_que.valor if p.o_que else ""])))
    return any(t in _QUANTIDADE for t in texto.split())


_POSSESSIVO_PROPRIO = re.compile(
    r"(?<!\w)(minha|minhas|meu|meus|nossa|nossas|nosso|nossos)\s+"
    r"(?=[a-z])")
"""Possessivo de primeira pessoa seguido de palavra MINÚSCULA, conferido
sobre o texto sem casefold: "minha enquete" é do autor; "Minha Casa Minha
Vida" e "Meu INSS" são nomes próprios (a revisão de 06/09/2026 pegou os
dois virando relato). Sem o casefold, o próprio texto do post decide."""
_VERBO_PROPRIO = re.compile(
    r"(?<!\w)(que )?(postei|publiquei|lancei|rodei|fiz|escrevi|criei|"
    r"divulguei)(?!\w)")
"""Verbo em primeira pessoa no referente: "as alts que postei", "a enquete
que rodei". "eu" solto saiu da lista: "acordo EU-Mercosul" casava."""
_AUTOR_NA_REESCRITA = re.compile(
    r"(?<!\w)d[oa] autora?(?!\w)"
    r"(?!\s+(d[eoa]s?|intelectual|material)(?!\w))"
    r"|(?<!\w)que [oa] autora? "
    r"(postou|fez|publicou|rodou|lancou|escreveu|criou|divulgou)(?!\w)")
"""Só as formas em que a reescrita atribui a coisa ao DONO do post: "a
enquete do autor", "que o autor postou". Não pega "o autor de Torto
Arado", "a autora de Harry Potter", "autor intelectual", "pelo autor",
"o autor dos ataques" — gente do mundo, achados da revisão de
06/09/2026. Residual conhecido: "a obra do autor vendeu 1 milhão" ainda
casa; é post sobre livro, raro neste radar, e registrado."""
_RE_HANDLE = re.compile(r"\APOST \(@(\w+)")


def _do_autor(p: "Premissa", handle: str = "") -> bool:
    """O referente é coisa do próprio autor — regra 7 em código
    (06/09/2026). Três sinais, qualquer um basta: possessivo ou verbo de
    primeira pessoa no sujeito ancorado ("as alts que postei", "minha
    enquete", "nosso post"); a reescrita atribuindo a coisa ao autor ("Na
    enquete do autor…", "que o autor postou"); ou a reescrita resolvendo o
    autor para o handle do cabeçalho ("na enquete de @handle") — a regra 3
    manda resolver referência, e o handle é a resolução mais provável.

    Caso que motivou: boletim de 25/08/2026 (refeito em 06/09) emitiu dois
    fatos assim, os dois ancorados e com número, e pagou US$ 0,18 em check
    e demanda sobre pesquisa eleitoral. Os limites das regexes vêm da
    revisão adversária do mesmo dia — ver as docstrings de cada uma."""
    pedacos = [p.quem.valor, p.quem.trecho] if p.quem else []
    for x in pedacos:
        sem_acento = "".join(
            ch for ch in unicodedata.normalize("NFKD", x)
            if not unicodedata.combining(ch))
        if _POSSESSIVO_PROPRIO.search(sem_acento):
            return True
        if _VERBO_PROPRIO.search(_normaliza(x)):
            return True
    if not p.afirmacao:
        return False
    reescrita = _normaliza(p.afirmacao)
    if _AUTOR_NA_REESCRITA.search(reescrita):
        return True
    if handle and re.search(
            rf"(?<!\w)d[eoa] @?{re.escape(handle.casefold())}(?!\w)",
            reescrita):
        return True
    return False


def _vazio(ref: Referente) -> bool:
    """O valor não identifica nada: só pronome/advérbio, ou aberto por
    determinante indefinido ("um empresário", "algum lugar")."""
    tokens = [t for t in _normaliza(ref.valor).split() if t not in _ARTIGOS
              or t in _INDEFINIDOS]
    if not tokens:
        return True
    if tokens[0] in _INDEFINIDOS:
        return True
    uteis = [t for t in tokens if t not in _ARTIGOS]
    return not uteis or all(t in _FECHADAS for t in uteis)


def roteia(analise: Analise, texto: str) -> Analise:
    """Rebaixa a nao_verificavel o fato que não ancora no texto. Código,
    não prompt: é a barreira do princípio 6 (filtro barato antes da
    chamada cara), e cada rebaixamento sai com motivo em `roteado`.

    Quatro condições para um fato seguir ao check, todas conferidas
    contra o TEXTO DO AUTOR (ver `texto_ancoravel`):

    1. `quem` com trecho literal — "cite onde está escrito" no lugar de
       "não adivinhe". Artigo inicial não conta contra ("a Selic" ancora
       em "Selic").
    2. `o_que` ancorado. Sujeito sozinho não tem o que conferir, e
       `quando` NÃO substitui: data é qualificador, não referente —
       "Esteves se encontrou com alguém ontem" confirma qualquer
       encontro. (A versão anterior deixava a data valer, e como a data
       do post estava no texto, ela vinha de graça.)
    3. `o_que` não é vazio: pronome, advérbio ou indefinido ("André foi
       lá", "Esteves se reuniu com um empresário").
    4. O SUJEITO traz entidade nomeada, ou o predicado traz número. É a
       condição por slot: procedência não é determinação — "O encontro que
       ocorreu" é substring literal do post e ancora perfeitamente, e ainda
       assim não identifica encontro nenhum. Conferir só o `o_que` deixava
       passar "o encontro" + "o Brasil". Exigir entidade no sujeito sempre
       mataria "o desemprego está em 5,3%"; por isso o número no predicado
       é a segunda porta, e só ele.
    5. O sujeito não é coisa do próprio autor (`_do_autor`): "as alts que
       postei" e "a enquete [do autor]" ancoram e trazem número, e ainda
       assim a prova é o post — vira RELATO, não nao_verificavel, porque
       é a regra 7 que se aplica (06/09/2026).

    O que passa é o que tem QUEM e O QUÊ nomeados no texto do autor. O
    resto vira nao_verificavel (ou relato, na condição 5) com o motivo na
    trilha, e o referente rejeitado vai para `hipotese`: rebaixamento
    errado tem de ser distinguível do certo por quem lê o boletim.
    """
    norm = _normaliza(texto_ancoravel(texto))
    cabecalho = _RE_HANDLE.match(texto)
    handle = cabecalho.group(1) if cabecalho else ""
    for p in analise.premissas:
        if p.tipo != "fato":
            continue
        novo_tipo = "nao_verificavel"
        if not _ancorado(p.quem, norm):
            motivo = "sujeito sem âncora literal no texto do autor"
            rejeitado = p.quem
        elif not _ancorado(p.o_que, norm):
            motivo = "sem o QUÊ ancorado (data não substitui)"
            rejeitado = p.o_que
        elif _vazio(p.o_que):
            motivo = "o QUÊ é pronome, advérbio ou indefinido"
            rejeitado = p.o_que
        elif _do_autor(p, handle):
            motivo = ("referente é coisa do próprio autor (enquete dele, o "
                      "que ele postou): relato, a prova é o post")
            rejeitado = p.quem
            novo_tipo = "relato"
        elif not (_tem_entidade_ou_numero(p.quem) or _tem_numero(p.o_que)
                  or _classe_mensuravel(p)):
            # A heurística é POR SLOT, e o slot que importa é o sujeito:
            # conferir só o o_que deixava passar "o encontro que ocorreu"
            # + "o Brasil" — sujeito indeterminado com objeto nomeado, que
            # é a tautologia de 01/09 com outra roupa. Mas exigir entidade
            # no sujeito sempre mataria "o desemprego está em 5,3%", que é
            # conferível: sujeito genérico passa quando o predicado traz
            # NÚMERO, porque é o número que a evidência confirma ou nega.
            motivo = ("sujeito sem entidade nomeada e predicado sem número: "
                      "não há o que casar no acervo")
            rejeitado = p.quem
        else:
            continue
        p.tipo = novo_tipo
        p.afirmacao = None
        p.roteado = motivo
        if (novo_tipo == "nao_verificavel" and not p.hipotese
                and rejeitado is not None):
            p.hipotese = f"{rejeitado.valor} (referente rejeitado)"
    return analise


def versao_roteador() -> str:
    """Identidade do roteador, que é CÓDIGO e não entra no prompt.

    Sem isto, consertar `roteia` não mudava `PROMPT_VERSAO`, e o boletim
    seguia reusando separações roteadas pela versão antiga — o freio
    corrigido não rodaria em nenhum post já separado, sem aviso."""
    import inspect

    fonte = "".join(inspect.getsource(f) for f in
                    (roteia, _ancorado, _tem_entidade_ou_numero, _vazio,
                     _normaliza, texto_ancoravel, _do_autor))
    # As regexes da condição 5 são dado, não código: mudar uma sem mudar
    # `_do_autor` tem de virar versão nova do mesmo jeito (revisão de
    # 06/09/2026).
    material = (fonte + repr(sorted(_ARTIGOS | _FECHADAS | _INDEFINIDOS))
                + _POSSESSIVO_PROPRIO.pattern + _VERBO_PROPRIO.pattern
                + _AUTOR_NA_REESCRITA.pattern + _RE_HANDLE.pattern)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


INSTRUCOES = """\
Você separa as afirmações de um texto que argumenta — análise, comentário,
opinião — em cinco tipos, para que só o verificável seja conferido depois.

  fato             algo já ocorrido, ou um estado presente NO MUNDO, com
                   referente que o texto identifica. Outra fonte poderia
                   confirmar ou desmentir. É o único tipo verificado.
  previsao         afirma sobre o futuro
  opiniao          juízo, avaliação, recomendação, valoração
  relato           o assunto é o próprio autor do texto — ver a regra 7
  nao_verificavel  afirma algo sobre o mundo, mas o texto não identifica
                   de quem ou do que fala — ver a regra 8

Regras que importam mais que as outras:

1. NÚMERO NÃO GARANTE QUE É FATO. "o dólar está em R$ 5,80" é fato; "o dólar
   está caro" é opinião mesmo falando da mesma coisa. O que separa é existir
   uma fonte capaz de dizer que está errado.

2. SEPARE A PREMISSA DA CONCLUSÃO, mesmo na mesma frase.

   Texto:  "Com o desemprego em 5,3%, o Copom não tem escolha."
   fato:      o desemprego está em 5,3%
   opiniao:   o Copom não tem escolha

3. REESCRITA SÓ EM FATO — E ELA PRECISA SE SUSTENTAR SOZINHA. O campo
   `afirmacao` existe apenas para tipo=fato: é o que vai ao verificador,
   e quem o lê não tem o texto original ao lado. Resolva pronome, apelido
   e referência implícita QUE O PRÓPRIO TEXTO permita resolver — e só
   isso: nome que o texto dá incompleto vai incompleto (regra 8).

   Errado: "ela subiu 5,9%"
   Certo:  "o lucro da Caixa subiu 5,9% no 2º trimestre de 2026"

   Em previsao, opiniao, relato e nao_verificavel, OMITA `afirmacao`: o
   `trecho` literal é o registro, e parafrasear é saída paga repetindo o
   post. O `trecho` é copiar-e-colar do texto, SEMPRE — nunca resolvido,
   nunca completado; a resolução vive em `valor` e em `afirmacao`.

   Texto:   "Ninguém fala da Caixa. O lucro dela subiu 5,9%."
   fato:    o lucro da Caixa subiu 5,9%
            quem {valor "o lucro da Caixa", trecho "O lucro dela"}

4. O TRECHO É LITERAL. Copie do texto, não reescreva. É o que permite conferir
   que a separação não inventou nada.

5. NÃO CORRIJA E NÃO JULGUE. Se o texto afirma um número que você acredita
   estar errado, extraia como está. Verificar é o passo seguinte, e é feito
   contra fonte, não contra o seu conhecimento.

6. O QUE NÃO É AFIRMAÇÃO FICA DE FORA. Pergunta retórica, saudação, chamada
   para seguir o perfil, emoji solto.

7. RELATO DO PRÓPRIO AUTOR NÃO É FATO VERIFICÁVEL. Frase cujo assunto é o
   autor do texto — o que ele diz, fez, costuma fazer, postou, como opera —
   é `relato`: a prova de que ele afirma é o próprio texto, e hábito pessoal
   não sai em veículo de imprensa. Mandar isso para verificação garante
   "sem evidência" pago, para sempre.

   MAS DESEMBRULHE ANTES: quando o "eu afirmo / eu disse" carrega um fato
   sobre o MUNDO, o fato interno é a premissa — extraia-o sem o embrulho.

   Texto:   "Na onda 4 sempre fico fora, no máximo trades curtos."
   relato:  o autor fica fora do mercado na onda 4 do ciclo

   Texto:   "Eu disse ontem: o IPCA de julho veio em 5,2%."
   fato:    o IPCA de julho de 2026 foi de 5,2%

   E COISA DO PRÓPRIO AUTOR NÃO É REFERENTE DO MUNDO. "Minha enquete", "as
   alts que postei", "o post que fiz", "meu operacional": o referente só
   existe em relação ao autor, e a prova de qualquer número sobre ele é o
   próprio post — relato, nunca fato, mesmo com porcentagem. Se além disso
   não diz QUAIS ("as alts que postei"), nao_verificavel também serve;
   fato, nunca. Sinal seguro: se a reescrita precisaria dizer "do autor"
   para fazer sentido, não é fato.

   Texto:   "Minha enquete fechou: 62% acham que o Copom corta em
             setembro."
   relato:  Minha enquete fechou: 62% acham que o Copom corta   (nada de
            fato "62% acham…": a enquete é dele, a prova é o post)

8. FATO EXIGE REFERENTE DETERMINADO — e ancorado no texto. Todo fato traz
   `quem` (o sujeito) e, quando o texto dá, `o_que` (a outra entidade, o
   número, o objeto) e `quando` (a data de OCORRÊNCIA), cada um com
   `trecho` copiado LITERALMENTE do texto e `valor` COMO O TEXTO ESCREVE:
   nome incompleto vai incompleto — "André", "Esteves", "Lula", "a Selic".
   Se o texto não identifica de quem ou do que fala — "o empresário", "um
   encontro", "o cara", "ele" sem antecedente — o tipo é nao_verificavel:
   conferir "ocorreu um encontro" contra um acervo confirma qualquer
   encontro. NÃO adivinhe o referente: o que você desconfia vai em
   `hipotese`, sem âncora, e NUNCA um nome próprio que o texto não
   escreveu — `hipotese` é diagnóstico interno, não sai para o leitor.

   MAS INDIVÍDUO NÃO IDENTIFICADO É DIFERENTE DE CLASSE. A pergunta que
   decide é uma só: PARA CONFERIR ESTA AFIRMAÇÃO, PRECISO SABER QUEM É?

   * "O cara tem banco dele" — preciso. Sem saber quem, não há o que
     conferir: nao_verificavel.
   * "Há muitas recuperações judiciais em marcas conhecidas" — NÃO
     preciso. O sujeito é uma CLASSE ("marcas conhecidas") e o que se
     afirma é sobre a classe, não sobre um membro dela; o acervo mede o
     fenômeno sem nomear ninguém. É FATO, e `quem` recebe a classe como
     o texto a escreve.

   A classe entra como fato só quando o predicado é MENSURÁVEL — muitos,
   recorde, aumentou, caiu, N por cento. "As marcas conhecidas estão em
   crise" é juízo e continua opiniao.

   Medido em 03/09/2026, e é o caso que fundou esta distinção: "quantas
   recuperações judiciais estão acontecendo em marcas icônicas" saía
   nao_verificavel, e a mesma afirmação levada ao verificador voltou
   CONFIRMADO por 4 veículos (CNN, Folha, G1, Agência Brasil), com a CNN
   escrevendo "onda de recuperações judiciais". A regra estava mandando
   descartar o que o acervo sustentava.

   RESOLVER não é COMPLETAR, e a diferença decide o `valor`: RESOLVER
   anáfora cujo antecedente está NO TEXTO é obrigatório ("O lucro dela"
   → valor "o lucro da Caixa", trecho "O lucro dela"); COMPLETAR além do
   que o texto escreve é proibido ("André" fica "André"). O `trecho` é a
   prova: copiar-e-colar, sempre.

   É regra geral só quando o texto TRAZ O MARCADOR — quantificador ou
   condicional ("sempre que", "toda vez que", "se... então"): aí não
   afirma que ocorreu, e não é fato. Sem marcador, presente narrando
   evento singular É ocorrência: "André se reune com Trump" é fato;
   "Sempre que André se reúne com Trump, o dólar cai" não é.

   O nome resolve QUEM; o QUÊ também tem de estar
   no texto: "André foi lá" é nao_verificavel. Data: só a que o texto dá
   ("ontem" resolvido pelo cabeçalho é ocorrência; a data do post não é).
   Detalhe que falta (o mês de um IPCA) fica faltando. Condicional ou
   regra geral ("sempre que X se reúne com Y, Z") não afirma que ocorreu.
   E referente não basta: "Lula errou de novo" é juízo, opiniao.

   Texto:   "Charada: André se reune com Trump, todos os rumos mudam
             imediatamente. Quem manda no Brasil?"
   fato:    André se reuniu com Trump   (não "André Esteves")
            quem {valor "André", trecho "André"} · o_que {valor "Trump",
            trecho "com Trump"}; o resto sai pelas regras 2 e 6
   opiniao: todos os rumos mudam imediatamente

   Texto:   "O encontro que ocorreu muda mais o rumo do país que eleição."
   opiniao: (trecho literal — nada de fato "ocorreu um encontro")

   Texto:   "O cara tem banco dele, mídia dele, todos no bolso."
   nao_verificavel: (sujeito não identificado; `hipotese` se houver)

9. O CABEÇALHO E A LINHA DE CONTEXTO. O texto começa por "POST (@handle,
   data):" — o handle é o AUTOR (regra 7) e a data é a do post, NUNCA
   data de ocorrência. Depois dela pode haver uma linha "(contexto — ...)".
   As premissas saem SÓ do texto do post, nunca da linha de contexto: o
   "post anterior do próprio autor" (a thread dele, ou um post dele mesmo
   que ele cita) é conferido por conta própria, e extrair dele aqui é a
   mesma premissa duas vezes. O contexto serve para RESOLVER o que o post
   referencia — pronome, "isso", "esse ponto", "o encontro", nome que só
   está lá — e nisso a linha do próprio autor vale como texto dele:
   `valor` resolvido por ela e `quem`/`o_que` ancorados nela, pode.

   Texto:   "(contexto — post anterior do próprio autor: A Selic está em
             15%.)\nE vai ficar assim até 2027."
   previsao: E vai ficar assim até 2027   (e NENHUM fato "a Selic está em
             15%": ele é do post anterior)

   A linha do "post citado pelo autor" (outra conta) não resolve nem
   ancora nada: são palavras de quem ele cita — o que o autor diz sobre
   elas é premissa, o citado em si não. Nunca copie `trecho` dela.
"""


def anotacao(p: Premissa) -> str:
    """Por que o roteador rebaixou — e SÓ isso.

    A `hipotese` fica fora da tela de propósito (revisão de 03/09/2026):
    é o único campo do sistema que fabrica um referente que o autor não
    escreveu, e o texto que gera `nao_verificavel` neste acervo é
    justamente o acusatório. Exibi-la fazia a ferramenta colar o nome de
    uma pessoa real a acusações que o post não atribuiu a ninguém, sem
    fonte — contra "todo veredito carrega a fonte" e contra o princípio 5.
    Ela continua no banco, para medir se o modelo entendeu o texto: é
    diagnóstico, não saída para o leitor."""
    return f" (roteador: {p.roteado})" if p.roteado else ""


def versao_prompt() -> str:
    """Identidade do que determina a separação, como hash curto.

    Mesmo mecanismo (e mesmo motivo) do `extract.versao_prompt`: separações
    de prompts diferentes não são comparáveis, e versão que depende de
    alguém lembrar de incrementar fica errada exatamente quando importa.
    O hash carimba cada separação gravada em `separacoes` — é o que torna
    medível, depois, se uma regra nova reduziu desperdício.

    O id do MODELO entra no material (revisão de 02/09/2026): sem ele, uma
    troca de Opus para Sonnet manteria o hash, o boletim reusaria
    separações de um modelo como se fossem do outro, e o gabarito poria
    as duas saídas sob a mesma versão — impossível saber se foi o prompt
    ou o modelo.
    """
    material = INSTRUCOES + json.dumps(
        {"schema": Analise.model_json_schema(),
         "modelo": llm.VERIFICACAO.id,
         "esforco": llm.VERIFICACAO.esforco,
         "roteador": versao_roteador()},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


PROMPT_VERSAO = versao_prompt()


def _hash_texto(texto: str) -> str:
    normalizado = " ".join(texto.lower().split())
    return hashlib.sha256(normalizado.encode("utf-8")).hexdigest()[:16]


def _separacao_gravada(conexao, hash_texto: str):
    return conexao.execute(
        "SELECT * FROM separacoes WHERE texto_hash = ? AND prompt_versao = ?",
        (hash_texto, PROMPT_VERSAO)).fetchone()


def _grava_separacao(conexao, hash_texto: str, analise: "Analise",
                     custo: float) -> None:
    conexao.execute(
        "INSERT OR IGNORE INTO separacoes "
        "(texto_hash, prompt_versao, premissas_json, custo_usd, separado_em) "
        "VALUES (?, ?, ?, ?, ?)",
        (hash_texto, PROMPT_VERSAO, analise.model_dump_json(), custo,
         datetime.now(timezone.utc).isoformat()))
    conexao.commit()


def separa(texto: str, conexao=None,
           forcar: bool = False) -> tuple[Analise, llm.Uso]:
    """Separa as premissas, reusando a separação gravada quando existir.

    Mesmo texto sob a MESMA versão de prompt produz a mesma separação —
    pagar de novo é desperdício puro (uma demo repetida custou US$ 0,17
    antes desta guarda). Com `conexao`, a separação é gravada em
    `separacoes` com o carimbo de versão; reuso devolve custo zero.
    `forcar` re-separa e regrava.
    """
    if conexao is not None and not forcar:
        gravada = _separacao_gravada(conexao, _hash_texto(texto))
        if gravada is not None:
            return (Analise.model_validate_json(gravada["premissas_json"]),
                    llm.Uso(modelo=llm.VERIFICACAO, entrada=0, saida=0,
                            cache_leitura=0, cache_escrita=0))
    r = llm.gera(INSTRUCOES, f"Texto:\n{texto}", Analise,
                 modelo=llm.VERIFICACAO)
    # O roteador roda ANTES de gravar: a separação em cache tem de ser a
    # que a produção exibe, rebaixamentos incluídos.
    roteia(r.dados, texto)
    if conexao is not None:
        if forcar:
            conexao.execute(
                "DELETE FROM separacoes WHERE texto_hash = ? "
                "AND prompt_versao = ?", (_hash_texto(texto), PROMPT_VERSAO))
        _grava_separacao(conexao, _hash_texto(texto), r.dados, r.uso.custo)
    return r.dados, r.uso
