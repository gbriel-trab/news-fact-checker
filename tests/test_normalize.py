"""Testes da normalização de URL e do hash de conteúdo.

Erro nestas funções não levanta exceção — só corrompe o acervo em silêncio.
Daí a cobertura ser mais detalhada aqui do que no resto.
"""

from src.normalize import hash_conteudo, limpa_html, normaliza_url


class TestNormalizaUrl:
    def test_remove_parametro_de_rastreamento(self):
        suja = "https://g1.globo.com/noticia.ghtml?utm_source=twitter&utm_medium=social"
        assert normaliza_url(suja) == "https://g1.globo.com/noticia.ghtml"

    def test_preserva_parametro_significativo(self):
        """Muitos veículos identificam a matéria por query. Descartar tudo
        colapsaria matérias distintas numa só, que é perda de dado."""
        url = "https://exemplo.com/materia?id=4231"
        assert normaliza_url(url) == "https://exemplo.com/materia?id=4231"

    def test_separa_materias_distintas_no_mesmo_caminho(self):
        a = normaliza_url("https://exemplo.com/ver?id=1&utm_source=x")
        b = normaliza_url("https://exemplo.com/ver?id=2&utm_source=x")
        assert a != b

    def test_ordem_dos_parametros_nao_gera_duplicata(self):
        a = normaliza_url("https://exemplo.com/x?b=2&a=1")
        b = normaliza_url("https://exemplo.com/x?a=1&b=2")
        assert a == b

    def test_remove_fragmento(self):
        url = "https://exemplo.com/materia#comentarios"
        assert normaliza_url(url) == "https://exemplo.com/materia"

    def test_remove_barra_final(self):
        assert normaliza_url("https://exemplo.com/secao/") == (
            "https://exemplo.com/secao"
        )

    def test_preserva_barra_da_raiz(self):
        assert normaliza_url("https://exemplo.com/") == "https://exemplo.com/"

    def test_rebaixa_esquema_e_host(self):
        url = "HTTPS://WWW.Exemplo.COM/Materia"
        # O caminho é sensível a maiúsculas no servidor e não pode ser alterado.
        assert normaliza_url(url) == "https://www.exemplo.com/Materia"

    def test_remove_porta_padrao(self):
        assert normaliza_url("https://exemplo.com:443/x") == "https://exemplo.com/x"
        assert normaliza_url("http://exemplo.com:80/x") == "http://exemplo.com/x"

    def test_preserva_porta_nao_padrao(self):
        assert normaliza_url("https://exemplo.com:8443/x") == (
            "https://exemplo.com:8443/x"
        )

    def test_ignora_espaco_ao_redor(self):
        assert normaliza_url("  https://exemplo.com/x  ") == "https://exemplo.com/x"

    def test_remove_rastreamento_da_bbc(self):
        """Caso real: a BBC marca o link do RSS com at_campaign/at_medium, e
        sem removê-los a mesma matéria entrava duas vezes no acervo."""
        url = "https://www.bbc.com/portuguese/articles/abc123?at_campaign=rss&at_medium=RSS"
        assert normaliza_url(url) == (
            "https://www.bbc.com/portuguese/articles/abc123"
        )

    def test_duas_formas_da_mesma_materia_convergem(self):
        """O caso que motiva a função: mesma matéria, endereços diferentes."""
        do_twitter = "https://g1.globo.com/pol/noticia.ghtml?utm_source=twitter#topo"
        do_email = "https://g1.globo.com/pol/noticia.ghtml?utm_campaign=news"
        assert normaliza_url(do_twitter) == normaliza_url(do_email)


class TestLimpaHtml:
    def test_remove_marcacao(self):
        assert limpa_html("<p>Olá <b>mundo</b></p>") == "Olá mundo"

    def test_resolve_entidade(self):
        assert limpa_html("Fran&ccedil;a &amp; Espanha") == "França & Espanha"

    def test_colapsa_espacos(self):
        assert limpa_html("a\n\n   b\tc") == "a b c"

    def test_aceita_none(self):
        assert limpa_html(None) == ""


class TestHashConteudo:
    def test_mesmo_texto_mesmo_hash(self):
        a = hash_conteudo("titulo", "resumo", "corpo")
        b = hash_conteudo("titulo", "resumo", "corpo")
        assert a == b

    def test_texto_diferente_hash_diferente(self):
        original = hash_conteudo("titulo", "resumo", "corpo")
        corrigido = hash_conteudo("titulo", "resumo", "corpo corrigido")
        assert original != corrigido

    def test_nao_confunde_fronteira_entre_campos(self):
        """Sem separador, ("ab", "c") e ("a", "bc") colidiriam — e duas
        matérias diferentes seriam lidas como a mesma."""
        assert hash_conteudo("ab", "c", "") != hash_conteudo("a", "bc", "")
