"use strict";
(() => {
  const esc = escapeHtml;
  let scope = "", instrument = "", operation = "", offset = 0, version = 0;
  const outcomes = {http_success:"HTTP success", http_error:"HTTP error", unknown:"Outcome unknown"};
  const classes = {
    before_request:"Execution timestamp before cancel request",
    late_observation_only:"Earlier execution imported late",
    timing_uncertain:"Within clock guard; timing uncertain",
    cancel_in_flight_or_clock_uncertain:"Cancel in flight or clock uncertainty",
    suspected_after_cancel_response:"Suspected fill after confirmed cancel response",
    after_unconfirmed_cancel:"Fill after unconfirmed cancel; stale status unknown",
    timing_unavailable:"Fill timing unavailable"
  };
  const ms = value => value == null ? "Unavailable" : `${esc(value)} ms`;
  async function refresh() {
    const generation = ++version;
    const opened = new Set([...$("execution-content").querySelectorAll("details[open]")].map(d => d.dataset.request));
    try {
      const response = await fetch(`/api/history/execution?${new URLSearchParams({scope,instrument,operation,offset,limit:25})}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      if (generation !== version) return;
      $("execution-content").innerHTML = `<p>${data.summary.requests} requests · ${data.summary.unavailable} timings unavailable</p>
        <div class="execution-metrics">${data.summary.latency.map(s => `<article class="metric"><strong>${esc(s.operation)} · ${esc(outcomes[s.outcome])}</strong><p>${s.samples} samples · p50 ${ms(s.p50_ms)} · p95 ${ms(s.p95_ms)}</p></article>`).join("")}</div>
        <details class="method-details"><summary>Calculation details</summary><p>${esc(data.policy.latency)}</p><p>Percentiles use this page; p95 needs 20 samples per operation and outcome.</p><p>${esc(data.policy.fills)}</p><p>${esc(data.policy.coverage)}</p></details>
        ${Object.entries(data.summary.fill_classifications).map(([key,count]) => `<p>${esc(classes[key])}: ${count} distinct fills</p>`).join("")}
        <button class="button" id="execution-export">Export</button>
        ${data.rows.length ? data.rows.map((r,i) => `<article class="execution-row"><strong>${esc(r.operation)} · ${esc(r.instrument_id)}</strong><p>${esc(formatDateTime(r.started_at))} · ${esc(outcomes[r.outcome])}${r.http_status ? ` (${r.http_status})` : ""}</p>
          <p>${r.timing_status === "measured" ? `Request ${ms(r.elapsed_ms)}${r.retries || r.transport_ms!==r.elapsed_ms ? ` · transport total ${ms(r.transport_ms)}` : ""}${r.retries ? ` · ${r.retries} retries` : ""}` : `Timing unavailable: ${esc(r.reason?.replaceAll("_"," ") ?? "incomplete evidence")}`}</p>
          ${r.attempts.length > 1 ? `<p>HTTP attempts: ${r.attempts.map(a => `${a.attempt}: ${ms(a.elapsed_ms)} (${a.http_status ? `HTTP ${a.http_status}` : "unknown outcome"})`).join(" · ")}</p>` : ""}
          ${r.operation === "cancel" ? `<p>${r.cancel_confirmation === "target_reported_canceled" ? "Response reports target canceled" : "Cancellation state unconfirmed"}. ${r.fills.length ? `${r.fills.length} linked fills shown.` : "No matching fills observed."}${r.fills_truncated ? " Fill evidence limited to latest 100." : ""}${!r.wall_clock_valid ? " Wall-clock comparison unavailable." : ""}</p>${r.fills.map(f => `<p class="execution-fill">${esc(classes[f.classification])} · ${esc(formatDateTime(f.event.occurred_at))} · ${esc(f.event.payload.quantity)} contracts</p>`).join("")}` : ""}
          ${r.order_id ? `<button class="text-button" data-execution-order="${i}">View order</button>` : ""}
          <details class="raw-evidence" data-request="${esc(r.session_id + ':' + r.request_id)}" ${opened.has(r.session_id + ':' + r.request_id) ? "open" : ""}><summary>Request details</summary><pre>${esc(evidenceText(r))}</pre></details></article>`).join("") : "<p>No matching requests</p>"}
        <div class="history-pager"><button class="button" id="execution-prev" ${offset === 0 ? "disabled" : ""}>Previous requests</button><span>${data.rows.length ? offset + 1 : 0}–${offset + data.rows.length}</span><button class="button" id="execution-next" ${data.has_more ? "" : "disabled"}>Next requests</button></div>`;
      $("execution-prev").onclick = () => {offset = Math.max(0,offset-25);refresh();};
      $("execution-next").onclick = () => {offset += 25;refresh();};
      document.querySelectorAll("[data-execution-order]").forEach(b => b.onclick = () => {
        $("history-search").value = data.rows[Number(b.dataset.executionOrder)].order_id;
        $("history-search-form").requestSubmit();
        $("history-search-form").scrollIntoView({block:"start"});
      });
      $("execution-export").onclick = () => {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([JSON.stringify(data,null,2)+"\n"],{type:"application/json"}));
        a.download = "othryss-execution-analysis.json";a.click();setTimeout(() => URL.revokeObjectURL(a.href),1000);
      };
    } catch {
      if (generation === version) $("execution-content").textContent = "Execution analysis unavailable. Refresh local data to retry.";
    }
  }
  $("execution-filter").onsubmit = event => {event.preventDefault();instrument = $("execution-market").value.trim();operation = $("execution-operation").value;offset=0;refresh();};
  onHistoryScope(["analytics"], event => {
    if (scope !== event.detail) {instrument="";operation="";offset=0;$("execution-market").value="";$("execution-operation").value="";$("execution-content").textContent="Loading execution analysis…";}
    scope=event.detail;refresh();
  });
})();
