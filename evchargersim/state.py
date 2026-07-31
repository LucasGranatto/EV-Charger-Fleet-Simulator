"""
evchargersim.state — ChargerState: estado de sessão/runtime de UM charge
point simulado (muda a cada mensagem, ao contrário de SimConfig que é
fixo após o boot).
"""

from dataclasses import dataclass, field

@dataclass
class ChargerState:
    """
    Estado de sessão/runtime de UM charge point simulado — muda ao
    longo da execução (diferente de SimConfig, fixo após o boot). Cada
    EVChargerSim tem seu próprio `self.state`, evitando que múltiplas
    instâncias no mesmo processo pisem umas nas outras.
    """
    current_offered_amps: float = 0.0  # limite vindo do CSMS (SetChargingProfile)
    current_actual_amps: float = 0.0   # o que o "carro" simula puxar de fato

    active_transaction_id: int | None = None
    energy_meter_wh: float = 0.0  # contador de energia acumulada (Wh)

    # Relido a cada ciclo por send_heartbeat_loop — uma mudança via
    # ChangeConfiguration(HeartbeatInterval) tem efeito imediato.
    current_heartbeat_interval: int = 120

    battery_soc_percent: float = 20.0

    session_suspended: bool = False        # True em SuspendedEV (pausa do carro)
    evse_suspended_by_profile: bool = False  # True em SuspendedEVSE (0A imposto pelo CSMS)

    # True entre "fault" e "clear" — console recusa "start" até limpar,
    # espelhando um charger real que não sai de Faulted sozinho.
    is_faulted: bool = False

    # ── Reserva (ReserveNow/CancelReservation): "start" local só aceita
    # o id_tag (ou parent_id_tag) reservado enquanto reservation_id != None.
    reservation_id: int | None = None
    reserved_for_id_tag: str | None = None
    reserved_parent_id_tag: str | None = None

    # ── Lista local de autorização (SendLocalList): id_tag -> status.
    # Se presente, o start local usa esse status sem chamar Authorize.
    local_auth_list: dict = field(default_factory=dict)
    local_list_version: int = 0

    # ── Authorization Cache: conceitualmente separada do LocalList acima
    # — aqui é o PRÓPRIO charger guardando id_tag_info de Authorize.conf
    # já vistos, pra sobreviver offline sem depender do CSMS ter mandado
    # SendLocalList. Chave: id_tag -> {"status", "expiry_date", "parent_id_tag"}.
    # Controlado por ChangeConfiguration(AuthorizationCacheEnabled).
    auth_cache: dict = field(default_factory=dict)
    auth_cache_enabled: bool = True

    # ── Disponibilidade (ChangeAvailability): "Operative"/"Inoperative".
    availability_status: str = "Operative"
    # Mudança p/ Inoperative pedida DURANTE sessão ativa: fica pendente
    # (resposta "Scheduled") até a sessão terminar — ver spec OCPP.
    pending_availability_change: str | None = None

    # ── Histórico de amostras (SoC/corrente/potência), usado pelo
    # gráfico expansível de cada card no painel web (GET /api/history/
    # <id>) — ver EVChargerSim._record_history_sample(). Janela
    # deslizante de tamanho fixo (não todo o histórico da execução);
    # não persistido entre reinícios, como o resto deste estado.
    history: list = field(default_factory=list)

    # ── Fila de mensagens não entregues (offline ou chaos), reenviadas
    # em ordem na reconexão — ver _call_or_queue / _flush_offline_queue.
    # Item: {"kind": str, "request": call.X, "local_tx_id": int|None}.
    offline_queue: list = field(default_factory=list)


