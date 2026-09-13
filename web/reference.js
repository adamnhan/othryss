"use strict";
(() => {
  const esc = escapeHtml;
  let scope = "", instrument = "", version = 0;
  const money = value => value == null ? "Unavailable" : `$${esc(value)}`;
  async function refresh() {
    const generation = ++version;
    try {
      const response = await fetch(`/api/history/references?${new URLSearchParams({scope, instrument, limit: "200"})}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      if (version !== generation) return;
      const worker = data.worker;
      $("reference-content").innerHTML = `<p><strong>${!worker ? "Capture not started" : worker.stale ? "Worker heartbeat stale" : esc(worker.status.replaceAll("_", " "))}</strong>${worker?.last_error ? ` · ${esc(worker.last_error)}` : ""}</p>

        ${data.markets.length ? data.markets.map((m, i) => `<button class="history-order" data-reference-market="${i}" aria-pressed="${m.instrument_id === instrument}"><span><strong>${esc(m.instrument_id)}</strong><small>${!m.selected ? "No longer watched" : m.open_gap ? "Capture gap" : m.stale ? "Stale / awaiting capture" : esc(m.status.replaceAll("_", " "))} · ${m.eligible ? "Usable reference" : "Unavailable for comparison"}</small></span><span>Bid ${money(m.quote?.bid)}<small>Ask ${money(m.quote?.ask)}</small></span><span>Mid ${money(m.eligible ? m.quote?.midpoint : null)}<small>${esc(formatDateTime(m.quote?.received_at, {fallback: "No snapshot yet"}))}</small></span></button>`).join("") : "<p>No watched markets</p>"}
        <details class="method-details"><summary>Coverage</summary><p>${esc(data.coverage ?? "Start reference capture to collect quotes.")}</p></details>`;
      document.querySelectorAll("[data-reference-market]").forEach(b => b.onclick = () => {
        instrument = data.markets[Number(b.dataset.referenceMarket)].instrument_id;
        refresh();
      });
      $("reference-detail").innerHTML = `<h3>${instrument ? esc(instrument) : "All watched markets"}</h3>
        <p>${data.quotes.length} quotes · ${data.gaps.length} gaps (latest 200 / 50)</p>
        <button class="button" id="reference-export">Export</button>
        ${instrument ? '<button class="button" id="reference-all">Show all markets</button>' : ""}
        <details class="raw-evidence"><summary>Quotes</summary><pre>${esc(evidenceText(data.quotes))}</pre></details>
        <details class="raw-evidence"><summary>Gaps</summary><pre>${esc(evidenceText(data.gaps))}</pre></details>`;
      if ($("reference-all")) $("reference-all").onclick = () => { instrument = ""; refresh(); };
      $("reference-export").onclick = () => {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2) + "\n"], {type:"application/json"}));
        a.download = "othryss-reference-prices.json"; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      };
    } catch {
      if (version !== generation) return;
      $("reference-content").textContent = "Reference capture unavailable. Refresh local data to retry.";
      $("reference-detail").textContent = "";
    }
  }
  onHistoryScope(["sources"], event => {
    if (scope !== event.detail) {
      instrument = "";
      $("reference-content").textContent = "Loading reference prices…";
      $("reference-detail").textContent = "";
    }
    scope = event.detail;
    refresh();
  });
})();
