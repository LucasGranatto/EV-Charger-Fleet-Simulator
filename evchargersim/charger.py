"""
evchargersim.charger — EVChargerSim: a classe que representa um Charge
Point AC genérico do ponto de vista do protocolo OCPP 1.6J. Implementa
os handlers de mensagens que o CSMS pode mandar PARA o charge point, os
loops de fundo (heartbeat, meter values, acumulador de energia), o
console de texto (modo charger único) e execute_command() — a lógica de
comando compartilhada com o painel web de controle (modo frota).
"""

import asyncio
import logging
import random
import sys
from datetime import datetime, timezone

import websockets
# `websockets` usa lazy loading e não expõe `exceptions` por padrão —
# sem este import explícito, `websockets.exceptions.ConnectionClosed`
# levanta AttributeError na hora de casar a exceção, mascarando quedas
# de rede reais em vez de capturá-las (bug real, corrigido aqui).
import websockets.exceptions
from ocpp.routing import on
from ocpp.v16 import call, call_result
from ocpp.v16 import ChargePoint as BaseChargePoint
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    AvailabilityStatus,
    AvailabilityType,
    CancelReservationStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    ClearCacheStatus,
    DataTransferStatus,
    DiagnosticsStatus,
    FirmwareStatus,
    Reason,
    RegistrationStatus,
    RemoteStartStopStatus,
    ReservationStatus,
    ResetType,
    UnlockStatus,
    UpdateStatus,
)

from .config import FAULT_CODE_MAP, SimConfig
from .physics import compute_actual_current, read_grid_voltage, _meter_line_color
from .state import ChargerState

# Tamanho da janela deslizante de state.history — ver
# EVChargerSim._record_history_sample(). Com o intervalo padrão de
# MeterValues (30s), 120 amostras cobrem 1h de histórico por charger;
# não é configurável via CLI de propósito (é só pro gráfico do painel,
# não pra análise de longo prazo — ver README pra exportar via /api/state
# se precisar de mais).
_HISTORY_MAX_SAMPLES = 120

# Campos de chaos ajustáveis AO VIVO via POST /api/chargers/<id>/chaos
# — ver EVChargerSim.apply_chaos_overrides(). Mesmo conjunto de nomes
# de CHARGER_OVERRIDE_FIELDS (config.py) restrito só aos de chaos —
# não reaproveita a constante de lá de propósito: aquela também inclui
# campos que só fazem sentido na CRIAÇÃO do charger (bateria/SoC
# inicial/corrente padrão), que não devem ser editáveis depois que a
# sessão já está rodando.
_CHAOS_FIELDS = frozenset({
    "chaos_disconnect_interval_seconds",
    "chaos_disconnect_jitter_seconds",
    "chaos_latency_min_ms",
    "chaos_latency_max_ms",
    "chaos_drop_rate",
    "max_offline_queue_size",
})

class EVChargerSim(BaseChargePoint):
    """
    Representa um Charge Point AC genérico do ponto de vista do protocolo.
    Implementa os handlers de mensagens que o CSMS pode mandar PARA o charge point.
    """

    def __init__(self, charge_point_id, connection, config: SimConfig, logger: logging.Logger):
        super().__init__(charge_point_id, connection)
        self.config = config
        # NÃO usar `self.logger` — BaseChargePoint já usa esse nome
        # internamente pra logar toda mensagem OCPP crua (via logger
        # "ocpp", suprimido em build_logger()). Sobrescrever com o
        # logger deste módulo faz esse tráfego vazar pro terminal. Por
        # isso o logger próprio da classe se chama `self.log`.
        self.log = logger
        self.state = ChargerState(
            battery_soc_percent=config.initial_soc_percent,
            current_heartbeat_interval=config.heartbeat_interval,
        )
        self.use_color = sys.stdout.isatty()

        # Task de agendamento do perfil de carga ativo (ver
        # _run_charging_schedule) — instância, não ChargerState, porque é
        # uma asyncio.Task, não dado serializável.
        self._profile_task: asyncio.Task | None = None

        # Plumbing de conectividade — também instância, não ChargerState
        # (são detalhes de transporte, não "dados simulados"). main()
        # alterna is_online e reatribui self._connection a cada
        # queda/reconexão; a instância inteira persiste entre elas.
        self.is_online: bool = False
        self._local_tx_counter: int = 0

        # True assim que o BootNotification INICIAL é aceito pelo CSMS
        # — não reseta em quedas/reconexões seguintes (é por isso que
        # não é ChargerState: sobrevive à sessão de transporte, só não
        # sobrevive a uma instância nova de verdade). Usado por
        # run_reconnect_sequence pra distinguir "queda depois de já
        # registrado" (não reenvia BootNotification, só resincroniza)
        # de "queda no meio das tentativas do boot inicial, antes de
        # qualquer Accepted" (precisa continuar tentando o boot — ainda
        # não existe registro nenhum do lado do CSMS pra resincronizar).
        self._boot_confirmed: bool = False

        # Guarda "um início de sessão já está em andamento" — cobre a
        # janela entre aceitar um RemoteStart/start local e
        # active_transaction_id ser de fato gravado em
        # _send_start_transaction. SEM isso, um segundo
        # RemoteStartTransaction (ex.: reenviado pelo próprio CSMS após
        # seu client-side timeout, enquanto o primeiro ainda nem tinha
        # terminado) passa pelo guard `active_transaction_id is not
        # None` porque esse campo ainda está vazio, e dispara uma SEGUNDA
        # _send_start_transaction concorrente — resultando em dois
        # StartTransaction completos pro mesmo conector físico. Ver
        # _try_begin_start/_end_start.
        self._start_in_progress: bool = False

        # Sinaliza pra _send_start_transaction que, assim que
        # active_transaction_id resolver (sucesso, enfileirado, ou
        # timeout sem confirmação), a sessão deve ser encerrada
        # IMEDIATAMENTE em vez de seguir pra Charging — usado por
        # on_reset quando o Reset chega enquanto o start ainda está em
        # andamento (active_transaction_id ainda None, então o guard
        # "sessão ativa" normal do on_reset não vê nada pra parar ainda).
        # Sem isso, um Reset nesse instante exato é silenciosamente
        # ignorado pela sessão que só termina de iniciar um instante
        # depois — o carregador fica "carregando" apesar do reset.
        self._abort_pending_start_reason: "Reason | None" = None

        # Reentrância de _flush_offline_queue: send_meter_values_loop
        # tenta esvaziar a fila a cada ciclo (oportunista) E
        # run_reconnect_sequence também chama no reconnect — se um flush
        # demorado (item lento/timeout) ainda estiver rodando quando o
        # próximo gatilho disparar, duas chamadas concorrentes podem
        # entrelaçar o envio das mensagens fora de ordem. Ver
        # _flush_offline_queue.
        self._flush_in_progress: bool = False

    def _try_begin_start(self) -> bool:
        """
        Tenta reservar "iniciando sessão" de forma atômica (sem await
        entre o check e o set — por isso é um método síncrono comum,
        não uma coroutine, e deve ser chamado ANTES de qualquer await
        no fluxo de start). Retorna False se já há um início em
        andamento; quem chama deve recusar o pedido nesse caso.
        """
        if self._start_in_progress:
            return False
        self._start_in_progress = True
        return True

    def _end_start(self):
        """Libera a reserva de _try_begin_start — SEMPRE via finally, em qualquer desfecho."""
        self._start_in_progress = False

    # --------------------------------------------------------
    # Handlers de mensagens recebidas do CSMS
    # (a lib ocpp converte todo o payload recursivamente de camelCase
    # pra snake_case antes de chamar estes handlers — nunca precisa de
    # fallback pra chaves tipo "startPeriod"/"idTag")
    # --------------------------------------------------------

    def _limit_to_amps(self, limit: float, unit: str) -> float:
        """
        Converte um limite de chargingSchedulePeriod para amperes.
        "W" é convertido usando a tensão nominal (simplificação
        monofásica); "A" passa direto.
        """
        if unit == "W":
            return round(limit / self.config.nominal_voltage, 2)
        if unit and unit != "A":
            self.log.warning(
                f"[PERFIL RECEBIDO] chargingRateUnit desconhecido '{unit}' — "
                "tratando como amperes (A)."
            )
        return float(limit)

    def _cancel_profile_task(self):
        """Cancela a task de agendamento de um perfil anterior, se houver."""
        if self._profile_task is not None and not self._profile_task.done():
            self._profile_task.cancel()
        self._profile_task = None

    def _enqueue_offline(self, kind: str, request, local_tx_id: int | None = None):
        """
        Acrescenta uma mensagem à fila offline, pra reenvio na próxima
        reconexão. Respeita config.max_offline_queue_size (0 = sem
        teto): ao atingir o limite, descarta a mais ANTIGA (FIFO) antes
        de enfileirar a nova — sem isso, um charger desconectado por
        muito tempo (CSMS caído, chaos-disconnect prolongado) acumula
        mensagem sem parar, e a reconexão despejaria tudo de uma vez no
        CSMS. Um charger real também tem buffer local finito.
        """
        queue = self.state.offline_queue
        max_size = self.config.max_offline_queue_size
        if max_size > 0 and len(queue) >= max_size:
            dropped = queue.pop(0)
            self.log.warning(
                f"[FILA OFFLINE] cheia ({max_size} mensagem(ns)) — descartando a mais "
                f"antiga ('{dropped['kind']}') para abrir espaço para '{kind}'. Ajuste "
                f"--max-offline-queue se isso não for desejado para este teste."
            )
        queue.append(
            {"kind": kind, "request": request, "local_tx_id": local_tx_id}
        )
        self.log.info(
            f"[FILA OFFLINE] '{kind}' enfileirado "
            f"(fila agora com {len(queue)} mensagem(ns))."
        )

    async def _call_or_queue(
        self,
        request,
        kind: str,
        queueable: bool = True,
        timeout: float | None = None,
        local_tx_id: int | None = None,
        return_queued: bool = False,
    ):
        """
        Ponto único por onde toda mensagem espontânea do charger
        (StatusNotification, MeterValues, Heartbeat, Start/StopTransaction)
        passa antes de sair pela rede. Duas responsabilidades:

        1) Fila offline: SÓ enfileira quando a mensagem de fato não saiu
           pela rede — offline já na entrada, chaos derrubando antes de
           tentar, ou a conexão caindo durante a tentativa
           (ConnectionClosed/OSError). Um simples asyncio.TimeoutError
           NÃO enfileira mais: a mensagem já tinha sido enviada (self.call
           manda o CALL antes de esperar a resposta) e o socket continua
           de pé — não há como saber se o CSMS recebeu/processou.
           Reenviar a mesma Start/StopTransaction nessa hora é o jeito
           mais fácil de o CSMS acabar com uma transação fantasma
           duplicada, caso ele só estivesse lento (não caído) — visto na
           prática contra um CSMS real que ocasionalmente estourava
           call_timeout_seconds só de lento, e cada timeout virava um
           reenvio silencioso registrado como uma segunda sessão pro
           mesmo conector. Ver _send_start_transaction/_send_stop_transaction
           pra como o "sem resposta, mas sem reenviar" é tratado.
        2) Chaos: latência artificial e perda simulada (SimConfig.chaos_*)
           são aplicadas aqui, antes de qualquer tentativa de envio — mas
           só quando há de fato uma conexão pra "perturbar" (offline
           checado primeiro, ver abaixo).

        return_queued=True muda o retorno para (response, queued) — só os
        dois chamadores que precisam saber se a mensagem foi de fato
        salva pra reenvio usam isso (_send_start_transaction/
        _send_stop_transaction), pra decidir como representar uma sessão
        cujo destino no CSMS ficou desconhecido. Todo o resto continua
        recebendo só `response | None`, como sempre.

        Retorna a resposta do CSMS, ou None se enfileirada, descartada
        (chaos) ou sem resposta a tempo.
        """
        def _result(response, queued):
            return (response, queued) if return_queued else response

        timeout = timeout if timeout is not None else self.config.call_timeout_seconds

        # Offline de verdade: chaos não tem nada pra perturbar aqui — a
        # mensagem já não ia sair mesmo. Resolver isso primeiro evita
        # pagar latência/drop artificiais em cima de algo que já está
        # simplesmente sem conexão.
        if not self.is_online:
            if queueable:
                self._enqueue_offline(kind, request, local_tx_id=local_tx_id)
                return _result(None, True)
            self.log.debug(f"[OFFLINE] '{kind}' pulado (não crítico, não enfileirável).")
            return _result(None, False)

        # Chaos: perda de mensagem simulada — a mensagem nunca chega a
        # sair, então enfileirar é seguro (equivalente a estar offline).
        if self.config.chaos_drop_rate > 0 and random.random() < self.config.chaos_drop_rate:
            self.log.warning(f"[CHAOS] '{kind}' descartado (perda de rede simulada).")
            if queueable:
                self._enqueue_offline(kind, request, local_tx_id=local_tx_id)
                return _result(None, True)
            return _result(None, False)

        # Chaos: atraso artificial, contabilizado DENTRO do orçamento de
        # timeout (não somado por fora) — chaos_latency_max_ms acima de
        # call_timeout_seconds simula "o CSMS não respondeu a tempo".
        remaining_timeout = timeout
        if self.config.chaos_latency_max_ms > 0:
            delay_ms = random.uniform(
                self.config.chaos_latency_min_ms, self.config.chaos_latency_max_ms
            )
            delay_s = delay_ms / 1000
            if delay_s >= remaining_timeout:
                await asyncio.sleep(remaining_timeout)
                # Diferente do timeout real mais abaixo: self.call() nunca
                # chega a ser invocado aqui, então a mensagem NUNCA saiu do
                # processo (equivalente ao chaos_drop_rate acima, não ao
                # asyncio.TimeoutError pós-envio) — reenfileirar é seguro,
                # sem risco da duplicata que o timeout pós-envio evita.
                self.log.warning(
                    f"[CSMS] '{kind}' nem chegou a ser enviada — orçamento de "
                    f"{timeout}s todo consumido por latência simulada "
                    "(chaos_latency) antes do envio."
                )
                if queueable:
                    self._enqueue_offline(kind, request, local_tx_id=local_tx_id)
                    return _result(None, True)
                return _result(None, False)
            if delay_s > 0:
                await asyncio.sleep(delay_s)
                remaining_timeout -= delay_s

        try:
            response = await asyncio.wait_for(self.call(request), timeout=remaining_timeout)
            return _result(response, False)
        except asyncio.TimeoutError:
            # A mensagem SAIU e o socket segue de pé — não sabemos se o
            # CSMS recebeu/processou. Deliberadamente NÃO enfileira (ver
            # docstring): quem chama decide como lidar com "sem resposta,
            # conexão ok".
            self.log.warning(
                f"[CSMS] '{kind}' não teve resposta em {timeout}s — conexão "
                "segue online, então NÃO reenviando automaticamente (o CSMS "
                "pode só estar lento, não ter perdido a mensagem)."
            )
            return _result(None, False)
        except (websockets.exceptions.ConnectionClosed, OSError) as exc:
            self.log.warning(f"[OFFLINE] conexão perdida enviando '{kind}' ({exc!r}).")
            self.is_online = False
            if queueable:
                self._enqueue_offline(kind, request, local_tx_id=local_tx_id)
                return _result(None, True)
            return _result(None, False)

    async def _flush_offline_queue(self):
        """
        Reenvia em ordem as mensagens acumuladas enquanto offline —
        StatusNotification/MeterValues/Start·StopTransaction chegam ao
        CSMS na mesma ordem em que aconteceram de verdade.

        Se um StartTransaction enfileirado usava um ID local temporário
        (negativo, atribuído por _send_start_transaction enquanto
        offline), o ID real devolvido pelo CSMS é propagado para
        qualquer StopTransaction enfileirado depois com esse mesmo ID
        local — senão o CSMS receberia um StopTransaction pra um
        transaction_id que nunca existiu do lado dele.

        Limitação conhecida (do protocolo, não deste simulador): OCPP
        1.6 não tem idempotência embutida — se a conexão cair depois do
        CSMS já ter processado uma mensagem mas antes da confirmação
        chegar aqui, um reenvio no próximo flush pode duplicar essa
        mensagem do lado do servidor.
        """
        if self._flush_in_progress:
            # send_meter_values_loop tenta esvaziar a fila a cada ciclo
            # (oportunista) e run_reconnect_sequence também chama no
            # reconnect — se um flush anterior ainda estiver rodando
            # (ex.: um item lento perto do call_timeout_seconds), uma
            # segunda chamada concorrente poderia pegar itens novos
            # enfileirados nesse meio tempo e mandá-los ANTES dos itens
            # mais antigos ainda em trânsito no primeiro flush — quebra
            # a ordem que esta função promete manter. Só pula; o próximo
            # ciclo tenta de novo.
            self.log.debug("[FILA OFFLINE] flush já em andamento — pulando chamada concorrente.")
            return

        state = self.state
        if not state.offline_queue:
            return

        self._flush_in_progress = True
        try:
            queue = state.offline_queue
            state.offline_queue = []  # o que não for entregue volta pro final, abaixo
            self.log.info(
                f"[FILA OFFLINE] reconectado — reenviando {len(queue)} mensagem(ns) pendente(s)..."
            )
            local_to_real: dict[int, int] = {}

            for i, item in enumerate(queue):
                kind, request, local_tx_id = item["kind"], item["request"], item["local_tx_id"]

                # Corrige a referência de ID local -> real antes de enviar,
                # se já resolvida por um StartTransaction anterior nesta
                # mesma rodada de flush.
                if kind == "StopTransaction" and local_tx_id in local_to_real:
                    request.transaction_id = local_to_real[local_tx_id]

                try:
                    response = await asyncio.wait_for(
                        self.call(request), timeout=self.config.call_timeout_seconds
                    )
                except asyncio.TimeoutError:
                    # A mensagem SAIU e o socket pode muito bem seguir de
                    # pé — um timeout aqui NÃO prova que a conexão caiu
                    # (mesmo raciocínio de _call_or_queue). Diferente do
                    # ConnectionClosed abaixo, deliberadamente NÃO marca
                    # is_online=False por causa disso: fazer isso deixaria
                    # o simulador "preso" acreditando estar offline pra
                    # sempre mesmo com o socket saudável, já que nada mais
                    # detectaria essa queda que nunca aconteceu (o listener
                    # central — cp.start() — segue lendo normalmente).
                    # Também não reenfileira este item — seria o mesmo
                    # risco de duplicar já corrigido em _call_or_queue.
                    # Loga como ERROR (merece atenção manual) e segue pro
                    # próximo item, em vez de abortar o resto do flush por
                    # causa de UM item incerto.
                    self.log.error(
                        f"[FILA OFFLINE] '{kind}' sem resposta do CSMS em "
                        f"{self.config.call_timeout_seconds}s durante o flush "
                        "— conexão segue online, então NÃO reenfileirando "
                        "(evita duplicar) e seguindo para o próximo item. "
                        "Verifique manualmente se o CSMS recebeu esta mensagem."
                    )
                    continue
                except (websockets.exceptions.ConnectionClosed, OSError) as exc:
                    self.log.warning(
                        f"[FILA OFFLINE] conexão caiu de novo durante o flush ({exc!r}) — "
                        f"{len(queue) - i} mensagem(ns) voltam para a fila."
                    )
                    self.is_online = False
                    # Soma (não sobrescreve): entre o início deste flush
                    # (que já tinha zerado state.offline_queue, linha
                    # acima) e agora, outra coroutine concorrente pode ter
                    # enfileirado algo nesse meio-tempo (ex: um fault ou
                    # StatusNotification que também perdeu conexão nessa
                    # janela) — um `=` simples aqui descartaria essas
                    # mensagens silenciosamente em vez de preservá-las.
                    state.offline_queue = queue[i:] + state.offline_queue
                    return

                self.log.info(f"[FILA OFFLINE] '{kind}' entregue com sucesso.")

                if kind == "StartTransaction" and local_tx_id is not None and response is not None:
                    real_id = response.transaction_id
                    local_to_real[local_tx_id] = real_id
                    if state.active_transaction_id == local_tx_id:
                        state.active_transaction_id = real_id
                    self.log.info(
                        f"[FILA OFFLINE] ID local {local_tx_id} resolvido para "
                        f"transaction_id real {real_id}"
                    )
                    if not self._start_transaction_authorized(response):
                        tag_status = response.id_tag_info.get("status")
                        self.log.warning(
                            f"[FILA OFFLINE] StartTransaction confirmado mas "
                            f"id_tag_info.status={tag_status} — abortando a sessão "
                            "que já rodava offline, StopTransaction imediato."
                        )
                        asyncio.create_task(self._send_stop_transaction(real_id, reason=Reason.other))

            self.log.info("[FILA OFFLINE] todas as mensagens pendentes foram entregues.")
        finally:
            self._flush_in_progress = False

    def _apply_offered_amps(self, offered_amps: float, source: str):
        """
        Aplica um novo limite de corrente oferecida e, se necessário,
        reflete a mudança num StatusNotification SuspendedEVSE/Charging.
        Extraído do handler de perfil original para ser reutilizável pelo
        agendador de múltiplos períodos (_run_charging_schedule) sem
        duplicar a lógica de suspensão.
        """
        state = self.state
        state.current_offered_amps = offered_amps
        state.current_actual_amps = compute_actual_current(
            offered_amps, state.battery_soc_percent
        )
        self.log.info(
            f"[{source}] limite oferecido={state.current_offered_amps}A | "
            f"corrente real (SoC {state.battery_soc_percent:.0f}%)={state.current_actual_amps}A"
        )

        # Reflete no StatusNotification quando o CSMS impõe/restaura 0A —
        # senão o status ficava travado em "Charging" mesmo com corrente
        # zerada. Só entra em jogo com sessão ativa e sem SuspendedEV
        # (que tem prioridade — é uma causa de suspensão diferente).
        if state.active_transaction_id is not None and not state.session_suspended:
            if state.current_offered_amps <= 0.0 and not state.evse_suspended_by_profile:
                state.evse_suspended_by_profile = True
                self.log.info(f"[{source}] 0A imposto pelo CSMS → SuspendedEVSE")
                asyncio.create_task(self.send_status_notification(
                    ChargePointStatus.suspended_evse))
            elif state.current_offered_amps > 0.0 and state.evse_suspended_by_profile:
                state.evse_suspended_by_profile = False
                self.log.info(f"[{source}] corrente restaurada pelo CSMS → Charging")
                asyncio.create_task(self.send_status_notification(
                    ChargePointStatus.charging))

    async def _run_charging_schedule(self, periods: list, unit: str):
        """
        Percorre TODOS os períodos de um chargingSchedule, não só o
        primeiro — antes um perfil com múltiplos degraus era achatado no
        valor do primeiro pra sessão inteira.

        Simplificação: cada start_period é tratado como segundos
        relativos ao momento em que este SetChargingProfile foi
        recebido (não ao início da transação nem a um startSchedule
        absoluto) — suficiente pra testar degraus manualmente; perfis
        recorrentes (Daily/Weekly) não são interpretados de forma especial.
        """
        ordered = sorted(periods, key=lambda p: p.get("start_period", 0))
        try:
            for i, period in enumerate(ordered):
                start_period = period.get("start_period", 0)
                amps = self._limit_to_amps(period["limit"], unit)
                self._apply_offered_amps(amps, source="PERFIL RECEBIDO")

                if i + 1 < len(ordered):
                    next_start = ordered[i + 1].get("start_period", 0)
                    wait = max(0, next_start - start_period)
                    if wait > 0:
                        self.log.info(
                            f"[PERFIL RECEBIDO] período atual válido por {wait}s "
                            f"antes do próximo degrau do perfil"
                        )
                        await asyncio.sleep(wait)
        except asyncio.CancelledError:
            # Esperado sempre que um novo SetChargingProfile, um
            # ClearChargingProfile, ou o fim da sessão substitui este
            # agendamento antes que ele termine sozinho — não é um erro.
            pass

    @on(Action.set_charging_profile)
    async def on_set_charging_profile(self, connector_id, cs_charging_profiles, **kwargs):
        """
        Chamado quando o CSMS manda um novo perfil de carga (ex: limitar a
        10A, ou uma rampa de vários degraus). Aqui simulamos o charge
        point "aceitando" e agendando a aplicação de todos os períodos.
        """
        schedule = cs_charging_profiles["charging_schedule"]
        periods = schedule["charging_schedule_period"]
        unit = schedule.get("charging_rate_unit", "A")

        self._cancel_profile_task()

        if periods:
            self.log.info(
                f"[PERFIL RECEBIDO] connector={connector_id} | "
                f"{len(periods)} período(s) | unidade={unit}"
            )
            self._profile_task = asyncio.create_task(
                self._run_charging_schedule(periods, unit)
            )
        else:
            self.log.warning("SetChargingProfile recebido sem chargingSchedulePeriod")

        return call_result.SetChargingProfile(status="Accepted")

    @on(Action.clear_charging_profile)
    async def on_clear_charging_profile(self, **kwargs):
        """
        Remove o(s) perfil(is) ativo(s) e volta à corrente padrão do
        simulador (se sessão ativa) ou 0A.
        """
        self._cancel_profile_task()
        state = self.state

        fallback_amps = (
            self.config.default_offered_amps if state.active_transaction_id is not None else 0.0
        )
        self._apply_offered_amps(fallback_amps, source="PERFIL LIMPO")
        self.log.info(
            "[CLEAR CHARGING PROFILE] perfil removido — voltando à corrente "
            f"padrão ({fallback_amps:.0f}A)"
        )
        return call_result.ClearChargingProfile(status="Accepted")

    @on(Action.remote_start_transaction)
    async def on_remote_start_transaction(self, id_tag, connector_id=None, **kwargs):
        self.log.info(f"[REMOTE START] id_tag={id_tag} connector={connector_id}")
        state = self.state

        if state.availability_status == "Inoperative":
            self.log.warning(
                "[REMOTE START] conector Inoperative (ChangeAvailability) — recusando."
            )
            return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)
        if state.active_transaction_id is not None:
            self.log.warning("[REMOTE START] já existe sessão ativa — recusando.")
            return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)
        if state.is_faulted:
            self.log.warning("[REMOTE START] charger em Faulted — recusando.")
            return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)
        if not self._try_begin_start():
            # active_transaction_id só é gravado dentro de
            # _send_start_transaction — antes disso, o guard acima não
            # pega um segundo RemoteStart que chegue enquanto o primeiro
            # ainda está a caminho (ex.: o próprio CSMS reenviando após
            # dar timeout na resposta dele, sem cancelar a tentativa
            # anterior). Sem este guard, os dois seguem em paralelo e o
            # CSMS acaba com duas transações completas pro mesmo conector.
            self.log.warning(
                "[REMOTE START] já existe um início de sessão em andamento "
                "(aguardando StartTransaction confirmar) — recusando para "
                "evitar StartTransaction duplicado."
            )
            return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)

        # Dispara o envio de StartTransaction em background, DEPOIS de responder
        # Accepted — replica o fluxo real: o carregador aceita o comando e só
        # manda StartTransaction como mensagem separada um instante depois
        # (após fechar o contator / autorizar localmente).
        asyncio.create_task(
            self._send_start_transaction(connector_id or self.config.connector_id, id_tag)
        )
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @on(Action.remote_stop_transaction)
    async def on_remote_stop_transaction(self, transaction_id, **kwargs):
        self.log.info(f"[REMOTE STOP] transaction_id={transaction_id}")
        # Reason.remote é o motivo correto da OCPP para uma sessão encerrada
        # via comando remoto do CSMS (botão "Parar" no dashboard) — sem
        # isso, o campo "reason" ia como None/nulo, e o histórico de
        # sessões nunca mostrava motivo nenhum para o caso mais comum.
        asyncio.create_task(
            self._send_stop_transaction(transaction_id, reason=Reason.remote)
        )
        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)

    @on(Action.change_availability)
    async def on_change_availability(self, connector_id, type, **kwargs):
        """
        Inoperative com sessão ativa -> Scheduled (aplicado só quando a
        sessão terminar, conforme o spec); sem sessão -> aplica na hora
        e manda StatusNotification Unavailable. Operative sempre aplica
        na hora (cancela um Scheduled pendente) e volta a Available.
        """
        self.log.info(f"[CHANGE AVAILABILITY] connector={connector_id} type={type}")
        state = self.state

        if type == AvailabilityType.inoperative:
            if state.active_transaction_id is not None or self._start_in_progress:
                # _start_in_progress cobre o mesmo instante que o Reset
                # trata (ver on_reset/_abort_pending_start_reason): um
                # start aceito mas ainda sem active_transaction_id
                # gravado. Aqui não precisa de um sinal de abort à parte
                # — quando a sessão resolver e active_transaction_id for
                # gravado, o mecanismo normal de "Scheduled" já aplica o
                # Inoperative no fim dela, então só tratar como sessão
                # ativa já resolve.
                state.pending_availability_change = "Inoperative"
                self.log.info(
                    "[CHANGE AVAILABILITY] sessão ativa (ou iniciando) — "
                    "mudança para Inoperative agendada para quando a "
                    "sessão terminar."
                )
                return call_result.ChangeAvailability(status=AvailabilityStatus.scheduled)

            state.availability_status = "Inoperative"
            asyncio.create_task(self.send_status_notification(ChargePointStatus.unavailable))
            return call_result.ChangeAvailability(status=AvailabilityStatus.accepted)

        # Operative
        state.pending_availability_change = None
        state.availability_status = "Operative"
        if state.active_transaction_id is None and not state.is_faulted:
            asyncio.create_task(self.send_status_notification(ChargePointStatus.available))
        return call_result.ChangeAvailability(status=AvailabilityStatus.accepted)

    @on(Action.reset)
    async def on_reset(self, type, **kwargs):
        """
        Sessão ativa é interrompida (StopTransaction com motivo
        soft/hard_reset) — não tem como continuar entregando corrente
        depois de reiniciar. Soft: interrupção breve, volta a Available
        rápido. Hard: fica Unavailable por um tempo, simulando o boot
        do firmware, antes de voltar.
        """
        self.log.info(f"[RESET] type={type}")
        is_hard = (type == ResetType.hard)
        reason = Reason.hard_reset if is_hard else Reason.soft_reset

        active_id = self.state.active_transaction_id
        if active_id is not None:
            self.log.info(
                f"[RESET] sessão ativa (tx={active_id}) será "
                f"interrompida pelo reset"
            )
            asyncio.create_task(self._handle_reset_flow(active_id, reason, is_hard))
        elif self._start_in_progress:
            # Um start foi aceito e está a caminho, mas
            # active_transaction_id ainda não foi gravado — não há nada
            # pra _handle_reset_flow parar ainda. Sinaliza pra
            # _send_start_transaction encerrar a sessão assim que ela
            # resolver, em vez de deixá-la completar e ir pra Charging
            # como se o reset nunca tivesse acontecido.
            self.log.info(
                "[RESET] início de sessão em andamento (ainda sem "
                "confirmação) — será encerrada assim que resolver."
            )
            self._abort_pending_start_reason = reason
            asyncio.create_task(self._handle_reset_flow(None, reason, is_hard))
        else:
            asyncio.create_task(self._handle_reset_flow(None, reason, is_hard))

        return call_result.Reset(status="Accepted")

    async def _handle_reset_flow(self, transaction_id, reason, is_hard: bool):
        """Executa a sequência de reset em background, após responder Accepted."""
        if transaction_id is not None:
            # skip_status_flow=True porque o reset tem sua própria sequência
            # de status abaixo (não o Finishing->Available padrão de um stop normal).
            await self._send_stop_transaction(
                transaction_id, reason=reason, skip_status_flow=True
            )

        if is_hard:
            # Hard reset: simula o carregador caindo (Unavailable) durante
            # o boot do firmware antes de voltar a responder normalmente.
            await self.send_status_notification(ChargePointStatus.unavailable)
            self.log.info("[RESET] hard reset — simulando reboot do firmware (5s)...")
            await asyncio.sleep(5)
            await self.send_boot_notification()
            await asyncio.sleep(1)
        else:
            self.log.info("[RESET] soft reset — reinício rápido do software (1s)...")
            await asyncio.sleep(1)

        await self.send_status_notification(ChargePointStatus.available)
        self.log.info("[RESET] concluído — carregador disponível novamente")

    def _current_ocpp_status(self) -> ChargePointStatus:
        """
        Deriva o ChargePointStatus real a partir do estado atual —
        usado por on_trigger_message (StatusNotification) pra reportar
        o status de verdade, em vez de só carregando/disponível.
        Prioridade: Faulted > sessão ativa (Charging/SuspendedEV/
        SuspendedEVSE) > Inoperative > Reserved > Available — mesma
        ordem usada em get_status_dict() (pro dashboard) e em
        run_reconnect_sequence().
        """
        state = self.state
        if state.is_faulted:
            return ChargePointStatus.faulted
        if state.active_transaction_id is not None:
            if state.session_suspended:
                return ChargePointStatus.suspended_ev
            if state.evse_suspended_by_profile:
                return ChargePointStatus.suspended_evse
            return ChargePointStatus.charging
        if state.availability_status == "Inoperative":
            return ChargePointStatus.unavailable
        if state.reservation_id is not None:
            return ChargePointStatus.reserved
        return ChargePointStatus.available

    @on(Action.trigger_message)
    async def on_trigger_message(self, requested_message, connector_id=None, **kwargs):
        """
        TriggerMessage pede para o carregador reenviar uma mensagem
        espontaneamente (ex: StatusNotification, Heartbeat). Usado pelo
        status_check() do CSMS real para forçar uma atualização de estado.
        """
        self.log.info(f"[TRIGGER MESSAGE] requested={requested_message} connector={connector_id}")
        if requested_message == "StatusNotification":
            # Antes só verificava "tem sessão ativa?" e reportava
            # Charging/Available — um TriggerMessage pedido com o
            # charger em Faulted/Inoperative/Reserved/Suspended
            # reportava o status errado, exatamente no handler cujo
            # propósito é ressincronizar o CSMS com o estado real.
            asyncio.create_task(self.send_status_notification(self._current_ocpp_status()))
        elif requested_message == "Heartbeat":
            # Via _call_or_queue, não self.call direto — offline, isso
            # levantaria ConnectionClosed numa task sem ninguém aguardando.
            asyncio.create_task(
                self._call_or_queue(call.Heartbeat(), kind="Heartbeat", queueable=False)
            )
        elif requested_message == "MeterValues":
            # Amostra IMEDIATA — é esse o propósito do trigger, não
            # esperar até 30s pelo próximo ciclo do loop periódico.
            asyncio.create_task(
                self._call_or_queue(self._build_meter_values_request(), kind="MeterValues")
            )
        return call_result.TriggerMessage(status="Accepted")

    @on(Action.get_configuration)
    async def on_get_configuration(self, key=None, **kwargs):
        """
        Retorna configurações simuladas de um charger AC real.
        HeartbeatInterval reporta o valor REAL em uso
        (state.current_heartbeat_interval) — um CSMS com sync loop
        periódico que sobrescreve seu próprio estado a partir daqui
        reverteria silenciosamente qualquer ChangeConfiguration se este
        handler respondesse um valor fixo em vez do atual.
        """
        # DEBUG: alguns CSMS chamam isso periodicamente (sync loop) —
        # mesmo padrão de ruído do Heartbeat. Só aparece com --verbose.
        self.log.debug(f"[GET CONFIGURATION] keys solicitadas={key}")
        all_config = [
            {"key": "HeartbeatInterval", "readonly": False,
             "value": str(self.state.current_heartbeat_interval)},
            {"key": "MeterValueSampleInterval", "readonly": False,
             "value": str(self.config.meter_values_interval)},
            {"key": "ConnectorPhaseRotation", "readonly": True, "value": "NotApplicable"},
            {"key": "NumberOfConnectors", "readonly": True, "value": "1"},
            {"key": "SupportedFeatureProfiles", "readonly": True,
             "value": "Core,SmartCharging,Reservation,LocalAuthListManagement,FirmwareManagement"},
            {"key": "LocalAuthListEnabled", "readonly": False, "value": "true"},
            {"key": "LocalAuthListMaxLength", "readonly": True, "value": "100"},
            {"key": "SendLocalListMaxLength", "readonly": True, "value": "20"},
            {"key": "AuthorizationCacheEnabled", "readonly": False,
             "value": "true" if self.state.auth_cache_enabled else "false"},
            {"key": "ReserveConnectorZeroSupported", "readonly": True, "value": "false"},
            {"key": "AvailabilityStatus", "readonly": True,
             "value": self.state.availability_status},
        ]
        if key:
            # CSMS pediu chaves específicas: filtra e reporta as desconhecidas
            known_keys_lower = {c["key"].lower() for c in all_config}
            requested_keys = {k.lower() for k in key}
            found = [c for c in all_config if c["key"].lower() in requested_keys]
            unknown = [k for k in key if k.lower() not in known_keys_lower]
            return call_result.GetConfiguration(configuration_key=found, unknown_key=unknown)
        return call_result.GetConfiguration(configuration_key=all_config, unknown_key=[])

    @on(Action.change_configuration)
    async def on_change_configuration(self, key, value, **kwargs):
        self.log.info(f"[CHANGE CONFIGURATION] key={key} value={value}")

        if key == "HeartbeatInterval":
            try:
                self.state.current_heartbeat_interval = int(value)
                self.log.info(
                    f"[HEARTBEAT] intervalo atualizado para "
                    f"{self.state.current_heartbeat_interval}s — efeito no próximo ciclo"
                )
            except ValueError:
                self.log.warning(f"[CHANGE CONFIGURATION] valor inválido para HeartbeatInterval: {value}")
                return call_result.ChangeConfiguration(status="Rejected")
        elif key == "AuthorizationCacheEnabled":
            enabled = str(value).strip().lower() in ("true", "1", "yes")
            self.state.auth_cache_enabled = enabled
            self.log.info(
                f"[AUTH CACHE] {'habilitada' if enabled else 'desabilitada'} via "
                "ChangeConfiguration."
            )
            # Desabilitar não apaga entradas já guardadas (comportamento
            # comum em charger real: religar depois volta a valer o que
            # já tinha) — só ClearCache realmente esvazia o dicionário.
        # Outras chaves são aceitas mas sem efeito simulado (ex:
        # MeterValueSampleInterval é fixo via config no boot).

        return call_result.ChangeConfiguration(status="Accepted")

    @on(Action.unlock_connector)
    async def on_unlock_connector(self, connector_id, **kwargs):
        """Libera o conector mecanicamente (ex: cabo travado)."""
        self.log.info(f"[UNLOCK CONNECTOR] connector={connector_id}")
        if self.state.active_transaction_id is not None:
            # Comportamento simplificado: não paramos a sessão
            # automaticamente — UnlockConnector não é, por si só, um
            # pedido de StopTransaction.
            self.log.warning(
                "[UNLOCK CONNECTOR] há uma sessão ativa — destravando o "
                "conector sem encerrar a sessão (comportamento simplificado)."
            )
        return call_result.UnlockConnector(status=UnlockStatus.unlocked)

    @on(Action.data_transfer)
    async def on_data_transfer(self, vendor_id, message_id=None, data=None, **kwargs):
        """
        Extensão vendor-specific do OCPP. Só reconhece o próprio
        vendor_id (echo, confirma que o transporte funciona); qualquer
        outro recebe UnknownVendorId, como manda o spec.
        """
        self.log.info(
            f"[DATA TRANSFER] recebido | vendor_id={vendor_id} "
            f"message_id={message_id} data={data!r}"
        )
        if vendor_id != "EVChargerSim":
            return call_result.DataTransfer(status=DataTransferStatus.unknown_vendor_id)
        return call_result.DataTransfer(status=DataTransferStatus.accepted, data=data)

    @on(Action.get_diagnostics)
    async def on_get_diagnostics(self, location, **kwargs):
        """Simula o nome do arquivo e a sequência Uploading -> Uploaded, sem subir nada de verdade."""
        file_name = f"diagnostics_{self.config.charge_point_id}_{int(datetime.now(timezone.utc).timestamp())}.zip"
        self.log.info(f"[GET DIAGNOSTICS] location={location} | arquivo simulado: {file_name}")
        asyncio.create_task(self._simulate_diagnostics_upload())
        return call_result.GetDiagnostics(file_name=file_name)

    async def _simulate_diagnostics_upload(self):
        """
        queueable=False: não faz sentido enfileirar "Uploading" pra
        chegar depois de um "Uploaded" já enfileirado — quebraria a
        ordem. try/except na função inteira: task solta de vida curta,
        sem isso uma falha no meio viraria "Task exception was never
        retrieved" mudo.
        """
        try:
            await asyncio.sleep(1)
            await self._call_or_queue(
                call.DiagnosticsStatusNotification(status=DiagnosticsStatus.uploading),
                kind="DiagnosticsStatusNotification(Uploading)", queueable=False,
            )
            self.log.info("[DIAGNOSTICS] status: Uploading")
            await asyncio.sleep(2)
            await self._call_or_queue(
                call.DiagnosticsStatusNotification(status=DiagnosticsStatus.uploaded),
                kind="DiagnosticsStatusNotification(Uploaded)", queueable=False,
            )
            self.log.info("[DIAGNOSTICS] status: Uploaded")
        except Exception:
            self.log.exception("[DIAGNOSTICS] erro inesperado durante a simulação de upload.")

    @on(Action.update_firmware)
    async def on_update_firmware(self, location, retrieve_date, **kwargs):
        """
        CSMS mandando atualizar o firmware. Um update de firmware real
        interrompe qualquer sessão ativa (o charger reinicia no fim) —
        replicamos isso encerrando a transação antes da sequência de
        download/instalação, igual ao hard reset.
        """
        self.log.info(f"[UPDATE FIRMWARE] location={location} retrieve_date={retrieve_date}")
        asyncio.create_task(self._simulate_firmware_update())
        return call_result.UpdateFirmware()

    async def _simulate_firmware_update(self):
        """Mesmo cuidado do _simulate_diagnostics_upload: try/except na função inteira, notificações via _call_or_queue com queueable=False."""
        try:
            state = self.state
            if state.active_transaction_id is not None:
                self.log.warning(
                    f"[FIRMWARE] sessão ativa (tx={state.active_transaction_id}) será "
                    "encerrada — o firmware update vai reiniciar o charger."
                )
                await self._send_stop_transaction(
                    state.active_transaction_id, reason=Reason.other, skip_status_flow=True
                )

            for status, delay in (
                (FirmwareStatus.downloading, 1),
                (FirmwareStatus.downloaded, 1),
                (FirmwareStatus.installing, 1),
            ):
                await self._call_or_queue(
                    call.FirmwareStatusNotification(status=status),
                    kind=f"FirmwareStatusNotification({status.value})", queueable=False,
                )
                self.log.info(f"[FIRMWARE] status: {status.value}")
                await asyncio.sleep(delay)

            # Reboot simulado, mesma sequência do hard reset.
            await self.send_status_notification(ChargePointStatus.unavailable)
            await asyncio.sleep(3)
            await self.send_boot_notification()
            await asyncio.sleep(1)
            await self.send_status_notification(ChargePointStatus.available)

            await self._call_or_queue(
                call.FirmwareStatusNotification(status=FirmwareStatus.installed),
                kind="FirmwareStatusNotification(Installed)", queueable=False,
            )
            self.log.info("[FIRMWARE] status: Installed — atualização concluída")
        except Exception:
            self.log.exception("[FIRMWARE] erro inesperado durante a simulação de atualização.")

    @on(Action.reserve_now)
    async def on_reserve_now(
        self, connector_id, expiry_date, id_tag, reservation_id, parent_id_tag=None, **kwargs
    ):
        """
        Reserva o conector para um id_tag (ou grupo, via parent_id_tag)
        específico até expiry_date. Enquanto reservado, "start" local só
        aceita esse id_tag — ver console_command_loop.
        """
        state = self.state
        self.log.info(
            f"[RESERVE NOW] connector={connector_id} id_tag={id_tag} "
            f"reservation_id={reservation_id} expiry={expiry_date}"
        )

        if state.is_faulted:
            return call_result.ReserveNow(status=ReservationStatus.faulted)
        if state.active_transaction_id is not None or state.reservation_id is not None:
            self.log.warning(
                "[RESERVE NOW] conector já ocupado (sessão ativa ou já "
                "reservado) — rejeitando com Occupied."
            )
            return call_result.ReserveNow(status=ReservationStatus.occupied)

        state.reservation_id = reservation_id
        state.reserved_for_id_tag = id_tag
        state.reserved_parent_id_tag = parent_id_tag
        asyncio.create_task(self.send_status_notification(ChargePointStatus.reserved))
        asyncio.create_task(self._expire_reservation_at(reservation_id, expiry_date))
        return call_result.ReserveNow(status=ReservationStatus.accepted)

    async def _expire_reservation_at(self, reservation_id: int, expiry_date: str):
        """
        Limpa a reserva sozinha quando expiry_date passa, sem precisar de
        um CancelReservation explícito — replica o comportamento real de
        uma reserva não usada expirar e o conector voltar a Available.
        """
        try:
            expiry = datetime.fromisoformat(expiry_date.replace("Z", "+00:00"))
            delay = (expiry - datetime.now(timezone.utc)).total_seconds()
        except ValueError:
            self.log.warning(
                f"[RESERVE NOW] expiry_date inválido/não-ISO8601 ('{expiry_date}') — "
                "reserva não expira automaticamente, só via CancelReservation."
            )
            return

        if delay > 0:
            await asyncio.sleep(delay)

        state = self.state
        if state.reservation_id == reservation_id:
            self.log.info(f"[RESERVE NOW] reserva {reservation_id} expirou sem uso")
            state.reservation_id = None
            state.reserved_for_id_tag = None
            state.reserved_parent_id_tag = None
            if state.active_transaction_id is None and not state.is_faulted:
                await self.send_status_notification(ChargePointStatus.available)

    @on(Action.cancel_reservation)
    async def on_cancel_reservation(self, reservation_id, **kwargs):
        state = self.state
        self.log.info(f"[CANCEL RESERVATION] reservation_id={reservation_id}")
        if state.reservation_id != reservation_id:
            return call_result.CancelReservation(status=CancelReservationStatus.rejected)

        state.reservation_id = None
        state.reserved_for_id_tag = None
        state.reserved_parent_id_tag = None
        if state.active_transaction_id is None and not state.is_faulted:
            asyncio.create_task(self.send_status_notification(ChargePointStatus.available))
        return call_result.CancelReservation(status=CancelReservationStatus.accepted)

    @on(Action.get_local_list_version)
    async def on_get_local_list_version(self, **kwargs):
        self.log.debug(f"[GET LOCAL LIST VERSION] atual={self.state.local_list_version}")
        return call_result.GetLocalListVersion(list_version=self.state.local_list_version)

    @on(Action.send_local_list)
    async def on_send_local_list(
        self, list_version, update_type, local_authorization_list=None, **kwargs
    ):
        """
        Recebe (parte d)a lista local de autorização do CSMS. "Full"
        substitui a lista inteira; "Differential" aplica só as entradas
        enviadas (uma entrada sem id_tag_info remove aquele id_tag da
        lista — comportamento padrão OCPP 1.6 para remoção diferencial).
        """
        state = self.state
        entries = local_authorization_list or []

        if update_type == "Full":
            state.local_auth_list = {}

        for entry in entries:
            entry_id_tag = entry.get("id_tag")
            id_tag_info = entry.get("id_tag_info")
            if not entry_id_tag:
                continue
            if id_tag_info is None:
                state.local_auth_list.pop(entry_id_tag, None)
                continue
            state.local_auth_list[entry_id_tag] = id_tag_info.get("status", "Accepted")

        state.local_list_version = list_version
        self.log.info(
            f"[SEND LOCAL LIST] update_type={update_type} | "
            f"nova versão={list_version} | {len(state.local_auth_list)} id_tag(s) na lista"
        )
        return call_result.SendLocalList(status=UpdateStatus.accepted)

    @on(Action.clear_cache)
    async def on_clear_cache(self, **kwargs):
        """
        Limpa a Authorization Cache — conceitualmente separada da lista
        local de autorização (local_auth_list, gerida por SendLocalList/
        GetLocalListVersion, que ClearCache NÃO afeta).
        """
        n = len(self.state.auth_cache)
        self.state.auth_cache.clear()
        self.log.info(f"[CLEAR CACHE] authorization cache limpa ({n} entrada(s) removida(s)).")
        return call_result.ClearCache(status=ClearCacheStatus.accepted)

    # --------------------------------------------------------
    # Rotinas que o charge point envia PARA o CSMS
    # --------------------------------------------------------

    async def send_boot_notification(self) -> tuple[bool, float]:
        """
        Não reseta SoC/is_faulted aqui — a mesma instância persiste
        através de reconexões (ver main()), então isso apagaria uma
        sessão/falha real em andamento. queueable=False: offline já é
        tratado pelo laço de reconexão em main().

        Retorna (accepted, retry_after_seconds). Em Accepted, o campo
        `interval` da resposta é aplicado como o heartbeat interval
        (é o comportamento definido pelo protocolo — o CSMS usa
        BootNotification pra sincronizar isso, não só ChangeConfiguration).
        Em Pending/Rejected, o mesmo campo `interval` diz quanto esperar
        antes de tentar de novo; quem decide se/quantas vezes tentar de
        novo é o chamador (run_first_boot_sequence / run_reconnect_sequence).
        """
        request = call.BootNotification(
            charge_point_model="EVChargerSim",
            charge_point_vendor="EVChargerSim",
            firmware_version="SIM-1.0",
        )
        response = await self._call_or_queue(request, kind="BootNotification", queueable=False)
        if response is None:
            return False, 10.0
        if response.status == RegistrationStatus.accepted:
            if response.interval and response.interval > 0:
                self.state.current_heartbeat_interval = response.interval
                self.log.info(
                    f"BootNotification aceito pelo CSMS — heartbeat ajustado "
                    f"para {response.interval}s (definido pelo CSMS)."
                )
            else:
                self.log.info("BootNotification aceito pelo CSMS.")
            return True, 0.0
        retry_after = response.interval if response.interval and response.interval > 0 else 10.0
        self.log.warning(
            f"BootNotification respondido com status={response.status} — "
            f"CSMS ainda não aceitou o registro, nova tentativa em {retry_after:.0f}s."
        )
        return False, retry_after

    async def send_status_notification(self, status: str):
        request = call.StatusNotification(
            connector_id=self.config.connector_id,
            error_code=ChargePointErrorCode.no_error,
            status=status,
        )
        response = await self._call_or_queue(request, kind=f"StatusNotification({status})")
        if response is not None:
            self.log.info(f"StatusNotification enviado: {status}")

    @staticmethod
    def _start_transaction_authorized(response) -> bool:
        """
        True se a StartTransactionResponse veio com id_tag_info.status
        Accepted. Isso é ortogonal a "o CSMS respondeu" — um CSMS pode
        aceitar a chamada RPC (e devolver um transaction_id de verdade)
        e ainda assim recusar o id_tag (Invalid/Blocked/Expired/
        ConcurrentTx), caso em que um carregador real não entrega
        energia mesmo com a transação já registrada do lado do servidor.
        """
        status = (response.id_tag_info or {}).get("status", AuthorizationStatus.accepted)
        return status == AuthorizationStatus.accepted

    async def _send_start_transaction(self, connector_id: int, id_tag: str):
        """
        Envia StartTransaction simulando o carregador autorizando e
        fechando o contator. Offline (ou mensagem perdida por chaos), a
        sessão roda localmente do mesmo jeito, com um ID de transação
        temporário (negativo) até o CSMS confirmar um ID real no
        próximo flush da fila offline.
        """
        state = self.state
        try:
            # Evita que um agendamento de perfil pendente da sessão
            # anterior "acorde" no meio desta e pise na corrente aplicada.
            self._cancel_profile_task()

            # Reseta SoC/medidor pra não encadear com a sessão anterior.
            state.battery_soc_percent = self.config.initial_soc_percent
            state.energy_meter_wh = 0.0
            state.session_suspended = False
            self.log.info(f"[BATERIA] SoC inicial desta sessão: {state.battery_soc_percent:.1f}%")

            # Aplica a corrente padrão imediatamente, antes de qualquer
            # SetChargingProfile chegar — um carregador físico começa a
            # entregar corrente assim que o contator fecha, não fica em
            # 0A esperando o CSMS reagir. O CSMS ainda pode sobrescrever
            # isso a qualquer momento.
            state.current_offered_amps = self.config.default_offered_amps
            state.current_actual_amps = compute_actual_current(
                state.current_offered_amps, state.battery_soc_percent
            )
            self.log.info(
                f"[SESSION] Corrente inicial: {state.current_offered_amps:.0f}A oferecido "
                f"/ {state.current_actual_amps:.1f}A real (aguardando SetChargingProfile do CSMS)"
            )

            await self.send_status_notification(ChargePointStatus.preparing)
            await asyncio.sleep(1)  # simula o delay real de fechamento do contator

            # ID local reservado ANTES de tentar enviar — se a mensagem
            # for enfileirada por qualquer motivo, já temos um ID pronto.
            self._local_tx_counter -= 1
            local_id = self._local_tx_counter

            request = call.StartTransaction(
                connector_id=connector_id,
                id_tag=id_tag,
                meter_start=int(state.energy_meter_wh),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            # return_queued=True: precisamos saber SE foi enfileirada de
            # verdade (offline/chaos/conexão caiu — mensagem nunca saiu,
            # reenviar depois é seguro) ou se só deu timeout com a conexão
            # de pé (mensagem saiu, destino desconhecido — NÃO reenviar
            # sozinho, ver _call_or_queue).
            response, queued = await self._call_or_queue(
                request,
                kind="StartTransaction",
                queueable=True,
                return_queued=True,
                local_tx_id=local_id,
            )

            if response is not None:
                state.active_transaction_id = response.transaction_id
                if not self._start_transaction_authorized(response):
                    tag_status = response.id_tag_info.get("status")
                    self.log.warning(
                        f"[START TRANSACTION] CSMS registrou a transação "
                        f"(transaction_id={state.active_transaction_id}) mas "
                        f"id_tag_info.status={tag_status} — abortando sem "
                        "entregar energia, StopTransaction imediato."
                    )
                    asyncio.create_task(
                        self._send_stop_transaction(state.active_transaction_id, reason=Reason.other)
                    )
                    return
                self.log.info(
                    f"⚡ [START TRANSACTION] aceito pelo CSMS | "
                    f"transaction_id={state.active_transaction_id} | id_tag={id_tag}"
                )
            elif queued:
                # Realmente offline (ou chaos derrubou a mensagem, ou a
                # conexão caiu na tentativa) — nunca chegou no CSMS, então
                # reenviar no próximo flush é seguro e necessário.
                state.active_transaction_id = local_id
                self.log.warning(
                    f"[FILA OFFLINE] StartTransaction enfileirado — sessão "
                    f"rodando localmente com ID temporário {local_id} até "
                    "reconectar e confirmar com o CSMS."
                )
            else:
                # Timeout com a conexão de pé: a mensagem SAIU e não
                # sabemos se o CSMS processou. A sessão roda localmente
                # com o ID temporário (fisicamente o carro já está
                # carregando), mas DELIBERADAMENTE sem reenvio automático
                # — reenviar arriscaria uma transação duplicada do lado
                # do CSMS se ele só estivesse lento, não caído (é
                # exatamente esse cenário que gerou o bug original desta
                # correção). Limitação inerente do OCPP 1.6 (sem
                # idempotência) — não tem como o simulador resolver isso
                # sozinho sem arriscar duplicar; fica registrado como
                # ERROR porque merece atenção manual do operador.
                state.active_transaction_id = local_id
                self.log.error(
                    f"[START TRANSACTION] sem resposta do CSMS em "
                    f"{self.config.call_timeout_seconds}s (conexão segue "
                    f"online) — sessão rodando localmente com ID temporário "
                    f"{local_id}, SEM confirmação do CSMS e SEM reenvio "
                    "automático (evita duplicar a transação do lado dele). "
                    "Verifique manualmente se o CSMS registrou esta sessão."
                )

            # Sessão consome a reserva do conector, se houver uma.
            if state.reservation_id is not None:
                self.log.info(
                    f"[SESSION] reserva {state.reservation_id} consumida pelo início desta sessão"
                )
                state.reservation_id = None
                state.reserved_for_id_tag = None
                state.reserved_parent_id_tag = None

            if self._abort_pending_start_reason is not None:
                # on_reset chegou enquanto esta sessão ainda não tinha
                # active_transaction_id gravado — o guard normal dele não
                # viu nada pra parar na hora, então sinalizou aqui.
                # active_transaction_id já está resolvido agora (real ou
                # placeholder local), então dá pra encerrar de verdade.
                abort_reason = self._abort_pending_start_reason
                self._abort_pending_start_reason = None
                self.log.warning(
                    f"[START TRANSACTION] reset foi pedido enquanto esta "
                    f"sessão ainda não tinha confirmado — encerrando "
                    f"imediatamente (transaction_id={state.active_transaction_id})."
                )
                asyncio.create_task(
                    self._send_stop_transaction(state.active_transaction_id, reason=abort_reason)
                )
                return

            # O CSMS real já manda um SetChargingProfile logo após o
            # boot — não precisamos de um "chute" além do já aplicado acima.
            await self.send_status_notification(ChargePointStatus.charging)
        except Exception:
            # Cobre algo genuinamente imprevisto fora do fluxo já
            # tratado acima — sem isso, uma falha aqui morreria em
            # silêncio (task via create_task, ninguém dá await nela).
            self.log.exception(
                "[START TRANSACTION] erro inesperado — sessão pode não ter "
                "sido registrada corretamente."
            )
        finally:
            # A partir daqui active_transaction_id (real ou o ID local
            # temporário) já reflete o desfecho, e esse campo é quem passa
            # a bloquear um novo start — libera a reserva feita por
            # _try_begin_start em on_remote_start_transaction/console.
            self._end_start()

    async def _send_stop_transaction(
        self,
        transaction_id: int,
        reason=None,
        skip_status_flow: bool = False,
    ):
        """
        Envia StopTransaction encerrando a sessão no CSMS.

        reason: motivo OCPP do encerramento — ex: Reason.hard_reset/
        soft_reset quando é um Reset que interrompe a sessão.
        skip_status_flow: True pula Finishing->Available (usado pelo
        hard reset, que tem sua própria sequência de status).
        """
        state = self.state
        self._cancel_profile_task()

        # Para FISICAMENTE agora, mesmo que o CSMS ainda não saiba —
        # replica um charger real (abre o contator na hora, avisa o
        # servidor depois) e é o que torna a fila offline coerente: se
        # continuássemos "carregando" até a confirmação, um
        # StopTransaction enfileirado não faria sentido.
        local_id_being_stopped = transaction_id if transaction_id is not None and transaction_id < 0 else None
        state.active_transaction_id = None
        state.current_offered_amps = 0.0
        state.current_actual_amps = 0.0
        state.session_suspended = False
        state.evse_suspended_by_profile = False

        try:
            await asyncio.sleep(0.5)

            request = call.StopTransaction(
                meter_stop=int(state.energy_meter_wh),
                timestamp=datetime.now(timezone.utc).isoformat(),
                transaction_id=transaction_id,
                reason=reason,
            )
            # return_queued=True: só pra logar com precisão se isto foi
            # de fato salvo pra reenvio (offline/chaos/conexão caiu) ou
            # se só deu timeout com a conexão de pé — local_tx_id permite
            # ao flush corrigir a referência se o Start correspondente
            # também ainda não foi confirmado.
            response, queued = await self._call_or_queue(
                request,
                kind="StopTransaction",
                queueable=True,
                return_queued=True,
                local_tx_id=local_id_being_stopped,
            )
            if response is not None:
                self.log.info(
                    f"🛑 [STOP TRANSACTION] enviado | transaction_id={transaction_id}"
                    + (f" | motivo={reason.value}" if reason else "")
                )
            elif queued:
                self.log.warning(
                    f"[FILA OFFLINE] StopTransaction enfileirado "
                    f"(transaction_id={transaction_id}) — será entregue ao "
                    "CSMS na próxima reconexão."
                )
            else:
                # Timeout com a conexão de pé: a mensagem SAIU e não
                # sabemos se o CSMS processou — mas a sessão já parou de
                # verdade localmente (contator aberto no topo da função),
                # então NÃO reenviamos automaticamente pelo mesmo motivo
                # do StartTransaction: se o CSMS só estava lento (não
                # caído), reenviar arrisca ele processar duas vezes o
                # encerramento da mesma transação.
                self.log.error(
                    f"[STOP TRANSACTION] sem resposta do CSMS em "
                    f"{self.config.call_timeout_seconds}s (conexão segue "
                    f"online) — sessão já encerrada localmente (transaction_id="
                    f"{transaction_id}), mas SEM confirmação do CSMS e SEM "
                    "reenvio automático. Verifique manualmente se o CSMS "
                    "registrou o encerramento desta sessão."
                )

            if skip_status_flow:
                # reset/fault/firmware têm sua própria sequência final de
                # status — uma mudança de disponibilidade pendente não é
                # aplicada aqui pra não brigar com ela.
                if state.pending_availability_change is not None:
                    self.log.warning(
                        "[CHANGE AVAILABILITY] mudança para Inoperative estava "
                        "agendada, mas a sessão terminou via reset/fault/firmware "
                        "(sequência de status própria) — reenvie ChangeAvailability "
                        "se ainda quiser aplicá-la."
                    )
                    state.pending_availability_change = None
                return

            # Mudança pra Inoperative pedida durante a sessão só é
            # aplicada agora que ela terminou — ver on_change_availability.
            if state.pending_availability_change == "Inoperative":
                state.availability_status = "Inoperative"
                state.pending_availability_change = None
                self.log.info(
                    "[CHANGE AVAILABILITY] aplicando mudança para Inoperative "
                    "agendada, agora que a sessão terminou."
                )
                await self.send_status_notification(ChargePointStatus.unavailable)
                return

            # Finishing (conector liberando) -> Available — sem isso o
            # conector ficaria "preso" em Charging mesmo sem sessão.
            await self.send_status_notification(ChargePointStatus.finishing)
            await asyncio.sleep(2)
            await self.send_status_notification(ChargePointStatus.available)
        except Exception:
            # Estado local já foi limpo acima — o que fica pendente aqui
            # é só a sequência de status pós-stop, não a sessão em si.
            self.log.exception(
                "[STOP TRANSACTION] erro inesperado após a sessão já ter "
                "sido encerrada localmente."
            )

    async def energy_accumulator_loop(self, interval_seconds: int = 30):
        """
        Acumula energia (Wh) enquanto há transação ativa e não suspensa,
        avançando SoC e corrente (tapering) a cada ciclo. Ao atingir
        100%, manda StopTransaction automaticamente (EV sinalizando
        bateria cheia). config.simulation_speed multiplica o delta de
        energia por ciclo (não o intervalo real entre ciclos).

        Iniciado uma vez em main() e roda para sempre, independente de
        reconexões — continuar "carregando" fisicamente mesmo offline é
        o que torna a fila offline coerente. Cada ciclo tem seu próprio
        try/except pra um erro isolado não derrubar o loop de vez.
        """
        state = self.state
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                if state.active_transaction_id is None:
                    continue
                if state.session_suspended or state.current_actual_amps <= 0:
                    continue

                power_w = self.config.nominal_voltage * state.current_actual_amps
                energy_delta_wh = (
                    power_w * (interval_seconds / 3600) * self.config.simulation_speed
                )
                state.energy_meter_wh += energy_delta_wh

                state.battery_soc_percent = min(
                    100.0,
                    state.battery_soc_percent
                    + (energy_delta_wh / self.config.battery_capacity_wh) * 100,
                )
                state.current_actual_amps = compute_actual_current(
                    state.current_offered_amps, state.battery_soc_percent
                )

                if state.battery_soc_percent >= 100.0:
                    state.current_actual_amps = 0.0
                    self.log.info(
                        "[BATERIA] SoC atingiu 100% — EV sinalizou bateria cheia. "
                        "Encerrando sessão automaticamente (Reason.ev_disconnected)."
                    )
                    asyncio.create_task(
                        self._send_stop_transaction(
                            state.active_transaction_id, reason=Reason.ev_disconnected
                        )
                    )
            except Exception:
                self.log.exception(
                    "[BATERIA] erro inesperado no acumulador de energia — "
                    "continuando no próximo ciclo."
                )

    async def send_heartbeat_loop(self):
        """
        Intervalo relido a cada ciclo (state.current_heartbeat_interval)
        — uma mudança via ChangeConfiguration tem efeito no próximo
        ciclo. Roda para sempre; Heartbeat é queueable=False (reenviar
        um "atrasado" depois de reconectar não tem valor).
        """
        while True:
            try:
                response = await self._call_or_queue(
                    call.Heartbeat(), kind="Heartbeat", queueable=False
                )
                if response is not None:
                    # DEBUG: "ainda estou vivo" a cada ciclo, sem info
                    # nova — só aparece com --verbose.
                    self.log.debug(
                        f"Heartbeat enviado (intervalo atual: "
                        f"{self.state.current_heartbeat_interval}s)."
                    )
            except Exception:
                self.log.exception("[HEARTBEAT] erro inesperado — continuando no próximo ciclo.")
            await asyncio.sleep(self.state.current_heartbeat_interval)

    def _build_meter_values_request(self, voltage_now: float | None = None) -> "call.MeterValues":
        """
        Monta o payload de MeterValues a partir do estado atual —
        reutilizado por send_meter_values_loop e por
        on_trigger_message (amostra imediata). voltage_now opcional: o
        loop passa a leitura que já tirou, pra bater com a linha de log.
        """
        state = self.state
        if voltage_now is None:
            voltage_now = read_grid_voltage(self.config.nominal_voltage)
        return call.MeterValues(
            connector_id=self.config.connector_id,
            meter_value=[
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sampledValue": [
                        {
                            "value": str(state.current_actual_amps),
                            "context": "Sample.Periodic",
                            "measurand": "Current.Import",
                            "unit": "A",
                        },
                        {
                            "value": str(state.current_offered_amps),
                            "context": "Sample.Periodic",
                            "measurand": "Current.Offered",
                            "unit": "A",
                        },
                        {
                            "value": str(voltage_now),
                            "context": "Sample.Periodic",
                            "measurand": "Voltage",
                            "unit": "V",
                        },
                        {
                            "value": str(round(voltage_now * state.current_actual_amps, 1)),
                            "context": "Sample.Periodic",
                            "measurand": "Power.Active.Import",
                            "unit": "W",
                        },
                        {
                            "value": str(int(state.energy_meter_wh)),
                            "context": "Sample.Periodic",
                            "measurand": "Energy.Active.Import.Register",
                            "unit": "Wh",
                        },
                    ],
                }
            ],
        )

    def _record_history_sample(self, power_kw: float, energy_kwh: float):
        """
        Acrescenta uma amostra à janela de histórico deste charger — usada
        por GET /api/history/<id> pro gráfico expansível de cada card no
        painel web. Uma amostra por ciclo de MeterValues, COM ou SEM
        sessão ativa (os períodos ociosos também aparecem no gráfico,
        em vez de deixar buracos). Janela deslizante de tamanho fixo
        (_HISTORY_MAX_SAMPLES) — remove do início, não reseta a cada
        sessão nova, pra dar uma linha do tempo contínua do charger.
        """
        state = self.state
        state.history.append({
            "t": datetime.now(timezone.utc).timestamp(),
            "soc": round(state.battery_soc_percent, 1),
            "actual_amps": state.current_actual_amps,
            "offered_amps": state.current_offered_amps,
            "power_kw": power_kw,
        })
        overflow = len(state.history) - _HISTORY_MAX_SAMPLES
        if overflow > 0:
            del state.history[:overflow]

    async def send_meter_values_loop(self, interval_seconds: int = 30):
        """
        Manda MeterValues periodicamente com a corrente "real" simulada
        — o que aparece no dashboard. Roda para sempre; offline, cada
        amostra é enfileirada e entregue em ordem na reconexão.

        Também tenta esvaziar a fila a cada ciclo se já estiver online —
        cobre o caso de uma mensagem "perdida" só por chaos_drop_rate
        (sem disconnect real), que senão ficaria presa até a próxima
        queda de conexão de verdade.
        """
        state = self.state
        while True:
            try:
                if self.is_online and state.offline_queue:
                    await self._flush_offline_queue()

                voltage_now = read_grid_voltage(self.config.nominal_voltage)
                await self._call_or_queue(
                    self._build_meter_values_request(voltage_now), kind="MeterValues"
                )

                power_kw = round((voltage_now * state.current_actual_amps) / 1000, 2)
                energy_kwh = round(state.energy_meter_wh / 1000, 2)
                self._record_history_sample(power_kw, energy_kwh)

                has_session = state.active_transaction_id is not None
                suspended = state.session_suspended or state.evse_suspended_by_profile
                color = _meter_line_color(has_session, suspended, state.is_faulted, self.use_color)
                offline_marker = " 📡✗" if not self.is_online else ""
                reset = "\033[0m" if self.use_color else ""

                # DEBUG (só aparece com --verbose): igual ao Heartbeat,
                # esta linha se repete a cada ciclo (padrão 30s) por
                # charger e sozinha já dominava o terminal em modo
                # frota com vários chargers rodando ao mesmo tempo.
                # Eventos reais (start/stop/fault/etc.) continuam em
                # INFO nos handlers correspondentes — nada relevante se
                # perde ao silenciar só o "ainda estou aqui" periódico.
                if has_session:
                    self.log.debug(
                        f"{color}🔋 SoC {state.battery_soc_percent:5.1f}%  "
                        f"⚡ {state.current_actual_amps:4.1f}/{state.current_offered_amps:4.1f}A  "
                        f"{power_kw:5.2f}kW  Σ{energy_kwh:6.2f}kWh{offline_marker}{reset}"
                    )
                else:
                    self.log.debug(f"{color}🔋 sem sessão ativa{offline_marker}{reset}")
            except Exception:
                self.log.exception(
                    "[METER VALUES] erro inesperado — continuando no próximo ciclo."
                )

            await asyncio.sleep(interval_seconds)

    async def console_command_loop(self):
        """
        Lê comandos do terminal em background (via run_in_executor para não
        bloquear o event loop) e delega pra execute_command() — mesma lógica
        usada pelo painel web de controle em modo frota (ver
        ControlPanelHandler), pra não duplicar as regras de cada comando em
        dois lugares.
        """
        loop = asyncio.get_running_loop()
        self.log.info(
            "[CONSOLE] Pronto. Comandos: start <id_tag> | stop | pause | "
            "resume | fault <código> | clear | datatransfer | queue | "
            "disconnect | help"
        )
        # Prompt visível (">> ") em vez de input() sem marcador nenhum —
        # sem isso, era fácil perder de vista onde exatamente o terminal
        # esperava você digitar algo no meio do stream de heartbeats e
        # meter values rolando por cima.
        prompt = "\033[32m>> \033[0m" if self.use_color else ">> "
        while True:
            raw = await loop.run_in_executor(None, input, prompt)
            parts = raw.strip().split()
            if not parts:
                continue
            await self.execute_command(parts[0].lower(), parts[1:])

    async def execute_command(self, cmd: str, args: list) -> str:
        """
        Executa um comando de ação local (start/stop/pause/.../help) e
        retorna uma mensagem curta de resultado.

        Extraído do antigo corpo de console_command_loop pra ter UMA fonte
        de verdade compartilhada entre o console de texto (modo charger
        único) e o painel web de controle (modo frota, --fleet) — cada
        chamador decide o que fazer com o retorno (console de texto já viu
        tudo pelo self.log; o painel web usa o retorno como toast de
        feedback pro clique do usuário).
        """
        state = self.state
        parts = [cmd] + list(args)

        # ── start <id_tag> ──────────────────────────────────────────
        if cmd == "start":
            if state.active_transaction_id is not None:
                msg = "Já existe uma sessão ativa."
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            if state.is_faulted:
                msg = "Charger em Faulted — rode 'clear' antes de iniciar uma nova sessão."
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            if state.availability_status == "Inoperative":
                msg = "Conector Inoperative (ChangeAvailability do CSMS) — sessão não pode ser iniciada."
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            if not self._try_begin_start():
                msg = "já existe um início de sessão em andamento — aguarde confirmar antes de tentar de novo."
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            id_tag = parts[1] if len(parts) > 1 else "LOCAL_TAG"
            # Conector reservado: só o id_tag (ou parent_id_tag) da
            # reserva pode iniciar sessão — qualquer outro é recusado
            # sem nem chamar Authorize, igual a um charger físico
            # reservado recusando um RFID errado no totem.
            if state.reservation_id is not None and id_tag not in (
                state.reserved_for_id_tag, state.reserved_parent_id_tag
            ):
                self._end_start()
                msg = (
                    f"Conector reservado (reservation_id={state.reservation_id}) "
                    f"para outro id_tag — '{id_tag}' recusado."
                )
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            self.log.info(
                f"[CONSOLE] RFID local: autorizando id_tag='{id_tag}' ..."
            )
            asyncio.create_task(
                self._local_start_flow(self.config.connector_id, id_tag)
            )
            return f"Autorizando id_tag='{id_tag}'..."

        # ── stop ────────────────────────────────────────────────────
        elif cmd == "stop":
            if state.active_transaction_id is None:
                msg = "Nenhuma sessão ativa para encerrar."
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            self.log.info(
                f"[CONSOLE] Encerrando sessão pelo cliente "
                f"(tx={state.active_transaction_id})"
            )
            asyncio.create_task(
                self._send_stop_transaction(
                    state.active_transaction_id, reason=Reason.ev_disconnected
                )
            )
            return "Encerrando sessão..."

        # ── pause ───────────────────────────────────────────────────
        elif cmd == "pause":
            if state.active_transaction_id is None:
                msg = "Nenhuma sessão ativa para pausar."
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            if state.session_suspended:
                msg = "Sessão já está suspensa."
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            state.session_suspended = True
            self.log.info("⏸️  [CONSOLE] Carregamento pausado → SuspendedEV")
            asyncio.create_task(
                self.send_status_notification(ChargePointStatus.suspended_ev)
            )
            return "Carregamento pausado."

        # ── resume ──────────────────────────────────────────────────
        elif cmd == "resume":
            if state.active_transaction_id is None:
                msg = "Nenhuma sessão ativa para retomar."
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            if not state.session_suspended:
                msg = "Sessão não está suspensa."
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            state.session_suspended = False
            self.log.info("▶️  [CONSOLE] Carregamento retomado → Charging")
            asyncio.create_task(
                self.send_status_notification(ChargePointStatus.charging)
            )
            return "Carregamento retomado."

        # ── fault <código> ──────────────────────────────────────────
        elif cmd == "fault":
            code_str = parts[1].lower() if len(parts) > 1 else ""
            error_code = FAULT_CODE_MAP.get(code_str)
            if error_code is None:
                msg = (
                    f"Código de falha desconhecido: '{code_str}'. "
                    f"Válidos: {', '.join(FAULT_CODE_MAP)}"
                )
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            self.log.warning(
                f"[CONSOLE] Simulando falha: {error_code.value}"
            )
            asyncio.create_task(
                self._send_fault_notification(error_code)
            )
            return f"Simulando falha: {error_code.value}"

        # ── clear ───────────────────────────────────────────────────
        elif cmd == "clear":
            if not state.is_faulted:
                msg = "Nenhuma falha ativa para limpar."
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            asyncio.create_task(self._send_fault_clear())
            return "Limpando falha..."

        # ── datatransfer <vendor_id> [message_id] [data...] ─────────
        elif cmd == "datatransfer":
            if len(parts) < 2:
                msg = "Uso: datatransfer <vendor_id> [message_id] [data...]"
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            vendor_id = parts[1]
            message_id = parts[2] if len(parts) > 2 else None
            data = " ".join(parts[3:]) if len(parts) > 3 else None
            asyncio.create_task(
                self._send_data_transfer(vendor_id, message_id, data)
            )
            return f"DataTransfer enviado (vendor_id={vendor_id})."

        # ── queue — mostra o que está pendente na fila offline ──────
        elif cmd == "queue":
            n = len(state.offline_queue)
            if n == 0:
                self.log.info("[CONSOLE] fila offline vazia.")
                summary = "fila offline vazia"
            else:
                kinds = ", ".join(item["kind"] for item in state.offline_queue)
                self.log.info(f"[CONSOLE] fila offline com {n} mensagem(ns): {kinds}")
                summary = f"fila offline com {n} mensagem(ns): {kinds}"
            connectivity = "online" if self.is_online else "OFFLINE"
            self.log.info(f"[CONSOLE] conectividade: {connectivity}")
            return f"{summary} | conectividade: {connectivity}"

        # ── authcache — inspeciona a Authorization Cache local ──
        elif cmd == "authcache":
            enabled = state.auth_cache_enabled
            n = len(state.auth_cache)
            self.log.info(
                f"[CONSOLE] authorization cache: "
                f"{'habilitada' if enabled else 'DESABILITADA'} | {n} entrada(s)"
            )
            for tag, entry in state.auth_cache.items():
                expiry = entry.get("expiry_date") or "sem validade"
                self.log.info(f"[CONSOLE]   {tag} -> {entry.get('status')} (expira: {expiry})")
            return f"authorization cache: {'habilitada' if enabled else 'DESABILITADA'} | {n} entrada(s)"

        # ── locallist — inspeciona a lista local (SendLocalList) ──
        elif cmd == "locallist":
            n = len(state.local_auth_list)
            self.log.info(
                f"[CONSOLE] lista local (SendLocalList): versão="
                f"{state.local_list_version} | {n} entrada(s)"
            )
            for tag, status in state.local_auth_list.items():
                self.log.info(f"[CONSOLE]   {tag} -> {status}")
            return f"lista local: versão={state.local_list_version} | {n} entrada(s)"

        # ── disconnect — derruba a conexão de propósito (chaos manual) ──
        elif cmd == "disconnect":
            if not self.is_online or self._connection is None:
                msg = "já está offline."
                self.log.warning(f"[CONSOLE] {msg}")
                return msg
            self.log.warning("[CONSOLE] forçando desconexão manual (teste de rede)...")
            asyncio.create_task(self._connection.close())
            return "Desconectando..."

        elif cmd == "help":
            help_text = (
                "Comandos:\n"
                "  start <id_tag>   — RFID local (Authorize/lista local → StartTransaction)\n"
                "  stop             — cliente encerra sessão (ev_disconnected)\n"
                "  pause            — carro pausa carregamento (SuspendedEV)\n"
                "  resume           — carro retoma carregamento (Charging)\n"
                "  fault <código>   — simula falha de hardware (Faulted)\n"
                f"  códigos de fault: {', '.join(FAULT_CODE_MAP)}\n"
                "  clear            — limpa a falha ativa (volta a Available)\n"
                "  datatransfer <vendor_id> [message_id] [data]\n"
                "                   — envia DataTransfer para o CSMS\n"
                "  queue            — mostra a fila offline e o status de conectividade\n"
                "  authcache        — mostra o estado/entradas da authorization cache\n"
                "  locallist        — mostra a versão/entradas da lista local (SendLocalList)\n"
                "  disconnect       — derruba a conexão de propósito (teste de rede)\n"
                "  help             — esta mensagem\n"
                "\n"
                "  Reserva (ReserveNow/CancelReservation), lista local "
                "(SendLocalList) e a\n"
                "  authorization cache (Authorize.conf guardado, usado "
                "offline) são\n"
                "  controladas pelo CSMS — 'start' respeita todas automaticamente.\n"
                "  Offline, mensagens (StatusNotification/MeterValues/Start·StopTransaction)\n"
                "  são enfileiradas e reenviadas automaticamente ao reconectar."
            )
            self.log.info(f"[CONSOLE] {help_text}")
            return help_text
        elif cmd:
            msg = f"Comando desconhecido: '{cmd}'. Digite 'help'."
            self.log.warning(f"[CONSOLE] {msg}")
            return msg
        return ""

    def get_status_dict(self) -> dict:
        """
        Retrato do estado atual desta instância, pro painel web de
        controle (modo frota) e para eventual uso futuro (ex: um endpoint
        de health-check). Não inclui nada sensível a serialização (sem
        objetos asyncio, sem a conexão em si).
        """
        s = self.state
        if s.is_faulted:
            status = "faulted"
        elif s.active_transaction_id is not None:
            status = "suspended" if s.session_suspended else "charging"
        elif s.availability_status == "Inoperative":
            status = "inoperative"
        else:
            status = "available"
        return {
            "charge_point_id": self.id,
            "online": self.is_online,
            "status": status,
            "active_transaction_id": s.active_transaction_id,
            "session_suspended": s.session_suspended,
            "soc_percent": round(s.battery_soc_percent, 1),
            "energy_wh": round(s.energy_meter_wh, 1),
            "offered_amps": round(s.current_offered_amps, 1),
            "actual_amps": round(s.current_actual_amps, 1),
            "availability_status": s.availability_status,
            "reservation_id": s.reservation_id,
            "queue_len": len(s.offline_queue),
            # Valores ATUAIS de config (não do boot) — refletem qualquer
            # ajuste feito ao vivo via POST /api/chargers/<id>/chaos, ver
            # apply_chaos_overrides(). O painel usa isso pra pré-preencher
            # o formulário de chaos de cada card com o que está valendo
            # de fato agora, não com o que foi passado no --fleet/CLI.
            "chaos": {
                "chaos_disconnect_interval_seconds": self.config.chaos_disconnect_interval_seconds,
                "chaos_disconnect_jitter_seconds": self.config.chaos_disconnect_jitter_seconds,
                "chaos_latency_min_ms": self.config.chaos_latency_min_ms,
                "chaos_latency_max_ms": self.config.chaos_latency_max_ms,
                "chaos_drop_rate": self.config.chaos_drop_rate,
                "max_offline_queue_size": self.config.max_offline_queue_size,
            },
        }

    def get_history(self) -> list:
        """
        Cópia da janela de amostras deste charger (ver
        _record_history_sample) — consumida por GET /api/history/<id>
        pro gráfico expansível do painel web. Cópia, não a lista
        interna, pra quem chamar não conseguir mutar state.history por
        engano.
        """
        return list(self.state.history)

    async def apply_chaos_overrides(self, overrides: dict) -> str:
        """
        Ajusta parâmetros de chaos deste charger JÁ CONECTADO, em tempo
        real — usado por POST /api/chargers/<id>/chaos no painel web.
        Muda os campos direto em self.config, o MESMO objeto lido a
        cada chamada por _call_or_queue() (chaos_latency/drop_rate) e a
        cada ciclo por _chaos_disconnect_loop() (orchestrator.py) — o
        efeito é imediato, sem precisar remover/readicionar o charger.

        `async def` só por consistência com o resto da API do painel
        (chega via run_coroutine_threadsafe, como execute_command()) —
        não há nenhum await de verdade aqui dentro, é tudo síncrono.
        """
        unknown = set(overrides) - _CHAOS_FIELDS
        if unknown:
            raise ValueError(f"campo(s) inválido(s) para chaos: {', '.join(sorted(unknown))}")
        if not overrides:
            raise ValueError("nenhum campo de chaos informado")

        applied = []
        for key, raw_value in overrides.items():
            caster = int if key == "max_offline_queue_size" else float
            try:
                value = caster(raw_value)
            except (TypeError, ValueError):
                raise ValueError(f"valor inválido para '{key}': {raw_value!r}")
            if value < 0:
                raise ValueError(f"'{key}' não pode ser negativo")
            if key == "chaos_drop_rate" and value > 1.0:
                raise ValueError("chaos_drop_rate deve estar entre 0.0 e 1.0")
            setattr(self.config, key, value)
            applied.append(f"{key}={value}")

        self.log.warning(f"[CHAOS] ajustado ao vivo via painel: {', '.join(applied)}")
        return f"chaos atualizado: {', '.join(applied)}"

    def _cache_auth_result(self, id_tag: str, id_tag_info: dict):
        """
        Guarda o resultado de um Authorize.conf na Authorization Cache
        (se habilitada) — permite autorizar esse mesmo id_tag localmente
        numa próxima queda de conexão, sem depender do CSMS ter mandado
        esse id_tag via SendLocalList. Conceitualmente separada da lista
        local: aqui é o PRÓPRIO charger "lembrando" respostas passadas.
        """
        if not self.state.auth_cache_enabled:
            return
        self.state.auth_cache[id_tag] = {
            "status": id_tag_info.get("status", "Invalid"),
            "expiry_date": id_tag_info.get("expiry_date") or id_tag_info.get("expiryDate"),
            "parent_id_tag": id_tag_info.get("parent_id_tag") or id_tag_info.get("parentIdTag"),
        }

    def _lookup_auth_cache(self, id_tag: str) -> str | None:
        """
        Consulta a Authorization Cache pra um id_tag, respeitando
        expiryDate quando presente. Retorna None se não encontrado,
        expirado, ou se o cache está desabilitado — quem chamou trata
        isso como "sem informação disponível", não como Invalid.
        """
        if not self.state.auth_cache_enabled:
            return None
        entry = self.state.auth_cache.get(id_tag)
        if entry is None:
            return None
        expiry_date = entry.get("expiry_date")
        if expiry_date:
            try:
                expiry_dt = datetime.fromisoformat(str(expiry_date).replace("Z", "+00:00"))
                if datetime.now(timezone.utc) >= expiry_dt:
                    self.log.info(
                        f"[AUTH CACHE] entrada de id_tag='{id_tag}' expirada "
                        f"({expiry_date}) — descartando."
                    )
                    self.state.auth_cache.pop(id_tag, None)
                    return None
            except ValueError:
                pass  # expiryDate malformado — trata como sem expiração
        return entry.get("status")

    async def _local_start_flow(self, connector_id: int, id_tag: str):
        """
        Start local (RFID no totem) — diferente do RemoteStart, precisa
        autorizar o id_tag antes de iniciar. Ordem de resolução (igual a
        um charger real):
          1) lista local (SendLocalList) — sem round-trip nenhum;
          2) online — Authorize direto ao CSMS, sempre (é a fonte de
             verdade); a resposta também alimenta a Authorization Cache
             pra uso futuro offline;
          3) offline — cai pra Authorization Cache, se habilitada;
        sem nenhuma das três, recusa (Authorize precisa de resposta
        síncrona, não dá pra enfileirar).
        """
        try:
            local_status = self.state.local_auth_list.get(id_tag)
            if local_status is not None:
                status = local_status
                self.log.info(
                    f"[LOCAL START] id_tag='{id_tag}' encontrado na lista local "
                    f"(status={status}) — sem chamada Authorize ao CSMS."
                )
            elif self.is_online:
                auth_request = call.Authorize(id_tag=id_tag)
                auth_response = await self._call_or_queue(
                    auth_request, kind="Authorize", queueable=False
                )
                if auth_response is None:
                    self.log.warning(
                        f"[LOCAL START] Authorize para id_tag='{id_tag}' não "
                        "teve resposta a tempo. Sessão não iniciada."
                    )
                    return
                status = auth_response.id_tag_info.get("status", "Invalid")
                self._cache_auth_result(id_tag, auth_response.id_tag_info)
            else:
                cached_status = self._lookup_auth_cache(id_tag)
                if cached_status is None:
                    self.log.warning(
                        f"[LOCAL START] offline e id_tag='{id_tag}' não está "
                        "na lista local nem na authorization cache — não é "
                        "possível autorizar sem conexão. Sessão não iniciada."
                    )
                    return
                status = cached_status
                self.log.info(
                    f"[LOCAL START] offline — id_tag='{id_tag}' autorizado "
                    f"via authorization cache (status={status})."
                )

            if status != AuthorizationStatus.accepted:
                self.log.warning(
                    f"[LOCAL START] id_tag='{id_tag}' não autorizado "
                    f"(status={status}). Sessão não iniciada."
                )
                return

            self.log.info(
                f"[LOCAL START] id_tag='{id_tag}' autorizado → iniciando transação"
            )
            await self._send_start_transaction(connector_id, id_tag)
        except Exception:
            self.log.exception("[LOCAL START] Falha no fluxo de autorização local.")
        finally:
            # Cobre os returns antecipados acima (offline, Authorize sem
            # resposta, id_tag recusado) — nesses casos _send_start_transaction
            # nunca roda, então ninguém mais soltaria a reserva feita pelo
            # comando "start" no console antes de chamar esta função.
            # Chamar de novo depois de _send_start_transaction já ter
            # soltado é inofensivo (_end_start só zera a flag).
            self._end_start()

    async def _send_fault_notification(self, error_code: ChargePointErrorCode):
        """
        Envia StatusNotification com status Faulted e o error_code informado.
        Se havia sessão ativa, encerra com Reason.other — comportamento real:
        um carregador que falha não pode simplesmente continuar a sessão,
        então manda StopTransaction antes de reportar o fault.
        """
        state = self.state
        if state.active_transaction_id is not None:
            self.log.warning(
                f"[FAULT] Sessão ativa (tx={state.active_transaction_id}) será "
                "encerrada pelo fault antes de reportar o erro."
            )
            await self._send_stop_transaction(
                state.active_transaction_id,
                reason=Reason.other,
                skip_status_flow=True,
            )

        state.current_offered_amps = 0.0
        state.current_actual_amps = 0.0
        state.is_faulted = True

        request = call.StatusNotification(
            connector_id=self.config.connector_id,
            error_code=error_code,
            status=ChargePointStatus.faulted,
        )
        # Via _call_or_queue (não self.call direto): offline, um fault
        # nunca chegava ao CSMS nem na reconexão (nunca era enfileirado).
        response = await self._call_or_queue(request, kind="StatusNotification(Faulted)")
        if response is not None:
            self.log.warning(
                f"⚠️  [FAULT] StatusNotification enviado: Faulted / {error_code.value} "
                "— use 'clear' para voltar a Available."
            )

    async def _send_fault_clear(self):
        """Limpa uma falha simulada — StatusNotification(Available, no_error)."""
        self.state.is_faulted = False
        await self.send_status_notification(ChargePointStatus.available)
        self.log.info("✅ [FAULT] Falha limpa — charger voltou para Available")

    async def _send_data_transfer(self, vendor_id: str, message_id: str | None, data: str | None):
        """
        Envia um DataTransfer arbitrário do charger para o CSMS (comando
        'datatransfer' do console). queueable=False: é um comando
        interativo de debug, a resposta é o próprio propósito de rodá-lo
        — não faz sentido enfileirar pra entregar minutos depois, sem
        ninguém olhando o terminal esperando a resposta. Ainda assim
        passa por _call_or_queue (em vez de self.call direto) para
        ganhar o timeout: antes, um CSMS que nunca respondesse deixava
        este comando pendurado pra sempre, sem erro nem log nenhum.
        """
        request = call.DataTransfer(vendor_id=vendor_id, message_id=message_id, data=data)
        response = await self._call_or_queue(request, kind="DataTransfer", queueable=False)
        if response is not None:
            self.log.info(
                f"[DATA TRANSFER] enviado | vendor_id={vendor_id} → "
                f"resposta: status={response.status} data={response.data!r}"
            )

    async def run_first_boot_sequence(self):
        """
        Primeira conexão: fica em Available até um RemoteStart/"start"
        local — _send_start_transaction avança pra Charging depois.

        Só avança para StatusNotification depois de um BootNotification
        Accepted — em Pending/Rejected um charger real não se apresenta
        como disponível, só fica retentando no intervalo que o CSMS mandou.
        """
        if not await self._boot_until_accepted():
            return  # ficou offline no meio das tentativas; main() reconecta e chama de novo
        self._boot_confirmed = True
        await asyncio.sleep(1)
        await self.send_status_notification(ChargePointStatus.available)

    async def _boot_until_accepted(self) -> bool:
        """
        Repete BootNotification até Accepted, esperando entre tentativas
        o `interval` que o próprio CSMS mandou na resposta (fallback 10s
        se o CSMS não mandar nada útil). Para de tentar se a conexão cair
        no meio — quem trata a reconexão é o laço em main(), que chama
        run_reconnect_sequence (e portanto isso de novo) quando voltar.
        """
        while True:
            accepted, retry_after = await self.send_boot_notification()
            if accepted:
                return True
            if not self.is_online:
                return False
            await asyncio.sleep(retry_after)

    async def run_reconnect_sequence(self):
        """
        Reconexão da mesma instância (com todo o estado acumulado) após
        uma queda de TRANSPORTE (WebSocket/rede) — NÃO reenvia
        BootNotification, só esvazia a fila offline e informa o status
        atual do conector, que pode não ser Available se uma sessão
        continuou rodando durante a queda.

        Antes esta função reenviava BootNotification em toda reconexão,
        igual ao boot inicial. Na prática isso confunde CSMS que tratam
        BootNotification como "este charge point acabou de (re)iniciar
        fisicamente" e, como efeito colateral, esquecem qualquer
        transação em andamento associada a esse charge_point_id — mesmo
        sem nenhum StopTransaction ter sido enviado. Pela spec OCPP 1.6,
        BootNotification comunica o BOOT do dispositivo (reset físico,
        perda de estado); uma queda de WebSocket é só um blip de
        transporte — a transação, se houver, é a mesma de antes, e o
        CSMS deve correlacioná-la pelo transaction_id (que ele mesmo
        atribuiu em StartTransaction.conf), não pela conexão TCP em si.
        BootNotification continua sendo enviado uma única vez, no boot
        de verdade do processo — ver run_first_boot_sequence.
        """
        if not self._boot_confirmed:
            # A conexão caiu no meio das tentativas do boot INICIAL,
            # antes de qualquer Accepted (ver _boot_until_accepted) —
            # main() já trata isso como "reconexão" (cp não é mais None
            # depois da 1ª tentativa), mas do lado do CSMS ainda não
            # existe registro nenhum pra resincronizar. Precisa
            # continuar tentando o boot, não pular direto pro resync
            # abaixo.
            await self.run_first_boot_sequence()
            return

        self.log.info(
            "[RECONEXÃO] esvaziando fila offline e resincronizando status "
            "(sem reenviar BootNotification — a transação, se houver, "
            "continua a mesma de antes da queda)..."
        )
        await self._flush_offline_queue()

        state = self.state
        if state.active_transaction_id is not None:
            await self.send_status_notification(ChargePointStatus.charging)
        elif state.is_faulted:
            await self.send_status_notification(ChargePointStatus.faulted)
        elif state.availability_status == "Inoperative":
            await self.send_status_notification(ChargePointStatus.unavailable)
        else:
            await self.send_status_notification(ChargePointStatus.available)
