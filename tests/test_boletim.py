"""As partes do boletim e da guarda de reuso que não dependem de rede.

O estado 'já entregue' impede o boletim de repetir posts entre rodadas; a
guarda de reuso do check impede pagar duas vezes pela mesma afirmação — 29%
do gasto de consulta medido em 31/08/2026 era repetição.
"""

from datetime import datetime, timedelta, timezone

from src.boletim import _hash_post, _ja_entregues, _marca_entregue
from src.check import consulta_recente
from src.storage import conecta, salva_consulta


def _banco(tmp_path):
    return conecta(tmp_path / "t.db")


class TestEstadoDoBoletim:
    def test_hash_estavel_a_espacos_e_caixa(self):
        assert _hash_post("Bitcoin  a 80 mil") == _hash_post("bitcoin a 80 MIL")

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
