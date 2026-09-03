"""Testes do minerador de apelidos.

O que se prende aqui é a fronteira entre encurtar um nome e trocar de
entidade. A mineração de 03/09/2026 mostrou que corroboração NÃO protege
disso: "presidente dos estados unidos" -> "estados unidos" tinha seis
veículos, porque todos os veículos escrevem assim. Quem separa é a forma
do par, e é código.
"""

import json

from src import apelidos
from src.apelidos import (_e_sigla, _resolve_cadeias, candidatos,
                          equivalentes, forma_de_apelido)


class TestFormaDeApelido:
    def test_encurtar_e_apelido(self):
        assert forma_de_apelido("correios",
                                "empresa brasileira de correios e telegrafos")
        assert forma_de_apelido("lula", "luiz inacio lula da silva")
        assert forma_de_apelido("senado", "senado federal")

    def test_sigla_e_apelido_nos_dois_sentidos(self):
        assert forma_de_apelido("mdb", "movimento democratico brasileiro")
        assert forma_de_apelido("movimento democratico brasileiro", "mdb")
        assert _e_sigla("psd", "partido social democratico")
        assert not _e_sigla("psd", "partido dos trabalhadores")

    def test_parentese_nao_atrapalha(self):
        assert forma_de_apelido("tse (tribunal superior eleitoral)",
                                "tribunal superior eleitoral")

    def test_cabeca_troca_o_referente(self):
        """Os dois que passaram pelas outras peneiras com 6 veículos."""
        assert not forma_de_apelido("presidente dos estados unidos",
                                    "estados unidos")
        assert not forma_de_apelido("o perfil de nicolas maduro",
                                    "nicolas maduro")
        assert not forma_de_apelido("campanha do presidente lula", "lula")
        assert not forma_de_apelido("renuncia ao cargo de ministro",
                                    "ministro")

    def test_sem_contencao_nem_sigla_recusa(self):
        assert not forma_de_apelido("manutencao do texto da pec",
                                    "pec do fim da escala 6x1")
        assert not forma_de_apelido("", "lula")


class TestPeneiras:
    def _banco(self, tmp_path, linhas):
        """linhas: (forma, canonico, veiculo, origem)."""
        from src.storage import conecta
        con = conecta(tmp_path / "t.db")
        for i, (forma, canonico, veiculo, origem) in enumerate(linhas, 1):
            con.execute(
                "INSERT INTO artigos (id, url_norm, url_original, veiculo, "
                "editoria, titulo, resumo, conteudo, hash_conteudo, "
                "coletado_em) VALUES (?,?,?,?,'x','t','r','c',?,'hoje')",
                (i, f"u{i}", f"u{i}", veiculo, f"h{i}"))
            con.execute(
                "INSERT INTO extracoes (id, artigo_id, modelo, prompt_versao, "
                "vocab_versao, tokens_entrada, tokens_saida, custo_usd, "
                "extraido_em) VALUES (?,?,'m','v',1,0,0,0.0,'t')", (i, i))
            con.execute(
                "INSERT INTO triplas (extracao_id, sentenca, sujeito, "
                "sujeito_canonico, relacao, tipo_relacao, origem) "
                "VALUES (?,0,?,?,'afirmou','evento',?)",
                (i, forma, canonico, origem))
        con.commit()
        return con

    def test_inferred_fica_de_fora(self, tmp_path):
        """A regra 1 da extração manda marcar apelido resolvido como
        INFERRED. Minerar o INFERRED seria minerar o palpite do modelo."""
        con = self._banco(tmp_path, [
            ("Lula", "Luiz Inácio Lula da Silva", "G1", "INFERRED"),
            ("Lula", "Luiz Inácio Lula da Silva", "Folha", "INFERRED"),
        ])
        aprovados, _ = candidatos(con)
        assert aprovados == []

    def test_um_veiculo_nao_basta(self, tmp_path):
        con = self._banco(tmp_path, [
            ("Lula", "Luiz Inácio Lula da Silva", "G1", "EXTRACTED"),
            ("Lula", "Luiz Inácio Lula da Silva", "G1", "EXTRACTED"),
        ])
        aprovados, _ = candidatos(con)
        assert aprovados == []

    def test_dois_veiculos_aprovam(self, tmp_path):
        con = self._banco(tmp_path, [
            ("Lula", "Luiz Inácio Lula da Silva", "G1", "EXTRACTED"),
            ("Lula", "Luiz Inácio Lula da Silva", "Folha", "EXTRACTED"),
        ])
        aprovados, _ = candidatos(con)
        assert [(c, l) for c, l, *_ in aprovados] == [
            ("lula", "luiz inacio lula da silva")]

    def test_ambiguidade_recusa_e_aparece_na_lista(self, tmp_path):
        """O filho: "Lula" também casa com "Fábio Luís Lula da Silva"."""
        con = self._banco(tmp_path, [
            ("Lula", "Luiz Inácio Lula da Silva", "G1", "EXTRACTED"),
            ("Lula", "Luiz Inácio Lula da Silva", "Folha", "EXTRACTED"),
            ("Lula", "Fábio Luís Lula da Silva", "Valor", "EXTRACTED"),
            ("Lula", "Fábio Luís Lula da Silva", "Estadao", "EXTRACTED"),
        ])
        aprovados, ambiguos = candidatos(con)
        assert aprovados == []
        assert [c for c, _ in ambiguos] == ["lula"]

    def test_forma_generica_recusa(self, tmp_path):
        con = self._banco(tmp_path, [
            ("a proposta", "PEC do fim da escala 6x1", "G1", "EXTRACTED"),
            ("a proposta", "PEC do fim da escala 6x1", "Folha", "EXTRACTED"),
        ])
        assert candidatos(con)[0] == []

    def test_forma_autonoma_e_sinalizada(self, tmp_path):
        con = self._banco(tmp_path, [
            ("Senado", "Senado Federal", "G1", "EXTRACTED"),
            ("Senado", "Senado Federal", "Folha", "EXTRACTED"),
            ("Senado", "Senado", "Valor", "EXTRACTED"),
        ])
        assert "senado" in apelidos.autonomas(con)


class TestCadeiasEEquivalentes:
    def test_cadeia_resolve_ate_o_fim(self):
        assert _resolve_cadeias({"a": "b", "b": "c"}) == {"a": "c", "b": "c"}

    def test_ciclo_nao_gira(self):
        assert _resolve_cadeias({"a": "b", "b": "a"}) is not None

    def test_equivalentes_vai_e_volta(self):
        mapa = {"lula": "luiz inacio lula da silva"}
        assert equivalentes("Lula", mapa) == {
            "lula", "luiz inacio lula da silva"}
        assert equivalentes("Luiz Inácio Lula da Silva", mapa) == {
            "lula", "luiz inacio lula da silva"}
        assert equivalentes("Trump", mapa) == {"trump"}


class TestPromocao:
    def test_promove_grava_e_carrega(self, tmp_path, monkeypatch):
        arquivo = tmp_path / "apelidos.json"
        monkeypatch.setattr(apelidos, "ARQUIVO_APELIDOS", arquivo)
        assert apelidos.promove({"lula": "luiz inacio lula da silva"},
                                "gabriel") == 1
        assert apelidos.carrega_promovidos() == {
            "lula": "luiz inacio lula da silva"}
        assert json.loads(arquivo.read_text(encoding="utf-8"))[
            "promovido_por"] == "gabriel"
        # Promover de novo não duplica nem conta.
        assert apelidos.promove({"lula": "luiz inacio lula da silva"},
                                "gabriel") == 0

    def test_arquivo_ausente_nao_derruba(self, tmp_path, monkeypatch):
        monkeypatch.setattr(apelidos, "ARQUIVO_APELIDOS", tmp_path / "nao.json")
        assert apelidos.carrega_promovidos() == {}


class TestCanonicoEChecagem:
    def test_apelido_promovido_muda_a_chave_e_a_assinatura(self, monkeypatch):
        from src import canonico
        antes = canonico.assinatura_apelidos()
        monkeypatch.setitem(canonico.APELIDOS, "lula",
                            "luiz inacio lula da silva")
        canonico.chave_canonica.cache_clear()
        assert canonico.chave_canonica("Lula") == "luiz inacio lula da silva"
        assert canonico.assinatura_apelidos() != antes
        canonico.chave_canonica.cache_clear()

    def test_assinatura_entra_na_versao_do_check(self):
        """Apelido muda o que a rota por chave recupera e o que o freio
        considera o mesmo sujeito: veredito de mapas diferentes não é
        comparável, e o gabarito precisa saber sob qual rodou."""
        import inspect
        from src import check
        assert "assinatura_apelidos" in inspect.getsource(check.versao_prompt)

    def test_mapa_vivo_nao_relaxa_o_freio(self, monkeypatch):
        """Apelido FUNDADO mas não promovido só amplia RECUPERAÇÃO. O freio
        compara por chave_canonica, que só conhece os promovidos — sigla é
        o caso puro, porque contenção de tokens não a resolve."""
        from src import canonico
        from src.check import sujeito_casa
        assert not sujeito_casa("BC", "Banco Central do Brasil")
        # Estar no mapa vivo não muda nada; só a promoção muda.
        monkeypatch.setattr(apelidos, "_mapa_vivo_cache",
                            lambda *a: {"bc": "banco central do brasil"})
        assert not sujeito_casa("BC", "Banco Central do Brasil")
        monkeypatch.setitem(canonico.APELIDOS, "bc", "banco central do brasil")
        canonico.chave_canonica.cache_clear()
        assert sujeito_casa("BC", "Banco Central do Brasil")
        canonico.chave_canonica.cache_clear()

    def test_contencao_ja_resolvia_o_nome_parcial(self):
        """Nome parcial não dependia de apelido: a contenção do freio já o
        aceitava. O apelido serve à rota por CHAVE, que exige igualdade."""
        from src.check import sujeito_casa
        assert sujeito_casa("Lula", "Luiz Inácio Lula da Silva")
        assert (canonico_chave("Lula") != canonico_chave(
            "Luiz Inácio Lula da Silva"))


def canonico_chave(nome):
    from src.canonico import chave_canonica
    return chave_canonica(nome)
