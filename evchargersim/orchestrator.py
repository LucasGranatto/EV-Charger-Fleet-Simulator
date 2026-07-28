"""
evchargersim.orchestrator — ciclo de vida de conexão/reconexão de UM
charge point (run_charger_lifecycle), os banners impressos ao ligar
(modo único e modo frota), o loop de chaos de desconexão, e main(): o
dispatcher que decide entre modo único (um EVChargerSim + console de
texto) e modo frota (N instâncias em paralelo + painel web de controle).
"""

import asyncio
import logging
import random
import sys
from dataclasses import replace

import websockets
import websockets.exceptions

from .charger import EVChargerSim
from .config import SimConfig
from .control_panel import start_control_server
from .logging_utils import build_logger

def _print_console_banner(config: SimConfig):
    """Painel de orientação do modo --console (legado: 1 charger, texto), impresso uma vez ao ligar."""
    bar = "═" * 70
    lines = [
        bar,
        "  EVChargerSim — simulador de Charge Point OCPP 1.6J (--console)",
        bar,
        f"  Charge Point ID   : {config.charge_point_id}",
        f"  CSMS              : {config.url}/{config.charge_point_id}",
        f"  Conector          : {config.connector_id}",
        f"  Bateria simulada  : {config.battery_capacity_wh / 1000:.1f} kWh"
        f" | SoC inicial: {config.initial_soc_percent:.0f}%",
        f"  Heartbeat         : {config.heartbeat_interval}s"
        f" | MeterValues: {config.meter_values_interval}s"
        f" | Corrente padrão: {config.default_offered_amps:.0f}A",
        bar,
    ]
    if config.chaos_disconnect_interval_seconds > 0 or config.chaos_drop_rate > 0 or config.chaos_latency_max_ms > 0:
        lines.insert(len(lines) - 1,
            f"  ⚠ CHAOS ativo     : desconexão a cada ~{config.chaos_disconnect_interval_seconds:.0f}s"
            if config.chaos_disconnect_interval_seconds > 0 else "  ⚠ CHAOS ativo     :"
        )
        if config.chaos_latency_max_ms > 0:
            lines.insert(len(lines) - 1,
                f"                      latência {config.chaos_latency_min_ms:.0f}"
                f"–{config.chaos_latency_max_ms:.0f}ms")
        if config.chaos_drop_rate > 0:
            lines.insert(len(lines) - 1,
                f"                      perda de mensagens {config.chaos_drop_rate * 100:.0f}%")
    if sys.stdout.isatty():
        cyan, reset = "\033[36m", "\033[0m"
        lines = [f"{cyan}{line}{reset}" for line in lines]
    print("\n".join(lines))


async def _chaos_disconnect_loop(cp: "EVChargerSim", config: SimConfig, logger: logging.Logger):
    """
    Se configurado, derruba o WebSocket em intervalos (± jitter) — pra
    testar reconexão/fila offline sem derrubar o servidor manualmente.
    Roda para sempre; cada ciclo espera de novo antes da próxima queda.
    """
    if config.chaos_disconnect_interval_seconds <= 0:
        return
    while True:
        jitter = random.uniform(
            -config.chaos_disconnect_jitter_seconds, config.chaos_disconnect_jitter_seconds
        )
        wait = max(1.0, config.chaos_disconnect_interval_seconds + jitter)
        await asyncio.sleep(wait)
        if cp.is_online and cp._connection is not None:
            logger.warning("[CHAOS] derrubando conexão de propósito (chaos_disconnect_interval)...")
            try:
                await cp._connection.close()
            except Exception:
                pass  # cp.start()/main() vão detectar a queda e reconectar normalmente



async def run_charger_lifecycle(
    config: SimConfig,
    logger: logging.Logger,
    registry: dict | None = None,
    enable_console: bool = True,
):
    """
    Loop de reconexão com backoff exponencial (2s -> 4s -> 8s ... até
    30s) para UM charge point (config.charge_point_id). A instância de
    EVChargerSim é criada UMA VEZ, na primeira conexão bem-sucedida, e
    persiste através de todas as reconexões — só a conexão WebSocket é
    trocada (`cp._connection = ws`), o que permite a uma sessão em
    andamento (SoC, energia, fila offline) sobreviver a uma queda de
    rede. Pelo mesmo motivo, os loops de fundo (heartbeat, meter values,
    acumulador, console, chaos) também são iniciados uma vez e rodam
    pra sempre.

    Extraído do antigo main() pra poder ser instanciado várias vezes em
    paralelo (uma task por charger) em modo frota (--fleet). Nesse
    modo:
      - `registry` é o dict compartilhado {charge_point_id: EVChargerSim}
        que o painel de controle web lê/escreve para despachar comandos.
      - `enable_console=False` desliga o console de texto (input()) —
        com N chargers no mesmo processo não dá pra ter N leitores de
        stdin brigando pela mesma entrada; quem manda comandos nesse
        modo é o painel web (ver start_control_server).

    cp.start() (o listener desta conexão específica) é lançado como
    task ANTES de esperar o boot/reconexão, não depois — precisa estar
    rodando pra sequer entregar a resposta do próprio BootNotification
    (ver comentário no laço abaixo).
    """
    backoff = 2
    max_backoff = 30
    cp: EVChargerSim | None = None
    # Loops de fundo que este charger sobe sozinho na 1ª conexão
    # (heartbeat/meter/acumulador/console/chaos) — rastreados aqui pra
    # serem cancelados no finally de baixo. Sem isso, cancelar esta task
    # (ex: via "remover charger" no dashboard) só para o laço de
    # reconexão em si; os loops de fundo ficavam orfãos, rodando pra
    # sempre contra uma conexão já fechada.
    background_tasks: list = []

    try:
        while True:
            url = f"{config.url}/{config.charge_point_id}"
            logger.info(f"Conectando em {url} ...")
            listener_task = None
            try:
                async with websockets.connect(url, subprotocols=["ocpp1.6"]) as ws:
                    logger.info("🔌 Conectado ao CSMS")

                    first_connection = cp is None
                    if first_connection:
                        cp = EVChargerSim(config.charge_point_id, ws, config, logger)
                        cp.is_online = True
                        if registry is not None:
                            registry[config.charge_point_id] = cp
                        background_tasks.append(asyncio.create_task(cp.send_heartbeat_loop()))
                        background_tasks.append(asyncio.create_task(
                            cp.send_meter_values_loop(interval_seconds=config.meter_values_interval)
                        ))
                        background_tasks.append(asyncio.create_task(
                            cp.energy_accumulator_loop(interval_seconds=config.meter_values_interval)
                        ))
                        if enable_console:
                            background_tasks.append(asyncio.create_task(cp.console_command_loop()))
                        background_tasks.append(asyncio.create_task(
                            _chaos_disconnect_loop(cp, config, logger)
                        ))
                    else:
                        cp._connection = ws
                        cp.is_online = True

                    # cp.start() PRECISA rodar em paralelo com o boot/reconexão,
                    # nunca depois — é o listener que entrega toda CALLRESULT
                    # recebida (inclusive a resposta do próprio
                    # BootNotification) pra quem está esperando via
                    # self.call(). Chamar run_first_boot_sequence/
                    # run_reconnect_sequence ANTES de start() estar rodando é
                    # um deadlock: _boot_until_accepted não retorna sem uma
                    # resposta, e a resposta nunca chega sem alguém lendo o
                    # socket — trava pra sempre, e por tabela NADA MAIS
                    # (heartbeat, meter values, Authorize, o que for) recebe
                    # resposta nenhuma daí em diante, já que é o mesmo listener
                    # que entrega tudo. (Isso passou despercebido antes porque
                    # o boot original tentava só uma vez e seguia em frente
                    # mesmo sem resposta; virou travamento permanente quando
                    # o retry-até-Accepted foi adicionado.)
                    listener_task = asyncio.create_task(cp.start())

                    if first_connection:
                        await cp.run_first_boot_sequence()
                    else:
                        await cp.run_reconnect_sequence()

                    backoff = 2
                    # Rotinas de fundo (heartbeat/meter/acumulador/console/chaos)
                    # já rodam à parte desde a primeira conexão; só falta
                    # esperar o listener desta conexão específica encerrar.
                    await listener_task

                logger.warning("Conexão encerrada pelo CSMS — tentando reconectar...")
            except (OSError, asyncio.TimeoutError,
                    websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.InvalidHandshake) as e:
                logger.warning(
                    f"Não foi possível conectar/manter conexão com o CSMS "
                    f"({e!r}) — nova tentativa em {backoff}s"
                )
            except Exception:
                logger.exception(
                    f"Erro inesperado na sessão com o CSMS — nova tentativa em {backoff}s"
                )
            finally:
                if cp is not None:
                    cp.is_online = False
                # Cobre saídas por exceção do boot/reconexão (não do próprio
                # listener) — sem isso, cp.start() ficaria rodando sozinho,
                # órfão, em cima de uma conexão que main() já desistiu.
                if listener_task is not None and not listener_task.done():
                    listener_task.cancel()
                    try:
                        await listener_task
                    except (asyncio.CancelledError, Exception):
                        pass

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
    finally:
        # Só executa quando este charger sai de cena PRA SEMPRE — task
        # cancelada (remove() do dashboard) ou uma exceção não prevista
        # escapando do while acima. Encerra os loops de fundo listados
        # em background_tasks; ver comentário na declaração da lista.
        for t in background_tasks:
            if not t.done():
                t.cancel()
        for t in background_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if cp is not None:
            cp.is_online = False



def _print_dashboard_banner(config: SimConfig, port: int, preloaded_ids: list):
    """Painel de orientação do modo padrão (dashboard), impresso uma vez ao ligar."""
    bar = "═" * 70
    lines = [
        bar,
        "  EVChargerSim — painel de controle",
        bar,
        f"  Painel de controle: http://localhost:{port}",
        f"  CSMS              : {config.url}/<charge_point_id>",
        f"  Bateria simulada  : {config.battery_capacity_wh / 1000:.1f} kWh"
        f" | SoC inicial: {config.initial_soc_percent:.0f}%",
    ]
    if preloaded_ids:
        lines.append(f"  Chargers pré-carregados: {', '.join(preloaded_ids)}")
    else:
        lines.append("  Nenhum charger pré-carregado — adicione pelo painel web acima.")
    lines.append(bar)
    lines.append("  Console de texto DESABILITADO neste modo — use o painel web acima")
    lines.append("  pra adicionar/remover chargers e controlar cada um deles.")
    lines.append(bar)
    if sys.stdout.isatty():
        cyan, reset = "\033[36m", "\033[0m"
        lines = [f"{cyan}{line}{reset}" for line in lines]
    print("\n".join(lines))


async def main(argv=None):
    config = SimConfig.load(argv)

    if config.console:
        # Modo legado: 1 charger, console de texto, sem painel web —
        # ver --console no help da CLI.
        logger = build_logger(config.charge_point_id, config.verbose)
        _print_console_banner(config)
        await run_charger_lifecycle(config, logger, registry=None, enable_console=True)
        return

    # Modo padrão: o painel web sobe sempre, mesmo sem nenhum charger
    # ainda — adicionar/remover chargers é feito dali (POST/DELETE
    # /api/chargers), a qualquer momento durante a execução, e não só
    # na hora de ligar. --fleet continua funcionando, só que agora como
    # conveniência pra pré-carregar alguns chargers de largada; o painel
    # segue disponível pra adicionar mais ou remover depois.
    loop = asyncio.get_running_loop()
    registry: dict = {}
    tasks: dict = {}
    dash_logger = build_logger("PAINEL", config.verbose)
    # Template usado pra qualquer charger futuro (pré-carregado agora ou
    # adicionado depois pelo painel) — charge_point_id/fleet_ids não
    # fazem sentido nele sozinho, cada charger recebe o seu via replace().
    base_config = replace(config, charge_point_id="", fleet_ids=())

    async def spawn(charge_point_id: str) -> str:
        charge_point_id = (charge_point_id or "").strip()
        if not charge_point_id:
            raise ValueError("charge_point_id vazio")
        if charge_point_id in tasks:
            raise ValueError(f"charger '{charge_point_id}' já existe")
        charger_config = replace(base_config, charge_point_id=charge_point_id)
        charger_logger = build_logger(charge_point_id, config.verbose)
        task = asyncio.create_task(
            run_charger_lifecycle(
                charger_config, charger_logger, registry=registry, enable_console=False
            )
        )
        tasks[charge_point_id] = task
        return f"charger '{charge_point_id}' adicionado"

    async def remove(charge_point_id: str) -> str:
        task = tasks.pop(charge_point_id, None)
        if task is None:
            raise ValueError(f"charger '{charge_point_id}' não encontrado")
        # Tira do registry ANTES de cancelar — o painel não deve mais
        # conseguir mandar comando pra um charger que já está saindo.
        registry.pop(charge_point_id, None)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return f"charger '{charge_point_id}' removido"

    async def broadcast_command(cmd: str, args: list) -> dict:
        """
        Executa `cmd` em TODOS os chargers atualmente no registry, em
        paralelo — usado pelas ações "todos" do painel (conectar/
        desconectar/pausar/retomar todos de uma vez). Uma exceção
        isolada em um charger vira só uma mensagem de erro na entrada
        dele do dict de retorno, sem derrubar os demais.

        Retorna {charge_point_id: mensagem}. Dict vazio se não há
        nenhum charger registrado ainda (registry vazio).
        """
        if not registry:
            return {}

        async def _run_one(charge_point_id: str, cp) -> tuple[str, str]:
            try:
                return charge_point_id, await cp.execute_command(cmd, args)
            except Exception as exc:
                return charge_point_id, f"erro: {exc!r}"

        # snapshot de .items() — evita RuntimeError se algum charger for
        # removido do registry por outra requisição enquanto isso roda
        pairs = list(registry.items())
        results = await asyncio.gather(*(_run_one(cid, cp) for cid, cp in pairs))
        return dict(results)

    start_control_server(
        registry, config.control_port, loop, dash_logger,
        spawn=spawn, remove=remove, broadcast=broadcast_command,
    )

    preloaded_ids = list(config.fleet_ids)
    _print_dashboard_banner(config, config.control_port, preloaded_ids)
    for charge_point_id in preloaded_ids:
        await spawn(charge_point_id)

    # Roda pra sempre — os chargers de fato vêm e vão via spawn()/remove()
    # disparados pelo painel web, não por um conjunto fixo de tasks
    # decidido na largada (ao contrário do antigo modo --fleet sozinho).
    await asyncio.Event().wait()
