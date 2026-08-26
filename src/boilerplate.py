"""Remoção de texto que não é notícia.

Feed de RSS traz, junto da matéria, coisas que o veículo escreveu sobre si
mesmo: chamada de podcast, link para outra reportagem, convite de newsletter.
Isso é factual e é inútil — ninguém vai checar se um podcast pertence a um
jornal —, e cada frase dessas custa tokens de entrada e produz tripla que
jamais será corroborada.

Duas regras, ambas determinísticas e auditáveis. A escolha por regra em vez de
julgamento do modelo é deliberada: pedir ao modelo que ignore rodapé faz ele
errar nos dois sentidos, e o erro caro — descartar notícia achando que é
rodapé — seria silencioso. Aqui dá para imprimir exatamente o que foi cortado.

Regra 1, repetição. Frase que aparece em várias matérias do mesmo veículo é
institucional por definição: notícia não se repete palavra por palavra. Melhora
sozinha conforme o acervo cresce.

Regra 2, marcador. Bloco de link começa com formas fixas ("Leia também",
"Assine"). A repetição não pega esses, porque cada chamada aponta para uma
matéria diferente e o texto muda — só o começo é constante.
"""

import collections
import re
import sqlite3

MARCADORES: tuple[str, ...] = (
    "leia também", "leia mais", "leia ainda", "veja também", "veja mais",
    "saiba mais", "confira também", "confira abaixo", "clique aqui",
    "assine ", "inscreva-se", "siga o ", "siga a ", "acompanhe o ",
    "com informações de", "matéria em atualização", "reportagem em atualização",
)
"""Começos que denunciam bloco de navegação. Lista fechada e curta: cada entrada
precisa ser inequívoca, porque um marcador ambíguo cortaria notícia real."""

MIN_MATERIAS = 8
"""Mínimo de matérias de um veículo para a contagem valer. Com poucas, uma
frase legítima que aparecesse em duas delas viraria 'repetida'."""

MIN_OCORRENCIAS = 4
"""Vezes que a frase precisa aparecer para ser considerada institucional."""

MIN_DIAS_DISTINTOS = 2
"""Datas de publicação distintas em que a frase precisa aparecer.

Sem isto a regra confunde institucional com cobertura repetida do mesmo fato.
Aconteceu: "A notícia da sua morte foi compartilhada por sua família nas redes
sociais" apareceu em quatro matérias da CNN sobre a mesma morte, no mesmo dia,
e seria cortada como rodapé.

Texto institucional acompanha o veículo por meses; parágrafo repetido de uma
cobertura vive dias. Exigir datas distintas separa os dois — de forma
imperfeita, porque uma cobertura pode durar dois dias. A separação melhora
conforme o acervo cobre mais tempo, e o que for cortado aparece no relatório
da rodada para conferência."""

_NORMALIZA = re.compile(r"\s+")


def _chave(frase: str) -> str:
    """Forma comparável da frase: minúscula, espaços colapsados."""
    return _NORMALIZA.sub(" ", frase.strip().lower())


def tem_marcador(frase: str) -> bool:
    """Diz se a frase começa com um marcador de bloco de navegação."""
    inicio = _chave(frase)[:40]
    return any(inicio.startswith(m) for m in MARCADORES)


def frases_repetidas(conexao: sqlite3.Connection, veiculo: str,
                     segmentar) -> set[str]:
    """Frases que se repetem entre matérias deste veículo.

    `segmentar` é injetada em vez de importada para manter este módulo
    independente da segmentação — e para os testes não dependerem dela.
    """
    linhas = conexao.execute(
        """
        SELECT conteudo, resumo, SUBSTR(data_publicacao, 1, 10) AS dia
        FROM artigos
        WHERE veiculo = ? AND MAX(LENGTH(conteudo), LENGTH(resumo)) > 800
        """,
        (veiculo,),
    ).fetchall()

    if len(linhas) < MIN_MATERIAS:
        return set()

    contagem: collections.Counter[str] = collections.Counter()
    dias: dict[str, set[str]] = collections.defaultdict(set)

    for linha in linhas:
        texto = max(linha["conteudo"], linha["resumo"], key=len)
        # `set` por matéria: frase repetida dentro de uma só não conta, senão
        # uma matéria com refrão inflaria a contagem sozinha.
        for frase in {_chave(f) for f in segmentar(texto)}:
            contagem[frase] += 1
            if linha["dia"]:
                dias[frase].add(linha["dia"])

    return {
        f for f, n in contagem.items()
        if n >= MIN_OCORRENCIAS and len(dias[f]) >= MIN_DIAS_DISTINTOS
    }


def filtra(frases: list[str], repetidas: set[str]) -> tuple[list[str], list[str]]:
    """Separa as frases aproveitáveis das institucionais.

    Devolve as duas listas — as removidas voltam para que a rodada possa
    mostrá-las. Filtro que corta em silêncio não pode ser conferido, e este
    corta antes de o texto chegar ao modelo.
    """
    limpas, removidas = [], []
    for frase in frases:
        if tem_marcador(frase) or _chave(frase) in repetidas:
            removidas.append(frase)
        else:
            limpas.append(frase)
    return limpas, removidas
