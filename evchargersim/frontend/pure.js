/*
 * evchargersim/frontend/pure.js — funções e constantes SEM dependência
 * de DOM/window/document, extraídas de app.js pra poderem ser testadas
 * isoladamente (ver tests/app.test.js) sem precisar simular um browser
 * inteiro (jsdom ou afins) — nenhuma delas lê ou escreve em elemento
 * nenhum, só transforma dados.
 *
 * Carregado como <script> normal, ANTES de app.js, em index.html — sem
 * build step, sem bundler, mesmo padrão de "arquivo solto no escopo
 * global" do resto do frontend. app.js continua usando estas funções
 * como globais (FAULT_CODES, sortChargers, etc.), exatamente como
 * quando elas viviam dentro dele.
 *
 * O bloco no final (`if (typeof module !== "undefined")`) só existe
 * pro Node conseguir dar `require()` neste arquivo a partir de
 * tests/app.test.js — no browser, `module` não existe, então aquele
 * bloco inteiro é ignorado silenciosamente e nunca roda.
 */

const FAULT_CODES = ["ground_failure", "over_current_failure", "over_voltage",
  "connector_lock_failure", "power_meter_failure", "weak_signal", "other_error"];

const STATUS_LABEL = {
  charging: "Carregando", suspended: "Suspenso", available: "Disponível",
  faulted: "Falha", inoperative: "Inoperativo",
};

// Ordem de prioridade quando o sort é "status" — falha primeiro (o que
// mais precisa de atenção), offline por último.
const STATUS_SORT_RANK = { faulted: 0, charging: 1, suspended: 2, available: 3, inoperative: 4 };

function escapeHtml(value) {
  return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/"/g, "&quot;");
}

// Parseia o textarea de "adicionar em lote" / o .txt importado: um ID
// por linha, ou vários separados por vírgula (ou os dois misturados) —
// remove espaços em volta, ignora linhas vazias, remove duplicatas
// mantendo a 1ª ocorrência (preserva a ordem que o usuário digitou).
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

function displayStatus(c) {
  return c.online ? c.status : "offline";
}

function buildFaultOptions(selected) {
  return FAULT_CODES.map((f) =>
    `<option value="${f}" ${f === selected ? "selected" : ""}>${f}</option>`
  ).join("");
}

// Mapeia uma série de valores pro espaço [x0,x1]×[y0,y1] de um SVG —
// usado pelas 3 linhas do gráfico de histórico (SoC, corrente real,
// limite ofertado). minVal/maxVal fixos (ex: 0–100 pro SoC) ou null
// pra escala dinâmica (min/max da própria série, com folga mínima de
// 1e-6 pra não dividir por zero quando todos os valores são iguais).
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

// ── Comparação de histórico entre chargers (aba Histórico > Comparar) ──
//
// Paleta fixa reaproveitada por índice (módulo do tamanho) quando o
// número de chargers selecionados excede a paleta — cada charger
// selecionado recebe sempre a MESMA cor entre re-renderizações, porque
// o índice vem da posição no conjunto ORDENADO de todos os chargers da
// frota (populateCompareList em app.js), não da ordem em que foram
// marcados na caixa de seleção.
const COMPARE_PALETTE = [
  "#c17f3a", "#2e9e6b", "#4f8fd9", "#d94f4f",
  "#d9a441", "#8f6fd9", "#3fb6c9", "#b3d94f",
];

function colorForCompareIndex(index) {
  return COMPARE_PALETTE[index % COMPARE_PALETTE.length];
}

// Métrica comparável -> rótulo/unidade/escala — fonte única usada tanto
// pra montar o <select> de métrica (buildMetricOptions) quanto pros
// rótulos de eixo/legend do gráfico de comparação. min/max fixos (SoC:
// sempre 0–100%) ou null pra escala dinâmica calculada a partir dos
// valores de fato presentes nas séries selecionadas.
const COMPARE_METRICS = {
  soc: { label: "SoC", unit: "%", min: 0, max: 100 },
  actual_amps: { label: "Corrente real", unit: "A", min: 0, max: null },
  offered_amps: { label: "Limite ofertado", unit: "A", min: 0, max: null },
  power_kw: { label: "Potência", unit: "kW", min: 0, max: null },
};

function buildMetricOptions(selected) {
  return Object.entries(COMPARE_METRICS).map(([key, def]) =>
    `<option value="${key}" ${key === selected ? "selected" : ""}>${def.label} (${def.unit})</option>`
  ).join("");
}

function formatCompareValue(metric, value) {
  if (value === undefined || value === null || Number.isNaN(value)) return "—";
  const unit = COMPARE_METRICS[metric] ? COMPARE_METRICS[metric].unit : "";
  return `${value}${unit}`;
}

// Intervalo de tempo (timestamp mín/máx) cobrindo TODAS as séries
// passadas — usado pra alinhar o eixo X de vários chargers no MESMO
// referencial de tempo real no gráfico de comparação. Sem isso, cada
// série usaria seu próprio início/fim (como buildLinePoints faz pra
// uma série só), o que desalinharia visualmente chargers que começaram
// a reportar em momentos diferentes — a comparação ficaria enganosa.
// Retorna null se nenhuma amostra existir em nenhuma série.
function combinedTimeRange(sampleArrays) {
  let tMin = null;
  let tMax = null;
  for (const samples of sampleArrays) {
    for (const s of samples) {
      if (tMin === null || s.t < tMin) tMin = s.t;
      if (tMax === null || s.t > tMax) tMax = s.t;
    }
  }
  if (tMin === null) return null;
  return { tMin, tMax };
}

// Mapeia UMA série de amostras (cada uma com timestamp real "t") pro
// espaço [x0,x1]×[y0,y1] de um SVG usando um referencial de tempo
// COMUM (tMin/tMax, ver combinedTimeRange) — diferente de
// buildLinePoints, que espaça pontos só pelo ÍNDICE assumindo uma
// única série igualmente amostrada. minVal/maxVal fixos ou null pra
// escala dinâmica (min/max da própria série, mesma folga de 1e-6 de
// buildLinePoints pra não dividir por zero).
function buildTimeSeriesPoints(samples, valueKey, x0, x1, y0, y1, tMin, tMax, minVal, maxVal) {
  if (!samples.length) return "";
  let lo = minVal, hi = maxVal;
  if (lo == null || hi == null) {
    const vals = samples.map((s) => s[valueKey]);
    lo = Math.min(...vals);
    hi = Math.max(...vals);
  }
  if (hi - lo < 1e-6) hi = lo + 1;
  const tSpan = tMax - tMin;
  return samples.map((s) => {
    const x = tSpan < 1e-6 ? x0 : x0 + ((s.t - tMin) / tSpan) * (x1 - x0);
    const y = y1 - ((s[valueKey] - lo) / (hi - lo)) * (y1 - y0);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}

// Ordena conforme sortBy ("id" | "status" | "soc") — ID é sempre o
// desempate, pra ordem estável entre re-renderizações.
function sortChargers(chargers, sortBy = "id") {
  const arr = [...chargers];
  if (sortBy === "status") {
    arr.sort((a, b) => {
      const ra = a.online ? (STATUS_SORT_RANK[a.status] ?? 5) : 6;
      const rb = b.online ? (STATUS_SORT_RANK[b.status] ?? 5) : 6;
      return ra !== rb ? ra - rb : a.charge_point_id.localeCompare(b.charge_point_id);
    });
  } else if (sortBy === "soc") {
    arr.sort((a, b) => b.soc_percent - a.soc_percent || a.charge_point_id.localeCompare(b.charge_point_id));
  } else {
    arr.sort((a, b) => a.charge_point_id.localeCompare(b.charge_point_id));
  }
  return arr;
}

// ── Exporta pra Node (tests/app.test.js) — ignorado no browser ──────
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    FAULT_CODES, STATUS_LABEL, STATUS_SORT_RANK,
    escapeHtml, escapeAttr, parseChargerIdsFromText,
    displayStatus, buildFaultOptions, buildLinePoints,
    formatSecondsAgo, sortChargers,
    COMPARE_PALETTE, COMPARE_METRICS, colorForCompareIndex, buildMetricOptions,
    formatCompareValue, combinedTimeRange, buildTimeSeriesPoints,
  };
}
