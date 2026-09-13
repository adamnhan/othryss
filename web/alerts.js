"use strict";
(() => {
  let version=0,scope="";
  const esc=escapeHtml;
  const labels={accepted:"Provider accepted",pending:"Queued",retry:"Retry scheduled",sending:"Sending",unknown:"Delivery unknown",failed:"Delivery failed",expired:"Expired",suppressed:"Superseded by incident state",canceled:"Canceled by configuration change"};
  onHistoryScope(["notifications"], async event=>{
    if(scope!==event.detail)$("alerts-content").textContent="Loading delivery status…";
    scope=event.detail;
    const current=++version;
    try {
      const response=await fetch(`/api/history/alerts?${new URLSearchParams({scope:event.detail})}`);
      const data=await response.json();if(!response.ok)throw new Error();
      if(current!==version)return;
      const stale=!data.worker||Date.now()/1000-data.worker.heartbeat_at>120;
      $("alerts-content").innerHTML=`<p>Delivery worker: ${stale?"not reporting recently":esc(data.worker.status.replaceAll("_"," "))}</p>
        ${data.routes.length?data.routes.map(r=>`<p><strong>${esc(r.route_id)}${r.route_id===r.kind?"":` · ${esc(r.kind)}`}</strong> · ${r.active?"Enabled":esc(r.error?.replaceAll("_"," ")??"Disabled")}</p>`).join(""):"<p>No notification destinations configured</p>"}
        <p>Latest ${data.deliveries.length} deliveries</p>
        ${data.deliveries.map(d=>`<article class="history-event"><strong>${esc(d.route_id)} · ${esc(d.event)} · ${esc(labels[d.status]??d.status)}</strong><p>${esc(formatDateTime(d.created_at == null ? null : d.created_at*1000))}${d.attempts>1?` · ${d.attempts} attempts`:""}</p>${d.error?`<p>${esc(d.error.replaceAll("_"," "))}</p>`:""}<details class="raw-evidence"><summary>Delivery details</summary><pre>${esc(evidenceText(d))}</pre></details></article>`).join("")}`;
    } catch {if(current===version)$("alerts-content").textContent="Alert delivery status unavailable. Refresh local data to retry.";}
  });
})();
