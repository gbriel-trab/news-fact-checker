"""Persistência do acervo em SQLite.

SQLite porque o acervo precisa ser consultável (é o índice de busca que o RSS
não oferece) sem exigir servidor rodando. A deduplicação vira restrição do
banco em vez de lógica na aplicação, o que a torna difícil de burlar por
engano.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Artigo, ResultadoGravacao

ESQUEMA = """
CREATE TABLE IF NOT EXISTS artigos (
    id               INTEGER PRIMARY KEY,
    url_norm         TEXT    NOT NULL,
    url_original     TEXT    NOT NULL,
    veiculo          TEXT    NOT NULL,
    editoria         TEXT    NOT NULL,
    titulo           TEXT    NOT NULL,
    resumo           TEXT    NOT NULL,
    conteudo         TEXT    NOT NULL,
    data_publicacao  TEXT,
    hash_conteudo    TEXT    NOT NULL,
    versao           INTEGER NOT NULL DEFAULT 1,
    coletado_em      TEXT    NOT NULL,

    -- O par (URL, hash) é o que define duplicata. Mesma URL com hash
    -- diferente não é duplicata: é a matéria editada, e entra como versão
    -- nova em vez de sobrescrever a anterior.
    UNIQUE (url_norm, hash_conteudo)
);

CREATE INDEX IF NOT EXISTS idx_artigos_url      ON artigos (url_norm);
CREATE INDEX IF NOT EXISTS idx_artigos_data     ON artigos (data_publicacao);
CREATE INDEX IF NOT EXISTS idx_artigos_veiculo  ON artigos (veiculo);
CREATE INDEX IF NOT EXISTS idx_artigos_editoria ON artigos (editoria);

-- Uma linha por (matéria, modelo, versão do prompt). O prompt mudou três vezes
-- durante a calibração, e triplas produzidas por versões diferentes não são
-- comparáveis entre si: sem registrar qual gerou o quê, o acervo vira mistura.
--
-- A restrição UNIQUE também evita pagar duas vezes pela mesma extração. Trocar
-- de modelo ou de prompt gera uma extração nova em vez de sobrescrever, o que
-- permite comparar as duas lado a lado.
CREATE TABLE IF NOT EXISTS extracoes (
    id              INTEGER PRIMARY KEY,
    artigo_id       INTEGER NOT NULL REFERENCES artigos (id),
    modelo          TEXT    NOT NULL,
    prompt_versao   TEXT    NOT NULL,
    vocab_versao    INTEGER NOT NULL,
    -- Total que entrou: fresco + cache lido + cache escrito. Mantido como
    -- soma para as linhas antigas continuarem significando a mesma coisa.
    tokens_entrada  INTEGER NOT NULL,
    -- A repartição. NULL nas linhas gravadas antes destas colunas existirem --
    -- que é a resposta honesta: para aquelas o dado nao foi guardado.
    --
    -- Precisa ficar separado porque os tres tem precos DIFERENTES: cache lido
    -- custa 0,1x da entrada e cache escrito custa 1,25x. Somados num campo so,
    -- o acervo registra o custo mas nao consegue mais dizer de onde ele veio,
    -- e a pergunta "o cache esta valendo a pena?" fica sem resposta.
    tokens_cache_leitura INTEGER,
    tokens_cache_escrita INTEGER,
    tokens_saida    INTEGER NOT NULL,
    custo_usd       REAL    NOT NULL,
    extraido_em     TEXT    NOT NULL,

    UNIQUE (artigo_id, modelo, prompt_versao)
);

CREATE TABLE IF NOT EXISTS triplas (
    id                INTEGER PRIMARY KEY,
    extracao_id       INTEGER NOT NULL REFERENCES extracoes (id) ON DELETE CASCADE,
    sentenca          INTEGER NOT NULL CHECK (typeof(sentenca) = 'integer'),

    sujeito           TEXT    NOT NULL,
    sujeito_canonico  TEXT    NOT NULL,
    relacao           TEXT    NOT NULL,
    -- Nulos quando a afirmação é atributo do sujeito, não relação com entidade.
    objeto            TEXT,
    objeto_canonico   TEXT,

    -- Os CHECK abaixo repetem validação que o Pydantic já faz na entrada. É
    -- defesa em profundidade: script de migração, correção manual ou import
    -- futuro não passam pelo Pydantic, e valor fora do domínio aqui não daria
    -- erro -- produziria aresta que a varredura de contradição ignora em
    -- silêncio, por não casar com nenhum dos dois tipos previstos.
    tipo_relacao      TEXT    NOT NULL CHECK (tipo_relacao IN ('evento', 'estado')),
    origem            TEXT    NOT NULL CHECK (origem IN ('EXTRACTED', 'INFERRED')),
    valor_numero      REAL,
    valor_unidade     TEXT,
    valor_contexto    TEXT,
    -- Nulo quando o texto não data o fato. Ver "Modelo da aresta".
    data_fato         TEXT
);

CREATE INDEX IF NOT EXISTS idx_triplas_extracao ON triplas (extracao_id);
-- Os dois índices por entidade canônica são o que torna a varredura de
-- contradição viável: ela agrupa por entidade e compara dentro do grupo.
CREATE INDEX IF NOT EXISTS idx_triplas_sujeito  ON triplas (sujeito_canonico);
CREATE INDEX IF NOT EXISTS idx_triplas_objeto   ON triplas (objeto_canonico);
CREATE INDEX IF NOT EXISTS idx_triplas_relacao  ON triplas (relacao);

-- Toda consulta ao check.py, com o que ela custou e o que respondeu.
--
-- Existe por dois motivos. O primeiro e contabil: sem isto o unico custo
-- registrado era o de extracao, e o relatorio subestimava o gasto real
-- chamando de "acumulado" o que era so uma parte.
--
-- O segundo importa mais. Este e o unico lugar onde fica o par
-- (afirmacao que chegou, veredito que saiu). E o conjunto de avaliacao do
-- sistema: sem ele nao ha como medir acerto contra Lupa ou Aos Fatos depois,
-- porque nao se sabe o que foi perguntado.
CREATE TABLE IF NOT EXISTS consultas (
    id             INTEGER PRIMARY KEY,
    afirmacao      TEXT    NOT NULL,
    veredito       TEXT    NOT NULL CHECK (
                       veredito IN ('confirmado', 'contradito', 'sem_evidencia')),
    justificativa  TEXT    NOT NULL,
    candidatas     INTEGER NOT NULL,
    citadas        INTEGER NOT NULL,
    veiculos       INTEGER NOT NULL,
    modelo         TEXT    NOT NULL,
    custo_usd      REAL    NOT NULL,
    consultado_em  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_consultas_data ON consultas (consultado_em);
"""


# Colunas acrescentadas depois de o banco existir. CREATE TABLE IF NOT EXISTS
# nao altera tabela ja criada -- sem isto, um banco antigo continuaria sem elas
# e a gravacao falharia com "no such column", que nao parece migracao faltando.
MIGRACOES: tuple[tuple[str, str], ...] = (
    ("extracoes", "tokens_cache_leitura INTEGER"),
    ("extracoes", "tokens_cache_escrita INTEGER"),
)


def _migra(conexao: sqlite3.Connection) -> None:
    """Acrescenta colunas que faltam. Nao remove nem renomeia nada.

    Só ADD COLUMN de propósito: é a operação que o SQLite faz sem reescrever a
    tabela e sem poder destruir dado. Migração que apaga fica para quando
    houver motivo e backup.
    """
    for tabela, definicao in MIGRACOES:
        coluna = definicao.split()[0]
        existentes = {linha[1] for linha
                      in conexao.execute(f"PRAGMA table_info({tabela})")}
        if coluna not in existentes:
            conexao.execute(f"ALTER TABLE {tabela} ADD COLUMN {definicao}")


def conecta(caminho: Path) -> sqlite3.Connection:
    """Abre o banco, criando arquivo e esquema se ainda não existirem."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conexao = sqlite3.connect(caminho)
    conexao.row_factory = sqlite3.Row
    conexao.executescript(ESQUEMA)
    _migra(conexao)
    conexao.commit()
    return conexao


def salva(conexao: sqlite3.Connection, artigo: Artigo) -> ResultadoGravacao:
    """Grava o artigo se ele for inédito ou uma nova versão de conhecido."""
    ja_visto = conexao.execute(
        "SELECT 1 FROM artigos WHERE url_norm = ? AND hash_conteudo = ? LIMIT 1",
        (artigo.url_norm, artigo.hash_conteudo),
    ).fetchone()
    if ja_visto:
        return ResultadoGravacao.DUPLICADO

    versao_anterior = conexao.execute(
        "SELECT MAX(versao) AS v FROM artigos WHERE url_norm = ?",
        (artigo.url_norm,),
    ).fetchone()["v"]

    versao = 1 if versao_anterior is None else versao_anterior + 1

    conexao.execute(
        """
        INSERT INTO artigos (
            url_norm, url_original, veiculo, editoria, titulo, resumo,
            conteudo, data_publicacao, hash_conteudo, versao, coletado_em
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artigo.url_norm,
            artigo.url_original,
            artigo.veiculo,
            artigo.editoria,
            artigo.titulo,
            artigo.resumo,
            artigo.conteudo,
            artigo.data_publicacao,
            artigo.hash_conteudo,
            versao,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conexao.commit()

    return ResultadoGravacao.NOVO if versao == 1 else ResultadoGravacao.ATUALIZADO


def estatisticas(conexao: sqlite3.Connection) -> dict[str, int]:
    """Números do acervo, para acompanhar o crescimento da coleta.

    `veiculos` conta redações distintas, não feeds: é a métrica que importa,
    porque corroboração exige fontes independentes.
    """
    linha = conexao.execute(
        """
        SELECT COUNT(*)                     AS registros,
               COUNT(DISTINCT url_norm)     AS materias,
               COUNT(DISTINCT veiculo)      AS veiculos,
               SUM(LENGTH(conteudo) + LENGTH(resumo)) AS bytes_texto
        FROM artigos
        """
    ).fetchone()
    return {
        "registros": linha["registros"] or 0,
        "materias": linha["materias"] or 0,
        "veiculos": linha["veiculos"] or 0,
        "bytes_texto": linha["bytes_texto"] or 0,
    }


def ja_extraido(conexao: sqlite3.Connection, artigo_id: int,
                modelo: str, prompt_versao: str) -> bool:
    """Diz se esta matéria já foi extraída por este modelo e prompt.

    Existe para não pagar duas vezes pela mesma chamada. Trocar de modelo ou
    mudar o prompt torna a resposta False, porque o resultado seria outro.
    """
    return conexao.execute(
        """
        SELECT 1 FROM extracoes
        WHERE artigo_id = ? AND modelo = ? AND prompt_versao = ?
        LIMIT 1
        """,
        (artigo_id, modelo, prompt_versao),
    ).fetchone() is not None


def salva_extracao(conexao: sqlite3.Connection, artigo_id: int, triplas,
                   modelo: str, prompt_versao: str, vocab_versao: int,
                   uso) -> int:
    """Grava uma extração e suas triplas numa transação. Devolve o id.

    Tudo ou nada: extração sem triplas, ou triplas órfãs, deixariam o acervo
    num estado que a varredura de contradição leria como ausência de fato.
    """
    with conexao:
        cursor = conexao.execute(
            """
            INSERT INTO extracoes (
                artigo_id, modelo, prompt_versao, vocab_versao,
                tokens_entrada, tokens_cache_leitura, tokens_cache_escrita,
                tokens_saida, custo_usd, extraido_em
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (artigo_id, modelo, prompt_versao, vocab_versao,
             uso.entrada + uso.cache_leitura + uso.cache_escrita,
             uso.cache_leitura, uso.cache_escrita,
             uso.saida, uso.custo, datetime.now(timezone.utc).isoformat()),
        )
        extracao_id = cursor.lastrowid

        conexao.executemany(
            """
            INSERT INTO triplas (
                extracao_id, sentenca, sujeito, sujeito_canonico, relacao,
                objeto, objeto_canonico, tipo_relacao, origem,
                valor_numero, valor_unidade, valor_contexto, data_fato
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (extracao_id, t.sentenca, t.sujeito, t.sujeito_canonico,
                 t.relacao, t.objeto, t.objeto_canonico, t.tipo_relacao,
                 t.origem, t.valor_numero, t.valor_unidade, t.valor_contexto,
                 t.data_fato)
                for t in triplas
            ],
        )
    return extracao_id


def salva_consulta(conexao: sqlite3.Connection, afirmacao: str, veredito: str,
                   justificativa: str, candidatas: int, citadas: int,
                   veiculos: int, modelo: str, custo: float) -> int:
    """Grava a consulta e o veredito. Devolve o id."""
    cursor = conexao.execute(
        """
        INSERT INTO consultas (afirmacao, veredito, justificativa, candidatas,
                               citadas, veiculos, modelo, custo_usd,
                               consultado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (afirmacao, veredito, justificativa, candidatas, citadas, veiculos,
         modelo, custo, datetime.now(timezone.utc).isoformat()),
    )
    conexao.commit()
    return cursor.lastrowid


def orfas(conexao: sqlite3.Connection,
          vocab_versao: int) -> tuple[int, float]:
    """Matérias pagas cujo resultado o acervo não enxerga.

    Extração sob vocabulário antigo é dinheiro gasto que não corrobora nada: o
    grafo a exclui — corretamente, porque relação de vocabulários diferentes
    não é comparável — e nada na saída dizia que ela existia.

    O sintoma é uma consulta responder "sem evidência" sobre matéria que está
    visivelmente no banco, e o sistema não ter como explicar a diferença. Foi
    exatamente o que aconteceu com a pesquisa do RS: o acervo tinha os 38% da
    Juliana Brizola, sob `liderou_pesquisa` e `obteve_percentual_em` de um
    vocabulário que não existe mais.
    """
    linha = conexao.execute(
        """
        SELECT COUNT(DISTINCT artigo_id) n, COALESCE(SUM(custo_usd), 0) c
        FROM extracoes
        WHERE vocab_versao < ?
          AND artigo_id NOT IN (SELECT artigo_id FROM extracoes
                                WHERE vocab_versao = ?)
        """,
        (vocab_versao, vocab_versao),
    ).fetchone()
    return linha["n"], linha["c"]


def estatisticas_triplas(conexao: sqlite3.Connection) -> dict[str, object]:
    """Números do acervo de triplas, para acompanhar o que já foi extraído."""
    linha = conexao.execute(
        """
        SELECT (SELECT COUNT(*) FROM triplas)                        AS triplas,
               (SELECT COUNT(*) FROM extracoes)                      AS materias,
               (SELECT COUNT(DISTINCT relacao) FROM triplas)         AS relacoes,
               (SELECT COUNT(DISTINCT sujeito_canonico) FROM triplas) AS entidades,
               (SELECT COALESCE(SUM(custo_usd), 0) FROM extracoes)   AS custo
        """
    ).fetchone()
    return dict(linha)
