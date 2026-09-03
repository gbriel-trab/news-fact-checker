"""Gabarito: casos fixos com resposta esperada, por etapa que chama modelo.

    python -m src.gabarito premissas --dry-run   # casos, notas e custo
    python -m src.gabarito premissas --historico # antes/depois, custo zero
    python -m src.gabarito premissas             # roda a bateria (PAGA)
    python -m src.gabarito premissas --vezes 2   # cada caso N vezes
    python -m src.gabarito premissas --so C1,C8  # só estes casos
    python -m src.gabarito check --dry-run       # julgamento e estruturação
    python -m src.gabarito premissas --marcar-revisado C1,C2 --por gabriel

O teste unitário prende o TEXTO do prompt; o gabarito prende o COMPORTAMENTO
do modelo sob esse prompt. A v2 do separador (01/09/2026) passou em todos
os testes e na validação manual de três posts, e engoliu o fato da charada
Esteves–Trump no boletim seguinte: o caso não estava em lista nenhuma. A
regra daqui em diante é a mesma que vale para código — mudança de prompt só
entra se todos os casos antigos seguirem passando — e cada incidente vira
um caso no mesmo dia.

Quatro limites, para o gabarito não dar segurança falsa:

* Ele só protege o que está listado. Por isso os casos são posts REAIS do
  radar sempre que existirem (campo `bloco_radar`: o registro primário
  viaja com o caso), e cada bug novo entra aqui antes do conserto.
* Resposta esperada é escrita À MÃO. Os pares-ouro do agrupamento (ver
  ARCHITECTURE) vieram da saída do sistema e estavam contaminados; caso
  cuja resposta esperada veio de um modelo passa no próprio teste para
  sempre. `revisado_por` carrega uma ASSINATURA do conteúdo revisado:
  editar o esperado depois invalida a revisão, e o relatório avisa.
* O modelo varia entre chamadas (sob a v1, o mesmo texto trocou fato por
  opinião em 2 de 3 repetições). `temperature` não existe no Opus 5 — a
  API devolve 400 —, então a mitigação é `--vezes N`: um caso só conta
  como firme quando passa em todas.
* Caso que é exemplo literal do prompt mede REPRODUÇÃO, não regra — o
  relatório marca `[repr]` quando o corpo compartilha 8+ palavras seguidas
  com as instruções (C1, C2, C16, C17). A generalização vive nos pares
  (C1 → C14, C21, C22; C2 → C8).

Casos marcados `fronteira` são lacunas conhecidas do prompt, não
regressão: entram no relatório em linha própria e não derrubam a bateria.
Quando um deles passar a acertar de forma estável, tira-se a marca.

A bateria NÃO toca a tabela `separacoes` (cache de produção): separa sem
conexão, e grava cada vez em `gabarito_rodadas` — livro-caixa próprio, uma
linha por vez, com o que o modelo devolveu. Sem isso, uma vez que FALHOU
viraria a separação que o boletim reusa de graça no dia seguinte.

O que este módulo NÃO é: medição de rendimento (Medição 1) nem gabarito
de checador profissional (Medição 2). Aqui a pergunta é só "o que já
funcionava continua funcionando?".
"""

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from . import config

DIR_GABARITOS = Path(__file__).resolve().parent.parent / "gabaritos"

CUSTO_ESTIMADO = {"premissas": 0.012, "check": 0.012,
                  "extracao": 0.045}
"""Por caso e por vez. Medido na primeira rodada completa (03/09/2026):
premissas 50 separações por US$ 0,31 (média 0,006; a primeira chamada de
cada rodada paga a escrita do cache do prompt, ~0,02, e o post longo de
análise custa 0,02 sempre); check 19 chamadas por US$ 0,145 (média
0,008, o estruturador com nome incompleto chegou a 0,027). Arredondado
extração 0,045 — medido sobre as 347 extrações já pagas do acervo
(média 0,038, máximo 0,271; matéria longa com muita entidade é a cara).
Arredondado para cima — serve para o aviso antes de gastar, o custo real vem da
fatura."""

PALAVRAS_EXEMPLO = 8
"""Palavras seguidas em comum com o prompt a partir das quais o caso é
exemplo literal. Medido em 02/09/2026: C1 15, C2 12, C16 10, C17 10;
todos os outros ≤ 5."""


# ------------------------------------------------------------ comparação

def _normaliza(texto: str) -> str:
    """Minúsculas, sem acento e com espaço colapsado: "reune" e "reúne"
    têm de casar (o trecho é literal do post, e o autor não acentua
    sempre), e um trecho que atravessa quebra de linha tem de continuar
    sendo substring do texto."""
    sem_acento = unicodedata.normalize("NFKD", texto)
    limpo = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return " ".join(limpo.casefold().split())


def _contem(texto: str, pedaco: str) -> bool:
    """Substring em fronteira de palavra E de número: '15' não casa
    '2015' nem '15,5'; 'maio' não casa 'maior'; 'dela' não casa 'modela'.
    Pontuação depois ('Trump.') continua casando. Residual da remoção de
    acento: 'março' vira 'marco' e casa a palavra 'marco'."""
    alvo = re.escape(_normaliza(pedaco))
    padrao = rf"(?<!\w)(?<!\d[,.]){alvo}(?!\w)(?![,.]\d)"
    return re.search(padrao, _normaliza(texto)) is not None


# ------------------------------------------------------------- arquivos

def carrega(nome: str) -> list[dict]:
    caminho = DIR_GABARITOS / f"{nome}.json"
    # utf-8-sig: editado à mão no Windows, e o Bloco de Notas grava BOM.
    with open(caminho, encoding="utf-8-sig") as arquivo:
        casos = json.load(arquivo)
    ids = [c["id"] for c in casos]
    repetidos = {i for i in ids if ids.count(i) > 1}
    if repetidos:
        raise ValueError(f"id repetido em {caminho.name}: {sorted(repetidos)}")
    return casos


_CHAVES_ASSINADAS = ("texto", "afirmacao", "fatos", "esperado",
                     "proibido_em_fato", "evidencias", "cita_minimo",
                     "fronteira")


def assinatura(caso: dict) -> str:
    """Resumo do que a revisão humana conferiu. Nota e origem ficam de
    fora de propósito: prosa muda sem mudar o que é cobrado do modelo."""
    material = json.dumps({k: caso.get(k) for k in _CHAVES_ASSINADAS},
                          sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


def revisado(caso: dict) -> bool:
    """Revisão só vale assinada sobre o conteúdo ATUAL do caso — editar o
    esperado depois de revisar volta o caso a 'sem revisão'."""
    return (caso.get("revisado_por") or "").endswith("#" + assinatura(caso))


def marca_revisao(nome: str, ids: set[str], por: str) -> list[str]:
    """Carimba `revisado_por` = "<quem> <data> #<assinatura>" nos casos
    pedidos e regrava o arquivo. Devolve os ids carimbados."""
    caminho = DIR_GABARITOS / f"{nome}.json"
    casos = carrega(nome)
    faltam = ids - {c["id"] for c in casos}
    if faltam:
        raise SystemExit(f"caso(s) inexistente(s): {sorted(faltam)}")
    carimbados = []
    for caso in casos:
        if caso["id"] in ids:
            caso["revisado_por"] = (f"{por} {date.today().isoformat()} "
                                    f"#{assinatura(caso)}")
            carimbados.append(caso["id"])
    with open(caminho, "w", encoding="utf-8") as arquivo:
        json.dump(casos, arquivo, ensure_ascii=False, indent=2)
        arquivo.write("\n")
    return carimbados


def reproduz_exemplo(caso: dict, prompt: str,
                     minimo: int = PALAVRAS_EXEMPLO) -> bool:
    """O corpo do caso compartilha `minimo` palavras seguidas com o
    prompt: é exemplo literal, e passar nele prova reprodução."""
    corpo = (caso.get("texto") or caso.get("afirmacao", "")).split("\n", 1)[-1]
    alvo = _normaliza(prompt)
    palavras = _normaliza(corpo).split()
    return any(" ".join(palavras[i:i + minimo]) in alvo
               for i in range(len(palavras) - minimo + 1))


@dataclass
class Resultado:
    """O que uma rodada de um caso produziu: as falhas (vazio = passou),
    o que o modelo devolveu (para o relatório) e o custo faturado."""

    caso: str
    falhas: list[str]
    obtido: str
    custo: float
    fronteira: bool = False

    @property
    def passou(self) -> bool:
        return not self.falhas


# ------------------------------------------------------------- premissas

def valida_expectativa(caso: dict) -> list[str]:
    """Avisos sobre a EXPECTATIVA, não sobre o modelo. Puro, sem API.

    Existe porque errei três vezes no mesmo dia (03/09/2026) escrevendo
    `contem` com um pedaço que `_contem` nunca casaria: "recupera" não
    casa "recuperação" e "alário mínimo" não casa "Salário mínimo",
    porque a busca ancora em fronteira de PALAVRA — a mesma guarda que
    impede "maio" casar "maior". Nas três, o modelo tinha acertado e o
    gabarito cobrava dele um erro meu.

    A regra: todo pedaço de `contem` tem de aparecer no TEXTO do próprio
    caso. Se não aparece, ou a expectativa está mal escrita ou o caso é
    o errado — e nos dois casos o número que a bateria dá é mentira."""
    corpo = " ".join(str(x) for x in
                     [caso.get("titulo", "")] + list(caso.get("sentencas", [])))
    avisos = []
    for pedido in caso.get("medida_esperada", []):
        for campo in ("propriedade_contem", "recorte_contem"):
            pedaco = pedido.get(campo)
            if pedaco and (" " in pedaco or pedaco != pedaco.lower()):
                avisos.append(
                    f"{caso['id']}: \"{pedaco}\" ({campo}) tem espaço ou "
                    f"maiúscula — estes campos são chave em snake_case")
    for pedido in caso.get("deve_conter", []):
        for campo in ("sujeito_contem", "objeto_contem"):
            pedaco = pedido.get(campo)
            if pedaco and not _contem(corpo, pedaco):
                avisos.append(
                    f"{caso['id']}: \"{pedaco}\" ({campo}) não casa nada no "
                    f"texto do caso — expectativa quebrada, não o modelo")
    return avisos


def confere_extracao(caso: dict, triplas: list) -> list[str]:
    """Compara uma extração com o esperado do caso. Puro: sem API.

    NÃO cobra o conjunto exato de triplas. Matéria de 5 sentenças rende
    dezenas, a ordem varia, e um gabarito assim falharia por variação
    legítima — viraria ruído e seria ignorado, que é como uma barreira
    morre. Cobra o que os DEFEITOS CONHECIDOS produzem:

    * `deve_conter`: a tripla que o texto claramente afirma tem de estar.
    * `relacao_proibida`: o par existe mas com a relação errada — é a
      assinatura do fallback para `outro`, que é 20,8% do acervo.
    * `max_outro`: teto da proporção de `outro`. `outro` é a relação que
      diz "não achei relação"; muito dela é vocabulário ou prompt falhando.
    * `proibido_no_canonico`: sobrenome puro, cargo ou nome curto onde o
      acervo já usa o longo. Fragmentar entidade é o que `canonico.py`
      existe para remendar na LEITURA — e remendo não é conserto.
    * `valor_esperado`: o número que a matéria dá tem de aparecer.
    """
    falhas: list[str] = []
    if not triplas:
        return ["nenhuma tripla extraída"]

    def _txt(t, campo):
        return str(getattr(t, campo, "") or "")

    for pedido in caso.get("deve_conter", []):
        def bate(t, p=pedido):
            if not _contem(_txt(t, "sujeito_canonico"), p["sujeito_contem"]):
                return False
            if p.get("relacao") and _txt(t, "relacao") != p["relacao"]:
                return False
            if p.get("objeto_contem") and not _contem(
                    _txt(t, "objeto_canonico"), p["objeto_contem"]):
                return False
            return True
        if not any(bate(t) for t in triplas):
            falhas.append(
                f"faltou ({pedido['sujeito_contem']}, "
                f"{pedido.get('relacao', '*')}, "
                f"{pedido.get('objeto_contem', '*')})")

    for proib in caso.get("relacao_proibida", []):
        for t in triplas:
            if _txt(t, "relacao") != proib["nao_use"]:
                continue
            if not _contem(_txt(t, "sujeito_canonico"),
                           proib["sujeito_contem"]):
                continue
            if proib.get("objeto_contem") and not _contem(
                    _txt(t, "objeto_canonico"), proib["objeto_contem"]):
                continue
            falhas.append(
                f"usou '{proib['nao_use']}' em "
                f"({_txt(t, 'sujeito_canonico')[:28]}, ..., "
                f"{_txt(t, 'objeto_canonico')[:28]})")

    teto = caso.get("max_outro")
    # Proporção com denominador pequeno não mede nada: 1 `outro` em 4
    # triplas dá 25% e estoura qualquer teto razoável. Piso de 8.
    if teto is not None and len(triplas) >= 8:
        n_outro = sum(1 for t in triplas if _txt(t, "relacao") == "outro")
        proporcao = n_outro / len(triplas)
        if proporcao > teto:
            falhas.append(f"'outro' em {n_outro}/{len(triplas)} "
                          f"({proporcao:.0%}) — teto {teto:.0%}")

    for nome in caso.get("proibido_no_canonico", []):
        for t in triplas:
            for campo in ("sujeito_canonico", "objeto_canonico"):
                if _normaliza(_txt(t, campo)) == _normaliza(nome):
                    falhas.append(f"canônico proibido: \"{nome}\" "
                                  f"(em {campo})")
                    break

    for pedido in caso.get("medida_esperada", []):
        alvo = [t for t in triplas
                if getattr(t, "valor_numero", None) == pedido["valor"]]
        if not alvo:
            falhas.append(f"faltou valor {pedido['valor']:g} (medida)")
            continue
        prop = [str(getattr(t, "valor_propriedade", "") or "") for t in alvo]
        rec = [str(getattr(t, "valor_recorte", "") or "") for t in alvo]
        # Substring crua, NÃO `_contem`: propriedade e recorte são chaves
        # em snake_case, e `_` conta como caractere de palavra — a busca
        # com fronteira fazia "salario" não casar "salario_minimo", que é
        # a resposta certa. Foi o sexto tropeço no mesmo mecanismo em
        # 03/09/2026.
        if not any(_normaliza(pedido["propriedade_contem"]) in _normaliza(x)
                   for x in prop):
            falhas.append(f"valor {pedido['valor']:g} sem propriedade "
                          f"contendo \"{pedido['propriedade_contem']}\" "
                          f"(veio {prop})")
        if pedido.get("recorte_contem") and not any(
                pedido["recorte_contem"] in x for x in rec):
            falhas.append(f"valor {pedido['valor']:g} sem recorte contendo "
                          f"\"{pedido['recorte_contem']}\" (veio {rec})")

    for valor in caso.get("valor_esperado", []):
        if not any(getattr(t, "valor_numero", None) == valor
                   for t in triplas):
            falhas.append(f"faltou valor {valor:g}")
    return falhas


def confere_premissas(caso: dict, premissas: list) -> list[str]:
    """Compara uma separação com o esperado do caso. Puro: sem API.

    `premissas` são objetos com `.tipo`, `.texto`, `.trecho` e
    `.afirmacao` (a classe Premissa do separador, ou um dublê). Regras:

    * `fatos`: número EXATO de premissas tipo fato, quando presente.
    * Não-fato com `afirmacao` preenchida é falha sempre: a regra 3 manda
      omitir, e a paráfrase de opinião era 38% da saída paga (v2).
    * Regra 4, conferível sem modelo: todo trecho tem de existir no texto.
    * `esperado`: cada item {tipo, contem} precisa de uma premissa daquele
      tipo cujo TEXTO EXIBIDO contenha o pedaço — em fato, a reescrita (é
      o que vai ao check; conferir o trecho aprovaria qualquer pedaço que
      já esteja no post, mesmo que a reescrita o tenha perdido).
    * `proibido_em_fato`: nenhum fato pode conter estes pedaços — a
      guarda contra completar o que o texto não diz ("André" → "André
      Esteves", IPCA sem mês → "de agosto").
    """
    falhas: list[str] = []
    fatos = [p for p in premissas if p.tipo == "fato"]
    if caso.get("fatos") is not None and len(fatos) != caso["fatos"]:
        falhas.append(f"esperava {caso['fatos']} fato(s), veio {len(fatos)}")
    texto_norm = _normaliza(caso["texto"]) if caso.get("texto") else None
    for p in premissas:
        if p.tipo != "fato" and getattr(p, "afirmacao", None):
            falhas.append(f"[{p.tipo}] veio com reescrita paga: "
                          f"\"{p.afirmacao[:60]}\"")
        if texto_norm is not None and _normaliza(p.trecho) not in texto_norm:
            falhas.append(f"trecho não é literal do texto (regra 4): "
                          f"\"{p.trecho[:80]}\"")
    for item in caso.get("esperado", []):
        # `quem` (opcional, só faz sentido em fato): o sujeito ancorado
        # que o separador v4 emite tem de conter o pedaço — é o campo,
        # não a impressão, que o gabarito passa a medir.
        def _bate(p, item=item):
            if p.tipo != item["tipo"] or not _contem(p.texto, item["contem"]):
                return False
            if item.get("quem"):
                referente = getattr(p, "quem", None)
                return bool(referente) and _contem(referente.valor, item["quem"])
            return True
        if not any(_bate(p) for p in premissas):
            falhas.append(f"faltou [{item['tipo']}] contendo "
                          f"\"{item['contem']}\""
                          + (f" com quem \"{item['quem']}\"" if item.get("quem")
                             else ""))
    for pedaco in caso.get("proibido_em_fato", []):
        for p in fatos:
            if _contem(p.texto, pedaco):
                falhas.append(f"fato contém \"{pedaco}\" (inventado): "
                              f"\"{p.texto[:80]}\"")
    return falhas


def _mostra_premissas(premissas: list) -> str:
    if not premissas:
        return "      (nenhuma premissa)"
    linhas = []
    for p in premissas:
        extra = ""
        quem = getattr(p, "quem", None)
        if quem:
            extra = f"  «quem: {quem.valor}»"
        roteado = getattr(p, "roteado", None)
        if roteado:
            extra += f"  «roteador: {roteado}»"
        linhas.append(f"      [{p.tipo}] {p.texto[:110]}{extra}")
    return "\n".join(linhas)


def _grava_rodada(conexao, qual: str, caso: str, versao: str, vez: int,
                  r: Resultado) -> None:
    if conexao is None:
        return
    conexao.execute(
        "INSERT INTO gabarito_rodadas (qual, caso, prompt_versao, vez, "
        "passou, falhas, obtido, custo_usd, rodado_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (qual, caso, versao, vez, int(r.passou),
         json.dumps(r.falhas, ensure_ascii=False), r.obtido, r.custo,
         datetime.now(timezone.utc).isoformat()))
    conexao.commit()


def roda_extracao(casos: list[dict], vezes: int, resultados: list,
                  conexao=None, mostrar: bool = True) -> None:
    """A bateria que faltava, e é a do MAIOR prompt do sistema.

    Montada em 03/09/2026, depois de um diagnóstico externo achar, só
    lendo, três exemplos marcados `Certo:` que usam relação inexistente
    no enum fechado. Nada disso era medido: o gabarito cobria separador,
    juiz e estruturador — os três prompts pequenos — e deixava de fora os
    16 mil caracteres da extração, que é quem produz o acervo inteiro.

    A entrada de cada caso é CONGELADA (título, veículo, data e as 5
    sentenças do corte de lide) para a bateria não depender do banco nem
    da segmentação de hoje."""
    from . import extract

    for caso in casos:
        for vez in range(1, vezes + 1):
            resposta = extract.extrai(caso["titulo"], caso["veiculo"],
                                      caso.get("data_publicacao"),
                                      caso["sentencas"])
            triplas = list(resposta.dados.triplas)
            r = Resultado(caso=caso["id"],
                          falhas=confere_extracao(caso, triplas),
                          obtido=_mostra_triplas(triplas),
                          custo=resposta.uso.custo,
                          fronteira=bool(caso.get("fronteira")))
            resultados.append(r)
            _grava_rodada(conexao, "extracao", caso["id"],
                          extract.versao_prompt(), vez, r)
            _imprime_vez(caso, vez, vezes, r, mostrar)


def _mostra_triplas(triplas: list) -> str:
    if not triplas:
        return "      (nenhuma tripla)"
    linhas = []
    for t in triplas[:14]:
        valor = ""
        if getattr(t, "valor_numero", None) is not None:
            valor = f" = {t.valor_numero:g} {t.valor_unidade or ''}".rstrip()
        ctx = getattr(t, "valor_contexto", None)
        linhas.append(f"      ({t.sujeito_canonico[:26]}, {t.relacao}, "
                      f"{(t.objeto_canonico or '—')[:24]}){valor}"
                      + (f"  «{ctx[:26]}»" if ctx else ""))
    if len(triplas) > 14:
        linhas.append(f"      … e mais {len(triplas) - 14}")
    return chr(10).join(linhas)


def roda_premissas(casos: list[dict], vezes: int, resultados: list,
                   conexao=None, mostrar: bool = True) -> None:
    """Preenche `resultados` uma vez por chamada, para que uma exceção no
    meio da bateria não apague o que já foi pago e medido."""
    from . import premissas as separador

    for caso in casos:
        for vez in range(1, vezes + 1):
            # SEM conexão: nunca reusa (mede o modelo, não o banco) e
            # nunca sobrescreve a separação que o boletim gravou — ela é
            # o registro do incidente.
            analise, uso = separador.separa(caso["texto"])
            r = Resultado(caso=caso["id"],
                          falhas=confere_premissas(caso, analise.premissas),
                          obtido=_mostra_premissas(analise.premissas),
                          custo=uso.custo, fronteira=bool(caso.get("fronteira")))
            resultados.append(r)
            _imprime_vez(caso, vez, vezes, r, mostrar)
            _grava_rodada(conexao, "premissas", caso["id"],
                          separador.PROMPT_VERSAO, vez, r)


def historico_premissas(casos: list[dict], conexao) -> None:
    """Antes/depois a custo zero: a separação de PRODUÇÃO gravada sob
    cada versão de prompt, conferida com a resposta esperada de HOJE.

    É como o gabarito responde "isto já falhava na versão anterior ou é
    novo?" sem pagar — e como se confere o comparador contra dado real
    antes de gastar: sob a v1 o C2 tem de acusar o fato 'Ocorreu um
    encontro' e o C3 os quatro fatos de 'o empresário'.
    """
    from . import premissas as separador

    atual = separador.PROMPT_VERSAO
    for caso in casos:
        linhas = conexao.execute(
            "SELECT prompt_versao, separado_em, premissas_json FROM separacoes "
            "WHERE texto_hash = ? ORDER BY separado_em",
            (separador._hash_texto(caso["texto"]),)).fetchall()
        if not linhas:
            print(f"  {caso['id']:<5} (nenhuma separação de produção gravada)")
            continue
        for l in linhas:
            analise = separador.Analise.model_validate_json(l["premissas_json"])
            falhas = confere_premissas(caso, analise.premissas)
            marca = "ok   " if not falhas else "FALHA"
            versao = l["prompt_versao"] + (" (atual)" if l["prompt_versao"] == atual
                                           else "")
            print(f"  {marca} {caso['id']:<5} {versao} "
                  f"{l['separado_em'][:16]}"
                  + ("" if not falhas else " — " + "; ".join(falhas)))
            print(_mostra_premissas(analise.premissas))


# ----------------------------------------------------------------- check

def confere_julgamento(caso: dict, veredito: str, citadas: list[int],
                       total: int, retida: bool = False) -> list[str]:
    """`esperado` é o veredito; `cita_minimo` (opcional) exige que o
    modelo tenha citado ao menos N evidências VÁLIDAS — índice fora da
    lista é descartado em produção (check.verifica) e aqui é falha;
    sem_evidencia com citação viola o próprio schema do julgamento,
    EXCETO quando é confirmação retida: aí a evidência fica visível de
    propósito. `espera_retida` exige que a retenção tenha acontecido —
    é como um caso prende o freio, e não só o veredito."""
    falhas: list[str] = []
    validas = [i for i in citadas if 1 <= i <= total]
    if len(validas) != len(citadas):
        falhas.append(f"citou índice fora da lista: {citadas}")
    if veredito != caso["esperado"]:
        falhas.append(f"esperava {caso['esperado']}, veio {veredito}")
    if caso.get("espera_retida") and not retida:
        falhas.append("esperava confirmação RETIDA pelo freio, e não foi")
    if veredito == "sem_evidencia" and validas and not retida:
        falhas.append(f"sem_evidencia citando {validas}")
    minimo = caso.get("cita_minimo", 0)
    if len(validas) < minimo:
        falhas.append(f"citou {len(validas)} evidência(s), mínimo {minimo}")
    return falhas


def confere_estrutura(caso: dict, relacao: str, sujeito: str,
                      busca: str) -> list[str]:
    """`esperado` traz `relacao` (exata), `sujeito_contem` e, opcional,
    `busca_sem` — pedaços que a frase de busca NÃO pode ter (negação,
    por exemplo: o acervo guarda o fato positivo)."""
    esperado = caso["esperado"]
    falhas: list[str] = []
    if esperado.get("relacao") and relacao != esperado["relacao"]:
        falhas.append(f"relação: esperava {esperado['relacao']}, veio {relacao}")
    if esperado.get("sujeito_contem") and not _contem(
            sujeito, esperado["sujeito_contem"]):
        falhas.append(f"sujeito \"{sujeito}\" não contém "
                      f"\"{esperado['sujeito_contem']}\"")
    for pedaco in esperado.get("busca_sem", []):
        if _contem(busca, pedaco):
            falhas.append(f"busca contém \"{pedaco}\": \"{busca}\"")
    return falhas


def _achados_de(evidencias: list[dict]) -> list:
    """Evidência fixa do caso no formato que `check.julga` lê. O `meta`
    espelha o que o índice devolve; distância zero porque aqui não há
    ranking — a evidência é dada, não recuperada.

    `sujeito` e `objeto` são obrigatórios no caso porque o FREIO os lê
    (03/09/2026): sem eles a bateria media um freio cego, que retinha
    tudo — o oposto do que ela existe para medir."""
    from . import indice

    return [indice.Achado(
        texto=e["texto"], distancia=0.0,
        meta={"veiculo": e["veiculo"], "titulo": e.get("titulo", ""),
              "url": e.get("url", ""), "data_fato": e.get("data_fato", ""),
              "origem": e.get("origem", "e"),
              "sujeito": e.get("sujeito", ""), "objeto": e.get("objeto", ""),
              "valor": e.get("valor", "")})
        for e in evidencias]


def roda_check(casos: list[dict], vezes: int, resultados: list,
               conexao=None, mostrar: bool = True) -> None:
    from . import check

    versao = check.PROMPT_VERSAO
    for caso in casos:
        for vez in range(1, vezes + 1):
            if caso["tipo"] == "julgamento":
                achados = _achados_de(caso["evidencias"])
                julgamento, uso = check.julga(caso["afirmacao"], achados)
                # O MESMO freio de produção, com as mesmas entradas: o
                # sujeito e os apoios vêm do estruturador, e o caso os
                # carrega fixos (medidos pelo caso E correspondente).
                # Sem isto o gabarito media um freio que não roda.
                citadas = [achados[i - 1] for i in julgamento.evidencias
                           if 1 <= i <= len(achados)]
                julgamento = check.aplica_alinhamento(
                    julgamento, citadas,
                    caso.get("sujeitos_estruturados", []),
                    caso.get("apoios_estruturados", []))
                falhas = confere_julgamento(caso, julgamento.veredito,
                                            julgamento.evidencias,
                                            len(caso["evidencias"]),
                                            julgamento.retida)
                alinhado = " | ".join(
                    f"{a.lacuna}: {a.na_afirmacao or '—'} ⇄ "
                    f"{a.na_evidencia or '—'}"
                    for a in julgamento.alinhamento)
                obtido = (f"      {julgamento.veredito} · cita "
                          f"{julgamento.evidencias} · "
                          f"{julgamento.justificativa[:140]}\n"
                          f"      {alinhado[:300]}")
            elif caso["tipo"] == "estrutura":
                afirmacao, uso = check.estrutura(caso["afirmacao"])
                falhas = confere_estrutura(caso, afirmacao.relacao.value,
                                           afirmacao.sujeito_canonico,
                                           afirmacao.busca)
                obtido = (f"      ({afirmacao.sujeito_canonico}, "
                          f"{afirmacao.relacao.value}, "
                          f"{afirmacao.objeto_canonico or '—'}) · busca: "
                          f"\"{afirmacao.busca}\"")
            else:
                raise ValueError(f"{caso['id']}: tipo desconhecido "
                                 f"{caso['tipo']!r}")
            r = Resultado(caso=caso["id"], falhas=falhas, obtido=obtido,
                          custo=uso.custo, fronteira=bool(caso.get("fronteira")))
            resultados.append(r)
            _imprime_vez(caso, vez, vezes, r, mostrar)
            _grava_rodada(conexao, "check", caso["id"], versao, vez, r)


# ------------------------------------------------------------- relatório

def _imprime_vez(caso: dict, vez: int, vezes: int, r: Resultado,
                 mostrar: bool = True) -> None:
    marca = "ok   " if r.passou else ("front" if r.fronteira else "FALHA")
    sufixo = f" ({vez}/{vezes})" if vezes > 1 else ""
    print(f"  {marca} {caso['id']:<5}{sufixo} US$ {r.custo:.4f}"
          + ("" if r.passou else " — " + "; ".join(r.falhas)))
    # O obtido sai também quando passa: várias notas mandam ler a saída
    # à mão (sujeito canônico completado, 'ontem' cru na reescrita).
    if not r.passou or mostrar:
        print(r.obtido)


def resume(resultados: list[Resultado],
           casos: list[dict]) -> tuple[int, int, int, list[str]]:
    """(regressões, fronteiras que falharam, sem revisão, instáveis).

    REGRESSÃO é caso não-fronteira que falhou em TODAS as vezes. Caso que
    falhou em algumas e passou em outras é INSTÁVEL, e a distinção nasceu
    de um caso concreto (03/09/2026): C13 e C25 apareceram como regressão
    numa bateria de uma passada e, repetidos três vezes, deram 2/3 e 3/3
    — o prompt não tinha mudado, só o roteador em código.

    Chamar variância de regressão é falso positivo dentro da ferramenta
    que existe para evitar falso positivo. E é o erro caro nas duas
    direções: bloqueia mudança boa, e ensina a ignorar a bateria.

    Instável não é aprovação: aparece no relatório com a contagem, porque
    caso que só passa às vezes é caso que o prompt não determina — só não
    é motivo para barrar um commit que não mexeu no prompt."""
    por_caso: dict[str, list[Resultado]] = {}
    for r in resultados:
        por_caso.setdefault(r.caso, []).append(r)
    regressoes = sum(1 for rs in por_caso.values()
                     if not rs[0].fronteira and all(not r.passou for r in rs))
    # Fronteira segue com `any`: ela não derrubava a bateria antes nem
    # depois, então contar variância aqui não custa falso positivo — e
    # com `all` a fronteira que passa às vezes sumia do relatório
    # inteiro, porque `instaveis` exclui fronteira. Lacuna conhecida que
    # some do relatório é lacuna que se esquece.
    fronteiras = sum(1 for rs in por_caso.values()
                     if rs[0].fronteira and any(not r.passou for r in rs))
    instaveis = sorted(
        f"{caso} ({sum(r.passou for r in rs)}/{len(rs)})"
        for caso, rs in por_caso.items()
        if not rs[0].fronteira and any(r.passou for r in rs)
        and any(not r.passou for r in rs))
    sem_revisao = sum(1 for c in casos if not revisado(c))
    return regressoes, fronteiras, sem_revisao, instaveis


def _filtra(casos: list[dict], so: str | None) -> list[dict]:
    if not so:
        return casos
    querido = {s.strip() for s in so.split(",") if s.strip()}
    achados = [c for c in casos if c["id"] in querido]
    faltam = querido - {c["id"] for c in achados}
    if faltam:
        raise SystemExit(f"caso(s) inexistente(s): {sorted(faltam)}")
    return achados


def _ao_menos_um(valor: str) -> int:
    # `--vezes 0` rodaria nada, resumiria zero regressões e sairia verde:
    # o pior resultado possível para o que decide se um prompt entra.
    n = int(valor)
    if n < 1:
        raise argparse.ArgumentTypeError("--vezes precisa ser >= 1")
    return n


def _prompt_de(qual: str) -> str:
    if qual == "extracao":
        from . import extract
        return extract.INSTRUCOES
    if qual == "premissas":
        from . import premissas
        return premissas.INSTRUCOES
    from . import check
    return check.INSTRUCOES_JULGAMENTO + "\n" + check.INSTRUCOES_ESTRUTURA


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Gabarito: o que já funcionava continua funcionando?")
    parser.add_argument("qual",
                        choices=("premissas", "check", "extracao"))
    parser.add_argument("--vezes", type=_ao_menos_um, default=1,
                        help="repetições por caso (variância do modelo)")
    parser.add_argument("--so", help="ids separados por vírgula")
    parser.add_argument("--dry-run", action="store_true",
                        help="lista casos, notas e custo estimado, sem chamar")
    parser.add_argument("--historico", action="store_true",
                        help="premissas: separações de produção gravadas, "
                             "por versão, contra o esperado de hoje (grátis)")
    parser.add_argument("--quieto", action="store_true",
                        help="não imprime a saída do modelo quando passa")
    parser.add_argument("--marcar-revisado", metavar="IDS",
                        help="carimba revisado_por nos casos (exige --por)")
    parser.add_argument("--por", help="quem revisou, para --marcar-revisado")
    args = parser.parse_args()

    if args.marcar_revisado:
        if not args.por:
            raise SystemExit("--marcar-revisado exige --por <nome>")
        ids = {s.strip() for s in args.marcar_revisado.split(",") if s.strip()}
        feitos = marca_revisao(args.qual, ids, args.por)
        print(f"revisado_por carimbado em: {', '.join(feitos)}")
        return

    casos = _filtra(carrega(args.qual), args.so)
    if not casos:
        raise SystemExit("nenhum caso selecionado")
    prompt = _prompt_de(args.qual)
    repr_ = [c["id"] for c in casos if reproduz_exemplo(c, prompt)]
    quebradas = [m for c in casos for m in valida_expectativa(c)]
    if quebradas:
        print("EXPECTATIVAS QUEBRADAS — conserte antes de gastar:")
        for m in quebradas:
            print(f"  {m}")
        sys.exit(3)
    estimado = len(casos) * args.vezes * CUSTO_ESTIMADO[args.qual]
    print(f"GABARITO {args.qual} · {len(casos)} caso(s) × {args.vezes} vez(es)"
          f" · estimado ~US$ {estimado:.2f}\n")

    if args.dry_run:
        for c in casos:
            marcas = ("[fronteira]" if c.get("fronteira") else "") + (
                "[repr]" if c["id"] in repr_ else "")
            estado = "revisado" if revisado(c) else "SEM REVISÃO"
            resumo = (c.get("texto") or c.get("afirmacao", "")).replace("\n", " / ")
            print(f"  {c['id']:<5}{marcas:<17}({estado}) {resumo[:80]}")
            print(f"        nota: {c['nota'][:200]}")
        print("\nNada foi enviado. Remova --dry-run para rodar.")
        return

    from .storage import conecta
    conexao = conecta(config.BANCO)
    try:
        if args.historico:
            if args.qual != "premissas":
                raise SystemExit("--historico só existe para premissas "
                                 "(o check não grava a evidência que julgou)")
            historico_premissas(casos, conexao)
            return
        resultados: list[Resultado] = []
        incompleto = False
        try:
            if args.qual == "extracao":
                roda_extracao(casos, args.vezes, resultados, conexao,
                              mostrar=not args.quieto)
            elif args.qual == "premissas":
                roda_premissas(casos, args.vezes, resultados, conexao,
                               mostrar=not args.quieto)
            else:
                roda_check(casos, args.vezes, resultados, conexao,
                           mostrar=not args.quieto)
        except (KeyboardInterrupt, Exception) as erro:  # noqa: BLE001
            # O que já foi pago e medido não some com a exceção.
            incompleto = True
            print(f"\nINTERROMPIDO: {type(erro).__name__}: {erro}")
    finally:
        conexao.close()

    regressoes, fronteiras, sem_revisao, instaveis = resume(resultados,
                                                            casos)
    custo = sum(r.custo for r in resultados)
    print(f"\n{len(casos)} caso(s) · {regressoes} regressão(ões) · "
          f"{fronteiras} fronteira(s) falhando · custo real US$ {custo:.4f}")
    if instaveis:
        print(f"{len(instaveis)} caso(s) INSTÁVEIS (passam às vezes): "
              f"{', '.join(instaveis)}. Não barram a bateria, mas são caso "
              "que o prompt não determina — rode com --vezes antes de "
              "concluir qualquer coisa sobre eles.")
    if repr_:
        print(f"{len(repr_)} caso(s) são exemplo literal do prompt e medem "
              f"reprodução, não regra: {', '.join(repr_)}")
    if sem_revisao:
        print(f"ATENÇÃO: {sem_revisao} caso(s) sem revisão humana válida da "
              "resposta esperada — até lá, o gabarito pode estar cobrando do "
              "modelo um erro seu. Use --marcar-revisado IDS --por NOME.")
    if incompleto:
        feitos = {r.caso for r in resultados}
        faltam = [c["id"] for c in casos if c["id"] not in feitos]
        print("Bateria incompleta; faltam: " + ", ".join(faltam))
        sys.exit(2)
    if regressoes:
        print("Regressão: a mudança de prompt NÃO deve entrar até isto passar.")
        sys.exit(1)


if __name__ == "__main__":
    main()
