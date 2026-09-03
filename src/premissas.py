"""Confere as premissas de um texto que argumenta.

    python -m src.premissas "cole o texto aqui"
    python -m src.premissas < post.txt

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

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from . import check, config, grafo, llm
from .storage import conecta


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
    def _fato_tem_reescrita(self) -> "Premissa":
        # Garantia, não pedido: se o modelo esquecer a reescrita num fato,
        # o trecho literal vira a consulta — perder a premissa paga seria
        # pior que verificar a frase crua.
        if self.tipo == "fato" and not self.afirmacao:
            self.afirmacao = self.trecho
        return self


class Analise(BaseModel):
    premissas: list[Premissa]


# --------------------------------------------------------------- roteador

def _normaliza(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto)
    limpo = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return " ".join(limpo.casefold().split())


_ARTIGOS = frozenset(
    "o a os as um uma uns umas e com de do da dos das em no na nos nas que "
    "mas se por para ao aos pelo pela sobre ate até".split())
_FECHADAS = frozenset(
    "la ali aqui isso isto aquilo nisso disso ele ela eles elas o a "
    "alguem algo tudo nada".split())


def _ancorado(ref: Referente | None, texto_norm: str) -> bool:
    return (ref is not None and bool(ref.trecho.strip())
            and _normaliza(ref.trecho) in texto_norm)


def _tem_entidade_ou_numero(valor: str) -> bool:
    """Heurística pré-LLM da barreira: nome próprio (maiúscula que não é
    artigo), sigla ou número. Não decide sozinha — é o segundo apoio
    depois das âncoras."""
    for token in valor.split():
        limpo = token.strip("\"'(),.;:!?«»")
        if not limpo:
            continue
        if any(c.isdigit() for c in limpo):
            return True
        if limpo[0].isupper() and limpo.casefold() not in _ARTIGOS:
            return True
    return False


def roteia(analise: Analise, texto: str) -> Analise:
    """Rebaixa a nao_verificavel o fato que não ancora no texto. Código,
    não prompt: é a barreira do princípio 6 (filtro barato antes da
    chamada cara), e cada rebaixamento sai com motivo em `roteado`.

    Quatro condições para um fato seguir ao check, todas conferidas
    contra o texto que o modelo recebeu:

    1. `quem` com trecho literal no texto — "cite onde está escrito"
       no lugar de "não adivinhe".
    2. `o_que` OU `quando` ancorado: sujeito sozinho não tem o que
       conferir ("Esteves se encontrou com alguém").
    3. `o_que` não é só pronome ou advérbio ("André foi lá").
    4. Entidade nomeada, número ou data de ocorrência em algum lugar:
       "o empresário" + "um banco" não passa; "desemprego" + "5,3%" passa.
       Data de janela não conta — só a que ancora num trecho.

    Residual conhecido: "o cara tem Banco dele" passa pela heurística 4
    se o modelo classificar como fato — a primeira barreira ali é o
    prompt, e o caso C3 do gabarito vigia.
    """
    norm = _normaliza(texto)
    for p in analise.premissas:
        if p.tipo != "fato":
            continue
        quando_ok = _ancorado(p.quando, norm)
        if not _ancorado(p.quem, norm):
            motivo = "sujeito sem âncora literal no texto"
        elif not _ancorado(p.o_que, norm) and not quando_ok:
            motivo = "só o sujeito: nenhum o_que nem quando ancorado"
        elif (p.o_que is not None and not quando_ok and all(
                t in _FECHADAS for t in _normaliza(p.o_que.valor).split())):
            motivo = "o_que é pronome ou advérbio: não há o que conferir"
        elif not (_tem_entidade_ou_numero(p.quem.valor)
                  or (p.o_que is not None
                      and _tem_entidade_ou_numero(p.o_que.valor))
                  or quando_ok):
            motivo = "sem entidade nomeada, número ou data de ocorrência"
        else:
            continue
        p.tipo = "nao_verificavel"
        p.afirmacao = None
        p.roteado = motivo
    return analise


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

   Para previsao, opiniao e relato, OMITA `afirmacao`: o `trecho` literal
   é o registro, e parafrasear opinião é saída paga repetindo o post.

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

8. FATO EXIGE REFERENTE DETERMINADO — e ancorado no texto. Todo fato traz
   `quem` (o sujeito) e, quando o texto dá, `o_que` (a outra entidade, o
   número, o objeto) e `quando` (a data de OCORRÊNCIA), cada um com
   `trecho` copiado LITERALMENTE do texto e `valor` COMO O TEXTO ESCREVE:
   nome incompleto vai incompleto — "André", "Esteves", "Lula", "a Selic".
   Se o texto não identifica de quem ou do que fala — "o empresário", "um
   encontro", "o cara", "ele" sem antecedente — o tipo é nao_verificavel:
   conferir "ocorreu um encontro" contra um acervo confirma qualquer
   encontro. NÃO adivinhe o referente: o que você desconfia vai em
   `hipotese`, sem âncora. O nome resolve QUEM; o QUÊ também tem de estar
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

9. LINHAS DE CONTEXTO. O texto pode abrir com "(contexto — ...)":
   "palavras do interlocutor" NÃO são premissa do autor — só o que ele
   responde é; "post anterior do próprio autor na thread" É texto do
   autor, mesmas regras; "post citado pelo autor" são palavras de quem ele
   cita — o que o autor diz sobre elas é premissa, o citado em si não.
"""


def anotacao(p: Premissa) -> str:
    """O que o leitor da trilha precisa saber além do tipo: por que o
    roteador rebaixou, e a hipótese do modelo — marcada como não
    conferida, porque nunca vai ao check."""
    partes = []
    if p.roteado:
        partes.append(f"roteador: {p.roteado}")
    if p.hipotese:
        partes.append(f"hipótese não conferida: {p.hipotese}")
    return f" ({'; '.join(partes)})" if partes else ""


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
         "esforco": llm.VERIFICACAO.esforco},
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


def main() -> None:
    # stdin entra na lista porque este módulo LÊ da entrada padrão: sem o
    # reconfigure, arquivo UTF-8 redirecionado no Windows chega em cp1252 e
    # o texto vai mojibake para o modelo ("cÃºpula") — visto em 30/08/2026.
    for fluxo in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Confere as premissas de um texto que argumenta.")
    parser.add_argument("texto", nargs="*",
                        help="o texto; se omitido, lê da entrada padrão")
    parser.add_argument("--dry-run", action="store_true",
                        help="mostra o que seria enviado, sem chamar a API")
    parser.add_argument("-v", action="store_true", help="mostra as candidatas")
    parser.add_argument("--forcar", action="store_true",
                        help="re-separa mesmo com separação gravada desta "
                             "versão de prompt")
    args = parser.parse_args()

    texto = " ".join(args.texto).strip() or sys.stdin.read().strip()
    if not texto:
        print('Uso: python -m src.premissas "texto"  ou  ... < arquivo.txt')
        sys.exit(1)

    if args.dry_run:
        print(f"--- system (fixo, cacheado) ---\n{INSTRUCOES}")
        print(f"--- user ---\nTexto:\n{texto}\n")
        print(f"~{(len(INSTRUCOES) + len(texto)) // 4} tokens de entrada")
        print("\nNada foi enviado. Remova --dry-run para rodar.")
        return

    print(f"TEXTO\n  {texto[:300]}{'...' if len(texto) > 300 else ''}\n")

    conexao = conecta(config.BANCO)
    analise, uso = separa(texto, conexao=conexao, forcar=args.forcar)
    if uso.custo == 0:
        print("(separação reusada — já paga nesta versão de prompt; "
              "--forcar re-separa)\n")
    fatos = [p for p in analise.premissas if p.tipo == "fato"]
    resto = [p for p in analise.premissas if p.tipo != "fato"]

    print(f"{len(analise.premissas)} afirmações · {len(fatos)} verificáveis\n")

    if resto:
        # Impresso ANTES, e nomeado pelo que é. Previsão e opinião não são
        # defeito do texto — são o texto. Mostrá-las como descarte sugeriria
        # que o autor deveria tê-las evitado.
        print("=" * 78)
        print("NÃO VERIFICÁVEL — e não deve ser")
        print("=" * 78)
        for p in resto:
            print(f"  [{p.tipo}] {p.texto}{anotacao(p)}")
        print()

    if not fatos:
        print("Nenhuma afirmação factual. Nada a conferir.")
        print(f"\n  custo: US$ {uso.custo:.4f}")
        conexao.close()
        return

    acervo = grafo.carrega(conexao)
    if not acervo:
        print("Acervo vazio. Rode a coleta, a extração e o índice.")
        conexao.close()
        sys.exit(1)

    print("=" * 78)
    print("PREMISSAS, CONFERIDAS CONTRA O ACERVO")
    print("=" * 78)
    for i, p in enumerate(fatos, 1):
        print(f"\n[{i}/{len(fatos)}] no texto: \"{p.trecho[:110]}\"")
        check.verifica(p.texto, verboso=args.v,
                       conexao=conexao, acervo=acervo)
    conexao.close()

    print("\n" + "=" * 78)
    # O aviso fecha a saída de propósito: é a última coisa lida, e é a que
    # impede a leitura errada. Ver o cabeçalho do módulo.
    print("Isto confere NÚMEROS contra o acervo, não avalia o autor.")
    print("Premissa sem evidência significa que os veículos coletados não")
    print("cobrem o assunto — não que a afirmação seja falsa.")
    print(f"\n  separação das premissas: US$ {uso.custo:.4f}"
          f" · mais uma verificação por premissa"
          f" · prompt {PROMPT_VERSAO}")


if __name__ == "__main__":
    main()
