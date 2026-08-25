"""Segmentação de texto em sentenças.

A unidade de trabalho da extração é a sentença, não a matéria: o modelo recebe
as sentenças numeradas e devolve, em cada tripla, o índice de onde ela saiu.
Isso mantém a rastreabilidade sem que a resposta precise repetir o texto de
origem — que já está no banco, e cujo reenvio inflaria o custo de saída.

Implementação por regex, sem dependência externa. Não é perfeita; é suficiente
para texto jornalístico e pode ser trocada por spaCy sem afetar o resto.
"""

import re

# Abreviações que terminam em ponto sem terminar a frase. Sem esta lista,
# "Sr. Silva afirmou" viraria duas sentenças e a segunda perderia o sujeito.
_ABREVIACOES = (
    "sr", "sra", "srs", "dr", "dra", "prof", "profa", "eng", "adv",
    "av", "r", "pça", "ltda", "cia", "art", "arts", "inc", "pág", "págs",
    "ed", "op", "cit", "etc", "ex", "aprox", "séc", "vs", "obs", "ref",
    "min", "max", "nº", "no", "n", "fl", "fls", "cap", "vol",
)

_PROTEGE_ABREV = re.compile(
    r"\b(" + "|".join(_ABREVIACOES) + r")\.",
    re.IGNORECASE,
)

# Ponto entre dígitos é separador de milhar ou parte de versão, nunca fim de
# frase: "R$ 1.000" e "Lei 8.666" não podem ser quebrados.
_PROTEGE_NUMERO = re.compile(r"(?<=\d)\.(?=\d)")

# Inicial isolada em nome próprio: "Luiz I. da Silva".
_PROTEGE_INICIAL = re.compile(r"\b([A-ZÁÉÍÓÚÂÊÔÃÕÇ])\.")

_MARCA = "\x00"

# Quebra em . ! ? seguidos de espaço e início de nova frase (maiúscula, aspa
# ou travessão). Reticências contam como um terminador só.
_FIM_DE_FRASE = re.compile(
    r"(?<=[.!?…])[\"'”’)\]]*\s+(?=[\"'“‘(\[—–]*[A-ZÁÉÍÓÚÂÊÔÃÕÇ0-9])"
)

_MIN_PALAVRAS = 3

_PALAVRA = re.compile(r"\w+", re.UNICODE)


def em_sentencas(texto: str) -> list[str]:
    """Divide o texto em sentenças, descartando fragmentos sem conteúdo.

    O corte é por contagem de palavras, não de caracteres. Comprimento é o
    critério errado: "Foto: Reuters." tem 14 caracteres e é lixo, e "O STF
    decidiu." também tem 14 e é uma afirmação verificável. O que separa os dois
    é ter sujeito e verbo, e três palavras é a aproximação barata disso.

    O descarte se limita a fragmento que não poderia sustentar afirmação
    nenhuma. Filtrar conteúdo — separar factual de opinião — é trabalho de
    outra etapa, e não deve ser feito aqui às escondidas.
    """
    if not texto or not texto.strip():
        return []

    protegido = _PROTEGE_ABREV.sub(lambda m: m.group(0)[:-1] + _MARCA, texto)
    protegido = _PROTEGE_NUMERO.sub(_MARCA, protegido)
    protegido = _PROTEGE_INICIAL.sub(lambda m: m.group(1) + _MARCA, protegido)

    partes = _FIM_DE_FRASE.split(protegido)

    sentencas = []
    for parte in partes:
        limpa = parte.replace(_MARCA, ".").strip()
        if len(_PALAVRA.findall(limpa)) >= _MIN_PALAVRAS:
            sentencas.append(limpa)
    return sentencas
