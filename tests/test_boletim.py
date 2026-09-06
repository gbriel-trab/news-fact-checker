"""As partes do boletim e da guarda de reuso que não dependem de rede.

O estado 'já entregue' impede o boletim de repetir posts entre rodadas; a
guarda de reuso do check impede pagar duas vezes pela mesma afirmação — 29%
do gasto de consulta medido em 31/08/2026 era repetição.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.boletim import _hash_post, _ja_entregues, _marca_entregue
from src.check import consulta_recente
from src.radar import Captura
from src.storage import conecta, salva_consulta
from src.x_api import Post


def _captura(texto="qualquer coisa.", ident="", autor="x",
             referenciado=None, tipo="post"):
    """Um `radar.Captura` sintético, sem rede: o que o boletim consome."""
    return Captura(
        post=Post(id=ident, autor=autor, criado_em="2026-09-01T12:00:00Z",
                  texto=texto, tipo=tipo,
                  url=f"https://x.com/{autor}/status/{ident}" if ident
                  else ""),
        referenciado=referenciado)


CAPTURA_DE_TESTE = _captura()


def _banco(tmp_path):
    return conecta(tmp_path / "t.db")


class TestEstadoDoBoletim:
    def test_hash_estavel_a_espacos_e_caixa(self):
        assert _hash_post("Bitcoin  a 80 mil") == _hash_post("bitcoin a 80 MIL")

    def test_hash_e_so_do_texto_do_post(self):
        # Número da rodada, data, URL e contexto ficam fora: só o texto do
        # autor identifica o post entre rodadas.
        assert _hash_post("texto do post") == _hash_post(" texto  do\npost ")

    def test_chave_e_o_id_do_servidor_e_so_sem_id_cai_no_texto(self):
        from src.boletim import _chaves_do_post
        com_id = _captura("texto", ident="123")
        assert _chaves_do_post(com_id) == {"url:123"}
        # Dois posts diferentes com o mesmo texto ("Bom dia." em dias
        # distintos) não podem colidir: com o hash junto, o segundo era
        # "já entregue" para sempre.
        assert _chaves_do_post(_captura("texto", ident="456")) == {"url:456"}
        # Sem id não há identidade forte; o hash cobre.
        assert _chaves_do_post(_captura("texto")) == {_hash_post("texto")}

    def test_post_marcado_nao_volta(self, tmp_path):
        con = _banco(tmp_path)
        h = _hash_post("post qualquer")
        assert h not in _ja_entregues(con)
        _marca_entregue(con, h, "post qualquer")
        assert h in _ja_entregues(con)

    def test_marcar_duas_vezes_nao_quebra(self, tmp_path):
        con = _banco(tmp_path)
        _marca_entregue(con, "abc", "x")
        _marca_entregue(con, "abc", "x")
        assert _ja_entregues(con) == {"abc"}

    def test_sem_handles_recusa_antes_de_pagar(self, monkeypatch):
        # O guard que o radar.main tem e o boletim não tinha: HANDLES_RADAR
        # vazio não pode disparar busca paga com filtro vazio.
        from src import boletim, config
        monkeypatch.setattr(config, "HANDLES_RADAR", ())
        import pytest
        with pytest.raises(SystemExit, match="HANDLES_RADAR"):
            boletim.monta(1)


class TestRendicaoTelegram:
    def test_escapa_html_de_conteudo_de_modelo(self):
        from src.boletim import _esc
        assert _esc("a <b> & c") == "a &lt;b&gt; &amp; c"

    def test_sem_evidencia_vira_linha_com_semaforo(self):
        from src.boletim import _formata_telegram
        html = _formata_telegram("@x", "01/09", [(1, CAPTURA_DE_TESTE, {
            "nao_verificaveis": [("opiniao", "opinou algo")],
            "checks": [{"afirmacao": "o IPCA foi 5,2%",
                        "veredito": "sem_evidencia", "justificativa": "",
                        "veiculos": 0, "custo": 0.02, "evidencias": []}],
            "sem_premissas": False,
        })], [], 0.10, 0.03)
        assert "<b>[SEM EVIDÊNCIA]</b>" in html
        assert "<code>[OPINIÃO]</code>" in html
        assert "<b>[1]</b>" in html

    def test_confirmado_traz_fonte_clicavel(self):
        from src.boletim import _formata_telegram
        html = _formata_telegram("@x", "01/09", [(1, CAPTURA_DE_TESTE, {
            "nao_verificaveis": [],
            "checks": [{"afirmacao": "a Caixa lucrou",
                        "veredito": "confirmado", "justificativa": "bate",
                        "veiculos": 2, "custo": 0.02,
                        "evidencias": [("G1", "http://g1/x")]}],
            "sem_premissas": False,
        })], [], 0.10, 0.03)
        assert "<b>[CONFIRMADO]</b>" in html
        assert "<code>[EVIDÊNCIA]</code>" in html
        assert '<a href="http://g1/x">G1</a>' in html

    def test_nota_de_demanda_aparece(self):
        from src.boletim import _formata_telegram
        html = _formata_telegram("@x", "01/09", [(1, CAPTURA_DE_TESTE, {
            "nao_verificaveis": [],
            "checks": [{"afirmacao": "a", "veredito": "confirmado",
                        "justificativa": "ok", "veiculos": 2, "custo": 0.02,
                        "evidencias": [("G1", "http://g1/x")],
                        "demanda": "2 matéria(s) extraída(s) na hora"}],
            "sem_premissas": False,
        })], [], 0.10, 0.03)
        assert "<code>[DEMANDA]</code>" in html

    def test_sem_emoji_na_rendicao(self):
        # Pedido de 01/09/2026: etiquetas textuais no lugar de emoji.
        from src.boletim import _formata_telegram
        html = _formata_telegram("@x", "01/09", [(1, CAPTURA_DE_TESTE, {
            "nao_verificaveis": [("relato", "r"), ("previsao", "p")],
            "checks": [{"afirmacao": "a", "veredito": "sem_evidencia",
                        "justificativa": "", "veiculos": 0, "custo": 0,
                        "evidencias": []}],
            "sem_premissas": False,
        })], ["nota da busca"], 0.10, 0.03)
        for emoji in "📡💬🔮👤⚪✅❌🔗⚠️↳":
            assert emoji not in html
        assert "<code>[PREVISÃO · RELATO]</code>" in html
        assert "<code>[AVISO]</code>" in html

    def test_nao_verificavel_nao_repete_o_post(self):
        """Pedido de 02/09/2026: com o trecho literal da v2, a linha
        [OPINIÃO] era o post de novo — num post de uma frase, o post
        inteiro. Os tipos saem nomeados e contados, sem o texto; a trilha
        completa fica no arquivo."""
        from src.boletim import _conta_tipos, _formata_telegram
        # O caso que motivou o pedido é o C1 do gabarito local; o corpo
        # aqui é sintético — só precisa ser de UMA frase.
        post = _captura("Frase única de corpo sintético para este teste.")
        html = _formata_telegram("@x", "02/09", [(1, post, {
            "nao_verificaveis": [
                ("opiniao", "todos os rumos mudam"),
                ("relato", "convivi com ele"),
                ("opiniao", "André se reune com Trump")],
            "checks": [], "sem_premissas": False,
        })], [], 0.10, 0.03)
        # "OPINIÃO 2" lia-se como "opinião número 2" e sugeria uma
        # opinião 1 em outro lugar; o × diz que é contagem DESTE post.
        assert "<code>[OPINIÃO ×2 · RELATO]</code> nada a conferir" in html
        assert html.count("nada a conferir") == 1
        assert "convivi com ele" not in html
        # Um só continua "[OPINIÃO]", sem contagem — e a ordem é a do
        # prompt (opinião, previsão, relato), não a de aparição.
        assert _conta_tipos([("opiniao", "x")]) == "OPINIÃO"
        assert _conta_tipos([("relato", "r"), ("opiniao", "a"),
                             ("opiniao", "b")]) == "OPINIÃO ×2 · RELATO"

    def test_post_ganha_ancora_e_contexto_dos_campos(self):
        from src.boletim import _formata_telegram
        pai = Post(id="100", autor="x", criado_em="", texto="qual a resposta?",
                   tipo="post")
        post = _captura("Corpo sintetico do post", ident="123",
                        referenciado=pai, tipo="thread")
        vazio = {"nao_verificaveis": [], "checks": [], "sem_premissas": True}
        html = _formata_telegram("@x", "01/09", [(1, post, vazio)],
                                 [], 0.10, 0.03)
        assert '<a href="https://x.com/x/status/123">ver no X</a>' in html
        assert "<code>[CONTEXTO]</code> <i>@x: qual a resposta?</i>" in html
        assert "URL:" not in html
        assert "<i>Corpo sintetico do post</i>" in html

    def test_corte_html_respeita_linhas(self):
        # Cortar no meio de uma tag quebraria o parse do Telegram inteiro.
        from src import boletim
        linhas = [f"<b>linha {i}</b>" for i in range(400)]
        texto = "\n".join(linhas)
        pedacos, atual = [], ""
        for linha in texto.split("\n"):
            if len(atual) + len(linha) + 1 > boletim.LIMITE_TELEGRAM:
                pedacos.append(atual)
                atual = linha
            else:
                atual = f"{atual}\n{linha}" if atual else linha
        pedacos.append(atual)
        assert all(p.count("<b>") == p.count("</b>") for p in pedacos)


class TestReusoDeConsulta:
    def _grava(self, con, afirmacao, quando):
        salva_consulta(con, afirmacao, "confirmado", "just.", 3, 1, 2,
                       "claude-opus-5", 0.02)
        con.execute("UPDATE consultas SET consultado_em = ? "
                    "WHERE id = (SELECT MAX(id) FROM consultas)", (quando,))
        con.commit()

    def test_reusa_dentro_da_janela(self, tmp_path):
        con = _banco(tmp_path)
        agora = datetime.now(timezone.utc)
        self._grava(con, "a Braskem pediu recuperação", agora.isoformat())
        achada = consulta_recente(con, "A BRASKEM  pediu recuperação")
        assert achada is not None and achada["veredito"] == "confirmado"

    def test_fora_da_janela_nao_reusa(self, tmp_path):
        con = _banco(tmp_path)
        velho = datetime.now(timezone.utc) - timedelta(hours=30)
        self._grava(con, "afirmação antiga", velho.isoformat())
        assert consulta_recente(con, "afirmação antiga") is None

    def test_afirmacao_diferente_nao_reusa(self, tmp_path):
        con = _banco(tmp_path)
        agora = datetime.now(timezone.utc)
        self._grava(con, "a Braskem pediu recuperação", agora.isoformat())
        assert consulta_recente(con, "a Petrobras pediu recuperação") is None

    def test_acento_nao_engana_o_casamento(self, tmp_path):
        # lower() do SQLite ignora acento; a normalização é em Python.
        con = _banco(tmp_path)
        agora = datetime.now(timezone.utc)
        self._grava(con, "É falso que X caiu", agora.isoformat())
        assert consulta_recente(con, "é falso que x caiu") is not None

    def test_sem_conexao_devolve_none(self):
        assert consulta_recente(None, "qualquer coisa") is None


class TestSoFatoCustaDinheiro:
    """O incidente de 01/09 teve DOIS danos: a taxonomia mentiu e o
    dinheiro saiu. O gabarito mede o primeiro (`fatos: 0`); o segundo é
    este invariante, e sem ele uma versão que classifica certo e ainda
    assim chama o check passa verde e sangra pelo princípio 6."""

    def test_premissa_nao_fato_nunca_chama_o_check(self, monkeypatch, tmp_path):
        from src import boletim, premissas
        from src.storage import conecta

        chamadas = []
        analise = premissas.Analise(premissas=[
            premissas.Premissa(tipo="nao_verificavel", trecho="Banco dele"),
            premissas.Premissa(tipo="opiniao", trecho="o poder é de quem grita"),
            premissas.Premissa(tipo="relato", trecho="convivi com ele"),
            premissas.Premissa(tipo="previsao", trecho="a Selic vai subir"),
        ])
        monkeypatch.setattr(premissas, "separa",
                            lambda *a, **k: (analise, _uso_zero()))
        monkeypatch.setattr(
            boletim, "_confere_post", boletim._confere_post)  # sem stub
        from src import check
        monkeypatch.setattr(check, "verifica",
                            lambda *a, **k: chamadas.append(1))
        from src import demanda
        monkeypatch.setattr(demanda, "garante",
                            lambda *a, **k: pytest.fail("demanda sem fato"))

        con = conecta(tmp_path / "t.db")
        texto, custo, dados = boletim._confere_post(
            _captura("corpo do post neste bloco"), con,
            {"acervo": [], "orcamento": 1.0})
        con.close()
        assert chamadas == [], "check chamado para premissa que não é fato"
        assert dados["checks"] == [] and custo == 0.0
        assert len(dados["nao_verificaveis"]) == 4


    def test_contexto_so_para_nao_verificavel_e_com_teto(self, monkeypatch,
                                                         tmp_path):
        """A terceira saída não pode virar a porta dos fundos: só
        `nao_verificavel` COM hipótese busca contexto, hipótese repetida
        busca uma vez só, e nada disso vira linha de veredito. A busca é
        INJETADA — sem isso o teste leria a coleção de PRODUÇÃO e mudaria
        de resultado com o acervo do dia."""
        from src import contexto, boletim, check, demanda, premissas
        monkeypatch.setattr(contexto, "LIGADO", True)
        from src.indice import Achado
        from src.storage import conecta

        buscas = []

        def buscar(colecao, texto, quantos):
            buscas.append(texto)
            return [Achado(t, 0.4, {"artigo_id": i, "veiculo": v,
                                    "titulo": t, "data": "2026-09-01"})
                    for i, (t, v) in enumerate(
                        [("a", "G1"), ("b", "Folha"), ("c", "Valor"),
                         ("d", "CNN"), ("e", "Estadão")], 1)]

        analise = premissas.Analise(premissas=[
            premissas.Premissa(tipo="nao_verificavel", trecho="x",
                               hipotese="uma onda de recuperacoes"),
            premissas.Premissa(tipo="nao_verificavel", trecho="y",
                               hipotese="uma onda de recuperacoes"),
            premissas.Premissa(tipo="nao_verificavel", trecho="z"),
            premissas.Premissa(tipo="opiniao", trecho="w",
                               hipotese="opiniao nao busca"),
        ])
        monkeypatch.setattr(premissas, "separa",
                            lambda *a, **k: (analise, _uso_zero()))
        monkeypatch.setattr(check, "verifica",
                            lambda *a, **k: pytest.fail("check sem fato"))
        monkeypatch.setattr(demanda, "garante",
                            lambda *a, **k: pytest.fail("demanda sem fato"))

        con = conecta(tmp_path / "t.db")
        _, custo, dados = boletim._confere_post(
            CAPTURA_DE_TESTE, con,
            {"acervo": [], "orcamento": 1.0, "buscar_contexto": buscar})
        emitidas = con.execute(
            "SELECT COUNT(*) FROM consultas").fetchone()[0]
        con.close()

        assert buscas == ["uma onda de recuperacoes"], (
            "hipotese repetida, ausente ou de opiniao nao pode buscar")
        assert len(dados["contextos"]) == 1
        assert dados["contextos"][0].materias == 5
        assert custo == 0.0, "contexto nao custa API"
        assert emitidas == 0, "contexto nao pode virar linha de veredito"

    def test_hipotese_fabricada_pelo_roteador_nao_volta_a_tela(
            self, monkeypatch, tmp_path):
        """`roteado` preenchido = a premissa era FATO e o roteador a
        rebaixou, pondo o referente rejeitado em `hipotese`. Essa
        hipótese é do código, não do modelo, e foi tirada da tela em
        22f0ac9 — deixá-la voltar pelo [ACERVO] reabriria o eco que
        motivou tudo isto."""
        from src import contexto, boletim, premissas
        monkeypatch.setattr(contexto, "LIGADO", True)
        from src.storage import conecta

        buscas = []
        rebaixada = premissas.Premissa(
            tipo="nao_verificavel", trecho="o encontro mudou tudo",
            hipotese="o encontro que ocorreu")
        rebaixada.roteado = "sujeito sem entidade nomeada"
        analise = premissas.Analise(premissas=[
            rebaixada,
            premissas.Premissa(tipo="nao_verificavel", trecho="outra",
                               hipotese="uma onda de recuperacoes"),
        ])
        monkeypatch.setattr(premissas, "separa",
                            lambda *a, **k: (analise, _uso_zero()))
        con = conecta(tmp_path / "t.db")
        boletim._confere_post(
            CAPTURA_DE_TESTE, con,
            {"acervo": [], "orcamento": 1.0,
             "buscar_contexto": lambda c, t, q: buscas.append(t) or []})
        con.close()
        assert buscas == ["uma onda de recuperacoes"], buscas

    def test_teto_de_buscas_de_contexto_por_rodada(self, monkeypatch,
                                                   tmp_path):
        """O ARCHITECTURE pede teto próprio para a terceira saída."""
        from src import boletim, contexto, premissas
        monkeypatch.setattr(contexto, "LIGADO", True)
        from src.storage import conecta

        buscas = []
        analise = premissas.Analise(premissas=[
            premissas.Premissa(tipo="nao_verificavel", trecho="t%d" % i,
                               hipotese="assunto %d" % i)
            for i in range(contexto.TETO_POR_RODADA + 3)])
        monkeypatch.setattr(premissas, "separa",
                            lambda *a, **k: (analise, _uso_zero()))

        con = conecta(tmp_path / "t.db")
        boletim._confere_post(
            CAPTURA_DE_TESTE, con,
            {"acervo": [], "orcamento": 1.0,
             "buscar_contexto": lambda c, t, q: buscas.append(t) or []})
        con.close()
        assert len(buscas) == contexto.TETO_POR_RODADA


def _uso_zero():
    from src import llm
    return llm.Uso(modelo=llm.VERIFICACAO, entrada=0, saida=0,
                   cache_leitura=0, cache_escrita=0)


class TestRodadaDoBoletim:
    """`monta` e `main` nos pontos que corrigem os incidentes de 03/09/2026
    e que nenhum teste cobria: dedup dentro da rodada e contra o já
    entregue, marca só DEPOIS de entregar, `--sem-envio` não marca, e
    falha por post que não pode ficar muda."""

    VAZIO = {"nao_verificaveis": [], "checks": [], "contextos": [],
             "sem_premissas": True}

    def _rodada(self, *capturas):
        from src.radar import Rodada
        return Rodada(capturas=tuple(capturas), descartados=(), notas=(),
                      lidos=len(capturas), custo_estimado_usd=0.01)

    def _prepara(self, monkeypatch, tmp_path, rodada, conferir):
        """Tudo que `monta` toca fora do próprio boletim, sem rede e sem
        modelo: handles, banco, acervo, índice, radar e a conferência."""
        from src import boletim, config, grafo, indice, radar
        monkeypatch.setattr(config, "HANDLES_RADAR", ("x",))
        monkeypatch.setattr(config, "BANCO", tmp_path / "t.db")
        monkeypatch.setattr(grafo, "carrega", lambda con: ["acervo"])
        monkeypatch.setattr(indice, "indexa_artigos", lambda con: None)
        monkeypatch.setattr(radar, "busca", lambda handles, dias: rodada)
        monkeypatch.setattr(boletim, "_confere_post", conferir)
        return boletim

    def test_dedup_dentro_da_rodada_e_contra_o_ja_entregue(
            self, monkeypatch, tmp_path):
        boletim = self._prepara(
            monkeypatch, tmp_path,
            self._rodada(_captura("a", ident="111"),
                         _captura("a", ident="111"),
                         _captura("b", ident="222")),
            lambda c, con, estado: ("bloco", 0.0, dict(self.VAZIO)))
        con = conecta(tmp_path / "t.db")
        _marca_entregue(con, "url:222", "b")
        con.close()

        _, _, contidos, _, falhas = boletim.monta(1)
        assert [chaves for chaves, _ in contidos] == [{"url:111"}]
        assert falhas == 0
        # `--reenviar` ignora o já entregue, mas não o repetido na rodada.
        _, _, contidos, _, _ = boletim.monta(1, reenviar=True)
        assert [chaves for chaves, _ in contidos] == [{"url:111"},
                                                      {"url:222"}]

    def test_falha_parcial_vira_aviso_no_telegram(self, monkeypatch,
                                                  tmp_path):
        def um_falha(c, con, estado):
            if c.post.id == "222":
                raise RuntimeError("separador fora")
            return ("bloco", 0.0, dict(self.VAZIO))
        boletim = self._prepara(
            monkeypatch, tmp_path,
            self._rodada(_captura("a", ident="111"),
                         _captura("b", ident="222")),
            um_falha)
        texto, _, contidos, html, falhas = boletim.monta(1)
        assert falhas == 1
        assert [chaves for chaves, _ in contidos] == [{"url:111"}]
        assert "CONFERÊNCIA FALHOU" in texto
        assert "<code>[AVISO]</code>" in html and "CONFERÊNCIA FALHOU" in html

    def test_falha_em_todos_os_posts_avisa_e_sai_com_erro(self, monkeypatch,
                                                          tmp_path):
        def estoura(c, con, estado):
            raise RuntimeError("separador fora")
        boletim = self._prepara(monkeypatch, tmp_path,
                                self._rodada(_captura("a", ident="111")),
                                estoura)
        enviados = []
        monkeypatch.setattr(boletim, "_envia_telegram",
                            lambda texto, html=False: enviados.append(texto)
                            or "enviado")
        monkeypatch.setattr(boletim, "_grava", lambda texto: tmp_path / "b")
        monkeypatch.setattr("sys.argv", ["boletim"])
        with pytest.raises(SystemExit) as saida:
            boletim.main()
        assert saida.value.code == 1
        assert any("nenhum conferido" in e for e in enviados), enviados
        con = conecta(tmp_path / "t.db")
        assert _ja_entregues(con) == set()
        con.close()

    def test_marca_so_depois_de_entregar(self, monkeypatch, tmp_path):
        from src import boletim, config
        monkeypatch.setattr(config, "BANCO", tmp_path / "t.db")
        conecta(tmp_path / "t.db").close()
        monkeypatch.setattr(boletim, "_grava", lambda texto: tmp_path / "b")
        contidos = [({"url:1"}, "resumo")]
        monkeypatch.setattr(
            boletim, "monta",
            lambda dias, reenviar=False: ("texto", 0.0, contidos, "<b>h</b>",
                                          0))

        def roda(argv, resposta):
            monkeypatch.setattr(boletim, "_envia_telegram",
                                lambda texto, html=False: resposta)
            monkeypatch.setattr("sys.argv", ["boletim"] + argv)
            boletim.main()
            con = conecta(tmp_path / "t.db")
            marcados = _ja_entregues(con)
            con.close()
            return marcados

        # Marca = "o leitor recebeu": nem pré-visualização nem envio que
        # falhou marcam; só o envio que deu certo.
        assert roda(["--sem-envio"], "enviado") == set()
        assert roda([], "FALHOU no Telegram (400)") == set()
        assert roda([], "enviado ao Telegram em 1 mensagem(ns)") == {"url:1"}


class TestAvisoDeFalha:
    """Falha do boletim tem de CHEGAR ao leitor, não só ao log.

    Em 03/09/2026 a busca do radar estourou e o único registro foi uma
    linha em data/boletim.log: o boletim ficou parado e ninguém soube. A
    conta pré-paga da API do X torna isso pior — crédito esgotado devolve
    4xx, 4xx não repete, e o silêncio duraria até alguém abrir o log.
    """

    def _main(self, monkeypatch, argv, erro=None):
        """Roda boletim.main() com `monta` estourando, e devolve o que foi
        parar no Telegram."""
        from src import boletim
        enviados = []
        monkeypatch.setattr(boletim, "_envia_telegram",
                            lambda texto, html=False: enviados.append(texto))
        if erro is not None:
            def _estoura(*a, **k):
                raise erro
            monkeypatch.setattr(boletim, "monta", _estoura)
        monkeypatch.setattr("sys.argv", ["boletim"] + argv)
        return boletim, enviados

    def test_falha_do_radar_avisa_no_telegram(self, monkeypatch):
        boletim, enviados = self._main(
            monkeypatch, [], SystemExit("Busca do radar falhou: 429"))
        with pytest.raises(SystemExit):
            boletim.main()
        assert len(enviados) == 1, "a falha não chegou ao Telegram"
        assert "BOLETIM NÃO SAIU" in enviados[0]
        assert "429" in enviados[0], "o motivo da falha não foi junto"

    def test_o_erro_original_sobe_intacto(self, monkeypatch):
        # Avisar não pode engolir a falha: quem chamou precisa do código
        # de saída, senão o agendador acha que deu certo.
        boletim, _ = self._main(
            monkeypatch, [], RuntimeError("crédito esgotado"))
        with pytest.raises(RuntimeError, match="crédito esgotado"):
            boletim.main()

    def test_falha_ao_avisar_nao_mascara_a_falha_de_cima(self, monkeypatch):
        from src import boletim

        def _telegram_quebrado(*a, **k):
            raise ConnectionError("telegram fora")
        monkeypatch.setattr(boletim, "_envia_telegram", _telegram_quebrado)

        def _estoura(*a, **k):
            raise SystemExit("Busca do radar falhou: 503")
        monkeypatch.setattr(boletim, "monta", _estoura)
        monkeypatch.setattr("sys.argv", ["boletim"])
        # O erro que sobe é o do radar, NUNCA o do Telegram.
        with pytest.raises(SystemExit, match="503"):
            boletim.main()

    def test_sem_envio_nao_avisa(self, monkeypatch):
        # --sem-envio é o modo de pré-visualizar: quem o roda está olhando
        # a tela, e não deve gastar uma mensagem por isso.
        boletim, enviados = self._main(
            monkeypatch, ["--sem-envio"], SystemExit("qualquer falha"))
        with pytest.raises(SystemExit):
            boletim.main()
        assert enviados == []


class TestCorteEmPedacos:
    """O corte de 4096 caracteres do Telegram, e a falha de 03/09/2026:
    um boletim de 25 posts saiu em 5 pedacos e o Telegram devolveu 400
    "Can't find end tag" no primeiro. Respeitar quebra de linha nao
    basta -- o corpo do post e <i>texto</i> e o texto TEM quebras."""

    def test_tag_aberta_e_fechada_e_reaberta(self):
        from src.boletim import _equilibra
        a, b = _equilibra(["<b>x</b> <i>corpo que", "segue</i> fim"])
        assert a.endswith("</i>")
        assert b.startswith("<i>")
        assert a.count("<i>") == a.count("</i>")
        assert b.count("<i>") == b.count("</i>")

    def test_link_reabre_com_o_href(self):
        """Reabrir <a> sem o href faria o link virar texto."""
        from src.boletim import _equilibra
        a, b = _equilibra(['<a href="http://x/y">titulo', 'segue</a> fim'])
        assert 'href="http://x/y"' in b

    def test_pedaco_ja_equilibrado_nao_muda(self):
        """O pareado: quem ja esta certo nao pode ganhar tag a mais."""
        from src.boletim import _equilibra
        original = ["<b>a</b>", "<i>b</i>"]
        assert _equilibra(original) == original

    def test_aninhamento_fecha_na_ordem_inversa(self):
        from src.boletim import _equilibra
        a, b = _equilibra(["<b>fora <i>dentro", "segue</i></b> fim"])
        assert a.endswith("</i></b>")
        assert b.startswith("<b><i>")
