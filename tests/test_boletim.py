"""As partes do boletim e da guarda de reuso que não dependem de rede.

O estado 'já entregue' impede o boletim de repetir posts entre rodadas; a
guarda de reuso do check impede pagar duas vezes pela mesma afirmação — 29%
do gasto de consulta medido em 31/08/2026 era repetição.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.boletim import _hash_post, _ja_entregues, _marca_entregue
from src.check import consulta_recente
from src.storage import conecta, salva_consulta


POST_DE_TESTE = "POST 1 (@x, 01 Sep 2026):\nqualquer coisa."


def _banco(tmp_path):
    return conecta(tmp_path / "t.db")


class TestEstadoDoBoletim:
    def test_hash_estavel_a_espacos_e_caixa(self):
        assert _hash_post("Bitcoin  a 80 mil") == _hash_post("bitcoin a 80 MIL")

    def test_hash_ignora_o_numero_do_cabecalho(self):
        # O N muda a cada rodada: com o cabeçalho no hash, o mesmo post
        # voltava como "inédito" na rodada seguinte.
        a = _hash_post("POST 1 (@x, 30 Aug 2026):\ntexto do post")
        b = _hash_post("POST 7 (@x, 31 Aug 2026):\ntexto do post")
        assert a == b == _hash_post("texto do post")

    def test_hash_ignora_url_e_contexto_de_resposta(self):
        com = _hash_post("POST 1 (@x, data):\n"
                         "URL: https://x.com/x/status/123\n"
                         "EM RESPOSTA A (@y): pergunta\n"
                         "texto do post")
        sem = _hash_post("texto do post")
        assert com == sem

    def test_chave_de_url_so_com_citacao_que_confere(self):
        from src.boletim import _chaves_do_post
        post = ("POST 1 (@x, data):\nURL: https://x.com/x/status/123\n"
                "texto")
        # x.com/i/status/N e x.com/handle/status/N são o mesmo status.
        chaves = _chaves_do_post(post, ("https://x.com/i/status/123",))
        assert "url:123" in chaves and _hash_post(post) in chaves
        # URL alegada fora das citações não vira identidade.
        chaves = _chaves_do_post(post, ("https://x.com/i/status/999",))
        assert chaves == {_hash_post(post)}

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
        html = _formata_telegram("@x", "01/09", [(1, "post", {
            "nao_verificaveis": [("opiniao", "opinou algo")],
            "checks": [{"afirmacao": "o IPCA foi 5,2%",
                        "veredito": "sem_evidencia", "justificativa": "",
                        "veiculos": 0, "custo": 0.02, "evidencias": []}],
            "sem_premissas": False,
        }, None)], [], [], 0.10, 0.03)
        assert "<b>[SEM EVIDÊNCIA]</b>" in html
        assert "<code>[OPINIÃO]</code>" in html
        assert "<b>[1]</b>" in html

    def test_confirmado_traz_fonte_clicavel(self):
        from src.boletim import _formata_telegram
        html = _formata_telegram("@x", "01/09", [(1, "post", {
            "nao_verificaveis": [],
            "checks": [{"afirmacao": "a Caixa lucrou",
                        "veredito": "confirmado", "justificativa": "bate",
                        "veiculos": 2, "custo": 0.02,
                        "evidencias": [("G1", "http://g1/x")]}],
            "sem_premissas": False,
        }, None)], [], ["https://x.com/i/status/12345"], 0.10, 0.03)
        assert "<b>[CONFIRMADO]</b>" in html
        assert "<code>[EVIDÊNCIA]</code>" in html
        assert '<a href="http://g1/x">G1</a>' in html
        # Sem par com o post, o link vai para o rodapé de sobras — com o
        # fim do ID como texto, nunca um número que prometa ordem.
        assert '<a href="https://x.com/i/status/12345">…12345</a>' in html
        assert "sem par" in html

    def test_nota_de_demanda_aparece(self):
        from src.boletim import _formata_telegram
        html = _formata_telegram("@x", "01/09", [(1, "post", {
            "nao_verificaveis": [],
            "checks": [{"afirmacao": "a", "veredito": "confirmado",
                        "justificativa": "ok", "veiculos": 2, "custo": 0.02,
                        "evidencias": [("G1", "http://g1/x")],
                        "demanda": "2 matéria(s) extraída(s) na hora"}],
            "sem_premissas": False,
        }, None)], [], [], 0.10, 0.03)
        assert "<code>[DEMANDA]</code>" in html

    def test_sem_emoji_na_rendicao(self):
        # Pedido de 01/09/2026: etiquetas textuais no lugar de emoji.
        from src.boletim import _formata_telegram
        html = _formata_telegram("@x", "01/09", [(1, "post", {
            "nao_verificaveis": [("relato", "r"), ("previsao", "p")],
            "checks": [{"afirmacao": "a", "veredito": "sem_evidencia",
                        "justificativa": "", "veiculos": 0, "custo": 0,
                        "evidencias": []}],
            "sem_premissas": False,
        }, None)], ["nota da busca"], ["https://x.com/i/status/9"],
            0.10, 0.03)
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
        post = ("POST 1 (@x, 01 Sep 2026):\n"
                "Charada: André se reune com Trump, todos os rumos mudam.")
        html = _formata_telegram("@x", "02/09", [(1, post, {
            "nao_verificaveis": [
                ("opiniao", "todos os rumos mudam"),
                ("relato", "convivi com ele"),
                ("opiniao", "André se reune com Trump")],
            "checks": [], "sem_premissas": False,
        }, None)], [], [], 0.10, 0.03)
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

    def test_post_com_url_validada_ganha_ancora_e_contexto(self):
        from src.boletim import _formata_telegram
        post = ("POST 3 (@x, 01 Sep 2026):\n"
                "URL: https://x.com/x/status/123\n"
                "EM RESPOSTA A (@y): qual a resposta?\n"
                "Expansão")
        vazio = {"nao_verificaveis": [], "checks": [], "sem_premissas": True}
        html = _formata_telegram(
            "@x", "01/09", [(1, post, vazio, "https://x.com/x/status/123")],
            [], ["https://x.com/i/status/123"], 0.10, 0.03)
        assert '<a href="https://x.com/x/status/123">ver no X</a>' in html
        assert ("<code>[CONTEXTO]</code> "
                "<i>EM RESPOSTA A (@y): qual a resposta?</i>") in html
        # A linha URL: não aparece no corpo, e o link pareado não repete
        # no rodapé de sobras.
        assert "URL:" not in html
        assert "também lidos" not in html
        assert "<i>Expansão</i>" in html

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
            "POST 1 (@x, 01 Sep 2026):\nO cara tem banco dele.", con,
            {"acervo": [], "orcamento": 1.0})
        con.close()
        assert chamadas == [], "check chamado para premissa que não é fato"
        assert dados["checks"] == [] and custo == 0.0
        assert len(dados["nao_verificaveis"]) == 4


    def test_contexto_so_para_nao_verificavel_e_com_teto(self, monkeypatch,
                                                         tmp_path):
        """A quarta saída não pode virar a porta dos fundos: só
        `nao_verificavel` COM hipótese busca contexto, hipótese repetida
        busca uma vez só, e nada disso vira linha de veredito. A busca é
        INJETADA — sem isso o teste leria a coleção de PRODUÇÃO e mudaria
        de resultado com o acervo do dia."""
        from src import boletim, check, demanda, premissas
        from src.indice import Achado
        from src.storage import conecta

        buscas = []

        def buscar(colecao, texto, quantos):
            buscas.append(texto)
            return [Achado(t, 0.4, {"artigo_id": i, "veiculo": v,
                                    "titulo": t, "data": "2026-09-01"})
                    for i, (t, v) in enumerate(
                        [("a", "G1"), ("b", "Folha"), ("c", "Valor")], 1)]

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
            POST_DE_TESTE, con,
            {"acervo": [], "orcamento": 1.0, "buscar_contexto": buscar})
        emitidas = con.execute(
            "SELECT COUNT(*) FROM consultas").fetchone()[0]
        con.close()

        assert buscas == ["uma onda de recuperacoes"], (
            "hipotese repetida, ausente ou de opiniao nao pode buscar")
        assert len(dados["contextos"]) == 1
        assert dados["contextos"][0].materias == 3
        assert custo == 0.0, "contexto nao custa API"
        assert emitidas == 0, "contexto nao pode virar linha de veredito"

    def test_hipotese_fabricada_pelo_roteador_nao_volta_a_tela(
            self, monkeypatch, tmp_path):
        """`roteado` preenchido = a premissa era FATO e o roteador a
        rebaixou, pondo o referente rejeitado em `hipotese`. Essa
        hipótese é do código, não do modelo, e foi tirada da tela em
        22f0ac9 — deixá-la voltar pelo [ACERVO] reabriria o eco que
        motivou tudo isto."""
        from src import boletim, premissas
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
            POST_DE_TESTE, con,
            {"acervo": [], "orcamento": 1.0,
             "buscar_contexto": lambda c, t, q: buscas.append(t) or []})
        con.close()
        assert buscas == ["uma onda de recuperacoes"], buscas

    def test_teto_de_buscas_de_contexto_por_rodada(self, monkeypatch,
                                                   tmp_path):
        """O ARCHITECTURE pede teto próprio para a quarta saída."""
        from src import boletim, contexto, premissas
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
            POST_DE_TESTE, con,
            {"acervo": [], "orcamento": 1.0,
             "buscar_contexto": lambda c, t, q: buscas.append(t) or []})
        con.close()
        assert len(buscas) == contexto.TETO_POR_RODADA


def _uso_zero():
    from src import llm
    return llm.Uso(modelo=llm.VERIFICACAO, entrada=0, saida=0,
                   cache_leitura=0, cache_escrita=0)
