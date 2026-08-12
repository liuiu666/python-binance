const fs = require("fs");
const os = require("os");
const express = require("express");

/**
 * 创建平板运行时服务。
 * 静态配置通过 deps 固定注入，余额、进程状态和数据更新状态通过 getter 实时读取。
 */
function createTabletRuntime(deps) {
  const {
    port,
    dataDir,
    autoScriptFile,
    serverSimTradingEnabled,
    managedProcessesEnabled,
    pythonExe,
    signalScriptFile,
    serverId,
    publicBaseUrl,
    tailAudit,
    appendAudit,
    apiAuthPublicInfo,
    getRealBalance,
    getSignalService,
    getDataUpdate
  } = deps;

  /** 枚举本机可供平板访问的 IPv4 HTTP 地址。 */
  function localHttpUrls() {
    const nets = os.networkInterfaces();
    const urls = [];
    for (const [name, items] of Object.entries(nets)) {
      for (const ni of items || []) {
        if (ni.family !== "IPv4" || ni.internal) continue;
        urls.push({
          interface: name,
          address: ni.address,
          url: `http://${ni.address}:${port}`
        });
      }
    }
    return urls;
  }

  /** 从 AutoJS 主脚本中读取对外展示的脚本版本。 */
  function autoScriptVersion() {
    try {
      const text = fs.readFileSync(autoScriptFile, "utf8");
      const match = text.match(/SCRIPT_VERSION\s*=\s*["']([^"']+)["']/);
      return match ? match[1] : null;
    } catch (e) {
      return null;
    }
  }

  /** 生成供网页和平板脚本使用的运行时地址及公开配置。 */
  function runtimeInfo() {
    const urls = localHttpUrls();
    const preferred = urls.find(item => item.address.startsWith("192.168.")) || urls[0] || null;
    const publicBase = String(publicBaseUrl || "").replace(/\/+$/, "");
    const base = publicBase || (preferred ? preferred.url : `http://127.0.0.1:${port}`);
    return {
      serverId,
      dataDir,
      port: Number(port),
      urls,
      tabletUrl: base,
      tabletPageUrl: `${base}/tablet.html`,
      auditUrl: `${base}/api/trade-audit`,
      signalUrl: `${base}/api/signal`,
      scriptUrl: `${base}/auto_btc.js`,
      loaderUrl: `${base}/auto_btc_loader.js`,
      bootstrapUrl: `${base}/auto_btc_bootstrap.js`,
      scriptVersion: autoScriptVersion(),
      serverSimTradingEnabled,
      managedProcessesEnabled,
      apiAuth: apiAuthPublicInfo
    };
  }

  /** 汇总最近 AutoJS 审计事件，保持既有诊断状态、检查项和建议规则不变。 */
  function tabletDiagnostics() {
    const runtime = runtimeInfo();
    const rows = tailAudit(200);
    const autojsEventNames = new Set([
      "autojs_loader_start",
      "autojs_loader_exec",
      "autojs_loader_error",
      "autojs_start",
      "autojs_keepalive_status",
      "autojs_heartbeat",
      "runtime_screen_wake",
      "runtime_relaunch_app",
      "runtime_keepalive_failed",
      "signal_tradeable",
      "signal_skipped",
      "order_attempt",
      "order_abort",
      "order_unverified",
      "order_done",
      "runtime_loop_error"
    ]);
    const autojsRows = rows.filter(row => autojsEventNames.has(row.event));
    const latestTabletPagePing = [...rows].reverse().find(row => (
      row.event === "tablet_page_ping" && row.source !== "codex_local_probe"
    )) || null;
    const latestEvent = autojsRows.length ? autojsRows[autojsRows.length - 1] : null;
    const latestHeartbeat = [...autojsRows].reverse().find(row => row.event === "autojs_heartbeat") || null;
    const latestKeepAliveStatus = [...autojsRows].reverse().find(row => (
      row.event === "autojs_keepalive_status" ||
      row.event === "runtime_screen_wake" ||
      row.event === "runtime_relaunch_app" ||
      row.event === "runtime_keepalive_failed" ||
      (row.event === "autojs_heartbeat" && row.keepAlive)
    )) || null;
    const latestKeepAliveFailure = [...autojsRows].reverse().find(row => row.event === "runtime_keepalive_failed") || null;
    const latestOrderDone = [...autojsRows].reverse().find(row => row.event === "order_done") || null;
    const keepAliveStatus = latestKeepAliveStatus?.keepAlive || latestHeartbeat?.keepAlive || null;
    const runningScriptVersion = latestHeartbeat?.version || latestEvent?.version || null;
    const scriptVersionMatches = !runtime.scriptVersion || !runningScriptVersion || runtime.scriptVersion === runningScriptVersion;
    const now = Date.now();
    const ageOf = row => row && row.serverTime ? now - Number(row.serverTime) : null;
    const heartbeatAgeMs = ageOf(latestHeartbeat);
    const keepAliveStatusAgeMs = ageOf(latestKeepAliveStatus);
    const keepAliveFailureAgeMs = ageOf(latestKeepAliveFailure);
    const eventAgeMs = ageOf(latestEvent);
    const tabletPagePingAgeMs = ageOf(latestTabletPagePing);
    const realBalance = getRealBalance();
    const balanceAgeMs = realBalance && realBalance.time ? now - Number(realBalance.time) : null;
    let status = "waiting_for_autojs_events";
    if (latestOrderDone) status = "has_order_done";
    else if (heartbeatAgeMs != null && heartbeatAgeMs <= 120000) status = "autojs_online_waiting_for_order_done";
    else if (latestEvent) status = "autojs_seen_waiting_for_order_done";

    const checks = {
      serverReachable: true,
      latestScriptServed: !!runtime.scriptVersion,
      servedScriptVersion: runtime.scriptVersion,
      runningScriptVersion,
      scriptVersionMatches,
      tabletPageSeen: tabletPagePingAgeMs != null && tabletPagePingAgeMs <= 120000,
      loaderStarted: autojsRows.some(row => row.event === "autojs_loader_start"),
      loaderError: autojsRows.some(row => row.event === "autojs_loader_error"),
      autojsStarted: autojsRows.some(row => row.event === "autojs_start"),
      heartbeatOnline: heartbeatAgeMs != null && heartbeatAgeMs <= 120000,
      keepAliveReported: !!keepAliveStatus,
      writeSettingsGranted: keepAliveStatus?.writeSettingsGranted === true,
      screenTimeoutNever: Number(keepAliveStatus?.screenOffTimeoutMs) >= 2147483000,
      batteryOptimizationIgnored: keepAliveStatus?.batteryOptimizationIgnored === true,
      screenOn: keepAliveStatus?.screenOn === true,
      keepAliveFailureRecent: keepAliveFailureAgeMs != null && keepAliveFailureAgeMs <= 600000,
      balanceRecent: balanceAgeMs != null && balanceAgeMs <= 120000,
      orderDoneSeen: !!latestOrderDone
    };
    const nextAction = checks.heartbeatOnline && !checks.scriptVersionMatches
      ? `服务器脚本已更新为 ${runtime.scriptVersion}，平板仍在运行 ${runningScriptVersion || "未知版本"}；请重启一次 AutoJS 脚本加载新版。`
      : checks.heartbeatOnline && checks.scriptVersionMatches
        ? `平板已运行新版 ${runningScriptVersion || runtime.scriptVersion}，等待下一次信号或手动5U测试单。`
      : !checks.tabletPageSeen && !checks.heartbeatOnline
        ? `Open ${runtime.tabletPageUrl} on the tablet to confirm tablet network access.`
      : checks.loaderError && !checks.autojsStarted
        ? "Loader ran but failed; check AutoJS log for autojs_loader_error and retry the loader URL."
      : !checks.loaderStarted
        ? `Tablet browser reaches server; run loader ${runtime.loaderUrl} or bootstrap ${runtime.bootstrapUrl} in AutoJS.`
      : !checks.autojsStarted
        ? `Loader ran; wait for autojs_start or run latest auto_btc.js from ${runtime.scriptUrl} directly.`
      : !checks.heartbeatOnline
        ? "AutoJS was seen but heartbeat is stale; restart the tablet script and confirm it can POST /api/trade-audit."
      : checks.keepAliveFailureRecent
        ? "AutoJS recently reported a keepalive failure; check tablet screen, lock state, and battery/background permissions."
      : keepAliveStatus && keepAliveStatus.writeSettingsGranted === false
        ? "AutoJS is online but lacks WRITE_SETTINGS; grant Modify system settings so screen timeout can be set to never."
      : keepAliveStatus && keepAliveStatus.batteryOptimizationIgnored === false
        ? "AutoJS is online but battery optimization may still stop it; allow unrestricted/background running on the tablet."
      : !checks.orderDoneSeen
        ? "Tablet is online; wait for the next tradeable signal or place a small manual test order."
        : "Live order audit is active; collect settled trades before raising stake.";

    return {
      status,
      runtime,
      checks,
      nextAction,
      latestEvent,
      latestEventAgeMs: eventAgeMs,
      latestTabletPagePing,
      latestTabletPagePingAgeMs: tabletPagePingAgeMs,
      latestHeartbeat,
      latestHeartbeatAgeMs: heartbeatAgeMs,
      latestKeepAliveStatus,
      latestKeepAliveStatusAgeMs: keepAliveStatusAgeMs,
      latestKeepAliveFailure,
      latestKeepAliveFailureAgeMs: keepAliveFailureAgeMs,
      keepAliveStatus,
      latestOrderDone,
      balance: realBalance,
      balanceAgeMs,
      recentAutojsEvents: autojsRows.slice(-20)
    };
  }

  /** 注册运行时、平板诊断及 AutoJS 脚本分发路由。 */
  function registerRoutes(app) {
    app.get("/api/runtime", (req, res) => {
      res.json(runtimeInfo());
    });

    app.get("/api/signal-service", (req, res) => {
      const signalService = getSignalService();
      res.json({
        serverId,
        ...signalService,
        running: !!signalService.pid,
        python: pythonExe,
        script: signalScriptFile,
        dataUpdate: getDataUpdate()
      });
    });

    app.get("/api/tablet-diagnostics", (req, res) => {
      res.json(tabletDiagnostics());
    });

    app.post("/api/tablet-page-ping", express.json({ limit: "20kb" }), (req, res) => {
      const item = {
        serverTime: Date.now(),
        event: "tablet_page_ping",
        ip: req.ip,
        userAgent: req.get("user-agent") || null,
        runtime: runtimeInfo(),
        ...(req.body && typeof req.body === "object" ? req.body : {})
      };
      appendAudit(item);
      res.json({ ok: true, item });
    });

    app.get("/auto_btc.js", (req, res) => {
      res.type("text/javascript").sendFile(autoScriptFile);
    });

    app.get("/auto_btc_loader.js", (req, res) => {
      const runtime = runtimeInfo();
      // 以下模板是返回给 AutoJS 执行的客户端脚本，内部函数不可改为服务端函数。
      const code = `"auto";

var LOADER_VERSION = "2026-06-07-loader1";
var SCRIPT_URL = "${runtime.scriptUrl}";
var AUDIT_URL = "${runtime.auditUrl}";

function logMsg(msg) {
    var t = new Date().toLocaleTimeString();
    console.log("[loader " + t + "] " + msg);
}

function postLoaderEvent(event, extra) {
    try {
        var payload = {
            event: event,
            loaderVersion: LOADER_VERSION,
            scriptUrl: SCRIPT_URL,
            clientTime: Date.now(),
            device: device.brand + "_" + (device.model || "").replace(/\\s+/g, "")
        };
        if (extra) {
            for (var k in extra) payload[k] = extra[k];
        }
        try {
            http.postJson(AUDIT_URL, payload, { timeout: 2500 });
        } catch (e1) {
            http.post(AUDIT_URL, { payload: JSON.stringify(payload) }, { timeout: 2500 });
        }
    } catch (e) {}
}

logMsg("fetch " + SCRIPT_URL);
postLoaderEvent("autojs_loader_start", {});
try {
    var res = http.get(SCRIPT_URL, { timeout: 8000 });
    if (!res || res.statusCode != 200) {
        var code = res ? res.statusCode : "no response";
        logMsg("download failed: " + code);
        postLoaderEvent("autojs_loader_error", { reason: "download_failed", statusCode: code });
        toast("auto_btc download failed: " + code);
        exit();
    }
    var script = res.body.string();
    logMsg("downloaded " + script.length + " bytes");
    postLoaderEvent("autojs_loader_exec", { bytes: script.length });
    engines.execScript("auto_btc_latest", script);
} catch (e) {
    logMsg("loader error: " + e);
    postLoaderEvent("autojs_loader_error", { reason: String(e) });
    toast("auto_btc loader error");
}
`;
      res.type("text/javascript").send(code);
    });

    app.get("/auto_btc_bootstrap.js", (req, res) => {
      const runtime = runtimeInfo();
      // Bootstrap 文本保持为独立 AutoJS 脚本，仅注入当前 loader 地址。
      const code = `"auto";

var LOADER_URL = "${runtime.loaderUrl}";

console.log("[bootstrap] fetch " + LOADER_URL);
try {
    var res = http.get(LOADER_URL, { timeout: 8000 });
    if (!res || res.statusCode != 200) {
        var code = res ? res.statusCode : "no response";
        console.log("[bootstrap] loader download failed: " + code);
        toast("loader download failed: " + code);
        exit();
    }
    var loader = res.body.string();
    console.log("[bootstrap] downloaded loader " + loader.length + " bytes");
    engines.execScript("auto_btc_loader_remote", loader);
} catch (e) {
    console.log("[bootstrap] error: " + e);
    toast("bootstrap error");
}
`;
      res.type("text/javascript").send(code);
    });
  }

  return {
    localHttpUrls,
    autoScriptVersion,
    runtimeInfo,
    tabletDiagnostics,
    registerRoutes
  };
}

module.exports = { createTabletRuntime };
