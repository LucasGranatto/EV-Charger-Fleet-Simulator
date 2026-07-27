"""
evchargersim.physics — funções puras (sem estado global/de instância) de
simulação física: tensão de rede, corrente real do "carro" dado o limite
oferecido, e a cor de log da linha de MeterValues.

Propositalmente sem dependência de ChargerState/EVChargerSim — fácil de
testar isoladamente (ver test_evchargersim.py).
"""

import random

def read_grid_voltage(nominal_voltage: float) -> float:
    """Simula pequena flutuação natural da tensão de rede (~±1.5V)."""
    return round(nominal_voltage + random.uniform(-1.5, 1.5), 1)




def compute_actual_current(offered_amps: float, soc_percent: float) -> float:
    """
    Calcula a corrente real que o "carro" puxaria dado o limite oferecido
    pelo CSMS e o estado de carga atual da bateria (SoC).

    Carregamento AC (diferente de DC rápido) tende a respeitar bem o
    limite oferecido na maior parte da curva — a redução por tapering só
    fica perceptível perto do fim (SoC alto), quando o carregador de
    bordo do veículo reduz a corrente para proteger a bateria.

    Função pura (sem estado global/de instância) de propósito — fácil de
    testar isoladamente com unittest, sem precisar montar um EVChargerSim
    inteiro. Ver test_evchargersim.py.
    """
    if offered_amps <= 0:
        return 0.0
    if soc_percent < 80:
        factor = 0.97  # praticamente o limite oferecido inteiro
    elif soc_percent < 90:
        factor = 0.75
    elif soc_percent < 97:
        factor = 0.45
    else:
        factor = 0.15  # últimos % da bateria, corrente bem reduzida
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

