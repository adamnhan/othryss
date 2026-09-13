"use strict";

const $ = (id) => document.getElementById(id);
const state = { data: null, replays: 1, source: "all", detail: "key", order: null, query: "", selected: null };
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
// Formatting never converts monetary or quantity strings through binary floats.
function decimal(value, minPlaces = 0) {
  if (value === null || value === undefined) return "Unavailable";
  let [whole, fraction = ""] = String(value).split(".");
  fraction = fraction.replace(/0+$/, "").padEnd(minPlaces, "0");
  return whole + (fraction ? `.${fraction}` : "");
}
function money(value) {
  const text = decimal(value, 2);
  return text.startsWith("-") ? `−$${text.slice(1)}` : `$${text}`;
}
// Display in the viewer's timezone; stored and exported evidence stays unchanged.
const localDateTime = new Intl.DateTimeFormat("en-US", {
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true,
  timeZoneName: "short"
});
const localClock = new Intl.DateTimeFormat("en-US", {
  hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true
});
function formatDateTime(value, {precise = false, clockOnly = false, fallback = "Unavailable"} = {}) {
  if (value == null || value === "") return fallback;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return fallback;
  // Preserve source subsecond precision in the evidence inspector.
  const fraction = precise && typeof value === "string" ? value.match(/T\d{2}:\d{2}:\d{2}(\.\d+)/)?.[1] : "";
  return (clockOnly ? localClock : localDateTime).formatToParts(date)
    .map(part => part.value + (part.type === "second" && fraction ? fraction : ""))
    .join("").replace(/\u202f|\u00a0/g, " ");
}
function time(value, precise = false) { return formatDateTime(value, {precise}); }
function updatedAgo(value) {
  const age = value ? Math.max(0, Math.floor((Date.now()-new Date(value).getTime())/1000)) : NaN;
  if (!Number.isFinite(age)) return "No updates yet";
  if (age < 5) return "Updated just now";
  const count = age < 60 ? age : age < 3600 ? Math.floor(age/60) : age < 86400 ? Math.floor(age/3600) : Math.floor(age/86400);
  const unit = age < 60 ? "second" : age < 3600 ? "minute" : age < 86400 ? "hour" : "day";
  return `Updated ${count} ${unit}${count === 1 ? "" : "s"} ago`;
}
// Evidence exports retain source values; the on-screen inspector uses local dates.
function evidenceText(value) {
  return JSON.stringify(value, function(key, item) {
    if (typeof item === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(item)) {
      return formatDateTime(item, {precise:true});
    }
    if (typeof item === "number" && /^(created_at|heartbeat_at|next_attempt_at|last_sent|activated_at|finished_at|started_at)$/.test(key) && item > 1000000000 && item < 100000000000) {
      return formatDateTime(item * 1000);
    }
    return item;
  }, 2);
}
// Hidden pages do not compete with the current page for API/database work.
function onHistoryScope(pages, listener) {
  let scope = "";
  const active = () => pages.includes(window.OthryssWorkspace?.page);
  document.addEventListener("history-scope", event => {
    scope = event.detail;
    if (scope && active()) listener(event);
  });
  document.addEventListener("workspace-account", event => {
    scope = event.detail?.scope_id ?? "";
  });
  document.addEventListener("workspace-page", () => {
    if (scope && active()) listener({detail: scope});
  });
}
function isFill(event) { return event.type === "ORDER_FILL"; }
function description(event) {
  const p = event.payload;
  if (isFill(event)) {
    const buy = p.exposure_direction === "increase_yes";
    return {
      title: `${p.liquidity === "maker" ? "Maker" : "Taker"} ${buy ? "buy" : "sell"} filled`,
      subtitle: `${decimal(p.quantity)} contracts at ${money(p.price_usd)} · ${money(p.fee_usd)} fee`,
      note: "Saved exchange fill. Its timestamp is the source execution time; the bot's receipt time was not recorded.",
    };
  }
  if (p.reason === "fill_stop") return {
    title: "Bot reflects partial fill",
    subtitle: `${decimal(p.signed_position)} held · ${decimal(p.reported_order_size)} remaining`,
    note: "The bot now reflects the fill. This inventory was derived from exchange fills and is not an independent exchange-position snapshot.",
  };
  if (p.reason === "final_cancel") return {
    title: "Bot clears tracked order",
    subtitle: `Local reason: final_cancel · ${decimal(p.signed_position)} held`,
    note: "The bot cleared its local order reference. A cancellation request timestamp and exchange confirmation are unavailable. This observation does not prove cancellation succeeded.",
  };
  return {
    title: "Local order observed",
    subtitle: `${decimal(p.reported_order_size)} reported remaining · ${decimal(p.signed_position)} held`,
    note: "A bot state observation, not an order acknowledgement. Its order size and position describe this probe only. Normal observation delay is not a confirmed discrepancy.",
  };
}

function renderSummary() {
  const data = state.data, totals = data.totals;
  if (data.fixture.synthetic) {
    $("fixture-kind").textContent = "Synthetic example";
    $("fixture-notice").textContent = "Synthetic orders, fills, and bot snapshots. No live account data.";
    $("fixture-provenance").textContent = "All records and identifiers in this example are invented. No exchange connection or real trades.";
  }
  $("instrument").textContent = data.events[0]?.instrument_id ?? "Unknown instrument";
  $("run-date").textContent = formatDateTime(data.events[0].occurred_at);
  $("fill-count").textContent = data.fill_count;
  const maker = data.events.filter((e) => isFill(e) && e.payload.liquidity === "maker").length;
  $("fill-foot").textContent = `${maker} maker / ${data.fill_count - maker} taker · ${decimal(totals.volume)} contracts`;
  $("net-quantity").innerHTML = `${escapeHtml(decimal(totals.net_quantity))}<small>contracts</small>`;
  $("fees").textContent = money(totals.fees);
  $("cash-flow").textContent = money(totals.net_cash_flow);
  $("cash-flow").classList.toggle("negative", totals.net_cash_flow.startsWith("-"));
  $("order-count").textContent = data.orders.length;
  $("footer-count").textContent = data.unique_events;
  $("source-file").textContent = data.fixture.source_filename;
  $("source-hash").textContent = `SHA-256 ${data.fixture.source_sha256}`;
}

function renderOrders() {
  $("order-cards").innerHTML = state.data.orders.map((order) => {
    const buy = order.directions.includes("increase_yes");
    const observed = order.first_reported_size !== null;
    return `<button class="order-card ${state.order === order.order_id ? "selected" : ""}" data-order="${escapeHtml(order.order_id)}" aria-pressed="${state.order === order.order_id}">
      <div class="order-card-top"><span class="direction ${buy ? "" : "exit"}">${buy ? "↗" : "↙"}</span><strong>${buy ? "Buy / entry" : "Sell / exit"}</strong><span class="order-alias">${escapeHtml(order.order_id)}</span></div>
      <div class="order-values"><div>${escapeHtml(decimal(order.filled_quantity))}<small>Filled contracts</small></div><div>${observed ? escapeHtml(decimal(order.last_reported_remaining)) : "—"}<small>${observed ? "Last local remaining" : "Remaining unavailable"}</small></div></div>
      <div class="order-status">${observed ? `First observed size: ${escapeHtml(decimal(order.first_reported_size))}` : "Order first seen in a saved fill"} · ${escapeHtml(order.liquidity.join(" / "))}</div></button>`;
  }).join("");
  document.querySelectorAll("[data-order]").forEach((button) => button.addEventListener("click", () => {
    state.order = state.order === button.dataset.order ? null : button.dataset.order;
    const event = state.data.events.find((e) => isFill(e) && e.payload.order_id === state.order);
    if (event) state.selected = event.event_id;
    const orderId = button.dataset.order;
    renderOrders(); renderTimeline();
    document.querySelector(`[data-order="${CSS.escape(orderId)}"]`)?.focus({ preventScroll: true });
  }));
}

function groupedEvents() {
  const all = state.data.events;
  if (state.detail === "all") return all.map((event) => ({ event, last: event, count: 1 }));
  const groups = [];
  for (const event of all) {
    const previous = groups.at(-1);
    if (previous && !isFill(event) && !isFill(previous.event) &&
        event.origin === previous.event.origin && JSON.stringify(event.payload) === JSON.stringify(previous.event.payload)) {
      previous.count += 1;
      previous.last = event;
    } else groups.push({ event, last: event, count: 1 });
  }
  return groups;
}

function renderTimeline() {
  const groups = groupedEvents().filter(({ event, count }) => {
    if (state.order && event.payload.order_id !== state.order) return false;
    if (state.source !== "all" && (isFill(event) ? "exchange" : "bot") !== state.source) return false;
    const text = `${description(event).title} ${description(event).subtitle} ${event.payload.order_id ?? ""} ${event.occurred_at} ${event.origin} ${count}`;
    return text.toLowerCase().includes(state.query.toLowerCase());
  });
  if (!groups.some(({ event }) => event.event_id === state.selected)) state.selected = groups.find(({ event }) => isFill(event))?.event.event_id ?? groups[0]?.event.event_id ?? null;
  $("event-count").textContent = groups.reduce((sum, group) => sum + group.count, 0);
  $("timeline-caption").textContent = state.order ? `${state.order} · unlinked run observations are outside this filter.` : "The sequence preserved in the source record.";
  $("timeline").innerHTML = groups.length ? groups.map(({ event, last, count }) => {
    const desc = description(event), fill = isFill(event);
    return `<button class="timeline-row ${state.selected === event.event_id ? "selected" : ""}" data-event="${escapeHtml(event.event_id)}" aria-pressed="${state.selected === event.event_id}">
      <span class="timeline-time">${time(event.occurred_at)}${count > 1 ? `<small>to ${time(last.occurred_at)}</small>` : ""}</span>
      <span><span class="timeline-title">${count > 1 ? "Order unchanged" : escapeHtml(desc.title)}${count > 1 ? `<span class="count">${count} samples</span>` : ""}</span><span class="timeline-description">${escapeHtml(desc.subtitle)}</span></span>
      <span class="timeline-source ${fill ? "" : "bot"}"><i class="source-dot ${fill ? "exchange" : "bot"}"></i>${fill ? "Exchange" : "Bot"}</span>
      <span class="timeline-order">${escapeHtml(event.payload.order_id ?? "Run state")}</span></button>`;
  }).join("") : '<div class="empty">No events match these filters. Try another source or clear your search.</div>';
  $("timeline-status").textContent = `${groups.length} ${state.detail === "key" ? "timeline entries" : "events"} · ${state.data.unique_events} records in run`;
  document.querySelectorAll("[data-event]").forEach((button) => button.addEventListener("click", () => {
    state.selected = button.dataset.event;
    renderTimeline();
    document.querySelector(`[data-event="${CSS.escape(state.selected)}"]`)?.focus({ preventScroll: true });
  }));
  const selected = groups.find(({ event }) => event.event_id === state.selected);
  renderInspector(selected);
}

function renderInspector(group) {
  if (!group) { $("inspector-content").innerHTML = '<div class="inspector-body"><h3>No event selected</h3><p class="detail-subtitle">Clear a filter to inspect the preserved evidence.</p></div>'; return; }
  const { event, count } = group, p = event.payload, fill = isFill(event), desc = description(event);
  const fields = fill ? [
    ["Order", p.order_id], ["Fill", p.fill_id], ["Execution price", money(p.price_usd)],
    ["Fee", money(p.fee_usd)], ["Liquidity", p.liquidity], ["Price basis", "YES outcome"],
  ] : [
    ["Tracked order", p.order_id ?? "None in snapshot"],
    ["Reported remaining", decimal(p.reported_order_size)],
    ["Local position", decimal(p.signed_position)], ["Observation scope", "Probe run"],
    ["Local reason", p.reason ?? "None recorded"],
  ];
  fields.push(["Source time (local)", time(event.occurred_at, true)], ["Receipt time", "Unavailable"], ["Source pointer", event.source_pointer]);
  const amount = fill ? decimal(p.quantity) : decimal(p.signed_position);
  $("inspector-content").innerHTML = `<div class="inspector-body">
    <span class="detail-type ${fill ? "" : "bot"}"><i class="source-dot ${fill ? "exchange" : "bot"}"></i>${fill ? "EXCHANGE FILL" : "BOT OBSERVATION"}</span>
    <h3>${escapeHtml(desc.title)}</h3><p class="detail-subtitle">${fill ? "Execution evidence saved in the historical run." : "Local state preserved by the trading bot."}${count > 1 ? ` First of ${count} identical observations; use All events to inspect each one.` : ""}</p>
    <div class="detail-amount">${escapeHtml(amount)}<small>${fill ? "contracts filled" : "contracts held"}</small></div>
    <dl class="detail-fields">${fields.map(([label, value]) => `<div class="detail-field"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}</dl>
    <p class="detail-note">${escapeHtml(desc.note)}</p>
    <details class="raw-evidence"><summary>Inspect normalized record</summary><pre>${escapeHtml(evidenceText(event))}</pre></details>
  </div>`;
}

function renderChart() {
  const points = state.data.position_points;
  const start = new Date(state.data.events[0].occurred_at).getTime();
  const end = new Date(state.data.events.at(-1).occurred_at).getTime();
  const width = 700, left = 42, right = 15, top = 22, bottom = 130;
  const max = Math.max(1, ...points.map((p) => Math.abs(Number(p.value))));
  const min = Math.min(0, ...points.map((p) => Number(p.value)));
  const range = max - min;
  const x = (t) => left + (new Date(t).getTime() - start) / Math.max(1, end - start) * (width - left - right);
  const y = (v) => bottom - (Number(v) - min) / range * (bottom - top);
  const net = points.filter((p) => p.series === "net_fills");
  const bot = points.filter((p) => p.series === "bot_position");
  $("final-report-note").innerHTML = `Last plotted bot sample: ${escapeHtml(decimal(bot.at(-1)?.value))} contracts. <strong>Final bot report: ${escapeHtml(decimal(state.data.fixture.reported_final_position))} contracts</strong> at ${time(state.data.fixture.reported_final_at)}. Not independently reconciled.`;
  let netPath = `M ${left} ${y(0)}`;
  for (const p of net) netPath += ` H ${x(p.occurred_at)} V ${y(p.value)}`;
  netPath += ` H ${width - right}`;
  let botPath = "";
  bot.forEach((p, i) => { botPath += i === 0 ? `M ${x(p.occurred_at)} ${y(p.value)}` : ` H ${x(p.occurred_at)} V ${y(p.value)}`; });
  const ticks = [min, min + range / 2, max];
  const netDots = net.map((p) => `<circle class="chart-fill-dot" cx="${x(p.occurred_at)}" cy="${y(p.value)}" r="3"><title>Net fills ${escapeHtml(decimal(p.value))} at ${time(p.occurred_at, true)}</title></circle>`).join("");
  const chartDescription = `Net recorded fills end at ${decimal(net.at(-1)?.value)} contracts. The last local sample reports ${decimal(bot.at(-1)?.value)}. These sources are not independent and their timestamps do not establish a confirmed mismatch.`;
  $("position-chart").innerHTML = `<svg viewBox="0 0 700 164" role="img" aria-labelledby="chart-title chart-desc"><title id="chart-title">Bot position and net fill quantity over this run</title><desc id="chart-desc">${escapeHtml(chartDescription)}</desc>
    ${ticks.map((v) => `<line class="chart-grid" x1="${left}" x2="${width - right}" y1="${y(v)}" y2="${y(v)}"/><text class="chart-text" x="0" y="${y(v) + 3}">${v.toFixed(2).replace(/\.00$/, "")}</text>`).join("")}
    <path class="chart-bot" d="${botPath}"/><path class="chart-net" d="${netPath}"/>${netDots}
    ${[0, .25, .5, .75, 1].map((ratio) => `<text class="chart-text" x="${left + ratio * (width - left - right)}" y="154" text-anchor="${ratio === 0 ? "start" : ratio === 1 ? "end" : "middle"}">${formatDateTime(start + ratio * (end - start), {clockOnly: true})}</text>`).join("")}
  </svg>`;
}

let toastTimer;
function toast(message) {
  clearTimeout(toastTimer); $("toast").textContent = message; $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 6500);
}
async function load(replay = false) {
  $("replay-button").disabled = true;
  try {
    const count = replay ? state.replays + 1 : state.replays;
    const response = await fetch(`/api/explorer?replays=${count}`);
    if (!response.ok) throw new Error((await response.json()).error ?? "Unable to load fixture");
    const data = await response.json();
    state.data = data; state.replays = count;
    $("load-state").hidden = true; $("explorer").hidden = $("view-select").value !== "fixture"; $("export-button").disabled = false;
    renderSummary(); renderOrders(); renderChart(); renderTimeline();
    if (replay) toast(`Replay ${count} complete. ${data.duplicates_skipped} duplicate records skipped across replays. Still ${data.fill_count} fills and ${money(data.totals.fees)} in fees.`);
  } catch (error) {
    if (state.data) toast(`Replay failed: ${error.message}`);
    else { $("load-state").textContent = `Could not load saved evidence: ${error.message}. Use Replay fixture to retry.`; $("load-state").classList.add("error"); }
  } finally { $("replay-button").disabled = false; }
}

$("replay-button").addEventListener("click", () => load(Boolean(state.data)));
$("export-button").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(state.data, null, 2) + "\n"], { type: "application/json" });
  const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = "othryss-partial-fill-evidence.json";
  link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  toast("Evidence export includes normalized records, computed totals, provenance, and coverage limits.");
});
$("all-orders").addEventListener("click", () => { state.order = null; renderOrders(); renderTimeline(); });
$("session-button").addEventListener("click", () => {
  if (!state.data) return;
  state.order = null; state.source = "all"; state.query = ""; $("event-search").value = "";
  document.querySelectorAll("[data-source]").forEach((b) => { b.classList.toggle("active", b.dataset.source === "all"); b.setAttribute("aria-pressed", b.dataset.source === "all"); });
  renderOrders(); renderTimeline(); $("main").scrollIntoView({ behavior: "auto" });
});
$("coverage-jump").addEventListener("click", () => { $("coverage").scrollIntoView({ behavior: "auto", block: "center" }); $("coverage").focus({ preventScroll: true }); });
for (const detail of ["key", "all"]) $(detail + "-events").addEventListener("click", () => {
  state.detail = detail;
  for (const kind of ["key", "all"]) { $(kind + "-events").classList.toggle("selected", kind === detail); $(kind + "-events").setAttribute("aria-pressed", kind === detail); }
  renderTimeline();
});
document.querySelectorAll("[data-source]").forEach((button) => button.addEventListener("click", () => {
  state.source = button.dataset.source;
  document.querySelectorAll("[data-source]").forEach((b) => { b.classList.toggle("active", b === button); b.setAttribute("aria-pressed", b === button); });
  renderTimeline();
}));
$("event-search").addEventListener("input", (event) => { state.query = event.target.value; renderTimeline(); });

// Workspace navigation changes presentation; source modules retain their state.
(() => {
  const pages = {
    overview: ["Overview", "A clear view of your account and what needs attention."],
    incidents: ["Incidents", "Investigate differences. Follow the evidence. Record your review."],
    orders: ["Orders", "Follow an order from intent to execution."],
    analytics: ["Execution", "Understand request timing and what happened after a fill."],
    sources: ["Data sources", "Check collection health, bot coverage, and reference prices."],
    notifications: ["Notifications", "Follow incident delivery to your configured destinations."],
    example: ["Saved example", "Explore a recorded fill and exit, with its evidence boundaries."]
  };
  let current = "overview", lastHistory = "overview", accountScope = "";
  function render(page, focus = false) {
    current = page;
    const example = page === "example";
    if (!example) lastHistory = page;
    if ($("view-select").value !== (example ? "fixture" : "history")) {
      $("view-select").value = example ? "fixture" : "history";
      $("view-select").dispatchEvent(new Event("change"));
    }
    document.querySelectorAll("[data-page]").forEach(node => { node.hidden = node.dataset.page !== page; });
    document.querySelectorAll("[data-nav]").forEach(node => {
      node.classList.toggle("active", node.dataset.nav === page);
      if (node.dataset.nav === page) node.setAttribute("aria-current", "page");
      else node.removeAttribute("aria-current");
    });
    $("page-title").textContent = pages[page][0];
    $("page-breadcrumb").textContent = pages[page][0];
    $("page-description").textContent = pages[page][1];
    $("page-description").hidden = !["overview","example"].includes(page);
    $("view-context").textContent = example ? "Saved evidence · no live connection for this example" : "";
    document.title = `${pages[page][0]} · Othryss`;
    if (focus) { $("main").focus({preventScroll:true}); window.scrollTo({top:0,behavior:"instant"}); }
    document.dispatchEvent(new CustomEvent("workspace-page", {detail:page}));
  }
  function navigate(page) {
    if (!pages[page]) return;
    if (location.hash === `#${page}`) render(page, true);
    else location.hash = page;
  }
  window.OthryssWorkspace = {navigate, get page() { return current; }};
  window.addEventListener("hashchange", () => {
    const page = location.hash.slice(1);
    if (pages[page]) render(page, true);
  });
  $("view-select").addEventListener("change", () => {
    const page = $("view-select").value === "fixture" ? "example" : current === "example" ? lastHistory : current;
    if (page !== current) navigate(page);
  });
  $("session-button").addEventListener("click", () => navigate("example"));
  document.addEventListener("workspace-account", ({detail:a}) => {
    const changed = a?.scope_id !== accountScope;
    accountScope = a?.scope_id ?? "";
    $("workspace-name").replaceChildren(document.createTextNode(a?.account ?? "Your workspace"));
    const small = document.createElement("small"); small.textContent = a ? `Kalshi · ${a.environment}` : "Local installation";
    $("workspace-name").append(small);
    if (changed || !a) {
      $("overview-incident-count").textContent = "—";
      $("overview-attention-title").textContent = "Checking incidents";
      $("overview-attention-note").textContent = "Waiting for monitoring evidence for this account.";
      $("overview-incidents").innerHTML = '<p class="empty">Loading incident evidence…</p>';
      $("nav-incident-count").hidden = true;
    }
  });
  document.addEventListener("workspace-sync", ({detail:s}) => {
    const fresh = s?.freshness === "recent" && s?.worker_heartbeat === "recent" && ["running","idle"].includes(s?.status) && !s?.last_error;
    const delayed = s?.freshness === "stale" && s?.worker_heartbeat === "recent" && ["running","idle"].includes(s?.status) && !s?.last_error;
    const label = fresh ? "Healthy" : !s || s.status === "not_configured" ? "Not configured" : delayed ? "Delayed" : "Needs attention";
    $("connection-chip").textContent = `Exchange: ${label}`;
    $("connection-chip").dataset.state = fresh ? "healthy" : "unknown";
    $("overview-sync-title").textContent = `Exchange connection: ${label}`;
    $("overview-connection").dataset.state = fresh ? "healthy" : "unknown";
    $("overview-sync-note").textContent = updatedAgo(s?.last_success_at);
  });
  document.addEventListener("workspace-incidents", ({detail:d}) => {
    if (!d || d.scope !== accountScope) return;
    if (!d.data) {
      $("overview-incident-count").textContent = "—";
      $("overview-attention-title").textContent = "Incident status unavailable";
      $("overview-attention-note").textContent = "Refresh local data to retry.";
      $("overview-incidents").innerHTML = '<p class="empty">Could not load incident evidence. Monitoring health is unknown.</p>';
      $("nav-incident-count").hidden = true;
      return;
    }
    const {counts,incidents,monitors} = d.data;
    const open = counts.open ?? 0, acknowledged = counts.acknowledged ?? 0, pending = counts.pending ?? 0;
    $("overview-incident-count").textContent = String(open);
    $("overview-attention-title").textContent = open ? `incident${open === 1 ? "" : "s"} to review` : "No open incidents";
    $("overview-attention-note").textContent = `${acknowledged} acknowledged · ${pending} pending. ${monitors.length ? "Check source coverage before drawing conclusions." : "Bot monitoring is not yet configured."}`;
    $("nav-incident-count").textContent = String(open); $("nav-incident-count").hidden = !open;
    const rows = incidents.filter(i => i.status === "open").slice(0,3);
    $("overview-incidents").innerHTML = rows.length ? rows.map(i => `<button class="overview-incident" data-overview-incident="${escapeHtml(i.incident_id)}"><span class="incident-marker" aria-hidden="true"></span><span><strong>${escapeHtml(i.rule.replaceAll("_"," "))}</strong><small>${escapeHtml(i.instrument_id)}</small></span><span class="assessment-pill">${escapeHtml(i.assessment.replaceAll("_"," "))}</span><span aria-hidden="true">↗</span></button>`).join("") : `<div class="overview-empty"><span aria-hidden="true">○</span><div><strong>No open incidents</strong><p>${open ? "Some open incidents are outside the retained list. Open the incident workspace to inspect coverage." : [acknowledged ? `${acknowledged} acknowledged` : "", pending ? `${pending} pending` : ""].filter(Boolean).join(" \u00b7 ")}</p></div></div>`;
    $("overview-incidents").querySelectorAll("[data-overview-incident]").forEach(button => button.onclick = () => {
      navigate("incidents");
      document.dispatchEvent(new CustomEvent("workspace-open-incident",{detail:button.dataset.overviewIncident}));
    });
  });
  render(pages[location.hash.slice(1)] ? location.hash.slice(1) : "overview");
})();
