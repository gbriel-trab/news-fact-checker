"""Camada de chamada de modelo.

Isola a conversa com a API atrás de uma função. O resto do código entrega
instruções, conteúdo e um schema, e recebe o objeto validado de volta — sem
saber qual modelo atendeu nem como.

O isolamento vale mesmo com um provedor só: mantém `extract.py` legível,
concentra num lugar as decisões de modelo, limite de tokens e cache, e permite
trocar de modelo sem tocar em quem chama.

O modelo é escolhido POR CHAMADA, não pela conta. Não há nada a configurar no
console — a mesma chave atende todos os modelos, e o console só trata de
cobrança, limite de gasto e chaves.
"""

import os
from dataclasses import dataclass

from pydantic import BaseModel

# Import por efeito colateral: config carrega o .env no ambiente. Sem ele, a
# chave da Anthropic ficaria invisível e a falha apareceria como "credencial
# ausente", sem indicar a causa.
from . import config  # noqa: F401

MAX_TOKENS_SAIDA = 16000
"""Teto de tokens gerados.

Era 8000 e truncou numa materia de 51 sentencas: a resposta cortou no meio de
uma string e o JSON ficou invalido. Truncamento e falha PAGA -- o modelo gera
ate o teto, a chamada e cobrada, e nao volta nada aproveitavel.

16000 cobre com folga a materia mais longa do acervo. Acima disso a SDK pede
streaming para nao esbarrar em timeout de HTTP."""


@dataclass(frozen=True, slots=True)
class Modelo:
    """Um modelo e o que muda junto com ele: preço e profundidade de raciocínio.

    Preço fica colado ao modelo de propósito. Antes era uma tabela solta do
    módulo, correta enquanto havia um modelo só — no instante em que passou a
    haver dois, uma tabela solta reportaria o custo do Opus para uma chamada de
    Haiku e ninguém perceberia, porque o número continua plausível.
    """

    id: str
    entrada: float
    """US$ por milhão de tokens de entrada."""
    saida: float
    """US$ por milhão de tokens de saída. Raciocínio conta como saída."""
    esforco: str | None
    """Profundidade de raciocínio, de low a max. `None` omite o parâmetro.

    Nem todo modelo aceita `output_config`. Omitir é o padrão seguro: mandar um
    parâmetro que o modelo não conhece derruba a chamada inteira, e uma chamada
    derrubada por configuração é mais barata de evitar do que de diagnosticar.
    """

    # Multiplicadores fixos da API: cache lido custa 0,1x da entrada, cache
    # escrito 1,25x. Derivados em vez de digitados por modelo — número copiado
    # à mão em três lugares erra em um deles.
    @property
    def cache_leitura(self) -> float:
        return self.entrada * 0.10

    @property
    def cache_escrita(self) -> float:
        return self.entrada * 1.25


# Preços conferidos em https://claude.com/pricing — confira antes de confiar
# no relatório de custo. A fatura real está no console.
HAIKU = Modelo("claude-haiku-4-5-20251001", entrada=1.00, saida=5.00,
               esforco=None)
SONNET = Modelo("claude-sonnet-5", entrada=3.00, saida=15.00, esforco="medium")
OPUS = Modelo("claude-opus-5", entrada=5.00, saida=25.00, esforco="medium")


EXTRACAO = HAIKU
"""O modelo do volume. Uma chamada por matéria, e é onde o acervo cresce.

Haiku porque o que segura a qualidade aqui não é o modelo, é o schema: o
vocabulário de relações é imposto pela API, os campos são obrigatórios e a
sentença de origem é indexada. É leitura estruturada, não raciocínio aberto —
onde modelo menor menos degrada.

O risco que sobra não é errar a relação, é canonizar entidade de forma
instável: "Braskem" numa matéria e "Braskem S.A." noutra faz a corroboração
não contar. Isso é falso negativo — o acervo perde uma confirmação que existia.
Ruim, mas do lado seguro: o erro que este projeto considera o pior é o
contrário, confirmar o que não foi confirmado.

NÃO DECIDIDO POR MEDIÇÃO. Trocar isto exige rodar `compare.py` sobre a mesma
matéria extraída pelos dois. E o piso de ruído já medido nesta base é 40 a 48%
de triplas idênticas entre DUAS RODADAS DO MESMO OPUS — abaixo disso não se
conclui nada sobre modelo nenhum."""

VERIFICACAO = OPUS
"""O modelo do julgamento. Uma chamada por consulta, custo desprezível.

Fica no forte porque é o único ponto onde o modelo precisa resistir à vontade
de agradar: `sem_evidencia` é a resposta certa com frequência, e devolvê-la
contraria o hábito de completar o que foi pedido. Economizar aqui rende
centavos e arrisca a única coisa que o sistema vende.

Também não medido — é prior, não resultado."""


@dataclass(frozen=True, slots=True)
class Uso:
    """Consumo de uma chamada. Existe para que custo seja medido, nao estimado."""

    modelo: Modelo
    entrada: int
    saida: int
    cache_leitura: int
    cache_escrita: int

    @property
    def custo(self) -> float:
        m = self.modelo
        return (
            self.entrada * m.entrada
            + self.saida * m.saida
            + self.cache_leitura * m.cache_leitura
            + self.cache_escrita * m.cache_escrita
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


def gera(system: str, user: str, esquema: type[BaseModel],
         modelo: Modelo = EXTRACAO) -> Resposta:
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

    extras = {}
    if modelo.esforco:
        extras["output_config"] = {"effort": modelo.esforco}

    cliente = anthropic.Anthropic()
    try:
        resposta = cliente.messages.parse(
            model=modelo.id,
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
            **extras,
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
            modelo=modelo,
            entrada=u.input_tokens,
            saida=u.output_tokens,
            cache_leitura=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_escrita=getattr(u, "cache_creation_input_tokens", 0) or 0,
        ),
    )


def descricao(modelo: Modelo) -> str:
    """Identificação do modelo, para relatório e para gravar junto das
    triplas — comparar extrações sem saber qual modelo as produziu é inútil."""
    return f"anthropic:{modelo.id}"
