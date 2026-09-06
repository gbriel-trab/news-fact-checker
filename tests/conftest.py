"""Amarras de toda a suíte: nenhum teste toca a rede.

Antes, a proibição vivia em três arquivos (test_radar, test_x_api,
test_x_auth) e o resto da suíte valia por omissão — e a omissão vazava:
18 testes de test_selecao.py faziam um HEAD em huggingface.co ao carregar
o modelo de embedding, porque o huggingface_hub 1.x ignora HF_HUB_OFFLINE
na consulta de metadados (só `local_files_only` a evita — ver
`src/indice.py`). "625 passed" dependia de internet.

Aqui, socket, requests e httpx são substituídos por função que levanta
AssertionError, em toda a suíte (autouse). AssertionError de propósito,
e não ConnectionError: o hub trata erro de conexão como "sem internet" e
cai para o cache, o que esconderia a tentativa — a suíte tem de FALHAR
quando algo tenta a rede, não tolerar. Teste que precisa de rede não
existe neste projeto; se um dia existir, ele desliga a fixture
explicitamente, e isso fica visível no diff.

HF_HUB_OFFLINE fica definida por precaução, para versões do hub que ainda
a honrem; não é ela que segura nada nesta versão.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

import socket  # noqa: E402

import pytest  # noqa: E402


def _proibido(*args, **kwargs):
    raise AssertionError("o teste tentou falar com a rede")


class _SocketProibido:
    def __init__(self, *args, **kwargs):
        raise AssertionError("o teste tentou abrir um socket")


@pytest.fixture(autouse=True)
def sem_rede(monkeypatch):
    """Barra a rede em TODOS os testes, pelas três portas que o projeto e
    suas dependências usam: o socket cru, `requests` e `httpx`."""
    import requests

    monkeypatch.setattr(socket, "socket", _SocketProibido)
    monkeypatch.setattr(socket, "create_connection", _proibido)
    monkeypatch.setattr(requests, "get", _proibido)
    monkeypatch.setattr(requests, "post", _proibido)
    monkeypatch.setattr(requests.Session, "request", _proibido)
    try:
        import httpx
    except ImportError:
        return
    monkeypatch.setattr(httpx.Client, "send", _proibido)
