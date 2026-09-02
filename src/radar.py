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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from . import config

URL_API = "https://api.x.ai/v1/responses"
MODELO = "grok-4.6"
TICK_USD = 1e-10
TIMEOUT = 180

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
        "EM RESPOSTA A (@autor): <texto do post respondido — inclua esta "
        "linha SOMENTE se o post for uma resposta; senão, omita>\n"
        "CITANDO (@autor): <texto do post citado/quotado — inclua esta "
        "linha SOMENTE se o post cita outro post; senão, omita>\n"
        "<texto literal>\n---\n"
        "A linha URL de cada bloco tem de apontar para o post transcrito "
        "NAQUELE bloco, nunca para outro. "
        "NÃO TRANSCREVA respostas a outros usuários — ignore-as por "
        "completo. Transcreva apenas: posts originais, quote-posts, e "
        "continuações de thread própria (o handle respondendo a si "
        "mesmo — nesse caso a linha EM RESPOSTA A traz o post anterior "
        "da própria thread). "
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


def busca(handles: tuple[str, ...], dias: int = 2) -> Rodada:
    chave = os.environ.get("XAI_API_KEY", "")
    if not chave:
        raise FalhaNoRadar(
            "XAI_API_KEY ausente no .env — o radar é o único módulo que "
            "usa a xAI, e é opcional. Ver .env.example.")
    try:
        resposta = requests.post(
            URL_API,
            headers={"Authorization": f"Bearer {chave}",
                     "Content-Type": "application/json"},
            json=_corpo(handles, dias), timeout=TIMEOUT)
        if resposta.status_code >= 400:
            # O corpo carrega o motivo real (modelo inexistente, sem
            # crédito, parâmetro inválido); só o código não diz nada.
            raise FalhaNoRadar(
                f"xAI respondeu {resposta.status_code}: "
                f"{resposta.text[:300]}")
        # Dentro do try: JSONDecodeError do requests é RequestException,
        # e corpo 200 que não é JSON também é "resposta ilegível".
        dados = resposta.json()
    except requests.RequestException as erro:
        raise FalhaNoRadar(f"busca na xAI falhou: {erro}") from erro

    bruto = json.dumps(dados, ensure_ascii=False)
    texto = "\n".join(_textos_de(dados.get("output", dados)))
    posts, notas = _posts_de(texto)
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
            print(f"  [{p.tipo}] {p.texto}")
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
