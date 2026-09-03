"""A quarta saída: contexto quando não há premissa para conferir.

Todo teste INJETA a busca. `indice.DIR_INDICE` é global e aponta para a
coleção de produção — teste que não injeta lê o acervo do dia e muda de
resultado sozinho, o que é pior que teste que falha.
"""

from src import contexto
from src.indice import Achado


def achado(titulo, veiculo, prox, artigo_id, data="2026-09-01"):
    """`proximidade` é 1 - distancia/2, então a distância é (1-p)*2."""
    return Achado(titulo, (1 - prox) * 2,
                  {"artigo_id": artigo_id, "veiculo": veiculo,
                   "titulo": titulo, "data": data})


def busca_de(achados):
    return lambda colecao, texto, quantos: achados


class TestLimiar:
    def test_positivo_assunto_coberto_vira_contexto(self):
        """O caso que motivou a saída: 3 matérias, 3 veículos, acima do
        limiar. Barreira nova entra com caso positivo pareado."""
        c = contexto.do_assunto("uma onda de recuperações judiciais",
                                busca_de([
            achado("Habib's entra em recuperação", "G1", 0.81, 1),
            achado("Braskem tem RJ aprovada", "Folha", 0.79, 2,
                   "2026-08-26"),
            achado("Casas Bahia negocia dívida", "Valor", 0.77, 3),
        ]))
        assert c is not None
        assert c.materias == 3
        assert c.veiculos == ["Folha", "G1", "Valor"]
        assert c.de == "2026-08-26" and c.ate == "2026-09-01"
        assert contexto.linha(c) == (
            "o acervo registra 3 matérias em 3 veículos "
            "(2026-08-26 a 2026-09-01)")

    def test_negativo_abaixo_do_limiar_nao_conta(self):
        """A 0.70 uma consulta sobre trigo no Cazaquistão trazia 34
        matérias do acervo. O limiar é o que separa assunto de ruído."""
        assert contexto.do_assunto("x", busca_de([
            achado("a", "G1", 0.74, 1), achado("b", "Folha", 0.70, 2),
            achado("c", "Valor", 0.60, 3)])) is None

    def test_um_veiculo_so_nao_e_acervo_cobrindo_assunto(self):
        """A segunda hipótese do C3 trazia UMA matéria, sobre um
        empresário preso por homicídio — coincidência de vocabulário.
        Corroboração neste projeto sempre se conta por veículo."""
        assert contexto.do_assunto("um empresário não identificado",
                                   busca_de([
            achado("a", "G1", 0.80, 1), achado("b", "G1", 0.79, 2),
            achado("c", "G1", 0.78, 3)])) is None

    def test_poucas_materias_nao_bastam(self):
        assert contexto.do_assunto("x", busca_de([
            achado("a", "G1", 0.80, 1),
            achado("b", "Folha", 0.79, 2)])) is None

    def test_assunto_vazio_nem_busca(self):
        def explode(*_):
            raise AssertionError("não devia buscar")
        assert contexto.do_assunto("   ", explode) is None


class TestContagem:
    def test_materia_editada_conta_uma_vez(self):
        """A mesma matéria entra duas vezes no índice quando é editada.
        Contar ACHADO em vez de matéria inflaria o número — e o número é
        justamente o que esta saída publica."""
        c = contexto.do_assunto("x", busca_de([
            achado("t", "G1", 0.81, 7), achado("t (atualizada)", "G1",
                                               0.80, 7),
            achado("outra", "Folha", 0.79, 8),
            achado("terceira", "Valor", 0.78, 9)]))
        assert c.materias == 3

    def test_data_ausente_nao_inventa_periodo(self):
        c = contexto.do_assunto("x", busca_de([
            achado("a", "G1", 0.80, 1, None),
            achado("b", "Folha", 0.79, 2, None),
            achado("c", "Valor", 0.78, 3, None)]))
        assert c.de == "" and c.ate == ""
        assert "(" not in contexto.linha(c)

    def test_a_amostra_carrega_a_fonte(self):
        """Princípio 2: nada aparece sem de onde veio."""
        c = contexto.do_assunto("x", busca_de([
            achado("mais perto", "G1", 0.82, 1),
            achado("meio", "Folha", 0.79, 2),
            achado("longe", "Valor", 0.76, 3),
            achado("mais longe", "BBC", 0.755, 4)]))
        assert c.amostra[0] == ("G1", "mais perto")
        assert len(c.amostra) == 3


class TestSaturacao:
    """O defeito mais grave que a revisão adversarial de 03/09/2026
    achou nesta saída: o boletim publicava `QUANTOS` como se fosse
    contagem do acervo. Medido, "a economia brasileira" tem 653 matérias
    acima do limiar e a saída dizia "50", "200" ou "400" conforme o valor
    da constante — a constante disfarçada de medição, que é exatamente o
    erro que o módulo diz existir para não cometer."""

    def _acervo(self, n):
        return [achado(f"t{i}", ["G1", "Folha", "Valor"][i % 3], 0.80, i)
                for i in range(n)]

    def test_expande_ate_o_limiar_voltar_a_cortar(self):
        pedidos, acervo = [], self._acervo(500)

        def buscar(colecao, texto, quantos):
            pedidos.append(quantos)
            return acervo[:quantos]

        c = contexto.do_assunto("x", buscar)
        assert pedidos == [200, 400, 800], pedidos
        assert c.materias == 500 and not c.saturou
        assert "mais de" not in contexto.linha(c)

    def test_no_teto_diz_mais_de_em_vez_de_inventar_numero(self):
        acervo = self._acervo(contexto.TETO_BUSCA * 2)
        c = contexto.do_assunto(
            "x", lambda colecao, texto, quantos: acervo[:quantos])
        assert c.saturou
        assert contexto.linha(c).startswith(
            f"o acervo registra mais de {c.materias} matérias")

    def test_sem_saturacao_nao_expande(self):
        pedidos = []

        def buscar(colecao, texto, quantos):
            pedidos.append(quantos)
            return [achado("a", "G1", 0.80, 1), achado("b", "Folha", 0.79, 2),
                    achado("c", "Valor", 0.78, 3), achado("d", "G1", 0.10, 4)]

        contexto.do_assunto("x", buscar)
        assert pedidos == [200]


class TestDedupPorUrl:
    def test_versoes_da_mesma_url_contam_uma_vez(self):
        """Na coleção `artigos` o id do documento É o artigo_id, então
        deduplicar por artigo_id é no-op. A duplicata real é a matéria
        RECOLETADA: linha nova, id novo, mesma url_norm — 17% do índice
        medido em 03/09/2026, com uma página indexada 31 vezes."""
        def com_url(t, v, prox, aid, url):
            a = achado(t, v, prox, aid)
            a.meta["url_norm"] = url
            return a

        c = contexto.do_assunto("x", busca_de([
            com_url("v1", "G1", 0.82, 1, "g1.com/a"),
            com_url("v2", "G1", 0.81, 2, "g1.com/a"),
            com_url("v3", "G1", 0.80, 3, "g1.com/a"),
            com_url("outra", "Folha", 0.79, 4, "folha.com/b"),
            com_url("terceira", "Valor", 0.78, 5, "valor.com/c"),
        ]))
        assert c.materias == 3, "as tres versoes da mesma url viraram tres"

    def test_sem_url_na_meta_cai_no_artigo_id(self):
        c = contexto.do_assunto("x", busca_de([
            achado("a", "G1", 0.80, 1), achado("b", "Folha", 0.79, 2),
            achado("c", "Valor", 0.78, 3)]))
        assert c.materias == 3


class TestNuncaVeredito:
    def test_a_linha_fala_do_acervo_e_nunca_confirma(self):
        c = contexto.do_assunto("x", busca_de([
            achado("a", "G1", 0.80, 1), achado("b", "Folha", 0.79, 2),
            achado("c", "Valor", 0.78, 3)]))
        texto = contexto.linha(c).lower()
        for proibido in ("confirmad", "corrobora", "verificad",
                         "comprova", "sustenta"):
            assert proibido not in texto

    def test_o_contexto_nao_e_gravado_como_consulta(self):
        """`consultas` tem CHECK com três vereditos; contexto não é um
        deles e não pode virar linha de veredito por acidente."""
        assert not hasattr(contexto.Contexto, "veredito")
