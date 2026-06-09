"auto";

var PACKAGE = "com.binance.dev";
var BASE_URL = "http://115.190.218.128:3000";
var SIGNAL_URL = BASE_URL + "/api/signal";
var CONFIG_URL = BASE_URL + "/api/config";
var AUDIT_URL = BASE_URL + "/api/trade-audit";
var BALANCE_URL = BASE_URL + "/api/balance";
var MANUAL_URL = BASE_URL + "/api/manual";
var API_TOKEN = "";
var SCRIPT_VERSION = "2026-06-09-arch-v10";
var POLL_INTERVAL = 3000;
var SIGNAL_MAX_AGE_MS = 60000;

var tradeConfig = { amount: "5", duration: "30", autoTrade: false, minConfidence: 35, tiersEnabled: false, tiers: [], skipConflictSignals: false, queueOrderPolicy: "confidence_desc", preventOverlapOrders: true, maxActionableLagMs: SIGNAL_MAX_AGE_MS };
var lastTradeTime = 0;
var lastDirection = "";
var lastSignalKeyByStrategy = {};
var lastTradeTimeByStrategy = {};
var activeUntilByStrategy = {};
var persistedOrderKeys = {};
var lastConflictSkipKey = "";
var isRunning = true;
var durationSet = false;
var durationSetTarget = "";

// Balance reporting state
var lastBalanceReport = 0;
var lastLogTime = 0;
var lastWakeLock = 0;
var lastRuntimeAliveCheck = 0;
var lastScreenNudge = 0;
var lastAuditHeartbeat = 0;
var balanceFailCount = 0;
var lastBalanceValue = null;
var BALANCE_INTERVAL_MS = 30000;
var RUNTIME_ALIVE_INTERVAL_MS = 10000;
var SCREEN_NUDGE_INTERVAL_MS = 180000;
var deviceId = (device.brand + "_" + (device.model || "").replace(/\s+/g, ""));

device.keepScreenOn();

function log(msg) {
    var t = new Date().toLocaleTimeString();
    console.log("[" + t + "] " + msg);
}

function authUrl(url) {
    if (!API_TOKEN) return url;
    return url + (url.indexOf("?") >= 0 ? "&" : "?") + "token=" + encodeURIComponent(API_TOKEN);
}

function reportTradeAudit(event, order, extra) {
    try {
        var payload = {
            event: event,
            clientTime: Date.now(),
            device: deviceId,
            balance: lastBalanceValue,
            version: SCRIPT_VERSION,
            strategyId: order && order.strategyId ? order.strategyId : "manual",
            direction: order && order.signal ? order.signal : null,
            amount: order && order.amount ? String(order.amount) : String(tradeConfig.amount),
            duration: order && order.duration ? String(order.duration) : String(tradeConfig.duration),
            signalTime: order && order.time ? order.time : null,
            actionableTime: order && order.actionableTime ? order.actionableTime : null,
            confidence: order && order.confidence != null ? order.confidence : null,
            rsi_value: order && order.rsi_value != null ? order.rsi_value : null,
            avg_prob: order && order.raw ? order.raw.avg_prob : null,
            threshold: order && order.raw ? order.raw.threshold : null,
            queueBatchId: order && order.queueBatchId ? order.queueBatchId : null,
            queuePosition: order && order.queuePosition ? order.queuePosition : null,
            queueLength: order && order.queueLength ? order.queueLength : null,
            queueOrderPolicy: order && order.queueOrderPolicy ? order.queueOrderPolicy : null,
            queueBatchStartClientTime: order && order.queueBatchStartClientTime ? order.queueBatchStartClientTime : null,
            previousOrderDoneClientTime: order && order.previousOrderDoneClientTime ? order.previousOrderDoneClientTime : null,
            sincePreviousDoneMs: order && order.sincePreviousDoneMs != null ? order.sincePreviousDoneMs : null,
            sinceQueueBatchStartMs: order && order.sinceQueueBatchStartMs != null ? order.sinceQueueBatchStartMs : null,
            signalPrice: order && order.signalPrice != null ? order.signalPrice : null,
            currentPrice: order && order.currentPrice != null ? order.currentPrice : null,
            actionableAgeMs: order && order.localActionableAgeMs != null ? order.localActionableAgeMs : (order && order.actionableAgeMs != null ? order.actionableAgeMs : null),
            maxActionableLagMs: order && order.maxActionableLagMs != null ? order.maxActionableLagMs : null,
            priceChangeBps: order && order.priceChangeBps != null ? order.priceChangeBps : null,
            directionMoveBps: order && order.directionMoveBps != null ? order.directionMoveBps : null,
            signal: order && order.raw ? order.raw : null
        };
        if (extra) {
            for (var k in extra) payload[k] = extra[k];
        }
        var res = null;
        try {
            res = http.postJson(authUrl(AUDIT_URL), payload, { timeout: 2500 });
        } catch (e1) {
            res = http.post(authUrl(AUDIT_URL), { payload: JSON.stringify(payload) }, { timeout: 2500 });
        }
        return !!(res && res.statusCode >= 200 && res.statusCode < 300);
    } catch (e) {
        log("[Audit] post err: " + e);
    }
    return false;
}

function summarizeSignal(sig) {
    if (!sig) return null;
    return {
        signal: sig.signal || null,
        confidence: sig.confidence != null ? sig.confidence : null,
        rsi: sig.rsi_value != null ? sig.rsi_value : null,
        high_conf: sig.high_conf,
        rsi_extreme: sig.rsi_extreme,
        session_ok: sig.session_ok,
        agree: sig.agree,
        threshold: sig.threshold,
        skip_hours_utc: sig.skip_hours_utc || [],
        actionable_time: sig.actionable_time || sig.candle_close_time || sig.time || null
    };
}

function parseSignalTimeMs(value) {
    if (value === undefined || value === null) return 0;
    var s = String(value).replace(/^\s+|\s+$/g, "");
    if (!s) return 0;
    s = s.replace(" ", "T").replace(/(\.\d{3})\d+/, "$1");
    var ms = Date.parse(s);
    if (!isNaN(ms)) return ms;
    var m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.(\d+))?(Z|([+-])(\d{2}):?(\d{2}))?$/);
    if (!m) return 0;
    var year = Number(m[1]);
    var month = Number(m[2]) - 1;
    var day = Number(m[3]);
    var hour = Number(m[4]);
    var minute = Number(m[5]);
    var second = Number(m[6] || 0);
    var milli = Number(String(m[7] || "0").substring(0, 3));
    var utc = Date.UTC(year, month, day, hour, minute, second, milli);
    if (m[8] && m[8] !== "Z") {
        var sign = m[9] === "-" ? -1 : 1;
        var offMin = Number(m[10] || 0) * 60 + Number(m[11] || 0);
        utc -= sign * offMin * 60000;
    }
    return utc;
}

function asNumberOrNull(value) {
    var n = Number(value);
    return isFinite(n) ? n : null;
}

function maxActionableLagMs() {
    var lag = Number(tradeConfig.maxActionableLagMs || SIGNAL_MAX_AGE_MS);
    if (!isFinite(lag) || lag <= 0) return SIGNAL_MAX_AGE_MS;
    return lag;
}

function buildExecutionContext(sig) {
    var ctx = sig && sig.execution_context ? sig.execution_context : {};
    var actionableTime = sig ? (sig.actionable_time || sig.candle_close_time || sig.time || "") : "";
    var actionableMs = asNumberOrNull(ctx.actionable_time_ms);
    if (!actionableMs) actionableMs = parseSignalTimeMs(actionableTime);
    var signalPrice = asNumberOrNull(ctx.signal_price);
    if (signalPrice === null && sig) signalPrice = asNumberOrNull(sig.price);
    var currentPrice = asNumberOrNull(ctx.current_price);
    var priceChangeBps = asNumberOrNull(ctx.price_change_bps);
    var directionMoveBps = asNumberOrNull(ctx.direction_move_bps);
    if (priceChangeBps === null && signalPrice && currentPrice) {
        priceChangeBps = ((currentPrice - signalPrice) / signalPrice) * 10000;
    }
    if (directionMoveBps === null && priceChangeBps !== null && sig && (sig.signal === "UP" || sig.signal === "DOWN")) {
        directionMoveBps = sig.signal === "UP" ? priceChangeBps : -priceChangeBps;
    }
    return {
        actionableTimeMs: actionableMs || 0,
        actionableAgeMs: asNumberOrNull(ctx.actionable_age_ms),
        maxActionableLagMs: asNumberOrNull(ctx.max_actionable_lag_ms) || maxActionableLagMs(),
        signalPrice: signalPrice,
        currentPrice: currentPrice,
        priceChangeBps: priceChangeBps,
        directionMoveBps: directionMoveBps
    };
}

function updateOrderTimingAge(order) {
    if (!order || !order.actionableTime) return { ok: true };
    var actionableMs = order.actionableTimeMs || parseSignalTimeMs(order.actionableTime);
    var lagMs = Number(order.maxActionableLagMs || maxActionableLagMs());
    order.maxActionableLagMs = lagMs;
    if (!actionableMs) {
        return { ok: false, reason: "signal_time_parse_failed", actionableMs: 0, ageMs: null, lagMs: lagMs };
    }
    order.actionableTimeMs = actionableMs;
    order.localActionableAgeMs = Date.now() - actionableMs;
    if (order.localActionableAgeMs > lagMs) {
        return { ok: false, reason: "stale_actionable_signal", actionableMs: actionableMs, ageMs: order.localActionableAgeMs, lagMs: lagMs };
    }
    return { ok: true, actionableMs: actionableMs, ageMs: order.localActionableAgeMs, lagMs: lagMs };
}

function reportHeartbeat(payload) {
    if (Date.now() - lastAuditHeartbeat < 60000) return;
    lastAuditHeartbeat = Date.now();
    reportTradeAudit("autojs_heartbeat", null, {
        screenOn: (function(){ try { return device.isScreenOn(); } catch(e) { return null; } })(),
        version: SCRIPT_VERSION,
        autoTrade: tradeConfig.autoTrade,
        minConfidence: tradeConfig.minConfidence,
        maxActionableLagMs: maxActionableLagMs(),
        signalOk: !!payload,
        strategies: {
            BTC_10min: summarizeSignal(payload && payload.BTC_10min),
            BTC_30min: summarizeSignal(payload && payload.BTC_30min)
        },
        balance: lastBalanceValue
    });
}
// ========== Keep device awake ==========
function ensureAwake() {
    try {
        if (!device.isScreenOn()) {
            log("Screen OFF -> wakeUp sequence");
            var w = device.width, h = device.height;
            // Stage 1: wake screen
            device.wakeUp();
            sleep(800);
            // Stage 2: dismiss keyguard with long swipe up
            swipe(w / 2, h * 0.85, w / 2, h * 0.15, 600);
            sleep(800);
            // Stage 3: verify, retry once if needed
            if (!device.isScreenOn()) {
                log("wakeUp retry");
                device.wakeUp(); sleep(1200);
                swipe(w / 2, h * 0.9, w / 2, h * 0.1, 1000);
                sleep(1000);
            }
            // Stage 4: ensure Binance foreground
            if (device.isScreenOn()) {
                if (currentPackage() != PACKAGE) {
                    log("relaunching " + PACKAGE);
                    app.launch(PACKAGE); sleep(3000);
                }
                log("awake OK, pkg=" + currentPackage());
            } else {
                log("WARNING: screen still off after wake attempts");
            }
        }
    } catch (e) { log("ensureAwake err: " + e); }
}

function verifyAwake() {
    log("verifyAwake: screenOn=" + device.isScreenOn() + " pkg=" + currentPackage());
    if (!device.isScreenOn()) ensureAwake();
    if (currentPackage() != PACKAGE) {
        log("launching " + PACKAGE);
        app.launch(PACKAGE); sleep(3000);
    }
}

function keepScreenAlwaysOn() {
    var dayMs = 24 * 60 * 60 * 1000;

    // AutoJS wake lock. This helps, but some OPPO/ColorOS builds may still
    // obey the system screen timeout unless we also change SCREEN_OFF_TIMEOUT.
    try {
        device.keepScreenOn(dayMs);
        log("[Screen] keepScreenOn(" + dayMs + "ms)");
    } catch (e) {
        log("[Screen] keepScreenOn err: " + e);
    }

    try {
        importClass(android.provider.Settings);
        var canWrite = true;
        try {
            canWrite = Settings.System.canWrite(context);
        } catch (e1) {}

        if (canWrite) {
            Settings.System.putInt(
                context.getContentResolver(),
                Settings.System.SCREEN_OFF_TIMEOUT,
                2147483647
            );
            log("[Screen] screen_off_timeout set to never");
            return true;
        }

        log("[Screen] WRITE_SETTINGS not granted; opening permission page");
        try {
            app.startActivity({
                action: "android.settings.action.MANAGE_WRITE_SETTINGS",
                data: "package:" + context.getPackageName()
            });
        } catch (e2) {
            log("[Screen] open WRITE_SETTINGS page err: " + e2);
        }
    } catch (e) {
        log("[Screen] Settings.System err: " + e);
    }

    // Last-resort fallback. Works only if AutoJS has shell/root/ADB privilege.
    try {
        var r = shell("settings put system screen_off_timeout 2147483647", true);
        log("[Screen] shell screen_off_timeout code=" + (r ? r.code : "null"));
        return r && r.code === 0;
    } catch (e3) {
        log("[Screen] shell screen_off_timeout err: " + e3);
    }

    return false;
}

function ensureRuntimeAlive(force) {
    var now = Date.now();
    if (!force && now - lastRuntimeAliveCheck < RUNTIME_ALIVE_INTERVAL_MS) return true;
    lastRuntimeAliveCheck = now;

    try {
        device.keepScreenOn(24 * 60 * 60 * 1000);
    } catch (e0) {
        log("[KeepAlive] keepScreenOn err: " + e0);
    }

    var screenOn = true;
    try {
        screenOn = device.isScreenOn();
    } catch (e1) {
        log("[KeepAlive] isScreenOn err: " + e1);
    }

    if (!screenOn) {
        log("[KeepAlive] screen off, waking");
        if (!forceWakeForTrade()) {
            reportTradeAudit("runtime_keepalive_failed", null, { reason: "screen_off_wake_failed" });
            return false;
        }
    }

    var pkg = "";
    try { pkg = currentPackage(); } catch (e2) {}
    if (pkg != PACKAGE) {
        log("[KeepAlive] Binance not foreground, pkg=" + pkg + " -> launch");
        try {
            app.launch(PACKAGE);
            sleep(3000);
            durationSet = false;
            durationSetTarget = "";
            reportTradeAudit("runtime_relaunch_app", null, { fromPackage: pkg || null, toPackage: PACKAGE });
        } catch (e3) {
            log("[KeepAlive] launch err: " + e3);
            reportTradeAudit("runtime_keepalive_failed", null, { reason: "launch_failed", fromPackage: pkg || null });
            return false;
        }
    }

    return true;
}

function safeNudgeScreen(force) {
    var now = Date.now();
    if (!force && now - lastScreenNudge < SCREEN_NUDGE_INTERVAL_MS) return;
    lastScreenNudge = now;

    try {
        if (!device.isScreenOn()) return;
    } catch (e0) {
        log("[KeepAlive] nudge isScreenOn err: " + e0);
        return;
    }

    var pkg = "";
    try { pkg = currentPackage(); } catch (e1) {}
    if (pkg != PACKAGE) return;

    var w = device.width;
    var h = device.height;
    // A tiny gesture in the upper chart area. It resets Android's idle timer
    // while staying far away from amount input and UP/DOWN buttons.
    var y = Math.floor(h * 0.32);
    var x1 = Math.floor(w * 0.34);
    var x2 = Math.floor(w * 0.33);

    try {
        log("[KeepAlive] screen nudge left");
        swipe(x1, y, x2, y, 80);
    } catch (e2) {
        log("[KeepAlive] nudge swipe err: " + e2);
    }
}

// ========== UI hierarchy dump for finding the balance TextView ==========
function debugDumpUI() {
    try {
        log("=== UI DUMP START (pkg=" + currentPackage() + ") ===");
        var pkg = currentPackage();
        var act = currentActivity ? currentActivity() : "?";
        log("activity: " + act);
        var all = selector().find();
        log("total nodes: " + all.length);
        var printed = 0;
        for (var i = 0; i < all.length && printed < 80; i++) {
            var n = all[i];
            var t = (n.text() || "").trim();
            var d = (n.desc() || "").trim();
            if (!t && !d) continue;
            var cls = (n.className() || "");
            var simple = cls ? cls.split(".").pop() : "?";
            var rid = n.id() || "";
            var combined = t + " " + d;
            // Print nodes that look like money/price/USDT, or anything with id containing balance/amount
            var isMoney = /[0-9]+\.[0-9]/.test(combined) || /^[0-9][0-9,.]*$/.test(t);
            var isKw = /USDT|余额|Balance|资产|钱包|资金|BTC|期权|交割|币安|合约|可用|balance|fund/i.test(combined);
            var idKw = /balance|amount|fund|asset/i.test(rid);
            if (isMoney || isKw || idKw) {
                var tdisp = t.length > 60 ? t.substring(0, 60) + "..." : t;
                log("  [" + simple + " id='" + rid + "'] txt='" + tdisp + "' desc='" + d.substring(0,40) + "'");
                printed++;
            }
        }
        log("=== UI DUMP END (" + printed + " matched) ===");
    } catch (e) { log("dump err: " + e); }
}

// ========== Scan balance from options screen ==========
// Binance OPD2409 options screen layout (from UI dump):
//   id=2131448003 "可用"        (label)
//   id=2131448004 "67.87 USDT"  (available balance) <-- we want this
//   id=2131449789 "0 USDT"      (probably margin, ignore)
//   id=2131449794 "0 USDT"      (probably frozen, ignore)
var BALANCE_VIEW_ID = "com.binance.dev:id/2131448004";

function scanBalance() {
    try {
        // Strategy A: direct id hit (fastest, most stable)
        var node = id(BALANCE_VIEW_ID).findOne(1500);
        if (node) {
            var t = (node.text() || "").trim();
            var m = t.match(/([0-9]+(?:\.[0-9]+)?)/);
            if (m) {
                var num = parseFloat(m[1]);
                if (!isNaN(num) && num >= 0) {
                    log("[Balance] hit id=" + BALANCE_VIEW_ID + " text='" + t + "' -> " + num);
                    return num;
                }
            }
        }
        // Strategy B: find "可用" label, take its sibling/next TextView
        var label = selector().text("可用").findOne(1500);
        if (label) {
            // Try same parent's other children, or next sibling
            var sib = label.nextSibling();
            while (sib) {
                var st = (sib.text() || "").trim();
                var mm = st.match(/([0-9]+(?:\.[0-9]+)?)\s*USDT/);
                if (mm) { var n = parseFloat(mm[1]); if (!isNaN(n) && n >= 0) { log("[Balance] via 可用 sibling: " + st + " -> " + n); return n; } }
                sib = sib.nextSibling();
            }
            // Try parent and look for any USDT-bearing text in descendants
            var par = label.parent();
            if (par) {
                var kids = par.find(selector().textMatches(/USDT/));
                for (var k = 0; k < kids.length; k++) {
                    var kt = (kids[k].text() || "").trim();
                    var km = kt.match(/([0-9]+(?:\.[0-9]+)?)\s*USDT/);
                    if (km) { var kn = parseFloat(km[1]); if (!isNaN(kn) && kn >= 0) { log("[Balance] via 可用 parent scan: " + kt + " -> " + kn); return kn; } }
                }
            }
        }
        // Strategy C: any TextView with "X USDT" that's the largest visible number (fallback)
        var allUsdt = selector().textMatches(/^[0-9]+(?:\.[0-9]+)?\s*USDT$/).find();
        var best = -1; var bestText = "";
        for (var j = 0; j < allUsdt.length; j++) {
            var ut = (allUsdt[j].text() || "").trim();
            var um = ut.match(/^([0-9]+(?:\.[0-9]+)?)\s*USDT$/);
            if (um) {
                var un = parseFloat(um[1]);
                if (!isNaN(un) && un > best) { best = un; bestText = ut; }
            }
        }
        if (best >= 0) { log("[Balance] via largest USDT fallback: " + bestText + " -> " + best); return best; }
        return null;
    } catch (e) {
        log("scanBalance err: " + e);
        return null;
    }
}

function reportBalance() {
    var amt = scanBalance();
    if (amt == null || isNaN(amt)) {
        balanceFailCount++;
        if (balanceFailCount == 5) debugDumpUI();
        log("[Balance] scan null (count=" + balanceFailCount + ")");
        return false;
    }
    balanceFailCount = 0;
    // Always POST (debug mode); dedup re-enabled once POST verified working
    if (lastBalanceValue != null && Math.abs(amt - lastBalanceValue) < 0.001) {
        lastBalanceReport = Date.now();
        return true;
    }
    try {
        var payload = { amount: amt, time: Date.now(), device: deviceId };
        var res = null;
        try {
            res = http.postJson(authUrl(BALANCE_URL), payload, { timeout: 3000 });
        } catch (e1) {
            res = http.post(authUrl(BALANCE_URL), { amount: String(amt), time: String(Date.now()), device: String(deviceId) }, { timeout: 3000 });
        }
        if (res && res.statusCode == 200) {
            lastBalanceValue = amt;
            lastBalanceReport = Date.now();
            log("[Balance] reported: " + amt + " USDT");
            return true;
        } else {
            log("[Balance] POST failed: code=" + (res ? res.statusCode : "no-res"));
        }
    } catch (e) {
        log("[Balance] post err: " + e);
    }
    return false;
}


// ========== Fetch dynamic config ==========
function fetchConfig() {
    try {
        var r = http.get(authUrl(CONFIG_URL), { timeout: 3000 });
        if (r.statusCode == 200) {
            var c = r.body.json();
            if (c.amount) tradeConfig.amount = c.amount;
            if (c.duration) tradeConfig.duration = c.duration;
            if (c.autoTrade !== undefined) tradeConfig.autoTrade = c.autoTrade;
            if (c.minConfidence !== undefined) tradeConfig.minConfidence = c.minConfidence;
            if (c.skipConflictSignals !== undefined) tradeConfig.skipConflictSignals = c.skipConflictSignals;
            if (c.queueOrderPolicy !== undefined) tradeConfig.queueOrderPolicy = c.queueOrderPolicy;
            if (c.preventOverlapOrders !== undefined) tradeConfig.preventOverlapOrders = !!c.preventOverlapOrders;
            if (c.maxActionableLagMs !== undefined) tradeConfig.maxActionableLagMs = Number(c.maxActionableLagMs);
        }
    } catch (e) {}
}

// ========== Signal ==========
function getSignalPayload() {
    try {
        var r = http.get(authUrl(SIGNAL_URL), { timeout: 5000 });
        if (r.statusCode == 200) {
            var data = r.body.json();
            // Extract config if present
            if (data._config) {
                var c = data._config;
                if (c.amount) tradeConfig.amount = c.amount;
                if (c.duration) tradeConfig.duration = c.duration;
                if (c.autoTrade !== undefined) tradeConfig.autoTrade = c.autoTrade;
                if (c.minConfidence !== undefined) tradeConfig.minConfidence = c.minConfidence;
                if (c.tiersEnabled !== undefined) tradeConfig.tiersEnabled = c.tiersEnabled;
                if (c.tiers && c.tiers.length !== undefined) tradeConfig.tiers = c.tiers;
                if (c.skipConflictSignals !== undefined) tradeConfig.skipConflictSignals = c.skipConflictSignals;
                if (c.queueOrderPolicy !== undefined) tradeConfig.queueOrderPolicy = c.queueOrderPolicy;
                if (c.preventOverlapOrders !== undefined) tradeConfig.preventOverlapOrders = !!c.preventOverlapOrders;
                if (c.maxActionableLagMs !== undefined) tradeConfig.maxActionableLagMs = Number(c.maxActionableLagMs);
                delete data._config;
            }
            return data;
        }
    } catch (e) {
        log("API err: " + e);
    }
    return null;
}

function amountForConfidence(conf) {
    if (tradeConfig.tiersEnabled && tradeConfig.tiers && tradeConfig.tiers.length) {
        var sorted = tradeConfig.tiers.slice().sort(function(a, b) {
            return Number(b.min) - Number(a.min);
        });
        for (var i = 0; i < sorted.length; i++) {
            var t = sorted[i];
            if (Number(conf) >= Number(t.min)) return String(t.amount);
        }
    }
    return String(tradeConfig.amount);
}

function amountForOrder(strategyId, sig, serverAmount) {
    if (sig && sig.confidence != null) return amountForConfidence(sig.confidence);
    if (serverAmount) return String(serverAmount);
    if (sig && sig.amount && sig.fixed_amount !== true) return String(sig.amount);
    return String(tradeConfig.amount);
}

function buildTradeQueue(data) {
    var queue = [];
    if (!data) return queue;
    var amounts = data._strategyAmounts || {};
    var defs = [
        { id: "BTC_30min", duration: "30" },
        { id: "BTC_10min", duration: "10" }
    ];
    for (var i = 0; i < defs.length; i++) {
        var d = defs[i];
        var sig = data[d.id];
        if (!sig) continue;
        var amount = amountForOrder(d.id, sig, amounts[d.id]);
        var exec = buildExecutionContext(sig);
        queue.push({
            strategyId: d.id,
            signal: sig.signal,
            confidence: sig.confidence || 0,
            rsi_value: sig.rsi_value,
            time: sig.time || "",
            actionableTime: sig.actionable_time || sig.candle_close_time || sig.time || "",
            actionableTimeMs: exec.actionableTimeMs,
            actionableAgeMs: exec.actionableAgeMs,
            maxActionableLagMs: exec.maxActionableLagMs,
            signalPrice: exec.signalPrice,
            currentPrice: exec.currentPrice,
            priceChangeBps: exec.priceChangeBps,
            directionMoveBps: exec.directionMoveBps,
            amount: String(amount),
            duration: String(sig.duration || d.duration),
            bypassMinConfidence: !!sig.bypass_min_confidence_filter,
            raw: sig
        });
    }
    return sortTradeQueue(queue);
}

function sortTradeQueue(queue) {
    var policy = tradeConfig.queueOrderPolicy || "confidence_desc";
    queue.sort(function(a, b) {
        var ta = a.actionableTime || a.time || "";
        var tb = b.actionableTime || b.time || "";
        if (ta !== tb) return ta < tb ? -1 : 1;
        if (policy === "10_then_30") {
            return (a.strategyId === "BTC_10min" ? 0 : 1) - (b.strategyId === "BTC_10min" ? 0 : 1);
        }
        if (policy === "30_then_10") {
            return (a.strategyId === "BTC_30min" ? 0 : 1) - (b.strategyId === "BTC_30min" ? 0 : 1);
        }
        var ca = Number(a.confidence || 0);
        var cb = Number(b.confidence || 0);
        if (cb !== ca) return cb - ca;
        return (a.strategyId === "BTC_30min" ? 0 : 1) - (b.strategyId === "BTC_30min" ? 0 : 1);
    });
    return queue;
}

function filterConflictQueue(queue) {
    if (!tradeConfig.skipConflictSignals || queue.length < 2) return queue;
    var buckets = {};
    for (var i = 0; i < queue.length; i++) {
        var order = queue[i];
        if (!order || !order.signal) continue;
        var k = order.actionableTime || order.time || "unknown";
        if (!buckets[k]) buckets[k] = [];
        buckets[k].push(order);
    }
    var skip = {};
    for (var key in buckets) {
        var group = buckets[key];
        var hasUp = false, hasDown = false;
        for (var j = 0; j < group.length; j++) {
            if (group[j].signal == "UP") hasUp = true;
            if (group[j].signal == "DOWN") hasDown = true;
        }
        if (hasUp && hasDown) {
            var ids = [];
            for (var n = 0; n < group.length; n++) {
                skip[group[n].strategyId + "|" + group[n].time + "|" + group[n].signal] = true;
                ids.push(group[n].strategyId + ":" + group[n].signal);
            }
            var conflictKey = key + "|" + ids.join(",");
            if (conflictKey != lastConflictSkipKey) {
                lastConflictSkipKey = conflictKey;
                for (var r = 0; r < group.length; r++) {
                    reportTradeAudit("signal_skipped", group[r], { reason: "direction_conflict_filter", group: ids.join(",") });
                }
            }
            log("Conflict filter skipped " + ids.join(" + ") + " at " + key);
        }
    }
    var out = [];
    for (var q = 0; q < queue.length; q++) {
        var o = queue[q];
        if (!skip[o.strategyId + "|" + o.time + "|" + o.signal]) out.push(o);
    }
    return out;
}

// ========== Duration ==========
function ensureDuration() {
    var target = tradeConfig.duration || "30";
    if (durationSet && durationSetTarget == target) return true;
    
    var timeBtn = id("tv_time_increment_b").findOne(3000);
    if (!timeBtn) return false;
    
    var current = timeBtn.text() || "";
    if (current.indexOf(target) >= 0) {
        durationSet = true;
        durationSetTarget = target;
        return true;
    }
    
    timeBtn.click();
    sleep(1500);
    
    var rows = id("2131439375").find();
    // Map duration to row index: 10min=0, 30min=1, 1hour=2, 1day=3
    var idx = {"10": 0, "30": 1, "60": 2, "1": 3};
    var targetIdx = idx[target] !== undefined ? idx[target] : 1;
    
    if (rows.length > targetIdx) {
        rows[targetIdx].click();
        sleep(1500);
    } else {
        click(1200, 1994);
        sleep(1500);
    }
    
    var v = id("tv_time_increment_b").findOne(2000);
    if (v && (v.text() || "").indexOf(target) >= 0) {
        log("Duration set: " + target + "min");
        durationSet = true;
        durationSetTarget = target;
        return true;
    }
    return false;
}

// ========== Set amount ==========
function setAmount(amt) {
    var input = id("2131431885").findOne(3000);
    if (!input) return false;
    input.click(); sleep(200);
    input.setText(amt); sleep(300);
    return true;
}

// ========== Direction ==========
function clickUp() {
    var btn = id("2131432526").findOne(2000);
    if (btn) { btn.click(); return; }
    click(1285, 1840);
}
function clickDown() {
    var btn = id("2131432527").findOne(2000);
    if (btn) { btn.click(); return; }
    click(2107, 1840);
}

// ========== Confirm ==========
function handleConfirm() {
    sleep(1500);
    var btn = id("2131448374").findOne(3000);
    if (btn) { btn.click(); log("Confirmed"); return; }
    var btns = selector().textMatches(".*确认.*|.*确定.*").clickable(true).find();
    for (var i = 0; i < btns.length; i++) { btns[i].click(); return; }
}

// ========== Trade ==========
// Forceful wake that survives Android 10+ restrictions
function forceWakeForTrade() {
    if (device.isScreenOn()) { log("wake: already on"); return true; }
    log("wake: screen off, attempting wake");
    var w = device.width, h = device.height;
    
    // Method 1: device.wakeUp + swipe
    try {
        device.wakeUp(); sleep(1000);
        swipe(w/2, h*0.85, w/2, h*0.15, 600); sleep(800);
    } catch (e) { log("wake m1 err: " + e); }
    
    if (device.isScreenOn()) { log("wake OK via m1"); return true; }
    
    // Method 2: shell keyevent (needs root or ADB privileges, but worth trying)
    try {
        shell("input keyevent KEYCODE_WAKEUP", true);
        sleep(800);
    } catch (e) { log("wake m2 err: " + e); }
    
    if (device.isScreenOn()) { log("wake OK via m2"); return true; }
    
    // Method 3: shell tap the screen
    try {
        shell("input tap " + (w/2) + " " + (h/2), true);
        sleep(800);
    } catch (e) { log("wake m3 err: " + e); }
    
    if (device.isScreenOn()) { log("wake OK via m3"); return true; }
    
    // Method 4: media-style wake (play media session event)
    try {
        shell("am broadcast -a android.intent.action.SCREEN_ON", true);
        sleep(800);
    } catch (e) {}
    
    var final = device.isScreenOn();
    log("wake final: " + (final ? "ON" : "STILL OFF"));
    return final;
}

function placeTrade(dir, order) {
    var prevAmt = tradeConfig.amount;
    var prevDur = tradeConfig.duration;
    var strategyId = order && order.strategyId ? order.strategyId : "manual";
    if (order && order.amount) tradeConfig.amount = String(order.amount);
    if (order && order.duration) {
        if (String(order.duration) != String(tradeConfig.duration)) {
            durationSet = false;
            durationSetTarget = "";
        }
        tradeConfig.duration = String(order.duration);
    }
    log(">>> TRADE " + strategyId + ": " + dir + " " + tradeConfig.amount + "U x " + tradeConfig.duration + "min");
    reportTradeAudit("order_attempt", order, { direction: dir });
    
    // STEP 1: ensure screen is on (clicks won't work otherwise)
    if (!forceWakeForTrade()) {
        log(">>> ABORT: cannot wake screen");
        reportTradeAudit("order_abort", order, { direction: dir, reason: "cannot_wake_screen" });
        tradeConfig.amount = prevAmt;
        tradeConfig.duration = prevDur;
        return false;
    }
    
    // STEP 2: ensure Binance foreground
    if (currentPackage() != PACKAGE) {
        log("step2: launching " + PACKAGE);
        app.launch(PACKAGE); sleep(3000);
    } else {
        log("step2: binance already foreground");
    }
    
    // STEP 3: set duration
    log("step3: setDuration");
    if (!ensureDuration()) {
        log(">>> ABORT: duration failed");
        reportTradeAudit("order_abort", order, { direction: dir, reason: "duration_failed" });
        tradeConfig.amount = prevAmt;
        tradeConfig.duration = prevDur;
        durationSet = false;
        durationSetTarget = "";
        return false;
    }
    
    // STEP 4: set amount
    log("step4: setAmount " + tradeConfig.amount);
    if (!setAmount(tradeConfig.amount)) {
        log(">>> ABORT: amount failed");
        reportTradeAudit("order_abort", order, { direction: dir, reason: "amount_failed" });
        tradeConfig.amount = prevAmt;
        tradeConfig.duration = prevDur;
        return false;
    }

    var finalTiming = updateOrderTimingAge(order);
    if (!finalTiming.ok) {
        log(">>> ABORT: " + finalTiming.reason + " before click ageMs=" + finalTiming.ageMs);
        reportTradeAudit("order_abort", order, {
            direction: dir,
            reason: finalTiming.reason + "_before_click",
            ageMs: finalTiming.ageMs,
            maxActionableLagMs: finalTiming.lagMs
        });
        tradeConfig.amount = prevAmt;
        tradeConfig.duration = prevDur;
        return false;
    }
    
    // STEP 5: click direction
    log("step5: click " + dir);
    if (dir == "UP") clickUp(); else clickDown();
    
    // STEP 6: confirm
    log("step6: confirm");
    handleConfirm();
    log(">>> ORDER DONE");
    reportTradeAudit("order_done", order, { direction: dir });
    
    // STEP 7: refresh balance
    sleep(2000);
    log("step7: refresh balance");
    reportBalance();

    if (order) {
        tradeConfig.amount = prevAmt;
        tradeConfig.duration = prevDur;
        durationSet = false;
        durationSetTarget = "";
    }
    return true;
}

// ========== Main ==========
// Check for manual trade command from web
function checkManualTrade() {
    try {
        var r = http.get(authUrl(MANUAL_URL), { timeout: 3000 });
        if (r.statusCode == 200) {
            var cmd = r.body.json();
            if (cmd && cmd.direction && Date.now() - cmd.time < 30000) {
                var prevAmt = tradeConfig.amount;
                var prevDur = tradeConfig.duration;
                if (cmd.amount) tradeConfig.amount = cmd.amount;
                if (cmd.duration) tradeConfig.duration = cmd.duration;
                durationSet = false;
                durationSetTarget = "";
                log("MANUAL: " + cmd.direction + " " + tradeConfig.amount + "U x " + tradeConfig.duration + "min");
                placeTrade(cmd.direction);
                tradeConfig.amount = prevAmt;
                tradeConfig.duration = prevDur;
                durationSet = false;
                durationSetTarget = "";
                http.request(authUrl(MANUAL_URL), { method: "DELETE", timeout: 3000 });
                return true;
            }
        }
    } catch (e) {}
    return false;
}

// Singleton guard: prevent multiple instances from running
var LOCK_FILE = "/sdcard/auto_btc.lock";
var ORDER_HISTORY_FILE = "/sdcard/auto_btc_order_history.json";

function orderKey(order) {
    if (!order) return "";
    return [
        order.strategyId || "manual",
        order.time || "",
        order.actionableTime || "",
        order.signal || "",
        order.duration || ""
    ].join("|");
}

function loadOrderHistory() {
    try {
        if (!files.exists(ORDER_HISTORY_FILE)) return;
        var raw = files.read(ORDER_HISTORY_FILE) || "{}";
        var data = JSON.parse(raw);
        var keyData = data.keys ? data.keys : data;
        activeUntilByStrategy = {};
        if (data.activeUntilByStrategy) {
            for (var sid in data.activeUntilByStrategy) {
                var until = Number(data.activeUntilByStrategy[sid]);
                if (until && until > Date.now()) activeUntilByStrategy[sid] = until;
            }
        }
        var cutoff = Date.now() - 24 * 60 * 60 * 1000;
        persistedOrderKeys = {};
        for (var k in keyData) {
            if (Number(keyData[k]) >= cutoff) persistedOrderKeys[k] = Number(keyData[k]);
        }
        log("[Dedupe] loaded " + Object.keys(persistedOrderKeys).length + " order keys, active=" + JSON.stringify(activeUntilByStrategy));
    } catch (e) {
        persistedOrderKeys = {};
        activeUntilByStrategy = {};
        log("[Dedupe] load err: " + e);
    }
}

function saveOrderHistory() {
    try {
        files.write(ORDER_HISTORY_FILE, JSON.stringify({
            keys: persistedOrderKeys,
            activeUntilByStrategy: activeUntilByStrategy
        }));
    } catch (e) {
        log("[Dedupe] save err: " + e);
    }
}

function hasPersistedOrder(order) {
    var k = orderKey(order);
    if (!k) return false;
    var ts = persistedOrderKeys[k] || 0;
    return ts && Date.now() - ts < 24 * 60 * 60 * 1000;
}

function rememberPersistedOrder(order) {
    var k = orderKey(order);
    if (!k) return;
    persistedOrderKeys[k] = Date.now();
    saveOrderHistory();
}

function setStrategyActiveUntil(order) {
    if (!order || !order.strategyId) return;
    var dur = Math.max(1, Number(order.duration || 0) || 0);
    activeUntilByStrategy[order.strategyId] = Date.now() + dur * 60 * 1000;
    saveOrderHistory();
}

function isStrategyOverlapping(order) {
    if (!tradeConfig.preventOverlapOrders || !order || !order.strategyId) return false;
    var until = Number(activeUntilByStrategy[order.strategyId] || 0);
    if (!until || until <= Date.now()) return false;
    return true;
}

function acquireLock() {
    try {
        var f = new java.io.File(LOCK_FILE);
        if (f.exists()) {
            var age = Date.now() - f.lastModified();
            // If lock is < 60s old, another instance is alive
            if (age < 60000) {
                log("[Lock] Another instance is running (lock age=" + Math.floor(age/1000) + "s), aborting");
                toast("auto_btc.js 已在运行，新实例退出");
                exit();
            }
        }
        files.ensureDir(LOCK_FILE.substring(0, LOCK_FILE.lastIndexOf("/")) + "/");
        files.write(LOCK_FILE, String(Date.now()));
        log("[Lock] acquired: " + LOCK_FILE);
        // Refresh lock periodically
        setInterval(function(){ try { files.write(LOCK_FILE, String(Date.now())); } catch(e){} }, 10000);
    } catch (e) { log("lock err: " + e); }
}

function releaseLock() {
    try { if (files.exists(LOCK_FILE)) files.remove(LOCK_FILE); } catch (e) {}
}

function main() {
    acquireLock();
    loadOrderHistory();
    log("=== Auto Trade Started === VERSION=" + SCRIPT_VERSION + " deviceId=" + deviceId);
    log("screen w=" + device.width + " h=" + device.height);
    var auditOk = reportTradeAudit("autojs_start", null, {
        version: SCRIPT_VERSION,
        screenOn: (function(){ try { return device.isScreenOn(); } catch(e) { return null; } })()
    });
    log("[Audit] start event " + (auditOk ? "posted" : "FAILED") + " -> " + AUDIT_URL);
    keepScreenAlwaysOn();
    setInterval(function(){
        try { device.keepScreenOn(24 * 60 * 60 * 1000); } catch (e) {}
    }, 30000);
    verifyAwake();
    ensureRuntimeAlive(true);
    log("init: sleep 5s before first balance scan");
    sleep(5000);
    log("init: scanning balance now");
    reportBalance();
    log("init: entering main loop");
    
    while (isRunning) {
        try {
            ensureRuntimeAlive(false);
            safeNudgeScreen(false);
            if (checkManualTrade()) { sleep(POLL_INTERVAL); continue; }
            
            var payload = getSignalPayload();
            reportHeartbeat(payload);
            var queue = buildTradeQueue(payload);
            queue = filterConflictQueue(queue);
            
            if (!tradeConfig.autoTrade) {
                if (Date.now() % 60000 < POLL_INTERVAL) log("AutoTrade OFF");
            } else if (queue.length > 0) {
                var traded = false;
                var queueBatchId = "q|" + Date.now() + "|" + (queue[0].actionableTime || queue[0].time || "unknown");
                var queueBatchStart = Date.now();
                var previousDoneAt = 0;
                for (var qi = 0; qi < queue.length; qi++) {
                    var order = queue[qi];
                    if (!order.signal) continue;
                    order.queueBatchId = queueBatchId;
                    order.queuePosition = qi + 1;
                    order.queueLength = queue.length;
                    order.queueOrderPolicy = tradeConfig.queueOrderPolicy || "confidence_desc";
                    order.queueBatchStartClientTime = queueBatchStart;
                    order.previousOrderDoneClientTime = previousDoneAt || null;
                    order.sincePreviousDoneMs = previousDoneAt ? Date.now() - previousDoneAt : null;
                    order.sinceQueueBatchStartMs = Date.now() - queueBatchStart;
                    var conf = order.confidence || 0;
                    if (!order.bypassMinConfidence && Number(conf) < Number(tradeConfig.minConfidence || 0)) {
                        reportTradeAudit("signal_skipped", order, { reason: "min_confidence_filter", minConfidence: tradeConfig.minConfidence });
                        continue;
                    }
                    var timing = updateOrderTimingAge(order);
                    if (!timing.ok && timing.reason == "signal_time_parse_failed") {
                        reportTradeAudit("signal_skipped", order, { reason: "signal_time_parse_failed", actionableTime: order.actionableTime });
                        continue;
                    }
                    if (!timing.ok) {
                        reportTradeAudit("signal_skipped", order, { reason: timing.reason, ageMs: timing.ageMs, maxActionableLagMs: timing.lagMs });
                        continue;
                    }
                    if (timing.actionableMs > Date.now() + 30000) continue;
                    var key = orderKey(order);
                    var lastKey = lastSignalKeyByStrategy[order.strategyId] || "";
                    var now = Date.now();
                    var lastTs = lastTradeTimeByStrategy[order.strategyId] || 0;
                    if (lastKey == key) continue;
                    if (hasPersistedOrder(order)) {
                        reportTradeAudit("signal_skipped", order, { reason: "duplicate_after_restart" });
                        continue;
                    }
                    if (isStrategyOverlapping(order)) {
                        reportTradeAudit("signal_skipped", order, { reason: "strategy_overlap_guard", activeUntil: activeUntilByStrategy[order.strategyId] });
                        continue;
                    }
                    if (now - lastTs < 60000) continue;
                    log("SIGNAL " + order.strategyId + ": " + order.signal + " " + conf + "% RSI=" + order.rsi_value + " amt=" + order.amount + "U dur=" + order.duration + "min");
                    reportTradeAudit("signal_tradeable", order, {});
                    var ok = placeTrade(order.signal, order);
                    if (ok) {
                        previousDoneAt = Date.now();
                        lastSignalKeyByStrategy[order.strategyId] = key;
                        lastTradeTimeByStrategy[order.strategyId] = now;
                        rememberPersistedOrder(order);
                        setStrategyActiveUntil(order);
                        lastTradeTime = now;
                        lastDirection = order.signal;
                        traded = true;
                    }
                    if (ok && qi < queue.length - 1) {
                        log("queue: wait before next strategy order");
                        sleep(5000);
                    }
                }
            if (Date.now() - lastWakeLock > 60000) {
                try { device.keepScreenOn(); } catch (e) {}
                lastWakeLock = Date.now();
            }
                if (!traded && Date.now() - lastLogTime > 15000) {
                    var s10 = payload && payload.BTC_10min ? payload.BTC_10min : null;
                    var s30 = payload && payload.BTC_30min ? payload.BTC_30min : null;
                    log("Beat | 30m RSI=" + (s30 ? s30.rsi_value : "--") + " sig=" + (s30 && s30.signal ? s30.signal : "--") + " | 10m RSI=" + (s10 ? s10.rsi_value : "--") + " sig=" + (s10 && s10.signal ? s10.signal : "--") + " | bal=" + (lastBalanceValue || "--"));
                    lastLogTime = Date.now();
                }
            } else if (payload) {
                if (Date.now() - lastLogTime > 15000) {
                    log("Beat | no strategy payload | bal=" + (lastBalanceValue || "--"));
                    lastLogTime = Date.now();
                }
            } else {
                if (Date.now() - lastLogTime > 30000) { log("Beat | no signal yet"); lastLogTime = Date.now(); }
            }
            // Periodic balance report (every ~30s) even without trades
            if (Date.now() - lastBalanceReport >= BALANCE_INTERVAL_MS) {
                reportBalance();
            }
        } catch (e) {
            log("Err: " + e);
        }
        sleep(POLL_INTERVAL);
    }
}

events.on("exit", function() { releaseLock(); log("Stopped"); });
main();
