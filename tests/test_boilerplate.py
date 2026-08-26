"""Testes do filtro de texto institucional.

Este filtro corta antes de o texto chegar ao modelo, então erro dele não
aparece como erro: aparece como fato que nunca foi extraído. Por isso o corte
é conservador e o que sai é reportado na rodada.
"""

import sqlite3

import pytest

from src.boilerplate import filtra, frases_repetidas, tem_marcador
from src.storage import conecta, salva
from tests.test_storage import artigo


class TestMarcador:
    def test_reconhece_bloco_de_link(self):
        assert tem_marcador("Leia também Senado aprova projeto que libera bingos")
        assert tem_marcador("Veja mais sobre o assunto na editoria de política")
        assert tem_marcador("Clique aqui para acessar o Agregador de Pesquisas")

    def test_ignora_maiusculas_e_espacos(self):
        assert tem_marcador("  LEIA MAIS   Café registra novas altas em NY")

    def test_nao_corta_noticia(self):
        assert not tem_marcador(
            "O ministro assinou o decreto na manhã desta terça-feira.")
        assert not tem_marcador(
            "A notícia da sua morte foi compartilhada pela família.")

    def test_marcador_no_meio_nao_conta(self):
        """Só o começo denuncia bloco de navegação; no meio pode ser notícia."""
        assert not tem_marcador(
            "O relator pediu que os senadores leiam também o parecer anexo.")


def _banco(tmp_path, materias):
    con = conecta(tmp_path / "b.db")
    for i, (texto, dia) in enumerate(materias):
        art = artigo(url=f"https://v.com/{i}", conteudo=texto)
        object.__setattr__(art, "data_publicacao", f"{dia}T10:00:00+00:00")
        salva(con, art)
    return con


def segmentar(texto):
    return [f.strip() for f in texto.split("|") if f.strip()]


PROMO = "Assinantes recebem nossa newsletter diária no e-mail cadastrado"
CORPO = "Texto suficientemente longo para a materia passar do limite minimo " * 12


class TestRepeticao:
    def test_frase_em_varias_materias_e_varios_dias_e_institucional(self, tmp_path):
        con = _banco(tmp_path, [
            (f"{CORPO}|Notícia {i} do dia|{PROMO}", f"2026-08-{20 + i % 3:02d}")
            for i in range(10)
        ])
        assert PROMO.lower() in frases_repetidas(con, "Veículo", segmentar)
        con.close()

    def test_cobertura_do_mesmo_fato_no_mesmo_dia_nao_e_institucional(self, tmp_path):
        """Caso real: quatro matérias da CNN sobre a mesma morte repetiam o
        mesmo parágrafo, no mesmo dia. Sem a exigência de datas distintas, o
        parágrafo seria cortado como rodapé."""
        repetida = "A família confirmou a morte nas redes sociais"
        con = _banco(tmp_path, [
            (f"{CORPO}|Detalhe {i} da cobertura|{repetida}", "2026-08-25")
            for i in range(10)
        ])
        assert repetida.lower() not in frases_repetidas(con, "Veículo", segmentar)
        con.close()

    def test_veiculo_com_poucas_materias_nao_produz_regra(self, tmp_path):
        """Com amostra pequena, frase legítima repetida viraria 'institucional'."""
        con = _banco(tmp_path, [
            (f"{CORPO}|{PROMO}", f"2026-08-{20 + i:02d}") for i in range(4)
        ])
        assert frases_repetidas(con, "Veículo", segmentar) == set()
        con.close()


class TestFiltra:
    def test_separa_e_devolve_as_duas_listas(self):
        frases = ["O ministro assinou o decreto.", "Leia também outra matéria",
                  "A oposição criticou a decisão."]
        limpas, removidas = filtra(frases, set())
        assert len(limpas) == 2 and len(removidas) == 1

    def test_corta_pela_lista_de_repetidas(self):
        limpas, removidas = filtra(
            ["Notícia real aqui.", "Assine nosso plano"],
            {"assine nosso plano"})
        assert limpas == ["Notícia real aqui."]
        assert removidas == ["Assine nosso plano"]

    def test_sem_repetidas_e_sem_marcador_nao_corta_nada(self):
        frases = ["Primeira afirmação do texto.", "Segunda afirmação do texto."]
        limpas, removidas = filtra(frases, set())
        assert limpas == frases and removidas == []
