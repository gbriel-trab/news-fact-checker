"""Lançador do boletim diário para o Agendador de Tarefas do Windows.

Mesmo desenho do ``coleta_agendada.pyw``, e pelos mesmos dois motivos:
fixa o caminho do projeto (tarefa nasce em System32) e roda via
``pythonw.exe`` — sem janela — mandando a saída para ``data/boletim.log``.
O boletim em si continua gravando o texto do dia em ``data/boletins/`` e
enviando pelo Telegram quando configurado; o log daqui é o diário de
bordo da EXECUÇÃO (rodou, custou, falhou), não o produto.

A tarefa aponta para cá:

    schtasks /Create /F /SC DAILY /TN "checador-boletim" /ST 08:00
        /TR "\"<projeto>\venv\Scripts\pythonw.exe\" \"<projeto>\boletim_agendado.pyw\""
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

LOG = RAIZ / "data" / "boletim.log"
LOG.parent.mkdir(exist_ok=True)

with open(LOG, "a", encoding="utf-8") as saida:
    sys.stdout = saida
    sys.stderr = saida
    print(f"\n===== {datetime.now(timezone.utc).isoformat()} =====")
    codigo = 0
    try:
        from src.boletim import main
        main()
    except SystemExit as erro:
        # SystemExit legítimo do boletim (sem handles, acervo vazio):
        # a mensagem vai para o log em vez de evaporar sem console.
        if erro.code not in (0, None):
            print(f"abortado: {erro.code}" if isinstance(erro.code, str)
                  else f"abortado: exit {erro.code}")
            codigo = 1
    print(f"===== fim · exit {codigo} =====")

sys.exit(codigo)
