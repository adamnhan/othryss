"use strict";
(() => {
  const esc=escapeHtml;
  let scope="", instrument="", order="", offset=0, version=0;
  const reasons={invalid_fill:"Unsupported fill evidence",capture_missing_at_fill:"No retained capture at fill time",quote_too_old:"No quote within 1 second of target",capture_gap:"Capture gap",awaiting_confirmation:"Waiting for reference confirmation",confirmation_missing:"Reference confirmation missing",horizon_not_elapsed:"Horizon not elapsed",invalid_reference:"Invalid or incomplete book",timing_uncertain:"Quote timing uncertain",connection_changed:"Capture connection changed",observation_gap:"Observation gap exceeds 20 seconds",clock_discontinuity:"Local clock discontinuity",sequence_reversal:"Reference sequence invalid",reference_scope_mismatch:"Reference scope mismatch",evidence_limit:"Evidence window exceeds query limit"};
  const money=value=>value==null?"Unavailable":`$${esc(value)}`;
  async function refresh() {
    const generation=++version;
    const opened=new Set([...$("markouts-content").querySelectorAll("details[open]")].map(d=>d.dataset.fill));
    try {
      const response=await fetch(`/api/history/markouts?${new URLSearchParams({scope,instrument,order,offset,limit:25})}`);
      const data=await response.json();
      if(!response.ok) throw new Error(data.error);
      if(generation!==version) return;
      $("markouts-content").innerHTML=`
        <div class="metrics history-metrics">${data.summary.map(s=>`<article class="metric"><div class="label">${s.horizon_seconds}s · displayed fills</div><div class="markout-value">${money(s.quantity_weighted_mean_usd)} / contract</div><p>${s.estimated_fills} estimates · ${s.pending_fills} pending · ${s.unavailable_fills} unavailable</p></article>`).join("")}</div>
        <details class="method-details"><summary>Calculation details</summary><p>${esc(data.policy.interpretation)}</p><p>${esc(data.coverage)}</p></details>
        <button class="button" id="markouts-export">Export</button>
        ${data.rows.length?data.rows.map((r,i)=>`<article class="markout-row"><strong>${esc(r.fill.instrument_id)}</strong><p>${esc(formatDateTime(r.fill.occurred_at))} · ${esc(r.fill.payload.quantity)} contracts · ${esc(r.fill.payload.exposure_direction.replaceAll("_"," "))} at ${money(r.fill.payload.price_usd)}</p>
          <div class="markout-horizons">${r.markouts.map(m=>`<div><strong>${m.horizon_seconds}s</strong><p>${m.status==="estimate"?`${money(m.per_contract_usd)} / contract <small>Estimate</small>`:m.status==="pending"?"Pending":"Unavailable"}</p><small>${m.reason?esc(reasons[m.reason]??m.reason):`Quote age ${esc(m.quote_age_seconds)}s · quantity-weighted ${money(m.quantity_weighted_usd)}`}</small></div>`).join("")}</div>
          <button class="text-button" data-markout-order="${i}">View order</button>
          <details class="raw-evidence" data-fill="${esc(r.fill.event_id)}" ${opened.has(r.fill.event_id)?"open":""}><summary>Fill details</summary><pre>${esc(evidenceText(r))}</pre></details></article>`).join(""):"<p>No matching fills</p>"}
        <div class="history-pager"><button class="button" id="markouts-prev" ${offset===0?"disabled":""}>Previous fills</button><span>${data.total?offset+1:0}–${Math.min(offset+data.rows.length,data.total)} of ${data.total} fills</span><button class="button" id="markouts-next" ${offset+data.rows.length>=data.total?"disabled":""}>Next fills</button></div>`;
      $("markouts-prev").onclick=()=>{offset=Math.max(0,offset-25);refresh();};
      $("markouts-next").onclick=()=>{offset+=25;refresh();};
      document.querySelectorAll("[data-markout-order]").forEach(b=>b.onclick=()=>{
        $("history-search").value=data.rows[Number(b.dataset.markoutOrder)].fill.payload.order_id;
        $("history-search-form").requestSubmit();
        $("history-search-form").scrollIntoView({block:"start"});
      });
      $("markouts-export").onclick=()=>{
        const a=document.createElement("a");
        a.href=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)+"\n"],{type:"application/json"}));
        a.download="othryss-fill-markouts.json";a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);
      };
    } catch {
      if(generation===version) $("markouts-content").textContent="Fill markouts unavailable. Refresh local data to retry.";
    }
  }
  $("markouts-filter").onsubmit=event=>{event.preventDefault();instrument=$("markouts-market").value.trim();order=$("markouts-order").value.trim();offset=0;refresh();};
  onHistoryScope(["analytics"], event=>{
    if(scope!==event.detail){instrument="";order="";offset=0;$("markouts-market").value="";$("markouts-order").value="";$("markouts-content").textContent="Loading fill markouts…";}
    scope=event.detail;refresh();
  });
})();
