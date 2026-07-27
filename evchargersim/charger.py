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
        """Acrescenta uma mensagem à fila offline, pra reenvio na próxima reconexão."""
        self.state.offline_queue.append(
            {"kind": kind, "request": request, "local_tx_id": local_tx_id}
        )
        self.log.info(
            f"[FILA OFFLINE] '{kind}' enfileirado "
            f"(fila agora com {len(self.state.offline_queue)} mensagem(ns))."
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
                self.log.warning(
                    f"[CSMS] '{kind}' não teve resposta em {timeout}s (orçamento "
                    "consumido por latência simulada — chaos_latency)."
                )
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
                    state.offline_queue = queue[i:]  # este item + os que nem tentamos
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

    @on(Action.trigger_message)
    async def on_trigger_message(self, requested_message, connector_id=None, **kwargs):
        """
        TriggerMessage pede para o carregador reenviar uma mensagem
        espontaneamente (ex: StatusNotification, Heartbeat). Usado pelo
        status_check() do CSMS real para forçar uma atualização de estado.
        """
        self.log.info(f"[TRIGGER MESSAGE] requested={requested_message} connector={connector_id}")
        if requested_message == "StatusNotification":
            current_status = (
                ChargePointStatus.charging if self.state.active_transaction_id is not None
                else ChargePointStatus.available
            )
            asyncio.create_task(self.send_status_notification(current_status))
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
