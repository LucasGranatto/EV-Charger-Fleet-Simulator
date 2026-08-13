"""
evchargersim.orchestrator — ciclo de vida de conexão/reconexão de UM
charge point (run_charger_lifecycle), os banners impressos ao ligar
(modo único e modo frota), o loop de chaos de desconexão, e main(): o
dispatcher que decide entre modo único (um EVChargerSim + console de
texto) e modo frota (N instâncias em paralelo + painel web de controle).

main() também cuida de: persistência da LISTA de chargers da frota
entre reinícios (--persist-file — ver _load_fleet_ids/_save_fleet_ids)
e encerramento gracioso em SIGINT/SIGTERM (ver __main__.py, que cancela
a task principal; o CancelledError sobe até aqui e o finally de main()
cancela e espera todos os chargers antes de derrubar o painel web).
"""

import asyncio
import json
import logging
import os
import random
import sys
from dataclasses import replace

import websockets
import websockets.exceptions
from ocpp.v16.enums import Reason

from .charger import EVChargerSim
from .config import CHARGER_OVERRIDE_FIELDS, SimConfig
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

    Roda para sempre, MESMO se começar com chaos desligado (interval
    <= 0) — faz um polling ocioso curto nesse caso, em vez de sair de
    cena. Sem isso, ligar o chaos depois pelo painel web (POST
    /api/chargers/<id>/chaos, ver EVChargerSim.apply_chaos_overrides)
    não teria efeito nenhum: essa task só é criada UMA VEZ por charger
    (na 1ª conexão, não a cada reconexão), então se ela retornasse de
    cara por não ter chaos configurado no boot, não sobraria nada
    rodando pra reagir a uma mudança posterior.

    Reconfere config.chaos_disconnect_interval_seconds tanto antes de
    dormir (pra saber quanto esperar) quanto depois de acordar (pra não
    derrubar a conexão à toa se o chaos foi desligado nesse meio-tempo).
    """
    idle_poll_seconds = 2.0
    while True:
        interval = config.chaos_disconnect_interval_seconds
        if interval <= 0:
            await asyncio.sleep(idle_poll_seconds)
            continue
        jitter = random.uniform(
            -config.chaos_disconnect_jitter_seconds, config.chaos_disconnect_jitter_seconds
        )
        wait = max(1.0, interval + jitter)
        await asyncio.sleep(wait)
        if config.chaos_disconnect_interval_seconds <= 0:
            continue  # desligado durante a espera — não derruba, volta a fazer polling ocioso
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



def _load_fleet_ids(path: "str | None", logger: logging.Logger) -> list:
    """
    Lê a lista de charge_point_id persistida em --persist-file — só a
    LISTA em si, nunca estado de sessão (SoC/energia/fila offline
    continuam efêmeros de propósito: cada charger volta "zerado", como
    um charger de verdade que perdeu energia — tentar reconstruir
    transaction_id/fila offline através de um restart completo do
    processo seria reconstruir estado que só o CSMS deveria arbitrar).

    Arquivo ausente (1ª execução) ou corrompido não é erro fatal — só
    loga um aviso e segue com frota vazia.
    """
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        ids = data.get("charger_ids", [])
        return [cid for cid in ids if isinstance(cid, str) and cid.strip()]
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            f"[PAINEL] não foi possível ler --persist-file '{path}' ({exc!r}) "
            f"— começando com frota vazia"
        )
        return []


def _save_fleet_ids(path: "str | None", charger_ids, logger: logging.Logger):
    """
    Grava a lista atual de charge_point_id em --persist-file — chamada
    a cada spawn/remove bem-sucedido (ver spawn()/remove() em main()).
    Escrita atômica (escreve num arquivo .tmp ao lado e faz os.replace)
    pra nunca deixar o arquivo pela metade se o processo morrer bem no
    meio da escrita.
    """
    if not path:
        return
    try:
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump({"charger_ids": sorted(charger_ids)}, fh, indent=2)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning(f"[PAINEL] não foi possível salvar --persist-file '{path}' ({exc!r})")


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
    if config.persist_file:
        lines.append(f"  Persistência de frota: {config.persist_file}")
    if config.control_token:
        lines.append("  ⚠ Autenticação ativa — /api/* exige --control-token em todo request")
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
        try:
            await run_charger_lifecycle(config, logger, registry=None, enable_console=True)
        except asyncio.CancelledError:
            # SIGINT/SIGTERM (ver __main__.py) — run_charger_lifecycle já
            # cancela seus próprios loops de fundo e fecha a conexão
            # graciosamente no finally dele antes disso propagar até aqui.
            logger.info("Encerrando graciosamente...")
            raise
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

    async def spawn(charge_point_id: str, overrides: dict | None = None) -> str:
        charge_point_id = (charge_point_id or "").strip()
        if not charge_point_id:
            raise ValueError("charge_point_id vazio")
        if charge_point_id in tasks:
            raise ValueError(f"charger '{charge_point_id}' já existe")

        charger_config = replace(base_config, charge_point_id=charge_point_id)
        if overrides:
            # Whitelist explícita (CHARGER_OVERRIDE_FIELDS) — o payload
            # vem de uma requisição HTTP (POST /api/chargers), então
            # nunca confiamos nele o bastante pra fazer replace() com
            # chaves arbitrárias (poderia incluir "url"/"console"/etc.,
            # que não fazem sentido por charger individual).
            unknown = set(overrides) - CHARGER_OVERRIDE_FIELDS
            if unknown:
                raise ValueError(
                    f"campo(s) de override não permitido(s): {', '.join(sorted(unknown))}. "
                    f"Permitidos: {', '.join(sorted(CHARGER_OVERRIDE_FIELDS))}"
                )
            try:
                charger_config = replace(charger_config, **overrides)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"valor de override inválido: {exc}")

        charger_logger = build_logger(charge_point_id, config.verbose)
        task = asyncio.create_task(
            run_charger_lifecycle(
                charger_config, charger_logger, registry=registry, enable_console=False
            )
        )
        tasks[charge_point_id] = task
        _save_fleet_ids(config.persist_file, tasks.keys(), dash_logger)
        return f"charger '{charge_point_id}' adicionado"

    async def remove(charge_point_id: str) -> str:
        task = tasks.pop(charge_point_id, None)
        if task is None:
            raise ValueError(f"charger '{charge_point_id}' não encontrado")
        # Tira do registry ANTES de cancelar — o painel não deve mais
        # conseguir mandar comando pra um charger que já está saindo.
        # Guarda a referência à parte (cp) porque ainda precisamos dela
        # logo abaixo pro StopTransaction gracioso, mesmo já fora do
        # registry.
        cp = registry.pop(charge_point_id, None)

        # Encerramento gracioso: se há sessão ATIVA (ou um START em
        # voo) e o charger está ONLINE, encerra a sessão no CSMS ANTES
        # de cancelar a task. Faltava por completo — remover pelo
        # painel ia direto pro cancel(), que só derruba a conexão
        # (igual ao botão "Disconnect", que é chaos de propósito — ver
        # comando "disconnect" em execute_command). Do ponto de vista do
        # CSMS isso é indistinguível de um charger real perdendo
        # energia no meio de uma sessão: nenhum StopTransaction chega,
        # a sessão fica "Charging" pendurada até o próprio CSMS arbitrar
        # isso via timeout de conexão/heartbeat — exatamente o
        # "disconnect não esperado" que estava sendo reportado.
        #
        # Só tenta se ONLINE: offline não há como entregar nada ao CSMS
        # agora — enfileirar seria inútil, já que a task é cancelada
        # antes de qualquer reconexão futura ter chance de esvaziar a
        # fila offline.
        if cp is not None and cp.is_online and cp._start_in_progress:
            # StartTransaction já em voo (Authorize aceito, request
            # mandado), mas ainda sem StartTransaction.conf — não há
            # active_transaction_id pra encerrar AGORA. Mesmo mecanismo
            # de sinal que on_reset usa (_abort_pending_start_reason):
            # quando _send_start_transaction resolver, ela mesma dispara
            # o StopTransaction sozinha. Best-effort, não bloqueante —
            # não há como esperar de forma segura aqui sem risco de
            # StopTransaction duplicado (o próprio fluxo interno já
            # dispara o dele via create_task assim que resolver); só
            # cobre o caso da resposta chegar antes da conexão cair de
            # verdade lá embaixo (task.cancel() + fechamento do
            # websocket, alguns instantes depois deste ponto).
            cp._abort_pending_start_reason = Reason.other
            dash_logger.info(
                f"[PAINEL] '{charge_point_id}' será removido com um StartTransaction "
                "em voo — sinalizado para encerrar assim que confirmar."
            )

        if cp is not None and cp.state.active_transaction_id is not None and cp.is_online:
            # skip_status_flow=True porque não faz sentido reportar
            # Finishing->Available de um charger que está de saída
            # (mesmo padrão de hard reset/fault — ver
            # _handle_reset_flow/_send_fault_notification). wait_for com
            # timeout curto (bem abaixo do timeout HTTP de 15s do DELETE
            # em control_panel.py) — se o CSMS não confirmar a tempo,
            # remove mesmo assim, sem travar o painel esperando
            # indefinidamente por um StopTransaction que pode não vir.
            dash_logger.info(
                f"[PAINEL] '{charge_point_id}' será removido com sessão ativa "
                f"(tx={cp.state.active_transaction_id}) — encerrando com "
                "StopTransaction antes de desconectar..."
            )
            try:
                await asyncio.wait_for(
                    cp._send_stop_transaction(
                        cp.state.active_transaction_id,
                        reason=Reason.other,
                        skip_status_flow=True,
                    ),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                dash_logger.warning(
                    f"[PAINEL] StopTransaction de '{charge_point_id}' não confirmou "
                    "em 10s ao remover — removendo mesmo assim."
                )
            except Exception:
                dash_logger.exception(
                    f"[PAINEL] erro encerrando sessão de '{charge_point_id}' antes "
                    "de remover — removendo mesmo assim."
                )

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        _save_fleet_ids(config.persist_file, tasks.keys(), dash_logger)
        return f"charger '{charge_point_id}' removido"

    async def broadcast_command(cmd: str, args: list, ids: "list | None" = None) -> dict:
        """
        Executa `cmd` em paralelo nos chargers do registry — em TODOS,
        ou só nos listados em `ids` quando fornecido (usado pelo painel
        pra respeitar o filtro de busca nas ações "todos": ver
        _handle_command_all em control_panel.py). Uma exceção isolada
        em um charger vira só uma mensagem de erro na entrada dele do
        dict de retorno, sem derrubar os demais.

        Retorna {charge_point_id: mensagem}. Dict vazio se não há
        nenhum charger correspondente (registry vazio, ou `ids` não
        bate com nenhum charger de fato registrado).
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
        if ids is not None:
            wanted = set(ids)
            pairs = [(cid, cp) for cid, cp in registry.items() if cid in wanted]
        else:
            pairs = list(registry.items())
        if not pairs:
            return {}
        results = await asyncio.gather(*(_run_one(cid, cp) for cid, cp in pairs))
        return dict(results)

    control_server = start_control_server(
        registry, config.control_port, loop, dash_logger,
        spawn=spawn, remove=remove, broadcast=broadcast_command,
        control_token=config.control_token,
    )

    # --fleet (CLI, desta execução) e --persist-file (execuções
    # anteriores) se somam — dedup preservando --fleet primeiro, já que
    # é a intenção explícita de AGORA.
    preloaded_ids = list(config.fleet_ids)
    persisted_ids = _load_fleet_ids(config.persist_file, dash_logger)
    restored_ids = [cid for cid in persisted_ids if cid not in preloaded_ids]
    preloaded_ids.extend(restored_ids)

    _print_dashboard_banner(config, config.control_port, preloaded_ids)
    if restored_ids:
        dash_logger.info(
            f"[PAINEL] restaurados de --persist-file: {', '.join(restored_ids)}"
        )
    for charge_point_id in preloaded_ids:
        try:
            await spawn(charge_point_id)
        except ValueError as exc:
            dash_logger.warning(f"[PAINEL] não foi possível pré-carregar '{charge_point_id}': {exc}")

    try:
        # Roda pra sempre — os chargers de fato vêm e vão via
        # spawn()/remove() disparados pelo painel web, não por um
        # conjunto fixo de tasks decidido na largada (ao contrário do
        # antigo modo --fleet sozinho). Só sai daqui por cancelamento
        # (SIGINT/SIGTERM — ver __main__.py).
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        dash_logger.info(
            "[PAINEL] encerrando graciosamente — parando painel e todos os chargers..."
        )
        raise
    finally:
        # Corre em qualquer saída (só existe uma na prática: cancelamento
        # do SIGINT/SIGTERM) — pra um restart nunca deixar chargers ou o
        # painel web órfãos rodando contra um event loop que já foi embora.
        # server.shutdown() é bloqueante (sincrono) por design do
        # socketserver; roda num executor pra não travar o event loop
        # principal enquanto isso.
        await loop.run_in_executor(None, control_server.shutdown)
        for task in list(tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        dash_logger.info("[PAINEL] painel e todos os chargers encerrados.")



