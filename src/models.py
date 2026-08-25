"""Estruturas de dados da coleta."""

from dataclasses import dataclass
from enum import Enum


class ResultadoGravacao(str, Enum):
    """Desfecho de uma tentativa de gravar artigo no acervo."""

    NOVO = "novo"
    """URL inédita. Artigo armazenado."""

    DUPLICADO = "duplicado"
    """URL e conteúdo já conhecidos. Descartado."""

    ATUALIZADO = "atualizado"
    """URL conhecida, conteúdo diferente: a matéria foi editada.

    Gravado como nova versão, preservando a anterior. É por aqui que
    retratação e correção ficam detectáveis — veículo costuma corrigir
    editando a mesma página, sem mudar o endereço.
    """


@dataclass(frozen=True, slots=True)
class Artigo:
    """Uma matéria coletada de um feed, antes de qualquer processamento."""

    fonte: str
    """Nome do veículo, conforme configurado em FEEDS."""

    titulo: str
    url_original: str
    url_norm: str
    """URL sem parâmetros de rastreamento. É a chave de deduplicação."""

    resumo: str
    conteudo: str
    """Corpo da matéria quando o feed o oferece; string vazia quando não."""

    data_publicacao: str | None
    """ISO 8601 em UTC, ou None quando o feed não informa.

    Note que esta é a data em que a *fonte publicou*, não a data em que o
    fato ocorreu. As duas divergem no caso de matéria sobre fato antigo, e
    a segunda só será conhecida na etapa de extração.
    """

    hash_conteudo: str
    """SHA-256 de título + resumo + conteúdo. Detecta edição na mesma URL."""

    @property
    def texto(self) -> str:
        """O texto mais completo disponível para extração de afirmações.

        Os campos do RSS não são usados de forma consistente entre veículos.
        Medido na primeira coleta real: CNN e InfoMoney entregam a matéria em
        `content`, enquanto G1 e Agência Brasil a entregam em `summary`, com
        `content` vazio ou irrelevante. BBC publica apenas manchete e linha
        fina, em qualquer um dos dois.

        Confiar em `conteudo` sozinho descartaria o corpo de metade das
        fontes, então vale o mais longo entre os dois.
        """
        return max(self.conteudo, self.resumo, key=len)
