"""Camada de provedor de LLM.

Duas implementações atrás da mesma função, escolhidas por variável de ambiente:

* **anthropic** — o provedor do projeto. Structured output validado pela API.
* **ollama** — modelo local, custo zero por chamada. Serve para iterar prompt
  sem acumular fatura e para a comparação de qualidade documentada.

A existência do caminho local não o torna o padrão. O entregável do projeto é
um programa que consome API de LLM, e o modelo local é ferramenta de
desenvolvimento e objeto de comparação — nunca substituto do que precisa ser
demonstrado.

Aviso que vale para qualquer conclusão tirada daqui: resultado ruim no modelo
local é evidência sobre o modelo, não sobre o prompt. As partes sutis da
extração — distinguir EXTRACTED de INFERRED, fazer entidades canônicas
convergirem entre fontes — são justamente onde modelo pequeno falha primeiro.
Prompt afinado no local precisa ser revalidado na API antes de virar decisão.
"""

import json
import os

import requests
from pydantic import BaseModel

# Import por efeito colateral: config carrega o .env no ambiente. Sem ele,
# as variaveis abaixo e a chave da Anthropic ficariam invisiveis.
from . import config  # noqa: F401

MODELO_ANTHROPIC = "claude-opus-5"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODELO = os.getenv("OLLAMA_MODELO", "qwen3.5:9b")
"""Modelo local padrao, sobrescrito por OLLAMA_MODELO no .env.

Qwen 3.5 9B foi escolhido por ser multilingue e caber inteiro em 16 GB de
VRAM com folga, o que mantem a iteracao de prompt rapida. Se a qualidade
nao bastar nas partes de julgamento, mistral-small (13 GB) e o proximo
degrau. Confira a tag com `ollama list` antes de confiar neste padrao.
"""

TIMEOUT_LOCAL = 600
"""Modelo local em CPU pode demorar minutos por matéria. Em GPU, segundos."""

MAX_TOKENS_SAIDA = 8000
"""Teto de tokens gerados, igual nos dois provedores para que a comparação de
qualidade não esbarre em limites diferentes."""

NUM_CTX_LOCAL = 16384
"""Janela de contexto do modelo local, em tokens.

Precisa acomodar instruções, matéria, schema e resposta. Consome mais VRAM,
mas o custo de errar para baixo é uma falha que não se parece com falha.
"""


class FalhaNoModelo(Exception):
    """O provedor respondeu, mas não com o que o schema exige."""


def provedor() -> str:
    """Provedor ativo. Lido a cada chamada para permitir troca sem reiniciar."""
    return os.getenv("LLM_PROVIDER", "anthropic").strip().lower()


def _via_anthropic(system: str, user: str, esquema: type[BaseModel]) -> BaseModel:
    import anthropic

    cliente = anthropic.Anthropic()
    resposta = cliente.messages.parse(
        model=MODELO_ANTHROPIC,
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


def _via_ollama(system: str, user: str, esquema: type[BaseModel]) -> BaseModel:
    """Chama o Ollama com o schema como restrição de formato.

    O campo `format` recebe o JSON Schema e o servidor restringe a geração a
    ele — é o equivalente local do structured output, e não um pedido no prompt.
    """
    try:
        resposta = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODELO,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": esquema.model_json_schema(),
                "stream": False,
                # Raciocínio desligado. Modelo de raciocínio escreve num campo
                # `thinking` separado e só depois preenche `content` — e o
                # padrão é verborrágico a ponto de estourar a janela antes de
                # chegar à resposta. Medido: 4.056 tokens de raciocínio para
                # uma frase de dez palavras, terminando com `content` vazio.
                # Com `think: False`, a mesma extração sai em 42 tokens.
                "think": False,
                "options": {
                    # Determinismo: a mesma matéria precisa produzir as mesmas
                    # triplas em duas execuções, senão não há como testar nem
                    # comparar provedores.
                    "temperature": 0,
                    # O Ollama usa 4.096 de contexto por padrão, o que não cabe
                    # uma matéria mais o schema mais a resposta. Estourar aqui
                    # não dá erro: devolve `content` vazio e `done_reason:
                    # length`, que parece falha de formato e não é.
                    "num_ctx": NUM_CTX_LOCAL,
                    "num_predict": MAX_TOKENS_SAIDA,
                },
            },
            timeout=TIMEOUT_LOCAL,
        )
        resposta.raise_for_status()
    except requests.RequestException as erro:
        raise FalhaNoModelo(
            f"Ollama não respondeu em {OLLAMA_URL}: {type(erro).__name__}. "
            f"O servidor está rodando e o modelo {OLLAMA_MODELO} foi baixado?"
        ) from erro

    dados = resposta.json()
    bruto = dados.get("message", {}).get("content", "")

    if not bruto:
        # Resposta vazia quase sempre significa janela estourada, nao formato
        # invalido. Dizer isso na mensagem evita horas procurando bug no schema.
        raise FalhaNoModelo(
            f"{OLLAMA_MODELO} devolveu conteudo vazio "
            f"(done_reason={dados.get('done_reason')}, "
            f"{dados.get('eval_count')} tokens gerados). "
            f"Se done_reason for 'length', a janela de contexto "
            f"({NUM_CTX_LOCAL}) nao coube a materia mais a resposta."
        )

    try:
        return esquema.model_validate(json.loads(bruto))
    except (json.JSONDecodeError, ValueError) as erro:
        # Modelo local restringido por schema ainda erra, sobretudo em texto
        # longo. Falhar alto é melhor que gravar tripla malformada no acervo.
        raise FalhaNoModelo(
            f"{OLLAMA_MODELO} devolveu saída fora do schema: {erro}"
        ) from erro


def gera(system: str, user: str, esquema: type[BaseModel]) -> BaseModel:
    """Manda o par system/user ao provedor ativo e devolve o objeto validado."""
    atual = provedor()
    if atual == "ollama":
        return _via_ollama(system, user, esquema)
    if atual == "anthropic":
        return _via_anthropic(system, user, esquema)
    raise FalhaNoModelo(
        f"LLM_PROVIDER='{atual}' desconhecido. Use 'anthropic' ou 'ollama'."
    )


def descricao() -> str:
    """Identificação do provedor ativo, para relatório e para gravar junto das
    triplas — comparar extrações sem saber qual modelo as produziu é inútil."""
    atual = provedor()
    return f"ollama:{OLLAMA_MODELO}" if atual == "ollama" else f"anthropic:{MODELO_ANTHROPIC}"
