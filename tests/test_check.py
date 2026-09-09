"""Testes do check — o que dá para prender sem modelo.

O juiz é chamada de modelo e vive no gabarito. O que se testa aqui é o
freio em CÓDIGO que retém confirmação sem sustentação, a identidade de
candidata, a diversidade de veículo no ranking e a versão do prompt.

Histórico que estes testes guardam: a consulta 82 confirmou "ocorreu um
encontro" com uma sessão de comissão qualquer (01/09/2026), e a primeira
versão do freio (03/09) conferia o sujeito contra o texto que o PRÓPRIO
juiz escrevia no alinhamento — parafrasear a evidência bastava para
passar. Agora a conferência é contra o metadado da evidência citada.
"""

import pytest

from src.check import (FATOR_BUSCA, INSTRUCOES_JULGAMENTO, PROMPT_VERSAO,
                       QUANTAS_CANDIDATAS, RESERVA_DIVERSIDADE, Alinhamento,
                       Julgamento, _chave_candidata, _diversifica,
                       _identifica, aplica_alinhamento, sujeito_casa,
                       versao_prompt)
from src.indice import Achado


def evidencia(texto, veiculo="G1", sujeito="", objeto="", valor="",
              distancia=0.1, relacao="afirmou", unidade="", contexto=""):
    return Achado(texto, distancia,
                  {"veiculo": veiculo, "sujeito": sujeito or texto.split()[0],
                   "objeto": objeto, "valor": valor, "relacao": relacao,
                   "unidade": unidade, "contexto": contexto})


def julgamento(veredito="confirmado", **lacunas):
    alinhamento = [Alinhamento(lacuna=l, na_afirmacao=v[0], na_evidencia=v[1])
                   for l, v in lacunas.items()]
    return Julgamento(alinhamento=alinhamento, veredito=veredito,
                      evidencias=[1] if veredito != "sem_evidencia" else [],
                      justificativa="juiz disse")


ESTEVES = evidencia("André Esteves participou de Reunião entre André Esteves "
                    "e Donald Trump",
                    sujeito="André Esteves",
                    objeto="Reunião entre André Esteves e Donald Trump")
COMISSAO = evidencia("Sessão da Comissão Mista da MP 1357/2026 de 28 de "
                     "agosto de 2026", veiculo="CNN Brasil",
                     sujeito="Comissão Mista da Medida Provisória 1357/2026",
                     objeto="Sessão da Comissão Mista da MP 1357/2026")


class TestSujeitoCasa:
    def test_nome_incompleto_e_contido(self):
        assert sujeito_casa("André", "André Esteves")
        assert sujeito_casa("André Esteves", "André")
        assert sujeito_casa("Esteves", "André Esteves")

    def test_chave_canonica_ignora_sufixo_e_acento(self):
        assert sujeito_casa("Braskem", "Braskem S.A.")
        assert sujeito_casa("desemprego", "Taxa de desemprego no Brasil")
        assert sujeito_casa("Selic", "Taxa Selic")

    def test_pontuacao_nao_atrapalha(self):
        assert sujeito_casa("André (e Trump)",
                            "André Esteves, do BTG, e Donald Trump")

    def test_generico_nao_casa(self):
        """Interseção só com substantivo comum não identifica ninguém —
        medido no acervo: 8 triplas de 'governo' e 28 de 'governo
        federal' são entidades diferentes."""
        assert not sujeito_casa("governo", "governo federal")
        assert not sujeito_casa("um encontro", "encontro de líderes")
        assert not sujeito_casa("a empresa", "a empresa Braskem")
        assert not sujeito_casa("um encontro",
                                "Sessão da Comissão Mista da MP 1357/2026")

    def test_cabeca_de_hierarquia_ou_evento_bloqueia(self):
        """A contenção não pode fabricar a corroboração que o canonico.py
        se recusa a fabricar. Sujeitos reais do acervo (03/09/2026)."""
        assert not sujeito_casa(
            "Luiz Inácio Lula da Silva",
            "governo do presidente Luiz Inácio Lula da Silva")
        assert not sujeito_casa("Donald Trump",
                                "Telefonema entre Lula e Donald Trump")
        assert not sujeito_casa("Flávio Bolsonaro",
                                "Encontro entre Flávio Bolsonaro e Villatoro")
        assert not sujeito_casa("Supremo Tribunal Federal",
                                "ministro do Supremo Tribunal Federal Toffoli")
        assert not sujeito_casa("Câmara", "juiz Diego Câmara")

    def test_cargo_parentesco_e_obra_nao_sao_a_entidade(self):
        """A revisão de 03/09 executou o freio e achou o par FUNDADOR
        passando por ele: o cargo não é o país, o pai não é o filho, a
        cinebiografia não é o biografado."""
        assert not sujeito_casa("Estados Unidos",
                                "Presidente dos Estados Unidos")
        assert not sujeito_casa("Omar Aziz", "Senador Omar Aziz")
        assert not sujeito_casa("Jair Bolsonaro",
                                "Cinebiografia de Jair Bolsonaro")
        assert not sujeito_casa("Fábio Luís Lula da Silva",
                                "Pai de Fábio Luís Lula da Silva")
        assert not sujeito_casa("Karina Ferreira",
                                "Advogados de Karina Ferreira")

    def test_falso_negativo_aceito_do_cargo(self):
        """O mesmo token que separa a corte do ministro dela separa o
        título do nome. Perder confirmação é o erro barato (princípio 5),
        e ela sai como RETIDA, com a evidência na tela."""
        assert not sujeito_casa("Diego Câmara", "juiz Diego Câmara")
        assert not sujeito_casa("Lula", "presidente Lula")

    def test_entidade_diferente_nao_casa(self):
        assert not sujeito_casa("Petrobras", "Braskem S.A.")
        assert not sujeito_casa("", "Braskem")
        assert not sujeito_casa("o", "a")


class TestIdentifica:
    def test_nome_proprio_identifica_generico_nao(self):
        assert _identifica("André Esteves")
        assert _identifica("Taxa Selic")
        assert not _identifica("Encontro")
        assert not _identifica("o empresário")
        assert not _identifica("")
        assert not _identifica(None)


class TestAplicaAlinhamento:
    def test_confirmado_com_sujeito_e_apoio_na_evidencia_passa(self):
        j = julgamento(quem=("André", "André Esteves"))
        assert aplica_alinhamento(j, [ESTEVES], ["André"],
                                  ["reunião com Donald Trump"]) is j

    def test_sujeito_generico_retem_mesmo_com_juiz_caprichado(self):
        """A consulta 82 de novo: o juiz alinha tudo e parafraseia a
        evidência com a palavra da afirmação. O freio não lê o que ele
        escreveu — lê o sujeito do estruturador."""
        j = julgamento(
            quem=("um encontro", "encontro (sessão da Comissão Mista)"),
            quando=("até 01/09/2026", "28 de agosto de 2026"))
        retido = aplica_alinhamento(j, [COMISSAO], ["Encontro"], [])
        assert retido.veredito == "sem_evidencia" and retido.retida
        assert "não nomeia um sujeito determinado" in retido.justificativa
        assert "juiz disse" in retido.justificativa

    def test_sujeito_vazio_do_estruturador_retem(self):
        j = julgamento(quem=("um encontro", "um encontro"))
        assert aplica_alinhamento(j, [ESTEVES], [""], []).retida
        assert aplica_alinhamento(j, [ESTEVES], [], []).retida

    def test_apoio_ausente_na_evidencia_retem(self):
        """Data não fecha referente: toda tripla tem data, e o juiz a
        preenche sempre. O apoio tem de ser o que a afirmação afirma."""
        j = julgamento(quem=("Braskem", "Braskem S.A."),
                       quando=("em 2026", "12 de agosto de 2026"))
        outra = evidencia("Braskem S.A. divulgou balanço com prejuízo",
                          sujeito="Braskem S.A.", objeto="balanço")
        retido = aplica_alinhamento(j, [outra], ["Braskem"],
                                    ["recuperação extrajudicial"])
        assert retido.retida and "mais um apoio" in retido.justificativa

    def test_valor_como_apoio(self):
        selic = evidencia("Taxa Selic tem atributo 15 % ao ano",
                          sujeito="Taxa Selic", valor=15)
        j = julgamento(quem=("Selic", "Taxa Selic"))
        assert aplica_alinhamento(j, [selic], ["Taxa Selic"], ["15"]) is j

    def test_sem_apoio_estrutural_a_decisao_do_juiz_vale(self):
        """Freio é backstop, não segundo juiz: afirmação sem objeto nem
        valor não tem o que conferir estruturalmente."""
        j = julgamento(quem=("André", "André Esteves"))
        assert aplica_alinhamento(j, [ESTEVES], ["André"], []) is j

    def test_sujeito_diferente_retem(self):
        j = julgamento(quem=("Petrobras", "Braskem S.A."))
        braskem = evidencia("Braskem S.A. solicitou recuperação extrajudicial",
                            sujeito="Braskem S.A.",
                            objeto="recuperação extrajudicial")
        assert aplica_alinhamento(j, [braskem], ["Petrobras"],
                                  ["recuperação extrajudicial"]).retida

    def test_objeto_contra_objeto_nao_ancora(self):
        """O par que confirmaria "Petrobras pediu recuperação" com
        "Braskem pediu recuperação": mesmo predicado, sujeito errado."""
        braskem = evidencia("Braskem S.A. solicitou recuperação extrajudicial",
                            sujeito="Braskem S.A.",
                            objeto="recuperação extrajudicial")
        j = julgamento(quem=("Petrobras", "Braskem S.A."))
        retido = aplica_alinhamento(
            j, [braskem], ["Petrobras", "recuperação extrajudicial"],
            ["recuperação extrajudicial"])
        assert retido.retida

    def test_troca_de_lados_do_estruturador_ancora(self):
        """Mas o objeto da afirmação contra o SUJEITO da tripla vale: o
        estruturador escolhe um dos lados e oscila."""
        selic = evidencia("Taxa Selic tem atributo 15 % ao ano",
                          sujeito="Taxa Selic", valor=15)
        j = julgamento(quem=("Copom", "Taxa Selic"))
        assert aplica_alinhamento(
            j, [selic], ["Comitê de Política Monetária", "Taxa Selic"],
            ["15"]) is j

    def test_confirmado_sem_citar_retem(self):
        j = julgamento(quem=("André", "André Esteves"))
        assert aplica_alinhamento(j, [], ["André"], ["reunião"]).retida

    def test_objeto_da_afirmacao_tambem_serve_de_sujeito(self):
        """O estruturador oscila entre os dois lados da tripla ('Copom'
        ou 'Taxa Selic' para o mesmo fato) — aceitar os dois evita falso
        negativo por escolha de lado."""
        selic = evidencia("Taxa Selic tem atributo 15 % ao ano",
                          sujeito="Taxa Selic", valor=15)
        j = julgamento(quem=("Copom", "Taxa Selic"))
        assert aplica_alinhamento(
            j, [selic], ["Comitê de Política Monetária", "Taxa Selic"],
            ["15"]) is j

    def test_uma_evidencia_boa_entre_varias_basta(self):
        j = julgamento(quem=("André", "André Esteves"))
        j.evidencias = [1, 2]
        assert aplica_alinhamento(j, [COMISSAO, ESTEVES], ["André"],
                                  ["reunião com Donald Trump"]) is j

    def test_retencao_preserva_as_evidencias_citadas(self):
        """O veredito que mais precisa de auditoria não pode ser o que
        menos mostra: as fontes ficam, rotuladas."""
        j = julgamento(quem=("Petrobras", "Braskem S.A."))
        retido = aplica_alinhamento(j, [ESTEVES], ["Petrobras"], ["x"])
        assert retido.evidencias == [1]

    def test_contradito_e_sem_evidencia_nao_sao_tocados(self):
        for v in ("contradito", "sem_evidencia"):
            j = julgamento(v, quem=("x", None))
            assert aplica_alinhamento(j, [ESTEVES], ["x"], ["y"]) is j


class TestRotaDupla:
    """A mesma tripla chegando pelas duas rotas tem de virar UMA candidata.

    O índice grava o texto com a relação CRUA; a rota por chave o
    renderiza com a NORMALIZADA. Enquanto a identidade era o texto, as
    duas grafias eram duas candidatas — corroboração inflada, princípio
    5. Barreira nova entra com caso positivo pareado (princípio 9): o
    primeiro teste é o que tem de FUNDIR, o segundo o que tem de
    CONTINUAR separado."""

    def _achado(self, relacao, texto):
        return Achado(texto, 0.0,
                      {"veiculo": "G1", "sujeito": "Caixa", "objeto": "",
                       "valor": 3.9e9, "relacao": relacao,
                       "unidade": "BRL", "contexto": "lucro"})

    def test_positivo_mesma_tripla_pelas_duas_rotas_funde(self):
        semantica = self._achado("outro", "Caixa outro 3900000000 BRL (lucro)")
        por_chave = self._achado("tem_atributo",
                                 "Caixa tem atributo 3900000000 BRL (lucro)")
        assert _chave_candidata(semantica) == _chave_candidata(por_chave)

    def test_negativo_triplas_de_verdade_diferentes_nao_fundem(self):
        lucro = self._achado("tem_atributo",
                             "Caixa tem atributo 3900000000 BRL (lucro)")
        outra = Achado("Caixa tem atributo 5000000000 BRL (lucro)", 0.0,
                       {"veiculo": "G1", "sujeito": "Caixa", "objeto": "",
                        "valor": 5.0e9, "relacao": "tem_atributo",
                        "unidade": "BRL", "contexto": "lucro"})
        veiculo = self._achado("tem_atributo",
                               "Caixa tem atributo 3900000000 BRL (lucro)")
        veiculo.meta["veiculo"] = "Valor"
        assert _chave_candidata(lucro) != _chave_candidata(outra)
        assert _chave_candidata(lucro) != _chave_candidata(veiculo)

    def test_alvo_da_relacao_passa_pela_mesma_normalizacao(self):
        """O buraco de verdade: o acervo vem normalizado e o estruturador
        vem cru. Sob `obteve_percentual_em` com número e sem objeto, a
        rota por chave devolvia VAZIO sem erro nenhum."""
        from src import grafo
        from src.check import AfirmacaoRecebida, Relacao, _por_chave
        acervo = [grafo.Afirmacao(
            "Juliana Brizola", grafo.relacao_normalizada(
                "obteve_percentual_em", None, 38.0),
            None, 38.0, "%", "intencao de voto", None, "EXTRACTED", "G1",
            "t", "u")]
        assert acervo[0].relacao == "tem_atributo"
        pedido = AfirmacaoRecebida(
            sujeito_canonico="Juliana Brizola",
            relacao=Relacao("obteve_percentual_em"),
            objeto_canonico=None, valor_numero=38.0,
            valor_unidade="%", busca="Juliana Brizola tem 38%")
        assert len(_por_chave(pedido, acervo)) == 1

    def test_relacao_crua_do_acervo_tambem_casa(self):
        """O pareado que faltava, e a revisão adversarial de 03/09/2026
        achou: normalizar o alvo consertou a afirmação numérica SEM
        objeto e quebrou a COM objeto, cuja relação o acervo gravou
        crua. Conserta um lado, abre o outro — o aperto do princípio 9."""
        from src import grafo
        from src.check import AfirmacaoRecebida, Relacao, _por_chave
        acervo = [grafo.Afirmacao(
            "Juliana Brizola", "obteve_percentual_em", "pesquisa Quaest",
            38.0, "%", None, None, "EXTRACTED", "G1", "t", "u")]
        assert acervo[0].relacao == "obteve_percentual_em"
        pedido = AfirmacaoRecebida(
            sujeito_canonico="Juliana Brizola",
            relacao=Relacao("obteve_percentual_em"),
            objeto_canonico="pesquisa Quaest", valor_numero=38.0,
            valor_unidade="%", busca="Juliana Brizola tem 38%")
        assert len(_por_chave(pedido, acervo)) == 1

    def test_caso_positivo_pareado_relacao_diferente_segue_sem_casar(self):
        """O pareado do anterior: normalizar o alvo não pode fazer
        relação de verdade diferente passar a casar."""
        from src import grafo
        from src.check import AfirmacaoRecebida, Relacao, _por_chave
        acervo = [grafo.Afirmacao(
            "Juliana Brizola", "se_reuniu_com", "Trump", None, None, None,
            None, "EXTRACTED", "G1", "t", "u")]
        pedido = AfirmacaoRecebida(
            sujeito_canonico="Juliana Brizola",
            relacao=Relacao("obteve_percentual_em"),
            objeto_canonico=None, valor_numero=38.0,
            valor_unidade="%", busca="Juliana Brizola tem 38%")
        assert _por_chave(pedido, acervo) == []


class TestCandidatas:
    def test_identidade_inclui_o_veiculo_e_ignora_caixa(self):
        """Modo história grava a mesma tripla para cada veículo que a
        afirma; até 03/09 a cópia do segundo veículo era descartada como
        duplicata e a corroboração saía subcontada. E a re-extração do
        mesmo artigo difere só em caixa."""
        g1 = evidencia("André Esteves participou de Reunião", "G1",
                       sujeito="André Esteves")
        valor = evidencia("André Esteves participou de Reunião", "Valor",
                          sujeito="André Esteves")
        g1_v3 = evidencia("andré esteves participou de reunião", "G1",
                          sujeito="André Esteves")
        assert _chave_candidata(g1) != _chave_candidata(valor)
        assert _chave_candidata(g1) == _chave_candidata(g1_v3)

    def test_reserva_abre_vaga_para_o_segundo_veiculo(self):
        """Medido em 03/09: para a reunião Esteves–Trump o G1 ocupava 7
        das 10 vagas com triplas do mesmo evento, e um veículo com
        fraseado diferente ficaria fora do julgamento."""
        muitas = [evidencia(f"G1 tripla {i}", "G1", distancia=0.1 * i)
                  for i in range(8)]
        muitas.append(evidencia("Valor tripla", "Valor", distancia=1.0))
        escolhidas = _diversifica(muitas, quantas=4, reserva=1)
        veiculos = [a.meta["veiculo"] for a in escolhidas]
        # Sem a reserva as 4 vagas seriam do G1 e o Valor (o mais
        # distante) ficaria fora; as 3 melhores continuam entrando.
        assert veiculos == ["G1", "G1", "G1", "Valor"]

    def test_reserva_volta_ao_ranking_se_nao_ha_veiculo_novo(self):
        so_g1 = [evidencia(f"t{i}", "G1", distancia=0.1 * i) for i in range(5)]
        escolhidas = _diversifica(so_g1, quantas=4, reserva=2)
        assert [a.texto for a in escolhidas] == ["t0", "t1", "t2", "t3"]

    def test_ordem_final_e_a_de_proximidade(self):
        itens = [evidencia("a", "G1", distancia=0.5),
                 evidencia("b", "Valor", distancia=0.1)]
        assert [a.texto for a in _diversifica(itens, quantas=2, reserva=1)] \
            == ["b", "a"]

    def test_constantes_dao_vaga_para_mais_de_um_veiculo(self):
        assert 0 < RESERVA_DIVERSIDADE < QUANTAS_CANDIDATAS
        assert FATOR_BUSCA >= 2


class TestPrompt:
    def test_regras_de_alinhamento_e_nome_parcial(self):
        assert "ALINHE ANTES DE JULGAR" in INSTRUCOES_JULGAMENTO
        assert "NOME INCOMPLETO CASA COM NOME COMPLETO" in INSTRUCOES_JULGAMENTO

    def test_alinhamento_vem_antes_do_veredito_no_schema(self):
        campos = list(Julgamento.model_json_schema()["properties"])
        assert campos.index("alinhamento") < campos.index("veredito")

    def test_retida_fica_fora_do_schema(self):
        assert "retida" not in Julgamento.model_json_schema()["properties"]

    def test_versao_tem_forma_estavel(self):
        v = versao_prompt()
        assert v == PROMPT_VERSAO and len(v) == 12
        assert all(c in "0123456789abcdef" for c in v)


class TestStorage:
    def test_consulta_grava_versao_e_retida(self, tmp_path):
        from src.storage import conecta, salva_consulta
        con = conecta(tmp_path / "t.db")
        salva_consulta(con, "x", "sem_evidencia", "j", 1, 1, 1, "m", 0.0,
                       prompt_versao="abc123", retida=True)
        salva_consulta(con, "y", "confirmado", "j", 1, 1, 1, "m", 0.0)
        linhas = list(con.execute(
            "SELECT prompt_versao, retida FROM consultas ORDER BY id"))
        assert [l["prompt_versao"] for l in linhas] == ["abc123", None]
        assert [l["retida"] for l in linhas] == [1, 0]

    def test_boletim_nao_confunde_retida_com_acervo_vazio(self, tmp_path):
        from src.boletim import _retida
        from src.storage import conecta, salva_consulta
        con = conecta(tmp_path / "t.db")
        salva_consulta(con, "x", "sem_evidencia", "j", 1, 1, 1, "m", 0.0,
                       retida=True)
        salva_consulta(con, "y", "sem_evidencia", "j", 1, 0, 0, "m", 0.0)
        retida, normal = con.execute(
            "SELECT * FROM consultas ORDER BY id").fetchall()
        assert _retida(retida) and not _retida(normal)


class TestApoioFragil:
    """O aviso de 03/09/2026, aprovado como AVISO e nao como mudanca de
    contagem: liveblog continua valendo veiculo (decisao do usuario), mas
    o leitor precisa saber quando a confirmacao inteira se apoia num link
    que nao mostra o fato."""

    LIVE = {"url": "https://redir.folha.com.br/x/*https://aovivo.folha.uol"
                   ".com.br/mercado/2026/08/01/6556-dolar.shtml"}
    FIRME = {"url": "https://g1.globo.com/economia/noticia/2026/08/26/rj.ghtml"}

    def test_dois_veiculos_com_um_ao_vivo_avisa(self):
        from src.check import apoio_fragil
        assert apoio_fragil([self.FIRME, self.LIVE], {"G1", "Folha"})

    def test_dois_veiculos_firmes_nao_avisam(self):
        """O pareado que nao pode virar alarme constante."""
        from src.check import apoio_fragil
        assert not apoio_fragil([self.FIRME, self.FIRME], {"G1", "Valor"})

    def test_tres_veiculos_nao_avisam_mesmo_com_ao_vivo(self):
        """Com tres, sobra corroboracao que se sustenta sozinha — o
        aviso e para o caso ESTREITO, senao vira ruido e se ignora."""
        from src.check import apoio_fragil
        assert not apoio_fragil([self.FIRME, self.FIRME, self.LIVE],
                                {"G1", "Valor", "Folha"})

    def test_um_veiculo_nao_entra_aqui(self):
        """Um veiculo ja tem o aviso proprio, de antes."""
        from src.check import apoio_fragil
        assert not apoio_fragil([self.LIVE], {"Folha"})


def _ev(veiculo, relacao, valor, data_fato, sujeito="Selic", unidade="%",
        publicada=""):
    return Achado(f"{sujeito} {relacao} {valor}", 0.0,
                  {"veiculo": veiculo, "titulo": "t", "url": "u",
                   "sujeito": sujeito, "relacao": relacao, "objeto": "",
                   "valor": valor, "unidade": unidade, "data_fato": data_fato,
                   "origem": "e", "data_publicacao": publicada})


class TestPorReferencia:
    """Regra 8 em código (08/09/2026): estado se julga na data de
    referência; evento não expira."""

    def test_estado_posterior_a_referencia_sai(self):
        from src.check import por_referencia
        ago = _ev("G1", "tem_atributo", 15.0, "2026-08-20")
        dez = _ev("Folha", "tem_atributo", 14.5, "2026-12-10")
        assert por_referencia([ago, dez], "2026-10-15T12:00:00Z") == [ago]
        assert por_referencia([ago, dez], "2027-01-15") == [ago, dez]

    def test_mesmo_veiculo_mesma_medida_fica_o_mais_recente_ate_a_referencia(self):
        from src.check import por_referencia
        jun = _ev("G1", "tem_atributo", 14.5, "2026-06-18")
        ago = _ev("G1", "tem_atributo", 15.0, "2026-08-20")
        assert por_referencia([jun, ago], "2026-10-15") == [ago]
        assert por_referencia([ago, jun], "2026-10-15") == [ago]
        # Veículos diferentes não se substituem: o juiz decide.
        folha = _ev("Folha", "tem_atributo", 14.5, "2026-06-18")
        assert por_referencia([folha, ago], "2026-10-15") == [folha, ago]

    def test_sem_data_fica_e_nao_substitui_datado(self):
        from src.check import por_referencia
        ago = _ev("G1", "tem_atributo", 15.0, "2026-08-20")
        sem = _ev("G1", "tem_atributo", 14.25, "")
        assert por_referencia([sem, ago], "2026-10-15") == [sem, ago]

    def test_evento_nao_expira_nem_e_filtrado(self):
        from src.check import por_referencia
        visita = _ev("G1", "participou_de", "", "2026-08-25", sujeito="Ratcliffe")
        depois = _ev("G1", "participou_de", "", "2026-12-25", sujeito="Ratcliffe")
        assert por_referencia([visita, depois], "2026-12-01") == [visita, depois]

    def test_empate_no_dia_se_desfaz_pela_hora_de_publicacao(self):
        from src.check import por_referencia
        manha = _ev("G1", "tem_atributo", 291, "2026-08-26", sujeito="desaparecidos",
                    unidade="pessoas", publicada="2026-08-26T10:08:00+00:00")
        noite = _ev("G1", "tem_atributo", 341, "2026-08-26", sujeito="desaparecidos",
                    unidade="pessoas", publicada="2026-08-26T22:30:00+00:00")
        assert por_referencia([manha, noite], "2026-08-27") == [noite]
        # Sem hora, empate exato: as duas ficam.
        a = _ev("G1", "tem_atributo", 291, "2026-08-26", sujeito="x")
        b = _ev("G1", "tem_atributo", 341, "2026-08-26", sujeito="x")
        assert por_referencia([a, b], "2026-08-27") == [a, b]

    def test_sem_referencia_ou_sem_relacao_nada_muda(self):
        from src.check import por_referencia
        dez = _ev("Folha", "tem_atributo", 14.5, "2026-12-10")
        assert por_referencia([dez], "") == [dez]
        crua = Achado("x", 0.0, {"veiculo": "G1", "data_fato": "2026-12-10",
                                 "origem": "e", "titulo": "", "url": ""})
        assert por_referencia([crua], "2026-10-15") == [crua]


class TestJuizComTempo:
    def test_corpo_leva_data_de_referencia_e_data_de_publicacao(self, monkeypatch):
        from types import SimpleNamespace

        from src import check
        visto = {}

        def falso(instrucoes, corpo, schema, modelo=None):
            visto["corpo"] = corpo
            return SimpleNamespace(
                dados=check.Julgamento(alinhamento=[], veredito="confirmado",
                                       evidencias=[1], justificativa="j"),
                uso=SimpleNamespace(custo=0.0))
        monkeypatch.setattr(check.llm, "gera", falso)
        ev = _ev("G1", "tem_atributo", 15.0, "2026-08-20",
                 publicada="2026-08-20T09:00:00+00:00")
        check.julga("A Selic está em 15%", [ev], "2026-10-15T13:00:00+00:00")
        assert "DATA DE REFERÊNCIA (quando a afirmação foi feita): 2026-10-15" in visto["corpo"]
        assert "publicada em 2026-08-20" in visto["corpo"]
        check.julga("x", [ev], "")
        assert "não informada" in visto["corpo"]

    def test_prompt_e_schema_conhecem_o_tempo_e_o_dividido(self):
        from src import check
        assert "A AFIRMAÇÃO TEM DATA" in check.INSTRUCOES_JULGAMENTO
        assert "EVENTO (algo que ocorreu num instante) não expira" in check.INSTRUCOES_JULGAMENTO
        assert "DIVIDIDO é o veredito quando VEÍCULOS DIFERENTES" in check.INSTRUCOES_JULGAMENTO
        assert "FALA CITADA É EVIDÊNCIA DE QUE ALGUÉM DISSE" in check.INSTRUCOES_JULGAMENTO
        assert "CONSTATAÇÃO DE\n   AUTORIDADE" in check.INSTRUCOES_JULGAMENTO
        enum = check.Julgamento.model_json_schema()["properties"]["veredito"]["enum"]
        assert enum == ["confirmado", "contradito", "sem_evidencia", "dividido"]
        assert check.ROTULOS["dividido"] == "DIVIDIDO"

    def test_verifica_repassa_a_referencia_e_assume_hoje_sem_ela(self, monkeypatch):
        from types import SimpleNamespace

        from src import check
        visto = {}
        afirmacao = SimpleNamespace(sujeito_canonico="selic", relacao=SimpleNamespace(value="tem_atributo"),
                                    objeto_canonico=None, busca="selic")
        monkeypatch.setattr(check, "estrutura", lambda t: (afirmacao, SimpleNamespace(custo=0.0)))
        monkeypatch.setattr(check, "apoios_de", lambda a: (["Selic"], ["15"]))
        # Datas relativas a hoje: com 2026-12-10 fixo o teste passaria a
        # falhar sozinho em 11/12/2026, sem ninguém mexer em código.
        from datetime import datetime, timedelta, timezone
        hoje_d = datetime.now(timezone.utc).date()
        antes = (hoje_d - timedelta(days=20)).isoformat()
        depois = (hoje_d + timedelta(days=90)).isoformat()
        ago = _ev("G1", "tem_atributo", 15.0, antes)
        dez = _ev("Folha", "tem_atributo", 14.5, depois)
        monkeypatch.setattr(check, "recupera", lambda a, acervo, mapa, ref="", sem_tempo=False: [ago, dez])

        def julga(texto, evidencias, referencia=""):
            visto["evidencias"] = evidencias
            visto["referencia"] = referencia
            return (check.Julgamento(alinhamento=[], veredito="confirmado",
                                     evidencias=[1], justificativa="j"),
                    SimpleNamespace(custo=0.0))
        monkeypatch.setattr(check, "julga", julga)
        futuro = (hoje_d + timedelta(days=200)).isoformat()
        check.verifica("A Selic está em 15%", acervo=[], referencia=futuro)
        assert visto["referencia"] == futuro
        assert visto["evidencias"] == [ago, dez]
        # Sem referência, a afirmação é de HOJE, e o estado publicado
        # depois de hoje sai.
        check.verifica("A Selic está em 15%", acervo=[])
        assert visto["referencia"][:2] == "20" and visto["evidencias"] == [ago]


class TestDividido:
    def test_dividido_exige_dois_veiculos_citados(self):
        from src import check
        j = check.Julgamento(alinhamento=[], veredito="dividido",
                             evidencias=[1, 2], justificativa="discordam")
        folha = _ev("Folha", "tem_atributo", 291, "2026-08-26")
        g1 = _ev("G1", "tem_atributo", 341, "2026-08-26")
        assert check.aplica_alinhamento(j, [folha, g1], ["x"], []) is j
        retido = check.aplica_alinhamento(j, [g1, g1], ["x"], [])
        assert retido.veredito == "sem_evidencia" and retido.retida
        assert "Divisão retida" in retido.justificativa


class TestReusoPorReferencia:
    """08/09/2026: a janela de 24h casava só pelo texto. Com a data de
    referência no julgamento, refazer dois dias na mesma sessão reusaria o
    veredito do primeiro no segundo."""

    def _grava(self, con, texto, veredito, referencia):
        from src.storage import salva_consulta
        return salva_consulta(con, texto, veredito, "j", 1, 1, 1, "m", 0.1,
                              referencia=referencia)

    def test_mesma_afirmacao_em_dias_diferentes_nao_reusa(self, tmp_path):
        from src import check
        from src.storage import conecta
        con = conecta(tmp_path / "t.db")
        self._grava(con, "A Selic está em 15%", "confirmado", "2026-08-25")
        assert check.consulta_recente(
            con, "A Selic está em 15%", referencia="2026-08-25T10:00:00Z") is not None
        assert check.consulta_recente(
            con, "A Selic está em 15%", referencia="2026-08-26T10:00:00Z") is None
        con.close()

    def test_linha_antiga_sem_referencia_vale_como_hoje(self, tmp_path):
        from datetime import datetime, timezone

        from src import check
        from src.storage import conecta
        con = conecta(tmp_path / "t.db")
        con.execute(
            "INSERT INTO consultas (afirmacao, veredito, justificativa, "
            "candidatas, citadas, veiculos, modelo, custo_usd, consultado_em) "
            "VALUES ('x', 'confirmado', 'j', 1, 1, 1, 'm', 0.1, ?)",
            (datetime.now(timezone.utc).isoformat(),))
        con.commit()
        hoje = datetime.now(timezone.utc).isoformat()
        assert check.consulta_recente(con, "x") is not None
        assert check.consulta_recente(con, "x", referencia=hoje) is not None
        assert check.consulta_recente(con, "x", referencia="2026-08-26") is None
        con.close()

    def test_verifica_grava_a_referencia(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from src import check
        from src.storage import conecta
        con = conecta(tmp_path / "t.db")
        afirmacao = SimpleNamespace(sujeito_canonico="selic",
                                    relacao=SimpleNamespace(value="tem_atributo"),
                                    objeto_canonico=None, busca="selic")
        monkeypatch.setattr(check, "estrutura", lambda t: (afirmacao, SimpleNamespace(custo=0.0)))
        monkeypatch.setattr(check, "recupera", lambda a, acervo, mapa, ref="", sem_tempo=False: [])
        check.verifica("A Selic está em 15%", conexao=con, acervo=[],
                       referencia="2026-08-26T13:00:00Z")
        assert con.execute("SELECT referencia FROM consultas").fetchone()[0] == "2026-08-26"
        con.close()


class TestBarreiraTemporalRevisada:
    """Os defeitos que a revisão adversária de 08/09/2026 achou em
    `por_referencia` — cada um com o par positivo/negativo."""

    def test_objeto_na_chave_dois_fatos_do_mesmo_sujeito_sobrevivem(self):
        """Sem o objeto, "Esteves integra o BTG" e "Esteves integra o
        conselho da B3" eram a mesma medida e a mais antiga sumia."""
        from src.check import por_referencia
        btg = _ev("G1", "integra", "", "2019-01-01", sujeito="André Esteves",
                  unidade="")
        btg.meta["objeto"] = "BTG Pactual"
        b3 = _ev("G1", "integra", "", "2025-01-01", sujeito="André Esteves",
                 unidade="")
        b3.meta["objeto"] = "conselho da B3"
        assert por_referencia([btg, b3], "2026-09-08") == [btg, b3]
        # Controle: mesmo objeto e datas diferentes, aí sim é o mesmo
        # fato e o antigo sai.
        velho = _ev("G1", "integra", "", "2015-01-01", sujeito="André Esteves",
                    unidade="")
        velho.meta["objeto"] = "BTG Pactual"
        assert por_referencia([velho, btg], "2026-09-08") == [btg]

    def test_empate_no_instante_nao_deixa_o_superado_vivo(self):
        """O empatado não entrava em `fora` nem virava vigente: quando um
        terceiro, mais novo, chegava, o empatado ficava."""
        from src.check import por_referencia
        jan_a = _ev("G1", "tem_atributo", 15.0, "2026-01-01")
        jan_b = _ev("G1", "tem_atributo", 15.0, "2026-01-01")
        fev = _ev("G1", "tem_atributo", 12.0, "2026-02-01")
        assert por_referencia([jan_a, jan_b, fev], "2026-03-01") == [fev]
        assert por_referencia([fev, jan_a, jan_b], "2026-03-01") == [fev]
        # Empatados sem nada mais novo: os dois ficam.
        assert por_referencia([jan_a, jan_b], "2026-03-01") == [jan_a, jan_b]

    def test_projecao_nao_e_cortada_nem_suprime(self):
        """`data_fato` posterior é legítimo (projeção, meta, orçamento):
        no acervo real são 40 triplas de estado, entre elas "salário
        mínimo deverá subir para R$ 1.741 em 2027", publicada em 25/08."""
        from src.check import por_referencia
        projecao = _ev("Agência Brasil", "tem_atributo", 1741, "2027-01-01",
                       sujeito="salário mínimo", unidade="BRL",
                       publicada="2026-08-25T10:00:00+00:00")
        atual = _ev("Agência Brasil", "tem_atributo", 1518, "2026-01-01",
                    sujeito="salário mínimo", unidade="BRL",
                    publicada="2026-01-02T10:00:00+00:00")
        assert por_referencia([atual, projecao], "2026-09-08") == [atual, projecao]

    def test_o_corte_e_pela_materia_publicada_nao_pelo_fato(self):
        from src.check import por_referencia
        depois = _ev("Folha", "tem_atributo", 14.5, "2026-12-10",
                     publicada="2026-12-10T09:00:00+00:00")
        assert por_referencia([depois], "2026-10-15") == []
        # Mesma data de fato, mas a matéria saiu ANTES da referência: fica.
        anunciada = _ev("Folha", "tem_atributo", 14.5, "2026-12-10",
                        publicada="2026-09-01T09:00:00+00:00")
        assert por_referencia([anunciada], "2026-10-15") == [anunciada]

    def test_outro_usa_o_tipo_que_o_modelo_deu(self):
        """`outro` é a válvula de escape do vocabulário (622 triplas): sem
        o palpite, toda tripla nela passava pela barreira como evento."""
        from src.check import por_referencia
        estado = _ev("G1", "outro", 80000, "2026-12-01", sujeito="Bitcoin",
                     unidade="USD", publicada="2026-12-01T10:00:00+00:00")
        estado.meta["tipo_relacao"] = "estado"
        assert por_referencia([estado], "2026-02-01") == []
        evento = _ev("G1", "outro", "", "2026-12-01", sujeito="Bitcoin",
                     publicada="2026-12-01T10:00:00+00:00")
        evento.meta["tipo_relacao"] = "evento"
        assert por_referencia([evento], "2026-02-01") == [evento]

    def test_com_removidas_devolve_o_que_a_barreira_tirou(self):
        from src.check import por_referencia
        dez = _ev("Folha", "tem_atributo", 14.5, "2026-12-10")
        ficam, fora = por_referencia([dez], "2026-10-15", com_removidas=True)
        assert (ficam, fora) == ([], [dez])


class TestTempoAntesDaEscolha:
    """A barreira roda dentro de `recupera`, antes da reserva de
    diversidade: filtrar depois gastava a vaga do segundo veículo numa
    evidência que ia ser removida em seguida."""

    def test_a_vaga_do_segundo_veiculo_nao_e_gasta_por_evidencia_futura(
            self, monkeypatch):
        from types import SimpleNamespace

        from src import check
        def com_distancia(e, distancia):
            return Achado(e.texto, distancia, e.meta)

        # A Folha tem duas triplas: a melhor ranqueada é POSTERIOR à
        # referência, e é ela que a reserva pegaria antes da barreira.
        futura = com_distancia(
            _ev("Folha", "tem_atributo", 14.5, "2026-12-10",
                publicada="2026-12-10T09:00:00+00:00"), 0.10)
        boa = com_distancia(
            _ev("Folha", "tem_atributo", 15.0, "2026-08-20",
                publicada="2026-08-20T09:00:00+00:00"), 0.35)
        do_g1 = [com_distancia(
            _ev("G1", "tem_atributo", 15.0, "2026-08-0%d" % d,
                sujeito="Selic %d" % d,
                publicada="2026-08-0%dT09:00:00+00:00" % d), 0.11 + d / 100)
            for d in range(1, 10)]
        monkeypatch.setattr(check.indice, "busca",
                            lambda *a, **k: [futura] + do_g1 + [boa])
        monkeypatch.setattr(check, "_por_chave", lambda *a, **k: [])
        afirmacao = SimpleNamespace(sujeito_canonico="selic", busca="selic",
                                    relacao=SimpleNamespace(value="tem_atributo"),
                                    objeto_canonico=None)
        saiu = check.recupera(afirmacao, [], None, "2026-10-15")
        assert "Folha" in {a.meta["veiculo"] for a in saiu}, \
            "a reserva perdeu a vaga do segundo veículo"
        assert futura not in saiu
        # Com `sem_tempo`, o material bruto volta — é o que `verifica` conta.
        assert futura in check.recupera(afirmacao, [], None, "2026-10-15",
                                        sem_tempo=True)


class TestListaVaziaPorTempo:
    """Lista esvaziada pela barreira não é "o acervo não cobre": sem a
    marca `retida`, o boletim pagava extração para cobrir o que já está
    coberto."""

    def test_grava_retida_e_nao_diz_que_o_acervo_nao_cobre(
            self, tmp_path, monkeypatch, capsys):
        from types import SimpleNamespace

        from src import check
        from src.storage import conecta
        con = conecta(tmp_path / "t.db")
        afirmacao = SimpleNamespace(sujeito_canonico="selic", busca="selic",
                                    relacao=SimpleNamespace(value="tem_atributo"),
                                    objeto_canonico=None)
        monkeypatch.setattr(check, "estrutura",
                            lambda t: (afirmacao, SimpleNamespace(custo=0.0)))
        dez = _ev("Folha", "tem_atributo", 14.5, "2026-12-10",
                  publicada="2026-12-10T09:00:00+00:00")
        monkeypatch.setattr(check, "recupera",
                            lambda a, acervo, mapa, ref="", sem_tempo=False: [dez])
        monkeypatch.setattr(check, "julga",
                            lambda *a, **k: pytest.fail("julgou sem evidência"))
        check.verifica("A Selic está em 15%", conexao=con, acervo=[],
                       referencia="2026-10-15T12:00:00Z")
        saida = capsys.readouterr().out
        assert "fora da janela temporal" in saida
        assert "não falam do assunto" not in saida
        linha = con.execute("SELECT veredito, retida, candidatas FROM "
                            "consultas").fetchone()
        assert (linha["veredito"], bool(linha["retida"]),
                linha["candidatas"]) == ("sem_evidencia", True, 1)
        con.close()
