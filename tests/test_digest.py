"""Testes do digest.

O que importa testar aqui não é a formatação, é a SEPARAÇÃO: um fato de
veículo único não pode acabar na lista dos confirmados por nenhum caminho.
"""

from src import digest
from src.grafo import Afirmacao


def af(sujeito, relacao, objeto, veiculo, valor=None, unidade=None,
       contexto=None, titulo="matéria"):
    return Afirmacao(
        sujeito=sujeito, relacao=relacao, objeto=objeto, valor=valor,
        unidade=unidade, contexto=contexto, data_fato="2026-08-26",
        origem="EXTRACTED", veiculo=veiculo, titulo=titulo,
        url=f"https://exemplo/{veiculo}/{sujeito}",
    )


class TestFiltroTopicos:
    def test_sem_topicos_devolve_tudo(self):
        afs = [af("Braskem", "outro", "Petrobras", "G1")]
        assert digest.filtra_topicos(afs, []) == afs

    def test_casa_ignorando_acento(self):
        afs = [af("Tribunal Superior Eleitoral", "outro", None, "G1")]
        assert len(digest.filtra_topicos(afs, ["eleitoral"])) == 1

    def test_casa_no_objeto_e_no_titulo(self):
        no_objeto = af("Lula", "outro", "Braskem", "G1")
        no_titulo = af("Lula", "outro", None, "G1", titulo="Braskem recua")
        fora = af("Lula", "outro", "Congresso", "G1")
        achados = digest.filtra_topicos([no_objeto, no_titulo, fora], ["braskem"])
        assert achados == [no_objeto, no_titulo]


class TestSeparacao:
    def test_dois_veiculos_confirmam(self):
        r = digest.recorta([
            af("Braskem", "outro", "Petrobras", "G1"),
            af("Braskem", "outro", "Petrobras", "Folha"),
        ])
        assert len(r.confirmados) == 1
        assert r.unicos == ()

    def test_duas_editorias_do_mesmo_veiculo_nao_confirmam(self):
        """A regra central do projeto: a unidade de corroboração é o veículo.

        Duas matérias da mesma redação sobre o mesmo fato são uma redação
        publicando duas vezes. Contá-las como duas fabricaria confirmação.
        """
        r = digest.recorta([
            af("Braskem", "outro", "Petrobras", "G1", titulo="Política"),
            af("Braskem", "outro", "Petrobras", "G1", titulo="Economia"),
        ])
        assert r.confirmados == ()
        assert len(r.unicos) == 1

    def test_fato_de_fonte_unica_nunca_entra_nos_confirmados(self):
        r = digest.recorta([
            af("Braskem", "outro", "Petrobras", "G1"),
            af("Braskem", "outro", "Petrobras", "Folha"),
            af("Braskem", "preve", None, "G1", valor=90.0, unidade="dias"),
        ])
        chaves_confirmadas = {c.chave for c in r.confirmados}
        chaves_unicas = {c.chave for c in r.unicos}
        assert not (chaves_confirmadas & chaves_unicas)
        assert len(r.unicos) == 1


class TestTaxaConfirmacao:
    def test_acervo_vazio_nao_divide_por_zero(self):
        assert digest.recorta([]).taxa_confirmacao == 0.0

    def test_metade_confirmada(self):
        r = digest.recorta([
            af("A", "outro", "B", "G1"),
            af("A", "outro", "B", "Folha"),
            af("C", "outro", "D", "G1"),
        ])
        assert r.taxa_confirmacao == 0.5
