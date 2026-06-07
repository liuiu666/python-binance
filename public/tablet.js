(function(){
  const elStatus=document.getElementById("status");
  const elNext=document.getElementById("nextAction");
  const elBootstrap=document.getElementById("bootstrapUrl");
  const elLoader=document.getElementById("loaderUrl");
  const elScript=document.getElementById("scriptUrl");
  const elVersion=document.getElementById("scriptVersion");
  const elAudit=document.getElementById("auditUrl");
  const elChecks=document.getElementById("checks");
  const elEvents=document.getElementById("events");
  const btn=document.getElementById("refreshBtn");

  function age(ms){
    if(ms==null)return "--";
    const s=Math.max(0,Math.floor(ms/1000));
    if(s<60)return s+"s";
    const m=Math.floor(s/60);
    if(m<60)return m+"m";
    return Math.floor(m/60)+"h";
  }

  function statusLabel(d){
    if(d.status==="has_order_done")return ["orders ok","ok"];
    if(d.status==="autojs_online_waiting_for_order_done")return ["heartbeat "+age(d.latestHeartbeatAgeMs),"ok"];
    if(d.status==="autojs_seen_waiting_for_order_done")return ["stale "+age(d.latestEventAgeMs),"warn"];
    return ["no heartbeat","fail"];
  }

  function renderChecks(checks){
    const labels={
      serverReachable:"Server reachable",
      latestScriptServed:"Latest script served",
      tabletPageSeen:"Tablet page seen",
      loaderStarted:"Loader started",
      loaderError:"Loader error",
      autojsStarted:"AutoJS started",
      heartbeatOnline:"Heartbeat online",
      balanceRecent:"Balance recent",
      orderDoneSeen:"Order done seen"
    };
    elChecks.innerHTML=Object.keys(labels).map(k=>{
      const ok=!!checks[k];
      return '<div class="row"><span class="label">'+labels[k]+'</span><span class="value '+(ok?"ok":"warn")+'">'+(ok?"yes":"no")+'</span></div>';
    }).join("");
  }

  function refresh(){
    fetch("/api/tablet-page-ping",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({page:"tablet",clientTime:Date.now(),href:location.href})
    }).catch(()=>{}).then(()=>fetch("/api/tablet-diagnostics")).then(r=>r.json()).then(d=>{
      const runtime=d.runtime||{};
      const st=statusLabel(d);
      elStatus.textContent=st[0];
      elStatus.className="status "+st[1];
      elNext.textContent=d.nextAction||"--";
      if(elBootstrap){
        elBootstrap.href=runtime.bootstrapUrl||"/auto_btc_bootstrap.js";
        elBootstrap.textContent=runtime.bootstrapUrl||"/auto_btc_bootstrap.js";
      }
      elLoader.href=runtime.loaderUrl||"/auto_btc_loader.js";
      elLoader.textContent=runtime.loaderUrl||"/auto_btc_loader.js";
      elScript.href=runtime.scriptUrl||"/auto_btc.js";
      elScript.textContent=runtime.scriptUrl||"/auto_btc.js";
      elVersion.textContent=runtime.scriptVersion||"--";
      elAudit.textContent=runtime.auditUrl||"--";
      renderChecks(d.checks||{});
      elEvents.textContent=JSON.stringify({
        latestTabletPagePingAge:age(d.latestTabletPagePingAgeMs),
        latestHeartbeatAge:age(d.latestHeartbeatAgeMs),
        recentAutojsEvents:d.recentAutojsEvents||[]
      },null,2);
    }).catch(e=>{
      elStatus.textContent="server error";
      elStatus.className="status fail";
      elNext.textContent=String(e);
    });
  }

  btn.addEventListener("click",refresh);
  setInterval(refresh,5000);
  refresh();
})();
