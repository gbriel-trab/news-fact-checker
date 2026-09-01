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
#
# Corrigido em 01/09/2026: o Sonnet 5 estava com US$ 3/15, que é o preço do
# Sonnet 4.6 — o certo é US$ 2/10. Consequência para o registro: o
# experimento Sonnet×Opus da verificação (31/08) SUPERESTIMOU o custo do
# Sonnet em ~33% — a média real era ~US$ 0,0064/consulta, não 0,0096. Não
# muda a decisão (ela foi por qualidade, não por preço), mas muda a conta.
HAIKU = Modelo("claude-haiku-4-5-20251001", entrada=1.00, saida=5.00,
               esforco=None)
SONNET = Modelo("claude-sonnet-5", entrada=2.00, saida=10.00, esforco="medium")
OPUS = Modelo("claude-opus-5", entrada=5.00, saida=25.00, esforco="medium")


EXTRACAO = OPUS
"""O modelo do volume. Uma chamada por matéria, e é onde o acervo cresce.

DECIDIDO POR MEDIÇÃO, na matéria 448 (TRE-SP nega recurso de Marçal), com o
mesmo prompt e o mesmo texto. Não é n suficiente para lei geral; é suficiente
para não trocar às cegas.

                 saída    custo    chave igual ao Opus    erro grave
    Opus 5        1813  $0,0515          10/10            nenhum
    Haiku 4.5     1235  $0,0132           2/8             2 de 8
    Sonnet 5      3229  $0,0751           3/9             1 de 9 + 1 duplicada

O Haiku pendurou a multa de R$ 420 mil no tribunal em vez de em quem foi
multado, e inverteu `abriu_processo_contra` — pôs o condenado como autor do
processo. Erro de SUJEITO, que é a categoria que este acervo não pode ter:
sua única mercadoria é quem disse e quem fez o quê.

O Sonnet acertou o falante e errou o objeto da fala — devolveu
(Manfré, afirmou, Pablo Marçal), que é o exemplo textual do que a regra 10
proíbe. E gerou a mesma tripla duas vezes.

SOBRE O CUSTO DO SONNET: ele é mais barato por token que o Opus e saiu MAIS
CARO na mesma matéria, porque gerou 78% mais saída. Preço por token não prevê
custo por matéria — o que prevê é quantos tokens o modelo decide gastar.

O caminho de economia que sobra não é trocar de modelo: é o Batch API, 50% de
desconto sem custo de qualidade, e o cache. Estas três medições pagaram
escrita de cache (0r/5233w) por rodarem uma matéria de cada vez; num lote de
40, só a primeira paga escrita e o resto lê a 0,1x."""

VERIFICACAO = OPUS
"""O modelo do julgamento. Uma chamada por consulta, custo desprezível.

Fica no forte porque é o único ponto onde o modelo precisa resistir à vontade
de agradar: `sem_evidencia` é a resposta certa com frequência, e devolvê-la
contraria o hábito de completar o que foi pedido. Economizar aqui rende
centavos e arrisca a única coisa que o sistema vende.

MEDIDO em 31/08/2026, preliminar: as 25 afirmações distintas do livro-caixa
re-julgadas no Sonnet 5 (US$ 0,0096/consulta contra 0,0188 do Opus — metade).
Concordância 23/25. Das duas divergências, uma favorece o Sonnet
(consistência entre grafias da mesma afirmação, onde o Opus flip-flopou) e a
que DECIDE favorece o Opus: no "pump iniciado em 19 de agosto", o Sonnet
confirmou citando a alta do mês — mas a afirmação específica é a DATA de
início, que a evidência não estabelece. Confirmar além da evidência é o
falso positivo do princípio 5, no único ponto onde ele é fatal.

Decisão: fica no Opus. Amostra pequena (16 das 25 eram sem_evidencia, onde
concordar é fácil); a revisão definitiva é a Medição 2, cujo gabarito tem
vereditos balanceados. As linhas do experimento estão em `consultas` com
modelo=claude-sonnet-5."""


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
