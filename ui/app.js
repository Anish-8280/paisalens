"use strict";

// Everything from the server is set through textContent: merchant names and answers can quote
// statement text, which must never become markup.

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";
const WRITE_HEADERS = { "X-Requested-With": "paisalens" };
const PAGE_SIZE = 50;
const HISTORY_TURNS_SENT = 6;
const HISTORY_ANSWER_LIMIT = 2000;
const EXAMPLE = { id: "example", name: "Example analysis", example: true, ai_allowed: true, accounts: [] };
const EXAMPLE_SUGGESTIONS = [
  "Where did most of my money go in September 2026?",
  "How much did I actually spend on Amazon, after refunds?",
  "Compare my food delivery spending month by month",
  "How much have I invested this financial year?",
];
const OWN_SUGGESTIONS = [
  "Where did most of my money go last month?",
  "Who are my top 5 merchants overall?",
  "Show my spending month by month",
  "What was my biggest single expense?",
];

const state = {
  config: { uploads_enabled: false, storage: "local", retention_minutes: 120, max_upload_mb: 10, categories: [] },
  analyses: [],
  current: null,     // the open analysis, or null on the New analysis page
  tab: "overview",
  month: "",
  txnOffset: 0,
  months: [],
};

// Conversations last until the page is reloaded, and are never written to browser storage.
const conversations = new Map();

const money = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" });
const moneyWhole = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("en-IN", { notation: "compact", maximumFractionDigits: 1 });
const formatMoney = (paise) => money.format(paise / 100);
const formatWhole = (paise) => moneyWhole.format(paise / 100);

function monthLabel(value, long = false) {
  const [year, month] = value.split("-").map(Number);
  const options = long ? { month: "long", year: "numeric" } : { month: "short" };
  return new Date(Date.UTC(year, month - 1, 1)).toLocaleDateString("en-IN", { ...options, timeZone: "UTC" });
}

function dateLabel(value) {
  return new Date(`${value}T00:00:00Z`).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

function el(tag, options = {}, ...children) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = options.text;
  for (const child of children) node.append(child);
  return node;
}

function svg(tag, attributes = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  return node;
}

async function request(url, options) {
  let response;
  try {
    response = await fetch(url, options);
  } catch {
    throw new Error("Couldn't reach the service. Check your connection and try again.");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof body.detail === "string" ? body.detail : "That request couldn't be processed.");
    error.status = response.status;
    throw error;
  }
  return body;
}

function table(headers, rows) {
  const head = el("tr");
  for (const header of headers) head.append(el("th", { text: header.label, className: header.number ? "number" : "" }));
  const body = el("tbody");
  for (const row of rows) {
    const tr = el("tr");
    row.forEach((value, index) => {
      const className = `${headers[index].number ? "number" : ""} ${headers[index].wrap ? "wrap" : ""}`.trim();
      if (value instanceof Node) tr.append(el("td", { className }, value));
      else tr.append(el("td", { text: value, className }));
    });
    body.append(tr);
  }
  return el("table", {}, el("thead", {}, head), body);
}

// Analyses and navigation

function allAnalyses() {
  return [...state.analyses, EXAMPLE];
}

function periodLabel(analysis) {
  if (!analysis.first_date) return "";
  const [first, last] = [analysis.first_date.slice(0, 7), analysis.last_date.slice(0, 7)];
  const to = monthLabel(last, true);
  return first === last ? to : `${monthLabel(first, first.slice(0, 4) !== last.slice(0, 4))} to ${to}`;
}

function renderSidebar() {
  const list = $("analysis-list");
  list.replaceChildren(...allAnalyses().map((analysis) => {
    const meta = analysis.example ? "Made-up data to explore" : periodLabel(analysis);
    const button = el("button", { className: "analysis-item" },
      el("span", { className: "analysis-name", text: analysis.name }),
      el("span", { className: "analysis-meta", text: meta }));
    button.type = "button";
    if (state.current && state.current.id === analysis.id) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => openAnalysis(analysis.id));
    return button;
  }));
  $("new-analysis").classList.toggle("active", state.current === null);
}

async function refreshAnalyses() {
  state.analyses = state.config.uploads_enabled ? (await request("/analyses")).analyses : [];
}

function retentionLabel() {
  const minutes = state.config.retention_minutes;
  return minutes % 60 === 0 ? `${minutes / 60} hour${minutes === 60 ? "" : "s"}` : `${minutes} minutes`;
}

function applyStorageWording() {
  const temporary = state.config.storage === "temporary";
  $("drop-limit").textContent = `PDF or CSV, up to ${state.config.max_upload_mb} MB`;
  const privacy = $("new-privacy");
  privacy.replaceChildren(el("strong", { text: temporary ? "Private and temporary." : "Stays on this computer." }), " ");
  privacy.append(temporary
    ? `Your transactions are held on this site's server, visible only from this browser, and deleted after ${retentionLabel()} of inactivity or whenever the server restarts, which can happen sooner. The file isn't kept. Nothing goes to an AI model unless you turn on questions. This is a demo without accounts or encryption at rest, so consider using a statement you're comfortable with.`
    : "The file isn't kept, only the transactions read from it. Nothing goes to an AI model unless you turn on questions for this analysis.");
  $("temporary-note").textContent = `Temporary: only this browser can see this analysis. It's deleted after ${retentionLabel()} of inactivity or when the server restarts.`;
}

// An analysis can expire while its page is open, so return to the list rather than showing a broken page.
async function handleLoadError(error) {
  if (error.status !== 404 || !state.current || state.current.example) throw error;
  conversations.delete(state.current.id);
  await refreshAnalyses();
  if (state.analyses.length) openAnalysis(state.analyses[0].id);
  else openAnalysis(EXAMPLE.id);
}

function showNewAnalysis() {
  state.current = null;
  $("view-analysis").hidden = true;
  $("view-new").hidden = false;
  renderSidebar();
  window.scrollTo(0, 0);
}

function openAnalysis(id, tab = "overview") {
  const analysis = allAnalyses().find((item) => item.id === id);
  if (!analysis) return showNewAnalysis();
  state.current = analysis;
  state.month = "";
  state.txnOffset = 0;
  state.months = [];
  $("txn-search").value = "";
  $("txn-category").replaceChildren();
  $("txn-month").replaceChildren();
  $("view-new").hidden = true;
  $("view-analysis").hidden = false;
  $("import-report").hidden = true;
  $("manage-tab").hidden = analysis.example;
  $("temporary-note").hidden = analysis.example || state.config.storage !== "temporary";
  renderHeader();
  renderSidebar();
  renderThread();
  showTab(tab);
  window.scrollTo(0, 0);
}

function renderHeader() {
  const analysis = state.current;
  $("analysis-title").textContent = analysis.name;
  $("analysis-subtitle").textContent = analysis.example
    ? "A made-up person's transactions, November 2025 to October 2026. \"Today\" is fixed at 2 November 2026."
    : `${periodLabel(analysis)} · ${analysis.transactions.toLocaleString("en-IN")} transactions`;
}

function showTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((button) => {
    if (button.dataset.tab === tab) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.querySelectorAll(".panel").forEach((panel) => { panel.hidden = panel.id !== `panel-${tab}`; });
  if (tab === "overview") loadStats().catch(handleLoadError);
  if (tab === "transactions") loadTransactions().catch(handleLoadError);
  if (tab === "ask") renderAsk();
  if (tab === "manage") renderManage();
}

function showReport(report) {
  const lines = [`Read ${report.read} transactions: ${report.added} new, ${report.duplicates} already here.`];
  if (report.skipped_rows) lines.push(`Skipped ${report.skipped_rows} rows that weren't transactions or couldn't be read.`);
  let warn = false;
  if (report.balance_checked && report.balance_mismatches) {
    warn = true;
    lines.push(`${report.balance_mismatches} of ${report.balance_checked} running balances didn't add up, so some amounts may be misread. Compare the totals with your statement.`);
  } else if (report.balance_checked) {
    lines.push(`Running balances add up on all ${report.balance_checked} rows checked.`);
  } else {
    lines.push("This statement has no running balance to check against, so compare the totals with your statement.");
  }
  const box = $("import-report");
  box.className = warn ? "report warn" : "report";
  box.replaceChildren(el("ul", {}, ...lines.map((line) => el("li", { text: line }))));
  box.hidden = false;
}

// Overview

async function loadStats() {
  const analysis = state.current;
  const params = new URLSearchParams({ analysis: analysis.id });
  if (state.month) params.set("month", state.month);
  const stats = await request(`/stats?${params}`);
  if (state.current !== analysis) return;
  state.months = stats.months;
  renderPeriod(stats);
  renderTiles(stats.summary);
  renderInsight(stats);
  renderMonthlyChart(stats.monthly, stats.month);
  renderBars($("categories"), stats.categories.map((c) => ({ label: c.category, value: c.spent_paise })));
  renderBars($("merchants"), stats.merchants.map((m) => ({ label: m.merchant, value: m.spent_paise })));
  renderRecurring(stats.recurring);
  renderLargest(stats.largest);
}

function renderPeriod(stats) {
  const select = $("stats-month");
  select.replaceChildren(el("option", { text: "All time" }));
  select.firstChild.value = "";
  for (const month of [...stats.months].reverse()) {
    const option = el("option", { text: monthLabel(month, true) });
    option.value = month;
    select.append(option);
  }
  select.value = state.month;
}

function renderTiles(summary) {
  const tiles = [
    ["Spent", formatWhole(summary.spent_paise)],
    ["Received", formatWhole(summary.received_paise)],
    ["Invested", formatWhole(summary.invested_paise)],
    ["Left over", formatWhole(summary.left_over_paise)],
  ];
  $("tiles").replaceChildren(...tiles.map(([label, value]) =>
    el("div", { className: "tile" }, el("div", { className: "tile-label", text: label }), el("div", { className: "tile-value", text: value }))));
}

function renderInsight(stats) {
  const top = stats.categories[0];
  const insight = $("insight");
  insight.hidden = !top || stats.summary.spent_paise <= 0;
  if (insight.hidden) return;
  const share = Math.round((top.spent_paise / stats.summary.spent_paise) * 100);
  insight.textContent = `${top.category} was the biggest category: ${formatWhole(top.spent_paise)}, about ${share}% of spending${stats.month ? ` in ${monthLabel(stats.month, true)}` : ""}.`;
}

function niceMax(value) {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].find((candidate) => candidate * magnitude >= value);
  return step * magnitude;
}

// One bar chart for the Overview and for answers. The numbers always come straight from the database.
function barChart(points, { onSelect, selected, height = 260, ariaLabel }) {
  const width = 800;
  const margin = { top: 12, right: 8, bottom: 26, left: 56 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const top = niceMax(Math.max(0, ...points.map((point) => point.value)));
  const y = (value) => margin.top + plotHeight - (Math.max(value, 0) / top) * plotHeight;

  const chart = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": ariaLabel });
  for (const fraction of [0, 0.25, 0.5, 0.75, 1]) {
    const value = top * fraction;
    chart.append(svg("line", { class: fraction ? "grid" : "baseline", x1: margin.left, x2: width - margin.right, y1: y(value), y2: y(value) }));
    const label = svg("text", { x: margin.left - 8, y: y(value) + 4, "text-anchor": "end" });
    label.textContent = `₹${compact.format(value / 100)}`;
    chart.append(label);
  }

  const slot = plotWidth / Math.max(points.length, 1);
  const barWidth = Math.min(slot * 0.56, 40);
  const longest = Math.max(...points.map((point) => point.short.length));
  const labelEvery = Math.max(1, Math.ceil((longest * 6.2 + 6) / slot));
  points.forEach((point, index) => {
    const x = margin.left + slot * index;
    const group = svg("g", { class: `slot${point.key === selected ? " selected" : ""}`, tabindex: "0", role: "img",
                             "aria-label": `${point.long}: ${formatWhole(point.value)}` });
    const topY = y(point.value);
    const baseline = y(0);
    const barX = x + (slot - barWidth) / 2;
    const radius = Math.min(4, barWidth / 2, baseline - topY);
    group.append(svg("path", {
      class: "mark",
      d: `M${barX} ${baseline}V${topY + radius}Q${barX} ${topY} ${barX + radius} ${topY}` +
         `H${barX + barWidth - radius}Q${barX + barWidth} ${topY} ${barX + barWidth} ${topY + radius}V${baseline}Z`,
    }));
    group.append(svg("rect", { class: "hit", x, y: margin.top, width: slot, height: plotHeight + margin.bottom }));
    group.addEventListener("mousemove", (event) => showTooltip(point, event.clientX, event.clientY));
    group.addEventListener("focus", () => {
      const box = group.getBoundingClientRect();
      showTooltip(point, box.left + box.width / 2, box.top + 40);
    });
    group.addEventListener("mouseleave", hideTooltip);
    group.addEventListener("blur", hideTooltip);
    if (onSelect) group.addEventListener("click", () => { hideTooltip(); onSelect(point.key); });
    chart.append(group);

    if (index % labelEvery === 0) {
      const label = svg("text", { x: x + slot / 2, y: height - 8, "text-anchor": "middle" });
      label.textContent = point.short;
      chart.append(label);
    }
  });
  return chart;
}

function renderMonthlyChart(monthly, selected) {
  const points = monthly.map((item) => ({ key: item.month, value: item.spent_paise, short: monthLabel(item.month), long: monthLabel(item.month, true) }));
  const onSelect = (month) => {
    state.month = state.month === month ? "" : month;
    loadStats().catch(handleLoadError);
  };
  $("monthly-chart").replaceChildren(barChart(points, { onSelect, selected, ariaLabel: "Bar chart of spending by month" }));
  $("monthly-table").replaceChildren(el("div", { className: "table-scroll" }, table(
    [{ label: "Month" }, { label: "Spent", number: true }],
    monthly.map((item) => [monthLabel(item.month, true), formatMoney(item.spent_paise)]))));
}

function showTooltip(point, x, y) {
  const tooltip = $("chart-tooltip");
  tooltip.replaceChildren(el("strong", { text: point.long }), el("div", { text: formatMoney(point.value) }));
  tooltip.hidden = false;
  const box = tooltip.getBoundingClientRect();
  tooltip.style.left = `${Math.max(8, Math.min(x + 12, window.innerWidth - box.width - 8))}px`;
  tooltip.style.top = `${Math.max(8, y - box.height - 12)}px`;
}

function hideTooltip() {
  $("chart-tooltip").hidden = true;
}

function renderBars(container, items) {
  if (!items.length) {
    container.replaceChildren(el("p", { className: "muted small", text: "Nothing in this period." }));
    return;
  }
  const largest = Math.max(...items.map((item) => item.value));
  container.replaceChildren(...items.map((item) => {
    const fill = el("div", { className: "bar-fill" });
    fill.style.width = `${(item.value / largest) * 100}%`;
    return el("div", { className: "bar-row" },
      el("div", { className: "bar-label" }, el("span", { text: item.label }), el("span", { text: formatWhole(item.value) })),
      el("div", { className: "bar-track" }, fill));
  }));
}

function renderRecurring(items) {
  if (!items.length) {
    $("recurring").replaceChildren(el("p", { className: "muted small", text: "None found. This needs at least three months of data." }));
    return;
  }
  $("recurring").replaceChildren(el("div", { className: "table-scroll" }, table(
    [{ label: "Merchant", wrap: true }, { label: "Typical", number: true }, { label: "Months", number: true }, { label: "Last paid" }, { label: "Status" }],
    items.map((item) => [item.merchant, formatMoney(item.typical_paise), String(item.months), dateLabel(item.last_date), item.active ? "Active" : "May have stopped"]))));
}

function renderLargest(items) {
  if (!items.length) {
    $("largest").replaceChildren(el("p", { className: "muted small", text: "Nothing in this period." }));
    return;
  }
  $("largest").replaceChildren(el("div", { className: "table-scroll" }, table(
    [{ label: "Date" }, { label: "Merchant", wrap: true }, { label: "Category" }, { label: "Amount", number: true }],
    items.map((item) => [dateLabel(item.txn_date), item.merchant, item.category, formatMoney(item.amount_paise)]))));
}

// Transactions

async function loadTransactions() {
  const analysis = state.current;
  const params = new URLSearchParams({
    analysis: analysis.id, search: $("txn-search").value.trim(), category: $("txn-category").value || "",
    month: $("txn-month").value || "", limit: PAGE_SIZE, offset: state.txnOffset,
  });
  const data = await request(`/transactions?${params}`);
  if (state.current !== analysis) return;
  fillFilter($("txn-category"), "All categories", data.categories.map((c) => [c, c]));
  if (!state.months.length) state.months = (await request(`/stats?analysis=${analysis.id}`)).months;
  fillFilter($("txn-month"), "All months", [...state.months].reverse().map((month) => [month, monthLabel(month, true)]));
  $("txn-empty").hidden = data.rows.length > 0;
  $("txn-table").replaceChildren(data.rows.length ? table(
    [{ label: "Date" }, { label: "Merchant", wrap: true }, { label: "Category" }, { label: "Account" }, { label: "Amount", number: true }],
    data.rows.map((row) => [
      dateLabel(row.txn_date), row.merchant,
      analysis.example ? row.category : categoryPicker(row),
      row.account,
      `${row.direction === "credit" ? "+" : "-"}${formatMoney(row.amount_paise)}`,
    ])) : "");
  const end = Math.min(state.txnOffset + PAGE_SIZE, data.total);
  $("txn-count").textContent = data.total ? `${state.txnOffset + 1}–${end} of ${data.total}` : "";
  $("txn-prev").disabled = state.txnOffset === 0;
  $("txn-next").disabled = end >= data.total;
}

function fillFilter(select, allLabel, options) {
  const current = select.value;
  select.replaceChildren(el("option", { text: allLabel }));
  select.firstChild.value = "";
  for (const [value, label] of options) {
    const option = el("option", { text: label });
    option.value = value;
    select.append(option);
  }
  select.value = options.some(([value]) => value === current) ? current : "";
}

function categoryPicker(row) {
  const fixed = ["internal_transfer", "card_payment", "investment", "income"].includes(row.txn_type);
  if (fixed) return el("span", { className: "tag", text: row.category });
  const select = el("select");
  select.setAttribute("aria-label", `Category for ${row.merchant}`);
  const options = state.config.categories.includes(row.category) ? state.config.categories : [row.category, ...state.config.categories];
  for (const category of options) {
    const option = el("option", { text: category });
    option.value = category;
    select.append(option);
  }
  select.value = row.category;
  select.addEventListener("change", async () => {
    select.disabled = true;
    try {
      await request("/categories", {
        method: "PATCH",
        headers: { ...WRITE_HEADERS, "Content-Type": "application/json" },
        body: JSON.stringify({ analysis: state.current.id, merchant: row.merchant, category: select.value }),
      });
      await loadTransactions();
    } catch (error) {
      select.disabled = false;
      window.alert(error.message);
    }
  });
  return select;
}

// Asking questions

function conversation() {
  if (!conversations.has(state.current.id)) conversations.set(state.current.id, []);
  return conversations.get(state.current.id);
}

// Supports the little formatting answers use: **bold** and "- " bullets. Everything else stays as text.
function appendInline(parent, text) {
  text.split(/(\*\*[^*]+\*\*)/).forEach((part) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) parent.append(el("strong", { text: part.slice(2, -2) }));
    else if (part) parent.append(part);
  });
}

function formatAnswer(text) {
  const container = el("div", { className: "answer-text" });
  let list = null;
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) { list = null; continue; }
    const bullet = trimmed.match(/^[-*•]\s+(.*)$/);
    if (bullet) {
      if (!list) { list = el("ul"); container.append(list); }
      const item = el("li");
      appendInline(item, bullet[1]);
      list.append(item);
    } else {
      list = null;
      const paragraph = el("p");
      appendInline(paragraph, trimmed);
      container.append(paragraph);
    }
  }
  return container;
}

function chartLabel(label) {
  return /^\d{4}-\d{2}$/.test(label) ? { short: monthLabel(label), long: monthLabel(label, true) } : { short: label.length > 14 ? `${label.slice(0, 13)}…` : label, long: label };
}

function renderResultTable(result) {
  const box = el("div");
  if (result.chart) {
    const points = result.chart.labels.map((label, index) => ({ key: label, value: result.chart.values_paise[index], ...chartLabel(label) }));
    if (points.some((point) => point.value > 0)) {
      box.append(el("div", { className: "chart answer-chart" }, barChart(points, { height: 200, ariaLabel: `Bar chart of ${result.chart.series}` })));
    }
  }
  const headers = result.columns.map((label, index) => ({ label, number: result.numeric[index], wrap: !result.numeric[index] }));
  const rows = result.rows.map((row) => row.map((value, index) => (/^\d{4}-\d{2}$/.test(String(value)) && index === 0 ? monthLabel(String(value), true) : String(value))));
  const details = el("details", { className: "answer-table" }, el("summary", { text: "Show the numbers" }), el("div", { className: "table-scroll" }, table(headers, rows)));
  if (result.truncated) details.append(el("p", { className: "more-note", text: `Showing the first ${result.rows.length} rows.` }));
  if (!result.chart) details.open = true;
  box.append(details);
  return box;
}

function renderMessage(message) {
  if (message.role === "user") return el("div", { className: "bubble-user", text: message.text });
  if (message.pending) return el("div", { className: "card bubble-answer" }, el("div", { className: "typing", text: "Working it out…" }));
  const card = el("article", { className: `card bubble-answer${message.failed ? " failed" : ""}` }, formatAnswer(message.text));
  if (message.table) card.append(renderResultTable(message.table));
  if (message.sql && message.sql.length) {
    card.append(el("details", {}, el("summary", { text: "How this was worked out" }), el("pre", { text: message.sql.join("\n\n") })));
  }
  return card;
}

function renderThread() {
  const messages = conversation();
  $("thread").replaceChildren(...messages.map(renderMessage));
  $("clear-chat").hidden = messages.length === 0;
  const suggestions = state.current.example ? EXAMPLE_SUGGESTIONS : OWN_SUGGESTIONS;
  $("suggestions").hidden = messages.length > 0;
  $("suggestions").replaceChildren(...suggestions.map((text) => {
    const button = el("button", { className: "example", text });
    button.type = "button";
    button.addEventListener("click", () => ask(text));
    return button;
  }));
}

function renderAsk() {
  const allowed = state.current.ai_allowed;
  $("ask-consent").hidden = allowed;
  $("ask-chat").hidden = !allowed;
  renderThread();
}

function historyToSend(messages) {
  const turns = [];
  for (let index = 0; index + 1 < messages.length; index += 1) {
    const [question, answer] = [messages[index], messages[index + 1]];
    if (question.role === "user" && answer.role === "answer" && !answer.failed && !answer.pending) {
      turns.push({ question: question.text, answer: answer.text.slice(0, HISTORY_ANSWER_LIMIT) });
    }
  }
  return turns.slice(-HISTORY_TURNS_SENT);
}

async function ask(question) {
  const analysis = state.current;
  const messages = conversation();
  const history = historyToSend(messages);
  const pending = { role: "answer", pending: true };
  messages.push({ role: "user", text: question }, pending);
  $("question").value = "";
  $("ask-submit").disabled = true;
  renderThread();
  try {
    const body = await request("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, analysis: analysis.id, history }),
    });
    Object.assign(pending, { pending: false, text: body.answer, sql: body.sql, table: body.table });
  } catch (error) {
    Object.assign(pending, { pending: false, failed: true, text: error.message });
  } finally {
    $("ask-submit").disabled = false;
    if (state.current === analysis && state.tab === "ask") {
      renderThread();
      pendingScroll();
    }
  }
}

function pendingScroll() {
  $("ask-form").scrollIntoView({ block: "nearest" });
}

// Manage

function renderManage() {
  const analysis = state.current;
  $("rename-input").value = analysis.name;
  $("add-account").value = analysis.accounts[0] || "My account";
  $("account-names").replaceChildren(...analysis.accounts.map((name) => { const option = el("option"); option.value = name; return option; }));
  $("ai-status").textContent = analysis.ai_allowed
    ? "On. Questions send results from this analysis, including merchant and people's names, amounts and dates, to the AI provider."
    : "Off. Nothing from this analysis is sent to the AI provider.";
  $("ai-toggle").textContent = analysis.ai_allowed ? "Turn off questions" : "Turn on questions";
}

async function changeAnalysis(change) {
  const updated = await request(`/analyses/${state.current.id}`, {
    method: "PATCH",
    headers: { ...WRITE_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify(change),
  });
  await refreshAnalyses();
  state.current = updated;
  renderHeader();
  renderSidebar();
}

function statementForm(file, account, password, selfName) {
  const form = new FormData();
  form.append("file", file);
  form.append("account", account);
  form.append("password", password);
  form.append("self_name", selfName);
  return form;
}

async function createAnalysis(event) {
  event.preventDefault();
  const file = $("new-file").files[0];
  if (!file) return;
  const form = statementForm(file, $("new-account").value, $("new-password").value, $("new-self").value);
  form.append("name", $("new-name").value);
  const button = $("new-submit");
  button.disabled = true;
  button.textContent = "Reading your statement…";
  $("new-error").hidden = true;
  try {
    const body = await request("/analyses", { method: "POST", headers: WRITE_HEADERS, body: form });
    await refreshAnalyses();
    $("new-form").reset();
    setDropTitle();
    openAnalysis(body.analysis.id);
    showReport(body.report);
  } catch (error) {
    $("new-error").textContent = error.message;
    $("new-error").hidden = false;
  } finally {
    $("new-password").value = "";
    button.disabled = false;
    button.textContent = "Start analysis";
  }
}

async function addStatement(event) {
  event.preventDefault();
  const file = $("add-file").files[0];
  if (!file) return;
  const button = $("add-submit");
  button.disabled = true;
  button.textContent = "Reading…";
  $("add-error").hidden = true;
  try {
    const form = statementForm(file, $("add-account").value, $("add-password").value, $("add-self").value);
    const body = await request(`/analyses/${state.current.id}/statements`, { method: "POST", headers: WRITE_HEADERS, body: form });
    await refreshAnalyses();
    $("add-form").reset();
    openAnalysis(body.analysis.id);
    showReport(body.report);
  } catch (error) {
    $("add-error").textContent = error.message;
    $("add-error").hidden = false;
  } finally {
    $("add-password").value = "";
    button.disabled = false;
    button.textContent = "Add statement";
  }
}

async function deleteAnalysis() {
  const { id, name } = state.current;
  if (!window.confirm(`Delete "${name}"? This can't be undone.`)) return;
  try {
    await request(`/analyses/${id}`, { method: "DELETE", headers: WRITE_HEADERS });
    conversations.delete(id);
    await refreshAnalyses();
    if (state.analyses.length) openAnalysis(state.analyses[0].id);
    else showNewAnalysis();
  } catch (error) {
    window.alert(error.message);
  }
}

function setDropTitle() {
  const file = $("new-file").files[0];
  $("drop-title").textContent = file ? file.name : "Choose a statement, or drop it here";
}

// Wiring

document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => showTab(button.dataset.tab)));
$("new-analysis").addEventListener("click", showNewAnalysis);
$("stats-month").addEventListener("change", (event) => { state.month = event.target.value; loadStats().catch(handleLoadError); });

$("ask-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (question && !$("ask-submit").disabled) ask(question);
});
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("ask-form").requestSubmit();
  }
});
$("clear-chat").addEventListener("click", () => { conversations.delete(state.current.id); renderThread(); });
$("ask-enable").addEventListener("click", async () => { await changeAnalysis({ ai_allowed: true }); renderAsk(); });

let searchTimer;
$("txn-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.txnOffset = 0; loadTransactions(); }, 250);
});
for (const id of ["txn-category", "txn-month"]) {
  $(id).addEventListener("change", () => { state.txnOffset = 0; loadTransactions(); });
}
$("txn-prev").addEventListener("click", () => { state.txnOffset = Math.max(0, state.txnOffset - PAGE_SIZE); loadTransactions(); });
$("txn-next").addEventListener("click", () => { state.txnOffset += PAGE_SIZE; loadTransactions(); });

$("new-form").addEventListener("submit", createAnalysis);
$("new-file").addEventListener("change", setDropTitle);
const dropzone = $("dropzone");
["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.add("dragging"); }));
["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.remove("dragging"); }));
dropzone.addEventListener("drop", (event) => {
  if (event.dataTransfer.files.length) {
    $("new-file").files = event.dataTransfer.files;
    setDropTitle();
  }
});

$("rename-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await changeAnalysis({ name: $("rename-input").value });
  renderManage();
});
$("add-form").addEventListener("submit", addStatement);
$("ai-toggle").addEventListener("click", async () => {
  const turningOn = !state.current.ai_allowed;
  if (turningOn && !window.confirm("Questions send results from this analysis, including merchant and people's names, amounts and dates, to the AI provider. Turn them on?")) return;
  await changeAnalysis({ ai_allowed: turningOn });
  renderManage();
});
$("delete-analysis").addEventListener("click", deleteAnalysis);

async function start() {
  try {
    state.config = await request("/config");
    await refreshAnalyses();
  } catch {
    // The example still works without these extras.
  }
  $("new-analysis").hidden = !state.config.uploads_enabled;
  applyStorageWording();
  openAnalysis(state.analyses.length ? state.analyses[0].id : EXAMPLE.id);
}

start();
