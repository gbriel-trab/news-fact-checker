"""Índice vetorial do acervo.

    python -m src.indice          # reindexa e mostra os números
    python -m src.indice "texto"  # busca

Existe para casar por SIGNIFICADO o que não dá para casar por string. Três
problemas do projeto são o mesmo problema em lugares diferentes:

    entidade      "PL 2.234/2022" e "Projeto de Lei 2.234 de 2022"
    atribuição    "bets causam impactos negativos" e "o setor provoca impactos"
    afirmação     o que chega de fora e o que o acervo contém

Vocabulário fechado (relação) casa exato. Vocabulário aberto casa por
proximidade — e entidade nova aparece todo dia, paráfrase é infinita.

O modelo roda local, de propósito: o orçamento de chamada paga fica para
extração e verificação, onde o LLM é insubstituível. Embedding não é.

ARMADILHA: indexação e consulta precisam usar o MESMO modelo. Modelos
diferentes produzem sistemas de coordenadas diferentes, e a busca não falha —
devolve resultado sem sentido. Por isso o nome do modelo vai gravado nos
metadados da coleção e é conferido na abertura.
"""

import sqlite3
import sys
from dataclasses import dataclass
from functools import lru_cache

from . import config, llm
from .storage import conecta

MODELO_EMBEDDING = "paraphrase-multilingual-MiniLM-L12-v2"
"""Multilíngue porque o acervo é em português. Pequeno porque roda a cada
consulta e a diferença de qualidade não paga a de latência aqui."""

DIR_INDICE = config.DIR_DADOS / "chroma"


@dataclass(frozen=True, slots=True)
class Achado:
    texto: str
    distancia: float
    meta: dict

    @property
    def proximidade(self) -> float:
        """0 a 1, mais alto é mais parecido.

        A coleção usa distância de cosseno, que vai de 0 (idêntico) a 2
        (oposto). O padrão do Chroma é L2, cuja escala depende da magnitude dos
        vetores — o ranking sai certo, mas o número não significa nada, e é
        dele que o limiar de fusão de entidades depende."""
        return max(0.0, 1.0 - self.distancia / 2)


@lru_cache(maxsize=1)
def _modelo():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(MODELO_EMBEDDING)


@lru_cache(maxsize=1)
def _cliente():
    import chromadb

    return chromadb.PersistentClient(path=str(DIR_INDICE))


def _colecao(nome: str):
    """Abre ou cria a coleção, travando o modelo usado nos metadados.

    Se o modelo mudar, os vetores antigos passam a ser incomparáveis com os
    novos. Falhar alto aqui evita uma busca que responde coisa sem sentido sem
    dar erro nenhum.
    """
    cliente = _cliente()
    colecao = cliente.get_or_create_collection(
        name=nome,
        # hnsw:space precisa ser fixado na criação — não dá para mudar depois.
        metadata={"modelo": MODELO_EMBEDDING, "hnsw:space": "cosine"},
    )

    gravado = (colecao.metadata or {}).get("modelo")
    if gravado != MODELO_EMBEDDING:
        raise RuntimeError(
            f"Coleção '{nome}' foi construída com '{gravado}' e agora o código "
            f"usa '{MODELO_EMBEDDING}'. Os vetores não são comparáveis — apague "
            f"{DIR_INDICE} e reindexe."
        )
    return colecao


def _vetores(textos: list[str]) -> list[list[float]]:
    return _modelo().encode(textos, show_progress_bar=False).tolist()


# ------------------------------------------------------------------ indexação

def indexa_entidades(conexao: sqlite3.Connection) -> int:
    """Indexa as entidades canônicas distintas do acervo.

    É o que permite reconhecer que uma entidade que chega já existe registrada
    sob outro nome. Sujeito e objeto entram juntos: a mesma entidade aparece
    nos dois papéis conforme a frase.
    """
    # Mesmo recorte do resto: so o modelo ativo, uma extracao por materia.
    # Entidade canonizada por outro modelo entra com grafia diferente e
    # concorre com a legitima na busca por semelhanca.
    ativas = """
        SELECT t.sujeito_canonico s, t.objeto_canonico o
        FROM triplas t JOIN extracoes e ON e.id = t.extracao_id
        WHERE e.id IN (
                  SELECT MAX(id) FROM extracoes
                  WHERE vocab_versao = (SELECT MAX(vocab_versao) FROM extracoes)
                    AND modelo = ?
                  GROUP BY artigo_id
              )
    """
    linhas = conexao.execute(
        f"""
        SELECT nome, COUNT(*) AS n FROM (
            SELECT s AS nome FROM ({ativas})
            UNION ALL
            SELECT o FROM ({ativas}) WHERE o IS NOT NULL
        ) GROUP BY 1
        """,
        (llm.EXTRACAO.id, llm.EXTRACAO.id),
    ).fetchall()

    total: dict[str, int] = {}
    for linha in linhas:
        total[linha["nome"]] = total.get(linha["nome"], 0) + linha["n"]

    if not total:
        return 0

    nomes = sorted(total)
    colecao = _colecao("entidades")
    colecao.upsert(
        ids=nomes,
        documents=nomes,
        embeddings=_vetores(nomes),
        metadatas=[{"ocorrencias": total[n]} for n in nomes],
    )
    return len(nomes)


def indexa_afirmacoes(conexao: sqlite3.Connection) -> int:
    """Indexa cada tripla como frase legível.

    A tripla é indexada pelo que ela AFIRMA, não pela frase de origem: é assim
    que uma afirmação que chega de fora encontra a tripla equivalente, ainda
    que os dois textos não se pareçam.
    """
    linhas = conexao.execute(
        """
        SELECT t.id, t.sujeito_canonico s, t.relacao r, t.objeto_canonico o,
               t.valor_numero vn, t.valor_unidade vu, t.valor_contexto vc,
               t.data_fato df, t.origem og, t.sentenca sent,
               a.id AS artigo_id, a.veiculo, a.titulo, a.url_norm
        FROM triplas t
        JOIN extracoes e ON e.id = t.extracao_id
        JOIN artigos   a ON a.id = e.artigo_id
        -- Mesmo recorte de `grafo.carrega`, e pelo mesmo motivo: uma extracao
        -- por materia, do modelo ativo. O indice alimenta a evidencia que o
        -- check.py julga -- deixar entrar tripla de teste de outro modelo poe
        -- no veredito uma frase que o acervo nao contem.
        WHERE e.id IN (
                  SELECT MAX(id) FROM extracoes
                  WHERE vocab_versao = (SELECT MAX(vocab_versao) FROM extracoes)
                    AND modelo = ?
                  GROUP BY artigo_id
              )
        """,
        (llm.EXTRACAO.id,),
    ).fetchall()

    if not linhas:
        return 0

    textos, ids, metas = [], [], []
    for l in linhas:
        partes = [l["s"], l["r"].replace("_", " ")]
        if l["o"]:
            partes.append(l["o"])
        if l["vn"] is not None:
            valor = f"{l['vn']:g} {l['vu'] or ''}".strip()
            partes.append(valor + (f" ({l['vc']})" if l["vc"] else ""))
        textos.append(" ".join(partes))
        ids.append(str(l["id"]))
        metas.append({
            "artigo_id": l["artigo_id"], "veiculo": l["veiculo"],
            "titulo": l["titulo"], "url": l["url_norm"],
            "sujeito": l["s"], "relacao": l["r"], "objeto": l["o"] or "",
            "data_fato": l["df"] or "", "origem": l["og"], "sentenca": l["sent"],
            "valor": l["vn"] if l["vn"] is not None else "",
            "unidade": l["vu"] or "", "contexto": l["vc"] or "",
        })

    colecao = _colecao("afirmacoes")
    for i in range(0, len(textos), 200):
        fatia = slice(i, i + 200)
        colecao.upsert(
            ids=ids[fatia], documents=textos[fatia],
            embeddings=_vetores(textos[fatia]), metadatas=metas[fatia],
        )
    return len(textos)


# --------------------------------------------------------------------- busca

def busca(colecao_nome: str, texto: str, quantos: int = 8) -> list[Achado]:
    colecao = _colecao(colecao_nome)
    if colecao.count() == 0:
        return []

    r = colecao.query(
        query_embeddings=_vetores([texto]),
        n_results=min(quantos, colecao.count()),
    )
    return [
        Achado(texto=doc, distancia=dist, meta=meta)
        for doc, dist, meta in zip(
            r["documents"][0], r["distances"][0], r["metadatas"][0])
    ]


def vetoriza(textos: list[str]):
    """Vetores normalizados, para comparar muitos textos entre si.

    Normalizados na saida para o produto interno JA SER o cosseno -- quem chama
    compara com `v[i] @ v[j]` e nao precisa saber disso.
    """
    import numpy as np

    v = np.array(_vetores(textos))
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def similaridade(a: str, b: str) -> float:
    """Cosseno entre dois textos, de 0 a 1. Não consulta a coleção.

    Serve para conferir, sem gravar nada, se duas coisas que outro critério
    juntou de fato tratam do mesmo assunto.
    """
    import numpy as np

    v = np.array(_vetores([a, b]))
    v = v / np.linalg.norm(v, axis=1, keepdims=True)
    return max(0.0, float(v[0] @ v[1]))


def entidade_parecida(nome: str, minimo: float = 0.85) -> Achado | None:
    """A entidade já registrada mais próxima deste nome, se houver.

    O limiar é alto de propósito. Semelhança não é identidade: "Banco do
    Brasil" e "Banco Central do Brasil" são altamente parecidos e são
    instituições diferentes. Fundir duas entidades distintas fabricaria
    corroboração — duas fontes "concordando" sobre algo que uma delas nunca
    disse —, que é o falso positivo que o projeto considera o pior erro.
    """
    achados = busca("entidades", nome, quantos=1)
    if achados and achados[0].proximidade >= minimo:
        return achados[0]
    return None


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")

    conexao = conecta(config.BANCO)

    if len(sys.argv) > 1:
        alvo = " ".join(sys.argv[1:])
        print(f'Buscando: "{alvo}"\n')
        for a in busca("afirmacoes", alvo, quantos=6):
            m = a.meta
            print(f"  {a.proximidade:.0%}  {a.texto[:82]}")
            print(f"        [{m['veiculo']}] {m['titulo'][:62]}")
        conexao.close()
        return

    print(f"Modelo: {MODELO_EMBEDDING}")
    print("Indexando (a primeira vez baixa o modelo)...\n")
    print(f"  entidades  : {indexa_entidades(conexao)}")
    print(f"  afirmações : {indexa_afirmacoes(conexao)}")
    print(f"\nÍndice em {DIR_INDICE}")
    conexao.close()


if __name__ == "__main__":
    main()
