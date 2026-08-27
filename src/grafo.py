"""Grafo das afirmações do acervo.

    python -m src.grafo              # números do grafo e o que ele encontra
    python -m src.grafo "Braskem"    # o que o acervo afirma sobre uma entidade

O índice vetorial acha o que é PARECIDO. O grafo responde o que é EXATO:
quantos veículos independentes afirmam a mesma coisa, e quando dois afirmam
coisas incompatíveis.

Os dois são necessários e fazem trabalhos opostos. Casar uma afirmação que
chega de fora exige proximidade; contar corroboração exige identidade — "quase
a mesma entidade" não corrobora nada.

A unidade de corroboração é o VEÍCULO. Duas editorias da mesma redação não são
fontes independentes, e contá-las como duas fabricaria confirmação.
"""

import collections
import re
import sqlite3
import sys
from dataclasses import dataclass

from . import config, llm
from .vocabulario import Relacao
from .storage import conecta

MIN_CONTEXTO_IGUAL = 0.95
"""Proximidade entre dois contextos para serem considerados a MESMA medida.

Alto de propósito, e ainda assim insuficiente sozinho — ver `_mesma_medida`.
Medido em 8 pares reais do acervo: os que descrevem a mesma medida ficaram em
0,97-1,00, e os que descrevem medidas diferentes chegaram a 0,93. A margem é
de 0,04. Amostra pequena e limiar frágil.

Erra para o lado seguro: contexto rejeitado por engano vira fato separado, que
deixa de confirmar. Contexto aceito por engano funde duas medidas e inventa
uma divergência — o falso positivo que o princípio 5 chama de pior erro.
"""

TOLERANCIA_RELATIVA = 0.02
"""Diferença relativa abaixo da qual dois números são considerados o mesmo.

Existe porque veículos arredondam de formas diferentes — 10,9 bilhões e
10.900.000.000 são o mesmo fato. Sem tolerância, arredondamento vira
contradição; com tolerância grande demais, contradição vira arredondamento.
"""


@dataclass(frozen=True, slots=True)
class Afirmacao:
    """Uma tripla, com a proveniência que o veredito precisa citar."""

    sujeito: str
    relacao: str
    objeto: str | None
    valor: float | None
    unidade: str | None
    contexto: str | None
    data_fato: str | None
    origem: str
    veiculo: str
    titulo: str
    url: str
    data_publicacao: str | None = None

    @property
    def chave(self) -> tuple[str, str, str]:
        """O que precisa coincidir para duas afirmações serem candidatas a
        "a mesma".

        O contexto NÃO entra aqui, e já entrou. Entrou para separar
        "Petrobras detém 47% do capital votante" de "36,1% do capital total",
        que são duas medidas e não um número em disputa. Mas `contexto` é texto
        livre escrito pelo modelo, e igualdade exata sobre texto gerado nunca
        casa: dois veículos escreveram "lucro recorrente NO 2º trimestre" e
        "...DO 2º trimestre", uma preposição de diferença, e o fato deixou de
        existir duas vezes.

        O efeito foi medido e é total: 126 fatos com número no acervo, ZERO
        confirmados. Nenhum número jamais corroborou, e a detecção de
        divergência entre veículos — que é o produto — nunca funcionou.

        A separação por medida continua existindo, em `agrupa`, por
        proximidade semântica em vez de igualdade de string.
        """
        return (self.sujeito, self.relacao, self.objeto or "")


@dataclass(frozen=True, slots=True)
class Corroboracao:
    """Um fato e os veículos que o afirmam."""

    chave: tuple[str, str, str]
    afirmacoes: tuple[Afirmacao, ...]

    @property
    def veiculos(self) -> set[str]:
        return {a.veiculo for a in self.afirmacoes}

    @property
    def confirmada(self) -> bool:
        return len(self.veiculos) >= 2

    @property
    def por_unidade(self) -> dict[str, list["Afirmacao"]]:
        """Afirmações com número, separadas por unidade.

        Comparar entre unidades é erro de categoria: 10,9 bilhões de dólares e
        56 bilhões de reais são a MESMA dívida, e sem esta separação o sistema
        os reportava como dois veículos divergindo. Falso positivo, que é o
        erro que o projeto considera o pior.

        Converter moeda resolveria mais casos, mas exige a cotação da data do
        fato — e cotação errada transforma o mesmo número em divergência
        inventada. Sem unidade comum, o sistema não compara.
        """
        grupos: dict[str, list[Afirmacao]] = collections.defaultdict(list)
        for a in self.afirmacoes:
            if a.valor is not None:
                grupos[a.unidade or "?"].append(a)
        return dict(grupos)

    @property
    def divergencias(self) -> list[tuple[str, list["Afirmacao"]]]:
        """Unidades em que os números afirmados não batem entre si."""
        achadas = []
        for unidade, grupo in self.por_unidade.items():
            vs = [a.valor for a in grupo]
            if len(vs) < 2:
                continue
            maior, menor = max(vs), min(vs)
            # Diferença relativa: veículos arredondam diferente, e
            # arredondamento não é contradição.
            fora = (menor != 0) if maior == 0 else (
                abs(maior - menor) / abs(maior) > TOLERANCIA_RELATIVA)
            if fora:
                achadas.append((unidade, grupo))
        return achadas

    @property
    def diverge(self) -> bool:
        return bool(self.divergencias)


def _relacao_normalizada(relacao: str, objeto: str | None,
                         valor: float | None) -> str:
    """Tripla com número e sem objeto é `tem_atributo`, diga o modelo o que
    disser.

    Não é conserto de prompt disfarçado: pelas próprias regras da extração,
    toda tripla carrega OU objeto OU valor, e a que carrega valor sem objeto É
    uma propriedade do sujeito. `outro` ali é o modelo deixando de aplicar a
    regra 6, não uma distinção que exista.

    Sem isto, dois veículos que publicam o MESMO número sobre o MESMO fato não
    se encontram porque um recebeu `tem_atributo` e o outro `outro` — foi o que
    aconteceu com o lucro da Caixa, quatro números idênticos e zero
    confirmações.

    Normalizado na LEITURA, não na gravação: assim vale também para o que já
    está no banco, e nenhuma extração paga é reescrita.
    """
    if valor is not None and not objeto:
        return Relacao.TEM_ATRIBUTO.value
    return relacao


def carrega(conexao: sqlite3.Connection,
            desde: str | None = None) -> list[Afirmacao]:
    """Lê as afirmações do vocabulário mais recente.

    Versões antigas ficam de fora: relações de vocabulários diferentes não são
    comparáveis, e misturá-las produziria corroboração inexistente entre nomes
    que só por acaso coincidem.

    E UMA extração por matéria, do MODELO DE EXTRAÇÃO ATIVO. Não basta pegar a
    mais recente: medir modelo exige extrair a mesma matéria com outro, e a
    medição passava a ser o que o acervo lia. Foi o que aconteceu ao testar
    Haiku e Sonnet — a matéria 448 passou a entrar pelas triplas do teste, com
    os erros do teste, sem nada indicar isso.

    Prender ao modelo ativo é o comportamento certo por um motivo maior que
    limpeza de teste: a corroboração casa por string canônica exata, e modelos
    diferentes canonizam diferente ("Presidência da República" contra
    "Presidência da República do Brasil"). Acervo lido por dois modelos perde
    confirmações sem produzir erro nenhum — só um número menor.

    Filtrar só por vocabulário não
    basta: a mesma matéria extraída duas vezes — que é justamente o que
    `compare.py` exige para avaliar modelo ou prompt — entrava com as duas, e
    as duas leituras do mesmo texto viravam duas afirmações. A contagem por
    veículo sobrevive a isso, porque é um conjunto; a detecção de divergência
    numérica não, porque dois números ligeiramente diferentes tirados do mesmo
    texto pelo mesmo veículo passam a parecer contradição interna. Falso
    positivo produzido por artefato de avaliação — o pior tipo, porque some
    quando se para de avaliar.

    `desde` recorta por data de PUBLICAÇÃO, não pela data do fato. São coisas
    diferentes e a distinção importa: o digest reporta o que a imprensa
    publicou na janela, e uma matéria de hoje pode tratar de fato de semana
    passada. Recortar por `data_fato` esconderia justamente a matéria nova
    sobre fato antigo — que é quando a corroboração costuma aparecer.
    """
    filtro = "AND a.data_publicacao >= ?" if desde else ""
    linhas = conexao.execute(
        f"""
        SELECT t.sujeito_canonico s, t.relacao r, t.objeto_canonico o,
               t.valor_numero vn, t.valor_unidade vu, t.valor_contexto vc,
               t.data_fato df, t.origem og,
               a.veiculo, a.titulo, a.url_norm, a.data_publicacao dp
        FROM triplas t
        JOIN extracoes e ON e.id = t.extracao_id
        JOIN artigos   a ON a.id = e.artigo_id
        WHERE e.id IN (
                  SELECT MAX(id) FROM extracoes
                  WHERE vocab_versao = (SELECT MAX(vocab_versao) FROM extracoes)
                    AND modelo = ?
                  GROUP BY artigo_id
              )
        {filtro}
        """,
        (llm.EXTRACAO.id, desde) if desde else (llm.EXTRACAO.id,),
    ).fetchall()
    return [
        Afirmacao(l["s"], _relacao_normalizada(l["r"], l["o"], l["vn"]),
                  l["o"], l["vn"], l["vu"], l["vc"],
                  l["df"], l["og"], l["veiculo"], l["titulo"], l["url_norm"],
                  l["dp"])
        for l in linhas
    ]


def constroi(afirmacoes: list[Afirmacao]):
    """Monta o grafo dirigido: entidades como nós, afirmações como arestas.

    NetworkX em vez de banco de grafo porque a lógica é a parte difícil, não a
    infraestrutura. Migrar depois é mecânico; um servidor a mais agora, não.
    """
    import networkx as nx

    g = nx.MultiDiGraph()
    for a in afirmacoes:
        g.add_node(a.sujeito)
        alvo = a.objeto or f"[{a.relacao}]"
        g.add_node(alvo)
        g.add_edge(a.sujeito, alvo, relacao=a.relacao, veiculo=a.veiculo,
                   valor=a.valor, data_fato=a.data_fato, origem=a.origem,
                   url=a.url, titulo=a.titulo)
    return g


def _digitos(texto: str | None) -> frozenset[str]:
    return frozenset(re.findall(r"\d+", texto or ""))


def _mesma_medida(a: Afirmacao, b: Afirmacao, proximidade: float) -> bool:
    """Se duas afirmações com número medem a mesma coisa.

    Duas travas, porque nenhuma sozinha basta.

    A SEMÂNTICA erra em período. "lucro do 1º semestre de 2026" e "lucro do 2º
    trimestre de 2026" dão 0,93 — mais alto que pares que de fato são iguais.
    Fundi-los inventaria uma divergência entre 7,4 bi e 3,9 bi, que são dois
    fatos corretos sobre janelas diferentes.

    Os DÍGITOS pegam exatamente isso, e só isso: {1, 2026} contra {2, 2026} não
    é compatível. A comparação é por subconjunto, não igualdade, porque um
    veículo escreve "ante o 2º trimestre de 2025" e o outro "do 2º trimestre de
    2026 ante 2025" — o segundo diz mais, não diz outra coisa.

    E os dígitos sozinhos não veem "capital votante" contra "capital total",
    onde não há número nenhum. Por isso as duas.
    """
    da, db = _digitos(a.contexto), _digitos(b.contexto)
    if not (da <= db or db <= da):
        return False
    return proximidade >= MIN_CONTEXTO_IGUAL


def _separa_por_medida(membros: list[Afirmacao]) -> list[list[Afirmacao]]:
    """Divide afirmações de mesma chave em grupos que medem a mesma coisa.

    Só roda quando há mais de uma afirmação COM número. Fato sem valor não tem
    medida a separar, e chamar o modelo de embedding para eles seria custo de
    latência sem pergunta a responder.
    """
    com_valor = [m for m in membros if m.valor is not None]
    if len(com_valor) < 2:
        return [membros]

    from . import indice

    contextos = [m.contexto or "" for m in com_valor]
    vetores = indice.vetoriza(contextos)

    grupos: list[list[Afirmacao]] = []
    indices: list[list[int]] = []
    for i, atual in enumerate(com_valor):
        for grupo, idxs in zip(grupos, indices):
            # Compara com o primeiro do grupo, não com todos: agrupamento
            # transitivo por representante. Aproximação deliberada — comparar
            # todos contra todos mudaria o resultado conforme a ordem de
            # leitura, que é pior que ser aproximado de forma previsível.
            if _mesma_medida(atual, grupo[0],
                             float(vetores[i] @ vetores[idxs[0]])):
                grupo.append(atual)
                idxs.append(i)
                break
        else:
            grupos.append([atual])
            indices.append([i])

    sem_valor = [m for m in membros if m.valor is None]
    if sem_valor:
        grupos.append(sem_valor)
    return grupos


def agrupa(afirmacoes: list[Afirmacao]) -> list[Corroboracao]:
    """Junta afirmações idênticas em fato, para contar quem afirma o quê."""
    por_chave: dict[tuple, list[Afirmacao]] = collections.defaultdict(list)
    for a in afirmacoes:
        por_chave[a.chave].append(a)
    return [Corroboracao(chave, tuple(grupo))
            for chave, membros in por_chave.items()
            for grupo in _separa_por_medida(membros)]


def sobre(afirmacoes: list[Afirmacao], entidade: str) -> list[Afirmacao]:
    """Tudo que o acervo afirma sobre uma entidade, como sujeito ou objeto."""
    alvo = entidade.lower()
    return [
        a for a in afirmacoes
        if alvo in a.sujeito.lower() or (a.objeto and alvo in a.objeto.lower())
    ]


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    conexao = conecta(config.BANCO)
    afirmacoes = carrega(conexao)
    conexao.close()

    if not afirmacoes:
        print("Nenhuma afirmação no vocabulário atual. Rode a extração.")
        return

    if len(sys.argv) > 1:
        alvo = " ".join(sys.argv[1:])
        achadas = sobre(afirmacoes, alvo)
        print(f'{len(achadas)} afirmações sobre "{alvo}"\n')
        for a in sorted(achadas, key=lambda x: x.relacao):
            valor = ""
            if a.valor is not None:
                valor = f"  = {a.valor:g} {a.unidade or ''} ({a.contexto or ''})"
            marca = " " if a.origem == "EXTRACTED" else "~"
            print(f" {marca} ({a.sujeito}, {a.relacao}, {a.objeto or '—'}){valor}")
            print(f"      [{a.veiculo}] fato {a.data_fato}")
        return

    g = constroi(afirmacoes)
    grupos = agrupa(afirmacoes)
    confirmados = [c for c in grupos if c.confirmada]
    divergentes = [c for c in grupos if c.diverge]

    print(f"Grafo: {g.number_of_nodes()} entidades · {g.number_of_edges()} arestas")
    print(f"Fatos distintos: {len(grupos)}")
    print(f"Confirmados por 2+ veículos: {len(confirmados)}")
    print(f"Com divergência numérica: {len(divergentes)}\n")

    if confirmados:
        print("=== CONFIRMADOS POR FONTES INDEPENDENTES ===")
        for c in sorted(confirmados, key=lambda x: -len(x.veiculos))[:12]:
            suj, rel, obj, ctx = c.chave
            rotulo = f"({suj}, {rel}, {obj or '—'})" + (f" · {ctx}" if ctx else "")
            print(f"  {len(c.veiculos)} veículos · {rotulo}")
            for a in c.afirmacoes:
                valor = f" = {a.valor:g} {a.unidade or ''}" if a.valor is not None else ""
                print(f"      [{a.veiculo}]{valor}")

    if divergentes:
        print("\n=== NÚMEROS QUE NÃO BATEM ===")
        for c in divergentes:
            suj, rel, obj, ctx = c.chave
            print(f"  ({suj}, {rel}, {obj or '—'})" + (f" · {ctx}" if ctx else ""))
            for unidade, grupo in c.divergencias:
                veics = {a.veiculo for a in grupo}
                escopo = "entre veículos" if len(veics) > 1 else f"dentro de {list(veics)[0]}"
                print(f"      em {unidade} · {escopo}")
                for a in sorted(grupo, key=lambda x: x.valor):
                    print(f"        {a.valor:>18,.2f}  [{a.veiculo}] {a.titulo[:42]}")


if __name__ == "__main__":
    main()
