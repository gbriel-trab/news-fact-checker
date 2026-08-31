"""A chave canônica funde variação de grafia sem fundir entidade distinta.

O caso que motivou está medido no acervo: "Braskem" e "Braskem S.A." somavam
100 triplas como duas entidades, e o grafo não via o caso mais denso dele.
O contra-caso é tão importante quanto: "Braskem Idesa" é subsidiária, e
fundi-la com a controladora fabricaria corroboração — o pior erro.
"""

from src.canonico import APELIDOS, chave_canonica


class TestFusaoLegitima:
    def test_sufixo_societario(self):
        assert chave_canonica("Braskem S.A.") == chave_canonica("Braskem")

    def test_sufixo_com_barra(self):
        assert chave_canonica("Vale S/A") == chave_canonica("Vale")

    def test_ltda(self):
        assert chave_canonica("Empresa X Ltda.") == chave_canonica("Empresa X")

    def test_caixa_e_acento(self):
        assert chave_canonica("PETROBRÁS") == chave_canonica("petrobras")

    def test_espacos_colapsam(self):
        assert chave_canonica("Banco  Central") == chave_canonica("Banco Central")

    def test_apelido_curado(self):
        assert (chave_canonica("Estados Unidos da América")
                == chave_canonica("Estados Unidos"))


class TestNaoFusao:
    def test_subsidiaria_nao_e_a_controladora(self):
        assert chave_canonica("Braskem Idesa") != chave_canonica("Braskem")

    def test_nomes_que_se_contem_ficam_separados(self):
        assert (chave_canonica("Banco Central do Brasil")
                != chave_canonica("Banco do Brasil"))

    def test_sufixo_so_como_token_proprio(self):
        # "Casa" termina em "sa" mas não carrega sufixo societário.
        assert chave_canonica("Casa") == "casa"

    def test_sobrenome_sa_nao_e_sufixo(self):
        # Sem acento, "Sá" vira "sa" — e o token nu não pode ser amputado,
        # senão pessoas distintas fundem (achado da revisão de 29/08/2026).
        assert chave_canonica("Fernando Sá") != chave_canonica("Fernando")
        assert chave_canonica("Estácio de Sá") == "estacio de sa"

    def test_sa_sem_pontuacao_fica(self):
        # Perda aceita na direção segura: sem ponto ou barra, não é sufixo.
        assert chave_canonica("Empresa SA") == "empresa sa"


class TestNoAgrupamento:
    """O ponto onde a chave trabalha de verdade: duas grafias da mesma
    entidade, vindas de veículos diferentes, têm que virar UM fato — era o
    caso Braskem/Braskem S.A., 100 triplas sem se encontrar."""

    @staticmethod
    def _afirmacao(sujeito, veiculo):
        from src.grafo import Afirmacao
        return Afirmacao(
            sujeito=sujeito, relacao="solicitou",
            objeto="Recuperação extrajudicial da Braskem", valor=None,
            unidade=None, contexto=None, data_fato="2026-08-26",
            origem="EXTRACTED", veiculo=veiculo, titulo="t",
            url=f"https://x/{veiculo}",
        )

    def test_grafias_diferentes_confirmam(self):
        from src import grafo
        grupos = grafo.agrupa([
            self._afirmacao("Braskem", "G1"),
            self._afirmacao("Braskem S.A.", "InfoMoney"),
        ])
        assert len(grupos) == 1
        assert grupos[0].confirmada

    def test_exibicao_preserva_a_grafia_original(self):
        from src import grafo
        grupos = grafo.agrupa([
            self._afirmacao("Braskem S.A.", "G1"),
            self._afirmacao("Braskem", "InfoMoney"),
        ])
        assert grupos[0].sujeito == "Braskem S.A."

    def test_subsidiaria_nao_confirma_a_controladora(self):
        from src import grafo
        grupos = grafo.agrupa([
            self._afirmacao("Braskem", "G1"),
            self._afirmacao("Braskem Idesa", "InfoMoney"),
        ])
        assert len(grupos) == 2
        assert not any(g.confirmada for g in grupos)


class TestContrato:
    def test_idempotente(self):
        vez = chave_canonica("Braskem S.A.")
        assert chave_canonica(vez) == vez

    def test_apelidos_ja_normalizados(self):
        # O mapa é consultado DEPOIS da normalização; chave ou valor fora da
        # forma normalizada nunca casaria e viraria apelido morto. A garantia
        # certa é: canonizar a CHAVE tem que desembocar no VALOR (falha se a
        # chave não estiver normalizada), e o valor é ponto fixo.
        for chave, valor in APELIDOS.items():
            assert chave_canonica(chave) == valor, (
                f"apelido {chave!r} não está na forma normalizada — nunca "
                f"casaria com nada")
            assert chave_canonica(valor) == valor, (
                f"destino {valor!r} não está na forma normalizada")
