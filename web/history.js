"use strict";

(() => {
  const h = { accounts: [], scope: "", query: "", filled: false, offset: 0, orders: null, detail: null, selected: null, eventOffset: 0, listVersion: 0, detailVersion: 0, catalogVersion: 0 };
  const stamp = formatDateTime;
  const esc = escapeHtml;
  let reconId = null, reconScope = null, reconData = null;
  async function api(path, params = {}) {
    const response = await fetch(`/api/history/${path}?${new URLSearchParams(params)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error ?? "Unable to read imported history");
    return data;
  }
  function message(text, error = false) {
    $("history-message").textContent = text;
    $("history-message").hidden = !text;
    $("history-message").classList.toggle("error", error);
  }
  function clearDetail() {
    h.detailVersion++; h.detail = null; h.selected = null; h.eventOffset = 0;
    $("history-detail").innerHTML = '<div class="empty">Select an order</div>';
  }
  function coverage() {
    const account = h.accounts.find((a) => a.scope_id === h.scope);
    document.dispatchEvent(new CustomEvent("workspace-account", {detail:account}));
    $("history-order-count").textContent = account.order_count.toLocaleString();
    $("history-fill-count").textContent = (account.event_counts.ORDER_FILL ?? 0).toLocaleString();
    $("history-snapshot-count").textContent = (account.event_counts.ORDER_OBSERVATION ?? 0).toLocaleString();
    const last = account.latest_import, full = account.latest_full_traversal;
    $("history-coverage").textContent = [
      full ? `Full import: ${stamp(full.finished_at)}.` : "No full import recorded.",
      last ? `Latest import: ${last.status}${last.ticker ? ` (${last.ticker})` : ""}, ${stamp(last.finished_at ?? last.started_at)}.` : "No imports yet.",
      account.unlinked_events ? `${account.unlinked_events} unlinked records.` : "",
    ].filter(Boolean).join(" ");
    renderSync(account.sync);
    if (reconScope !== h.scope) { reconId = null; reconScope = h.scope; reconData = null; }
    loadReconciliation();
    document.dispatchEvent(new CustomEvent("history-scope", {detail: h.scope}));
  }
  async function loadReconciliation() {
    if (window.OthryssWorkspace?.page !== "sources" || !h.scope) return;
    const scope = h.scope, selected = reconId;
    try {
      const data = await api("reconciliation", { scope, ...(selected ? { check: selected } : {}) });
      if (h.scope !== scope || selected !== reconId) return;
      const content = $("reconciliation-content");
      if (!data.configured) { content.textContent = "Position reconciliation is not configured for this account."; return; }
      const wasOpen = content.querySelector("details")?.open;
      reconData = data;
      if (selected && !data.recent_checks.some((c) => c.check_id === selected)) {
        data.recent_checks.push({check_id: selected, checked_at: data.checked_at, status: data.result?.status ?? "historical"});
      }
      const result = data.result, labels = { consistent: "Consistent", unexplained_difference: "Difference · needs review", pending_timing: "Timing uncertain", waiting_baseline: "Establishing baseline", waiting_snapshot: "Waiting for another snapshot", waiting_fills: "Waiting for fill coverage", lifecycle_blocked: "Settlement / closure", unavailable: "Evidence unavailable" };
      const metrics = result ? [["BASELINE", result.baseline?.quantity], ["NET FILLS", result.net_fill_quantity], ["EXPECTED", result.expected_quantity], ["OBSERVED", result.observed_quantity], ["DIFFERENCE", result.difference]] : [];
      content.innerHTML = `<p><strong>${esc(data.target.instrument_id)}</strong> · subaccount ${data.target.subaccount} · signed YES contracts</p>
        <p class="recon-status" data-recon-status="${esc(result?.status ?? "waiting")}">${esc(result ? labels[result.status] ?? result.status : "Waiting for the first position capture")}${data.stale ? " · check is stale" : ""}</p>
        <p>${esc(result?.reason ?? "The collector will capture a baseline and compare later mature snapshots.")}</p>
        ${data.target.last_error ? `<p class="negative">${esc(data.target.last_error)} The check below is the last retained result.</p>` : ""}
        <div class="history-toolbar recon-controls"><label>Recent checks<select id="recon-check"><option value="">Latest check</option>${data.recent_checks.map((c) => `<option value="${esc(c.check_id)}" ${selected === c.check_id ? "selected" : ""}>${esc(stamp(c.checked_at))} · ${esc(labels[c.status] ?? c.status)}</option>`).join("")}</select></label>${result ? '<button class="button" id="recon-export">Export</button>' : ""}</div>
        ${result ? `<dl class="history-fields recon-values">${metrics.map(([label, value]) => `<div><dt>${label}</dt><dd>${esc(decimal(value))}</dd></div>`).join("")}</dl>
        <p>Check: ${esc(stamp(data.checked_at))}. Compared snapshot: ${esc(stamp(result.target?.received_at))}. Latest capture: ${esc(stamp(result.latest_capture?.received_at))}.</p>
        <details class="raw-evidence" ${wasOpen ? "open" : ""}><summary>Check details</summary><p>${esc(result.interpretation)}</p>
        <p>Baseline and observed quantities are exchange snapshots. Expected quantity is baseline plus signed fills. ${result.fill_count} fills contributed; ${result.fills_truncated ? "only the first 100 records are included here and in this export." : "all contributing records are included."}</p>
        ${result.fills.map((f) => `<button class="text-button recon-order" data-recon-order="${esc(f.payload.order_id)}">Inspect order ${esc(f.payload.order_id)}</button>`).join("")}
        <pre>${esc(evidenceText(result))}</pre></details>` : ""}`;
      $("recon-check").addEventListener("change", (event) => { reconId = event.target.value || null; loadReconciliation(); });
      if (result) $("recon-export").addEventListener("click", () => {
        const link = document.createElement("a");
        link.href = URL.createObjectURL(new Blob([JSON.stringify(reconData, null, 2) + "\n"], {type:"application/json"}));
        link.download = "othryss-position-check.json"; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      });
      content.querySelectorAll("[data-recon-order]").forEach((button) => button.addEventListener("click", () => {
        window.OthryssWorkspace?.navigate("orders");
        h.query = button.dataset.reconOrder; h.filled = false; h.offset = 0;
        $("history-search").value = h.query; $("history-filled").checked = false; orders();
        $("history-search-form").scrollIntoView({behavior:"auto", block:"start"});
      }));
    } catch {
      if (scope === h.scope) $("reconciliation-content").textContent = "Position check unavailable. Refresh local data to retry.";
    }
  }
  function renderSync(sync) {
    document.dispatchEvent(new CustomEvent("workspace-sync", {detail:sync}));
    const target = $("history-sync");
    if (!sync || sync.status === "not_configured") {
      target.textContent = "Collection not configured";
      return;
    }
    const current=sync.freshness==="recent" && sync.worker_heartbeat==="recent" && ["running","idle"].includes(sync.status) && !sync.last_error;
    const opened=target.querySelector("details")?.open;
    target.innerHTML=`<strong>Collection: ${current?"Current":"Needs attention"}</strong><p>${esc(updatedAgo(sync.last_success_at))}</p>${sync.last_error?`<p class="account-warning">${esc(sync.last_error)}</p>`:""}<details ${opened?"open":""}><summary>Sync details</summary><p>Status: ${esc(sync.status)} · heartbeat ${esc(sync.worker_heartbeat??"unknown")}</p><p>Last synced: ${esc(stamp(sync.last_success_at))}</p><p>Coverage through: ${esc(stamp(sync.coverage_through))}</p>${sync.next_attempt_at?`<p>Next attempt: ${esc(stamp(sync.next_attempt_at))}</p>`:""}</details>`;

  }
  let healthBusy = false;
  async function pollHealth() {
    if (healthBusy || document.hidden || $("history-view").hidden || !h.scope) return;
    healthBusy = true;
    const scope = h.scope;
    try {
      const data = await api("health", { scope });
      if (h.scope === scope) renderSync(data.sync);
      await loadReconciliation();
      document.dispatchEvent(new CustomEvent("history-scope", {detail: h.scope}));
    } catch {
      if (h.scope === scope) { $("history-sync").textContent = "Collection status unavailable. The local server could not be reached; displayed orders may be stale."; document.dispatchEvent(new CustomEvent("workspace-sync", {detail:{status:"unavailable"}})); }
    } finally { healthBusy = false; }
  }
  async function catalog() {
    const version = ++h.catalogVersion;
    h.listVersion++; clearDetail();
    $("history-content").hidden = true;
    message("Loading imported history…");
    try {
      const data = await api("accounts");
      if (version !== h.catalogVersion) return;
      h.accounts = data.accounts;
      $("history-account").innerHTML = h.accounts.map((a) => `<option value="${esc(a.scope_id)}">${esc(a.account)} · ${esc(a.venue)} / ${esc(a.environment)}</option>`).join("");
      if (!h.accounts.length) { h.scope=""; document.dispatchEvent(new CustomEvent("workspace-account", {detail:null})); document.dispatchEvent(new CustomEvent("workspace-sync", {detail:null})); message(data.message ?? "No accounts imported yet. Run onboarding to connect your account, or explore the saved bot example."); return; }
      if (!h.accounts.some((a) => a.scope_id === h.scope)) h.scope = h.accounts[0].scope_id;
      $("history-account").value = h.scope;
      h.offset = 0;
      $("history-content").hidden = false;
      coverage();
      message("");
      await orders();
    } catch (error) { if (version === h.catalogVersion) { message(`${error.message} Use Refresh local data to retry.`, true); document.dispatchEvent(new CustomEvent("workspace-sync", {detail:{status:"unavailable"}})); } }
  }
  function renderOrders() {
    const names={active:"Active",inactive:"Inactive",unknown:"Status unknown"};
    $("history-orders").innerHTML=Object.entries(h.groups).filter(([key,g])=>key!=="unknown"||g.total).map(([key,g])=>{
      const visible=g.orders.slice(0,g.visible),base=key==="active"?25:5;
      return `<section class="order-group" id="orders-${key}" aria-label="${names[key]} orders"><div class="order-group-heading"><h3>${names[key]} <span class="history-muted">${g.total.toLocaleString()}</span></h3>${key==="unknown"?"<span class='history-muted'>No confirmed status</span>":""}</div>
        ${visible.length?visible.map((order,index)=>`<button class="history-order" data-history-order="${index}" data-order-group="${key}" aria-pressed="${h.selected?.order_id===order.order_id&&h.selected?.instrument_id===order.instrument_id}"><span><strong>${esc(order.instrument_id)}</strong><small class="mono" title="${esc(order.order_id)}">${esc(order.order_id.length>24?order.order_id.slice(0,8)+"\u2026"+order.order_id.slice(-6):order.order_id)}</small></span><span>${esc(order.status??"Unknown")}</span><span>${order.fill_count} ${order.fill_count===1?"fill":"fills"}</span><span>${esc(stamp(order.last_evidence_at))}</span></button>`).join(""):`<p class="history-note">No ${key} orders</p>`}
        <div class="order-group-actions">${g.visible<g.total?`<button class="button" data-orders-more="${key}" ${g.busy?"disabled":""}>${g.busy?"Loading...":"Show more"}</button>`:""}${g.visible>base?`<button class="text-button" data-orders-less="${key}" ${g.busy?"disabled":""}>Show fewer</button>`:""}${visible.length<g.total?`<span class="history-muted">${visible.length} of ${g.total.toLocaleString()}</span>`:""}</div>${g.error?`<p class="history-note account-warning" role="status">${esc(g.error)}</p>`:""}</section>`;
    }).join("");
    document.querySelectorAll("[data-history-order]").forEach(button=>button.onclick=()=>{
      h.selected=h.groups[button.dataset.orderGroup].orders[Number(button.dataset.historyOrder)];h.eventOffset=0;
      document.querySelectorAll("[data-history-order]").forEach(b=>b.setAttribute("aria-pressed",String(b===button)));
      detail(true);
    });
    document.querySelectorAll("[data-orders-more]").forEach(button=>button.onclick=()=>moreOrders(button.dataset.ordersMore));
    document.querySelectorAll("[data-orders-less]").forEach(button=>button.onclick=()=>{const key=button.dataset.ordersLess;h.groups[key].visible=key==="active"?25:5;renderOrders();$("orders-"+key).scrollIntoView({block:"start"});});
  }
  async function moreOrders(group) {
    const version=h.listVersion,g=h.groups[group];
    if(g.busy)return;
    if(g.visible<g.orders.length){g.visible=g.orders.length;renderOrders();return;}
    g.busy=true;g.error=null;renderOrders();
    try {
      const data=await api("order-groups",{scope:h.scope,q:h.query,filled:h.filled?"1":"0",group,offset:g.orders.length,limit:25,through:h.through});
      if(version!==h.listVersion)return;
      g.orders.push(...data.groups[group].orders);g.total=data.groups[group].total;g.visible=g.orders.length;
    } catch {if(version===h.listVersion)g.error="Could not load more orders. Try again.";}
    finally {if(version===h.listVersion){g.busy=false;renderOrders();}}
  }
  async function orders() {
    if (window.OthryssWorkspace?.page !== "orders" || !h.scope) return;
    const version=++h.listVersion;
    clearDetail();h.groups={};h.through=null;
    $("history-orders").innerHTML='<div class="empty">Loading orders...</div>';
    $("history-page").textContent="";message("");
    try {
      const data=await api("order-groups",{scope:h.scope,q:h.query,filled:h.filled?"1":"0"});
      if(version!==h.listVersion)return;
      h.through=data.through;
      h.groups=Object.fromEntries(Object.entries(data.groups).map(([key,g])=>[key,{...g,visible:g.orders.length,busy:false,error:null}]));
      const total=Object.values(h.groups).reduce((sum,g)=>sum+g.total,0);
      $("history-page").textContent=total?"Latest recorded statuses":"0 orders";
      renderOrders();
      if(!total)$("history-orders").insertAdjacentHTML("beforeend",'<p class="history-note">No orders match</p>');
    } catch(error) {
      if(version!==h.listVersion)return;
      $("history-orders").innerHTML='<div class="empty">Could not load orders. Refresh to retry.</div>';message(error.message,true);
    }
  }
  async function detail(focus = false) {
    const version = ++h.detailVersion;
    h.detail = null;
    $("history-detail").innerHTML = '<div class="empty">Loading order evidence…</div>';
    try {
      const data = await api("order-investigation", { scope: h.scope, instrument: h.selected.instrument_id, order: h.selected.order_id });
      if (version !== h.detailVersion) return;
      h.detail = data;
      const p = data.latest_observation?.payload;
      const fields = p ? [["Last observed status", p.status], ["Initial quantity", decimal(p.initial_quantity)], ["Snapshot filled quantity", decimal(p.filled_quantity)], ["Snapshot remaining", decimal(p.remaining_quantity)], ["Source update time", stamp(data.latest_observation.occurred_at)], ["Imported at", stamp(data.latest_observation.received_at)]] : [["Exchange order snapshot", "Unavailable — order linked from fills only"]];
      $("history-detail").innerHTML = `<div class="panel-heading"><div><h2>${esc(data.instrument_id)}</h2><p class="mono">${esc(data.order_id)}</p><p>${esc(data.account.account)} · ${esc(data.account.environment)} · strategy unknown</p></div><button class="button" id="history-export">Export</button></div>
        <div class="metrics history-detail-metrics">
          ${[["RECORDED FILLS", String(data.fill_count)], ["FILLED QUANTITY", decimal(data.totals.volume)], ["EXECUTION FEES", money(data.totals.fees)], ["NET FILL CASH FLOW", money(data.totals.net_cash_flow)]].map(([label, value]) => `<article class="metric"><div class="label">${label}</div><div class="metric-value">${esc(value)}</div></article>`).join("")}
        </div><p class="history-note">Imported fills · YES contracts · cash flow excludes settlements</p>
        <dl class="history-fields">${fields.map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value ?? "Unavailable")}</dd></div>`).join("")}</dl>
        ${window.OthryssOrderDetail.render(data)}`;
      window.OthryssOrderDetail.bind(data, eventHtml, () => detail());
      $("history-export").addEventListener("click", exportOrder);
      if (focus) { $("history-detail").scrollIntoView({ behavior: "auto", block: "start" }); $("history-detail").focus({ preventScroll: true }); }
    } catch (error) {
      if (version !== h.detailVersion) return;
      $("history-detail").innerHTML = `<div class="empty">${esc(error.message)} Select the order again to retry.</div>`;
    }
  }
  function eventHtml(event) {
    const p = event.payload, fill = event.type === "ORDER_FILL";
    const direction = p.exposure_direction === "increase_yes" ? "Increase YES" : p.exposure_direction === "decrease_yes" ? "Decrease YES" : "Direction unavailable";
    const title = fill ? `${p.liquidity ?? "Unknown liquidity"} fill · ${direction}` : `Exchange order snapshot · ${p.status ?? "unknown status"}`;
    const subtitle = fill ? `${decimal(p.quantity)} contracts at ${money(p.price_usd)} · ${money(p.fee_usd)} fee` : `${decimal(p.filled_quantity)} snapshot filled · ${decimal(p.remaining_quantity)} remaining`;
    return `<article class="history-event"><div><strong>${esc(title)}</strong><p>${esc(subtitle)}</p><p class="mono">${esc(stamp(event.occurred_at ?? event.received_at))}${event.occurred_at ? "" : " · receipt time; source time unavailable"}</p></div>
      <details class="raw-evidence"><summary>Record details</summary><p>Imported at ${esc(stamp(event.received_at))}. ${event.evidence.length} of ${event.evidence_count} source references.</p><pre>${esc(evidenceText(event))}</pre></details></article>`;
  }
  function exportOrder() {
    if (!h.detail) return;
    const result = h.detail;
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2) + "\n"], {type: "application/json"}));
    link.download = "othryss-order-evidence.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    toast(`Exported this investigation: ${result.events.length} exchange records and ${result.requests.rows.length} bot requests, with coverage details.`);
  }
  function switchView() {
    const history = $("view-select").value === "history";
    $("history-view").hidden = !history;
    $("explorer").hidden = history || !state.data;
    $("load-state").hidden = history || Boolean(state.data);
    $("export-button").hidden = history; $("replay-button").hidden = history;
    if (!history && !state.data) load();
  }
  $("view-select").addEventListener("change", switchView);
  $("session-button").addEventListener("click", () => { $("view-select").value = "fixture"; switchView(); });
  $("history-refresh").addEventListener("click", catalog);
  $("history-account").addEventListener("change", () => { h.scope = $("history-account").value; h.offset = 0; coverage(); orders(); });
  $("history-search-form").addEventListener("submit", (event) => { event.preventDefault(); window.OthryssWorkspace?.navigate("orders"); h.query = $("history-search").value.trim(); h.filled = $("history-filled").checked; h.offset = 0; orders(); });
  document.addEventListener("workspace-page", () => {
    if (!h.scope) return;
    if (window.OthryssWorkspace?.page === "orders") orders();
    if (window.OthryssWorkspace?.page === "sources") loadReconciliation();
  });
  // All deferred page scripts must register before the first account event.
  document.addEventListener("DOMContentLoaded", () => { switchView(); catalog(); }, {once:true});
  setInterval(pollHealth, 15000);
})();
