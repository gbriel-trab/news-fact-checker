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
    """Carrega o modelo de embedding, do cache local e sem tocar a rede.

    Offline por padrao. O modelo nao muda entre execucoes, entao consultar o
    HuggingFace a cada veredito nao decide nada -- so adiciona latencia, uma
    dependencia de rede que o resto do `check.py` nao tem, e um aviso de token
    impresso no meio da resposta, que parece defeito do sistema.

    Cai para online quando o cache ainda nao existe, que e a primeira execucao
    de quem clonou o repositorio.
    """
    import os

    # A variavel PRECISA ser definida antes do import: huggingface_hub le
    # HF_HUB_OFFLINE uma vez, no proprio import, e guarda numa constante.
    # Definir depois nao tem efeito nenhum e nao da erro -- so continua online.
    anterior = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    # Mesma regra da linha acima: antes do import, ou nao vale.
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    try:
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(MODELO_EMBEDDING)
    except Exception:
        # Sem cache ainda: precisa baixar mesmo. A barra de progresso aqui e
        # bem-vinda -- sao ~450 MB e o silencio pareceria travamento.
        if anterior is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = anterior
        # Reimporta: o modulo ja carregado guardou a constante antiga, e so um
        # processo novo a releria. Aqui basta porque a primeira execucao ainda
        # nao tinha o modulo carregado quando entrou no try.
        import importlib

        import huggingface_hub.constants

        importlib.reload(huggingface_hub.constants)
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


def texto_da_tripla(sujeito: str, relacao: str, objeto: str | None,
                    valor: float | None, unidade: str | None,
                    contexto: str | None) -> str:
    """A frase legivel de uma tripla, como ela entra no indice.

    Existe como funcao unica porque duas partes do sistema precisam produzir a
    MESMA string: a indexacao, e a recuperacao por chave exata, que monta o
    texto a partir do grafo em vez do indice. Duas renderizacoes que divergem
    fariam a mesma tripla aparecer com dois textos diferentes na mesma resposta.
    """
    partes = [sujeito, relacao.replace("_", " ")]
    if objeto:
        partes.append(objeto)
    if valor is not None:
        medida = f"{valor:g} {unidade or ''}".strip()
        partes.append(medida + (f" ({contexto})" if contexto else ""))
    return " ".join(partes)


# ------------------------------------------------------------------ indexação

def indexa_artigos(conexao: sqlite3.Connection, dias: int = 10) -> int:
    """Indexa título+lead das matérias RECENTES — o índice do COLETADO.

    As outras coleções indexam o que foi extraído; esta indexa o que foi
    apenas coletado, e existe para a extração sob demanda (`demanda`):
    premissa sem cobertura procura aqui a matéria que o seletor não
    priorizou. A janela é a mesma do agrupamento, pelo mesmo motivo —
    premissa de post é sobre o agora, e matéria sem `data_publicacao`
    fica de fora com ela.

    Upsert por id, e só embeda o que ainda não está na coleção: rodar a
    cada premissa do boletim custa uma consulta ao Chroma, não a janela
    inteira de embeddings. Devolve quantas matérias NOVAS entraram.
    """
    from . import agrupa

    linhas = agrupa.carrega(conexao, dias)
    if not linhas:
        return 0
    colecao = _colecao("artigos")
    presentes = set(colecao.get(
        ids=[str(l["id"]) for l in linhas], include=[])["ids"])
    novas = [l for l in linhas if str(l["id"]) not in presentes]
    # Em lotes, como indexa_afirmacoes: o Chroma recusa upsert acima de
    # ~5.4k itens, e a primeira indexação de uma janela cheia passa disso
    # (5.666 matérias em 01/09/2026, descoberto no primeiro uso).
    for i in range(0, len(novas), 200):
        lote = novas[i:i + 200]
        colecao.upsert(
            ids=[str(l["id"]) for l in lote],
            embeddings=_vetores(
                [agrupa.texto_de_agrupamento(l) for l in lote]),
            documents=[l["titulo"] for l in lote],
            metadatas=[{"artigo_id": l["id"], "veiculo": l["veiculo"],
                        "titulo": l["titulo"],
                        "data": l["data_publicacao"] or ""}
                       for l in lote],
        )
    return len(novas)


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
                  -- Prefere extração COM tripla: o modo história grava
                  -- linha vazia como marcador, e MAX(id) cru deixava o
                  -- vazio superar triplas boas (revisão de 01/09/2026).
                  SELECT (SELECT e3.id FROM extracoes e3
                          WHERE e3.artigo_id = e2.artigo_id
                            AND e3.vocab_versao = e2.vocab_versao
                            AND e3.modelo = e2.modelo
                          ORDER BY (SELECT COUNT(*) FROM triplas t2
                                    WHERE t2.extracao_id = e3.id) > 0 DESC,
                                   e3.id DESC
                          LIMIT 1)
                  FROM extracoes e2
                  WHERE e2.vocab_versao = (SELECT MAX(vocab_versao)
                                           FROM extracoes)
                    AND e2.modelo = ?
                  GROUP BY e2.artigo_id
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


def indexa_afirmacoes(conexao: sqlite3.Connection,
                      so_artigos: list[int] | None = None) -> int:
    """Indexa cada tripla como frase legível.

    A tripla é indexada pelo que ela AFIRMA, não pela frase de origem: é assim
    que uma afirmação que chega de fora encontra a tripla equivalente, ainda
    que os dois textos não se pareçam.

    `so_artigos` restringe às triplas dessas matérias — é o caminho da
    extração sob demanda, que indexa meia dúzia de triplas novas por vez.
    Sem o filtro, cada extração de demanda re-embedava o recorte INTEIRO
    (minutos de CPU por rodada, crescendo com o acervo — revisão de
    01/09/2026). A reindexação completa continua sendo o padrão do CLI.
    """
    filtro_artigo = ""
    extras: tuple = ()
    if so_artigos is not None:
        if not so_artigos:
            return 0
        filtro_artigo = (
            f" AND a.id IN ({','.join('?' * len(so_artigos))})")
        extras = tuple(so_artigos)
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
                  -- Prefere extração COM tripla: o modo história grava
                  -- linha vazia como marcador, e MAX(id) cru deixava o
                  -- vazio superar triplas boas (revisão de 01/09/2026).
                  SELECT (SELECT e3.id FROM extracoes e3
                          WHERE e3.artigo_id = e2.artigo_id
                            AND e3.vocab_versao = e2.vocab_versao
                            AND e3.modelo = e2.modelo
                          ORDER BY (SELECT COUNT(*) FROM triplas t2
                                    WHERE t2.extracao_id = e3.id) > 0 DESC,
                                   e3.id DESC
                          LIMIT 1)
                  FROM extracoes e2
                  WHERE e2.vocab_versao = (SELECT MAX(vocab_versao)
                                           FROM extracoes)
                    AND e2.modelo = ?
                  GROUP BY e2.artigo_id
              )
        """ + filtro_artigo,
        (llm.EXTRACAO.id,) + extras,
    ).fetchall()

    if not linhas:
        return 0

    textos, ids, metas = [], [], []
    for l in linhas:
        textos.append(texto_da_tripla(l["s"], l["r"], l["o"], l["vn"],
                                      l["vu"], l["vc"]))
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
