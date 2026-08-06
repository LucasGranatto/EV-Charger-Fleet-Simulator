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
  const input = document.getElementById("new-charger-id");
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

// Extrai IDs de um .txt: um por linha e/ou separados por vírgula
// (os dois formatos ao mesmo tempo são tolerados), linhas em branco e
// espaços em volta descartados, duplicatas removidas preservando a
// 1ª ocorrência — arquivo exportado de qualquer planilha/lista simples
// já funciona sem exigir um formato exato.
function parseChargerIdsFromText(text) {
  const seen = new Set();
  const ids = [];
  for (const raw of text.split(/[\n,]+/)) {
    const id = raw.trim();
    if (id && !seen.has(id)) {
      seen.add(id);
      ids.push(id);
    }
  }
  return ids;
}

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
    const input = cardEl.querySelector(`[data-role="${role}"]`);
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

  const badge = el.querySelector('[data-role="chaos-badge"]');
  badge.hidden = groups.length === 0;
  badge.textContent = String(groups.length);

  const toggleBtn = el.querySelector('[data-role="chaos-toggle"]');
  toggleBtn.title = groups.length === 0
    ? "Ajustar chaos deste charger"
    : `Chaos ativo: ${descriptions.join(" · ")}`;

  const summary = el.querySelector('[data-role="chaos-active-summary"]');
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
  const panel = cardEl.querySelector('[data-role="chaos-panel"]');
  const opening = panel.hidden;
  panel.hidden = !opening;
  if (opening) {
    populateChaosForm(cardEl, cardEl._chaosSnapshot || {});
    cardEl.querySelector('[data-role="chaos-status"]').textContent = "";
  }
}

async function applyChaos(chargeId, cardEl) {
  const btn = cardEl.querySelector('[data-role="chaos-apply"]');
  const statusEl = cardEl.querySelector('[data-role="chaos-status"]');
  const payload = {};
  for (const [role, field] of Object.entries(CHAOS_FIELD_ROLES)) {
    const input = cardEl.querySelector(`[data-role="${role}"]`);
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

// ── Gráfico expansível de histórico (SoC/corrente) por card ─────────
//
// Cada card tem seu próprio botão de toggle (📈) que abre um painel
// com um gráfico de linha simples (sem dependência externa — 2
// <polyline> num <svg> só). Ao contrário do resto do painel, isso NÃO
// vem pelo /api/events: só é buscado sob demanda enquanto o painel
// está aberto, via poll (não dá pra empurrar por SSE algo que só
// interessa quando o usuário está de fato olhando aquele card
// específico, e incluir o histórico de todo mundo em CADA snapshot do
// SSE inflaria o payload à toa).
const historyPollers = new Map(); // charge_point_id -> intervalId
const HISTORY_POLL_MS = 4000;

function stopHistoryPolling(chargeId) {
  const intervalId = historyPollers.get(chargeId);
  if (intervalId) {
    clearInterval(intervalId);
    historyPollers.delete(chargeId);
  }
}

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

function toggleHistoryPanel(chargeId, panel) {
  const opening = panel.hidden;
  panel.hidden = !opening;
  stopHistoryPolling(chargeId); // idempotente — evita 2 timers se o usuário clicar rápido
  if (opening) {
    fetchAndRenderHistory(chargeId, panel);
    historyPollers.set(chargeId, setInterval(() => fetchAndRenderHistory(chargeId, panel), HISTORY_POLL_MS));
  }
}

// Mapeia uma lista de valores para pontos "x,y" de um <polyline>,
// dentro da área de plotagem [x0,x1]×[y0,y1], normalizando pelo
// min/max informado (ou pelo min/max da própria série, se omitido).
// Achata a escala se hi==lo (série constante) pra não dividir por zero.
function buildLinePoints(values, x0, x1, y0, y1, minVal, maxVal) {
  const n = values.length;
  let lo = minVal, hi = maxVal;
  if (lo == null || hi == null) {
    lo = Math.min(...values);
    hi = Math.max(...values);
  }
  if (hi - lo < 1e-6) hi = lo + 1;
  return values.map((v, i) => {
    const x = n === 1 ? x0 : x0 + (i / (n - 1)) * (x1 - x0);
    const y = y1 - ((v - lo) / (hi - lo)) * (y1 - y0);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}

// "há quanto tempo" de um timestamp relativo ao último ponto da série
// — usado nos rótulos do eixo X, pra dar noção real de escala temporal
// em vez de um gráfico "flutuando" sem referência de tempo.
function formatSecondsAgo(secondsAgo) {
  if (secondsAgo < 5) return "agora";
  const minAgo = secondsAgo / 60;
  return minAgo < 1 ? `-${Math.round(secondsAgo)}s` : `-${Math.round(minAgo)}min`;
}

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
  const svg = panel.querySelector('[data-role="history-svg"]');
  const emptyMsg = panel.querySelector('[data-role="history-empty"]');
  const nowSoc = panel.querySelector('[data-role="history-soc-now"]');
  const nowAmps = panel.querySelector('[data-role="history-amps-now"]');
  const nowOffered = panel.querySelector('[data-role="history-offered-now"]');
  const windowLabel = panel.querySelector('[data-role="history-window"]');

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
      <div class="card-top-right">
        <span class="pill" data-role="pill"></span>
        <button class="icon-btn" data-role="history-toggle" type="button" title="Ver histórico de SoC/corrente">
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
    <div class="history-panel" data-role="history-panel" hidden>
      <div class="history-chart-wrap">
        <svg class="history-chart" viewBox="0 0 300 122" preserveAspectRatio="xMidYMid meet" data-role="history-svg" aria-hidden="true"></svg>
        <span class="history-empty" data-role="history-empty">ainda sem amostras suficientes — aguarde o próximo ciclo de MeterValues</span>
      </div>
      <div class="history-legend">
        <span class="legend-item legend-soc"><i></i>SoC <b data-role="history-soc-now">—</b></span>
        <span class="legend-item legend-amps"><i></i>Corrente <b data-role="history-amps-now">—</b></span>
        <span class="legend-item legend-offered"><i></i>Limite <b data-role="history-offered-now">—</b></span>
        <span class="history-window" data-role="history-window"></span>
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

  el.querySelector('[data-role="history-toggle"]').addEventListener("click", () =>
    toggleHistoryPanel(chargeId, el.querySelector('[data-role="history-panel"]')));

  el.querySelector('[data-role="chaos-toggle"]').addEventListener("click", () =>
    toggleChaosPanel(el));
  el.querySelector('[data-role="chaos-apply"]').addEventListener("click", () =>
    applyChaos(chargeId, el));

  updateCard(el, c);
  return el;
}

function updateCard(el, c) {
  const status = displayStatus(c);
  const hasTx = c.active_transaction_id !== null;

  el.dataset.status = status;

  // Guardado, não aplicado ao formulário aqui — ver toggleChaosPanel().
  // Reaplicar a cada snapshot (a cada ~1s com sessão ativa) tornaria
  // impossível digitar num campo enquanto o painel de chaos está aberto.
  const chaos = c.chaos || {};
  el._chaosSnapshot = chaos;
  updateChaosIndicators(el, chaos);

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
      stopHistoryPolling(id);
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
document.getElementById("import-file-btn").addEventListener("click", () => {
  document.getElementById("import-file-input").click();
});
document.getElementById("import-file-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (file) await importChargersFromFile(file);
  // Zera o input — sem isso, escolher o MESMO arquivo de novo em
  // seguida não dispara "change" (o browser considera que o valor não
  // mudou), então uma 2ª tentativa de importar o mesmo .txt ficaria
  // silenciosamente sem efeito.
  e.target.value = "";
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
