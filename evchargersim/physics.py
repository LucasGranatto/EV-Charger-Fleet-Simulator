"""
evchargersim.physics — funções puras (sem estado global/de instância) de
simulação física: tensão de rede, corrente real do "carro" dado o limite
oferecido, e a cor de log da linha de MeterValues.

Propositalmente sem dependência de ChargerState/EVChargerSim — fácil de
testar isoladamente (ver test_evchargersim.py).
"""

import math
import random

def read_grid_voltage(nominal_voltage: float) -> float:
    """Simula pequena flutuação natural da tensão de rede (~±1.5V)."""
    return round(nominal_voltage + random.uniform(-1.5, 1.5), 1)


# Parâmetros da curva logística de tapering — ver compute_actual_current().
# Escolhidos pra aproximar os mesmos pontos de referência do tapering
# antigo em degraus (≈0.97 abaixo de 80%, ≈0.75 perto de 90%, ≈0.45
# perto de 93-95%, ≈0.15 acima de 97%), só que como uma curva contínua
# em vez de saltos instantâneos.
_TAPER_MAX_FACTOR = 0.97   # fator com a bateria "fria" (SoC baixo/médio)
_TAPER_MIN_FACTOR = 0.12   # fator no fim da carga (SoC ~100%)
_TAPER_MIDPOINT_SOC = 91.0  # SoC onde a curva está na metade do caminho
_TAPER_STEEPNESS = 0.28    # quão abrupta é a transição em torno do midpoint


def compute_actual_current(offered_amps: float, soc_percent: float) -> float:
    """
    Calcula a corrente real que o "carro" puxaria dado o limite oferecido
    pelo CSMS e o estado de carga atual da bateria (SoC).

    Carregamento AC (diferente de DC rápido) tende a respeitar bem o
    limite oferecido na maior parte da curva — a redução por tapering só
    fica perceptível perto do fim (SoC alto), quando o carregador de
    bordo do veículo reduz a corrente para proteger a bateria.

    O fator de tapering é uma curva logística contínua em função do SoC
    (não mais degraus fixos) — um carregador de bordo real reduz a
    corrente suavemente conforme a bateria se aproxima da carga plena,
    nunca em saltos instantâneos. Além de mais realista fisicamente,
    isso evita descontinuidades verticais no gráfico de histórico do
    painel (SoC/corrente ao longo do tempo).

    Função pura (sem estado global/de instância) de propósito — fácil de
    testar isoladamente com unittest, sem precisar montar um EVChargerSim
    inteiro. Ver test_evchargersim.py.
    """
    if offered_amps <= 0:
        return 0.0
    exponent = _TAPER_STEEPNESS * (soc_percent - _TAPER_MIDPOINT_SOC)
    factor = _TAPER_MIN_FACTOR + (_TAPER_MAX_FACTOR - _TAPER_MIN_FACTOR) / (1 + math.exp(exponent))
    return round(offered_amps * factor, 1)



def _meter_line_color(has_session: bool, suspended: bool, faulted: bool, use_color: bool) -> str:
    """
    Cor da linha de MeterValues conforme o estado atual do charger —
    verde carregando normalmente, amarelo suspenso (carro ou CSMS
    pausou), cinza sem sessão, vermelho em Faulted. Sem isso, a linha
    de status mais frequente do terminal saía sempre na mesma cor,
    então "está carregando de verdade ou só suspenso?" exigia ler o
    texto todo em vez de notar pela cor.
    """
    if not use_color:
        return ""
    if faulted:
        return "\033[31m"    # vermelho
    if not has_session:
        return "\033[2m"     # cinza (dim)
    if suspended:
        return "\033[33m"    # amarelo
    return "\033[32m"        # verde

