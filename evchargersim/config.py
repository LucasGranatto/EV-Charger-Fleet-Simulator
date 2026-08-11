"""
evchargersim.config — configuração de uma instância (SimConfig) e parsing
de argumentos de CLI (_parse_args).

Precedência final de valores: CLI > --config (arquivo JSON) > defaults
definidos abaixo em SimConfig. Ver SimConfig.load().
"""

import argparse
import json
from dataclasses import dataclass

from ocpp.v16.enums import ChargePointErrorCode

FAULT_CODE_MAP = {
    "ground_failure":         ChargePointErrorCode.ground_failure,
    "over_current_failure":   ChargePointErrorCode.over_current_failure,
    "over_voltage":           ChargePointErrorCode.over_voltage,
    "connector_lock_failure": ChargePointErrorCode.connector_lock_failure,
    "power_meter_failure":    ChargePointErrorCode.power_meter_failure,
    "weak_signal":            ChargePointErrorCode.weak_signal,
    "other_error":            ChargePointErrorCode.other_error,
}

# Campos de SimConfig que POST /api/chargers pode sobrescrever POR
# CHARGER individual no modo frota (ex: {"charge_point_id": "CH01",
# "battery_capacity_wh": 30000}) — whitelist explícita, tanto por
# segurança (o payload vem de uma requisição HTTP) quanto por sentido:
# campos como url/control_port/console/fleet_ids não fazem sentido por
# charger. Ver orchestrator.main().spawn().
CHARGER_OVERRIDE_FIELDS = frozenset({
    "connector_id",
    "meter_values_interval",
    "heartbeat_interval",
    "default_offered_amps",
    "simulation_speed",
    "battery_capacity_wh",
    "initial_soc_percent",
    "nominal_voltage",
    "number_of_phases",
    "hardware_max_amps",
    "max_schedule_periods",
    "max_tx_profiles",
    "call_timeout_seconds",
    "chaos_disconnect_interval_seconds",
    "chaos_disconnect_jitter_seconds",
    "chaos_latency_min_ms",
    "chaos_latency_max_ms",
    "chaos_drop_rate",
    "max_offline_queue_size",
})


@dataclass
class SimConfig:
    """
    Configuração de uma instância — fixa após o boot (ao contrário de
    ChargerState, que muda a cada mensagem). Precedência: CLI > --config
    (JSON) > defaults abaixo.
    """
    charge_point_id: str = "EVCHARGERSIM_01"
    url: str = "ws://localhost:9001"
    verbose: bool = False
    connector_id: int = 1

    meter_values_interval: int = 30
    heartbeat_interval: int = 120

    default_offered_amps: float = 16.0
    simulation_speed: float = 1.0

    battery_capacity_wh: float = 50_000.0
    initial_soc_percent: float = 20.0

    nominal_voltage: float = 225.0
    # Todo cálculo de potência do simulador (energy_accumulator_loop,
    # MeterValues, GetCompositeSchedule em W, conversão W→A recebida do
    # CSMS) era estritamente monofásico: P = nominal_voltage × amps,
    # sem multiplicar por fase nenhuma. Isso é uma simplificação válida
    # só pra ligação monofásica de fato — pra trifásico (comum em CSMS
    # OCPP reais; ex: 32A trifásico ≈ 22kW, o "AC rápido" típico
    # europeu), nominal_voltage é tratado como tensão fase-neutro e a
    # potência real é number_of_phases × nominal_voltage × amps. Com
    # number_of_phases=1 (padrão) o comportamento não muda em nada —
    # só passa a valer quando o charger simulado é configurado como
    # trifásico. Sem isso, um CSMS que eleva a corrente pensando num
    # charger trifásico via SetChargingProfile via a sessão continuar
    # "lenta" mesmo após o limite subir, porque o simulador só
    # computava 1/3 (ou 1/número de fases real) da potência esperada.
    number_of_phases: int = 1

    # ── Limites físicos do hardware — sem isso, o simulador deixava
    # aplicar qualquer corrente que o CSMS pedisse via SetChargingProfile,
    # coisa que nenhum charger real faz (a fiação/breaker/contator tem um
    # teto físico, e isso normalmente é anunciado via GetConfiguration
    # pra o CSMS não precisar adivinhar). hardware_max_amps é esse teto
    # — reportado como a chave "CurrentMax" (ver on_get_configuration) e
    # de fato APLICADO como um clamp em qualquer corrente oferecida (ver
    # _apply_offered_amps), não só anunciado. max_schedule_periods é o
    # equivalente pro lado "memória limitada de firmware": quantos
    # períodos de um chargingSchedule este charger de fato guarda —
    # reportado como "ChargingScheduleMaxPeriods" e truncado de verdade
    # em on_set_charging_profile se o CSMS mandar mais que isso.
    hardware_max_amps: float = 32.0
    max_schedule_periods: int = 10

    # Quantos perfis TxProfile este charger aceita ter instalados AO
    # MESMO TEMPO, um por stack_level distinto — reportado nas chaves
    # "ChargeProfileMaxStackLevel"/"MaxChargingProfilesInstalled" (ver
    # on_get_configuration) e de fato aplicado em on_set_charging_profile:
    # um novo stack_level além deste teto é Rejected, não aceito sem
    # limite. TxProfile (só esse purpose empilha de verdade — ver
    # _recompute_tx_profile_effective_amps) é escopado à transação: o
    # de MAIOR stack_level em efeito a cada instante vence, igual à
    # semântica real da spec (ChargePointMaxProfile/TxDefaultProfile
    # continuam sem stacking, 1 perfil de cada vez, como antes).
    max_tx_profiles: int = 3

    # Timeout para chamadas críticas (Start/StopTransaction) — sem isso,
    # um CSMS que trava sem responder deixa o simulador pendurado pra
    # sempre. Ver _send_start_transaction / _send_stop_transaction.
    call_timeout_seconds: float = 30.0

    # ── Instabilidade de rede injetável (chaos) — tudo opt-in, 0/desligado
    # por padrão. Ver README para exemplos de uso.
    chaos_disconnect_interval_seconds: float = 0.0  # 0 = desabilitado
    chaos_disconnect_jitter_seconds: float = 5.0
    chaos_latency_min_ms: float = 0.0
    chaos_latency_max_ms: float = 0.0
    chaos_drop_rate: float = 0.0  # 0.0-1.0

    # Teto da fila offline (offline_queue) de CADA charger — sem isso,
    # um charger desconectado por muito tempo (CSMS caído, chaos-
    # disconnect prolongado) acumula toda mensagem "queueable"
    # indefinidamente, e a reconexão despeja tudo de uma vez no CSMS.
    # 0 desabilita o teto (comportamento antigo, sem limite — use com
    # cautela). Ver EVChargerSim._enqueue_offline().
    max_offline_queue_size: int = 500

    # ── Modo frota (multi-charger) — ver --fleet no help da CLI. Quando
    # fleet_ids não é vazio, main() ignora charge_point_id/connector_id
    # únicos acima e sobe uma instância de EVChargerSim por ID da lista,
    # todas compartilhando os demais campos deste SimConfig (url,
    # intervalos, chaos, etc.) via dataclasses.replace(). O console de
    # texto (input()) é desabilitado nesse modo — quem controla os
    # chargers é o painel web em control_port.
    fleet_ids: tuple = ()
    control_port: int = 8080

    # Modo legado: 1 charger, console de texto (input()), SEM painel
    # web. Ver --console no help da CLI. Por padrão (False), rodar o
    # programa sempre sobe o painel de controle web, mesmo sem nenhum
    # charger pré-carregado — chargers são adicionados/removidos dali.
    console: bool = False

    # Arquivo JSON onde a LISTA de charger_id da frota (não o estado de
    # sessão de cada um — SoC/energia/fila offline continuam efêmeros,
    # de propósito, ver comentário em orchestrator._save_fleet_state)
    # é salva a cada spawn/remove e recarregada no próximo boot — sem
    # isso, reiniciar o processo (deploy, crash, etc.) esquece quais
    # chargers estavam rodando e você precisa readicionar um por um.
    # None (padrão) desliga a persistência inteiramente.
    persist_file: str | None = None

    # Se definido, todo request a /api/* do painel exige esse token —
    # via header "Authorization: Bearer <token>" (POST/DELETE) ou
    # querystring "?token=<token>" (GET, inclusive /api/events, que o
    # EventSource do browser não consegue mandar com header custom).
    # None (padrão) desliga a autenticação — mesmo comportamento de
    # antes, pra não quebrar quem já usa isso numa rede confiável.
    control_token: str | None = None

    @classmethod
    def load(cls, argv=None) -> "SimConfig":
        """Monta a config final combinando defaults, --config e flags de CLI."""
        args = _parse_args(argv)
        cfg = cls()

        if args.config:
            try:
                with open(args.config, "r", encoding="utf-8") as fh:
                    overrides = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                raise SystemExit(
                    f"Não foi possível ler --config '{args.config}': {exc}"
                )
            unknown = set(overrides) - {f for f in cfg.__dataclass_fields__}
            if unknown:
                raise SystemExit(
                    f"Chave(s) desconhecida(s) em '{args.config}': "
                    f"{', '.join(sorted(unknown))}. Chaves válidas: "
                    f"{', '.join(sorted(cfg.__dataclass_fields__))}"
                )
            for key, value in overrides.items():
                setattr(cfg, key, value)

        # CLI só sobrescreve o que foi de fato passado (senão o default
        # do argparse sempre pisaria no valor vindo do --config).
        cli_overrides = {
            "charge_point_id": args.charge_point_id,
            "url": args.url,
            "connector_id": args.connector_id,
            "meter_values_interval": args.meter_interval,
            "heartbeat_interval": args.heartbeat_interval,
            "default_offered_amps": args.default_amps,
            "simulation_speed": args.sim_speed,
            "battery_capacity_wh": args.battery_wh,
            "initial_soc_percent": args.initial_soc,
            "nominal_voltage": args.voltage,
            "number_of_phases": args.phases,
            "hardware_max_amps": args.hardware_max_amps,
            "max_schedule_periods": args.max_schedule_periods,
            "max_tx_profiles": args.max_tx_profiles,
            "call_timeout_seconds": args.call_timeout,
            "chaos_disconnect_interval_seconds": args.chaos_disconnect_interval,
            "chaos_disconnect_jitter_seconds": args.chaos_disconnect_jitter,
            "chaos_latency_min_ms": args.chaos_latency_min,
            "chaos_latency_max_ms": args.chaos_latency_max,
            "chaos_drop_rate": args.chaos_drop_rate,
            "max_offline_queue_size": args.max_offline_queue,
            "control_port": args.control_port,
            "persist_file": args.persist_file,
            "control_token": args.control_token,
        }
        for key, value in cli_overrides.items():
            if value is not None:
                setattr(cfg, key, value)
        if args.verbose:
            cfg.verbose = True
        if args.console:
            cfg.console = True
        if args.fleet:
            # IDs explícitos, na ordem digitada; espaços em volta de cada
            # vírgula são tolerados ("CH01, CH02,CH03") e IDs vazios
            # (vírgula duplicada/sobrando) são descartados.
            cfg.fleet_ids = tuple(
                cid.strip() for cid in args.fleet.split(",") if cid.strip()
            )

        return cfg


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="EVChargerSim — simulador standalone de Charge Point OCPP 1.6J.")
    parser.add_argument("charge_point_id", nargs="?", default=None,
                         help="ID do charge point (padrão: EVCHARGERSIM_01).")
    parser.add_argument("--url", default=None,
                         help="URL base do CSMS, sem o ID (padrão: ws://localhost:9001).")
    parser.add_argument("--config", default=None,
                         help="Arquivo JSON com valores padrão (ver SimConfig). CLI tem prioridade.")
    parser.add_argument("--connector-id", type=int, default=None)
    parser.add_argument("--meter-interval", type=int, default=None,
                         help="Intervalo de MeterValues em segundos (padrão: 30).")
    parser.add_argument("--heartbeat-interval", type=int, default=None,
                         help="Intervalo inicial de Heartbeat em segundos (padrão: 120).")
    parser.add_argument("--default-amps", type=float, default=None,
                         help="Corrente ao iniciar sessão, antes do 1º SetChargingProfile (padrão: 16.0).")
    parser.add_argument("--sim-speed", type=float, default=None,
                         help="Fator de aceleração da simulação (padrão: 1.0 = tempo real).")
    parser.add_argument("--battery-wh", type=float, default=None,
                         help="Capacidade da bateria simulada em Wh (padrão: 50000).")
    parser.add_argument("--initial-soc", type=float, default=None,
                         help="SoC inicial de cada sessão, em %% (padrão: 20.0).")
    parser.add_argument("--voltage", type=float, default=None,
                         help="Tensão nominal de referência em V, fase-neutro (padrão: 225.0).")
    parser.add_argument("--phases", type=int, default=None, choices=[1, 2, 3],
                         help="Número de fases do charger simulado — afeta todo cálculo de "
                              "potência (padrão: 1 = monofásico; use 3 para trifásico).")
    parser.add_argument("--hardware-max-amps", type=float, default=None,
                         help="Teto físico de corrente deste charger (fiação/breaker) — "
                              "anunciado via GetConfiguration (chave CurrentMax) e "
                              "de fato aplicado a qualquer corrente oferecida, mesmo "
                              "que o CSMS peça mais via SetChargingProfile (padrão: 32.0).")
    parser.add_argument("--max-schedule-periods", type=int, default=None,
                         help="Quantos períodos de um chargingSchedule este charger "
                              "de fato guarda (memória limitada de firmware) — "
                              "anunciado via GetConfiguration (chave "
                              "ChargingScheduleMaxPeriods) e usado pra truncar perfis "
                              "maiores recebidos via SetChargingProfile (padrão: 10).")
    parser.add_argument("--max-tx-profiles", type=int, default=None,
                         help="Quantos perfis TxProfile este charger aceita ter "
                              "instalados ao mesmo tempo, um por stack_level distinto "
                              "— anunciado via GetConfiguration (chaves "
                              "ChargeProfileMaxStackLevel/MaxChargingProfilesInstalled) "
                              "e de fato aplicado: um SetChargingProfile TxProfile além "
                              "deste teto é Rejected (padrão: 3).")
    parser.add_argument("--call-timeout", type=float, default=None,
                         help="Timeout (s) para Start/StopTransaction (padrão: 30.0).")
    parser.add_argument("--chaos-disconnect-interval", type=float, default=None,
                         help="Derruba o WebSocket a cada N segundos ± jitter (padrão: desabilitado).")
    parser.add_argument("--chaos-disconnect-jitter", type=float, default=None,
                         help="Variação (± segundos) em torno do intervalo acima (padrão: 5.0).")
    parser.add_argument("--chaos-latency-min", type=float, default=None,
                         help="Atraso mínimo artificial (ms) por mensagem (padrão: 0).")
    parser.add_argument("--chaos-latency-max", type=float, default=None,
                         help="Atraso máximo artificial (ms) por mensagem (padrão: 0).")
    parser.add_argument("--chaos-drop-rate", type=float, default=None,
                         help="Probabilidade (0.0–1.0) de perda simulada de mensagem (padrão: 0.0).")
    parser.add_argument("--max-offline-queue", type=int, default=None,
                         help="Teto da fila offline de cada charger — ao exceder, a mensagem "
                              "mais antiga é descartada para abrir espaço (padrão: 500). "
                              "0 desabilita o teto (sem limite — use com cautela).")
    parser.add_argument("--verbose", action="store_true",
                         help="Mostra Heartbeat/GetConfiguration no terminal (padrão: silenciosos).")
    parser.add_argument("--console", action="store_true",
                         help="Modo legado: 1 charger (charge_point_id acima), console de texto, "
                              "SEM painel web. Por padrão (sem esta flag) o programa sobe direto "
                              "o painel de controle web e você adiciona/remove chargers por lá.")
    parser.add_argument("--fleet", default=None,
                         help="Lista de charge_point_id separados por vírgula (ex: CH01,CH02,CH03) "
                              "pra já subir pré-carregados no painel web — opcional, você também "
                              "pode adicionar chargers pelo próprio painel depois de ligar.")
    parser.add_argument("--control-port", type=int, default=None,
                         help="Porta HTTP do painel de controle web (padrão: 8080).")
    parser.add_argument("--persist-file", default=None,
                         help="Arquivo JSON pra lembrar quais chargers da frota estavam rodando "
                              "entre reinícios do processo (padrão: desabilitado — frota some ao "
                              "reiniciar). Só a LISTA de IDs é salva, não SoC/energia/fila offline "
                              "de cada um — cada charger volta com sessão zerada, como um charger "
                              "de verdade que perdeu energia.")
    parser.add_argument("--control-token", default=None,
                         help="Se definido, exige esse token em todo request a /api/* do painel "
                              "(padrão: desabilitado, sem autenticação).")
    return parser.parse_args(argv)
