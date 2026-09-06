"""Boletim diário do radar: posts dos handles com as premissas conferidas.

    python -m src.boletim               # posts novos das últimas 24h
    python -m src.boletim --dias 3      # janela maior
    python -m src.boletim --sem-envio   # monta e grava, não envia

A segunda saída do sistema, e a que reconcilia o AC1 com o ARCHITECTURE:
é proativa (agente, não chatbot) e verifica população DISTINTA do acervo
(o que os handles alegaram, nunca a imprensa contra ela mesma). O post
indica onde olhar; a evidência vem do acervo — post não entra nele.

O fluxo por rodada:

    radar.busca(handles, janela)
      → descarta o que já foi entregue (tabela boletim_posts, por hash do
        texto do post E por ID de status)
      → para cada post inédito: premissas.separa → check de cada fato
      → monta o texto → imprime → grava em data/boletins/ → envia

REGRA DE ENTREGA: o arquivo em data/boletins/ é o registro; um post só é
marcado como entregue DEPOIS de o arquivo do dia ser gravado. Post cuja
conferência falhou no meio (API fora do ar) não é marcado — volta inteiro
na próxima rodada, e os vereditos que chegaram a ser pagos são reusados
pela janela do check em vez de pagos de novo. Telegram é melhor-esforço:
falha de envio vira status legível apontando o arquivo, nunca perda.

Entrega por TELEGRAM quando TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID estiverem
no .env (bot gratuito via @BotFather; ver .env.example). WhatsApp fica como
camada futura — o montador é o mesmo, só troca o carteiro.

Custo por rodada: a busca na API do X (US$ 0,005 por post lido) + separação
por post inédito (~US$ 0,03) + uma verificação por premissa factual
(~US$ 0,02-0,05). As duas metades do rodapé têm NATUREZA diferente, e ele
diz qual é qual: a da Anthropic vem do LIVRO-CAIXA — as
linhas que a rodada de fato gravou em `consultas`, e veredito reusado não
grava linha e não soma; a da busca é ESTIMATIVA feita no cliente (a API do
X não devolve preço), então sai rotulada de estimada. Estimativa
apresentada como medição seria fatura inventada.

O enquadramento é lei aqui, dobrado — e vai no CABEÇALHO além do rodapé,
para sobreviver a entrega parcial: a saída é conferência de premissas,
nunca placar do autor; o texto de cada post é o registro do servidor do X,
conferível no link.
"""

import argparse
import contextlib
import hashlib
import io
import os
import re
import sys
from datetime import datetime, timezone

import requests

from . import config, normalize

DIR_BOLETINS = config.DIR_DADOS / "boletins"
LIMITE_TELEGRAM = 4096

ENQUADRAMENTO = ("Conferência de premissas contra o acervo — não avalia o "
                 "autor. Sem evidência = o acervo não cobre.")


def _hash_post(texto: str) -> str:
    """Hash do TEXTO do post, em minúsculas e com espaço colapsado.

    Só o texto do autor entra: nada de número da rodada, data, URL ou
    contexto — tudo isso muda entre rodadas, ou conforme o referenciado foi
    lido ou não, e o mesmo post voltaria como inédito."""
    normalizado = " ".join(texto.lower().split())
    return hashlib.sha256(normalizado.encode("utf-8")).hexdigest()[:16]


def _chaves_do_post(c: "radar.Captura") -> set[str]:
    """A identidade de um post para dedup: 'url:<id do status>' quando o
    servidor deu o id; o hash do texto só quando não deu.

    Uma só, e não as duas: com o hash junto, dois posts diferentes com o
    mesmo texto ("Bom dia." em dias distintos) colidiam, e o segundo era
    tratado como já entregue para sempre, sem rastro. Post editado no X
    ganha status novo, então a versão pré-edição continua contando como
    inédita, como decidido."""
    if c.post.id:
        return {"url:" + c.post.id}
    return {_hash_post(c.post.texto)}


def _ja_entregues(conexao) -> set[str]:
    return {linha["hash"] for linha in
            conexao.execute("SELECT hash FROM boletim_posts")}


def _marca_entregue(conexao, hash_: str, resumo: str) -> None:
    conexao.execute(
        "INSERT OR IGNORE INTO boletim_posts (hash, resumo, entregue_em) "
        "VALUES (?, ?, ?)",
        (hash_, resumo[:120], datetime.now(timezone.utc).isoformat()))
    conexao.commit()


def _evidencias_gravadas(linha) -> list[tuple[str, str]]:
    """(veículo, url) do que o juiz citou, lido da coluna `evidencias`.

    Existe porque veredito reusado precisa poder mostrar a fonte de novo:
    até 03/09/2026 `consultas` guardava só a CONTAGEM de citadas, e o
    princípio 2 valia na hora de imprimir, não no arquivo."""
    import json as _json
    try:
        bruto = linha["evidencias"]
    except (IndexError, KeyError, TypeError):
        return []
    try:
        itens = _json.loads(bruto or "[]")
    except (ValueError, TypeError):
        return []
    return [(i.get("veiculo", ""), i.get("url", ""),
             i.get("titulo", ""), i.get("data", ""))
            for i in itens if isinstance(i, dict) and i.get("url")]


def _retida(linha) -> bool:
    """A consulta é confirmação retida pelo freio de alinhamento? Linha
    antiga não tem a coluna — vale False, que é o comportamento de antes."""
    try:
        return bool(linha["retida"])
    except (IndexError, KeyError):
        return False


_RE_EVIDENCIA = re.compile(
    r"^\s*\[([^\]]+)\][^\n]*\n\s+(https?://\S+)", re.MULTILINE)


def _confere_post(c: "radar.Captura", conexao,
                  estado: dict) -> tuple[str, float, dict]:
    """Separa as premissas de um post e confere cada fato. Devolve
    (bloco de texto puro, custo Anthropic, dados estruturados).

    `estado` é MUTÁVEL de propósito — {"acervo": ..., "orcamento": ...} —
    e pertence à rodada, não ao post: acervo recarregado e orçamento de
    demanda debitado sobrevivem mesmo quando esta função morre no meio
    (a revisão de 01/09/2026 mostrou que devolver os dois por retorno
    deixava uma exceção restaurar o orçamento já gasto — TETO furado no
    caminho de falha, que é o caminho onde teto mais importa).

    Os dados estruturados alimentam a rendição HTML do Telegram — montada
    das linhas de `consultas`, nunca do stdout capturado, para o celular
    não herdar a verborragia do terminal. O texto puro continua sendo a
    trilha completa (arquivo e console).

    Antes de cada fato ir ao check, a EXTRAÇÃO SOB DEMANDA (`demanda`)
    tenta cobrir a premissa com matéria coletada e ainda não extraída,
    dentro do orçamento da rodada. Se extraiu, o check roda com
    `forcar=True` — sem isso a janela de reuso de 24h devolvia o
    "sem evidência" antigo e a extração recém-paga nunca era julgada.
    Demanda é otimização: falha nela vira linha do boletim e um débito
    PESSIMISTA no orçamento (não dá para saber se a chamada chegou a ser
    cobrada), nunca derruba o check.

    O custo vem do livro-caixa: soma das linhas de `consultas` gravadas
    DURANTE esta função (id > marco) mais o custo faturado das extrações
    de demanda; veredito reusado não grava e não soma.
    """
    from . import check, contexto, demanda, grafo, premissas, radar

    marco = conexao.execute(
        "SELECT COALESCE(MAX(id), 0) FROM consultas").fetchone()[0]

    # O contexto entra ATRIBUÍDO: a linha do próprio autor (a thread dele)
    # resolve referência e ancora referente; a de quem ele cita, não. A
    # premissa sai só do texto do post — o pai da thread, quando está na
    # rodada, é conferido por conta própria (regra 9 do separador).
    analise, uso = premissas.separa(radar.para_separacao(c),
                                    conexao=conexao)
    partes: list[str] = []
    custo_demanda = 0.0
    dados: dict = {"nao_verificaveis": [], "checks": [], "contextos": [],
                   "sem_premissas": not analise.premissas}

    resto = [p for p in analise.premissas if p.tipo != "fato"]
    fatos = [p for p in analise.premissas if p.tipo == "fato"]

    vistos: set[str] = set()
    for p in resto:
        # p.texto: desde a evolução de 01/09/2026 o separador só reescreve
        # FATO; aqui vem o trecho literal do post — que é o que o leitor
        # quer ver, sem a paráfrase paga que repetia o post. A anotação
        # (motivo do roteador, hipótese não conferida) fica na trilha.
        partes.append(f"  [{p.tipo}] {p.texto} — nada a conferir"
                      f"{premissas.anotacao(p)}")
        dados["nao_verificaveis"].append((p.tipo, p.texto))

        # A TERCEIRA SAÍDA. Só para `nao_verificavel`, e buscando pela
        # HIPOTESE — o assunto que o separador já nomeou —, nunca pelo
        # texto do post: medido em 03/09/2026, buscar pelo post trazia 200
        # matérias e 13 veículos para o C3, sobre assunto nenhum. Ver
        # src/contexto.py. Contexto NÃO é veredito: não vai a `consultas`,
        # não conta como corroboração e não dispara demanda.
        # `p.roteado` preenchido significa que a premissa era FATO e o
        # roteador a rebaixou, pondo o referente rejeitado em `hipotese`.
        # Essa hipótese é do código, não do modelo, e foi TIRADA da tela
        # de propósito em 22f0ac9 — deixá-la voltar pelo [ACERVO] seria
        # reabrir, um commit depois, o eco que motivou tudo isto.
        if (not contexto.LIGADO
                or p.tipo != "nao_verificavel" or not p.hipotese or p.roteado
                or p.hipotese in vistos
                or estado.get("buscas_contexto", 0)
                >= contexto.TETO_POR_RODADA):
            continue
        vistos.add(p.hipotese)
        estado["buscas_contexto"] = estado.get("buscas_contexto", 0) + 1
        achado = contexto.do_assunto(p.hipotese,
                                     buscar=estado.get("buscar_contexto"))
        if achado:
            partes.append(f"        → {achado.assunto} — "
                          f"{contexto.linha(achado)}")
            for veiculo, titulo in achado.amostra:
                partes.append(f"          · {veiculo}: {titulo}")
            dados["contextos"].append(achado)

    def _roda_check(afirmacao: str, forcar: bool):
        """Um check capturado + a linha de consulta que ele produziu."""
        marco_f = conexao.execute(
            "SELECT COALESCE(MAX(id), 0) FROM consultas").fetchone()[0]
        s = io.StringIO()
        with contextlib.redirect_stdout(s):
            check.verifica(afirmacao, conexao=conexao,
                           acervo=estado["acervo"], forcar=forcar)
        linha = conexao.execute(
            "SELECT * FROM consultas WHERE id > ? "
            "ORDER BY id DESC LIMIT 1", (marco_f,)).fetchone()
        if linha is None:
            linha = check.consulta_recente(conexao, afirmacao)
        return s, linha

    for p in fatos:
        nota_demanda = None
        # CHECK PRIMEIRO; a demanda só depois de "sem evidência". A ordem
        # inversa usava proximidade vetorial como oráculo de cobertura e
        # caiu no primeiro teste vivo (01/09/2026, caso Esteves-Trump):
        # "Esteves integra BTG" no índice fingia cobrir a premissa da
        # REUNIÃO, a extração era pulada e o check morria sem evidência
        # com as matérias na mão. Proximidade casa entidade; quem sabe se
        # o FATO está coberto é o próprio veredito. O preço é um segundo
        # check quando a demanda dispara — só nesse caso.
        saida, nova = _roda_check(p.texto, forcar=False)
        # Retida NÃO é "o acervo não cobre": a evidência está lá e o freio
        # de alinhamento não a conferiu. Disparar a demanda aqui pagaria
        # extração para cobrir o que já está coberto, e o segundo check
        # seria retido de novo (achado da revisão de 03/09/2026).
        if (nova is not None and nova["veredito"] == "sem_evidencia"
                and not _retida(nova)):
            try:
                r = demanda.garante(conexao, p.texto,
                                    estado["orcamento"])
            except Exception as erro:  # noqa: BLE001 — não derruba o check
                r = None
                # Débito pessimista: a falha pode ter vindo DEPOIS de a
                # chamada ser cobrada (llm.py registra esse caso), e teto
                # que não desconta falha não é teto.
                estado["orcamento"] -= demanda.CUSTO_ESTIMADO
                partes.append(f"  demanda falhou ({type(erro).__name__}: "
                              f"{erro}) — fica o veredito sem o acervo "
                              f"novo; orçamento debitado por precaução")
            if r is not None and r.motivo == "extraiu":
                estado["orcamento"] -= r.custo
                custo_demanda += r.custo
                estado["acervo"] = grafo.carrega(conexao)
                nota_demanda = (f"{r.materias} matéria(s) extraída(s) na "
                                f"hora, {r.triplas} triplas")
                partes.append(f"  [DEMANDA] {nota_demanda} · "
                              f"US$ {r.custo:.4f}")
                # forcar: sem isso a janela de reuso devolveria o
                # "sem evidência" que acabou de motivar a extração.
                saida, nova = _roda_check(p.texto, forcar=True)
            elif r is not None and r.motivo == "sem_tripla":
                # Pagou e o acervo não mudou: sem recarregar e sem o
                # segundo check, que não teria como mudar de veredito.
                estado["orcamento"] -= r.custo
                custo_demanda += r.custo
                partes.append(f"  [DEMANDA] {r.materias} matéria(s) "
                              f"extraída(s), nenhuma tripla · "
                              f"US$ {r.custo:.4f} — o acervo não mudou")
            elif r is not None and r.motivo == "teto":
                partes.append("  [DEMANDA] teto da rodada atingido — "
                              "fica o veredito só com o acervo")
        partes.append(f'  premissa: "{p.texto}"')
        evidencias = _RE_EVIDENCIA.findall(saida.getvalue())
        # Raspar o stdout só funciona quando o check RODOU. No reuso ele
        # imprime "veredito gravado nas últimas 24h" e nada mais, então a
        # lista vinha vazia e o boletim mostrava "CONFIRMADO · 4
        # veículos" sem um link — veredito sem fonte, contra o princípio
        # 2. A coluna `evidencias` é a reserva; linha antiga não a tem e
        # vale lista vazia, que é o comportamento de antes.
        if not evidencias and nova is not None:
            evidencias = _evidencias_gravadas(nova)
        dados["checks"].append({
            "afirmacao": p.texto,
            "veredito": nova["veredito"] if nova else "sem_evidencia",
            "justificativa": nova["justificativa"] if nova else "",
            "veiculos": nova["veiculos"] if nova else 0,
            "custo": nova["custo_usd"] if nova else 0.0,
            "evidencias": evidencias,
            "demanda": nota_demanda,
            "retida": bool(nova is not None and _retida(nova)),
        })
        # Sem evidência vira UMA linha: a enumeração do que foi olhado e
        # rejeitado é trilha de auditoria — mora em `consultas` e no
        # painel, não no bolso.
        if nova and nova["veredito"] == "sem_evidencia":
            razao = ("a evidência não foi conferida (confirmação retida)"
                     if _retida(nova) else "o acervo não cobre")
            partes.append(f"    → SEM EVIDÊNCIA — {razao} · "
                          f"US$ {nova['custo_usd']:.4f}")
        else:
            partes.append("    " + "\n    ".join(
                linha for linha in saida.getvalue().splitlines() if linha))

    if not analise.premissas:
        partes.append("  (nenhuma afirmação separável)")

    pago_em_checks = conexao.execute(
        "SELECT COALESCE(SUM(custo_usd), 0) FROM consultas WHERE id > ?",
        (marco,)).fetchone()[0]
    return ("\n".join(partes), uso.custo + pago_em_checks + custo_demanda,
            dados)


def monta(dias: int, reenviar: bool = False, *,
          desde: str = "", ate: str = "",
          ) -> tuple[str, float, list[tuple[set[str], str]], str]:
    """Roda a cadeia e devolve (texto, custo total, [(chaves, resumo)] dos
    posts contidos, HTML do Telegram, quantos posts inéditos falharam).
    Quem marca entrega é o chamador, DEPOIS de gravar — e marca TODAS as
    chaves de cada post.

    Com `desde`/`ate` (datas ou ISO8601), a janela é a pedida em vez de
    "os últimos `dias`", e o cabeçalho mostra a janela em vez de hoje: é
    o refazer de um dia passado, um por rodada (06/09/2026). O arquivo
    continua sendo o de hoje, em append — o registro é de quando rodou.

    Com `reenviar`, o estado 'já entregue' é ignorado e a janela inteira
    volta — para auditar formato novo sem apagar histórico. O dedup
    DENTRO da rodada continua valendo.

    Falha na conferência de um post não derruba a rodada nem o marca:
    o post volta inteiro na próxima, e o que já foi pago em vereditos é
    reusado pela janela do check.
    """
    from . import grafo, radar
    from .storage import conecta

    if not config.HANDLES_RADAR:
        raise SystemExit(
            "Nenhum handle no radar — defina HANDLES_RADAR no .env "
            "(ver .env.example) antes de rodar o boletim.")

    conexao = conecta(config.BANCO)
    try:
        acervo = grafo.carrega(conexao)
        if not acervo:
            raise SystemExit("Acervo vazio — rode coleta, extração e "
                             "índice antes do boletim.")

        try:
            rodada = radar.busca(config.HANDLES_RADAR, dias,
                                 desde=desde, ate=ate)
        except radar.FalhaNoRadar as erro:
            raise SystemExit(f"Busca do radar falhou: {erro}") from erro

        # `vistos` acumula as chaves da própria rodada: o mesmo post lido
        # duas vezes (handle repetido na lista) não pode virar entrega dupla.
        vistos = set() if reenviar else _ja_entregues(conexao)
        ineditos: list[tuple[radar.Captura, set[str]]] = []
        for c in rodada.capturas:
            chaves = _chaves_do_post(c)
            if chaves & vistos:
                continue
            vistos |= chaves
            ineditos.append((c, chaves))

        # Data LOCAL, não UTC: é cabeçalho para o leitor, e às 21:30 de
        # um dia o UTC já virou o outro — o digest chegava "datado de
        # amanhã" (apontado pelo usuário em 01/09/2026 às 22h). UTC fica
        # para os carimbos internos (estado, banco), onde comparação
        # importa mais que leitura.
        hoje = datetime.now().astimezone().strftime("%d/%m/%Y")
        if desde:
            hoje = f"janela {desde} → {ate or 'agora'} (UTC)"
        handles = ", ".join("@" + h for h in config.HANDLES_RADAR)
        linhas = [f"RADAR · {handles} · {hoje}",
                  "texto literal do post, lido pela API oficial do X — o "
                  "registro é o post, no link",
                  ENQUADRAMENTO, ""]
        custo = rodada.custo_estimado_usd
        contidos: list[tuple[set[str], str]] = []
        estruturados: list[tuple[int, radar.Captura, dict]] = []

        if not ineditos:
            linhas.append((f"Nenhum post novo na {hoje}." if desde else
                           f"Nenhum post novo na janela de {dias} dia(s).")
                          if not rodada.capturas else
                          f"{len(rodada.capturas)} post(s) na janela, todos "
                          f"já entregues em boletins anteriores.")
        from . import demanda, indice
        # O índice do COLETADO é o que a terceira saída lê, e até
        # 03/09/2026 só a demanda o atualizava — quando nenhum post
        # gerava demanda, o contexto reportava o período do índice
        # VELHO como se fosse o do acervo. É incremental (embeda só o
        # que falta) e não custa API.
        try:
            indice.indexa_artigos(conexao)
        except Exception:  # noqa: BLE001
            # Índice indisponível não derruba o boletim: a terceira saída
            # some, o resto continua. Contexto é acréscimo, não o
            # produto.
            pass
        # O estado é da RODADA e mutável de propósito: exceção num post
        # não pode restaurar orçamento de demanda já gasto nem descartar
        # o acervo recarregado (revisão de 01/09/2026).
        estado = {"acervo": acervo, "orcamento": demanda.TETO_USD}
        falhas = 0
        for i, (c, chaves) in enumerate(ineditos, 1):
            linhas.append(radar.como_texto(c, i))
            # Falha num post não derruba o lote — padrão do extract.main.
            # Mas é CONTADA: separação estourando em todos os posts virava
            # arquivo cheio de "CONFERÊNCIA FALHOU", Telegram sem nada e
            # código de saída 0 — o silêncio que `_avisa_falha` existe
            # para não deixar acontecer. A contagem vai para as notas
            # (Telegram) e para `main`, que decide se avisa e sai com erro.
            try:
                bloco, gasto, dados = _confere_post(c, conexao, estado)
            except Exception as erro:  # noqa: BLE001 — vira linha do boletim
                falhas += 1
                linhas.append(f"  CONFERÊNCIA FALHOU ({type(erro).__name__}: "
                              f"{erro}) — o post volta na próxima rodada")
                linhas.append("")
                continue
            custo += gasto
            linhas.append(bloco)
            linhas.append("")
            contidos.append((chaves, c.post.texto))
            estruturados.append((i, c, dados))

        notas = list(rodada.notas)
        if falhas:
            notas.append(f"{falhas} post(s) inédito(s) com CONFERÊNCIA "
                         f"FALHOU — voltam na próxima rodada; a trilha está "
                         f"no arquivo")
        for nota in notas:
            linhas.append(f"aviso da busca: {nota}")
        # O que foi lido e não virou post, com o motivo: é a trilha de
        # auditoria do descarte, e mora no arquivo, não no bolso.
        for p, motivo in rodada.descartados:
            linhas.append(f"descartado: @{p.autor} "
                          f"{p.url or p.id or '(sem id)'} — {motivo}")
        linhas.append("")
        linhas.append(ENQUADRAMENTO)
        # Duas carteiras, dois consoles: quem confere fatura precisa saber
        # de qual bolso saiu cada parte. E as duas metades não têm o mesmo
        # peso: a da busca é conta nossa, a da Anthropic é livro-caixa. O
        # rótulo vai na linha para não confundir as duas.
        busca = rodada.custo_estimado_usd
        linhas.append(f"custo da rodada: US$ {custo:.4f} "
                      f"(busca no X US$ {busca:.4f} estimado + "
                      f"Anthropic US$ {custo - busca:.4f} medido)")
        html = _formata_telegram(handles, hoje, estruturados, notas,
                                 custo, busca)
        return "\n".join(linhas), custo, contidos, html, falhas
    finally:
        conexao.close()


# ------------------------------------------------------------- rendição

def _esc(texto: str) -> str:
    return (texto.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


_TAG_TIPO = {"opiniao": "OPINIÃO", "previsao": "PREVISÃO",
             "relato": "RELATO", "nao_verificavel": "NÃO VERIFICÁVEL"}
_TAG_VEREDITO = {"confirmado": "CONFIRMADO", "contradito": "CONTRADITO",
                 "sem_evidencia": "SEM EVIDÊNCIA"}


def _conta_tipos(nao_verificaveis) -> str:
    """'OPINIÃO ×8 · RELATO ×2' — os tipos na ordem fixa do prompt, com a
    contagem só quando passa de um. Um só sai '[OPINIÃO]', como antes.

    O `×` entrou em 03/09/2026: sem ele, "OPINIÃO 2" lia-se como
    "opinião número 2" e sugeria uma opinião 1 em algum outro lugar. A
    contagem sempre foi DESTE post — o que faltava era dizer isso na
    tipografia."""
    contagem: dict[str, int] = {}
    for tipo, _ in nao_verificaveis:
        contagem[tipo] = contagem.get(tipo, 0) + 1
    ordem = [t for t in _TAG_TIPO if t in contagem]
    ordem += [t for t in contagem if t not in _TAG_TIPO]
    return " · ".join(
        _TAG_TIPO.get(t, t.upper()) + (f" ×{contagem[t]}" if contagem[t] > 1
                                       else "")
        for t in ordem)


def _formata_telegram(handles: str, hoje: str, estruturados, notas,
                      custo: float, custo_busca: float) -> str:
    """A rendição HTML do Telegram: os MESMOS dados do texto puro, com
    hierarquia visual — negrito no cabeçalho, itálico no post, etiqueta
    monoespaçada no tipo e link clicável na evidência. Trilha completa
    continua no arquivo; aqui é o resumo para o bolso. Tudo que vem de
    modelo ou de post passa por `_esc` antes de virar HTML.

    Sem emoji, por pedido (01/09/2026): etiquetas textuais [RELATO],
    [CONFIRMADO] etc. O Telegram não aceita cor de texto — a paleta é
    negrito (veredito), itálico (texto de post) e `<code>` (etiquetas),
    que os clientes renderizam num tom próprio: é a "cor" possível.

    Não-verificáveis saem CONTADOS, sem o texto (pedido de 02/09/2026):
    desde a v2 do separador o que há para exibir é o trecho literal, e
    a linha "[OPINIÃO] <trecho>" virava eco — num post de uma frase, o
    post inteiro de novo logo abaixo dele; num post de análise, a
    mensagem dobrada, frase a frase. O tipo continua nomeado (opinião e
    relato não são descarte, são o texto); o trecho fica no arquivo."""
    from . import contexto, radar

    def tag(texto: str) -> str:
        return f"<code>[{texto}]</code>"

    p: list[str] = [f"<b>RADAR · {_esc(handles)} · {hoje}</b>",
                    f"<i>{_esc(ENQUADRAMENTO)}</i>", ""]
    if not estruturados:
        p.append("Nenhum post novo na janela.")
    for i, c, dados in estruturados:
        # Cabeçalho: número, (handle, data) e a âncora do próprio status.
        # O CORPO vai na íntegra, sem truncar: post é conteúdo, não resumo.
        post = c.post
        meta = (f" <i>(@{_esc(post.autor)}, "
                f"{_esc(radar.quando(post.criado_em))})</i>")
        ver = (f' — <a href="{_esc(post.url)}">ver no X</a>' if post.url
               else "")
        p.append(f"<b>[{i}]</b>{meta}{ver}")
        # [CONTEXTO] é palavra do próprio autor (a thread dele); [CITANDO]
        # é o post de outra conta que ele comenta. A atribuição vem da
        # comparação de autor, não do texto.
        ref = c.referenciado
        if ref is not None and ref.texto.strip():
            rotulo = "CONTEXTO" if c.contexto_proprio else "CITANDO"
            p.append(f"{tag(rotulo)} <i>@{_esc(ref.autor)}: "
                     f"{_esc(' '.join(ref.texto.split()))}</i>")
        p.append(f"<i>{_esc(post.texto)}</i>")

        # [ACERVO], e não [CONTEXTO]: esse rótulo já significa "EM
        # RESPOSTA A" aqui em cima. O texto descreve o ACERVO, nunca a
        # premissa — nada de "confirmado", nada de contagem de veículo
        # apresentada como corroboração do que o post insinua.
        for c in dados.get("contextos", []):
            fontes = " · ".join(_esc(v) for v, _ in c.amostra)
            p.append(f"{tag('ACERVO')} {_esc(c.assunto)} — "
                     f"{_esc(contexto.linha(c))}"
                     + (f" · <i>{fontes}</i>" if fontes else ""))
        for c in dados["checks"]:
            rotulo = _TAG_VEREDITO.get(c["veredito"], c["veredito"].upper())
            if c.get("demanda"):
                p.append(f"{tag('DEMANDA')} {_esc(c['demanda'])}")
            if c["veredito"] == "sem_evidencia":
                razao = ("evidência não conferida" if c.get("retida")
                         else "o acervo não cobre")
                p.append(f"<b>[{rotulo}]</b> {razao} · "
                         f"<i>{_esc(c['afirmacao'])}</i>")
            else:
                # O TÍTULO, não só o nome do veículo: "CNN · Folha · G1"
                # não diz O QUE confirma. Cada linha é uma manchete
                # clicável, com a data do fato quando o acervo a tem.
                # (Achado do usuário no digest de 03/09/2026.)
                # Duas formas chegam aqui: o raspão do stdout dá
                # (veículo, url), e a coluna `evidencias` dá
                # (veículo, url, título, data). Aceitar as duas, porque a
                # primeira é o que existe para veredito antigo.
                def _fonte(e):
                    veiculo, url = e[0], e[1]
                    titulo = e[2] if len(e) > 2 else ""
                    data = e[3] if len(e) > 3 else ""
                    linha = f'   <a href="{_esc(url)}">{_esc(veiculo)}</a>'
                    if titulo:
                        linha += f': {_esc(titulo)}'
                    if data:
                        linha += f' <i>· {_esc(str(data)[:10])}</i>'
                    if normalize.e_live(url):
                        linha += ' <i>· liveblog</i>'
                    return linha

                fontes = chr(10).join(
                    _fonte(e) for e in c["evidencias"][:4] if len(e) >= 2)
                p.append(f"<b>[{rotulo}]</b> · {c['veiculos']} veículo(s) — "
                         f"<i>{_esc(c['afirmacao'])}</i>")
                p.append(f"    {_esc(c['justificativa'])}")
                if fontes:
                    p.append(f"    {tag('EVIDÊNCIA')} {fontes}")
        # A opinião vai DEPOIS do veredito (pedido de 03/09/2026): o que
        # o sistema apurou vem primeiro, e o que ele não tinha como
        # conferir fecha o bloco. Antes abria, e a primeira coisa que o
        # leitor via era o que o sistema NÃO fez.
        if dados["nao_verificaveis"]:
            p.append(f"{tag(_conta_tipos(dados['nao_verificaveis']))} "
                     "nada a conferir")
        if dados["sem_premissas"]:
            p.append("(nenhuma afirmação separável)")
        p.append("")
    for nota in notas:
        p.append(f"{tag('AVISO')} {_esc(nota)}")
    p.append("")
    # "estimado" só na busca, "medido" só na Anthropic — e isto é regra,
    # não estilo: a API do X não devolve preço, então a
    # metade da busca é multiplicação nossa (`x_api.custo_estimado_usd`),
    # enquanto a da Anthropic sai das linhas gravadas em `consultas`. Este
    # rodapé é o que chega ao celular; sem o rótulo, o dono leria as duas
    # como fatura.
    p.append(f"<i>custo: US$ {custo:.2f} (busca no X {custo_busca:.2f} "
             f"estimado + Anthropic {custo - custo_busca:.2f} medido)</i>")
    return "\n".join(p)


# ---------------------------------------------------------------- entrega

def _grava(texto: str) -> "os.PathLike":
    DIR_BOLETINS.mkdir(parents=True, exist_ok=True)
    # Dia LOCAL também no nome do arquivo: "o boletim de terça" tem que
    # estar no arquivo de terça, não no de quarta por causa do UTC.
    caminho = DIR_BOLETINS / (
        datetime.now().astimezone().strftime("%Y-%m-%d") + ".txt")
    # Append: duas rodadas no mesmo dia ficam no mesmo arquivo, separadas.
    with open(caminho, "a", encoding="utf-8") as arquivo:
        arquivo.write(texto + "\n\n" + "=" * 72 + "\n\n")
    return caminho


_RE_TAG = re.compile(r"<(/?)(b|i|a|code|u|s|pre)(\s[^>]*)?>", re.IGNORECASE)


def _equilibra(pedacos: list) -> list:
    """Fecha no fim de cada pedaco as tags abertas, e reabre no seguinte.

    Cortar respeitando quebra de linha NAO basta, e isso custou a entrega
    de um boletim inteiro em 03/09/2026: o corpo do post e `<i>texto</i>`
    e o texto TEM quebras de linha, entao o corte caia DENTRO da tag -- o
    `<i>` ficava aberto num pedaco e o `</i>` orfao no seguinte, e o
    Telegram devolvia 400 "Can't find end tag" no pedaco 1 de 5. Um
    boletim de 25 posts nao cabe numa mensagem so, entao a falha aparece
    justamente quando ha MAIS o que entregar.

    Guarda a ABERTURA literal, com atributos, para reabrir igual: um
    `<a href="...">` cortado no meio precisa do href de volta, senao o
    link vira texto."""
    saida, abertas = [], []
    for pedaco in pedacos:
        corpo = "".join(abertas) + pedaco
        pilha = []
        for m in _RE_TAG.finditer(corpo):
            if m.group(1):
                if pilha and _RE_TAG.match(pilha[-1]).group(2).lower() \
                        == m.group(2).lower():
                    pilha.pop()
            else:
                pilha.append(m.group(0))
        fecho = "".join("</" + _RE_TAG.match(t).group(2) + ">"
                        for t in reversed(pilha))
        saida.append(corpo + fecho)
        abertas = list(pilha)
    return saida


def _envia_telegram(texto: str, html: bool = False) -> str:
    """Envia se o .env tiver bot e chat. Devolve o status para o relatório —
    qualquer falha vira texto, nunca traceback: o arquivo já é o registro.

    Com `html`, o corte em pedaços respeita quebras de linha (cortar no
    meio de uma tag quebraria o parse do Telegram inteiro)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return ("não enviado — TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes "
                "no .env (ver .env.example)")
    if html:
        pedacos, atual = [], ""
        for linha in texto.split("\n"):
            if len(atual) + len(linha) + 1 > LIMITE_TELEGRAM:
                pedacos.append(atual)
                atual = linha
            else:
                atual = f"{atual}\n{linha}" if atual else linha
        if atual:
            pedacos.append(atual)
        pedacos = _equilibra(pedacos)
    else:
        pedacos = [texto[i:i + LIMITE_TELEGRAM]
                   for i in range(0, len(texto), LIMITE_TELEGRAM)]
    for n, pedaco in enumerate(pedacos, 1):
        corpo = {"chat_id": chat, "text": pedaco,
                 "disable_web_page_preview": True}
        if html:
            corpo["parse_mode"] = "HTML"
        try:
            resposta = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=corpo, timeout=30)
        except requests.RequestException as erro:
            return (f"FALHOU no Telegram (pedaço {n}/{len(pedacos)}, "
                    f"{type(erro).__name__}) — o boletim está no arquivo")
        if not resposta.ok:
            return (f"FALHOU no Telegram ({resposta.status_code}, pedaço "
                    f"{n}/{len(pedacos)}): {resposta.text[:200]} — o "
                    f"boletim está no arquivo")
    return f"enviado ao Telegram em {len(pedacos)} mensagem(ns)"


def _avisa_falha(erro: str) -> None:
    """Uma linha no Telegram quando o boletim morre antes de existir.

    O silêncio era o pior modo de falhar. Em 03/09/2026 a busca estourou
    e o único registro foi uma linha em `data/boletim.log`, que ninguém
    abre: o boletim ficou parado e a descoberta veio um dia depois. Com a
    conta pré-paga da API do X isso piora — crédito esgotado devolve 4xx,
    e 4xx não repete.

    Aviso curto de propósito: diz QUE falhou e onde está a trilha, não
    reproduz infraestrutura. Falhar ao avisar não pode virar a falha
    reportada — o erro que importa é o de cima, e ele sobe intacto.
    """
    aviso = ("<b>BOLETIM NÃO SAIU</b>\n"
             f"{_esc(erro[:300])}\n"
             "<i>a trilha está em data/boletim.log</i>")
    try:
        _envia_telegram(aviso, html=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Boletim do radar: posts com premissas conferidas.")
    parser.add_argument("--dias", type=int, default=1,
                        help="janela da busca (padrão: 1)")
    parser.add_argument("--desde", default="",
                        help="início da janela (AAAA-MM-DD ou ISO8601, UTC); "
                             "com ele, --dias é ignorado")
    parser.add_argument("--ate", default="",
                        help="fim EXCLUSIVO da janela (AAAA-MM-DD ou "
                             "ISO8601, UTC); padrão: agora")
    parser.add_argument("--sem-envio", action="store_true",
                        help="monta e grava o arquivo, não envia")
    parser.add_argument("--reenviar", action="store_true",
                        help="ignora o estado 'já entregue' e refaz a "
                             "janela inteira (CARO: refaz busca e checks)")
    args = parser.parse_args()

    try:
        texto, custo, contidos, html, falhas = monta(
            args.dias, reenviar=args.reenviar,
            desde=args.desde, ate=args.ate)
    except (SystemExit, Exception) as erro:
        # --sem-envio nao avisa: e' o modo de pre-visualizar, e quem
        # o roda esta olhando a tela.
        if not args.sem_envio:
            _avisa_falha(f"{type(erro).__name__}: {erro}")
        raise
    print(texto)
    caminho = _grava(texto)
    print(f"\ngravado em {caminho}")

    if falhas and not contidos:
        # Todos os inéditos falharam: o arquivo tem a trilha, mas o leitor
        # não receberia nada e o agendador veria sucesso. É a mesma classe
        # de silêncio de quando o boletim morre antes de existir, e leva o
        # mesmo tratamento — aviso no Telegram e código de saída ≠ 0.
        aviso = (f"{falhas} post(s) inédito(s) e nenhum conferido — a "
                 f"trilha está em {caminho}")
        if not args.sem_envio:
            _avisa_falha(aviso)
        print(f"FALHOU: {aviso}")
        sys.exit(1)

    # Marca DEPOIS de gravar: o arquivo é o registro de entrega. Se o
    # processo morrer antes desta linha, nada foi marcado e a próxima
    # rodada refaz — reusando os vereditos pagos, pela janela do check.
    from .storage import conecta
    # `--sem-envio` NÃO marca entregue. Ele pulava só o envio e gravava a
    # marca assim mesmo, então o modo que existe para pré-visualizar
    # QUEIMAVA os posts: a rodada seguinte os tratava como já entregues e
    # o boletim de verdade saía vazio. Descoberto em 03/09/2026, depois
    # de uma rodada de US$ 1,68 que marcou 20 posts sem mandar nenhum.
    # Marca = "o leitor recebeu", e com --sem-envio ninguém recebeu.
    # E marca DEPOIS DE ENTREGAR, não antes. Em 03/09/2026 o boletim de
    # 9 dias marcou 14 posts às 20:05 e o Telegram recusou a mensagem
    # inteira (tag cortada no pedaço): 25 posts apurados, nada entregue,
    # e todos queimados. Marca = "o leitor recebeu"; envio que falhou não
    # é recebimento. Telegram NÃO CONFIGURADO é o único caso em que a
    # marca vale sem envio, porque aí o arquivo é o registro, como sempre
    # foi.
    status = "pulada — nada novo"
    entregue = False
    if contidos and not args.sem_envio:
        # O celular recebe a rendição HTML; o texto puro é a trilha,
        # gravada no arquivo acima.
        status = _envia_telegram(html, html=True)
        entregue = not status.startswith("FALHOU")
    elif args.sem_envio:
        status = "pulada — --sem-envio"
    print(f"entrega: {status}")

    if contidos and not args.sem_envio and entregue:
        conexao = conecta(config.BANCO)
        for chaves, resumo in contidos:
            for chave in chaves:
                _marca_entregue(conexao, chave, resumo)
        conexao.close()
    elif contidos and not args.sem_envio:
        print("NADA foi marcado como entregue: a entrega falhou e os "
              "posts continuam inéditos para a próxima rodada.")


if __name__ == "__main__":
    main()
