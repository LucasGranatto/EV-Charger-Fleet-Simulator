// tests/pure.test.js — testes unitários das funções sem dependência de
// DOM extraídas de app.js pra pure.js (ver pure.js). Roda com o test
// runner embutido do Node (>=18), sem instalar nenhuma dependência
// nova nem precisar de build step — mesma filosofia "sem bundler" do
// resto do projeto:
//
//   node --test tests/
//
// Cobre só as funções que valem a pena testar isoladamente: lógica
// pura, sem toque em DOM/rede/tempo real. O resto do app.js (wiring de
// eventos, fetch, SSE) continua sem cobertura automatizada — precisaria
// de um DOM simulado (jsdom ou puppeteer) pra fazer sentido, e não é o
// que este primeiro passo se propõe a resolver.

const { test, describe } = require("node:test");
const assert = require("node:assert/strict");

const {
  FAULT_CODES,
  escapeHtml,
  escapeAttr,
  parseChargerIdsFromText,
  displayStatus,
  buildFaultOptions,
  buildLinePoints,
  formatSecondsAgo,
  sortChargers,
} = require("../pure.js");

describe("escapeHtml", () => {
  test("escapa &, < e > (nessa ordem, sem dupla-escapar)", () => {
    assert.equal(escapeHtml("<script>a & b</script>"), "&lt;script&gt;a &amp; b&lt;/script&gt;");
  });

  test("não mexe em texto sem caracteres especiais", () => {
    assert.equal(escapeHtml("CH01"), "CH01");
  });

  test("converte valores não-string (número) via String()", () => {
    assert.equal(escapeHtml(42), "42");
  });
});

describe("escapeAttr", () => {
  test("também escapa aspas duplas, além do que escapeHtml já cobre", () => {
    assert.equal(escapeAttr(`diz "oi" & <tudo>`), "diz &quot;oi&quot; &amp; &lt;tudo&gt;");
  });
});

describe("parseChargerIdsFromText", () => {
  test("aceita um ID por linha", () => {
    assert.deepEqual(parseChargerIdsFromText("CH01\nCH02\nCH03"), ["CH01", "CH02", "CH03"]);
  });

  test("aceita IDs separados por vírgula", () => {
    assert.deepEqual(parseChargerIdsFromText("CH01, CH02,CH03"), ["CH01", "CH02", "CH03"]);
  });

  test("tolera os dois formatos misturados, linhas em branco e espaços em volta", () => {
    const input = "  CH01 \n\nCH02, CH03\n , CH04\n";
    assert.deepEqual(parseChargerIdsFromText(input), ["CH01", "CH02", "CH03", "CH04"]);
  });

  test("remove duplicatas preservando a 1ª ocorrência", () => {
    assert.deepEqual(parseChargerIdsFromText("CH01,CH02,CH01,CH03,CH02"), ["CH01", "CH02", "CH03"]);
  });

  test("string vazia ou só espaços/vírgulas resulta em lista vazia", () => {
    assert.deepEqual(parseChargerIdsFromText("   \n , ,\n"), []);
  });
});

describe("displayStatus", () => {
  test("charger online mostra o status real", () => {
    assert.equal(displayStatus({ online: true, status: "charging" }), "charging");
  });

  test("charger offline sempre mostra 'offline', ignorando o campo status", () => {
    assert.equal(displayStatus({ online: false, status: "charging" }), "offline");
  });
});

describe("buildFaultOptions", () => {
  test("gera uma <option> por código de falha conhecido", () => {
    const html = buildFaultOptions(null);
    for (const code of FAULT_CODES) {
      assert.match(html, new RegExp(`<option value="${code}"[^>]*>${code}</option>`));
    }
  });

  test("marca 'selected' só na opção que bate com o valor passado", () => {
    const html = buildFaultOptions("over_voltage");
    assert.match(html, /<option value="over_voltage" selected>/);
    // as demais não devem ter selected
    const others = FAULT_CODES.filter((c) => c !== "over_voltage");
    for (const code of others) {
      assert.match(html, new RegExp(`<option value="${code}" >`));
    }
  });
});

describe("buildLinePoints", () => {
  test("mapeia série crescente pro topo->base esperado (Y invertido: maior valor = Y menor)", () => {
    const points = buildLinePoints([0, 50, 100], 0, 100, 0, 100, 0, 100);
    const parsed = points.split(" ").map((p) => p.split(",").map(Number));
    assert.deepEqual(parsed[0], [0, 100]);   // valor mínimo -> base do gráfico (y=100)
    assert.deepEqual(parsed[1], [50, 50]);   // valor do meio -> meio do gráfico
    assert.deepEqual(parsed[2], [100, 0]);   // valor máximo -> topo do gráfico (y=0)
  });

  test("série constante não gera divisão por zero (achata a escala)", () => {
    const points = buildLinePoints([5, 5, 5], 0, 100, 0, 100, null, null);
    assert.doesNotThrow(() => points);
    for (const [, y] of points.split(" ").map((p) => p.split(",").map(Number))) {
      assert.ok(Number.isFinite(y));
    }
  });

  test("1 único valor não divide por zero no eixo X", () => {
    const points = buildLinePoints([42], 0, 100, 0, 100, 0, 100);
    const [x] = points.split(",").map(Number);
    assert.equal(x, 0);
  });

  test("usa min/max da própria série quando minVal/maxVal são omitidos", () => {
    const points = buildLinePoints([10, 20, 30], 0, 100, 0, 100, null, null);
    const parsed = points.split(" ").map((p) => p.split(",").map(Number));
    assert.deepEqual(parsed[0], [0, 100]);
    assert.deepEqual(parsed[2], [100, 0]);
  });
});

describe("formatSecondsAgo", () => {
  test("menos de 5s vira 'agora'", () => {
    assert.equal(formatSecondsAgo(0), "agora");
    assert.equal(formatSecondsAgo(4.9), "agora");
  });

  test("entre 5s e 1min mostra segundos", () => {
    assert.equal(formatSecondsAgo(45), "-45s");
  });

  test("1min ou mais mostra minutos arredondados", () => {
    assert.equal(formatSecondsAgo(60), "-1min");
    assert.equal(formatSecondsAgo(150), "-3min");
  });
});

describe("sortChargers", () => {
  const chargers = [
    { charge_point_id: "CH03", online: true, status: "available", soc_percent: 10 },
    { charge_point_id: "CH01", online: true, status: "faulted", soc_percent: 90 },
    { charge_point_id: "CH02", online: false, status: "charging", soc_percent: 50 },
  ];

  test("default (sem 2º argumento) ordena por ID", () => {
    const sorted = sortChargers(chargers);
    assert.deepEqual(sorted.map((c) => c.charge_point_id), ["CH01", "CH02", "CH03"]);
  });

  test("'status': faulted primeiro, offline sempre por último, independente do status reportado", () => {
    const sorted = sortChargers(chargers, "status");
    assert.deepEqual(sorted.map((c) => c.charge_point_id), ["CH01", "CH03", "CH02"]);
  });

  test("'soc': maior SoC primeiro", () => {
    const sorted = sortChargers(chargers, "soc");
    assert.deepEqual(sorted.map((c) => c.charge_point_id), ["CH01", "CH02", "CH03"]);
  });

  test("não muta o array original (retorna uma cópia)", () => {
    const original = [...chargers];
    sortChargers(chargers, "soc");
    assert.deepEqual(chargers, original);
  });

  test("empate é desempatado por ID (ordem estável entre re-renderizações)", () => {
    const tied = [
      { charge_point_id: "B", online: true, status: "available", soc_percent: 50 },
      { charge_point_id: "A", online: true, status: "available", soc_percent: 50 },
    ];
    assert.deepEqual(sortChargers(tied, "soc").map((c) => c.charge_point_id), ["A", "B"]);
  });
});
