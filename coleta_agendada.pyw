"""Lançador da coleta para o Agendador de Tarefas do Windows.

Existe por dois motivos que o agendador impõe:

1. Tarefa agendada nasce com diretório de trabalho System32, onde
   ``python -m src.collect`` não encontra o pacote. Este arquivo fixa o
   caminho do projeto sozinho, e o /TR da tarefa dispensa o
   ``cmd /c cd ...`` — que abria uma janela de console a cada disparo.
2. Via ``pythonw.exe`` (Python sem console, sem janela), o relatório da
   coleta não teria para onde ir. Aqui ele vai para ``data/coleta.log``
   (o .gitignore já cobre ``*.log``), que vira o registro de saúde da
   coleta contínua: feed que falha fica gravado, não evapora.

A tarefa aponta para cá:

    schtasks /Create /F /SC MINUTE /MO 15 /TN "checador-coleta" /ST 00:00
        /TR "\"<projeto>\venv\Scripts\pythonw.exe\" \"<projeto>\coleta_agendada.pyw\""
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

LOG = RAIZ / "data" / "coleta.log"
LOG.parent.mkdir(exist_ok=True)

with open(LOG, "a", encoding="utf-8") as saida:
    sys.stdout = saida
    sys.stderr = saida
    print(f"\n===== {datetime.now(timezone.utc).isoformat()} =====")
    # Direto em coleta_tudo, sem collect.main(): o main reconfigura o
    # console (que aqui não existe) e o arquivo já nasce UTF-8.
    from src.collect import coleta_tudo
    codigo = coleta_tudo()
    print(f"===== fim · exit {codigo} =====")

sys.exit(codigo)
