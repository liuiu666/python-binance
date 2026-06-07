(function(){
  const $=id=>document.getElementById(id);
  const elPrice=$("currentPrice"),elPriceChange=$("priceChange"),elPriceTop=$("currentPriceTop");
  const elRsiTop=$("rsiTop"),elConfTop=$("confTop"),elAmountTop=$("amountTop"),elRealBalance=$("realBalanceTop");
  const elBannerSignals=$("bannerSignals"),elSigTime=$("sigTime"),elStrategy10=$("strategy10"),elStrategy30=$("strategy30");
  const elReportHealth=$("reportHealth"),elReportEdge10=$("reportEdge10"),elReportEdge30=$("reportEdge30"),elReportFilter=$("reportFilter"),elReportTablet=$("reportTablet"),elReportShadow=$("reportShadow"),elReportLive=$("reportLive");
  const elBtnUp=$("btnUp"),elBtnDown=$("btnDown"),elUpPayout=$("upPayout"),elDownPayout=$("downPayout");
  const canvas=$("priceChart"),ctx=canvas.getContext("2d");
  const gaugeCanvas=$("gaugeCanvas"),gCtx=gaugeCanvas.getContext("2d");

  let currentPrice=null,firstPrice=null,priceHistory=[],signalData=null,signalAmount="5",ws=null,lastWsPrice=0;
  let configDirty=false,configLoadInFlight=false;
  const PAYOUT=0.85;

  function fmt(n,d){return n==null||Number.isNaN(Number(n))?"--":Number(n).toLocaleString("en-US",{minimumFractionDigits:d??2,maximumFractionDigits:d??2})}
  function fmtPrice(n){return n==null?"--":Number(n).toFixed(2)}
  function showToast(m,t){const e=document.createElement("div");e.className="toast "+(t||"info");e.textContent=m;document.body.appendChild(e);setTimeout(()=>{e.style.opacity="0";setTimeout(()=>e.remove(),300)},2500)}
  function setReportValue(el,text,state){if(!el)return;el.textContent=text||"--";el.className="report-value "+(state||"")}
  function markConfigDirty(){configDirty=true;const s=$("configStatus");if(s)s.textContent="未保存"}

  function resizeCanvas(){
    const r=canvas.parentElement.getBoundingClientRect();
    const d=window.devicePixelRatio||1;
    canvas.width=Math.max(1,r.width*d);canvas.height=Math.max(1,r.height*d);
    canvas.style.width=r.width+"px";canvas.style.height=r.height+"px";
    ctx.setTransform(d,0,0,d,0,0);
  }

  function drawChart(){
    const r=canvas.parentElement.getBoundingClientRect(),w=r.width,h=r.height;
    ctx.clearRect(0,0,w,h);
    if(priceHistory.length<2){ctx.fillStyle="#5a6478";ctx.font="14px sans-serif";ctx.fillText("等待价格数据...",20,30);return}
    const data=priceHistory.slice(-240),vals=data.map(x=>Number(x.price)).filter(Number.isFinite);
    const min=Math.min(...vals),max=Math.max(...vals),pad=(max-min)*0.1||1;
    ctx.strokeStyle="#26334f";ctx.lineWidth=1;
    for(let i=0;i<4;i++){const y=20+i*(h-40)/3;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke()}
    ctx.strokeStyle="#00d4aa";ctx.lineWidth=2;ctx.beginPath();
    data.forEach((p,i)=>{const x=i/(data.length-1)*w;const y=h-20-((p.price-(min-pad))/((max+pad)-(min-pad)))*(h-40);if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y)});
    ctx.stroke();
  }

  function drawGauge(confidence,signal,rsiVal){
    const w=gaugeCanvas.width,h=gaugeCanvas.height,cx=w/2,cy=h-10,r=Math.max(1,h-30),start=Math.PI,end=2*Math.PI;
    gCtx.clearRect(0,0,w,h);
    gCtx.beginPath();gCtx.arc(cx,cy,r,start,end);gCtx.strokeStyle="#1e2a40";gCtx.lineWidth=14;gCtx.lineCap="round";gCtx.stroke();
    if(rsiVal!=null){
      const a=start+(Math.max(0,Math.min(100,rsiVal))/100)*Math.PI;
      gCtx.beginPath();gCtx.arc(cx,cy,r,start,a);gCtx.strokeStyle=rsiVal<30?"#00d4aa":rsiVal>70?"#ff4757":"#ffd32a";gCtx.lineWidth=14;gCtx.lineCap="round";gCtx.stroke();
    }
    gCtx.textAlign="center";
    if(confidence!=null&&signal){
      gCtx.font="bold 24px sans-serif";gCtx.fillStyle=signal==="UP"?"#00d4aa":"#ff4757";gCtx.fillText(Number(confidence).toFixed(0)+"%",cx,cy-30);
      gCtx.font="bold 13px sans-serif";gCtx.fillText(signal==="UP"?"看涨":"看跌",cx,cy-12);
    }else if(rsiVal!=null){
      gCtx.font="bold 20px sans-serif";gCtx.fillStyle="#ffd32a";gCtx.fillText("RSI "+Number(rsiVal).toFixed(0),cx,cy-20);
    }
  }

  function strategyLabel(s){
    if(!s)return"等待数据";
    if(s.signal)return(s.signal==="UP"?"看涨":"看跌")+" "+s.confidence+"%";
    const reasons=[];
    if(!s.agree)reasons.push("模型分歧");
    if(!s.high_conf)reasons.push("强度不足");
    if(!s.rsi_extreme)reasons.push("RSI "+(s.rsi_value!=null?Number(s.rsi_value).toFixed(0):"--"));
    if(s.vol_ok===false)reasons.push("波动不足");
    if(s.session_ok===false)reasons.push("时段过滤");
    return reasons.length?reasons.join(" | "):"等待";
  }

  function amountForConfidence(conf,config){
    const base=(config&&config.amount)||$("cfgAmount").value||"5";
    const tiersEnabled=config?!!config.tiersEnabled:$("cfgTiersEnabled").classList.contains("on");
    const tiers=config&&Array.isArray(config.tiers)?config.tiers:collectTiers();
    if(tiersEnabled&&conf!=null&&Array.isArray(tiers)&&tiers.length){
      const sorted=[...tiers].sort((a,b)=>Number(b.min)-Number(a.min));
      for(const t of sorted){
        if(Number(conf)>=Number(t.min))return String(t.amount);
      }
    }
    return String(base);
  }

  function amountForSignal(strategyId,s,data){
    const config=data&&data._config?data._config:null;
    if(s&&s.confidence!=null)return amountForConfidence(s.confidence,config);
    const amts=data&&data._strategyAmounts?data._strategyAmounts:{};
    return String((config&&config.amount)||amts[strategyId]||$("cfgAmount").value||"5");
  }

  function updateStrategyCard(card,s,amountFallback){
    if(!card)return;
    const state=card.querySelector(".strategy-state"),meta=card.querySelector(".strategy-meta");
    if(state){state.textContent=strategyLabel(s);state.className="strategy-state "+(s&&s.signal?(s.signal==="UP"?"up":"down"):"wait")}
    if(meta){
      const amount=amountFallback||(s&&s.amount)||$("cfgAmount").value||"5";
      const duration=s&&(s.duration||s.interval_min)?(s.duration||s.interval_min):"--";
      const rsi=s&&s.rsi_value!=null?" | RSI "+Number(s.rsi_value).toFixed(0):"";
      meta.textContent=amount+"U x "+duration+"分钟"+rsi;
    }
  }

  function updateSignals(data){
    if(!data)return;
    const amts=data._strategyAmounts||{};
    updateStrategyCard(elStrategy10,data.BTC_10min,amountForSignal("BTC_10min",data.BTC_10min,data));
    updateStrategyCard(elStrategy30,data.BTC_30min,amountForSignal("BTC_30min",data.BTC_30min,data));
    const s30=data.BTC_30min,s10=data.BTC_10min;
    const active=(s30&&s30.signal?s30:null)||(s10&&s10.signal?s10:null)||s30||s10;
    if(!active)return;
    signalData=active;
    signalAmount=amountForSignal(active.strategy_id,active,data);
    const parts=[];
    if(s30&&s30.signal)parts.push('<span class="signal-direction '+(s30.signal==="UP"?"up":"down")+'">30分 '+(s30.signal==="UP"?"看涨":"看跌")+'</span><span class="signal-confidence">'+s30.confidence+'%</span>');
    if(s10&&s10.signal)parts.push('<span class="signal-direction '+(s10.signal==="UP"?"up":"down")+'">10分 '+(s10.signal==="UP"?"看涨":"看跌")+'</span><span class="signal-confidence">'+s10.confidence+'%</span>');
    elBannerSignals.innerHTML=parts.length?parts.join(""):'<span class="signal-direction neutral">监控中 | 10分/30分并行</span>';
    elSigTime.textContent=active.time||"";
    drawGauge(active.confidence,active.signal,active.rsi_value);
    const probs=active.probs||[0.5,0.5,0.5];
    ["bar1","bar2","bar3"].forEach((id,i)=>{const p=probs[i]??0.5,bar=$(id),val=$("val"+(i+1));if(bar){bar.style.width=(p*100)+"%";bar.style.background=p>0.6?"#00d4aa":p<0.4?"#ff4757":"#ffd32a"}if(val){val.textContent=(p*100).toFixed(1)+"%";val.style.color=bar?bar.style.background:"#ffd32a"}});
    $("verdictText").textContent=active.signal?(active.signal==="UP"?"看涨 ":"看跌 ")+active.confidence+"%":strategyLabel(active);
    $("verdictText").style.color=active.signal?(active.signal==="UP"?"#00d4aa":"#ff4757"):"#5a6478";
    $("thresholdInfo").textContent=active.signal?"RSI="+Number(active.rsi_value).toFixed(0)+" | 强度 "+active.confidence+"%":"强度=离50%多远，还需 RSI<30 或 >70";
    updateTopbar();
  }

  function updateTopbar(){
    if(elPriceTop)elPriceTop.textContent=fmtPrice(currentPrice);
    if(signalData){
      if(elRsiTop)elRsiTop.textContent=signalData.rsi_value!=null?Number(signalData.rsi_value).toFixed(0):"--";
      if(elConfTop){
        if(signalData.confidence!=null){elConfTop.textContent=Number(signalData.confidence).toFixed(0)+"%";elConfTop.style.color=Number(signalData.confidence)>=60?"var(--green)":"var(--text-secondary)"}
        else{elConfTop.textContent="等待强度";elConfTop.style.color="var(--text-muted)"}
      }
    }
    if(elAmountTop)elAmountTop.textContent=signalAmount||$("cfgAmount").value||"5";
  }

  function updatePriceDisplay(){
    if(!currentPrice)return;
    elPrice.textContent=fmtPrice(currentPrice);
    if(firstPrice){
      const ch=currentPrice-firstPrice,pct=ch/firstPrice*100;
      elPriceChange.textContent=(ch>=0?"+":"")+fmtPrice(ch)+" ("+(pct>=0?"+":"")+pct.toFixed(2)+"%)";
      elPriceChange.style.color=ch>=0?"var(--green)":"var(--red)";
    }
    updateTopbar();
  }

  function placeTrade(direction){
    const amount=parseFloat($("cfgAmount").value)||5,dur=$("cfgDuration").value||"30";
    fetch("/api/manual",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({direction,amount:String(amount),duration:dur})})
      .then(r=>r.json()).then(()=>showToast((direction==="UP"?"看涨":"看跌")+" "+amount+"U x "+dur+"分钟 -> 平板","success"))
      .catch(()=>showToast("发送失败","error"));
  }
  elBtnUp.addEventListener("click",()=>placeTrade("UP"));
  elBtnDown.addEventListener("click",()=>placeTrade("DOWN"));

  function fetchSignals(){fetch("/api/signal").then(r=>r.json()).then(updateSignals).catch(()=>{})}
  function fetchPriceFallback(){fetch("/api/price").then(r=>r.json()).then(d=>{if(d.price&&(!currentPrice||Date.now()-lastWsPrice>5000)){currentPrice=d.price;if(!firstPrice)firstPrice=currentPrice;priceHistory.push({time:Date.now(),price:currentPrice});priceHistory=priceHistory.slice(-600);updatePriceDisplay();drawChart()}}).catch(()=>{})}

  function updateReports(data){
    const d=data&&data.decision?data.decision:{},h=(data&&data.health)||d.system_health||{},overall=h.overall||"--";
    setReportValue(elReportHealth,overall,overall==="ok"?"ok":overall==="fail"?"fail":"warn");
    const v=d.production_summary||d.validated_walkforward||{};
    const e10=v.BTC_10min&&v.BTC_10min.edge_over_breakeven,e30=v.BTC_30min&&v.BTC_30min.edge_over_breakeven;
    setReportValue(elReportEdge10,e10!=null?("+"+Number(e10).toFixed(2)+"pp"):"--",e10>0?"ok":"warn");
    setReportValue(elReportEdge30,e30!=null?("+"+Number(e30).toFixed(2)+"pp"):"--",e30>0?"ok":"warn");
    const p=d.parallel_portfolio||{};
    setReportValue(elReportFilter,p.win_rate!=null?Number(p.win_rate).toFixed(1)+"% / "+Number((p.frequency||{}).trades_per_day||0).toFixed(1)+"/d":"--",p.win_rate?"ok":"warn");
    setReportValue(elReportShadow,(d.shadow_candidate_decision||{}).status?"watching":"--","warn");
  }

  function fetchReports(){fetch("/api/reports").then(r=>r.json()).then(updateReports).catch(()=>{})}
  function age(ms){if(ms==null)return"--";const s=Math.max(0,Math.floor(ms/1000));if(s<60)return s+"s";const m=Math.floor(s/60);return m<60?m+"m":Math.floor(m/60)+"h"}
  function fetchRuntime(){fetch("/api/runtime").then(r=>r.json()).then(d=>{setReportValue(elReportTablet,(d.tabletUrl||"").replace("http://",""),"ok");elReportTablet.title="page="+(d.tabletPageUrl||"--")+" | bootstrap="+(d.bootstrapUrl||"--")+" | loader="+(d.loaderUrl||"--")+" | script="+(d.scriptUrl||"--")+" | "+(d.scriptVersion||"no version")}).catch(()=>setReportValue(elReportTablet,"--","warn"))}
  function fetchTabletDiagnostics(){fetch("/api/tablet-diagnostics").then(r=>r.json()).then(d=>{let label="no heartbeat",state="warn";if(d.status==="has_order_done"){label="orders ok";state="ok"}else if(d.status==="autojs_online_waiting_for_order_done"){label="heartbeat "+age(d.latestHeartbeatAgeMs);state="ok"}else if(d.checks&&d.checks.loaderError){label="loader error"}else if(d.checks&&d.checks.loaderStarted){label="loader seen"}else if(d.checks&&d.checks.tabletPageSeen){label="page seen"}setReportValue(elReportLive,label,state);elReportLive.title=(d.nextAction||"")+" | bootstrap="+((d.runtime||{}).bootstrapUrl||"--")}).catch(()=>{})}

  function renderTiers(tiers){
    const list=$("tiersList");
    list.innerHTML="";
    (tiers||[]).forEach(t=>{
      const row=document.createElement("div");
      row.className="tier-row";
      row.innerHTML='<input class="tier-min" type="number" min="0" max="100" step="1" value="'+Number(t.min||0)+'"><span>%</span><input class="tier-amount" type="number" min="1" step="1" value="'+Number(t.amount||5)+'"><button class="tier-del" type="button">x</button>';
      list.appendChild(row);
    });
    updateStakePreview();
  }

  function collectTiers(){
    return Array.from(document.querySelectorAll(".tier-row"))
      .map(r=>({min:Number(r.querySelector(".tier-min").value),amount:Number(r.querySelector(".tier-amount").value)}))
      .filter(t=>Number.isFinite(t.min)&&Number.isFinite(t.amount)&&t.min>=0&&t.min<=100&&t.amount>0)
      .sort((a,b)=>b.min-a.min);
  }

  function strengthToProbText(strength){
    const n=Math.max(0,Math.min(100,Number(strength)||0));
    const up=(50+n/2).toFixed(0);
    const down=(50-n/2).toFixed(0);
    return "上涨≥"+up+"% 或 ≤"+down+"%";
  }

  function tierRuleText(tiers,baseAmount){
    const rules=(tiers||[]).map(t=>"强度≥"+Number(t.min).toFixed(0)+"% "+Number(t.amount).toFixed(0)+"U");
    rules.push("其它 "+baseAmount+"U");
    return rules.join(" / ");
  }

  function tierExplainText(tiers){
    return (tiers||[]).map(t=>"强度≥"+Number(t.min).toFixed(0)+"% ≈ "+strengthToProbText(t.min)).join("；");
  }

  function updateStakePreview(){
    const baseAmount=$("cfgAmount").value||"5";
    const tiersEnabled=$("cfgTiersEnabled").classList.contains("on");
    const tiers=collectTiers();
    const text=tiersEnabled?tierRuleText(tiers,baseAmount):"固定 "+baseAmount+"U";
    const stake10=$("stake10Preview"),stake30=$("stake30Preview"),tierPreview=$("tierPreview");
    if(stake10)stake10.textContent=tiersEnabled?text:"固定 "+baseAmount+"U";
    if(stake30)stake30.textContent=tiersEnabled?text:"固定 "+baseAmount+"U";
    if(tierPreview)tierPreview.textContent=tiersEnabled?"金额规则: "+text+" | 换算: "+tierExplainText(tiers):"分级关闭: 两个策略使用默认 "+baseAmount+"U";
    updateTopbar();
  }

  window.addTierRow=function(){
    markConfigDirty();
    renderTiers([...collectTiers(),{min:50,amount:Number($("cfgAmount").value)||5}]);
  };
  window.toggleTiers=function(){
    markConfigDirty();
    const b=$("cfgTiersEnabled"),p=$("tiersPanel"),on=!b.classList.contains("on");
    b.textContent=on?"开启":"关闭";
    b.className="toggle-btn "+(on?"on":"off");
    p.style.display=on?"block":"none";
    updateStakePreview();
  };
  window.toggleAuto=function(){markConfigDirty();const b=$("cfgAutoTrade"),on=!b.classList.contains("on");b.textContent=on?"开启":"关闭";b.className="toggle-btn "+(on?"on":"off")};
  window.toggleConflictFilter=function(){markConfigDirty();const b=$("cfgSkipConflictSignals"),on=!b.classList.contains("on");b.textContent=on?"开启":"关闭";b.className="toggle-btn "+(on?"on":"off")};
  window.togglePreventOverlap=function(){markConfigDirty();const b=$("cfgPreventOverlapOrders"),on=!b.classList.contains("on");b.textContent=on?"开启":"关闭";b.className="toggle-btn "+(on?"on":"off")};
  window.loadConfig=function(force){
    if(configDirty&&!force)return;
    if(configLoadInFlight)return;
    configLoadInFlight=true;
    fetch("/api/config").then(r=>r.json()).then(c=>{
      $("cfgAmount").value=c.amount||"5";
      $("cfgDuration").value=c.duration||"30";
      $("cfgConfidence").value=c.minConfidence||10;
      const auto=$("cfgAutoTrade");
      auto.textContent=c.autoTrade?"开启":"关闭";
      auto.className="toggle-btn "+(c.autoTrade?"on":"off");
      const tiers=$("cfgTiersEnabled"),panel=$("tiersPanel");
      tiers.textContent=c.tiersEnabled?"开启":"关闭";
      tiers.className="toggle-btn "+(c.tiersEnabled?"on":"off");
      panel.style.display=c.tiersEnabled?"block":"none";
      const conflict=$("cfgSkipConflictSignals");
      if(conflict){
        conflict.textContent=c.skipConflictSignals?"开启":"关闭";
        conflict.className="toggle-btn "+(c.skipConflictSignals?"on":"off");
      }
      const overlap=$("cfgPreventOverlapOrders");
      if(overlap){
        const on=c.preventOverlapOrders!==false;
        overlap.textContent=on?"开启":"关闭";
        overlap.className="toggle-btn "+(on?"on":"off");
      }
      const queuePolicy=$("cfgQueueOrderPolicy");
      if(queuePolicy)queuePolicy.value=c.queueOrderPolicy||"confidence_desc";
      renderTiers(c.tiers&&c.tiers.length?c.tiers:[{min:80,amount:25},{min:60,amount:12},{min:40,amount:6}]);
      updateStakePreview();
      configDirty=false;
      configLoadInFlight=false;
    }).catch(()=>{configLoadInFlight=false});
  };
  window.saveConfig=function(){
    const c={
      amount:$("cfgAmount").value,
      duration:$("cfgDuration").value,
      minConfidence:Number($("cfgConfidence").value),
      autoTrade:$("cfgAutoTrade").classList.contains("on"),
      tiersEnabled:$("cfgTiersEnabled").classList.contains("on"),
      skipConflictSignals:$("cfgSkipConflictSignals")&&$("cfgSkipConflictSignals").classList.contains("on"),
      preventOverlapOrders:!$("cfgPreventOverlapOrders")||$("cfgPreventOverlapOrders").classList.contains("on"),
      queueOrderPolicy:$("cfgQueueOrderPolicy")?$("cfgQueueOrderPolicy").value:"confidence_desc",
      tiers:collectTiers()
    };
    fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(c)})
      .then(r=>r.json())
      .then(d=>{
        showToast("配置已保存: 10分钟/30分钟 "+(d.tiersEnabled?"按档位":"默认 "+d.amount+"U"),"success");
        configDirty=false;
        $("configStatus").textContent="已同步";
        renderTiers(d.tiers||[]);
        updateStakePreview();
      })
      .catch(()=>showToast("保存失败","error"));
  };

  function connect(){
    ws=new WebSocket((location.protocol==="https:"?"wss:":"ws:")+"//"+location.host+"/ws");
    ws.onmessage=e=>{
      const msg=JSON.parse(e.data);
      if(msg.type==="init"){if(msg.price){currentPrice=msg.price;firstPrice=currentPrice;lastWsPrice=Date.now()}if(msg.history)priceHistory=msg.history;updatePriceDisplay();resizeCanvas();drawChart();if(msg.realBalance&&msg.realBalance.amount!=null){elRealBalance.textContent=fmt(msg.realBalance.amount,2);elRealBalance.style.color="var(--green)"}}
      if(msg.type==="price"){currentPrice=msg.price;if(msg.history)priceHistory=msg.history;if(!firstPrice&&currentPrice)firstPrice=currentPrice;lastWsPrice=Date.now();updatePriceDisplay();drawChart()}
      if(msg.type==="state"&&msg.realBalance&&msg.realBalance.amount!=null){elRealBalance.textContent=fmt(msg.realBalance.amount,2);elRealBalance.style.color="var(--green)"}
      if(msg.type==="balance"&&msg.amount!=null){elRealBalance.textContent=fmt(msg.amount,2);elRealBalance.style.color="var(--green)"}
      if(msg.type==="trade_update"&&msg.trade){showToast("订单 #"+msg.trade.id+" "+msg.trade.status,"info")}
      if(msg.type==="error")showToast(msg.message,"error");
    };
    ws.onclose=()=>setTimeout(connect,2000);ws.onerror=()=>ws.close();
  }

  function updatePayouts(){const a=parseFloat($("cfgAmount").value)||5;elUpPayout.textContent="+"+fmt(a*PAYOUT,2)+" USDT";elDownPayout.textContent="+"+fmt(a*PAYOUT,2)+" USDT";updateTopbar()}
  $("cfgAmount").addEventListener("input",()=>{markConfigDirty();updatePayouts();updateStakePreview()});
  $("cfgDuration").addEventListener("change",markConfigDirty);
  $("cfgConfidence").addEventListener("input",markConfigDirty);
  $("cfgQueueOrderPolicy").addEventListener("change",markConfigDirty);
  $("tiersList").addEventListener("input",()=>{markConfigDirty();updateStakePreview()});
  $("tiersList").addEventListener("click",e=>{
    if(e.target&&e.target.classList.contains("tier-del")){
      markConfigDirty();
      e.target.closest(".tier-row").remove();
      updateStakePreview();
    }
  });
  window.addEventListener("resize",()=>{resizeCanvas();drawChart()});
  resizeCanvas();drawGauge(null,null,null);connect();loadConfig();updatePayouts();
  fetchSignals();fetchReports();fetchRuntime();fetchTabletDiagnostics();fetchPriceFallback();
  setInterval(fetchSignals,3000);setInterval(fetchPriceFallback,3000);setInterval(updateTopbar,2000);
  setInterval(fetchReports,15000);setInterval(fetchRuntime,30000);setInterval(fetchTabletDiagnostics,15000);setInterval(loadConfig,10000);
})();
