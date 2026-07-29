const FAULT_CODES = ["ground_failure", "over_current_failure", "over_voltage",
  "connector_lock_failure", "power_meter_failure", "weak_signal", "other_error"];

const STATUS_LABEL = {
  charging: "Carregando", suspended: "Suspenso", available: "Disponível",
  faulted: "Falha", inoperative: "Inoperativo",
};

// Elementos de card já criados, por charge_point_id — o coração da
// renderização por diff. Uma vez que o card de um charger existe, ele
// NUNCA é destruído/recriado enquanto esse charger continuar na lista;
// só os pedaços de texto/classe que mudaram são atualizados. Isso
// elimina de vez a classe de bug que já apareceu duas vezes aqui
// (campo de id_tag/seleção de fault sendo resetado ou perdendo o foco
// a cada poll de 1.5s) — o <input>/<select> em si nunca são recriados,
// então o navegador nunca tem motivo pra tirar o foco ou o cursor deles.
const cardElements = new Map();

// Valor atual de cada campo de id_tag, indexado por charger — só
// existe pra dar um valor inicial ao criar um card pela 1ª vez; depois
// disso o próprio <input> é a fonte da verdade (nunca é recriado).
const idTagInputs = {};
const faultSelections = {};

let currentFilter = "";

function escapeHtml(value) {
  return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/"/g, "&quot;");
}

// ── Toasts empilháveis ──────────────────────────────────────────────

function toast(message, kind = "info") {
  const stack = document.getElementById("toast-stack");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  stack.appendChild(el);
  const raf = window.requestAnimationFrame || ((cb) => setTimeout(cb, 0));
  raf(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 250);
  }, 3200);
}

// ── Modal de confirmação (substitui confirm() nativo) ───────────────

function confirmDialog(message) {
  const overlay = document.getElementById("confirm-overlay");
  const title = document.getElementById("confirm-title");
  const okBtn = document.getElementById("confirm-ok");
  const cancelBtn = document.getElementById("confirm-cancel");
  title.textContent = message;
  overlay.hidden = false;
  okBtn.focus();

  return new Promise((resolve) => {
    function cleanup(result) {
      overlay.hidden = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("keydown", onKeydown);
      resolve(result);
    }
    function onOk() { cleanup(true); }
    function onCancel() { cleanup(false); }
    function onKeydown(e) {
      if (e.key === "Escape") cleanup(false);
      if (e.key === "Enter") cleanup(true);
    }
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("keydown", onKeydown);
  });
}

// ── Ações (comandos, adicionar, remover) ────────────────────────────

async function sendCommand(chargeId, cmd, args) {
  try {
    const res = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ charge_point_id: chargeId, cmd, args: args || [] }),
    });
    const data = await res.json();
    toast(`[${chargeId}] ${data.message || (data.ok ? "ok" : "erro")}`, data.ok ? "success" : "error");
  } catch (e) {
    toast(`[${chargeId}] falha ao enviar comando: ${e}`, "error");
  }
  // Sem refresh() manual aqui — a mudança de estado chega sozinha pelo
  // stream de /api/events assim que o backend processar o comando.
}

async function addOneCharger(chargeId) {
  const res = await fetch("/api/chargers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ charge_point_id: chargeId }),
  });
  return res.json();
}

// Aceita tanto um ID único quanto uma lista separada por vírgula
// ("CH01, CH02, CH03") — dispara um POST por ID e resume o resultado
// num único toast, em vez de exigir que o usuário adicione um de cada vez.
async function addCharger() {
  const input = document.getElementById("new-charger-id");
  const ids = input.value.split(",").map((s) => s.trim()).filter(Boolean);
  if (ids.length === 0) {
    toast("Digite ao menos um ID antes de adicionar.", "error");
    return;
  }

  const results = await Promise.all(ids.map((id) =>
    addOneCharger(id).catch((e) => ({ ok: false, message: `${id}: ${e}` }))
  ));
  const okCount = results.filter((r) => r.ok).length;
  const failed = results.filter((r) => !r.ok).map((r) => r.message);

  if (ids.length === 1) {
    toast(results[0].message, results[0].ok ? "success" : "error");
  } else if (failed.length === 0) {
    toast(`${okCount} chargers adicionados.`, "success");
  } else {
    toast(`${okCount} adicionado(s), ${failed.length} falharam: ${failed.join(" · ")}`, "error");
  }
  if (okCount > 0) input.value = "";
  // Sem refresh() manual — /api/events mostra o(s) novo(s) charger(s)
  // assim que ele(s) conectar(em) de verdade.
}

async function removeCharger(chargeId) {
  const confirmed = await confirmDialog(
    `Remover o charger "${chargeId}"? A sessão/conexão atual dele será encerrada.`
  );
  if (!confirmed) return;
  try {
    const res = await fetch(`/api/chargers/${encodeURIComponent(chargeId)}`, { method: "DELETE" });
    const data = await res.json();
    toast(data.message || (data.ok ? "ok" : "erro"), data.ok ? "success" : "error");
    delete idTagInputs[chargeId];
    delete faultSelections[chargeId];
  } catch (e) {
    toast(`[${chargeId}] falha ao remover: ${e}`, "error");
  }
  // Sem refresh() manual — /api/events remove o card sozinho assim que
  // o backend tirar o charger do registry.
}

// Dispara um comando em TODOS os chargers registrados de uma vez
// (botões "Start"/"Stop"/"Pausar"/"Retomar"/"Desconectar" da
// bulk-actions-row). O backend (/api/command/all) já isola falha de um
// charger sem derrubar os demais — aqui só resume o resultado num
// único toast em vez de um por charger, pra não inundar a tela com N
// toasts de uma vez.
async function sendBulkCommand(cmd, { args = [], confirmMessage } = {}) {
  if (confirmMessage) {
    const confirmed = await confirmDialog(confirmMessage);
    if (!confirmed) return;
  }
  try {
    const res = await fetch("/api/command/all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cmd, args }),
    });
    const data = await res.json();
    toast(data.message || (data.ok ? "ok" : "erro"), data.ok ? "success" : "error");
  } catch (e) {
    toast(`Falha ao enviar comando em massa: ${e}`, "error");
  }
  // Sem refresh() manual — /api/events reflete o efeito em todos os
  // chargers afetados assim que o backend processar cada um.
}

// ── Construção/atualização de cards (diff, não innerHTML) ───────────

function displayStatus(c) {
  return c.online ? c.status : "offline";
}

function buildFaultOptions(selected) {
  return FAULT_CODES.map((f) =>
    `<option value="${f}" ${f === selected ? "selected" : ""}>${f}</option>`
  ).join("");
}

function createCard(c) {
  const el = document.createElement("div");
  el.className = "card";
  el.id = `card-${c.charge_point_id}`;

  const initialTag = idTagInputs[c.charge_point_id] ?? "LOCAL_TAG";
  const initialFault = faultSelections[c.charge_point_id] ?? FAULT_CODES[0];

  el.innerHTML = `
    <div class="card-top">
      <div class="id-block">
        <span class="led" data-role="led"></span>
        <span class="cp-id">${escapeHtml(c.charge_point_id)}</span>
      </div>
      <span class="pill" data-role="pill"></span>
    </div>
    <div class="gauge-row">
      <div class="gauge" data-role="gauge">
        ${Array.from({ length: 10 }, () => '<div class="gauge-cell"></div>').join("")}
      </div>
      <span class="gauge-value" data-role="soc-value"></span>
    </div>
    <div class="telemetry">
      <div class="telemetry-item">
        <span class="telemetry-label">Energia</span>
        <span class="telemetry-value" data-role="energy"></span>
      </div>
      <div class="telemetry-item">
        <span class="telemetry-label">Corrente</span>
        <span class="telemetry-value" data-role="current"></span>
      </div>
      <div class="telemetry-item">
        <span class="telemetry-label">Fila offline</span>
        <span class="telemetry-value" data-role="queue"></span>
      </div>
    </div>
    <div class="control-strip">
      <div class="start-row">
        <input type="text" data-role="id-tag" placeholder="id_tag" value="${escapeAttr(initialTag)}">
        <button data-role="btn-start">Start</button>
      </div>
      <div class="btn-row">
        <button data-role="btn-stop">Stop</button>
        <button data-role="btn-pause">Pause</button>
        <button data-role="btn-resume">Resume</button>
      </div>
      <div class="btn-row">
        <select data-role="fault-select">${buildFaultOptions(initialFault)}</select>
        <button data-role="btn-fault">Fault</button>
        <button data-role="btn-clear">Clear</button>
      </div>
      <div class="btn-row">
        <button data-role="btn-disconnect">Disconnect</button>
        <button class="danger" data-role="btn-remove">Remover</button>
      </div>
    </div>`;

  const chargeId = c.charge_point_id;
  const idTagInput = el.querySelector('[data-role="id-tag"]');
  const faultSelect = el.querySelector('[data-role="fault-select"]');
  idTagInput.addEventListener("input", () => { idTagInputs[chargeId] = idTagInput.value; });
  faultSelect.addEventListener("change", () => { faultSelections[chargeId] = faultSelect.value; });

  el.querySelector('[data-role="btn-start"]').addEventListener("click", () =>
    sendCommand(chargeId, "start", [idTagInput.value]));
  el.querySelector('[data-role="btn-stop"]').addEventListener("click", () =>
    sendCommand(chargeId, "stop", []));
  el.querySelector('[data-role="btn-pause"]').addEventListener("click", () =>
    sendCommand(chargeId, "pause", []));
  el.querySelector('[data-role="btn-resume"]').addEventListener("click", () =>
    sendCommand(chargeId, "resume", []));
  el.querySelector('[data-role="btn-fault"]').addEventListener("click", () =>
    sendCommand(chargeId, "fault", [faultSelect.value]));
  el.querySelector('[data-role="btn-clear"]').addEventListener("click", () =>
    sendCommand(chargeId, "clear", []));
  el.querySelector('[data-role="btn-disconnect"]').addEventListener("click", () =>
    sendCommand(chargeId, "disconnect", []));
  el.querySelector('[data-role="btn-remove"]').addEventListener("click", () =>
    removeCharger(chargeId));

  updateCard(el, c);
  return el;
}

function updateCard(el, c) {
  const status = displayStatus(c);
  const hasTx = c.active_transaction_id !== null;

  el.dataset.status = status;

  const led = el.querySelector('[data-role="led"]');
  led.className = `led ${status}`;

  const pill = el.querySelector('[data-role="pill"]');
  pill.className = `pill ${status}`;
  pill.textContent = c.online ? (STATUS_LABEL[c.status] || c.status) : "Offline";

  const gauge = el.querySelector('[data-role="gauge"]');
  const cells = gauge.children;
  const filledCount = Math.round((c.soc_percent / 100) * 10);
  for (let i = 0; i < cells.length; i++) {
    const filled = i < filledCount;
    cells[i].className = "gauge-cell" + (filled ? " filled" : "") + (filled && status === "charging" ? " charging" : "");
  }
  el.querySelector('[data-role="soc-value"]').textContent = `${c.soc_percent}%`;

  el.querySelector('[data-role="energy"]').textContent = `${(c.energy_wh / 1000).toFixed(2)} kWh`;
  el.querySelector('[data-role="current"]').textContent = `${c.actual_amps}A / ${c.offered_amps}A`;
  el.querySelector('[data-role="queue"]').textContent = String(c.queue_len);

  el.querySelector('[data-role="btn-start"]').disabled = hasTx;
  el.querySelector('[data-role="btn-stop"]').disabled = !hasTx;
  el.querySelector('[data-role="btn-pause"]').disabled = !(hasTx && !c.session_suspended);
  el.querySelector('[data-role="btn-resume"]').disabled = !(hasTx && c.session_suspended);
  el.querySelector('[data-role="btn-clear"]').disabled = status !== "faulted";
  el.querySelector('[data-role="btn-disconnect"]').disabled = !c.online;

  const matchesFilter = !currentFilter || c.charge_point_id.toLowerCase().includes(currentFilter);
  el.style.display = matchesFilter ? "" : "none";
}

function renderEmptyState(hasFilterButNoMatch) {
  const grid = document.getElementById("grid");
  cardElements.clear();
  grid.innerHTML = hasFilterButNoMatch
    ? `<div class="empty"><strong>Nenhum charger corresponde ao filtro.</strong><span>Tente outro termo de busca.</span></div>`
    : `<div class="empty"><strong>Nenhum charger conectado.</strong><span>Digite um ID acima e clique em "+ Adicionar" para começar a simular.</span></div>`;
}

function updateStatsStrip(chargers) {
  const strip = document.getElementById("stats-strip");
  const total = chargers.length;
  const online = chargers.filter((c) => c.online).length;
  const charging = chargers.filter((c) => c.online && c.status === "charging").length;
  const faulted = chargers.filter((c) => c.online && c.status === "faulted").length;

  strip.innerHTML = `
    <div class="stat"><span class="stat-value">${total}</span><span class="stat-label">Total</span></div>
    <div class="stat stat-online"><span class="stat-value">${online}</span><span class="stat-label">Online</span></div>
    <div class="stat stat-charging"><span class="stat-value">${charging}</span><span class="stat-label">Carregando</span></div>
    <div class="stat stat-faulted"><span class="stat-value">${faulted}</span><span class="stat-label">Falha</span></div>`;
}

function syncGrid(chargers) {
  updateStatsStrip(chargers);

  const grid = document.getElementById("grid");
  if (chargers.length === 0) {
    renderEmptyState(false);
    return;
  }
  if (grid.querySelector(".empty")) {
    grid.innerHTML = "";
  }

  const sorted = [...chargers].sort((a, b) => a.charge_point_id.localeCompare(b.charge_point_id));
  const seen = new Set();
  let anyVisible = false;

  sorted.forEach((c, index) => {
    seen.add(c.charge_point_id);
    let el = cardElements.get(c.charge_point_id);
    if (!el) {
      el = createCard(c);
      cardElements.set(c.charge_point_id, el);
    } else {
      updateCard(el, c);
    }
    if (el.style.display !== "none") anyVisible = true;

    const nodeAtIndex = grid.children[index];
    if (nodeAtIndex !== el) {
      grid.insertBefore(el, nodeAtIndex || null);
    }
  });

  for (const [id, el] of cardElements) {
    if (!seen.has(id)) {
      el.remove();
      cardElements.delete(id);
    }
  }

  if (!anyVisible && currentFilter) {
    renderEmptyState(true);
  }
}

async function refresh() {
  try {
    const res = await fetch("/api/state");
    const chargers = await res.json();
    syncGrid(chargers);
  } catch (e) {
    // silencioso — a conexão SSE (connectEventStream) é quem mantém a
    // tela atualizada de verdade; esta função só serve pra 1ª pintura.
  }
}

// ── Server-Sent Events (substitui o polling de 1.5s) ────────────────
// /api/events mantém uma conexão aberta e só empurra um novo snapshot
// quando algo de fato muda no backend — ver _handle_sse em
// control_panel.py. EventSource reconecta sozinho (com backoff nativo
// do próprio browser) se a conexão cair, então não precisa de nenhuma
// lógica de retry manual aqui.
let eventSource = null;

function setSseIndicator(connected) {
  const dot = document.getElementById("sse-dot");
  if (!dot) return;
  dot.classList.toggle("connected", connected);
  dot.title = connected ? "Atualização ao vivo (SSE conectado)" : "Reconectando ao painel...";
}

function connectEventStream() {
  eventSource = new EventSource("/api/events");
  eventSource.onopen = () => setSseIndicator(true);
  eventSource.onmessage = (event) => {
    setSseIndicator(true);
    try {
      syncGrid(JSON.parse(event.data));
    } catch (e) {
      // payload malformado — ignora este evento, o próximo corrige o estado
    }
  };
  eventSource.onerror = () => {
    // O browser já entra em modo "connecting" e tenta de novo sozinho;
    // só refletimos isso visualmente enquanto isso não volta.
    setSseIndicator(false);
  };
}

document.getElementById("add-charger-btn").addEventListener("click", addCharger);
document.getElementById("new-charger-id").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addCharger();
});
document.getElementById("search-input").addEventListener("input", (e) => {
  currentFilter = e.target.value.trim().toLowerCase();
  for (const [id, el] of cardElements) {
    el.style.display = (!currentFilter || id.toLowerCase().includes(currentFilter)) ? "" : "none";
  }
});

document.querySelector('[data-role="bulk-start"]').addEventListener("click", () => {
  const idTag = document.getElementById("bulk-id-tag").value.trim() || "LOCAL_TAG";
  sendBulkCommand("start", { args: [idTag] });
});
document.querySelector('[data-role="bulk-stop"]').addEventListener("click", () =>
  sendBulkCommand("stop"));
document.querySelector('[data-role="bulk-pause"]').addEventListener("click", () =>
  sendBulkCommand("pause"));
document.querySelector('[data-role="bulk-resume"]').addEventListener("click", () =>
  sendBulkCommand("resume"));
document.querySelector('[data-role="bulk-disconnect"]').addEventListener("click", () =>
  sendBulkCommand("disconnect", {
    confirmMessage: "Desconectar TODOS os chargers? Cada um reconecta sozinho em seguida, mas as conexões atuais serão encerradas agora.",
  }));

refresh();            // 1ª pintura imediata, antes do stream conectar
connectEventStream(); // daqui pra frente, toda atualização vem por push
