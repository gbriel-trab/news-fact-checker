"""Verificação de uma afirmação contra o acervo.

É o motor, chamado pelo boletim. Recebe uma afirmação que NÃO veio do acervo,
procura evidência, e devolve um veredito com as fontes que o sustentam.

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

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from . import apelidos, grafo, indice, llm, normalize, vocabulario
from .canonico import chave_canonica
from .storage import salva_consulta
from .vocabulario import Relacao

LACUNAS = ("quem", "o_que", "quando", "onde", "quanto")
"""As lacunas de uma afirmação, na ordem em que o juiz as alinha."""

MIN_PROXIMIDADE = 0.55
"""Piso de similaridade para uma afirmação do acervo virar evidência candidata.

Abaixo disso o texto trata de outro assunto, e incluí-lo faria o modelo julgar
contra material irrelevante — que é como se produz veredito confiante e errado.
"""

QUANTAS_CANDIDATAS = 10

FATOR_BUSCA = 3
RESERVA_DIVERSIDADE = 2
"""Quantas candidatas buscar antes de escolher, e quantas vagas ficam
RESERVADAS para veículos ainda não representados.

Corroboração é contada por veículo, então diversidade no ranking é
condição necessária para o veredito poder dizer "2 veículos" — e medido
em 03/09/2026 um único veículo ocupava 7 das 10 vagas com triplas do
mesmo evento. A reserva no fim é melhor que teto por veículo: testei as
duas na busca da charada, e o teto trocava a confirmação do BTG ao g1
por "Trump exerce cargo nos Estados Unidos". Relevância manda nas vagas
livres; a reserva só garante que o segundo veículo tenha por onde entrar."""


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


class Alinhamento(BaseModel):
    """Uma lacuna da afirmação e a contraparte que a evidência citada dá."""

    lacuna: Literal["quem", "o_que", "quando", "onde", "quanto"]
    na_afirmacao: str | None = Field(
        description="O que a afirmação diz nesta lacuna; null se ela não diz."
    )
    na_evidencia: str | None = Field(
        description=(
            "O trecho da evidência citada que responde a esta lacuna; null "
            "se nenhuma evidência responde."
        )
    )


class Julgamento(BaseModel):
    """O veredito sobre a afirmação, dada a evidência recuperada.

    O alinhamento vem ANTES do veredito no schema de propósito: o modelo
    preenche lacuna a lacuna e só então decide. Desde 03/09/2026 a
    confirmação passa por `aplica_alinhamento`, em código: sujeito sem
    contraparte, ou que não casa pela chave canônica, não confirma —
    a consulta 82 confirmou 'ocorreu um encontro' com uma sessão de
    comissão qualquer, e prosa não impediu (J2 falhou três vezes)."""

    alinhamento: list[Alinhamento] = Field(
        description=(
            "As cinco lacunas (quem, o_que, quando, onde, quanto), "
            "preenchidas antes do veredito. Sem contraparte = null."
        )
    )
    veredito: Literal["confirmado", "contradito", "sem_evidencia", "dividido"]
    retida: SkipJsonSchema[bool] = False
    """Preenchido em CÓDIGO por `aplica_alinhamento`, nunca pelo modelo, e
    fora do schema enviado: este `sem_evidencia` é confirmação retida — a
    evidência existe e não foi conferida, que não é "o acervo não cobre"."""
    evidencias: list[int] = Field(
        description=(
            "Números das evidências que sustentam o veredito, da lista "
            "apresentada. Vazio quando o veredito for sem_evidencia; em "
            "dividido, as dos DOIS lados."
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

Quatro vereditos possíveis:

  confirmado      a evidência afirma o mesmo que a afirmação
  contradito      a evidência afirma algo incompatível com ela
  sem_evidencia   a evidência não trata do que a afirmação diz
  dividido        veículos diferentes se contradizem sobre o que ela diz

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

6. ALINHE ANTES DE JULGAR. Para cada lacuna — quem, o quê, quando, onde,
   quanto — diga o que a afirmação afirma e qual trecho da evidência citada
   responde. Lacuna que a afirmação preenche e a evidência não: não pode
   confirmar. Evidência COMPATÍVEL não é evidência que SUSTENTA: "ocorreu
   um encontro" é satisfeito por qualquer reunião do mundo, e por isso não
   é confirmável — o QUEM da afirmação precisa de contraparte na evidência.

7. NOME INCOMPLETO CASA COM NOME COMPLETO quando outra lacuna fecha o
   referente: "André se reuniu com Trump" é sustentado por "André Esteves
   participou de reunião com Donald Trump" — o QUEM é contido e o O QUÊ
   coincide. Sozinho, sem outra lacuna, o nome parcial não confirma.

8. A AFIRMAÇÃO TEM DATA. A "data de referência" é quando ela foi feita, e o
   tempo entra no julgamento pelo tipo da evidência:
   * ESTADO (valor vigente: taxa, preço, contagem, cargo, saldo) se julga NA
     REFERÊNCIA. Entre evidências de estado sobre a mesma medida, vale a
     mais recente até a referência — a anterior foi superada, não
     contradiz. Estado datado DEPOIS da referência não sustenta nem
     contradiz: quem afirmou não podia saber. Estado SEM data não
     substitui um datado.
   * EVENTO (algo que ocorreu num instante) não expira: evidência de evento
     anterior à referência vale, por mais distante que seja.
   Afirmação sem data própria descreve o estado NA referência.

9. DIVIDIDO é o veredito quando VEÍCULOS DIFERENTES, falando do mesmo
   instante e da mesma medida ou do mesmo fato, são incompatíveis entre si
   — um sustenta a afirmação e outro a contradiz. Cite os dois lados. NÃO é
   dividido: evolução no tempo (regra 8), arredondamento (regra 4), o mesmo
   veículo em versões diferentes, nem negativa de parte interessada (regra
   10). Um veículo de cada lado já é dividido: o sistema não escolhe lado.

10. FALA CITADA É EVIDÊNCIA DE QUE ALGUÉM DISSE. Evidência do tipo "X
   afirmou / negou que Z" sustenta que X disse Z — não que Z é verdade ou
   mentira. Negativa de PARTE INTERESSADA (o próprio sujeito da afirmação,
   o governo, a empresa ou a pessoa envolvida) NÃO contradiz o que outro
   veículo relata na própria voz: o veredito segue a evidência de fato, e
   a negativa entra na justificativa. Diferente é a CONSTATAÇÃO DE
   AUTORIDADE sobre o fato (órgão técnico, tribunal, auditoria, perícia)
   relatada pelo veículo: isso é evidência sobre o fato, e pode confirmar
   ou contradizer.
"""


ROTULOS = {"confirmado": "CONFIRMADO", "contradito": "CONTRADITO",
           "sem_evidencia": "SEM EVIDÊNCIA", "dividido": "DIVIDIDO"}


def por_referencia(evidencias: list, referencia: str, *,
                   com_removidas: bool = False):
    """Estado se julga NA DATA DE REFERÊNCIA (quando a afirmação foi
    feita) — e isso é código, antes do juiz (08/09/2026).

    Duas peças, e a revisão adversária da mesma noite mostrou que confundi-las
    apaga evidência boa:

    1. **O que o autor não podia saber sai.** O eixo é QUANDO A MATÉRIA FOI
       PUBLICADA, não quando o fato ocorre: `data_fato` posterior é
       legítimo em projeção, meta e orçamento — no acervo real são 40
       triplas de estado ("salário mínimo deverá subir para R$ 1.741 em
       2027", publicada em 25/08/2026), e cortar por ela tornava todas
       invisíveis para qualquer check. Sem data de publicação (entrada
       indexada antes de 08/09/2026), a data do fato serve de aproximação.
    2. **Entre estados da MESMA coisa, fica o vigente.** Mesma coisa é
       (veículo, sujeito canônico, relação, OBJETO canônico, unidade): sem
       o objeto, "Esteves integra o BTG" e "Esteves integra o conselho da
       B3" viravam a mesma medida e a mais antiga sumia — dois fatos, não
       um valor que mudou. Empate no instante mantém TODOS os empatados
       (o desempate é a hora de publicação da matéria; entrada antiga não
       a tem, e empate é o caso comum). Projeção (fato posterior à
       referência) e estado sem data não substituem nem são substituídos:
       não são "o valor vigente", e o juiz decide com a regra 8.

    Evento nunca é tocado: não expira. Veículos diferentes não se
    substituem — se discordam no mesmo instante, quem diz é o juiz
    (`dividido`). A ordem original se mantém, porque o juiz lê em ordem.

    Com `com_removidas`, devolve (ficam, removidas): quem chama precisa
    saber que a lista esvaziou POR TEMPO, e não porque o acervo não
    cobre — a diferença decide se a demanda paga extração.
    """
    ref = str(referencia or "")[:10]
    if not ref:
        return (list(evidencias), []) if com_removidas else list(evidencias)

    def data_do_fato(e) -> str:
        return str(e.meta.get("data_fato") or "")[:10]

    def publicada(e) -> str:
        return str(e.meta.get("data_publicacao") or "")

    def conhecida_em(e) -> str:
        """Quando isto ficou conhecido: a matéria, senão o fato."""
        return publicada(e)[:10] or data_do_fato(e)

    fora: set[int] = set()
    vigente: dict[tuple, tuple[list[int], tuple[str, str]]] = {}
    for i, e in enumerate(evidencias):
        m = e.meta
        tipo = vocabulario.tipo_de(str(m.get("relacao") or "outro"),
                                   str(m.get("tipo_relacao") or "") or None)
        if tipo != "estado":
            continue
        if conhecida_em(e) > ref:
            fora.add(i)
            continue
        fato = data_do_fato(e)
        if not fato or fato > ref:
            continue
        chave = (str(m.get("veiculo") or "").casefold(),
                 chave_canonica(str(m.get("sujeito") or "")),
                 str(m.get("relacao") or ""),
                 chave_canonica(str(m.get("objeto") or "")),
                 str(m.get("unidade") or "").casefold())
        instante = (fato, publicada(e))
        atual = vigente.get(chave)
        if atual is None:
            vigente[chave] = ([i], instante)
        elif instante > atual[1]:
            fora.update(atual[0])
            vigente[chave] = ([i], instante)
        elif instante == atual[1]:
            atual[0].append(i)
        else:
            fora.add(i)
    ficam = [e for i, e in enumerate(evidencias) if i not in fora]
    if not com_removidas:
        return ficam
    return ficam, [e for i, e in enumerate(evidencias) if i in fora]


def versao_prompt() -> str:
    """Identidade do julgamento e da estruturação, como hash curto — o
    mesmo mecanismo do `premissas.versao_prompt`, e pelo mesmo motivo:
    veredito de prompt (ou modelo) diferente não é comparável, e a
    coluna `prompt_versao` de `consultas` é o que permite ao gabarito
    dizer 'isto já falhava antes ou é novo'."""
    from .canonico import assinatura_apelidos

    material = INSTRUCOES_ESTRUTURA + INSTRUCOES_JULGAMENTO + json.dumps(
        {"estrutura": AfirmacaoRecebida.model_json_schema(),
         "julgamento": Julgamento.model_json_schema(),
         "modelo": llm.VERIFICACAO.id,
         "esforco": llm.VERIFICACAO.esforco,
         "apelidos": assinatura_apelidos()},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


PROMPT_VERSAO = versao_prompt()


_PALAVRAS_VAZIAS = frozenset(
    "o a os as um uma uns umas de do da dos das e em no na nos nas com para "
    "por entre ao aos pelo pela pelos pelas sobre que se sua seu suas seus "
    "of the".split())

_GENERICOS = frozenset(
    "governo encontro reuniao sessao presidente ministro senador deputado "
    "empresa empresario banqueiro executivo investidor operador analista "
    "advogado juiz politico programa taxa camara banco pais mercado "
    "comissao conselho projeto medida acordo decisao processo caso grupo "
    "equipe autoridade orgao instituicao pessoa homem mulher cara sujeito "
    "cidade estado ativo indice bolsa moeda".split())
"""Substantivo comum que NÃO identifica sozinho. Se a interseção entre os
dois nomes só tem palavra desta lista, não é a mesma entidade: "governo"
⊄ "governo federal", "encontro" ⊄ "encontro de líderes"."""

from .canonico import CABECAS as _CABECAS  # noqa: E402  (lista única)
"""Cabeça de hierarquia, cargo, parentesco ou obra. Mora no `canonico`
porque o minerador de apelidos usa a MESMA guarda, e as duas cópias
divergiram: a revisão de 03/09/2026 executou o freio e achou o par
fundador do minerador passando por aqui."""


def _tokens(nome: str) -> set[str]:
    # Pontuação fora: o juiz escreve "André (e Trump)" e "o desemprego
    # (taxa de desemprego no Brasil)" — e com parênteses colados nenhum
    # token casava (primeira rodada do gabarito v4, 03/09/2026).
    limpo = re.sub(r"[^\w\s]", " ", chave_canonica(nome))
    return {t for t in limpo.split() if t not in _PALAVRAS_VAZIAS}


def sujeito_casa(afirmacao: str, evidencia: str) -> bool:
    """Os dois nomes identificam a MESMA entidade?

    Igualdade de chave canônica, ou contenção de tokens com duas guardas
    (achado da revisão de 03/09/2026 — a versão sem elas fabricava
    exatamente a corroboração que `canonico.py` se recusa a fabricar):

    * a interseção precisa de ao menos um token não-genérico: "governo"
      ⊄ "governo federal", "taxa" ⊄ "taxa de juros";
    * os tokens extras do lado maior não podem ser cabeça de hierarquia
      ou de evento: "Lula" ⊄ "governo do presidente Lula", "Trump" ⊄
      "Telefonema entre Lula e Trump", "Braskem" ⊄ "Braskem Idesa" só
      passa se "idesa" não for cabeça — e não é, então este par continua
      casando; apelido curado é o caminho certo para separá-los, como o
      `canonico.py` já decidiu.

    Falso negativo aceito, e é grande: título antes do nome ("presidente
    Lula", "senador Omar Aziz", "juiz Diego Câmara") também não casa com o
    nome nu, porque o MESMO token separa a pessoa da instituição que ela
    dirige — "Presidente dos Estados Unidos" não é os Estados Unidos, e
    nenhuma regra barata distingue os dois usos. Perder uma confirmação é
    o erro barato: ela sai como retida, com a evidência na tela. Fabricar
    uma é o caro (princípio 5).

    Continua valendo o que o freio precisa: "André" ⊂ "André Esteves",
    "desemprego" ⊂ "taxa de desemprego no Brasil", "Braskem" = "Braskem
    S.A.". "um encontro" contra "sessão da comissão mista" não casa.
    """
    ta, te = _tokens(afirmacao), _tokens(evidencia)
    if not ta or not te:
        return False
    if ta == te:
        return True
    if not (ta <= te or te <= ta):
        return False
    if not (ta & te) - _GENERICOS:
        return False
    extras = (te - ta) if ta <= te else (ta - te)
    return not (extras & _CABECAS)


def _identifica(nome: str | None) -> bool:
    """O nome nomeia alguém ou algo, ou é substantivo comum solto?
    "André Esteves" sim, "Encontro" não, "" não."""
    return bool(nome) and bool(_tokens(nome) - _GENERICOS)


def apoios_de(afirmacao: "AfirmacaoRecebida") -> tuple[list[str], list[str]]:
    """(sujeitos candidatos, apoios) da afirmação ESTRUTURADA.

    Dois sujeitos porque o estruturador escolhe um dos lados da tripla e
    oscila entre eles ("Copom" ou "Taxa Selic" para o mesmo fato, medido
    em 03/09/2026); aceitar os dois evita falso negativo por escolha de
    lado. Apoios são o que a afirmação afirma ALÉM do sujeito — é o que
    tem de reaparecer na evidência citada."""
    sujeitos = [s for s in (afirmacao.sujeito_canonico,
                            afirmacao.objeto_canonico) if s]
    apoios = [a for a in (afirmacao.objeto_canonico,
                          f"{afirmacao.valor_numero:g}"
                          if afirmacao.valor_numero is not None else None)
              if a]
    return sujeitos, apoios


def _texto_da_evidencia(e: "indice.Achado") -> str:
    m = e.meta
    return " ".join(str(x) for x in (e.texto, m.get("sujeito", ""),
                                     m.get("objeto", ""), m.get("valor", "")))


def aplica_alinhamento(julgamento: Julgamento, citadas: list,
                       sujeitos: list[str],
                       apoios: list[str]) -> Julgamento:
    """Retém, em código, confirmação que a evidência CITADA não sustenta.

    Só toca `confirmado` — é o único veredito que pode ser falso positivo
    (princípio 5). O freio não lê o que o juiz escreveu no alinhamento:
    a revisão de 03/09/2026 mostrou que `na_evidencia` é texto livre, e
    o juiz tende a parafrasear a evidência com a palavra da afirmação —
    a consulta 82 passaria de novo. Aqui as duas condições são medidas
    contra o METADADO da evidência citada:

    1. a afirmação nomeia um sujeito determinado (`_identifica`) — sem
       isso não há o que conferir, e é o caso "ocorreu um encontro";
    2. existe UMA evidência citada em que o sujeito casa (`sujeito_casa`;
       `sujeitos` vem ordenado — [sujeito, objeto] da afirmação
       estruturada — porque objeto-contra-objeto é o par que confirmaria
       "Petrobras pediu recuperação" com "Braskem pediu recuperação") E
       um apoio da afirmação reaparece nessa evidência. Sem apoio
       estrutural (afirmação sem objeto nem valor), a decisão semântica
       do juiz vale — o freio é backstop, não segundo juiz.

    Retida mantém as evidências e a justificativa do juiz visíveis, e
    marca `retida`: "achei isto e não conferi" é diferente de "o acervo
    não cobre", e quem consome (boletim, demanda) precisa distinguir —
    senão a retenção dispara extração paga para cobrir o que já está
    coberto."""
    if julgamento.veredito == "dividido":
        # Dividido exige veículos DIFERENTES de cada lado (regra 9): com
        # um só citado, o juiz chamou de divisão o que é uma redação —
        # fica retido, com a evidência visível, e sem disparar demanda.
        veiculos = {str(e.meta.get("veiculo") or "").casefold() for e in citadas}
        if len(veiculos) >= 2:
            return julgamento
        return julgamento.model_copy(update={
            "veredito": "sem_evidencia", "retida": True,
            "justificativa": (f"Divisão retida: {len(veiculos)} veículo "
                              f"citado, e dividido exige veículos diferentes "
                              f"de cada lado. O juiz havia escrito: "
                              f"{julgamento.justificativa}")})
    if julgamento.veredito != "confirmado":
        return julgamento
    if not any(_identifica(s) for s in sujeitos):
        motivo = ("a afirmação não nomeia um sujeito determinado"
                  + (f' (veio "{sujeitos[0]}")' if sujeitos else ""))
    elif not citadas:
        motivo = "confirmado sem citar evidência"
    else:
        sujeito, objeto = (sujeitos + [None, None])[:2]
        for e in citadas:
            ev_sujeito = e.meta.get("sujeito") or ""
            ev_objeto = e.meta.get("objeto") or ""
            # Pares aceitos: o sujeito da afirmação em qualquer lado da
            # tripla, ou o objeto dela no SUJEITO da tripla (o
            # estruturador troca os lados). Objeto contra objeto fica de
            # fora de propósito: é o par que confirma "Petrobras pediu
            # recuperação" com "Braskem pediu recuperação".
            if not (sujeito_casa(sujeito or "", ev_sujeito)
                    or sujeito_casa(sujeito or "", ev_objeto)
                    or sujeito_casa(objeto or "", ev_sujeito)):
                continue
            corpo = _tokens(_texto_da_evidencia(e))
            if not apoios or any(_tokens(a) & corpo for a in apoios):
                return julgamento
        motivo = ("nenhuma evidência citada casa o sujeito "
                  f"{sujeitos[0]!r} e mais um apoio da afirmação")
    return julgamento.model_copy(update={
        "veredito": "sem_evidencia", "retida": True,
        "justificativa": (f"Confirmação retida: {motivo}. O juiz havia "
                          f"escrito: {julgamento.justificativa}")})


def estrutura(texto: str) -> tuple[AfirmacaoRecebida, llm.Uso]:
    r = llm.gera(INSTRUCOES_ESTRUTURA, f"Afirmação: {texto}",
                 AfirmacaoRecebida, modelo=llm.VERIFICACAO)
    return r.dados, r.uso


def _por_chave(afirmacao: AfirmacaoRecebida,
               acervo: list[grafo.Afirmacao],
               mapa: dict[str, str] | None = None) -> list[indice.Achado]:
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
    # Os alvos incluem os apelidos FUNDADOS no acervo e ainda não promovidos:
    # "lula" também procura por "luiz inacio lula da silva" porque algum
    # veículo escreveu as duas formas na mesma matéria. Isto só AMPLIA a
    # recuperação — o que entrar passa pelo juiz e pelo freio como qualquer
    # candidata, e a identidade só vira oficial por promoção humana
    # (src/apelidos.py). É a diferença entre propor e fornecer.
    alvos = {chave_canonica(afirmacao.sujeito_canonico)}
    if mapa:
        alvos |= {chave_canonica(x) for x in
                  apelidos.equivalentes(afirmacao.sujeito_canonico, mapa)}
    # A relação do ACERVO já vem normalizada — `grafo.carrega` aplica
    # `relacao_normalizada` na leitura. A do estruturador NÃO passa por
    # nada. Comparar os dois lados crus contra normalizados fazia a rota
    # devolver lista VAZIA, sem erro, exatamente no caso que ela existe
    # para cobrir: afirmação com número e sem objeto. Se o estruturador
    # escolhesse `obteve_percentual_em` para "Juliana Brizola tem 38%" —
    # e o vocabulário permite, "número no valor" —, a tripla gravada como
    # `tem_atributo` nunca casava. Só o prompt segurava isso; agora o
    # código segura. (Achado da revisão de 03/09/2026.)
    alvo_relacao = grafo.relacao_normalizada(
        afirmacao.relacao.value, afirmacao.objeto_canonico,
        afirmacao.valor_numero)
    for a in acervo:
        # UNIÃO, não troca. Normalizar o alvo consertava a afirmação
        # numérica sem objeto, mas TROCAR a relação quebrava o caso
        # oposto e mais comum: afirmação com número E objeto, cuja
        # relação o acervo gravou crua. Barreira que conserta um lado e
        # abre o outro é o aperto do princípio 9 outra vez — as duas
        # grafias casam, e só elas.
        if (chave_canonica(a.sujeito) in alvos
                and a.relacao in {alvo_relacao, afirmacao.relacao.value}):
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
                    # Lidos por `por_referencia`: a data da MATÉRIA (o eixo
                    # do corte) e o tipo da relação, que só o modelo sabe
                    # quando a relação é `outro` — sem ele, 622 triplas em
                    # `outro` passavam pela barreira como se fossem evento.
                    "data_publicacao": a.data_publicacao or "",
                    "tipo_relacao": a.tipo_relacao or "",
                },
            ))
    return achados


def recupera(afirmacao: AfirmacaoRecebida,
             acervo: list[grafo.Afirmacao] | None = None,
             mapa: dict[str, str] | None = None,
             referencia: str = "",
             sem_tempo: bool = False) -> list[indice.Achado]:
    """Junta candidatas por proximidade semântica e por identidade exata.

    As duas rotas são complementares e cobrem falhas uma da outra: a vetorial
    encontra o fato descrito com outras palavras, e a chave exata garante que
    tudo que o acervo afirma sobre aquele par (sujeito, relação) chegue ao
    julgamento, independente de como ficou o ranking.

    A chave exata entra PRIMEIRO. A ordem importa porque o modelo lê a lista em
    ordem, e porque um teto de candidatas cortaria o fim — que era exatamente
    onde a evidência certa estava caindo.
    """
    # O TEMPO entra antes da escolha (08/09/2026). Filtrar depois gastava
    # a reserva de diversidade numa evidência que a barreira ia remover em
    # seguida: a vaga do segundo veículo não voltava ao ranking e a lista
    # chegava ao juiz com um veículo só — o oposto do que a reserva existe
    # para garantir, e do critério de duas fontes do AC1. `sem_tempo`
    # devolve o material bruto, para quem precisa CONTAR o que a barreira
    # tira (`verifica`).
    janela = "" if sem_tempo else referencia
    achados = por_referencia(_por_chave(afirmacao, acervo or [], mapa),
                             janela)
    vistos = {_chave_candidata(a) for a in achados}

    # Busca FATOR_BUSCA× e escolhe com teto por veículo: sem isso um
    # veículo enche as vagas com as próprias triplas do mesmo evento
    # (medido em 03/09/2026: G1 em 7 das 10 para a reunião Esteves–Trump)
    # e o segundo veículo, que é o que corrobora, fica de fora.
    candidatas = []
    for a in por_referencia(
            indice.busca("afirmacoes", afirmacao.busca,
                         QUANTAS_CANDIDATAS * FATOR_BUSCA), janela):
        chave = _chave_candidata(a)
        if a.proximidade >= MIN_PROXIMIDADE and chave not in vistos:
            a.meta.setdefault("rota", "semantica")
            candidatas.append(a)
            vistos.add(chave)

    return achados + _diversifica(candidatas)


def _diversifica(ordenadas: list[indice.Achado],
                 quantas: int = QUANTAS_CANDIDATAS,
                 reserva: int = RESERVA_DIVERSIDADE) -> list[indice.Achado]:
    """Pega as melhores por proximidade, guardando `reserva` vagas para
    veículos ainda não representados; se não houver veículo novo, as
    vagas voltam ao ranking. A ordem final é a de proximidade, como o
    julgamento espera."""
    livres = max(0, quantas - reserva)
    escolhidas = ordenadas[:livres]
    vistos = {a.meta.get("veiculo", "") for a in escolhidas}
    resto = ordenadas[livres:]
    novos = [a for a in resto if a.meta.get("veiculo", "") not in vistos]
    # Um por veículo novo, na ordem de proximidade.
    for a in novos:
        if len(escolhidas) - livres >= reserva:
            break
        if a.meta.get("veiculo", "") not in vistos:
            escolhidas.append(a)
            vistos.add(a.meta.get("veiculo", ""))
    if len(escolhidas) < quantas:
        faltam = quantas - len(escolhidas)
        escolhidas += [a for a in resto if a not in escolhidas][:faltam]
    return sorted(escolhidas, key=lambda a: a.distancia)


def _chave_candidata(a: indice.Achado) -> tuple:
    """Identidade de uma candidata: VEÍCULO + os CAMPOS da tripla.

    Até 03/09/2026 o veículo não entrava, e o modo história grava a
    mesma tripla, com o mesmo texto, para cada veículo que a afirma —
    a cópia do Valor era descartada como duplicata da do G1, e a
    reunião Esteves–Trump saía 'CONFIRMADO · 1 veículo' com dois veículos
    no acervo. Corroboração é contada por veículo; a dedup tem de ser.
    Caixa fora: re-extração do mesmo artigo difere em "Reunião" ×
    "reunião", e as duas rotas traziam a mesma tripla duas vezes.

    O TEXTO saiu da identidade em 03/09/2026, e essa é a correção que
    fecha a rota dupla. O índice grava o texto com a relação CRUA
    (`indice.indexa_afirmacoes` lê `t.relacao` do banco) e a rota por
    chave o renderiza com a NORMALIZADA (`grafo.carrega` normaliza na
    leitura). Para uma tripla com valor e sem objeto gravada sob outra
    relação, o Chroma guarda "Caixa outro 3,9 bi" e a rota por chave
    produz "Caixa tem atributo 3,9 bi": textos diferentes, chaves
    diferentes, dedup não dispara, e a MESMA tripla ocupa uma vaga em
    cada rota — corroboração inflada, que é o falso positivo do
    princípio 5.

    Consertar do lado do índice seria normalizar na GRAVAÇÃO, contra o
    que a própria `relacao_normalizada` existe para evitar, e obrigaria
    a reindexar o acervo a cada mudança da regra. Então a identidade
    passa a sair dos CAMPOS do metadado, que as duas rotas gravam
    iguais, com a relação normalizada na leitura — aqui. Nada se perde:
    são os mesmos campos que `texto_da_tripla` renderiza."""
    m = a.meta
    valor = m.get("valor")
    return (m.get("veiculo", ""),
            chave_canonica(m.get("sujeito", "")),
            grafo.relacao_normalizada(
                m.get("relacao", ""), m.get("objeto") or None,
                valor if valor not in (None, "") else None),
            chave_canonica(m.get("objeto", "")),
            str(valor if valor is not None else ""),
            (m.get("unidade") or "").casefold(),
            (m.get("contexto") or "").casefold())


_ORIGEM_LEGIVEL = {"EXTRACTED": "explícita", "INFERRED": "inferida",
                   "e": "explícita", "i": "inferida"}
"""O enum de origem em português de tela. Cobre as duas gerações de
valores gravados (EXTRACTED/INFERRED até 01/09/2026; e/i do schema magro
em diante) — o cru vazava para o Telegram e para o julgamento."""


def _origem_legivel(valor: str) -> str:
    return _ORIGEM_LEGIVEL.get(valor, valor)


def julga(texto: str, evidencias: list[indice.Achado],
          referencia: str = "") -> tuple[Julgamento, llm.Uso]:
    linhas = []
    for i, e in enumerate(evidencias, 1):
        m = e.meta
        publicada = str(m.get("data_publicacao") or "")[:10]
        linhas.append(
            f"[{i}] {e.texto}\n"
            f"    veículo: {m['veiculo']} · data do fato: {m['data_fato'] or 'não informada'}"
            + (f" · publicada em {publicada}" if publicada else "")
            + f" · afirmação {_origem_legivel(m['origem'])}\n"
            f"    matéria: {m['titulo']}"
        )
    # A data de referência é a do POST (fato da API), nunca resolvida
    # pelo modelo — regra 8 do julgamento.
    quando = str(referencia or "")[:10] or "não informada"
    corpo = (
        f"AFIRMAÇÃO A VERIFICAR:\n{texto}\n"
        f"DATA DE REFERÊNCIA (quando a afirmação foi feita): {quando}\n\n"
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
O boletim ignora a janela quando re-verifica (`forcar=True`).
"""


def consulta_recente(conexao, texto: str,
                     horas: int = HORAS_REUSO,
                     referencia: str = ""):
    """Veredito já gravado para esta afirmação dentro da janela, ou None.

    O casamento é por texto normalizado (minúsculas, espaços colapsados) em
    Python — o lower() do SQLite ignora acento e mentiria em "É falso que".

    E pela DATA DE REFERÊNCIA (08/09/2026): desde que o dia da afirmação
    entra no julgamento (regra 8), o mesmo texto pode ter vereditos
    diferentes em dias diferentes — refazer 25/08 e 26/08 na mesma sessão,
    com a mesma premissa citada, reusaria o veredito do primeiro dia no
    segundo. Linha antiga não tem a coluna e vale como veredito de HOJE,
    que é o que ela era: só reusa quando a referência atual também é hoje.
    """
    if conexao is None:
        return None
    alvo = " ".join(texto.lower().split())
    dia = (str(referencia or "")[:10]
           or datetime.now(timezone.utc).date().isoformat())
    hoje = datetime.now(timezone.utc).date().isoformat()
    limite = (datetime.now(timezone.utc)
              - timedelta(hours=horas)).isoformat()
    for linha in conexao.execute(
            "SELECT * FROM consultas WHERE consultado_em >= ? "
            "ORDER BY id DESC", (limite,)):
        if " ".join(linha["afirmacao"].lower().split()) != alvo:
            continue
        try:
            gravada = str(linha["referencia"] or "")[:10]
        except (IndexError, KeyError):
            gravada = ""
        if gravada == dia or (not gravada and dia == hoje):
            return linha
    return None


def verifica(texto: str, verboso: bool = False,
             conexao=None, acervo=None, forcar: bool = False,
             referencia: str = "") -> None:
    """`referencia` é QUANDO a afirmação foi feita (ISO; a data do post no
    boletim). Sem ela, agora: a afirmação avulsa é de hoje. Entra na
    seleção de estado vigente (`por_referencia`) e no prompt do juiz."""
    print(f'AFIRMAÇÃO\n  "{texto}"\n')
    referencia = referencia or datetime.now(timezone.utc).isoformat()

    if not forcar:
        anterior = consulta_recente(conexao, texto, referencia=referencia)
        if anterior is not None:
            rotulo = ROTULOS.get(anterior["veredito"],
                                 str(anterior["veredito"]).upper())
            quando = anterior["consultado_em"][:16].replace("T", " ")
            print(f"VEREDITO (reusado — verificada em {quando} UTC)\n"
                  f"  {rotulo} · {anterior['veiculos']} veículo(s)\n")
            print(f"POR QUE\n  {anterior['justificativa']}\n")
            print("  Sem custo: veredito gravado nas últimas "
                  f"{HORAS_REUSO}h e reaproveitado "
                  "(o acervo pode ter crescido desde então).")
            return

    afirmacao, uso1 = estrutura(texto)
    if verboso:
        print(f"  estruturada: ({afirmacao.sujeito_canonico}, "
              f"{afirmacao.relacao.value}, {afirmacao.objeto_canonico or '—'})")
        print(f"  busca: \"{afirmacao.busca}\"\n")

    # Apelidos fundados no acervo e ainda não promovidos ampliam a rota por
    # chave; falha ao montá-los não derruba a verificação.
    try:
        mapa = apelidos.mapa_vivo(conexao) if conexao is not None else {}
    except Exception:  # noqa: BLE001
        mapa = {}
    brutas = recupera(afirmacao, acervo, mapa, referencia, sem_tempo=True)
    evidencias, fora_do_tempo = por_referencia(brutas, referencia,
                                               com_removidas=True)
    removidas_por_tempo = len(fora_do_tempo)

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

    if not evidencias and removidas_por_tempo:
        # A barreira temporal tirou TUDO: o acervo trata do assunto, e o
        # que falta é matéria da época. Dizer "os veículos não falam do
        # assunto" seria falso — e, sem a marca `retida`, o boletim
        # dispararia extração paga para cobrir o que já está coberto (o
        # gasto que a marca existe para evitar desde 03/09).
        print("VEREDITO\n  SEM EVIDÊNCIA (evidência fora da janela "
              f"temporal) · {removidas_por_tempo} candidata(s) "
              "descartada(s)\n")
        print("  O acervo trata do assunto, mas o que ele tem é posterior "
              "à data da afirmação\n  (ou foi superado por valor mais "
              "recente do mesmo veículo).")
        print(f"\n  custo: US$ {uso1.custo:.4f}")
        if conexao is not None:
            salva_consulta(conexao, texto, "sem_evidencia",
                           f"{removidas_por_tempo} candidata(s) fora da "
                           "janela temporal da referência; nenhuma restou "
                           "para julgar.",
                           removidas_por_tempo, 0, 0, llm.VERIFICACAO.id,
                           uso1.custo, prompt_versao=PROMPT_VERSAO,
                           retida=True, referencia=referencia)
        return

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
                           0, 0, 0, llm.VERIFICACAO.id, uso1.custo,
                           prompt_versao=PROMPT_VERSAO,
                           referencia=referencia)
        return

    julgamento, uso2 = julga(texto, evidencias, referencia)
    citadas = [evidencias[i - 1] for i in julgamento.evidencias
               if 1 <= i <= len(evidencias)]
    # O sujeito e os apoios vêm do ESTRUTURADOR e a conferência é contra o
    # metadado da evidência citada: o juiz não decide sozinho se "um
    # encontro" é um sujeito, nem escreve a contraparte que se confere.
    julgamento = aplica_alinhamento(julgamento, citadas,
                                    *apoios_de(afirmacao))
    veiculos = {e.meta["veiculo"] for e in citadas}

    rotulo = ROTULOS[julgamento.veredito]
    if julgamento.retida:
        # A evidência continua na tela, rotulada: o veredito que mais
        # precisa de auditoria não pode ser o que menos mostra.
        print(f"VEREDITO\n  SEM EVIDÊNCIA (confirmação retida) · "
              f"{len(veiculos)} veículo(s) encontrado(s), nenhum conferido\n")
    else:
        print(f"VEREDITO\n  {rotulo} · {len(veiculos)} "
              f"{'veículo' if len(veiculos) == 1 else 'veículos'}\n")

    if verboso:
        print("ALINHAMENTO")
        for a in julgamento.alinhamento:
            print(f"  {a.lacuna:<7} {a.na_afirmacao or '—'}  ⇄  "
                  f"{a.na_evidencia or '—'}")
        print()

    if citadas:
        print("EVIDÊNCIA ENCONTRADA MAS NÃO CONFERIDA" if julgamento.retida
              else "EVIDÊNCIA")
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

    if julgamento.veredito == "dividido":
        print("\n  ATENÇÃO: os veículos discordam. Nenhum lado conta como "
              "confirmação.")
    elif len(veiculos) == 1 and julgamento.veredito != "sem_evidencia":
        print("\n  ATENÇÃO: um veículo só. Sem confirmação independente.")
    elif apoio_fragil(_fontes_citadas(citadas), veiculos):
        print("\n  ATENCAO: dois veiculos, e um deles e pagina "
              "ao vivo - o link nao mostra o fato sozinho.")

    print(f"\n  {len(evidencias)} candidatas recuperadas · "
          f"custo US$ {uso1.custo + uso2.custo:.4f}")

    if conexao is not None:
        salva_consulta(conexao, texto, julgamento.veredito,
                       julgamento.justificativa, len(evidencias),
                       len(citadas), len(veiculos), llm.VERIFICACAO.id,
                       uso1.custo + uso2.custo, prompt_versao=PROMPT_VERSAO,
                       retida=julgamento.retida,
                       referencia=referencia,
                       # (veículo, título, url) do que o JUIZ citou — não
                       # do que foi recuperado. É o que o princípio 2
                       # exige poder mostrar de novo depois.
                       evidencias=_fontes_citadas(citadas))


def apoio_fragil(fontes: list, veiculos) -> bool:
    """Confirmação de EXATAMENTE dois veículos com um deles em liveblog.

    A CONTAGEM não muda — decisão do usuário em 03/09/2026, e ela é
    defensável: o fato é real e o veículo realmente o publicou. O que não
    é defensável é o leitor não saber.

    O caso perigoso é estreito, e por isso é o único que avisa: com dois
    veículos o critério do AC1 ("confirmado por duas fontes
    independentes") passa a se apoiar num link que não mostra o fato — é
    preciso rolar a página até achar a entrada. Com três ou mais sobra
    corroboração que se sustenta sozinha; com um, o aviso que já existia
    cobre.

    Medido em 03/09/2026: 7 confirmações têm exatamente dois veículos e
    NENHUMA tem liveblog hoje. Mas nove veículos publicam liveblog no
    acervo (InfoMoney 63, Folha 25, G1 20) — é questão de tempo."""
    if len(veiculos) != 2:
        return False
    return any(normalize.e_live(f.get("url", "")) for f in fontes)



def _fontes_citadas(citadas) -> list[dict]:
    """(veículo, título, url, data) do que o juiz citou, UMA vez por URL.

    Uma matéria rende várias triplas e o juiz cita mais de uma; sem a
    dedup o boletim mostrava "2 veículo(s)" e QUATRO linhas, Valor e G1
    repetidos. A contagem de veículos já era por veículo — o que estava
    fora de passo era a lista de fontes."""
    vistas, saida = set(), []
    for a in citadas:
        url = a.meta.get("url", "")
        if url in vistas:
            continue
        vistas.add(url)
        saida.append({"veiculo": a.meta.get("veiculo", ""),
                      "titulo": a.meta.get("titulo", ""),
                      "url": url,
                      "data": a.meta.get("data_fato", "")})
    return saida
