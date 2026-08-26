"""Camada de chamada de modelo.

Isola a conversa com a API atrás de uma função. O resto do código entrega
instruções, conteúdo e um schema, e recebe o objeto validado de volta — sem
saber qual modelo atendeu nem como.

O isolamento vale mesmo com um provedor só: mantém `extract.py` legível,
concentra num lugar as decisões de modelo, limite de tokens e cache, e permite
trocar de modelo sem tocar em quem chama.
"""

import os

from pydantic import BaseModel

# Import por efeito colateral: config carrega o .env no ambiente. Sem ele, a
# chave da Anthropic ficaria invisível e a falha apareceria como "credencial
# ausente", sem indicar a causa.
from . import config  # noqa: F401

MODELO = "claude-opus-5"

MAX_TOKENS_SAIDA = 8000


class FalhaNoModelo(Exception):
    """O modelo respondeu, mas não com o que o schema exige."""


def gera(system: str, user: str, esquema: type[BaseModel]) -> BaseModel:
    """Manda o par system/user ao modelo e devolve o objeto validado.

    O schema não é pedido no prompt: vai como restrição da chamada, e a API
    garante que a resposta o satisfaça. É o que sustenta o vocabulário
    controlado de relações — pedir no texto seria sugestão, não garantia.
    """
    import anthropic

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise FalhaNoModelo(
            "ANTHROPIC_API_KEY não está definida. Preencha no .env "
            "(a chave é criada em https://console.anthropic.com/settings/keys) "
            "e defina um limite de gasto mensal no console antes de rodar."
        )

    cliente = anthropic.Anthropic()
    resposta = cliente.messages.parse(
        model=MODELO,
        max_tokens=MAX_TOKENS_SAIDA,
        # O bloco de instruções é idêntico em toda chamada, então vai marcado
        # para cache. ATENÇÃO: o prefixo mínimo cacheável é ~1024 tokens, e
        # hoje este bloco tem ~625 — ou seja, a marcação ainda não faz efeito
        # e a falha é silenciosa. Ela passa a valer quando o vocabulário
        # fechado e mais exemplos entrarem nas instruções. Conferir em
        # `usage.cache_read_input_tokens` antes de contar com a economia.
        system=[{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user}],
        output_format=esquema,
    )
    return resposta.parsed_output


def descricao() -> str:
    """Identificação do modelo ativo, para relatório e para gravar junto das
    triplas — comparar extrações sem saber qual modelo as produziu é inútil."""
    return f"anthropic:{MODELO}"
