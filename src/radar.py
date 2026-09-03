"""Radar de rede social: o que os handles acompanhados estão alegando.

    python -m src.radar                     # posts recentes dos handles
    python -m src.radar --dias 5            # janela maior
    python -m src.radar --conferir 2        # captura e confere o post 2
    python -m src.radar --dry-run           # mostra o que seria enviado

O papel está fixado no ARCHITECTURE.md: rede social é RADAR, nunca evidência.
O post indica onde olhar; a evidência vem sempre da imprensa ou da
instituição. Nada do que este módulo captura entra no acervo.

Duas honestidades que a saída carrega sempre:

* O texto exibido é TRANSCRIÇÃO DE MODELO (o Grok busca e transcreve), não
  registro primário — cada post sai com o link do status para conferência.
  Testado em 30/08/2026: pedindo transcrição, o post volta na íntegra; mas
  a fidelidade é auditável no link, não garantida pela API.
* Conferir premissas de um post é CONFERÊNCIA, nunca placar do autor.
  Premissa sem evidência = o acervo não cobre, não "o autor errou".

Custo: uma busca custa centavos (~US$ 0,03 medido). O preço vem no rodapé
de toda rodada, convertido de `cost_in_usd_ticks` (tick = 1e-10 USD,
conferido contra o console da xAI em 30/08/2026).
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from . import config

URL_API = "https://api.x.ai/v1/responses"
MODELO = "grok-4.6"
TICK_USD = 1e-10
TIMEOUT = 600
"""Leitura da busca. Era 180 e foi medido em 03/09/2026, depois de TRÊS
boletins seguidos morrerem em ReadTimeout: a API estava NO AR (GET
/v1/models em 0,3s, chat sem busca em 1,5s) e a mesma busca, com UM
handle e UM dia, voltou HTTP 200 em 107s — seis chamadas de x_search do
lado do servidor. A rodada real tem mais handles e mais dias, então
passava dos 180s sempre.

O diagnóstico errado custou o dia: eu li o timeout como "a xAI caiu" e
escrevi isso três vezes. Timeout do CLIENTE não é queda do servidor —
prova de indisponibilidade é o servidor responder erro, não o cliente
desistir. 600s é folga de 5,6x sobre a medição de um handle."""

TENTATIVAS = 3
ESPERA = 45
TETO_ESPERA = 300
"""Repetição da busca, e o motivo dela: em 03/09/2026 a xAI estourou os
180s de leitura às 12:00 e o boletim do dia inteiro morreu ali — uma
tentativa só, sem repetição, e a próxima chance 24h depois. Timeout e
erro de servidor (5xx, 429) são passageiros por definição; a espera
cresce a cada tentativa para não insistir em cima de uma API que já está
lenta. Erro de requisição (4xx que não 429) NÃO repete: parâmetro
inválido ou falta de crédito não melhoram na segunda vez, e repetir só
atrasaria o aviso.

A espera saiu de 20 para 45 segundos na mesma tarde: a rodada manual das
12:06 levou 429 "at capacity, try again in a few minutes". 45 e 90
somam os poucos minutos que a própria xAI pede, e é o que um trabalho
diário pode esperar sem virar processo pendurado."""

_DELIM = re.compile(r"^POST\s+(\d+)", re.MULTILINE)


class FalhaNoRadar(Exception):
    """A busca não pôde ser feita ou a resposta não pôde ser lida."""


@dataclass(frozen=True, slots=True)
class Rodada:
    """O que uma busca devolveu: posts, avisos do modelo, links e custo."""

    posts: tuple[str, ...]
    notas: tuple[str, ...]
    links: tuple[str, ...]
    custo_usd: float
    bruto: str
    detalhe_custo: str = ""
    """De onde o custo veio (tokens e chamadas de busca), legível.

    Existe porque o custo por rodada triplicou quando o formato passou a
    exigir URL e contexto de thread (0,03 → 0,11-0,25, medido em
    01/09/2026) e só o total não diz qual alavanca puxar."""


def _prompt(handles: tuple[str, ...], dias: int) -> str:
    # Os handles vão NOMEADOS no texto, além do filtro allowed_x_handles:
    # medido em 30/08/2026, o modelo não enxerga a configuração da
    # ferramenta — só o filtro restringe, só o prompt direciona.
    nomes = ", ".join(f"@{h}" for h in handles)
    return (
        f"Busque os posts dos últimos {dias} dias de: {nomes}. "
        "TRANSCREVA cada um na ÍNTEGRA, sem resumir, sem parafrasear e sem "
        "comentar. Formato obrigatório, um bloco por post:\n"
        "POST N (@handle, data):\n"
        "URL: <link do PRÓPRIO post transcrito, x.com/.../status/...>\n"
        "EM RESPOSTA A (@autor, <link do post respondido>): <texto do "
        "post respondido — inclua esta linha SOMENTE se o post for uma "
        "resposta; senão, omita>\n"
        "CITANDO (@autor): <texto do post citado/quotado — inclua esta "
        "linha SOMENTE se o post cita outro post; senão, omita>\n"
        "<texto literal>\n---\n"
        "A linha URL de cada bloco tem de apontar para o post transcrito "
        "NAQUELE bloco, nunca para outro; e o link em EM RESPOSTA A, para "
        "o post respondido. Traga TUDO que encontrar — post, quote e "
        "resposta —, sem filtrar: quem descarta é o programa. "
        "Se um handle não retornar nada, diga qual, numa linha à parte."
    )


def _corpo(handles: tuple[str, ...], dias: int) -> dict:
    hoje = datetime.now(timezone.utc).date()
    return {
        "model": MODELO,
        "tools": [{
            "type": "x_search",
            "allowed_x_handles": list(handles),
            "from_date": (hoje - timedelta(days=dias)).isoformat(),
            # A doc diz "including both dates", mas o limite superior é a
            # MEIA-NOITE UTC do to_date, medido em 31/08 e 01/09/2026:
            # três buscas, 43 posts lidos, os mais novos às 23:19 e 23:54
            # da véspera e ZERO do dia corrente — com posts do dia
            # existindo. Com to_date=hoje, a busca nunca via o próprio
            # dia; amanhã é o que faz "hoje até agora" entrar.
            "to_date": (hoje + timedelta(days=1)).isoformat(),
        }],
        "input": _prompt(handles, dias),
    }


def _handles_de(argumento: str) -> tuple[str, ...]:
    """Normaliza ANTES de filtrar: '@' sozinho vira vazio e cai fora.

    Na ordem inversa, '@' sobrevivia ao filtro, virava handle vazio depois
    do lstrip, e disparava uma busca paga com `allowed_x_handles=[""]` —
    o guard de lista vazia via um tuple de um elemento e não protegia nada.
    """
    return tuple(x for x in
                 (h.strip().lstrip("@").strip()
                  for h in argumento.split(","))
                 if x)


def _textos_de(objeto) -> list[str]:
    """Todo output_text da resposta, em qualquer nível do aninhamento."""
    achados: list[str] = []
    if isinstance(objeto, dict):
        if objeto.get("type") == "output_text" and "text" in objeto:
            achados.append(objeto["text"])
        for valor in objeto.values():
            achados.extend(_textos_de(valor))
    elif isinstance(objeto, list):
        for valor in objeto:
            achados.extend(_textos_de(valor))
    return achados


def _links_de(bruto: str) -> tuple[str, ...]:
    """URLs de status individuais citadas na resposta, deduplicadas.

    Vêm nas anotações inline, não num campo `citations` — medido em
    30/08/2026. Regex sobre o JSON serializado é deliberado: o formato das
    anotações não é documentado, e campo que muda de lugar não pode
    derrubar a captura.

    Este conjunto NÃO tem ordem que corresponda aos posts: as anotações
    chegam com start/end zerados (medido em 01/09/2026), então não existe
    pareamento estrutural link↔post. Numerar estes links como se casassem
    com a numeração dos posts foi o defeito do boletim de 31/08. O
    pareamento é pedido ao modelo (linha URL: de cada bloco) e conferido
    contra este conjunto por `url_do_post`.
    """
    urls = re.findall(r"https://x\.com/[\w./]*status/\d+", bruto)
    vistos: dict[str, None] = dict.fromkeys(urls)
    return tuple(vistos)


def _citacoes_de(objeto) -> tuple[str, ...]:
    """URLs de status nas anotações `url_citation` — o conjunto do
    SERVIDOR, imune ao texto do modelo.

    Existe porque o regex sobre o JSON inteiro (`_links_de`) também pesca
    URLs escritas pelo próprio modelo — e no teste de 01/09/2026 o texto
    trazia duas URLs sem anotação correspondente (IDs sequenciais de
    2024, prováveis invenções). Validar a linha URL: contra um conjunto
    que contém o texto do modelo deixaria a alucinação validar a si
    mesma. Quando não há anotação nenhuma, `busca` cai no regex — captura
    frouxa é melhor que nenhuma, mas aí sem valor de validação.
    """
    achados: list[str] = []
    if isinstance(objeto, dict):
        if (objeto.get("type") == "url_citation"
                and re.search(r"x\.com/[\w./]*status/\d+",
                              str(objeto.get("url", "")))):
            achados.append(objeto["url"])
        for valor in objeto.values():
            achados.extend(_citacoes_de(valor))
    elif isinstance(objeto, list):
        for valor in objeto:
            achados.extend(_citacoes_de(valor))
    return tuple(dict.fromkeys(achados))


_RE_URL_BLOCO = re.compile(
    r"^\s*URL:\s*(https://x\.com/[\w./]*status/(\d+))", re.MULTILINE)
_RE_LINHA_URL = re.compile(r"^\s*URL:[^\n]*\n?", re.MULTILINE | re.IGNORECASE)
_RE_RESPOSTA_CAPT = re.compile(r"^\s*EM RESPOSTA A\s*([^\n]*)$",
                               re.MULTILINE | re.IGNORECASE)
_RE_CITANDO_CAPT = re.compile(r"^\s*CITANDO\s*([^\n]*)$",
                              re.MULTILINE | re.IGNORECASE)


def para_separacao(bloco: str) -> str:
    """O bloco como o separador de premissas deve vê-lo.

    A linha URL: sai (ruído de tokens); as linhas EM RESPOSTA A e CITANDO
    viram contexto com a ATRIBUIÇÃO certa. Desde 01/09/2026 a captura
    exclui resposta a terceiros (decisão do usuário: neste domínio a
    substância vive em post, quote e thread própria), então EM RESPOSTA A
    normalmente aponta o post ANTERIOR DA PRÓPRIA THREAD — palavras do
    mesmo autor, e rotulá-las de "interlocutor" poria premissa legítima
    sob suspeita. A comparação de handle decide: mesmo handle do
    cabeçalho → contexto do próprio autor; outro handle (o modelo
    desobedeceu a exclusão, ou é quote) → reatribuído a quem falou.
    (Quando o autor reescreve o citado no próprio corpo — caso RIOT —
    a linha nem aparece; a atribuição das aspas é problema do separador.)
    """
    m_cab = re.match(r"^POST\s+\d+\s*\((@\w+)", bloco)
    handle_autor = (m_cab.group(1).lower() if m_cab else "")

    def _rotula_resposta(m: re.Match) -> str:
        conteudo = m.group(1).strip()
        m_quem = re.match(r"\((@\w+)", conteudo)
        quem = m_quem.group(1).lower() if m_quem else ""
        if quem and quem == handle_autor:
            return ("(contexto — post anterior do próprio autor na "
                    f"thread: {conteudo})")
        return ("(contexto — palavras do interlocutor, não do autor "
                f"do post: {conteudo})")

    sem_url = _RE_LINHA_URL.sub("", bloco)
    com_resposta = _RE_RESPOSTA_CAPT.sub(_rotula_resposta, sem_url)
    return _RE_CITANDO_CAPT.sub(
        lambda m: ("(contexto — post citado pelo autor; as afirmações são "
                   f"de quem ele cita: {m.group(1).strip()})"), com_resposta)


def id_status(url: str) -> str | None:
    """O número do status numa URL do X, ou None se não houver."""
    m = re.search(r"status/(\d+)", url)
    return m.group(1) if m else None


def url_do_post(bloco: str, citados: tuple[str, ...]) -> tuple[str | None, bool]:
    """(URL que o bloco alega, se ela confere com as citações da busca).

    A linha URL: é escrita pelo MODELO; as citações são anexadas pelo
    SERVIDOR com o que a ferramenta de busca de fato leu. URL alegada que
    não está entre as citações é alegação sem lastro — sai como (url,
    False) e quem consome decide o aviso. A comparação é por ID do status
    porque o mesmo post aparece como x.com/i/status/N nas anotações e
    x.com/handle/status/N no texto do modelo.
    """
    m = _RE_URL_BLOCO.search(bloco)
    if not m:
        return None, False
    ids_citados = {id_status(u) for u in citados}
    return m.group(1), m.group(2) in ids_citados


def _limpa(pedaco: str) -> str:
    return pedaco.strip().strip("-").strip()


def resposta_a_terceiro(bloco: str, handles: tuple[str, ...]) -> str | None:
    """O handle a quem este bloco responde, se NÃO for um dos monitorados.

    Devolve None quando o bloco é post próprio, quote, ou continuação de
    thread do próprio autor — os três que o projeto quer.

    Existe porque o PROMPT não segurou. Ele diz, desde 01/09/2026, "NÃO
    TRANSCREVA respostas a outros usuários — ignore-as por completo", e
    medido no acervo em 03/09/2026 o modelo transcreveu assim mesmo: das
    14 entradas do dia 31/08, 3 eram posts e 11 eram respostas, quase
    todas ao @grok e várias sem uma palavra do autor no corpo. O usuário
    conferiu na aba Respostas do X e o número batia — o que não batia era
    a regra sendo obedecida.

    Prompt é pedido, código é barreira. Esta é a barreira, e ela usa a
    mesma comparação de handle que `para_separacao` já fazia para decidir
    atribuição — só que agora para DESCARTAR, e antes de pagar
    separação e check por cada uma."""
    m_resp = _RE_RESPOSTA_CAPT.search(bloco)
    if not m_resp:
        return None
    # Sem exigir o parentese de fechamento: `boletim_posts.resumo` guarda
    # `resumo[:120]`, entao o handle pode chegar CORTADO ("@corte",
    # "@vuvie"). Exigir o ")" fazia a funcao falhar ABERTO justamente no
    # dado truncado — e falhar aberto aqui e deixar passar o que se quer
    # barrar.
    m_quem = re.search(r"\(@(\w+)", m_resp.group(0))
    if not m_quem:
        return None
    quem = m_quem.group(1).lower()
    proprios = {h.lower().lstrip("@") for h in handles}
    m_cab = re.match(r"^POST\s+\d+\s*\(@(\w+)", bloco)
    if m_cab:
        proprios.add(m_cab.group(1).lower())
    # Prefixo nos DOIS sentidos, por causa do corte: "@perfil_t" e o
    # proprio autor truncado e tem de FICAR; "@grok" nao e prefixo de
    # ninguem monitorado e SAI. Comparar por igualdade crua descartaria a
    # thread propria truncada — o caso vizinho que o C25 depende.
    if any(quem.startswith(p) or p.startswith(quem) for p in proprios):
        return None
    return "@" + quem


def _id_proprio(bloco: str) -> str | None:
    """O status ID do post transcrito NESTE bloco (a linha URL)."""
    m = _RE_LINHA_URL.search(bloco)
    return id_status(m.group(0)) if m else None


def _id_do_pai(bloco: str) -> str | None:
    """O status ID do post RESPONDIDO, se o modelo tiver dado o link."""
    m = _RE_RESPOSTA_CAPT.search(bloco)
    return id_status(m.group(0)) if m else None


def filtra_respostas(posts, handles) -> tuple[tuple, list]:
    """Descarta resposta a terceiro, SEGUINDO A CADEIA. (fica, descartado)

    Três camadas, da mais confiável para a menos:

    1. ID DO PAI. Se o link do post respondido aponta para um status que
       NÃO é de nenhum post do próprio autor nesta rodada, é resposta a
       terceiro — não importa o handle que o modelo escreveu. Esta camada
       existe porque o modelo MENTE: em 03/09/2026 ele deu
       `EM RESPOSTA A (@perfil_teste)` para "Vaza… furazoio", que é
       resposta ao @streetmanwtf; apontou a RAIZ da thread como pai.
    2. CADEIA. Filho de bloco descartado cai junto. É o caso que o ID do
       pai sozinho não pega: o autor responde a SI MESMO dentro de uma
       resposta a terceiro, e o pai imediato é legítimo. Itera até
       estabilizar.
    3. HANDLE. Quando não há link (modelo antigo, ou dado truncado), cai
       na comparação de handle de `resposta_a_terceiro`.

    O que FICA: post próprio, quote, e continuação de thread própria — o
    autor respondendo a si mesmo, que é o caso do C25 e o único tipo de
    resposta que o dono do projeto quer ver."""
    meus = {i for i in (_id_proprio(b) for b in posts) if i}
    fora: dict[int, str] = {}
    for k, bloco in enumerate(posts):
        alvo = resposta_a_terceiro(bloco, handles)
        pai = _id_do_pai(bloco)
        if alvo:
            fora[k] = alvo
        elif pai and pai not in meus:
            # Tem link de pai e o pai NÃO é post meu: terceiro, e este é
            # o sinal que o handle mentiroso não derruba.
            fora[k] = "pai fora da conta do autor"
    # Cadeia: enquanto alguém novo cair, quem responde a ele cai também.
    mudou = True
    while mudou:
        mudou = False
        ids_fora = {_id_proprio(posts[k]) for k in fora}
        for k, bloco in enumerate(posts):
            if k in fora:
                continue
            pai = _id_do_pai(bloco)
            if pai and pai in ids_fora:
                fora[k] = "responde a bloco já descartado (cadeia)"
                mudou = True
    ficam = tuple(b for k, b in enumerate(posts) if k not in fora)
    return ficam, [(posts[k], m) for k, m in sorted(fora.items())]


def _posts_de(texto: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Separa os blocos POST N do resto. Devolve (posts, notas).

    NOTAS são o que o modelo escreveu fora dos blocos — tipicamente o
    aviso "handle X não retornou nada", que o próprio prompt pede numa
    linha à parte. Descartá-las faria um handle sumir da rodada em
    silêncio; fundi-las ao último post mandaria comentário de modelo para
    o `premissas` como se fosse texto do autor. Nenhum caractere da
    resposta é jogado fora sem aparecer.

    Sem marcador nenhum, o texto inteiro vira um post único — resposta
    fora do formato não é descartada, é mostrada como veio.
    """
    if not _DELIM.search(texto):
        limpo = texto.strip()
        return ((limpo,) if limpo else ()), ()

    posicoes = [m.start() for m in _DELIM.finditer(texto)]
    notas: list[str] = []
    preambulo = _limpa(texto[:posicoes[0]])
    if preambulo:
        notas.append(preambulo)

    posts: list[str] = []
    for inicio, fim in zip(posicoes, posicoes[1:] + [len(texto)]):
        corpo, _, resto = texto[inicio:fim].partition("---")
        if bloco := _limpa(corpo):
            posts.append(bloco)
        # O que sobra depois do delimitador e antes do próximo POST é
        # comentário do modelo, não texto do autor.
        if sobra := _limpa(resto):
            notas.append(sobra)
    return tuple(posts), tuple(notas)


def _pede(chave: str, handles: tuple[str, ...], dias: int,
          dormir=time.sleep) -> dict:
    """A chamada à xAI, com repetição no que é passageiro.

    Devolve o JSON já decodificado. `dormir` existe para o teste não
    esperar de verdade."""
    ultimo = ""
    resposta_atual = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            resposta = resposta_atual = requests.post(
                URL_API,
                headers={"Authorization": f"Bearer {chave}",
                         "Content-Type": "application/json"},
                json=_corpo(handles, dias), timeout=TIMEOUT)
            if resposta.status_code >= 400:
                # O corpo carrega o motivo real (modelo inexistente, sem
                # crédito, parâmetro inválido); só o código não diz nada.
                ultimo = (f"xAI respondeu {resposta.status_code}: "
                          f"{resposta.text[:300]}")
                if resposta.status_code < 500 and resposta.status_code != 429:
                    raise FalhaNoRadar(ultimo)
            else:
                # JSONDecodeError do requests é RequestException, e corpo
                # 200 que não é JSON também é "resposta ilegível" — cai no
                # except abaixo e vira mais uma tentativa.
                dados = resposta.json()
                # E corpo 200 com JSON VÁLIDO mas fora do formato (uma
                # lista, um null, um JSON de erro de proxy) escapava daqui
                # limpo e estourava AttributeError lá na frente, onde o
                # boletim só captura FalhaNoRadar — morte silenciosa, com
                # o log guardando só o cabeçalho do dia. O contrato se
                # fecha aqui, no mesmo lugar onde a repetição já mora.
                if not isinstance(dados, dict):
                    raise requests.exceptions.InvalidJSONError(
                        f"corpo 200 não é objeto JSON: {type(dados).__name__}")
                return dados
        except requests.RequestException as erro:
            ultimo = f"busca na xAI falhou: {type(erro).__name__}: {erro}"
            resposta_atual = None
        if tentativa < TENTATIVAS:
            espera = _quanto_esperar(tentativa, resposta_atual)
            print(f"  {ultimo} — tentativa {tentativa}/{TENTATIVAS}, "
                  f"repetindo em {espera}s")
            dormir(espera)
    # O timeout de LEITURA não prova que a busca não foi cobrada: o cliente
    # desistiu, o servidor pode ter rodado o x_search inteiro. O livro-caixa
    # do projeto cobre a Anthropic, não a xAI — então a mensagem diz o que
    # se sabe e o que não se sabe, em vez de deixar o gasto invisível.
    raise FalhaNoRadar(
        f"{ultimo} (após {TENTATIVAS} tentativas). Timeout não garante que a "
        f"busca deixou de ser cobrada — conferir o console da xAI.")


def _quanto_esperar(tentativa: int, resposta) -> int:
    """A espera da próxima tentativa: a nossa, ou a que o servidor pediu.

    Medido em 03/09/2026: a xAI responde 429 dizendo "try again in a few
    minutes", e as três tentativas cabiam inteiras dentro desse bloqueio —
    45 + 90 = 2min15. Quando vier `Retry-After`, ele manda; o teto existe
    para um valor grande não pendurar a tarefa agendada."""
    espera = ESPERA * tentativa
    try:
        pedida = int((resposta.headers or {}).get("Retry-After", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        pedida = 0
    if pedida > 0:
        print(f"  (a xAI pediu Retry-After: {pedida}s)")
    return min(max(espera, pedida), TETO_ESPERA)


def busca(handles: tuple[str, ...], dias: int = 2) -> Rodada:
    chave = os.environ.get("XAI_API_KEY", "")
    if not chave:
        raise FalhaNoRadar(
            "XAI_API_KEY ausente no .env — o radar é o único módulo que "
            "usa a xAI, e é opcional. Ver .env.example.")
    dados = _pede(chave, handles, dias)

    bruto = json.dumps(dados, ensure_ascii=False)
    texto = "\n".join(_textos_de(dados.get("output", dados)))
    posts, notas = _posts_de(texto)
    # A BARREIRA. O prompt pede para nao transcrever resposta a terceiro
    # e o modelo transcreve assim mesmo; aqui elas sao descartadas ANTES
    # de custar separacao, check e demanda. O descarte e CONTADO e vai
    # para as notas: descarte silencioso e o que esconde defeito.
    posts, descartadas = filtra_respostas(posts, handles)
    if descartadas:
        motivos = ", ".join(sorted({m for _, m in descartadas}))
        notas = tuple(notas) + (
            f"{len(descartadas)} resposta(s) a terceiro descartada(s) "
            f"antes de custar: {motivos}",)
    uso = dados.get("usage", {})
    ticks = uso.get("cost_in_usd_ticks", 0)
    buscas = sum(1 for item in dados.get("output", [])
                 if isinstance(item, dict)
                 and "search" in str(item.get("type", "")))
    partes = [f"{chave.replace('_tokens', '')} {uso[chave]:,}"
              for chave in ("input_tokens", "output_tokens",
                            "reasoning_tokens")
              if isinstance(uso.get(chave), int)]
    if buscas:
        partes.append(f"{buscas} chamada(s) de busca")
    return Rodada(
        posts=posts,
        notas=notas,
        links=_citacoes_de(dados) or _links_de(bruto),
        custo_usd=ticks * TICK_USD,
        bruto=bruto,
        detalhe_custo=" · ".join(partes),
    )


def _confere(post: str, custo_busca: float) -> None:
    """Separa as premissas do post e julga cada uma, no rito do premissas.

    O rito importa tanto quanto o resultado, e é o mesmo do
    `premissas.main`: acervo vazio aborta ANTES de pagar verificação;
    previsão e opinião saem nomeadas pelo que são, nunca como descarte; o
    trecho literal aparece antes de cada veredito (é o elo auditável entre
    o que o autor escreveu e o que foi conferido); e o fecho impede a
    leitura de placar.
    """
    from . import check, grafo, premissas
    from .storage import conecta

    conexao = conecta(config.BANCO)
    acervo = grafo.carrega(conexao)
    if not acervo:
        print("Acervo vazio. Rode a coleta, a extração e o índice antes "
              "de conferir — verificar contra o nada só gasta.")
        conexao.close()
        sys.exit(1)

    analise, uso = premissas.separa(para_separacao(post), conexao=conexao)
    fatos = [p for p in analise.premissas if p.tipo == "fato"]
    resto = [p for p in analise.premissas if p.tipo != "fato"]

    if resto:
        print("NÃO VERIFICÁVEL — e não deve ser")
        for p in resto:
            print(f"  [{p.tipo}] {p.texto}{premissas.anotacao(p)}")
        print()

    if not fatos:
        print("Nenhuma premissa verificável no post.")
    else:
        for i, p in enumerate(fatos, 1):
            print(f"[{i}/{len(fatos)}] no post: \"{p.trecho[:110]}\"")
            check.verifica(p.texto, conexao=conexao, acervo=acervo)
    conexao.close()

    print("\nIsto confere premissas contra o acervo, não avalia o autor.")
    print("Premissa sem evidência significa que os veículos coletados não")
    print("cobrem o assunto — não que a afirmação seja falsa.")
    print(f"\n  separação: US$ {uso.custo:.4f} · mais uma verificação por "
          f"premissa · busca: US$ {custo_busca:.4f}")


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Radar: o que os handles acompanhados estão alegando.")
    parser.add_argument("--handles",
                        help="lista separada por vírgula; sem isto, usa "
                             "config.HANDLES_RADAR")
    parser.add_argument("--dias", type=int, default=2,
                        help="janela da busca (padrão: 2)")
    parser.add_argument("--conferir", type=int, metavar="N",
                        help="separa as premissas do post N e confere cada "
                             "uma contra o acervo (mais chamadas pagas)")
    parser.add_argument("--dry-run", action="store_true",
                        help="mostra a requisição, sem chamar a API")
    args = parser.parse_args()

    handles = (_handles_de(args.handles) if args.handles
               else config.HANDLES_RADAR)
    if not handles:
        print("Nenhum handle válido. Ver HANDLES_RADAR em config.py.")
        sys.exit(1)

    if args.dry_run:
        print(json.dumps(_corpo(handles, args.dias), indent=2,
                         ensure_ascii=False))
        print("\nNada foi enviado. Remova --dry-run para rodar.")
        return

    try:
        rodada = busca(handles, args.dias)
    except FalhaNoRadar as erro:
        print(f"FALHOU: {erro}")
        sys.exit(1)

    print(f"RADAR · {', '.join('@' + h for h in handles)} · "
          f"últimos {args.dias} dias")
    print("  transcrição de modelo — o registro é o post, no link\n")

    if not rodada.posts:
        print("Nenhum post na janela.")
    for i, post in enumerate(rodada.posts, 1):
        print(f"[{i}] {post}\n")
    for nota in rodada.notas:
        print(f"  aviso da busca: {nota}")
    if rodada.links:
        print("Links citados:")
        for link in rodada.links:
            print(f"  {link}")
    print(f"\n  busca: US$ {rodada.custo_usd:.4f}"
          + (f" ({rodada.detalhe_custo})" if rodada.detalhe_custo else ""))

    if args.conferir is not None:
        if not (1 <= args.conferir <= len(rodada.posts)):
            print(f"\nNão existe post {args.conferir} nesta rodada.")
            sys.exit(1)
        print("\n" + "=" * 78)
        print(f"CONFERINDO O POST {args.conferir}")
        print("=" * 78)
        _confere(rodada.posts[args.conferir - 1], rodada.custo_usd)


if __name__ == "__main__":
    main()
