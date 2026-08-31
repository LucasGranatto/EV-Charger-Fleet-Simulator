// FAULT_CODES, STATUS_LABEL, STATUS_SORT_RANK, escapeHtml, escapeAttr,
// parseChargerIdsFromText, displayStatus, buildFaultOptions,
// buildLinePoints, formatSecondsAgo e sortChargers agora vivem em
// pure.js (carregado ANTES deste arquivo em index.html) — são as
// funções/constantes sem nenhuma dependência de DOM, extraídas de
// propósito pra dar pra testar isoladamente (ver tests/app.test.js)
// sem precisar simular um browser inteiro. Continuam disponíveis aqui
// como globais, exatamente como antes.

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
// "cards" (padrão, 1 card por charger com todos os controles) ou
// "table" (1 linha por charger, só telemetria — pensado pra escanear
// dezenas/centenas de chargers de uma vez; ver renderTable()).
let viewMode = "cards";
// Último snapshot recebido (via SSE ou refresh()) — guardado pra poder
// reordenar (mudar currentSort) instantaneamente, sem esperar o
// próximo evento do stream.
let lastChargers = [];

// Atalho pra achar um elemento pelo atributo data-role dentro de um
// container (cardEl, panel, document, etc.) — substitui o padrão
// `container.querySelector('[data-role="X"]')` que se repetia 40+
// vezes espalhado por createCard/updateCard/renderHistoryChart (cada
// ocorrência reescrevendo a mesma string de seletor CSS à mão).
// Comportamento idêntico, mesmo seletor por trás — só reduz a
// repetição textual e centraliza esse padrão num único lugar.
function qr(container, role) {
  return container.querySelector(`[data-role="${role}"]`);
}

// Atalho pra document.getElementById — repetido 75+ vezes no arquivo
// (algumas IDs buscadas em até 6 pontos diferentes, ex:
// history-charger-select). Mesmo `$(id)` por
// trás, só sem reescrever o nome inteiro em cada chamada.
const $ = (id) => document.getElementById(id);

// ── Tema claro/escuro ────────────────────────────────────────────────
// Persistido em localStorage (mesmo padrão do token abaixo) — a
// aplicação em si já acontece ANTES deste script rodar, via script
// inline no <head> de index.html (evita o flash do tema errado no
// reload); aqui só cuidamos de TROCAR o tema depois que a página já
// está de pé, respondendo ao clique no botão ☀/☾ do topbar.
const THEME_STORAGE_KEY = "evchargersim_theme";

function getTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

function setTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch (e) {
    // localStorage indisponível — tema ainda troca nesta sessão, só
    // não persiste entre reloads (mesma degradação graciosa do script
    // inline no <head>).
  }
}

function toggleTheme() {
  setTheme(getTheme() === "light" ? "dark" : "light");
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

function promptDialog(message, defaultValue = "") {
  const overlay = $("prompt-overlay");
  const title = $("prompt-title");
  const input = $("prompt-input");
  const okBtn = $("prompt-ok");
  const cancelBtn = $("prompt-cancel");
  title.textContent = message;
  input.value = defaultValue;
  overlay.hidden = false;
  input.focus();
  input.select();

  return new Promise((resolve) => {
    function cleanup(result) {
      overlay.hidden = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("keydown", onKeydown);
      resolve(result);
    }
    function onOk() { cleanup(input.value); }
    function onCancel() { cleanup(null); } // null == "cancelado", mesma convenção do window.prompt()
    function onKeydown(e) {
      // Diferente do confirmDialog (ação destrutiva): aqui Enter
      // confirma incondicionalmente, igual ao window.prompt() nativo
      // que este modal substitui — salvar um token é uma ação de baixo
      // risco e reversível (é só digitar de novo), não precisa da
      // mesma cautela de foco que a remoção de um charger.
      if (e.key === "Escape") cleanup(null);
      if (e.key === "Enter") cleanup(input.value);
    }
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("keydown", onKeydown);
  });
}

async function promptForToken() {
  const current = getControlToken();
  const value = await promptDialog(
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
  const stack = $("toast-stack");
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
  const overlay = $("confirm-overlay");
  const title = $("confirm-title");
  const okBtn = $("confirm-ok");
  const cancelBtn = $("confirm-cancel");
  title.textContent = message;
  overlay.hidden = false;
  // Foco no botão SEGURO (Cancelar) por padrão, não no destrutivo
  // ("Remover", classe .danger) -- um Enter reflexo (ex: vindo de ter
  // digitado algo em outro campo, ou só o hábito de confirmar diálogos
  // com Enter) não deve conseguir disparar a ação perigosa sem uma
  // interação deliberada (clique ou Tab até o botão "Remover" e só
  // então Enter/Espaço).
  cancelBtn.focus();

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
      // Só Escape é tratado aqui, sempre como "cancelar" (padrão
      // universal de diálogo). Enter NÃO é interceptado de propósito:
      // <button> já ativa via Enter/Espaço quem estiver com foco no
      // momento (comportamento nativo do browser), então Enter confirma
      // só se o usuário tiver movido o foco até o botão "Remover" --
      // nunca como padrão. Antes, Enter confirmava incondicionalmente
      // aqui, ignorando qual botão estava focado.
      if (e.key === "Escape") cleanup(false);
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
  const batteryKwh = parseFloat($("adv-battery-kwh").value);
  const initialSoc = parseFloat($("adv-initial-soc").value);
  const defaultAmps = parseFloat($("adv-default-amps").value);
  const phases = $("adv-phases").value;
  if (!Number.isNaN(batteryKwh) && batteryKwh > 0) overrides.battery_capacity_wh = batteryKwh * 1000;
  if (!Number.isNaN(initialSoc)) overrides.initial_soc_percent = initialSoc;
  if (!Number.isNaN(defaultAmps) && defaultAmps >= 0) overrides.default_offered_amps = defaultAmps;
  if (phases) overrides.number_of_phases = parseInt(phases, 10);
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

// Núcleo compartilhado por addCharger() (IDs digitados) e
// importChargersFromFile() (IDs de um .txt) — dispara um POST por ID
// (overrides de "opções avançadas" aplicados a TODOS da leva, como já
// acontecia antes) e resume o resultado num único toast expansível,
// em vez de um toast por ID (importante sobretudo pro caso de arquivo,
// que pode trazer dezenas de IDs de uma vez).
async function addManyChargers(ids, { sourceLabel = null } = {}) {
  if (ids.length === 0) {
    toast("Nenhum ID válido encontrado.", "error");
    return { okCount: 0, failed: [] };
  }

  const overrides = collectAddOverrides();
  const results = await Promise.all(ids.map((id) =>
    addOneCharger(id, overrides).catch((e) => ({ ok: false, message: `${id}: ${e}` }))
  ));
  const okCount = results.filter((r) => r.ok).length;
  const failed = results.filter((r) => !r.ok).map((r) => r.message);
  const prefix = sourceLabel ? `[${sourceLabel}] ` : "";

  if (ids.length === 1) {
    toast(prefix + results[0].message, results[0].ok ? "success" : "error");
  } else if (failed.length === 0) {
    toast(`${prefix}${okCount} chargers adicionados.`, "success");
  } else {
    toast(`${prefix}${okCount} adicionado(s), ${failed.length} falharam.`, failed.length === okCount ? "error" : "success", failed);
  }
  return { okCount, failed };
}

// Aceita tanto um ID único quanto uma lista separada por vírgula
// ("CH01, CH02, CH03") digitada no campo — ver addManyChargers() para
// o que de fato dispara as requisições.
async function addCharger() {
  const input = $("new-charger-id");
  const ids = input.value.split(",").map((s) => s.trim()).filter(Boolean);
  if (ids.length === 0) {
    toast("Digite ao menos um ID antes de adicionar.", "error");
    return;
  }
  const { okCount } = await addManyChargers(ids);
  if (okCount > 0) input.value = "";
  // Sem refresh() manual — /api/events mostra o(s) novo(s) charger(s)
  // assim que ele(s) conectar(em) de verdade.
}

// parseChargerIdsFromText agora vive em pure.js.


// Lê o .txt escolhido no <input type="file"> inteiramente no browser
// (FileReader) — nenhuma rota nova no backend: cada ID vira o mesmo
// POST /api/chargers que "+ Adicionar" já dispara, um por um.
async function importChargersFromFile(file) {
  let text;
  try {
    text = await file.text();
  } catch (e) {
    toast(`Não foi possível ler o arquivo: ${e}`, "error");
    return;
  }
  const ids = parseChargerIdsFromText(text);
  if (ids.length === 0) {
    toast(`Nenhum ID encontrado em "${file.name}".`, "error");
    return;
  }
  await addManyChargers(ids, { sourceLabel: file.name });
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
  // Em modo tabela, cardElements não é mais atualizado a cada snapshot
  // (ver o branch `viewMode === "table"` em syncGrid) — usar o filtro
  // direto sobre lastChargers evita que ações em massa apliquem num
  // conjunto de IDs desatualizado, congelado de antes de trocar de modo.
  if (viewMode === "table") {
    return lastChargers
      .filter((c) => !currentFilter || c.charge_point_id.toLowerCase().includes(currentFilter))
      .map((c) => c.charge_point_id);
  }
  const ids = [];
  for (const [id, el] of cardElements) {
    if (el.style.display !== "none") ids.push(id);
  }
  return ids;
}

function updateBulkCountLabel() {
  const label = $("bulk-actions-label");
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

// ── Painel de ajuste de chaos por card (ao vivo, sem remover/readicionar) ──
//
// Ao contrário do histórico, os valores de chaos JÁ vêm em todo
// snapshot do SSE (campo c.chaos, ver get_status_dict() em
// charger.py) — não precisa de fetch próprio. O truque é só
// pré-preencher o formulário UMA VEZ, no momento em que o usuário abre
// o painel (não a cada snapshot, ou ficaria impossível digitar: o
// campo resetaria a cada ~1s enquanto uma sessão está ativa). O
// snapshot mais recente fica guardado em cardEl._chaosSnapshot,
// atualizado em silêncio por updateCard() a cada refresh.
const CHAOS_FIELD_ROLES = {
  "chaos-disconnect-interval": "chaos_disconnect_interval_seconds",
  "chaos-disconnect-jitter": "chaos_disconnect_jitter_seconds",
  "chaos-latency-min": "chaos_latency_min_ms",
  "chaos-latency-max": "chaos_latency_max_ms",
  "chaos-drop-rate": "chaos_drop_rate",
  "chaos-max-queue": "max_offline_queue_size",
};

function populateChaosForm(cardEl, chaos) {
  for (const [role, field] of Object.entries(CHAOS_FIELD_ROLES)) {
    const input = qr(cardEl, role);
    if (input) input.value = chaos[field] ?? 0;
  }
}

// Quais campos de fato ligam cada tipo de chaos — note que
// max_offline_queue_size FICA DE FORA de propósito: é um teto de fila
// (config normal, tem default 500 > 0), não uma instabilidade injetada.
// Contá-lo como "chaos ativo" era o bug por trás do dot que parecia
// aceso o tempo todo mesmo sem nenhum chaos de verdade ligado — ver
// histórico de updateCard() antes desta versão. chaos_disconnect_jitter
// também fica fora do gatilho (só tem efeito quando o intervalo > 0,
// então é o intervalo que decide se "desconexão" está ativa).
const CHAOS_GROUP_TRIGGER_FIELDS = {
  disconnect: ["chaos_disconnect_interval_seconds"],
  latency: ["chaos_latency_min_ms", "chaos_latency_max_ms"],
  drop: ["chaos_drop_rate"],
};

function activeChaosGroups(chaos) {
  return Object.entries(CHAOS_GROUP_TRIGGER_FIELDS)
    .filter(([, fields]) => fields.some((f) => (chaos[f] || 0) > 0))
    .map(([group]) => group);
}

function describeChaosGroup(group, chaos) {
  switch (group) {
    case "disconnect": {
      const interval = chaos.chaos_disconnect_interval_seconds;
      const jitter = chaos.chaos_disconnect_jitter_seconds || 0;
      return jitter > 0
        ? `desconexão a cada ~${interval}s (±${jitter}s)`
        : `desconexão a cada ${interval}s`;
    }
    case "latency": {
      const min = chaos.chaos_latency_min_ms || 0;
      const max = chaos.chaos_latency_max_ms || 0;
      return `latência ${min}–${max}ms`;
    }
    case "drop":
      return `perda de msg ${Math.round((chaos.chaos_drop_rate || 0) * 100)}%`;
    default:
      return group;
  }
}

// Substitui o antigo dot âmbar único (que só dizia "tem algo ligado",
// sem dizer o quê — e ainda saía sempre aceso por causa do bug acima)
// por: um badge neutro com a CONTAGEM de chaos ativos no ícone do
// card, um resumo em texto no topo do painel expandido listando cada
// um por nome/valor, e um destaque nos próprios campos do grupo ativo
// dentro do formulário. Roda a cada snapshot (chamada de updateCard),
// não só quando o painel abre — só mexe em classList/textContent,
// nunca no <input> em si, então não atrapalha quem estiver digitando.
function updateChaosIndicators(el, chaos) {
  const groups = activeChaosGroups(chaos);
  const descriptions = groups.map((g) => describeChaosGroup(g, chaos));

  const badge = qr(el, "chaos-badge");
  badge.hidden = groups.length === 0;
  badge.textContent = String(groups.length);

  const toggleBtn = qr(el, "chaos-toggle");
  toggleBtn.title = groups.length === 0
    ? "Ajustar chaos deste charger"
    : `Chaos ativo: ${descriptions.join(" · ")}`;

  const summary = qr(el, "chaos-active-summary");
  if (summary) {
    summary.textContent = groups.length === 0
      ? "Nenhum chaos ativo no momento."
      : `Ativo agora: ${descriptions.join(" · ")}`;
    summary.classList.toggle("chaos-active-summary-on", groups.length > 0);
  }

  el.querySelectorAll("[data-chaos-group]").forEach((label) => {
    label.classList.toggle("active", groups.includes(label.dataset.chaosGroup));
  });
}

function toggleChaosPanel(cardEl) {
  const panel = qr(cardEl, "chaos-panel");
  const opening = panel.hidden;
  panel.hidden = !opening;
  if (opening) {
    populateChaosForm(cardEl, cardEl._chaosSnapshot || {});
    qr(cardEl, "chaos-status").textContent = "";
  }
}

async function applyChaos(chargeId, cardEl) {
  const btn = qr(cardEl, "chaos-apply");
  const statusEl = qr(cardEl, "chaos-status");
  const payload = {};
  for (const [role, field] of Object.entries(CHAOS_FIELD_ROLES)) {
    const input = qr(cardEl, role);
    const value = parseFloat(input.value);
    payload[field] = Number.isFinite(value) ? value : 0;
  }

  btn.disabled = true;
  statusEl.textContent = "aplicando...";
  statusEl.classList.remove("chaos-status-error");
  try {
    const res = await apiFetch(`/api/chargers/${encodeURIComponent(chargeId)}/chaos`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.ok) {
      statusEl.textContent = "aplicado ✓";
    } else {
      statusEl.textContent = "erro";
      statusEl.classList.add("chaos-status-error");
      toast(data.message, "error");
    }
  } catch (e) {
    statusEl.textContent = "falhou";
    statusEl.classList.add("chaos-status-error");
    toast(`Falha ao ajustar chaos de ${chargeId}: ${e}`, "error");
  } finally {
    btn.disabled = false;
  }
}

// Ajusta o número de fases de um charger JÁ CONECTADO, ao vivo — POST
// /api/chargers/<id>/phases (ver EVChargerSim.apply_power_overrides()).
// Sem botão "Aplicar" dedicado: dispara direto no "change" do <select>
// (ver createCard). Se falhar, updateCard() vai repor o select pro
// valor real assim que o próximo snapshot chegar — não precisa
// reverter manualmente aqui.
async function setPhases(chargeId, selectEl) {
  selectEl.disabled = true;
  try {
    const res = await apiFetch(`/api/chargers/${encodeURIComponent(chargeId)}/phases`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ number_of_phases: parseInt(selectEl.value, 10) }),
    });
    const data = await res.json();
    if (data.ok) {
      toast(`[${chargeId}] ${data.message}`, "success");
    } else {
      toast(`[${chargeId}] ${data.message}`, "error");
    }
  } catch (e) {
    toast(`[${chargeId}] falha ao ajustar fases: ${e}`, "error");
  } finally {
    selectEl.disabled = false;
  }
}



// ── Aba "Histórico" — gráfico ampliado de UM charger por vez ────────
//
// Antes o gráfico vivia dentro de cada card, pequeno demais pra tirar
// leitura fina dos dados. Agora é uma aba própria: o botão 📈 de cada
// card só navega pra cá e pré-seleciona aquele charger — o SVG e a
// lógica de renderização (renderHistoryChart) são os MESMOS de antes,
// só que reaproveitados dentro de um container bem maior (o viewBox é
// só um sistema de coordenadas abstrato; escalar o container escala
// tudo — grade, texto, linhas — proporcionalmente, sem precisar de
// nenhuma lógica de "modo grande" separada).
//
// Como só existe UM gráfico visível por vez (o da aba, não mais um por
// card), só precisa de UM poller ativo — bem mais simples que o Map
// de intervalos por charger de antes.
const HISTORY_POLL_MS = 4000;
let historyViewChargeId = null;
let historyViewPollId = null;

async function fetchAndRenderHistory(chargeId, panel) {
  try {
    const res = await apiFetch(`/api/history/${encodeURIComponent(chargeId)}`);
    if (!res.ok) return; // 404 (charger removido nesse meio-tempo) — próximo poll silencia sozinho
    renderHistoryChart(panel, await res.json());
  } catch (e) {
    // Silencioso de propósito: uma falha de rede pontual aqui não deve
    // virar um toast a cada 4s — o próximo poll tenta de novo.
  }
}

function stopHistoryViewPolling() {
  if (historyViewPollId) {
    clearInterval(historyViewPollId);
    historyViewPollId = null;
  }
}

function startHistoryViewPolling() {
  stopHistoryViewPolling();
  if (!historyViewChargeId) return;
  const panel = $("history-view-card");
  fetchAndRenderHistory(historyViewChargeId, panel);
  historyViewPollId = setInterval(() => fetchAndRenderHistory(historyViewChargeId, panel), HISTORY_POLL_MS);
}

// Reconstrói as opções do <select> só quando o CONJUNTO de IDs muda —
// preservando a seleção atual sempre que possível, pra não expulsar o
// usuário do charger que ele estava olhando a cada snapshot do SSE.
function populateHistoryChargerSelect(chargers) {
  const select = $("history-charger-select");
  const ids = chargers.map((c) => c.charge_point_id).sort();
  const currentOptionIds = Array.from(select.options).map((o) => o.value);
  const changed = ids.length !== currentOptionIds.length || ids.some((id, i) => id !== currentOptionIds[i]);

  if (changed) {
    const previousSelection = historyViewChargeId;
    select.innerHTML = ids.map((id) => `<option value="${escapeAttr(id)}">${escapeHtml(id)}</option>`).join("");
    if (previousSelection && ids.includes(previousSelection)) {
      select.value = previousSelection;
    } else if (ids.length > 0) {
      historyViewChargeId = ids[0];
      select.value = ids[0];
    } else {
      historyViewChargeId = null;
    }
  }

  const noChargers = $("history-view-no-chargers");
  const card = $("history-view-card");
  const nav = document.querySelector(".history-view-toolbar");
  const hasChargers = ids.length > 0;
  noChargers.hidden = hasChargers;
  card.hidden = !hasChargers;
  nav.hidden = !hasChargers;
}

// Cabeçalho da aba (LED/pill/ID) reflete o snapshot mais recente do
// charger selecionado — isso SIM vem de graça pelo SSE (é o mesmo
// c.status/c.online de sempre), só os pontos do gráfico em si é que
// dependem do poll acima.
function updateHistoryViewHeader() {
  const c = lastChargers.find((x) => x.charge_point_id === historyViewChargeId);
  const led = $("history-view-led");
  const pill = $("history-view-pill");
  const idEl = $("history-view-id");
  if (!c) {
    led.className = "led";
    pill.className = "pill";
    pill.textContent = "";
    idEl.textContent = "—";
    return;
  }
  const status = displayStatus(c);
  led.className = `led ${status}`;
  pill.className = `pill ${status}`;
  pill.textContent = c.online ? (STATUS_LABEL[c.status] || c.status) : "Offline";
  idEl.textContent = c.charge_point_id;
}

function selectHistoryCharger(chargeId) {
  historyViewChargeId = chargeId;
  $("history-charger-select").value = chargeId;
  updateHistoryViewHeader();
  if (activeView === "history") startHistoryViewPolling();
}

// Chamado pelo botão 📈 de um card específico — garante que o charger
// exista nas opções (populateHistoryChargerSelect já rodou em todo
// syncGrid) antes de selecioná-lo e trocar de aba.
function openHistoryView(chargeId) {
  historyViewChargeId = chargeId;
  setActiveView("history");
}

// ── Abas de nível superior (Frota / Histórico) ───────────────────────
let activeView = "fleet";

function setActiveView(view) {
  activeView = view;
  document.querySelectorAll(".view-tab").forEach((btn) => {
    const active = btn.dataset.view === view;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".view-panel").forEach((panel) => {
    panel.hidden = panel.dataset.viewPanel !== view;
  });

  if (view === "history") {
    populateHistoryChargerSelect(lastChargers);
    $("history-charger-select").value = historyViewChargeId || "";
    updateHistoryViewHeader();
    populateCompareList(lastChargers);
    if (historyMode === "compare") {
      startCompareViewPolling();
    } else {
      startHistoryViewPolling();
    }
  } else {
    stopHistoryViewPolling();
    stopCompareViewPolling();
  }
}

// ── Aba "Histórico" > modo "Comparar" ────────────────────────────────
//
// Sobrepõe a MESMA métrica (SoC, corrente real, limite ofertado ou
// potência) de vários chargers num único gráfico, alinhados pelo
// tempo REAL (não pelo índice da amostra — ver buildTimeSeriesPoints
// em pure.js) — responde de relance perguntas como "qual desses 3
// chargers chegou primeiro na fase de tapering?" ou "algum ficou muito
// tempo parado?", sem precisar alternar um por um no modo Individual.
//
// Reaproveita o MESMO endpoint GET /api/history/<id> do modo
// Individual — um fetch paralelo por charger selecionado a cada ciclo
// de poll, nada de endpoint novo no backend.
const COMPARE_POLL_MS = 4000;
const COMPARE_MAX_SELECTION = 8;
let historyMode = "single"; // "single" | "compare"
let compareSelectedIds = new Set();
let compareMetric = "soc";
let compareViewPollId = null;
// id -> índice de cor, reatribuído a cada populateCompareList() a
// partir da posição no conjunto ORDENADO de todos os chargers da
// frota — assim um charger mantém a mesma cor entre re-renderizações
// (só muda se OUTRO charger anterior a ele na ordem for removido).
let compareColorIndexById = new Map();

function setHistoryMode(mode) {
  historyMode = mode;
  $("history-mode-single").classList.toggle("active", mode === "single");
  $("history-mode-compare").classList.toggle("active", mode === "compare");
  $("history-mode-single").setAttribute("aria-pressed", mode === "single" ? "true" : "false");
  $("history-mode-compare").setAttribute("aria-pressed", mode === "compare" ? "true" : "false");

  $("history-view-select-wrap").hidden = mode !== "single";
  $("history-view-nav-wrap").hidden = mode !== "single";
  $("history-view-card").hidden = mode !== "single";
  $("history-compare-card").hidden = mode !== "compare";

  if (mode === "compare") {
    stopHistoryViewPolling();
    startCompareViewPolling();
  } else {
    stopCompareViewPolling();
    startHistoryViewPolling();
  }
}

// Reconstrói a lista de checkboxes só quando o CONJUNTO de IDs muda —
// mesmo espírito de populateHistoryChargerSelect (não expulsar o
// usuário do meio de uma seleção em andamento a cada snapshot do SSE).
// Chargers removidos da frota também saem de compareSelectedIds, pra
// não deixar o gráfico tentando comparar um charger que já não existe.
function populateCompareList(chargers) {
  const list = $("history-compare-list");
  const ids = chargers.map((c) => c.charge_point_id).sort();
  const currentIds = Array.from(list.querySelectorAll("input[type=checkbox]")).map((el) => el.value);
  const changed = ids.length !== currentIds.length || ids.some((id, i) => id !== currentIds[i]);
  if (!changed) return;

  compareColorIndexById = new Map(ids.map((id, i) => [id, i]));
  for (const id of Array.from(compareSelectedIds)) {
    if (!ids.includes(id)) compareSelectedIds.delete(id);
  }

  if (ids.length === 0) {
    list.innerHTML = `<div class="compare-check-empty">nenhum charger na frota ainda</div>`;
  } else {
    list.innerHTML = ids.map((id) => {
      const color = colorForCompareIndex(compareColorIndexById.get(id));
      const checked = compareSelectedIds.has(id) ? "checked" : "";
      return `
        <label class="compare-check">
          <input type="checkbox" value="${escapeAttr(id)}" ${checked}>
          <i class="compare-check-swatch" style="background:${color}"></i>
          <span class="compare-check-id">${escapeHtml(id)}</span>
        </label>`;
    }).join("");
  }
  updateCompareHint();
}

function updateCompareHint() {
  const hint = $("history-compare-hint");
  const n = compareSelectedIds.size;
  hint.classList.toggle("limit-reached", n >= COMPARE_MAX_SELECTION);
  if (n >= COMPARE_MAX_SELECTION) {
    hint.textContent = `limite de ${COMPARE_MAX_SELECTION} chargers atingido`;
  } else {
    hint.textContent = `${n} de até ${COMPARE_MAX_SELECTION} chargers selecionados`;
  }
}

function toggleCompareCharger(id, checked) {
  if (checked && compareSelectedIds.size >= COMPARE_MAX_SELECTION) {
    toast(`Selecione no máximo ${COMPARE_MAX_SELECTION} chargers por vez pra manter o gráfico legível.`, "info");
    const input = document.querySelector(`#history-compare-list input[value="${CSS.escape(id)}"]`);
    if (input) input.checked = false;
    return;
  }
  if (checked) compareSelectedIds.add(id);
  else compareSelectedIds.delete(id);
  updateCompareHint();
  fetchAndRenderCompare();
}

async function fetchAndRenderCompare() {
  const card = $("history-compare-card");
  const ids = Array.from(compareSelectedIds);
  if (ids.length === 0) {
    renderCompareChart(card, {});
    return;
  }
  try {
    const results = await Promise.all(ids.map(async (id) => {
      const res = await apiFetch(`/api/history/${encodeURIComponent(id)}`);
      return [id, res.ok ? await res.json() : []];
    }));
    const seriesById = Object.fromEntries(results);
    // Um charger pode ter sido removido/desmarcado enquanto o fetch
    // estava em voo — descarta o resultado se a seleção já mudou,
    // pra não pintar um gráfico com dado velho por cima do atual.
    if (historyMode === "compare") renderCompareChart(card, seriesById);
  } catch (e) {
    // Silencioso, igual ao poll do modo Individual: falha pontual de
    // rede não deve virar toast a cada 4s, o próximo poll tenta de novo.
  }
}

function stopCompareViewPolling() {
  if (compareViewPollId) {
    clearInterval(compareViewPollId);
    compareViewPollId = null;
  }
}

function startCompareViewPolling() {
  stopCompareViewPolling();
  fetchAndRenderCompare();
  compareViewPollId = setInterval(fetchAndRenderCompare, COMPARE_POLL_MS);
}

// Mesma técnica visual do gráfico Individual (grade + linhas num SVG
// de coordenadas abstratas), só que N linhas em vez de 3 fixas — uma
// por charger selecionado, cada uma na cor atribuída em
// populateCompareList — e o eixo X mapeado por TEMPO REAL comum (ver
// combinedTimeRange/buildTimeSeriesPoints em pure.js), não por índice,
// pra alinhar corretamente chargers com históricos de tamanhos ou
// janelas diferentes.
function renderCompareChart(card, seriesById) {
  const svg = qr(card, "compare-svg");
  const emptyMsg = qr(card, "compare-empty");
  const legend = qr(card, "compare-legend");

  const ids = Object.keys(seriesById);
  const seriesArrays = ids.map((id) => seriesById[id]);
  const range = combinedTimeRange(seriesArrays);
  const hasEnoughData = range !== null && ids.some((id) => seriesById[id].length >= 2);

  if (ids.length === 0 || !hasEnoughData) {
    svg.innerHTML = "";
    emptyMsg.hidden = false;
    emptyMsg.textContent = ids.length === 0
      ? "marque um ou mais chargers na lista ao lado para comparar"
      : "ainda sem amostras suficientes — aguarde o próximo ciclo de MeterValues";
    legend.innerHTML = "";
    return;
  }
  emptyMsg.hidden = true;

  const plotX0 = 32, plotX1 = 272, plotY0 = 10, plotY1 = 86;
  const metricDef = COMPARE_METRICS[compareMetric] || COMPARE_METRICS.soc;

  // Escala fixa (SoC: 0–100%) ou dinâmica (corrente/potência: maior
  // valor entre TODAS as séries selecionadas, com piso de 32 pras
  // séries de corrente não ficarem com uma escala minúscula quando
  // todo mundo está com pouca carga).
  let scaleMin = metricDef.min, scaleMax = metricDef.max;
  if (scaleMax == null) {
    const allVals = seriesArrays.flat().map((s) => s[compareMetric]);
    scaleMax = Math.max(compareMetric === "power_kw" ? 1 : 32, ...allVals);
    scaleMin = 0;
  }

  const yTicks = [scaleMin, (scaleMin + scaleMax) / 2, scaleMax];
  const gridLines = yTicks.map((tick) => {
    const y = (plotY1 - ((tick - scaleMin) / (scaleMax - scaleMin)) * (plotY1 - plotY0)).toFixed(1);
    return `<line x1="${plotX0}" y1="${y}" x2="${plotX1}" y2="${y}" class="history-grid${tick === scaleMin ? " history-grid-base" : ""}" />`;
  }).join("");
  const yLabels = yTicks.map((tick) => {
    const y = (plotY1 - ((tick - scaleMin) / (scaleMax - scaleMin)) * (plotY1 - plotY0) + 3).toFixed(1);
    return `<text x="${plotX0 - 5}" y="${y}" class="history-axis-label history-axis-label-soc" text-anchor="end">${Math.round(tick)}${metricDef.unit}</text>`;
  }).join("");

  const xTicks = [
    { x: plotX0, label: formatSecondsAgo(range.tMax - range.tMin), anchor: "start" },
    { x: (plotX0 + plotX1) / 2, label: formatSecondsAgo((range.tMax - range.tMin) / 2), anchor: "middle" },
    { x: plotX1, label: "agora", anchor: "end" },
  ];
  const xLabels = xTicks.map(({ x, label, anchor }) =>
    `<text x="${x}" y="${plotY1 + 14}" class="history-axis-label history-axis-label-time" text-anchor="${anchor}">${label}</text>`
  ).join("");

  let linesSvg = "";
  const legendItems = [];
  for (const id of ids) {
    const samples = seriesById[id];
    const color = colorForCompareIndex(compareColorIndexById.get(id) ?? 0);
    if (samples.length >= 2) {
      const points = buildTimeSeriesPoints(
        samples, compareMetric, plotX0, plotX1, plotY0, plotY1,
        range.tMin, range.tMax, scaleMin, scaleMax
      );
      linesSvg += `<polyline points="${points}" class="history-line" style="stroke:${color}" />`;
      const [lastX, lastY] = points.split(" ").pop().split(",");
      linesSvg += `<circle cx="${lastX}" cy="${lastY}" r="2.2" class="history-dot" style="fill:${color}" />`;
    }
    const lastValue = samples.length ? samples[samples.length - 1][compareMetric] : null;
    legendItems.push(
      `<span class="legend-item"><i style="background:${color}"></i>${escapeHtml(id)} <b>${formatCompareValue(compareMetric, lastValue)}</b></span>`
    );
  }

  svg.innerHTML = `${gridLines}${linesSvg}${yLabels}${xLabels}`;
  legend.innerHTML = legendItems.join("");
}

// buildLinePoints e formatSecondsAgo agora vivem em pure.js.


// Gráfico de histórico — 3 séries sobre uma grade legível: SoC (área
// preenchida + linha, escala fixa 0–100% no eixo esquerdo), corrente
// real puxada e limite oferecido pelo CSMS (escala dinâmica no eixo
// direito, 0–max). Ter as 3 juntas responde de relance a pergunta que
// só números soltos não respondiam: "o carro está sendo limitado pelo
// CSMS, ou é a própria bateria/tapering que está reduzindo a corrente?"
// (linha verde de corrente real colada na cinza-tracejada de limite =
// não; um vão entre elas = sim). Grade + rótulos de eixo (SoC à
// esquerda, corrente à direita, tempo embaixo) dão a régua que faltava
// pra tirar um número aproximado só de olhar, sem precisar do legend.
function renderHistoryChart(panel, samples) {
  const svg = qr(panel, "history-svg");
  const emptyMsg = qr(panel, "history-empty");
  const nowSoc = qr(panel, "history-soc-now");
  const nowAmps = qr(panel, "history-amps-now");
  const nowOffered = qr(panel, "history-offered-now");
  const windowLabel = qr(panel, "history-window");

  if (!samples || samples.length < 2) {
    svg.innerHTML = "";
    emptyMsg.hidden = false;
    nowSoc.textContent = "—";
    nowAmps.textContent = "—";
    if (nowOffered) nowOffered.textContent = "—";
    windowLabel.textContent = "";
    return;
  }
  emptyMsg.hidden = true;

  // Área de plotagem com margem pra rótulos de eixo — não usa mais o
  // viewBox inteiro (era o motivo do gráfico antigo não ter onde
  // colocar nenhuma escala sem sobrepor os dados).
  const plotX0 = 32, plotX1 = 272, plotY0 = 10, plotY1 = 86;

  const socSeries = samples.map((s) => s.soc);
  const ampsSeries = samples.map((s) => s.actual_amps);
  const offeredSeries = samples.map((s) => s.offered_amps);
  const ampsMax = Math.max(32, ...ampsSeries, ...offeredSeries);

  const socPoints = buildLinePoints(socSeries, plotX0, plotX1, plotY0, plotY1, 0, 100);
  const ampsPoints = buildLinePoints(ampsSeries, plotX0, plotX1, plotY0, plotY1, 0, ampsMax);
  const offeredPoints = buildLinePoints(offeredSeries, plotX0, plotX1, plotY0, plotY1, 0, ampsMax);
  const socAreaPoints = `${plotX0},${plotY1} ${socPoints} ${plotX1},${plotY1}`;

  // Grade horizontal na escala de SoC (0/25/50/75/100%) — a base (0%)
  // sai um pouco mais forte, funcionando como o "chão" do gráfico.
  const socTicks = [0, 25, 50, 75, 100];
  const gridLines = socTicks.map((tick) => {
    const y = (plotY1 - (tick / 100) * (plotY1 - plotY0)).toFixed(1);
    return `<line x1="${plotX0}" y1="${y}" x2="${plotX1}" y2="${y}" class="history-grid${tick === 0 ? " history-grid-base" : ""}" />`;
  }).join("");
  const socLabels = socTicks.map((tick) => {
    const y = (plotY1 - (tick / 100) * (plotY1 - plotY0) + 3).toFixed(1);
    return `<text x="${plotX0 - 5}" y="${y}" class="history-axis-label history-axis-label-soc" text-anchor="end">${tick}</text>`;
  }).join("");

  // Eixo direito (corrente): só mín/máx, pra não competir visualmente
  // com a grade de SoC — o objetivo aqui é dar a escala, não outra grade.
  const ampsLabels = [0, ampsMax].map((val) => {
    const y = (plotY1 - (val / ampsMax) * (plotY1 - plotY0) + 3).toFixed(1);
    return `<text x="${plotX1 + 5}" y="${y}" class="history-axis-label history-axis-label-amps">${Math.round(val)}A</text>`;
  }).join("");

  // Eixo X: início da janela, meio e "agora" — dá noção real de
  // quanto tempo o gráfico cobre sem precisar ler o texto do legend.
  const tEnd = samples[samples.length - 1].t;
  const tMid = samples[Math.floor(samples.length / 2)].t;
  const xTicks = [
    { x: plotX0, label: formatSecondsAgo(tEnd - samples[0].t), anchor: "start" },
    { x: (plotX0 + plotX1) / 2, label: formatSecondsAgo(tEnd - tMid), anchor: "middle" },
    { x: plotX1, label: "agora", anchor: "end" },
  ];
  const xLabels = xTicks.map(({ x, label, anchor }) =>
    `<text x="${x}" y="${plotY1 + 14}" class="history-axis-label history-axis-label-time" text-anchor="${anchor}">${label}</text>`
  ).join("");

  // Ponto atual destacado em cada série — ancora visualmente onde o
  // valor do legend está de fato "pousando" na linha.
  const [lastSocX, lastSocY] = socPoints.split(" ").pop().split(",");
  const [lastAmpsX, lastAmpsY] = ampsPoints.split(" ").pop().split(",");

  svg.innerHTML = `
    ${gridLines}
    <polygon points="${socAreaPoints}" class="history-area-soc" />
    <polyline points="${offeredPoints}" class="history-line history-line-offered" />
    <polyline points="${ampsPoints}" class="history-line history-line-amps" />
    <polyline points="${socPoints}" class="history-line history-line-soc" />
    <circle cx="${lastSocX}" cy="${lastSocY}" r="2.4" class="history-dot history-dot-soc" />
    <circle cx="${lastAmpsX}" cy="${lastAmpsY}" r="2.4" class="history-dot history-dot-amps" />
    ${socLabels}
    ${ampsLabels}
    ${xLabels}
  `;

  const last = samples[samples.length - 1];
  nowSoc.textContent = `${last.soc}%`;
  nowAmps.textContent = `${last.actual_amps}A`;
  if (nowOffered) nowOffered.textContent = `${last.offered_amps}A`;

  const spanMin = Math.max(0, (last.t - samples[0].t) / 60);
  windowLabel.textContent = spanMin < 1 ? "últimos segundos" : `últimos ${Math.round(spanMin)}min`;
}

// ── Construção/atualização de cards (diff, não innerHTML) ───────────
// displayStatus e buildFaultOptions agora vivem em pure.js.

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
      <div class="card-top-right">
        <span class="pill" data-role="pill"></span>
        <button class="icon-btn" data-role="history-toggle" type="button" title="Ver histórico ampliado (aba Histórico)">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 13.5V2.5h1v10h11.5v1H2ZM4 11l2.8-4 2.4 2.8L13.5 3l.8.6-5.3 6.9-2.4-2.8L4.8 11.7 4 11Z"/></svg>
        </button>
        <button class="icon-btn" data-role="chaos-toggle" type="button" title="Ajustar chaos deste charger">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1 6.6 6H2l3.6 3-1.4 5L8 11l3.8 3-1.4-5L14 6H9.4L8 1Z"/></svg>
          <span class="chaos-badge" data-role="chaos-badge" hidden></span>
        </button>
      </div>
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
    <div class="chaos-panel" data-role="chaos-panel" hidden>
      <p class="chaos-hint">Aplica em tempo real, sem remover/readicionar. 0 desliga cada chaos individualmente.</p>
      <p class="chaos-active-summary" data-role="chaos-active-summary"></p>
      <div class="chaos-grid">
        <label data-chaos-group="disconnect">Desconexão a cada (s)
          <input type="number" min="0" step="1" data-role="chaos-disconnect-interval">
        </label>
        <label data-chaos-group="disconnect">± jitter (s)
          <input type="number" min="0" step="1" data-role="chaos-disconnect-jitter">
        </label>
        <label data-chaos-group="latency">Latência mín (ms)
          <input type="number" min="0" step="1" data-role="chaos-latency-min">
        </label>
        <label data-chaos-group="latency">Latência máx (ms)
          <input type="number" min="0" step="1" data-role="chaos-latency-max">
        </label>
        <label data-chaos-group="drop">Perda de msg (0–1)
          <input type="number" min="0" max="1" step="0.05" data-role="chaos-drop-rate">
        </label>
      </div>
      <div class="chaos-grid chaos-grid-secondary">
        <label>Teto fila offline <span class="chaos-field-note">(config, não é chaos)</span>
          <input type="number" min="0" step="1" data-role="chaos-max-queue">
        </label>
      </div>
      <div class="chaos-actions">
        <button class="btn-primary" data-role="chaos-apply" type="button">Aplicar</button>
        <span class="chaos-status" data-role="chaos-status"></span>
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
        <label class="phases-label" for="phases-${c.charge_point_id}">Fases</label>
        <select id="phases-${c.charge_point_id}" data-role="phases-select" title="Número de fases — afeta todo cálculo de potência, aplica ao vivo">
          <option value="1">1 (monofásico)</option>
          <option value="2">2</option>
          <option value="3">3 (trifásico)</option>
        </select>
      </div>
      <div class="btn-row">
        <button data-role="btn-disconnect">Disconnect</button>
        <button class="danger" data-role="btn-remove">Remover</button>
      </div>
    </div>`;

  const chargeId = c.charge_point_id;
  const idTagInput = qr(el, "id-tag");
  const faultSelect = qr(el, "fault-select");
  const phasesSelect = qr(el, "phases-select");
  idTagInput.addEventListener("input", () => { idTagInputs[chargeId] = idTagInput.value; });
  faultSelect.addEventListener("change", () => { faultSelections[chargeId] = faultSelect.value; });
  // Aplica imediatamente ao trocar (sem botão "Aplicar" separado, ao
  // contrário do painel de chaos) — é um único campo, não um grupo de
  // valores relacionados, então a fricção extra de um botão dedicado
  // não compensa. setPhases() já cuida do toast de sucesso/erro.
  phasesSelect.addEventListener("change", () => setPhases(chargeId, phasesSelect));

  qr(el, "btn-start").addEventListener("click", () =>
    sendCommand(chargeId, "start", [idTagInput.value]));
  qr(el, "btn-stop").addEventListener("click", () =>
    sendCommand(chargeId, "stop", []));
  qr(el, "btn-pause").addEventListener("click", () =>
    sendCommand(chargeId, "pause", []));
  qr(el, "btn-resume").addEventListener("click", () =>
    sendCommand(chargeId, "resume", []));
  qr(el, "btn-fault").addEventListener("click", () =>
    sendCommand(chargeId, "fault", [faultSelect.value]));
  qr(el, "btn-clear").addEventListener("click", () =>
    sendCommand(chargeId, "clear", []));
  qr(el, "btn-disconnect").addEventListener("click", () =>
    sendCommand(chargeId, "disconnect", []));
  qr(el, "btn-remove").addEventListener("click", () =>
    removeCharger(chargeId));

  qr(el, "history-toggle").addEventListener("click", () =>
    openHistoryView(chargeId));

  qr(el, "chaos-toggle").addEventListener("click", () =>
    toggleChaosPanel(el));
  qr(el, "chaos-apply").addEventListener("click", () =>
    applyChaos(chargeId, el));

  updateCard(el, c);
  return el;
}

function updateCard(el, c) {
  const status = displayStatus(c);
  const hasTx = c.active_transaction_id !== null;

  el.dataset.status = status;

  // Reflete o valor ATUAL de number_of_phases (pode ter sido ajustado
  // ao vivo por outra aba/pessoa) — diferente do painel de chaos, um
  // <select> não tem o problema de "digitação interrompida" ao
  // reaplicar a cada snapshot, então é seguro sincronizar sempre.
  // Só pula enquanto o próprio select está focado, pra não fechar o
  // dropdown embaixo do dedo/mouse do usuário no meio de uma escolha.
  const phasesSelect = qr(el, "phases-select");
  if (phasesSelect && document.activeElement !== phasesSelect) {
    phasesSelect.value = String(c.number_of_phases ?? 1);
  }

  // Guardado, não aplicado ao formulário aqui — ver toggleChaosPanel().
  // Reaplicar a cada snapshot (a cada ~1s com sessão ativa) tornaria
  // impossível digitar num campo enquanto o painel de chaos está aberto.
  const chaos = c.chaos || {};
  el._chaosSnapshot = chaos;
  updateChaosIndicators(el, chaos);

  const led = qr(el, "led");
  led.className = `led ${status}`;

  const pill = qr(el, "pill");
  pill.className = `pill ${status}`;
  pill.textContent = c.online ? (STATUS_LABEL[c.status] || c.status) : "Offline";

  // Amplitude reflete a corrente real puxada (c.actual_amps) contra um
  // teto de referência de 32A (AC monofásico/trifásico comum) — só
  // usado quando "charging" (as outras classes de status fixam sua
  // própria amplitude em CSS, ver .wave[data-status=...] .wave-path).
  const wave = qr(el, "wave");
  wave.dataset.status = status;
  const ampRatio = Math.min(1, (c.actual_amps || 0) / 32);
  wave.querySelectorAll(".wave-path").forEach((p) => {
    p.style.setProperty("--wave-amp", (0.15 + ampRatio * 0.7).toFixed(2));
  });

  const gauge = qr(el, "gauge");
  const cells = gauge.children;
  const filledCount = Math.round((c.soc_percent / 100) * 10);
  for (let i = 0; i < cells.length; i++) {
    const filled = i < filledCount;
    cells[i].className = "gauge-cell" + (filled ? " filled" : "") + (filled && status === "charging" ? " charging" : "");
  }
  qr(el, "soc-value").textContent = `${c.soc_percent}%`;

  qr(el, "energy").textContent = `${(c.energy_wh / 1000).toFixed(2)} kWh`;
  qr(el, "current").textContent = `${c.actual_amps}A / ${c.offered_amps}A`;
  qr(el, "queue").textContent = String(c.queue_len);

  qr(el, "btn-start").disabled = hasTx;
  qr(el, "btn-stop").disabled = !hasTx;
  qr(el, "btn-pause").disabled = !(hasTx && !c.session_suspended);
  qr(el, "btn-resume").disabled = !(hasTx && c.session_suspended);
  qr(el, "btn-clear").disabled = status !== "faulted";
  qr(el, "btn-disconnect").disabled = !c.online;

  const matchesFilter = !currentFilter || c.charge_point_id.toLowerCase().includes(currentFilter);
  el.style.display = matchesFilter ? "" : "none";
}

function renderEmptyState(hasFilterButNoMatch) {
  const grid = $("grid");
  cardElements.clear();
  grid.innerHTML = hasFilterButNoMatch
    ? `<div class="empty">
         <svg class="empty-mark" viewBox="0 0 32 32" aria-hidden="true"><path d="M13 2 L5 18 H13 L11 30 L27 12 H17 L19 2 Z" /></svg>
         <strong>Nenhum charger corresponde ao filtro.</strong>
         <span>Tente outro termo de busca.</span>
       </div>`
    : `<div class="empty">
         <svg class="empty-mark" viewBox="0 0 32 32" aria-hidden="true"><path d="M13 2 L5 18 H13 L11 30 L27 12 H17 L19 2 Z" /></svg>
         <strong>Nenhum charger conectado.</strong>
         <span>Digite um ID acima e clique em "+ Adicionar" para começar a simular.</span>
       </div>`;
}

function renderTable(chargers) {
  const tbody = $("dense-table-body");
  const filtered = chargers.filter(
    (c) => !currentFilter || c.charge_point_id.toLowerCase().includes(currentFilter)
  );
  if (filtered.length === 0) {
    const msg = currentFilter
      ? "Nenhum charger corresponde ao filtro."
      : "Nenhum charger conectado.";
    tbody.innerHTML = `<tr class="empty-row"><td colspan="7">${escapeHtml(msg)}</td></tr>`;
    return;
  }

  const sorted = sortChargers(filtered, currentSort);
  tbody.innerHTML = sorted.map((c) => {
    const status = displayStatus(c);
    const label = c.online ? (STATUS_LABEL[c.status] || c.status) : "Offline";
    return `
      <tr data-id="${escapeAttr(c.charge_point_id)}">
        <td><span class="led ${status}"></span></td>
        <td class="dt-id">${escapeHtml(c.charge_point_id)}</td>
        <td><span class="pill ${status}">${escapeHtml(label)}</span></td>
        <td class="dt-num">${c.soc_percent}%</td>
        <td class="dt-num">${c.actual_amps}A / ${c.offered_amps}A</td>
        <td class="dt-num">${(c.energy_wh / 1000).toFixed(2)} kWh</td>
        <td class="dt-num">${c.queue_len}</td>
      </tr>`;
  }).join("");

  // Clicar na linha filtra pelo ID exato e volta pro modo card — a
  // tabela é pra escanear/localizar, não pra agir (não duplica os
  // botões de start/stop/fault/etc. de createCard aqui).
  tbody.querySelectorAll("tr[data-id]").forEach((row) => {
    row.addEventListener("click", () => {
      const id = row.dataset.id;
      $("search-input").value = id;
      currentFilter = id.toLowerCase();
      applyViewMode("cards");
    });
  });
}

// Alterna entre modo card (padrão, detalhado, ideal até ~20 chargers)
// e modo tabela (1 linha por charger, só telemetria — pra escanear uma
// frota grande sem ter que rolar dezenas de cards). #grid usa o
// atributo `hidden` normal do HTML; ver `#grid[hidden]` em style.css
// (precisa de override explícito porque a regra de ID `#grid { display:
// grid }` tem especificidade maior que o `[hidden] { display: none }`
// padrão do navegador).
function applyViewMode(mode) {
  viewMode = mode;
  $("view-mode-cards").classList.toggle("active", mode === "cards");
  $("view-mode-table").classList.toggle("active", mode === "table");
  syncGrid(lastChargers);
}

// Estado inicial, ANTES do 1º snapshot chegar (via refresh()/GET
// /api/state ou o 1º evento do SSE, o que vier primeiro) — sem isso, a
// tela ficava em branco por um instante e depois, se a frota realmente
// estivesse vazia, mostrava o MESMO texto de renderEmptyState(false)
// ("Nenhum charger conectado"): ambíguo entre "ainda não sei" e "sei
// que não tem nenhum". syncGrid() (chamado pelo 1º refresh()/evento)
// substitui isso pelo estado real assim que os dados chegam — não
// precisa de nenhuma lógica extra de transição, o innerHTML é só
// sobrescrito na próxima renderização, exatamente como já acontecia
// entre renderEmptyState() e a grade populada.
function renderLoadingState() {
  const grid = $("grid");
  cardElements.clear();
  grid.innerHTML = `<div class="empty empty-loading"><strong>Carregando frota…</strong><span>Conectando ao painel de controle.</span></div>`;
}

function updateStatsStrip(chargers) {
  const strip = $("stats-strip");
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

// sortChargers agora vive em pure.js (default "id" lá é só pra uso
// isolado em teste — aqui sempre passamos currentSort explicitamente,
// ver syncGrid()).


function syncGrid(chargers) {
  lastChargers = chargers;
  updateStatsStrip(chargers);

  // Mantém a aba Histórico sincronizada mesmo quando ela não está
  // ativa no momento (troca de aba não deve mostrar dado velho) — só
  // reinicia o poll se o charger selecionado de fato mudou (ex: foi
  // removido e outro assumiu o lugar), não a cada snapshot.
  const chargerBeforeSync = historyViewChargeId;
  populateHistoryChargerSelect(chargers);
  populateCompareList(chargers);
  if (activeView === "history") {
    updateHistoryViewHeader();
    if (historyMode === "single" && historyViewChargeId !== chargerBeforeSync) startHistoryViewPolling();
  }

  const grid = $("grid");
  if (chargers.length === 0) {
    // Frota vazia: sempre mostra o empty-state no #grid, mesmo em modo
    // tabela — não faria sentido mostrar uma tabela com só cabeçalho;
    // a mensagem de "como adicionar" é mais útil que linhas vazias.
    grid.hidden = false;
    $("dense-table-wrap").hidden = true;
    renderEmptyState(false);
    updateBulkCountLabel();
    return;
  }
  if (grid.querySelector(".empty")) {
    grid.innerHTML = "";
  }

  // Modo tabela: rebuild simples e direto (ver renderTable) em vez do
  // diffing de cards abaixo — não precisa manter/animar 300 elementos
  // de card fora de tela só pra escanear status de uma frota grande.
  // Os cards continuam intactos em cardElements, prontos assim que o
  // usuário volta pro modo card (ver applyViewMode).
  if (viewMode === "table") {
    grid.hidden = true;
    $("dense-table-wrap").hidden = false;
    renderTable(chargers);
    updateBulkCountLabel();
    return;
  }
  grid.hidden = false;
  $("dense-table-wrap").hidden = true;

  const sorted = sortChargers(chargers, currentSort);
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
  const dot = $("sse-dot");
  const label = $("link-status-label");
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

$("add-charger-btn").addEventListener("click", addCharger);
$("new-charger-id").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addCharger();
});
$("import-file-btn").addEventListener("click", () => {
  $("import-file-input").click();
});
$("import-file-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (file) await importChargersFromFile(file);
  // Zera o input — sem isso, escolher o MESMO arquivo de novo em
  // seguida não dispara "change" (o browser considera que o valor não
  // mudou), então uma 2ª tentativa de importar o mesmo .txt ficaria
  // silenciosamente sem efeito.
  e.target.value = "";
});
$("add-advanced-toggle").addEventListener("click", () => {
  const row = $("advanced-add-row");
  row.hidden = !row.hidden;
});
$("search-input").addEventListener("input", (e) => {
  currentFilter = e.target.value.trim().toLowerCase();
  if (viewMode === "table") {
    renderTable(lastChargers);
  } else {
    for (const [id, el] of cardElements) {
      el.style.display = (!currentFilter || id.toLowerCase().includes(currentFilter)) ? "" : "none";
    }
  }
  updateBulkCountLabel();
});
$("sort-select").addEventListener("change", (e) => {
  currentSort = e.target.value;
  syncGrid(lastChargers); // reordena na hora, sem esperar o próximo evento do SSE
});
$("view-mode-cards").addEventListener("click", () => applyViewMode("cards"));
$("view-mode-table").addEventListener("click", () => applyViewMode("table"));
$("token-btn").addEventListener("click", promptForToken);
$("theme-toggle-btn").addEventListener("click", toggleTheme);
$("bulk-fault-select").innerHTML = buildFaultOptions(FAULT_CODES[0]);

document.querySelectorAll(".view-tab").forEach((btn) => {
  btn.addEventListener("click", () => setActiveView(btn.dataset.view));
});
$("history-charger-select").addEventListener("change", (e) => {
  selectHistoryCharger(e.target.value);
});
$("history-mode-single").addEventListener("click", () => setHistoryMode("single"));
$("history-mode-compare").addEventListener("click", () => setHistoryMode("compare"));
$("history-compare-metric-select").innerHTML = buildMetricOptions(compareMetric);
$("history-compare-metric-select").addEventListener("change", (e) => {
  compareMetric = e.target.value;
  fetchAndRenderCompare();
});
// Delegação num único listener no container (em vez de um por
// checkbox) — a lista inteira é reconstruída via innerHTML sempre que
// o conjunto de chargers muda (ver populateCompareList), então
// listeners individuais seriam perdidos a cada reconstrução.
$("history-compare-list").addEventListener("change", (e) => {
  const input = e.target.closest("input[type=checkbox]");
  if (input) toggleCompareCharger(input.value, input.checked);
});
$("history-prev-btn").addEventListener("click", () => {
  const select = $("history-charger-select");
  const options = Array.from(select.options);
  if (options.length === 0) return;
  const i = options.findIndex((o) => o.value === historyViewChargeId);
  const next = options[(i - 1 + options.length) % options.length];
  selectHistoryCharger(next.value);
});
$("history-next-btn").addEventListener("click", () => {
  const select = $("history-charger-select");
  const options = Array.from(select.options);
  if (options.length === 0) return;
  const i = options.findIndex((o) => o.value === historyViewChargeId);
  const next = options[(i + 1) % options.length];
  selectHistoryCharger(next.value);
});

const bulkStartBtn = qr(document, "bulk-start");
const bulkStopBtn = qr(document, "bulk-stop");
const bulkPauseBtn = qr(document, "bulk-pause");
const bulkResumeBtn = qr(document, "bulk-resume");
const bulkDisconnectBtn = qr(document, "bulk-disconnect");
const bulkFaultBtn = qr(document, "bulk-fault");
const bulkClearBtn = qr(document, "bulk-clear");

bulkStartBtn.addEventListener("click", () => {
  const idTag = $("bulk-id-tag").value.trim() || "LOCAL_TAG";
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
  const code = $("bulk-fault-select").value;
  sendBulkCommand("fault", {
    args: [code],
    button: bulkFaultBtn,
    confirmMessage: `Colocar TODOS os chargers visíveis em Faulted (${code})? Use "Clear" depois pra voltar ao normal.`,
  });
});
bulkClearBtn.addEventListener("click", () =>
  sendBulkCommand("clear", { button: bulkClearBtn }));

renderLoadingState();  // esqueleto até o 1º snapshot chegar (ver acima)
refresh();            // 1ª pintura imediata, antes do stream conectar
connectEventStream(); // daqui pra frente, toda atualização vem por push
