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
"""


def conecta(caminho: Path) -> sqlite3.Connection:
    """Abre o banco, criando arquivo e esquema se ainda não existirem."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conexao = sqlite3.connect(caminho)
    conexao.row_factory = sqlite3.Row
    conexao.executescript(ESQUEMA)
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
