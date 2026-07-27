"""
EVChargerSim — simulador de Charge Point OCPP 1.6J (mobilityhouse/ocpp).

Simula o lado carro/carregador de um ponto AC genérico, conectando no
seu CSMS real via WebSocket, pra testar a lógica do servidor sem
hardware físico.

Uso — modo padrão (painel de controle web):
    python -m evchargersim --url ws://seu-csms:9001
    # abre em http://localhost:8080 SEM nenhum charger — adicione e
    # remova pelo próprio painel, a qualquer momento, com o processo
    # já rodando (não precisa saber de antemão quantos vai usar).

    python -m evchargersim --fleet CH01,CH02,CH03 --url ws://seu-csms:9001
    # mesma coisa, mas já sobe com esses 3 pré-carregados (opcional).

Uso — modo legado (1 charger, console de texto, sem painel web):
    python -m evchargersim --console
    python -m evchargersim --console CARREGADOR_02 --url ws://host:9000
    python -m evchargersim --console --config sim.json

Reconexão automática com backoff exponencial. Enquanto offline, a
sessão continua rodando fisicamente (SoC sobe, energia acumula) e
mensagens não entregues ficam numa fila local, reenviadas em ordem ao
reconectar — ver comando "queue" no console (--console) ou o campo
"fila offline" de cada card no painel web.

Instabilidade de rede injetável (--chaos-disconnect-interval,
--chaos-latency-min/max, --chaos-drop-rate) e o comando/botão
"disconnect" ajudam a testar a robustez do CSMS sem depender de uma
queda real.

Estrutura do pacote:
    config.py          SimConfig, FAULT_CODE_MAP, parsing de CLI
    state.py           ChargerState (estado de runtime de UM charger)
    physics.py         funções puras de simulação (tensão, corrente)
    logging_utils.py   logger colorido do projeto (1 logger por charger)
    charger.py         EVChargerSim — a classe do protocolo em si
    control_panel.py   servidor HTTP do painel de controle
    orchestrator.py    ciclo de vida de conexão + main()
    frontend/          HTML/CSS/JS do painel — arquivos estáticos, sem build step
"""

from .config import SimConfig
from .orchestrator import main

__all__ = ["SimConfig", "main"]
__version__ = "3.0.0"
