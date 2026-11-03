"use strict";

// Everything from the server is set through textContent: merchant names and answers can quote
// statement text, which must never become markup.

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";
const WRITE_HEADERS = { "X-Requested-With": "paisalens" };
const PAGE_SIZE = 50;

const state = {
  config: { uploads_enabled: false, has_my_data: false, questions_on_my_data: false, categories: [] },
  dataset: "demo",
  tab: "stats",
  month: "",
  txnOffset: 0,
};

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
      if (value instanceof Node) {
        tr.append(el("td", { className: headers[index].className || "" }, value));
      } else {
        tr.append(el("td", { text: value, className: `${headers[index].number ? "number" : ""} ${headers[index].className || ""}`.trim() }));
      }
    });
    body.append(tr);
  }
  return el("table", {}, el("thead", {}, head), body);
}

// Navigation and notices

function setNotice() {
  const notice = $("notice");
  if (state.dataset === "demo") {
    notice.textContent = "You're viewing a made-up person's transactions from November 2025 to October 2026. \"Today\" is fixed at 2 November 2026.";
    return;
  }
  notice.textContent = state.config.questions_on_my_data
    ? "Your data. Stats and transactions stay on this computer, but questions on the Ask tab send results to the AI provider."
    : "Your data. Everything here stays on this computer. Questions are off, because they would send your data to the AI provider.";
}

function showTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((button) => {
    if (button.dataset.tab === tab) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.querySelectorAll(".panel").forEach((panel) => { panel.hidden = panel.id !== `panel-${tab}`; });
  if (tab === "stats") loadStats();
  if (tab === "transactions") loadTransactions();
  if (tab === "ask") updateAskPanel();
}

function updateAskPanel() {
  const blocked = state.dataset === "mine" && !state.config.questions_on_my_data;
  $("ask-form").hidden = blocked;
  $("examples").hidden = blocked || state.dataset === "mine";
  $("result").hidden = !blocked;
  if (blocked) showAnswer("Questions about your own data are off, so nothing is sent to the AI provider. Use the Stats and Transactions tabs, which stay on this computer.", [], false);
  else if ($("answer").dataset.blocked) { $("result").hidden = true; }
  $("answer").dataset.blocked = blocked ? "1" : "";
}

function setDataset(dataset) {
  state.dataset = dataset;
  state.month = "";
  state.txnOffset = 0;
  $("result").hidden = true;
  setNotice();
  showTab(state.tab);
}

// Stats

function showStatsEmpty(message) {
  $("stats-body").hidden = true;
  $("stats-month").parentElement.hidden = true;
  $("stats-empty").hidden = false;
  $("stats-empty").textContent = message;
}

async function loadStats() {
  const params = new URLSearchParams({ dataset: state.dataset });
  if (state.month) params.set("month", state.month);
  let stats;
  try {
    stats = await request(`/stats?${params}`);
  } catch (error) {
    showStatsEmpty(error.status === 404 ? "You haven't uploaded a statement yet. Add one on the Upload tab." : error.message);
    return;
  }
  $("stats-empty").hidden = true;
  $("stats-body").hidden = false;
  $("stats-month").parentElement.hidden = false;
  renderPeriod(stats);
  renderTiles(stats.summary);
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

function niceMax(value) {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].find((candidate) => candidate * magnitude >= value);
  return step * magnitude;
}

function renderMonthlyChart(monthly, selected) {
  const width = 800;
  const height = 260;
  const margin = { top: 12, right: 8, bottom: 26, left: 52 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const top = niceMax(Math.max(0, ...monthly.map((m) => m.spent_paise)));
  const y = (value) => margin.top + plotHeight - (Math.max(value, 0) / top) * plotHeight;

  const chart = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Bar chart of spending by month" });
  for (const fraction of [0, 0.25, 0.5, 0.75, 1]) {
    const value = top * fraction;
    chart.append(svg("line", { class: fraction ? "grid" : "baseline", x1: margin.left, x2: width - margin.right, y1: y(value), y2: y(value) }));
    const label = svg("text", { x: margin.left - 8, y: y(value) + 4, "text-anchor": "end" });
    label.textContent = `₹${compact.format(value / 100)}`;
    chart.append(label);
  }

  const slot = plotWidth / Math.max(monthly.length, 1);
  const barWidth = Math.min(slot * 0.55, 36);
  const labelEvery = monthly.length > 14 ? 3 : monthly.length > 8 ? 2 : 1;
  monthly.forEach((item, index) => {
    const x = margin.left + slot * index;
    const group = svg("g", { class: `slot${item.month === selected ? " selected" : ""}`, tabindex: "0", role: "img",
                             "aria-label": `${monthLabel(item.month, true)}: ${formatWhole(item.spent_paise)}` });
    const top_y = y(item.spent_paise);
    const baseline = y(0);
    const barX = x + (slot - barWidth) / 2;
    const radius = Math.min(4, barWidth / 2, baseline - top_y);
    group.append(svg("path", {
      class: "mark",
      d: `M${barX} ${baseline}V${top_y + radius}Q${barX} ${top_y} ${barX + radius} ${top_y}` +
         `H${barX + barWidth - radius}Q${barX + barWidth} ${top_y} ${barX + barWidth} ${top_y + radius}V${baseline}Z`,
    }));
    group.append(svg("rect", { class: "hit", x, y: margin.top, width: slot, height: plotHeight + margin.bottom }));
    group.addEventListener("mousemove", (event) => showTooltip(item, event.clientX, event.clientY));
    group.addEventListener("focus", () => {
      const box = group.getBoundingClientRect();
      showTooltip(item, box.left + box.width / 2, box.top + 40);
    });
    group.addEventListener("mouseleave", hideTooltip);
    group.addEventListener("blur", hideTooltip);
    group.addEventListener("click", () => selectMonth(item.month));
    chart.append(group);

    if (index % labelEvery === 0) {
      const label = svg("text", { x: x + slot / 2, y: height - 8, "text-anchor": "middle" });
      label.textContent = monthLabel(item.month);
      chart.append(label);
    }
  });
  $("monthly-chart").replaceChildren(chart);
  $("monthly-table").replaceChildren(el("div", { className: "table-scroll" }, table(
    [{ label: "Month" }, { label: "Spent", number: true }],
    monthly.map((item) => [monthLabel(item.month, true), formatMoney(item.spent_paise)]))));
}

function showTooltip(item, x, y) {
  const tooltip = $("chart-tooltip");
  tooltip.replaceChildren(el("strong", { text: monthLabel(item.month, true) }), el("div", { text: formatMoney(item.spent_paise) }));
  tooltip.hidden = false;
  const box = tooltip.getBoundingClientRect();
  tooltip.style.left = `${Math.max(8, Math.min(x + 12, window.innerWidth - box.width - 8))}px`;
  tooltip.style.top = `${Math.max(8, y - box.height - 12)}px`;
}

function hideTooltip() {
  $("chart-tooltip").hidden = true;
}

function selectMonth(month) {
  hideTooltip();
  state.month = state.month === month ? "" : month;
  loadStats();
}

function renderBars(container, items) {
  if (!items.length) {
    container.replaceChildren(el("p", { className: "hint", text: "Nothing in this period." }));
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
    $("recurring").replaceChildren(el("p", { className: "hint", text: "None found yet. This needs at least three months of data." }));
    return;
  }
  $("recurring").replaceChildren(el("div", { className: "table-scroll" }, table(
    [{ label: "Merchant", className: "merchant" }, { label: "Typical", number: true }, { label: "Months", number: true }, { label: "Last paid" }, { label: "Status" }],
    items.map((item) => [item.merchant, formatMoney(item.typical_paise), String(item.months), dateLabel(item.last_date), item.active ? "Active" : "Stopped?"]))));
}

function renderLargest(items) {
  if (!items.length) {
    $("largest").replaceChildren(el("p", { className: "hint", text: "Nothing in this period." }));
    return;
  }
  $("largest").replaceChildren(el("div", { className: "table-scroll" }, table(
    [{ label: "Date" }, { label: "Merchant", className: "merchant" }, { label: "Category" }, { label: "Amount", number: true }],
    items.map((item) => [dateLabel(item.txn_date), item.merchant, item.category, formatMoney(item.amount_paise)]))));
}

// Transactions

async function loadTransactions() {
  const params = new URLSearchParams({
    dataset: state.dataset, search: $("txn-search").value.trim(), category: $("txn-category").value || "",
    month: $("txn-month").value || "", limit: PAGE_SIZE, offset: state.txnOffset,
  });
  let data;
  try {
    data = await request(`/transactions?${params}`);
  } catch (error) {
    $("txn-empty").hidden = false;
    $("txn-empty").textContent = error.status === 404 ? "You haven't uploaded a statement yet. Add one on the Upload tab." : error.message;
    $("txn-table").replaceChildren();
    $("txn-count").textContent = "";
    $("txn-prev").disabled = $("txn-next").disabled = true;
    return;
  }
  fillFilter($("txn-category"), "All categories", data.categories.map((c) => [c, c]));
  await fillMonthFilter();
  $("txn-empty").hidden = data.rows.length > 0;
  $("txn-empty").textContent = "No transactions match.";
  const editable = state.dataset === "mine";
  $("txn-table").replaceChildren(data.rows.length ? table(
    [{ label: "Date" }, { label: "Merchant", className: "merchant" }, { label: "Category" }, { label: "Account" }, { label: "Amount", number: true }],
    data.rows.map((row) => [
      dateLabel(row.txn_date), row.merchant,
      editable ? categoryPicker(row) : row.category,
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

async function fillMonthFilter() {
  if ($("txn-month").dataset.loadedFor === state.dataset) return;
  const stats = await request(`/stats?dataset=${state.dataset}`);
  fillFilter($("txn-month"), "All months", [...stats.months].reverse().map((month) => [month, monthLabel(month, true)]));
  $("txn-month").dataset.loadedFor = state.dataset;
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
        body: JSON.stringify({ merchant: row.merchant, category: select.value }),
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

function showAnswer(text, queries, isError) {
  $("answer").textContent = text;
  $("answer").classList.toggle("error", isError);
  $("sql").textContent = queries.join("\n\n");
  $("sql-details").hidden = queries.length === 0;
  $("result").hidden = false;
}

async function ask(question) {
  const submit = $("submit");
  submit.disabled = true;
  submit.textContent = "Working it out…";
  try {
    const body = await request("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, dataset: state.dataset }),
    });
    showAnswer(body.answer, body.sql, false);
  } catch (error) {
    showAnswer(error.message, [], true);
  } finally {
    submit.disabled = false;
    submit.textContent = "Ask";
  }
}

// Uploading

async function upload(event) {
  event.preventDefault();
  const file = $("upload-file").files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  form.append("account", $("upload-account").value);
  form.append("password", $("upload-password").value);
  form.append("self_name", $("upload-name").value);

  const button = $("upload-submit");
  const result = $("upload-result");
  button.disabled = true;
  button.textContent = "Reading…";
  try {
    const body = await request("/upload", { method: "POST", headers: WRITE_HEADERS, body: form });
    state.config.has_my_data = true;
    const lines = [`Read ${body.read} transactions: ${body.added} new, ${body.duplicates} already imported.`];
    if (body.skipped_rows) lines.push(`Skipped ${body.skipped_rows} rows that weren't transactions, or couldn't be read.`);
    if (body.balance_checked) {
      lines.push(body.balance_mismatches
        ? `Warning: ${body.balance_mismatches} of ${body.balance_checked} running balances didn't add up, so some amounts may be misread. Compare the totals with your statement.`
        : `Running balances add up on all ${body.balance_checked} rows checked.`);
    } else {
      lines.push("This statement has no running balance to check against, so compare the totals with your statement.");
    }
    result.replaceChildren(el("h2", { text: "Statement added" }), el("ul", { className: "results" }, ...lines.map((line) => el("li", { text: line }))));
    result.hidden = false;
    $("upload-form").reset();
    $("dataset").value = "mine";
    state.dataset = "mine";
    state.month = "";
    $("txn-month").dataset.loadedFor = "";
    setNotice();
  } catch (error) {
    result.replaceChildren(el("p", { className: "error", text: error.message }));
    result.hidden = false;
  } finally {
    $("upload-password").value = "";
    button.disabled = false;
    button.textContent = "Upload";
  }
}

async function deleteData() {
  if (!window.confirm("Delete all your uploaded transactions and category corrections from this computer? This can't be undone.")) return;
  try {
    await request("/my-data", { method: "DELETE", headers: WRITE_HEADERS });
    state.config.has_my_data = false;
    $("txn-month").dataset.loadedFor = "";
    $("upload-result").replaceChildren(el("p", { text: "Your data has been deleted." }));
    $("upload-result").hidden = false;
  } catch (error) {
    $("upload-result").replaceChildren(el("p", { className: "error", text: error.message }));
    $("upload-result").hidden = false;
  }
}

// Wiring

document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => showTab(button.dataset.tab)));
$("dataset").addEventListener("change", (event) => { $("txn-month").dataset.loadedFor = ""; setDataset(event.target.value); });
$("stats-month").addEventListener("change", (event) => { state.month = event.target.value; loadStats(); });

$("ask-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (question) ask(question);
});
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("ask-form").requestSubmit();
  }
});
document.querySelectorAll(".example").forEach((button) => {
  button.addEventListener("click", () => {
    $("question").value = button.textContent;
    $("ask-form").requestSubmit();
  });
});

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

$("upload-form").addEventListener("submit", upload);
$("delete-data").addEventListener("click", deleteData);

async function start() {
  try {
    state.config = await request("/config");
  } catch {
    // The demo still works without these extras.
  }
  $("upload-tab").hidden = !state.config.uploads_enabled;
  $("dataset-control").hidden = !state.config.uploads_enabled;
  setNotice();
  showTab("stats");
}

start();
