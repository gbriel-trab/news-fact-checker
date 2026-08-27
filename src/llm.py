"""Camada de chamada de modelo.

Isola a conversa com a API atrás de uma função. O resto do código entrega
instruções, conteúdo e um schema, e recebe o objeto validado de volta — sem
saber qual modelo atendeu nem como.

O isolamento vale mesmo com um provedor só: mantém `extract.py` legível,
concentra num lugar as decisões de modelo, limite de tokens e cache, e permite
trocar de modelo sem tocar em quem chama.
"""

import os
from dataclasses import dataclass

from pydantic import BaseModel

# Import por efeito colateral: config carrega o .env no ambiente. Sem ele, a
# chave da Anthropic ficaria invisível e a falha apareceria como "credencial
# ausente", sem indicar a causa.
from . import config  # noqa: F401

MODELO = "claude-opus-5"

ESFORCO = "medium"
"""Profundidade de raciocínio, de low a max. O padrão da API é high.

Baixado para medium por decisão explícita: raciocínio é cobrado como saída, a
saída é ~87% do custo, e cerca de metade dela eram tokens de raciocínio. A
extração tem schema rígido e vocabulário fechado, o que restringe o espaço de
resposta — é onde deliberação máxima menos rende.

Trocar isto muda o custo e a qualidade juntos, e a comparação está registrada
no README."""

MAX_TOKENS_SAIDA = 16000
"""Teto de tokens gerados.

Era 8000 e truncou numa materia de 51 sentencas: a resposta cortou no meio de
uma string e o JSON ficou invalido. Truncamento e falha PAGA -- o modelo gera
ate o teto, a chamada e cobrada, e nao volta nada aproveitavel.

16000 cobre com folga a materia mais longa do acervo. Acima disso a SDK pede
streaming para nao esbarrar em timeout de HTTP."""

# US$ por milhao de tokens. Cache lido custa 0,1x da entrada; cache escrito,
# 1,25x. Numeros usados so para relatorio -- a fatura real esta no console.
PRECO = {"entrada": 5.00, "saida": 25.00, "cache_leitura": 0.50, "cache_escrita": 6.25}


@dataclass(frozen=True, slots=True)
class Uso:
    """Consumo de uma chamada. Existe para que custo seja medido, nao estimado."""

    entrada: int
    saida: int
    cache_leitura: int
    cache_escrita: int

    @property
    def custo(self) -> float:
        return (
            self.entrada * PRECO["entrada"]
            + self.saida * PRECO["saida"]
            + self.cache_leitura * PRECO["cache_leitura"]
            + self.cache_escrita * PRECO["cache_escrita"]
        ) / 1_000_000

    def __str__(self) -> str:
        cache = ""
        if self.cache_leitura or self.cache_escrita:
            cache = f" · cache {self.cache_leitura}r/{self.cache_escrita}w"
        return (f"{self.entrada} entrada · {self.saida} saida{cache}"
                f" · US$ {self.custo:.4f}")


@dataclass(frozen=True, slots=True)
class Resposta:
    dados: BaseModel
    uso: Uso


class FalhaNoModelo(Exception):
    """O modelo respondeu, mas não com o que o schema exige."""


def gera(system: str, user: str, esquema: type[BaseModel]) -> Resposta:
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
    try:
        resposta = cliente.messages.parse(
            model=MODELO,
            max_tokens=MAX_TOKENS_SAIDA,
            # O bloco de instruções é idêntico em toda chamada e vai marcado
            # para cache. Passou a valer quando o vocabulário fechado entrou:
            # o prefixo mínimo cacheável é ~1024 tokens e o bloco hoje tem
            # ~2000. Confirmado em execução — a segunda matéria de cada rodada
            # lê o prefixo do cache em vez de reenviá-lo.
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user}],
            output_format=esquema,
            output_config={"effort": ESFORCO},
        )
    except Exception as erro:
        # Resposta truncada chega aqui como erro de JSON invalido, o que parece
        # defeito de schema e nao e. Nomear a causa evita procurar no lugar
        # errado -- e lembrar que foi cobrado evita repetir sem ajustar.
        if "Invalid JSON" in str(erro) or "EOF while parsing" in str(erro):
            raise FalhaNoModelo(
                f"Resposta truncada: a geracao bateu no teto de "
                f"{MAX_TOKENS_SAIDA} tokens antes de fechar o JSON. "
                f"A chamada foi cobrada mesmo assim. Materia longa demais para "
                f"o teto atual, ou triplas demais por materia."
            ) from erro
        raise

    u = resposta.usage
    return Resposta(
        dados=resposta.parsed_output,
        uso=Uso(
            entrada=u.input_tokens,
            saida=u.output_tokens,
            cache_leitura=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_escrita=getattr(u, "cache_creation_input_tokens", 0) or 0,
        ),
    )


def descricao() -> str:
    """Identificação do modelo ativo, para relatório e para gravar junto das
    triplas — comparar extrações sem saber qual modelo as produziu é inútil."""
    return f"anthropic:{MODELO}"
