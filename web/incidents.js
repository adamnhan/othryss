"use strict";
(() => {
  const esc=escapeHtml;
  let scope="", selection=null, generation=0, data=null, busy=false;
  const label=r=>r.replaceAll("_", " ");
  async function api(path, params) {
    const response=await fetch(`/api/history/${path}?${new URLSearchParams(params)}`);
    const result=await response.json();
    if (!response.ok) throw new Error(result.error ?? "Incident evidence unavailable");
    return result;
  }
  function list() {
    const filter=$("incident-filter").value;
    const items=data.incidents.filter(i=>filter==="all" || (filter==="active" ? i.status!=="resolved" : i.status==="resolved"));
    $("incident-list").innerHTML=`<p>${data.counts.open ?? 0} open · ${data.counts.acknowledged ?? 0} acknowledged · ${data.counts.pending ?? 0} pending · ${data.counts.resolved ?? 0} resolved</p>`+
      (items.length ? items.map(i=>`<button class="history-order" data-incident="${esc(i.incident_id)}"><span><strong>${esc(label(i.rule))}</strong><small>${esc(i.instrument_id)}</small></span><span>${esc(i.status)}<small>Assessment: ${esc(i.assessment)}</small></span><span>${esc(formatDateTime(i.last_seen))}</span></button>`).join("") : '<p class="empty">No matching incidents</p>');
    $("incident-list").querySelectorAll("[data-incident]").forEach(b=>b.onclick=()=>detail("incident", b.dataset.incident, true));
  }
  async function detail(kind, id, focus=false) {
    selection={kind,id}; const current=scope;
    try {
      const result=await api(kind, {scope,[kind==="incident" ? "incident" : "check"]:id});
      if(scope!==current || selection?.id!==id) return;
      const i=result.incident;
      $("incident-detail").innerHTML=`<h3>${i ? `${esc(label(i.rule))} · ${esc(i.status)}` : "Bot-state comparison"}</h3>
        ${i ? `<p>Assessment: <strong>${esc(i.assessment)}</strong>. First seen ${esc(formatDateTime(i.first_seen))}. Last checked ${esc(formatDateTime(i.last_seen))}.</p>${i.assessment!=="clear" && ["open","acknowledged"].includes(i.status)?"<p>Resolution awaits clear checks.</p>":""}` : `<p>${esc(result.result.reason)}</p>`}
        ${i ? `<p>${esc(result.latest_evidence?.result?.reason ?? "Unavailable")}</p>` : ""}
        <details class="raw-evidence"><summary>Evidence</summary><pre>${esc(evidenceText(result))}</pre></details>
        ${i && ["open","acknowledged"].includes(i.status) ? `<label>Note<textarea id="incident-note" maxlength="1000" rows="2"></textarea></label><div class="history-toolbar"><button class="button" id="incident-ack" ${i.status==="acknowledged" ? "disabled" : ""}>Acknowledge</button><button class="button" id="incident-resolve" ${i.assessment!=="clear" ? "disabled" : ""}>Resolve</button></div>` : ""}
        <button class="button" id="incident-export">Export</button><p id="incident-action-status" role="status"></p>`;
      $("incident-export").onclick=()=>{
        const a=document.createElement("a"); a.href=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)+"\n"],{type:"application/json"}));
        a.download=i ? "othryss-incident.json" : "othryss-bot-check.json"; a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),1000);
      };
      async function action(action) {
        if(busy) return; busy=true;
        const note=$("incident-note").value;
        try {
          const response=await fetch("/api/history/incident-action",{method:"POST",headers:{"Content-Type":"application/json","X-Othryss-Review":"1"},body:JSON.stringify({scope:current,incident:id,action,note})});
          const value=await response.json(); if(!response.ok) throw new Error(value.error);
          if(scope===current) { await refresh(false); await detail(kind,id); }
        } catch(error) { if(scope===current && selection?.id===id) $("incident-action-status").textContent=error.message; }
        finally { busy=false; }
      }
      if($("incident-ack")) $("incident-ack").onclick=()=>action("acknowledge");
      if($("incident-resolve")) $("incident-resolve").onclick=()=>action("resolve");
      if(focus){$("incident-detail").setAttribute("tabindex","-1");$("incident-detail").scrollIntoView({block:"start"});$("incident-detail").focus({preventScroll:true});}
    } catch(error) { if(scope===current && selection?.id===id) $("incident-detail").textContent=error.message; }
  }
  async function refresh() {
    const version=++generation;
    try {
      const value=await api("incidents",{scope}); if(version!==generation) return;
      data=value;
      document.dispatchEvent(new CustomEvent("workspace-incidents",{detail:{scope,data:value}}));
      $("bot-monitors").innerHTML=value.monitors.length ? value.monitors.map(m=>`<p><strong>${esc(m.instrument_id)}</strong> · subaccount ${m.subaccount} · source ${esc(m.source_status)} · ${m.grace_seconds}s grace<br>${esc(m.source_status === "stopped" ? m.source_reason : m.last_error ?? m.source_reason)}${m.latest_check ? ` · ${esc(m.latest_check.status)}${m.check_stale ? " (stale check)" : ""} <button class="text-button" data-bot-check="${esc(m.latest_check.check_id)}">View check</button><br>${esc(m.latest_check.position_coverage ?? m.latest_check.reason)}` : " · Waiting for a comparable exchange capture"}</p>`).join("") : "No bot-state monitors";
      $("bot-monitors").querySelectorAll("[data-bot-check]").forEach(b=>b.onclick=()=>detail("bot-check",b.dataset.botCheck));
      list();
      // Preserve an in-progress review note during automatic refreshes.
    } catch { if(version===generation) { $("bot-monitors").textContent="Bot reconciliation unavailable. Refresh local data to retry."; document.dispatchEvent(new CustomEvent("workspace-incidents",{detail:{scope,data:null}})); } }
  }
  $("incident-filter").onchange=()=>{if(data) list();};
  document.addEventListener("workspace-open-incident",event=>detail("incident",event.detail,true));
  document.addEventListener("history-scope", event=>{
    if(scope!==event.detail) {selection=null; $("incident-detail").textContent="";}
    scope=event.detail; refresh();
  });
})();
