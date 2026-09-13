"use strict";
(() => {
  const esc=escapeHtml,usd=v=>v==null?"Unavailable":`$${esc(v)}`;
  let scope="",version=0,offset=0,fillWindow="24h";
  async function refresh() {
    const generation=++version;
    try {
      const response=await fetch(`/api/history/account-risk?${new URLSearchParams({scope,window:fillWindow,offset})}`);
      const d=await response.json();if(!response.ok)throw new Error(d.error);
      if(version!==generation)return;
      if(!d.available){$("account-risk-content").textContent=d.reason;return;}
      const f=d.fills;
      const detailsOpen=$("account-evidence-details")?.open;
      const c=d.settings.config,limitsOpen=$("account-limits")?.open;
      const excluded=f.unknown_subaccount_excluded+f.other_subaccounts_excluded+f.invalid_fills_excluded;
      const warnings=[];
      if(!d.positions_fresh)warnings.push("Inventory unavailable or stale");
      if(!d.balance_fresh)warnings.push("Balance unavailable or stale");
      if(excluded)warnings.push(`${excluded} fills excluded`);
      if(f.truncated)warnings.push("Fill totals are partial");
      if(d.sync.freshness!=="recent")warnings.push("Fill collection is not current");
      if(c.enabled && d.risk_check?.status==="difference")warnings.push("Inventory limit exceeded");
      else if(c.enabled && (!d.risk_check || d.risk_check.status!=="clear"))warnings.push("Inventory assessment unavailable");
      $("account-risk-content").innerHTML=`<div class="snapshot-status"><span class="assessment-pill" data-state="${d.positions_fresh&&d.balance_fresh?"healthy":"unknown"}">${d.positions_fresh&&d.balance_fresh?esc(updatedAgo(d.observed_at)):"Snapshot needs attention"}</span></div>
        ${warnings.length?`<p class="account-warning" role="status">${warnings.map(esc).join(" · ")}</p>`:""}
        <div class="metrics history-metrics">${[["AVAILABLE BALANCE",d.balance_fresh?usd(d.balance.available_balance_usd):"Unavailable"],["POSITION VALUE",d.balance_fresh?usd(d.balance.portfolio_value_usd):"Unavailable"],["CONTRACTS HELD",d.absolute_contracts==null?"Unavailable":esc(d.absolute_contracts)],["MARKETS HELD",d.positions_fresh?d.position_count:"Unavailable"]].map(([label,value])=>`<article class="metric"><div class="label">${label}</div><div class="metric-value">${value}</div></article>`).join("")}</div>
        <div class="inventory-heading"><h3>Positions</h3></div>
        ${d.positions.length?`${d.positions_fresh?"":"<p>Last captured positions</p>"}<div class="inventory-columns"><span>MARKET</span><span>YES CONTRACTS</span></div>${d.positions.map(p=>`<div class="account-position"><strong>${esc(p.instrument_id)}</strong><span><b>${esc(p.quantity)}</b></span></div>`).join("")}`:`<p class="inventory-empty">${d.positions_fresh?"No open positions":"Current inventory unavailable"}</p>`}
        <div class="history-pager" ${d.position_count<=25?"hidden":""}><button class="button" id="account-prev" ${offset===0?"disabled":""}>Previous</button><span>${d.positions.length?offset+1:0}–${offset+d.positions.length} of ${d.position_count}</span><button class="button" id="account-next" ${offset+d.positions.length>=d.position_count?"disabled":""}>Next</button></div>
        ${c.enabled?`<details id="account-limits" class="account-limits" ${limitsOpen?"open":""}><summary>Inventory monitoring: On</summary>
          <dl class="history-fields">${c.per_market_limit!=null?`<div><dt>Per market</dt><dd>${esc(c.per_market_limit)} contracts</dd></div>`:""}${c.total_limit!=null?`<div><dt>Total</dt><dd>${esc(c.total_limit)} contracts</dd></div>`:""}<div><dt>Alert delay</dt><dd>${c.grace_seconds}s</dd></div></dl>
          ${c.overrides.map(r=>`<p>${esc(r.ticker)}: ${esc(r.limit)} contracts</p>`).join("")}
        </details>`:'<p id="account-limits" class="account-limits">Inventory monitoring: Off</p>'}
        <div class="account-fills-heading"><h3>Fills</h3><select id="account-window" aria-label="Fill summary window">${[["24h","Last 24 hours"],["7d","Last 7 days"],["all","All imported fills"]].map(([value,label])=>`<option value="${value}" ${value===fillWindow?"selected":""}>${label}</option>`).join("")}</select></div>
        <p class="account-fill-totals"><strong>${f.fill_count} ${f.fill_count===1?"fill":"fills"}</strong> · ${esc(f.volume_contracts)} contracts · ${usd(f.fees_usd)} fees</p>
        <details id="account-evidence-details" class="overview-evidence" ${detailsOpen?"open":""}><summary>Details</summary><p>${esc(d.coverage)}</p><p>Snapshot: ${esc(formatDateTime(d.observed_at,{fallback:"Not captured"}))}</p>
          <h3>Imported fills & fees</h3><p>${esc(f.coverage)}</p><p>Maker ${f.liquidity_counts.maker} · taker ${f.liquidity_counts.taker} · unknown ${f.liquidity_counts.unknown}</p>
          <p>Excluded: ${f.unknown_subaccount_excluded} with unknown subaccount, ${f.other_subaccounts_excluded} from other subaccounts, ${f.invalid_fills_excluded} invalid records.${f.truncated?" Scan limited to the latest 100,000 records.":""} Collection: ${esc(d.sync.freshness??"unknown")}.</p>
          ${f.markets.map(m=>`<div class="account-position"><strong>${esc(m.instrument_id)}</strong><span>${m.fill_count} fills · ${esc(m.volume_contracts)} contracts · ${usd(m.fees_usd)} fees</span></div>`).join("")}
          ${f.markets_truncated?`<p>Showing 100 of ${f.market_count} markets; totals cover all scanned valid fills.</p>`:""}
        </details><button class="text-button account-export" id="account-export">Export</button>`;
      $("account-window").onchange=event=>{fillWindow=event.target.value;refresh();};
      $("account-prev").onclick=()=>{offset=Math.max(0,offset-25);refresh();};$("account-next").onclick=()=>{offset+=25;refresh();};
      $("account-export").onclick=()=>{const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([JSON.stringify(d,null,2)+"\n"],{type:"application/json"}));a.download="othryss-account-overview.json";a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);};
    } catch {if(version===generation){$("account-risk-content").textContent="Account unavailable. Refresh to retry.";}}
  }
  onHistoryScope(["overview"], event=>{
    if(scope!==event.detail){scope=event.detail;offset=0;fillWindow="24h";$("account-risk-content").textContent="Loading account...";}
    scope=event.detail;refresh();
  });
})();
