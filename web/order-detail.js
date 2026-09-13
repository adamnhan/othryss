"use strict";
(() => {
  const esc = escapeHtml;
  const words = value => String(value ?? "unavailable").replaceAll("_", " ");
  const time = value => formatDateTime(value);
  const raw = (value, label = "Inspect evidence") => `<details class="raw-evidence"><summary>${esc(label)}</summary><pre>${esc(evidenceText(value))}</pre></details>`;
  const ms = value => value == null ? "Unavailable" : `${esc(value)} ms`;
  const usd = value => value == null ? "Unavailable" : `$${esc(value)}`;
  const classifications = {
    before_request: "Execution timestamp before cancel request",
    late_observation_only: "Earlier execution imported late",
    timing_uncertain: "Within clock guard; timing uncertain",
    cancel_in_flight_or_clock_uncertain: "Cancel in flight or clock uncertainty",
    suspected_after_cancel_response: "Suspected fill after confirmed cancel response",
    after_unconfirmed_cancel: "Fill after unconfirmed cancel; stale status unknown",
    timing_unavailable: "Fill timing unavailable"
  };
  function render(data) {
    const requests = data.requests, marks = data.markouts, incidents = data.incidents;
    const limitations = [
      data.exchange_truncated ? `Exchange records: first ${data.events.length} of ${data.total}; totals include all stored fills.` : `${data.events.length} of ${data.total} stored exchange records included.`,
      `${requests.rows.length} linked bot requests${requests.truncated ? `; only the latest ${data.limits.requests} candidate requests were inspected` : ""}.`,
      requests.excluded_requests ? `${requests.excluded_requests} candidate requests excluded because their identity or order link conflicts.` : "",
      requests.ambiguous_client_ids.length ? `${requests.ambiguous_client_ids.length} reused or ambiguous client IDs excluded from client-only linking.` : "",
      requests.client_ids_truncated ? `Client ID discovery limited to ${data.limits.client_ids}.` : "",
      requests.rows.some(r => r.trace_truncated || r.fills_truncated) ? "Some request traces or cancellation fill lists reached their evidence limit; inspect each request's flags." : "",
      `${marks.rows.length} of ${marks.total} fills included in markouts${marks.truncated ? "; latest fills only" : ""}.`,
      `Reference capture: ${words(data.references.status)}${data.references.truncated ? "; quote or gap limit reached" : ""}.`,
      incidents.truncated ? `Related incidents limited to ${data.limits.incidents}, with direct links first.` : "",
      incidents.rows.some(i => i.actions_truncated || i.first_evidence.truncated || i.latest_evidence.truncated || i.first_evidence.exchange?.truncated || i.latest_evidence.exchange?.truncated) ? "Some incident actions or check evidence exceeded retrieval limits; omissions are marked in the export." : "",
      data.timeline_truncated ? `Timeline shows the latest ${data.timeline.length} of ${data.timeline_total} assembled entries. Source sections and export retain the bounded evidence behind them.` : ""
    ].filter(Boolean);
    return `<section class="order-investigation" aria-label="Unified order investigation">
      <div class="panel-heading"><div><h2>Order details</h2><p>Captured ${esc(time(data.captured_at))}</p></div><button class="button" id="order-investigation-refresh">Refresh</button></div>
      <nav class="order-investigation-nav" aria-label="Investigation sections">${[["order-trace", "Bot requests"], ["order-markouts", "Fill markouts"], ["order-incidents", "Related incidents"], ["order-timeline", "Timeline"]].map(([id, text]) => `<a href="#${id}">${text}</a>`).join("")}</nav>
      <details class="order-coverage"><summary>Coverage · ${esc(words(data.identity.status))} subaccount${data.identity.subaccount == null ? "" : ` (${data.identity.subaccount})`}</summary><ul>${limitations.map(l => `<li>${esc(l)}</li>`).join("")}</ul><p>${esc(data.identity.coverage)}</p><p>${esc(data.export_scope)}</p></details>
      <section id="order-trace"><h3>Bot requests</h3>
        ${requests.rows.length ? requests.rows.map(r => `<article class="order-request"><strong>${esc(words(r.operation))} · ${esc(words(r.outcome))}${r.http_status ? ` · HTTP ${r.http_status}` : ""}</strong><p>${esc(time(r.started_at))} · Strategy: ${esc(r.strategy_id ?? "unknown")}</p>

          <p>${r.timing_status === "measured" ? `Request ${ms(r.elapsed_ms)}${r.retries || r.transport_ms!==r.elapsed_ms ? ` · transport total ${ms(r.transport_ms)}` : ""}${r.retries ? ` · ${r.retries} retries` : ""}` : `Timing unavailable: ${esc(words(r.reason))}`}</p>
          ${r.attempts.length > 1 ? `<p>HTTP attempts: ${r.attempts.map(a => `${a.attempt}: ${ms(a.elapsed_ms)} (${a.http_status ? `HTTP ${a.http_status}` : "outcome unknown"})`).join(" · ")}</p>` : ""}
          <p>Linked by ${esc(words(r.link_basis))}.${r.related_order_ids.length ? ` Related exchange order IDs: ${r.related_order_ids.map(esc).join(", ")}. Their fills are separate.` : ""}</p>
          ${r.operation === "cancel" ? `<p>${r.cancel_confirmation === "target_reported_canceled" ? "Response reports target canceled" : "Cancellation state unconfirmed"}. ${r.fills.length ? `${r.fills.length} selected-order fills linked.` : "No selected-order fills observed."}</p>${r.fills.map(f => `<p>${esc(classifications[f.classification])} · ${esc(f.event.payload.fill_id)} · ${esc(time(f.event.occurred_at))}</p>`).join("")}` : ""}
          ${raw(r, "Request details")}</article>`).join("") : "<p>No linked bot requests; timing unavailable.</p>"}
        </section>
      <section id="order-markouts"><h3>Fill markouts</h3><p>Estimates per contract · before fees</p>
        ${marks.rows.length ? marks.rows.map(r => `<article class="order-markout"><strong>Fill ${esc(r.fill.payload.fill_id)}</strong><p>${esc(r.fill.payload.quantity)} contracts at ${usd(r.fill.payload.price_usd)} · ${esc(words(r.fill.payload.exposure_direction))} · ${esc(time(r.fill.occurred_at))}</p>
          <div class="order-horizons">${r.markouts.map(m => `<div><strong>${m.horizon_seconds}s</strong><p>${m.status === "estimate" ? `${usd(m.per_contract_usd)} / contract` : esc(words(m.status))}</p><small>${m.reason ? esc(words(m.reason)) : `Quantity-weighted: ${usd(m.quantity_weighted_usd)}`}</small>${m.reference ? `<p>Reference midpoint ${usd(m.reference.midpoint)}<br><small>Receipt ${esc(time(m.reference.received_at))}</small></p>` : ""}</div>`).join("")}</div>
          ${raw(r, "Fill details")}</article>`).join("") : "<p>No fills; markouts unavailable.</p>"}
        <details class="raw-evidence"><summary>Market reference context · ${data.references.rows.length} observations</summary><p>${esc(data.references.coverage)}</p><p>Window: ${esc(time(data.references.from))} through ${esc(time(data.references.through))}.${data.references.window_limited ? " The window excludes earlier order history." : ""}</p><pre>${esc(evidenceText(data.references))}</pre></details></section>
      <section id="order-incidents"><h3>Related incidents</h3>
        ${incidents.rows.length ? incidents.rows.map(i => `<article class="order-incident"><strong>${i.relation === "direct" ? "Direct order evidence" : "Same-market context"} · ${esc(words(i.rule))}</strong><p>${esc(i.status)} · assessment ${esc(i.assessment)} · ${esc(i.entity)}</p><p>First detected ${esc(time(i.first_seen))} · Last checked ${esc(time(i.last_seen))}</p><p>${i.actions.length ? i.actions.map(a => `${esc(words(a.action))} ${esc(time(a.occurred_at))}${a.note ? `: ${esc(a.note)}` : ""}`).join("<br>") : "No review actions"}</p>${raw(i, "Incident details")}</article>`).join("") : "<p>No linked incidents</p>"}</section>
      <section id="order-timeline"><div class="panel-heading"><h3>Timeline</h3><label>Evidence source<select id="order-timeline-source"><option value="all">All sources</option><option value="exchange">Exchange</option><option value="bot">Bot</option><option value="reference">Market reference</option><option value="incident">Incident</option></select></label></div><p id="order-timeline-count" aria-live="polite"></p><div id="history-events"></div><div class="history-pager"><button class="button" id="order-timeline-prev">Earlier</button><button class="button" id="order-timeline-next">Later</button></div></section>
    </section>`;
  }
  function bind(data, eventHtml, refresh) {
    $("order-investigation-refresh").onclick = refresh;
    let offset = 0;
    const size = 25;
    function timeline() {
      const selected = $("order-timeline-source").value;
      const rows = data.timeline.filter(e => selected === "all" || e.source === selected);
      $("order-timeline-count").textContent = `${rows.length ? offset + 1 : 0}–${Math.min(offset + size, rows.length)} of ${rows.length} entries`;
      $("order-timeline-prev").disabled = offset === 0;
      $("order-timeline-next").disabled = offset + size >= rows.length;
      $("history-events").innerHTML = rows.length ? rows.slice(offset, offset + size).map(e => e.source === "exchange" ? eventHtml(e.evidence) : `<article class="order-timeline-event" data-order-source="${esc(e.source)}"><strong>${esc(e.kind)} · ${esc(words(e.source))}${e.relation ? ` · ${esc(words(e.relation))}` : ""}</strong><p>${esc(time(e.at))} · ${esc(words(e.clock))}</p>${e.source === "bot" ? `<p>${esc(words(e.evidence.payload.operation))} · sequence ${e.evidence.sequence} · request ${esc(e.request_id)}</p>` : ""}${e.source === "reference" ? `<p>Bid ${usd(e.evidence.bid)} · ask ${usd(e.evidence.ask)} · midpoint ${usd(e.evidence.midpoint)} · ${esc(e.evidence.quality)}</p>` : ""}${raw(e.evidence)}</article>`).join("") : '<p>No entries for this source in the retained timeline.</p>';
    }
    $("order-timeline-source").onchange = () => { offset = 0; timeline(); };
    $("order-timeline-prev").onclick = () => { offset = Math.max(0, offset - size); timeline(); };
    $("order-timeline-next").onclick = () => { offset += size; timeline(); };
    timeline();
  }
  window.OthryssOrderDetail = {render, bind};
})();
