/* =====================================================================
   Self-Evolving Trading AI — Dashboard Frontend
   Fetches API, renders Plotly charts, polls every 30s, handles nav
   ===================================================================== */

const POLL_INTERVAL = 30000;
let charts = { equity: null, price: null };

// ---------------------------------------------------------------------------
// Nav
// ---------------------------------------------------------------------------
function initNav() {
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      document.querySelectorAll(".nav-item").forEach((x) => x.classList.remove("active"));
      el.classList.add("active");
      document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
      document.getElementById("page-" + el.dataset.page).classList.add("active");
    });
  });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function fmtTime(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts);
    return d.toLocaleString([], { hour: "2-digit", minute: "2-digit" });
  } catch (e) {
    return String(ts);
  }
}

function fmtDate(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts);
    return d.toLocaleDateString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch (e) {
    return String(ts);
  }
}

function money(v) {
  if (v == null || v === undefined || Number.isNaN(v)) return "$0.00";
  const sign = v < 0 ? "-" : "";
  return sign + "$" + Math.abs(v).toFixed(2);
}

function pct(v) {
  if (v == null || v === undefined || Number.isNaN(v)) return "0.0%";
  return v.toFixed(1) + "%";
}

function pnlClass(v) {
  if (v > 0) return "pnl-positive";
  if (v < 0) return "pnl-negative";
  return "pnl-neutral";
}

// ---------------------------------------------------------------------------
// Fetch with graceful fallback
// ---------------------------------------------------------------------------
async function fetchJson(path) {
  try {
    const res = await fetch(path);
    if (!res.ok) return null;
    return await res.json();
  } catch (e) {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Data loads
// ---------------------------------------------------------------------------
async function loadMetrics() {
  const data = await fetchJson("/api/metrics");
  if (!data) return;

  document.getElementById("kpiTotalTrades").textContent = data.total_trades ?? 0;
  document.getElementById("kpiWinRate").textContent = pct(data.win_rate ?? 0);
  document.getElementById("kpiWinTrades").textContent = data.win_trades ?? 0;
  document.getElementById("kpiLossTrades").textContent = data.loss_trades ?? 0;
  document.getElementById("kpiTotalPnL").textContent = money(data.total_pnl ?? 0);
  document.getElementById("kpiDailyPnL").textContent = money(data.daily_pnl ?? 0);

  document.getElementById("accCorrect").textContent = data.correct_observations ?? 0;
  document.getElementById("accWrong").textContent = data.wrong_observations ?? 0;

  const el = document.getElementById("modeBadge");
  const modeVal = document.getElementById("modeValue");
  if (data.mode === "PAPER") {
    el.className = "mode-badge paper";
    el.textContent = "PAPER TRADING";
    modeVal.textContent = "PAPER";
    modeVal.style.color = "#22c55e";
  } else if (data.mode === "REAL") {
    el.className = "mode-badge real";
    el.textContent = "LIVE TRADING";
    modeVal.textContent = "REAL";
    modeVal.style.color = "#ef4444";
  } else {
    el.className = "mode-badge";
    el.textContent = "—";
    modeVal.textContent = "—";
  }
}

async function loadRegime() {
  const data = await fetchJson("/api/regime");
  if (!data) return;

  const chip = document.getElementById("regimeChip");
  const details = document.getElementById("regimeDetails");

  chip.textContent = (data.label || "unknown").toUpperCase();
  const ind = data.indicators || {};
  const parts = [];
  if (ind.trend != null) parts.push("Trend: " + ind.trend);
  if (ind.volatility != null) parts.push("Vol: " + ind.volatility);
  if (ind.momentum != null) parts.push("Mom: " + ind.momentum);
  details.textContent = parts.length ? parts.join("   •   ") : "—";
  details.textContent += data.timestamp ? "   •   " + fmtDate(data.timestamp) : "";
}

async function loadStrategy() {
  const data = await fetchJson("/api/strategy");
  if (!data) return;

  document.getElementById("strategyId").textContent = data.id || "—";
  document.getElementById("strategyName").textContent = data.name || "No active strategy";
  const meta = [];
  if (data.generation != null) meta.push("Generation " + data.generation);
  if (data.parent_id) meta.push("Parent: " + data.parent_id);
  document.getElementById("strategyMeta").textContent = meta.length ? meta.join("  |  ") : "—";
}

async function loadTrades() {
  const data = await fetchJson("/api/trades?limit=50");
  if (!data) return;

  const body = document.getElementById("tradesTableBody");
  const allBody = document.getElementById("allTradesTableBody");
  body.innerHTML = "";
  allBody.innerHTML = "";

  data.forEach((t) => {
    const row = document.createElement("tr");
    const obs = t.observation || "—";
    const obsClass = t.direction_right === 1 ? "obs-correct" : t.direction_right === 0 ? "obs-wrong" : "obs-neutral";
    const statusClass = t.status === "open" ? "status-open" : "status-filled";
    const sideClass = (t.side || "").toLowerCase() === "buy" ? "side-buy" : "side-sell";

    row.innerHTML = `
      <td>${fmtTime(t.created_at)}</td>
      <td>${escapeHtml(t.symbol || "—")}</td>
      <td class="${sideClass}">${escapeHtml(t.side || "—")}</td>
      <td>${t.entry_price != null ? t.entry_price.toFixed(4) : "—"}</td>
      <td>${t.exit_price != null ? t.exit_price.toFixed(4) : "—"}</td>
      <td class="${pnlClass(t.pnl)}">${money(t.pnl)}</td>
      <td class="${obsClass}">${escapeHtml(obs)}</td>
      <td class="${statusClass}">${escapeHtml((t.status || "—").toUpperCase())}</td>
    `;
    body.appendChild(row);

    const row2 = row.cloneNode(true);
    row2.innerHTML = `
      <td>${fmtTime(t.created_at)}</td>
      <td>${escapeHtml(t.symbol || "—")}</td>
      <td class="${sideClass}">${escapeHtml(t.side || "—")}</td>
      <td>${t.qty != null ? t.qty.toFixed(4) : "—"}</td>
      <td>${t.entry_price != null ? t.entry_price.toFixed(4) : "—"}</td>
      <td>${t.exit_price != null ? t.exit_price.toFixed(4) : "—"}</td>
      <td class="${pnlClass(t.pnl)}">${money(t.pnl)}</td>
      <td class="${obsClass}">${escapeHtml(obs)}</td>
      <td class="${statusClass}">${escapeHtml((t.status || "—").toUpperCase())}</td>
    `;
    allBody.appendChild(row2);
  });
}

async function loadLessons() {
  const data = await fetchJson("/api/lessons?limit=20");
  if (!data) return;
  const list = document.getElementById("lessonsList");
  list.innerHTML = "";
  data.forEach((l) => {
    const item = document.createElement("div");
    item.className = "lesson-item";
    item.innerHTML = `<div>${escapeHtml(l.lesson || "")}</div><div class="meta">${fmtTime(l.created_at)}</div>`;
    list.appendChild(item);
  });
}

async function loadRules() {
  const data = await fetchJson("/api/rules");
  if (!data) return;
  const list = document.getElementById("rulesList");
  list.innerHTML = "";
  data.forEach((r) => {
    const item = document.createElement("div");
    item.className = "rule-item";
    item.innerHTML = `<div>${escapeHtml(r.rule || "")}</div><div class="meta">${fmtTime(r.created_at)}</div>`;
    list.appendChild(item);
  });
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------
async function loadEquityChart() {
  const data = await fetchJson("/api/equity-curve");
  const el = document.getElementById("equityChart");
  if (!charts.equity) {
    charts.equity = el;
  }
  Plotly.newPlot("equityChart", [], {
    template: "plotly_dark",
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    margin: { l: 40, r: 20, t: 20, b: 30 },
    xaxis: { showgrid: false, zeroline: false },
    yaxis: { showgrid: true, gridcolor: "#1e293b", zeroline: false },
    displayModeBar: false,
  });
  if (!data || data.length === 0) return;

  const xs = data.map((d) => d.t);
  const ys = data.map((d) => d.equity);
  Plotly.newPlot("equityChart", [
    {
      x: xs,
      y: ys,
      mode: "lines",
      line: { color: "#38bdf8", width: 2 },
      fill: "tozeroy",
      fillcolor: "rgba(56,189,248,0.1)",
    },
  ], {
    template: "plotly_dark",
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    margin: { l: 40, r: 20, t: 20, b: 30 },
    xaxis: { showgrid: false, zeroline: false },
    yaxis: { showgrid: true, gridcolor: "#1e293b", zeroline: false, tickprefix: "$" },
    displayModeBar: false,
  });
}

async function loadPriceChart() {
  const data = await fetchJson("/api/price-chart/BTC/USD");
  if (!data || data.length === 0) return;

  const xs = data.map((d) => d.t);
  const opens = data.map((d) => d.o);
  const highs = data.map((d) => d.h);
  const lows = data.map((d) => d.l);
  const closes = data.map((d) => d.c);

  Plotly.newPlot("priceChart", [
    {
      type: "candlestick",
      x: xs,
      open: opens,
      high: highs,
      low: lows,
      close: closes,
      increasing: { line: { color: "#22c55e" } },
      decreasing: { line: { color: "#ef4444" } },
    },
  ], {
    template: "plotly_dark",
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    margin: { l: 40, r: 20, t: 20, b: 30 },
    xaxis: { showgrid: false, zeroline: false, rangeslider: { visible: false } },
    yaxis: { showgrid: true, gridcolor: "#1e293b", zeroline: false, tickprefix: "$" },
    displayModeBar: false,
  });
}

// ---------------------------------------------------------------------------
// Poll loop
// ---------------------------------------------------------------------------
async function refresh() {
  await Promise.all([
    loadMetrics(),
    loadRegime(),
    loadStrategy(),
    loadTrades(),
    loadLessons(),
    loadRules(),
    loadEquityChart(),
    loadPriceChart(),
  ]);
  document.getElementById("lastUpdated").textContent = new Date().toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function startPolling() {
  refresh();
  setInterval(refresh, POLL_INTERVAL);
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  initNav();
  startPolling();
});
