const FAULT_CODES = ["ground_failure", "over_current_failure", "over_voltage",
  "connector_lock_failure", "power_meter_failure", "weak_signal", "other_error"];

const STATUS_LABEL = {
  charging: "Carregando", suspended: "Suspenso", available: "Disponível",
  faulted: "Falha", inoperative: "Inoperativo",
};

// Ordem de prioridade quando currentSort === "status" — falha primeiro
// (o que mais precisa de atenção), offline por último.
const STATUS_SORT_RANK = { faulted: 0, charging: 1, suspended: 2, available: 3, inoperative: 4 };

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
let currentSort = "id";
// Último snapshot recebido (via SSE ou refresh()) — guardado pra poder
// reordenar (mudar currentSort) instantaneamente, sem esperar o
// próximo evento do stream.
let lastChargers = [];

function escapeHtml(value) {
  return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/"/g, "&quot;");
}

// ── Autenticação (--control-token, opcional) ────────────────────────
// Token guardado em localStorage — este é um app real de verdade
// rodando no navegador do usuário (não um Artifact do Claude), então
// persistir entre reloads é o comportamento certo aqui. Vazio = painel
// sem autenticação (comportamento padrão, --control-token não usado).
const TOKEN_STORAGE_KEY = "evchargersim_control_token";

function getControlToken() {
  return localStorage.getItem(TOKEN_STORAGE_KEY) || "";
}

function setControlToken(value) {
  if (value) localStorage.setItem(TOKEN_STORAGE_KEY, value);
  else localStorage.removeItem(TOKEN_STORAGE_KEY);
}

// Anexa "?token=" numa URL — só necessário pro EventSource (GET
// /api/events), que não consegue mandar um header Authorization
// customizado. Chamadas via fetch() usam o header normalmente (ver
// apiFetch), que é a forma preferida.
function withTokenParam(url) {
  const token = getControlToken();
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

// Wrapper único de fetch() pra toda chamada à API do painel — injeta o
// token (se configurado) e centraliza o aviso de "acesso negado" num
// único lugar, em vez de repetir a checagem de status 401 em cada
// função que fala com o backend.
async function apiFetch(url, options = {}) {
  const token = getControlToken();
  const headers = { ...(options.headers || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401) {
    toast("Acesso negado — defina o token do painel (🔒 no topo) e tente de novo.", "error");
  }
  return res;
}

function promptForToken() {
  const current = getControlToken();
  const value = window.prompt(
    "Token do painel (--control-token do servidor) — deixe vazio se o painel não exige token:",
    current
  );
  if (value === null) return; // cancelado, nada muda
  setControlToken(value.trim());
  toast(value.trim() ? "Token salvo." : "Token removido.", "success");
  // Reabre o stream de eventos já com o token atualizado.
  if (eventSource) eventSource.close();
  connectEventStream();
  refresh();
}

// ── Toasts empilháveis (com detalhe opcional expansível) ────────────

function toast(message, kind = "info", details = null) {
  const stack = document.getElementById("toast-stack");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;

  const msgEl = document.createElement("div");
  msgEl.className = "toast-message";
  msgEl.textContent = message;
  el.appendChild(msgEl);

  // Detalhe por charger (ex: resultado de uma ação em massa) — só
  // aparece quando faz sentido, escondido atrás de um toggle pra não
  // inflar o toast de ações simples de 1 charger só.
  if (details && details.length > 0) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "toast-toggle";
    toggle.textContent = `ver detalhes (${details.length}) ▸`;

    const list = document.createElement("div");
    list.className = "toast-details";
    list.hidden = true;
    list.innerHTML = details.map((d) => `<div>${escapeHtml(d)}</div>`).join("");

    toggle.addEventListener("click", () => {
      list.hidden = !list.hidden;
      toggle.textContent = list.hidden ? `ver detalhes (${details.length}) ▸` : "ocultar detalhes ▾";
    });

    el.appendChild(toggle);
    el.appendChild(list);
  }

  stack.appendChild(el);
  const raf = window.requestAnimationFrame || ((cb) => setTimeout(cb, 0));
  raf(() => el.classList.add("show"));
  // Toasts com detalhes ficam mais tempo na tela — o usuário pode
  // querer abrir a lista antes que suma sozinho.
  const lifetime = details && details.length > 0 ? 6000 : 3200;
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 250);
  }, lifetime);
}

// Monta o toast de resultado de uma ação em massa (/api/command/all) —
// mensagens de exceção isolada (prefixo "erro:", ver broadcast_command
// em orchestrator.py) contam como falha pra decidir a cor do toast;
// qualquer outra resposta (mesmo "já em sessão" etc.) é só informativa.
function showBulkResultToast(data) {
  if (!data.ok) {
    toast(data.message || "erro", "error");
    return;
  }
  const results = data.results || {};
  const entries = Object.entries(results);
  const failed = entries.filter(([, msg]) => /^erro:/i.test(msg));
  const kind = entries.length === 0 ? "error" : (failed.length > 0 ? "error" : "success");
  const details = entries.map(([id, msg]) => `${id}: ${msg}`);
  toast(data.message, kind, details);
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
    const res = await apiFetch("/api/command", {
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

// Lê os campos de "opções avançadas" do formulário de adicionar
// charger — só entram no payload os campos de fato preenchidos, cada
// um virando um override de SimConfig só pro(s) charger(s) desta leva
// (ver CHARGER_OVERRIDE_FIELDS em config.py).
function collectAddOverrides() {
  const overrides = {};
  const batteryKwh = parseFloat(document.getElementById("adv-battery-kwh").value);
  const initialSoc = parseFloat(document.getElementById("adv-initial-soc").value);
  const defaultAmps = parseFloat(document.getElementById("adv-default-amps").value);
  if (!Number.isNaN(batteryKwh) && batteryKwh > 0) overrides.battery_capacity_wh = batteryKwh * 1000;
  if (!Number.isNaN(initialSoc)) overrides.initial_soc_percent = initialSoc;
  if (!Number.isNaN(defaultAmps) && defaultAmps >= 0) overrides.default_offered_amps = defaultAmps;
  return overrides;
}

async function addOneCharger(chargeId, overrides) {
  const res = await apiFetch("/api/chargers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ charge_point_id: chargeId, ...overrides }),
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

  const overrides = collectAddOverrides();
  const results = await Promise.all(ids.map((id) =>
    addOneCharger(id, overrides).catch((e) => ({ ok: false, message: `${id}: ${e}` }))
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
    const res = await apiFetch(`/api/chargers/${encodeURIComponent(chargeId)}`, { method: "DELETE" });
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

// IDs dos chargers atualmente visíveis na grade (respeitando o filtro
// de busca) — é isso, e não "todos os chargers do registry", que as
// ações da bulk-actions-row devem afetar. Card escondido (display:none,
// ver updateCard) não entra.
function visibleChargerIds() {
  const ids = [];
  for (const [id, el] of cardElements) {
    if (el.style.display !== "none") ids.push(id);
  }
  return ids;
}

function updateBulkCountLabel() {
  const label = document.getElementById("bulk-actions-label");
  if (!label) return;
  const n = visibleChargerIds().length;
  label.textContent = currentFilter ? `Visíveis (${n}):` : `Todos (${n}):`;
}

// Dispara um comando nos chargers VISÍVEIS no momento (respeita o
// filtro de busca — ver visibleChargerIds) de uma vez. O backend
// (/api/command/all) já isola falha de um charger sem derrubar os
// demais — aqui só resume o resultado num único toast expansível em
// vez de um por charger, pra não inundar a tela com N toasts de uma vez.
async function sendBulkCommand(cmd, { args = [], confirmMessage, button } = {}) {
  const ids = visibleChargerIds();
  if (ids.length === 0) {
    toast("Nenhum charger visível pra aplicar essa ação (confira o filtro de busca).", "error");
    return;
  }
  if (confirmMessage) {
    const confirmed = await confirmDialog(confirmMessage);
    if (!confirmed) return;
  }
  if (button) button.disabled = true;
  try {
    const res = await apiFetch("/api/command/all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cmd, args, ids }),
    });
    const data = await res.json();
    showBulkResultToast(data);
  } catch (e) {
    toast(`Falha ao enviar comando em massa: ${e}`, "error");
  } finally {
    if (button) button.disabled = false;
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
    <div class="wave" data-role="wave">
      <div class="wave-track">
        <svg viewBox="0 0 240 40" preserveAspectRatio="none" aria-hidden="true">
          <path class="wave-path" d="M0,20 Q15,0 30,20 T60,20 T90,20 T120,20 T150,20 T180,20 T210,20 T240,20" />
        </svg>
        <svg viewBox="0 0 240 40" preserveAspectRatio="none" aria-hidden="true">
          <path class="wave-path" d="M0,20 Q15,0 30,20 T60,20 T90,20 T120,20 T150,20 T180,20 T210,20 T240,20" />
        </svg>
      </div>
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

  // Amplitude reflete a corrente real puxada (c.actual_amps) contra um
  // teto de referência de 32A (AC monofásico/trifásico comum) — só
  // usado quando "charging" (as outras classes de status fixam sua
  // própria amplitude em CSS, ver .wave[data-status=...] .wave-path).
  const wave = el.querySelector('[data-role="wave"]');
  wave.dataset.status = status;
  const ampRatio = Math.min(1, (c.actual_amps || 0) / 32);
  wave.querySelectorAll(".wave-path").forEach((p) => {
    p.style.setProperty("--wave-amp", (0.15 + ampRatio * 0.7).toFixed(2));
  });

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

// Ordena conforme currentSort ("id" | "status" | "soc") — ID é sempre
// o desempate, pra ordem estável entre re-renderizações.
function sortChargers(chargers) {
  const arr = [...chargers];
  if (currentSort === "status") {
    arr.sort((a, b) => {
      const ra = a.online ? (STATUS_SORT_RANK[a.status] ?? 5) : 6;
      const rb = b.online ? (STATUS_SORT_RANK[b.status] ?? 5) : 6;
      return ra !== rb ? ra - rb : a.charge_point_id.localeCompare(b.charge_point_id);
    });
  } else if (currentSort === "soc") {
    arr.sort((a, b) => b.soc_percent - a.soc_percent || a.charge_point_id.localeCompare(b.charge_point_id));
  } else {
    arr.sort((a, b) => a.charge_point_id.localeCompare(b.charge_point_id));
  }
  return arr;
}

function syncGrid(chargers) {
  lastChargers = chargers;
  updateStatsStrip(chargers);

  const grid = document.getElementById("grid");
  if (chargers.length === 0) {
    renderEmptyState(false);
    updateBulkCountLabel();
    return;
  }
  if (grid.querySelector(".empty")) {
    grid.innerHTML = "";
  }

  const sorted = sortChargers(chargers);
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
  updateBulkCountLabel();
}

async function refresh() {
  try {
    const res = await apiFetch("/api/state");
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
  const label = document.getElementById("link-status-label");
  if (!dot) return;
  dot.classList.toggle("connected", connected);
  dot.title = connected ? "Atualização ao vivo (SSE conectado)" : "Reconectando ao painel...";
  if (label) label.textContent = connected ? "ao vivo" : "reconectando";
}

function connectEventStream() {
  eventSource = new EventSource(withTokenParam("/api/events"));
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
    // só refletimos isso visualmente enquanto isso não volta. Um 401
    // (token errado/ausente) também cai aqui — o EventSource não expõe
    // o status code, então o toast de auth só aparece via apiFetch()
    // nas outras chamadas; aqui só o indicador fica vermelho mesmo.
    setSseIndicator(false);
  };
}

document.getElementById("add-charger-btn").addEventListener("click", addCharger);
document.getElementById("new-charger-id").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addCharger();
});
document.getElementById("add-advanced-toggle").addEventListener("click", () => {
  const row = document.getElementById("advanced-add-row");
  row.hidden = !row.hidden;
});
document.getElementById("search-input").addEventListener("input", (e) => {
  currentFilter = e.target.value.trim().toLowerCase();
  for (const [id, el] of cardElements) {
    el.style.display = (!currentFilter || id.toLowerCase().includes(currentFilter)) ? "" : "none";
  }
  updateBulkCountLabel();
});
document.getElementById("sort-select").addEventListener("change", (e) => {
  currentSort = e.target.value;
  syncGrid(lastChargers); // reordena na hora, sem esperar o próximo evento do SSE
});
document.getElementById("token-btn").addEventListener("click", promptForToken);
document.getElementById("bulk-fault-select").innerHTML = buildFaultOptions(FAULT_CODES[0]);

const bulkStartBtn = document.querySelector('[data-role="bulk-start"]');
const bulkStopBtn = document.querySelector('[data-role="bulk-stop"]');
const bulkPauseBtn = document.querySelector('[data-role="bulk-pause"]');
const bulkResumeBtn = document.querySelector('[data-role="bulk-resume"]');
const bulkDisconnectBtn = document.querySelector('[data-role="bulk-disconnect"]');
const bulkFaultBtn = document.querySelector('[data-role="bulk-fault"]');
const bulkClearBtn = document.querySelector('[data-role="bulk-clear"]');

bulkStartBtn.addEventListener("click", () => {
  const idTag = document.getElementById("bulk-id-tag").value.trim() || "LOCAL_TAG";
  sendBulkCommand("start", { args: [idTag], button: bulkStartBtn });
});
bulkStopBtn.addEventListener("click", () =>
  sendBulkCommand("stop", {
    button: bulkStopBtn,
    confirmMessage: "Parar a sessão de TODOS os chargers visíveis? Isso encerra a transação (StopTransaction) de cada um agora.",
  }));
bulkPauseBtn.addEventListener("click", () =>
  sendBulkCommand("pause", { button: bulkPauseBtn }));
bulkResumeBtn.addEventListener("click", () =>
  sendBulkCommand("resume", { button: bulkResumeBtn }));
bulkDisconnectBtn.addEventListener("click", () =>
  sendBulkCommand("disconnect", {
    button: bulkDisconnectBtn,
    confirmMessage: "Desconectar TODOS os chargers visíveis? Cada um reconecta sozinho em seguida, mas as conexões atuais serão encerradas agora.",
  }));
bulkFaultBtn.addEventListener("click", () => {
  const code = document.getElementById("bulk-fault-select").value;
  sendBulkCommand("fault", {
    args: [code],
    button: bulkFaultBtn,
    confirmMessage: `Colocar TODOS os chargers visíveis em Faulted (${code})? Use "Clear" depois pra voltar ao normal.`,
  });
});
bulkClearBtn.addEventListener("click", () =>
  sendBulkCommand("clear", { button: bulkClearBtn }));

refresh();            // 1ª pintura imediata, antes do stream conectar
connectEventStream(); // daqui pra frente, toda atualização vem por push
