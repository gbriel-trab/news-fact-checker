"""Teto diário de extração (06/09/2026): o dobro da média medida no
livro-caixa, conferido antes de cada chamada paga, nos três caminhos —
lote por história, lote por matéria e demanda."""

from datetime import datetime, timedelta, timezone

import pytest

from src import demanda, extract
from src.storage import conecta


_seq = iter(range(1, 10_000))


def _linha(con, custo, quando):
    # artigo_id distinto por linha: (artigo, modelo, prompt) é UNIQUE.
    con.execute(
        "INSERT INTO extracoes (artigo_id, modelo, prompt_versao, "
        "vocab_versao, tokens_entrada, tokens_saida, custo_usd, extraido_em) "
        "VALUES (?, 'm', 'v', 1, 0, 0, ?, ?)", (next(_seq), custo, quando))
    con.commit()


def _agora():
    return datetime.now(timezone.utc)


class TestGastoDeHoje:
    def test_soma_so_o_dia_utc_corrente(self, tmp_path):
        con = conecta(tmp_path / "t.db")
        hoje = _agora().replace(hour=0, minute=0, second=1).isoformat()
        ontem = (_agora() - timedelta(days=1)).isoformat()
        _linha(con, 0.40, hoje)
        _linha(con, 0.30, _agora().isoformat())
        _linha(con, 5.00, ontem)
        assert extract.gasto_hoje_usd(con) == pytest.approx(0.70)

    def test_teto_e_o_dobro_da_media_medida(self):
        # US$ 13,80 em 12 dias corridos (26/08 a 06/09/2026) = 1,15/dia.
        assert extract.TETO_DIARIO_USD == pytest.approx(2 * 1.15, abs=0.01)


class TestConfereTeto:
    def test_abaixo_do_teto_passa_e_devolve_o_gasto(self, tmp_path):
        con = conecta(tmp_path / "t.db")
        _linha(con, 1.00, _agora().isoformat())
        assert extract.confere_teto_diario(con, teto=2.30) == pytest.approx(1.0)

    def test_no_teto_ou_acima_levanta(self, tmp_path):
        con = conecta(tmp_path / "t.db")
        _linha(con, 2.30, _agora().isoformat())
        with pytest.raises(extract.TetoDiario) as erro:
            extract.confere_teto_diario(con, teto=2.30)
        assert "2.30" in str(erro.value) and "meia-noite UTC" in str(erro.value)

    def test_extrai_grupo_confere_antes_de_pagar(self, tmp_path, monkeypatch):
        con = conecta(tmp_path / "t.db")
        _linha(con, 9.99, _agora().isoformat())
        monkeypatch.setattr(extract, "extrai_historia",
                            lambda *a: pytest.fail("pagou acima do teto"))
        with pytest.raises(extract.TetoDiario):
            extract.extrai_grupo(con, [{"veiculo": "G1", "conteudo": "x",
                                        "resumo": "", "titulo": "t"}])


class TestDemandaRespeitaOTetoDiario:
    def test_vira_motivo_teto_diario_sem_gastar(self, monkeypatch):
        monkeypatch.setattr(demanda, "candidatas", lambda c, t, r="", q="": ["m"])

        def estoura(*a, **k):
            raise extract.TetoDiario("hoje já deu")

        monkeypatch.setattr(demanda.extract, "extrai_grupo", estoura)
        r = demanda.garante(None, "x")
        assert r.motivo == "teto_diario" and r.custo == 0 and r.materias == 0
