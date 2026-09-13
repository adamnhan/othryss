"use strict";
(() => {
  const esc = escapeHtml;
  let scope = "", selected = null, version = 0;
  const stopped=s=>s.health?.stopped || s.source_status==="stopped";
  function sessionRow(s) {
    const h=s.health;
    const state=!h?"Heartbeat unavailable":stopped(s)?"Producer stopped":s.stale?"Heartbeat stale":s.source_status==="healthy"?"Healthy":s.source_status??"Heartbeat recent";
    const issues=[s.sequence_gaps?`${s.sequence_gaps} sequence gaps`:"",h?.dropped?`${h.dropped} dropped`:"",h?.write_failures?`${h.write_failures} write failures`:"",h?.heartbeat_failures?`${h.heartbeat_failures} heartbeat errors`:"",h?.queue_depth?`${h.queue_depth} queued`:"",h?.capped?"Spool capacity reached":""].filter(Boolean);
    return `<article class="source-session"><strong>${esc(h?.instrument_id??s.session_id)}</strong><p>${esc(state)} · ${s.records} records</p>${issues.length?`<p class="account-warning">${issues.map(esc).join(" · ")} (session totals)</p>`:""}${s.source_reason && s.source_status!=="healthy" && !stopped(s)?`<p>${esc(s.source_reason)}</p>`:""}</article>`;
  }
  async function api(path, params) {
    const response = await fetch(`/api/history/${path}?${new URLSearchParams(params)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error ?? "Telemetry unavailable");
    return data;
  }
  async function detail(session, request) {
    selected = `${session}:${request}`;
    const current = scope, selection = selected;
    try {
      const data = await api("telemetry-request", {scope, session, request});
      if (scope !== current || selection !== selected) return;
      $("telemetry-detail").innerHTML = `<h3>Request trace · ${esc(data.strategy_id)} (bot-declared)</h3>
        <p>${data.telemetry_truncated ? "Trace limited to 200 records." : ""} ${data.exchange_events.length} of ${data.exchange_event_count} exchange records</p>
        ${data.records.map(r => `<article class="history-event"><strong>${esc(r.type)} · ${esc(r.payload.operation)}</strong><p>${esc(r.payload.outcome ?? r.payload.intent_scope ?? "")}${r.payload.http_status ? ` · HTTP ${esc(r.payload.http_status)}` : ""}${r.payload.duration_ns ? ` · ${esc((Number(r.payload.duration_ns) / 1e6).toFixed(2))} ms ${r.type === "HTTP_RESPONSE" ? "HTTP round trip" : "request elapsed, including retries"}` : ""}</p><details class="raw-evidence"><summary>Record details</summary><pre>${esc(evidenceText(r))}</pre></details></article>`).join("")}
        <h3>Linked exchange evidence</h3><p>${data.order_ids.length ? data.order_ids.map(esc).join(" · ") : "No linked order"}</p>
        ${data.exchange_events.map(e => `<details class="raw-evidence"><summary>${esc(e.type)} · ${esc(e.payload.order_id)}</summary><pre>${esc(evidenceText(e))}</pre></details>`).join("")}
        <button class="button" id="telemetry-export">Export</button>`;
      $("telemetry-export").onclick = () => {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2) + "\n"], {type:"application/json"}));
        a.download = "othryss-request-trace.json"; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      };
    } catch (error) { if (scope === current && selection === selected) $("telemetry-detail").textContent = error.message; }
  }
  onHistoryScope(["sources"], async event => {
    if (scope !== event.detail) { selected = null; $("telemetry-detail").textContent = ""; }
    scope = event.detail;
    const generation = ++version;
    try {
      const data = await api("telemetry", {scope});
      if (version !== generation) return;
      const retiredOpen=document.querySelector("#telemetry-content .retired-sources")?.open;
      $("telemetry-content").innerHTML = `${data.file_errors ? `<p class="negative">${data.file_errors} spool files could not be verified. Import checkpoints retained.</p>` : ""}
        ${data.sessions.length ? data.sessions.filter(s=>!stopped(s)).map(sessionRow).join("") : "<p>No bot telemetry captured</p>"}
        ${data.sessions.some(stopped)?`<details class="retired-sources" ${retiredOpen?"open":""}><summary>${data.sessions.filter(stopped).length} stopped sessions</summary>${data.sessions.filter(stopped).map(sessionRow).join("")}</details>`:""}
        <p>Latest ${data.requests.length} requests</p>
        ${data.requests.map((r, i) => `<button class="history-order" data-telemetry-request="${i}"><span><strong>${esc(r.instrument_id)}</strong><small>${esc(formatDateTime(r.started_at))}</small></span><span>${r.records} bot records</span><span class="mono">${esc(r.request_id)}</span></button>`).join("")}`;
      document.querySelectorAll("[data-telemetry-request]").forEach(b => b.onclick = () => {
        const r = data.requests[Number(b.dataset.telemetryRequest)]; detail(r.session_id, r.request_id);
      });
      if (selected) { const [session, request] = selected.split(":"); detail(session, request); }
    } catch { if (version === generation) $("telemetry-content").textContent = "Telemetry unavailable. Refresh local data to retry."; }
  });
})();
