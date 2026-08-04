"auto";

var PACKAGE = "com.binance.dev";
var BASE_URL = "http://115.190.218.128:3000";
var SIGNAL_URL = BASE_URL + "/api/signal?source=autojs";
var CONFIG_URL = BASE_URL + "/api/config";
var AUDIT_URL = BASE_URL + "/api/trade-audit";
var BALANCE_URL = BASE_URL + "/api/balance";
var MANUAL_URL = BASE_URL + "/api/manual";
var API_TOKEN = "";
var SCRIPT_VERSION = "2026-08-05-llm-3min-live";
var POLL_INTERVAL = 1000;
var SIGNAL_MAX_AGE_MS = 3 * 60 * 1000;
var STRATEGY_COOLDOWN_MS = 10 * 60 * 1000;
var ORDER_VERIFY_TIMEOUT_MS = 12000;
var ORDER_VERIFY_POLL_MS = 250;
var ORDER_BALANCE_TOLERANCE_USDT = 0.25;
var TOUCH_NUDGE_ENABLED = true;
var POST_TRADE_NUDGE_GUARD_MS = 120000;
var CONFIRM_TEXT_PATTERN = /.*Confirm.*|.*Submit.*|.*Place\s+Order.*|.*\u786e\u8ba4.*|.*\u786e\u5b9a.*|.*\u63d0\u4ea4.*|.*\u4e0b\u5355\u786e\u8ba4.*/i;
var CONFIRM_BUTTON_IDS = ["2131448753", "2131448374"];
var UI_FAST_POLL_MS = 80;
var AMOUNT_FIND_TIMEOUT_MS = 900;
var DIRECTION_FIND_TIMEOUT_MS = 450;
var CONFIRM_FIND_TIMEOUT_MS = 6000;
var CONFIRM_MIN_SETTLE_MS = 700;
var BALANCE_FIND_TIMEOUT_MS = 650;

var tradeConfig = { amount: "5", strategyAmounts: { BTC_10min_SAFE: "5", BTC_10min_TAKER: "5" }, strategyVariants: [], duration: "10", autoTrade: false };
var lastTradeTime = 0;
var lastDirection = "";
var lastSignalKeyByStrategy = {};
var lastTradeTimeByStrategy = {};
var activeUntilByStrategy = {};
var persistedOrderKeys = {};
var isRunning = true;
var durationSet = false;
var durationSetTarget = "";
var lastAmountInputProbe = null;
var lastConfirmProbe = null;
var lastDirectionProbe = null;
var lastNodeClickProbe = null;

// Balance reporting state
var lastBalanceReport = 0;
var lastLogTime = 0;
var lastWakeLock = 0;
var lastRuntimeAliveCheck = 0;
var lastScreenNudge = 0;
var lastAuditHeartbeat = 0;
var balanceFailCount = 0;
var lastBalanceValue = null;
var lastTradeInteractionMs = 0;
var tradeInteractionActive = false;
var BALANCE_INTERVAL_MS = 30000;
var RUNTIME_ALIVE_INTERVAL_MS = 10000;
var SCREEN_NUDGE_INTERVAL_MS = 60000;
var SCREEN_TIMEOUT_NEVER_MS = 2147483647;
var SCREEN_TIMEOUT_ENSURE_INTERVAL_MS = 60000;
var deviceId = (device.brand + "_" + (device.model || "").replace(/\s+/g, ""));
var lastKeepAliveStatus = {};
var lastScreenTimeoutEnsure = 0;

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
    return SIGNAL_MAX_AGE_MS;
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
    if (order.localActionableAgeMs < 0) {
        return { ok: false, reason: "actionable_time_not_reached", actionableMs: actionableMs, ageMs: order.localActionableAgeMs, lagMs: lagMs };
    }
    if (order.localActionableAgeMs > lagMs) {
        return { ok: false, reason: "stale_actionable_signal", actionableMs: actionableMs, ageMs: order.localActionableAgeMs, lagMs: lagMs };
    }
    return { ok: true, actionableMs: actionableMs, ageMs: order.localActionableAgeMs, lagMs: lagMs };
}

function reportHeartbeat(payload) {
    if (Date.now() - lastAuditHeartbeat < 60000) return;
    lastAuditHeartbeat = Date.now();
    var strategies = {};
    var variants = (payload && payload._strategyVariants) || tradeConfig.strategyVariants || [];
    for (var i = 0; i < variants.length; i++) {
        var id = variants[i].id;
        strategies[id] = summarizeSignal(payload && payload[id]);
    }
    reportTradeAudit("autojs_heartbeat", null, {
        screenOn: (function(){ try { return device.isScreenOn(); } catch(e) { return null; } })(),
        version: SCRIPT_VERSION,
        autoTrade: tradeConfig.autoTrade,
        maxActionableLagMs: maxActionableLagMs(),
        signalOk: !!payload,
        strategies: strategies,
        keepAlive: readKeepAliveStatus(),
        balance: lastBalanceValue
    });
}

function readKeepAliveStatus() {
    var out = {
        version: SCRIPT_VERSION,
        packageName: null,
        foregroundPackage: null,
        screenOn: null,
        writeSettingsGranted: null,
        screenOffTimeoutMs: null,
        batteryOptimizationIgnored: null,
        touchNudgeEnabled: TOUCH_NUDGE_ENABLED,
        touchNudgeGuardMs: POST_TRADE_NUDGE_GUARD_MS,
        nudgeIntervalMs: SCREEN_NUDGE_INTERVAL_MS,
        runtimeCheckIntervalMs: RUNTIME_ALIVE_INTERVAL_MS
    };
    try { out.packageName = context.getPackageName(); } catch (e0) {}
    try { out.foregroundPackage = currentPackage(); } catch (e1) {}
    try { out.screenOn = device.isScreenOn(); } catch (e2) {}

    try {
        importClass(android.provider.Settings);
        try { out.writeSettingsGranted = Settings.System.canWrite(context); } catch (e3) {}
        try {
            out.screenOffTimeoutMs = Settings.System.getInt(
                context.getContentResolver(),
                Settings.System.SCREEN_OFF_TIMEOUT,
                -1
            );
        } catch (e4) {}
    } catch (e5) {
        out.settingsError = String(e5);
    }

    try {
        var pm = context.getSystemService(android.content.Context.POWER_SERVICE);
        if (pm && pm.isIgnoringBatteryOptimizations) {
            out.batteryOptimizationIgnored = pm.isIgnoringBatteryOptimizations(context.getPackageName());
        }
    } catch (e6) {
        out.batteryOptimizationError = String(e6);
    }

    lastKeepAliveStatus = out;
    return out;
}

function ensureSystemScreenTimeoutNever(force, openPermissionPage) {
    var now = Date.now();
    if (!force && now - lastScreenTimeoutEnsure < SCREEN_TIMEOUT_ENSURE_INTERVAL_MS) {
        return { skipped: true, status: lastKeepAliveStatus };
    }
    lastScreenTimeoutEnsure = now;

    var result = { ok: false, changed: false, canWrite: null, before: null, after: null, method: null };
    try {
        importClass(android.provider.Settings);
        try { result.canWrite = Settings.System.canWrite(context); } catch (e1) {}
        try {
            result.before = Settings.System.getInt(
                context.getContentResolver(),
                Settings.System.SCREEN_OFF_TIMEOUT,
                -1
            );
        } catch (e2) {}

        if (result.canWrite) {
            if (Number(result.before) < 2147483000) {
                Settings.System.putInt(
                    context.getContentResolver(),
                    Settings.System.SCREEN_OFF_TIMEOUT,
                    SCREEN_TIMEOUT_NEVER_MS
                );
                result.changed = true;
            }
            try {
                result.after = Settings.System.getInt(
                    context.getContentResolver(),
                    Settings.System.SCREEN_OFF_TIMEOUT,
                    -1
                );
            } catch (e3) {}
            result.ok = Number(result.after) >= 2147483000;
            result.method = "Settings.System";
            readKeepAliveStatus();
            return result;
        }

        if (openPermissionPage) {
            log("[Screen] WRITE_SETTINGS not granted; opening permission page");
            try {
                app.startActivity({
                    action: "android.settings.action.MANAGE_WRITE_SETTINGS",
                    data: "package:" + context.getPackageName()
                });
            } catch (e4) {
                result.permissionPageError = String(e4);
                log("[Screen] open WRITE_SETTINGS page err: " + e4);
            }
        }
    } catch (e5) {
        result.settingsError = String(e5);
        log("[Screen] Settings.System err: " + e5);
    }

    try {
        var r = shell("settings put system screen_off_timeout " + SCREEN_TIMEOUT_NEVER_MS, true);
        result.shellCode = r ? r.code : null;
        log("[Screen] shell screen_off_timeout code=" + (r ? r.code : "null"));
        if (r && r.code === 0) {
            result.changed = true;
            result.method = "shell";
            readKeepAliveStatus();
            var latest = readKeepAliveStatus();
            result.after = latest.screenOffTimeoutMs;
            result.ok = Number(result.after) >= 2147483000;
            return result;
        }
    } catch (e6) {
        result.shellError = String(e6);
        log("[Screen] shell screen_off_timeout err: " + e6);
    }

    readKeepAliveStatus();
    return result;
}

function reportKeepAliveStatus(event, extra) {
    var status = readKeepAliveStatus();
    var payload = { keepAlive: status };
    if (extra) {
        for (var k in extra) payload[k] = extra[k];
    }
    reportTradeAudit(event || "autojs_keepalive_status", null, payload);
    return status;
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
    var wakeLockRequested = false;

    // AutoJS wake lock. This helps, but some OPPO/ColorOS builds may still
    // obey the system screen timeout unless we also change SCREEN_OFF_TIMEOUT.
    try {
        device.keepScreenOn(dayMs);
        wakeLockRequested = true;
        log("[Screen] keepScreenOn(" + dayMs + "ms)");
    } catch (e) {
        log("[Screen] keepScreenOn err: " + e);
    }

    var timeoutResult = ensureSystemScreenTimeoutNever(true, true);
    if (timeoutResult.ok) log("[Screen] screen_off_timeout set to never");
    return wakeLockRequested || timeoutResult.ok;
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

    var timeoutResult = ensureSystemScreenTimeoutNever(false, false);
    if (timeoutResult && timeoutResult.changed) {
        reportKeepAliveStatus("autojs_keepalive_status", {
            phase: "reapply_screen_timeout",
            screenTimeoutBeforeMs: timeoutResult.before,
            screenTimeoutAfterMs: timeoutResult.after,
            screenTimeoutMethod: timeoutResult.method,
            screenTimeoutOk: timeoutResult.ok
        });
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
            reportKeepAliveStatus("runtime_keepalive_failed", { reason: "screen_off_wake_failed" });
            return false;
        }
        reportKeepAliveStatus("runtime_screen_wake", { reason: "screen_was_off" });
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
            reportKeepAliveStatus("runtime_relaunch_app", { fromPackage: pkg || null, toPackage: PACKAGE });
        } catch (e3) {
            log("[KeepAlive] launch err: " + e3);
            reportKeepAliveStatus("runtime_keepalive_failed", { reason: "launch_failed", fromPackage: pkg || null });
            return false;
        }
    }

    return true;
}

function tradeUiBlocksNudge() {
    if (tradeInteractionActive) return true;
    try {
        if (findOnceSafe(id("2131448374"))) return true;
    } catch (e1) {}
    try {
        if (findOnceSafe(selector().textMatches(CONFIRM_TEXT_PATTERN))) return true;
    } catch (e2) {}
    try {
        var input = findOnceSafe(className("android.widget.EditText"));
        if (input && input.focused() === true) return true;
    } catch (e3) {}
    try {
        if (findOnceSafe(classNameMatches(/Dialog|Popup/i))) return true;
    } catch (e4) {}
    return false;
}

function findOnceSafe(sel) {
    try {
        if (sel && typeof sel.findOnce === "function") return sel.findOnce();
    } catch (e1) {}
    try {
        if (sel && typeof sel.findOne === "function") return sel.findOne(1);
    } catch (e2) {}
    return null;
}

function safeNudgeScreen(force) {
    var now = Date.now();
    if (!TOUCH_NUDGE_ENABLED) return;
    if (!force && now - lastScreenNudge < SCREEN_NUDGE_INTERVAL_MS) return;
    if (!force && now - lastTradeInteractionMs < POST_TRADE_NUDGE_GUARD_MS) return;
    if (tradeUiBlocksNudge()) return;
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

function parseBalanceText(text, requireUsdt) {
    var value = String(text || "").trim();
    if (requireUsdt && !/USDT/i.test(value)) return null;
    var match = value.match(/([0-9]+(?:\.[0-9]+)?)/);
    if (!match) return null;
    var amount = parseFloat(match[1]);
    return !isNaN(amount) && amount >= 0 ? amount : null;
}

function scanBalanceOnce(allowGenericFallback) {
    var balanceIds = ["2131448004", BALANCE_VIEW_ID, PACKAGE + ":id/2131448004"];
    for (var i = 0; i < balanceIds.length; i++) {
        var direct = null;
        try { direct = findOnceSafe(id(balanceIds[i])); } catch (e1) {}
        if (!direct) continue;
        var directText = nodeTextSafe(direct);
        var directAmount = parseBalanceText(directText, false);
        if (directAmount != null) {
            return { amount: directAmount, method: "id:" + balanceIds[i], text: directText };
        }
    }

    var label = null;
    try { label = findOnceSafe(selector().text("\u53ef\u7528")); } catch (e2) {}
    if (label) {
        try {
            var sib = label.nextSibling();
            while (sib) {
                var siblingText = nodeTextSafe(sib);
                var siblingAmount = parseBalanceText(siblingText, true);
                if (siblingAmount != null) {
                    return { amount: siblingAmount, method: "available_sibling", text: siblingText };
                }
                sib = sib.nextSibling();
            }
        } catch (e3) {}
        try {
            var par = label.parent();
            var kids = par ? par.find(selector().textMatches(/USDT/i)) : [];
            for (var k = 0; k < kids.length; k++) {
                var childText = nodeTextSafe(kids[k]);
                var childAmount = parseBalanceText(childText, true);
                if (childAmount != null) {
                    return { amount: childAmount, method: "available_parent", text: childText };
                }
            }
        } catch (e4) {}
    }

    if (allowGenericFallback === false) return null;

    try {
        var allUsdt = selector().textMatches(/^[0-9]+(?:\.[0-9]+)?\s*USDT$/i).find();
        var best = -1;
        var bestText = "";
        for (var j = 0; j < allUsdt.length; j++) {
            var candidateText = nodeTextSafe(allUsdt[j]);
            var candidateAmount = parseBalanceText(candidateText, true);
            if (candidateAmount != null && candidateAmount > best) {
                best = candidateAmount;
                bestText = candidateText;
            }
        }
        if (best >= 0) return { amount: best, method: "largest_usdt", text: bestText };
    } catch (e5) {}
    return null;
}

function scanBalance(timeoutMs, allowGenericFallback) {
    var timeout = timeoutMs == null ? BALANCE_FIND_TIMEOUT_MS : Math.max(0, Number(timeoutMs) || 0);
    var startedAt = Date.now();
    try {
        while (true) {
            var result = scanBalanceOnce(allowGenericFallback);
            if (result) {
                log("[Balance] " + result.method + " text='" + result.text + "' -> " + result.amount + " in " + (Date.now() - startedAt) + "ms");
                return result.amount;
            }
            if (Date.now() - startedAt >= timeout) return null;
            sleep(UI_FAST_POLL_MS);
        }
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
            if (c.strategyAmounts) tradeConfig.strategyAmounts = c.strategyAmounts;
            if (c.strategyVariants) tradeConfig.strategyVariants = c.strategyVariants;
            if (c.duration) tradeConfig.duration = c.duration;
            if (c.autoTrade !== undefined) tradeConfig.autoTrade = c.autoTrade;
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
                if (c.strategyAmounts) tradeConfig.strategyAmounts = c.strategyAmounts;
                if (c.strategyVariants) tradeConfig.strategyVariants = c.strategyVariants;
                if (c.duration) tradeConfig.duration = c.duration;
                if (c.autoTrade !== undefined) tradeConfig.autoTrade = c.autoTrade;
                delete data._config;
            }
            return data;
        }
    } catch (e) {
        log("API err: " + e);
    }
    return null;
}

function amountForOrder(strategyId, sig, serverAmount) {
    if (sig && sig.amount && sig.fixed_amount === true) return String(sig.amount);
    var baseAmount = serverAmount || (tradeConfig.strategyAmounts && tradeConfig.strategyAmounts[strategyId]) || tradeConfig.amount;
    if (sig && sig.amount) return String(sig.amount);
    return String(baseAmount);
}

function buildTradeQueue(data) {
    var queue = [];
    if (!data) return queue;
    var amounts = data._strategyAmounts || {};
    var defs = data._strategyVariants || tradeConfig.strategyVariants || [
        { id: "BTC_10min_TAKER", duration: "10" },
        { id: "BTC_10min_SAFE", duration: "10" }
    ];
    for (var i = 0; i < defs.length; i++) {
        var d = defs[i];
        if (d.enabled === false) continue;
        if (d.tradeEnabled === false) continue;
        var sig = data[d.id];
        if (!sig || !sig.signal) continue;
        var amount = amountForOrder(d.id, sig, amounts[d.id]);
        var exec = buildExecutionContext(sig);
        queue.push({
            strategyId: d.id,
            configIndex: i,
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
            duration: String(sig.duration || d.duration || tradeConfig.duration || "10"),
            raw: sig
        });
    }
    return sortTradeQueue(queue);
}

function sortTradeQueue(queue) {
    queue.sort(function(a, b) {
        var ta = a.actionableTime || a.time || "";
        var tb = b.actionableTime || b.time || "";
        if (ta !== tb) return ta < tb ? -1 : 1;
        var ap = a.strategyId.indexOf("BTC_10min_TAKER") === 0 ? 0 : 1;
        var bp = b.strategyId.indexOf("BTC_10min_TAKER") === 0 ? 0 : 1;
        if (ap !== bp) return ap - bp;
        return Number(a.configIndex || 0) - Number(b.configIndex || 0);
    });
    return queue;
}

function filterConflictQueue(queue) {
    return queue;
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
    // Map duration to row index. Current production strategies use 10 minutes.
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
    var texts = [];
    for (var i = 0; i < rows.length; i++) {
        try { texts.push(rows[i].text ? rows[i].text() : ""); } catch (e) {}
    }
    reportTradeAudit("duration_select_failed", null, {
        targetDuration: target,
        currentText: current,
        rowCount: rows.length,
        rowTexts: texts.join("|")
    });
    return false;
}

// ========== Set amount ==========
function rectNumber(rect, name) {
    try {
        var value = rect && rect[name];
        if (typeof value === "function") value = value.call(rect);
        value = Number(value);
        return isFinite(value) ? value : null;
    } catch (e) {
        return null;
    }
}

function nodeTextSafe(node) {
    try { return String((node.text && node.text()) || ""); } catch (e1) {}
    return "";
}

function nodeDescSafe(node) {
    try { return String((node.desc && node.desc()) || ""); } catch (e1) {}
    return "";
}

function nodeIdSafe(node) {
    try { return String((node.id && node.id()) || ""); } catch (e1) {}
    return "";
}

function nodeClassSafe(node) {
    try { return String((node.className && node.className()) || ""); } catch (e1) {}
    return "";
}

function nodeBoolSafe(node, name) {
    try {
        if (node && typeof node[name] === "function") return node[name]() === true;
    } catch (e) {}
    return null;
}

function nodeBoundsSafe(node) {
    try {
        var b = node.bounds();
        var left = rectNumber(b, "left");
        var top = rectNumber(b, "top");
        var right = rectNumber(b, "right");
        var bottom = rectNumber(b, "bottom");
        if (left === null || top === null || right === null || bottom === null) return null;
        return {
            left: left,
            top: top,
            right: right,
            bottom: bottom,
            width: Math.max(0, right - left),
            height: Math.max(0, bottom - top)
        };
    } catch (e) {
        return null;
    }
}

function amountCandidateInfo(node, source) {
    var bounds = nodeBoundsSafe(node);
    return {
        node: node,
        source: source,
        bounds: bounds,
        text: nodeTextSafe(node),
        desc: nodeDescSafe(node),
        id: nodeIdSafe(node),
        className: nodeClassSafe(node),
        clickable: nodeBoolSafe(node, "clickable"),
        enabled: nodeBoolSafe(node, "enabled"),
        visible: nodeBoolSafe(node, "visibleToUser")
    };
}

function pushAmountCandidate(list, seen, node, source) {
    if (!node) return;
    var info = amountCandidateInfo(node, source);
    if (!info.bounds || info.bounds.width < 20 || info.bounds.height < 20) return;
    var key = [
        info.id,
        info.className,
        info.text,
        info.desc,
        info.bounds.left,
        info.bounds.top,
        info.bounds.right,
        info.bounds.bottom
    ].join("|");
    if (seen[key]) return;
    seen[key] = true;
    list.push(info);
}

function pushAmountCandidateList(list, seen, nodes, source) {
    if (!nodes) return;
    try {
        for (var i = 0; i < nodes.length; i++) pushAmountCandidate(list, seen, nodes[i], source);
    } catch (e) {}
}

function amountCandidateScore(info) {
    var score = 0;
    var idText = String(info.id || "");
    var classText = String(info.className || "");
    var labelText = String((info.text || "") + " " + (info.desc || ""));
    var b = info.bounds || {};
    var cy = (Number(b.top || 0) + Number(b.bottom || 0)) / 2;
    var cx = (Number(b.left || 0) + Number(b.right || 0)) / 2;

    if (info.source === "legacy_id" || info.source === "full_legacy_id") score += 120;
    if (/EditText/i.test(classText)) score += 70;
    if (/2131431885|amount|input|qty|quantity|sum|stake|money|edit/i.test(idText)) score += 60;
    if (/\bUSDT\b|Amount|Stake|Qty|Quantity|Margin|Cost|Buy|Sell/i.test(labelText)) score += 20;
    if (cy > device.height * 0.38 && cy < device.height * 0.88) score += 18;
    if (cx > device.width * 0.35) score += 8;
    if (info.enabled === false) score -= 100;
    if (info.visible === false) score -= 80;
    return score;
}

function collectAmountInputCandidatesOnce() {
    var list = [];
    var seen = {};
    try { pushAmountCandidate(list, seen, findOnceSafe(id("2131431885")), "legacy_id"); } catch (e1) {}
    try { pushAmountCandidate(list, seen, findOnceSafe(id(PACKAGE + ":id/2131431885")), "full_legacy_id"); } catch (e2) {}
    try { pushAmountCandidateList(list, seen, selector().idMatches(/2131431885|amount|input|qty|quantity|sum|stake|money|edit/i).find(), "id_matches"); } catch (e3) {}
    try { pushAmountCandidateList(list, seen, className("android.widget.EditText").find(), "edit_text"); } catch (e4) {}
    try { pushAmountCandidateList(list, seen, classNameMatches(/EditText|TextInput/i).find(), "class_matches"); } catch (e5) {}

    list.sort(function(a, b) {
        return amountCandidateScore(b) - amountCandidateScore(a);
    });
    return list;
}

function findAmountInputCandidates(timeoutMs) {
    var timeout = timeoutMs == null ? AMOUNT_FIND_TIMEOUT_MS : Math.max(0, Number(timeoutMs) || 0);
    var startedAt = Date.now();
    while (true) {
        var list = collectAmountInputCandidatesOnce();
        if (list.length || Date.now() - startedAt >= timeout) return list;
        sleep(UI_FAST_POLL_MS);
    }
}

function compactAmountCandidate(info) {
    return {
        source: info.source,
        score: amountCandidateScore(info),
        id: info.id,
        className: info.className,
        text: String(info.text || "").substring(0, 40),
        desc: String(info.desc || "").substring(0, 40),
        bounds: info.bounds,
        clickable: info.clickable,
        enabled: info.enabled,
        visible: info.visible
    };
}

function setTextOnAmountCandidate(info, amt) {
    var node = info.node;
    var target = String(amt);
    var startedAt = Date.now();
    try { node.click(); } catch (e1) {}
    sleep(UI_FAST_POLL_MS);
    try {
        var directResult = node.setText(target);
        if (directResult === false) throw new Error("node.setText returned false");
        sleep(UI_FAST_POLL_MS);
        if (lastAmountInputProbe) {
            lastAmountInputProbe.setMethod = "node.setText";
            lastAmountInputProbe.setMs = Date.now() - startedAt;
            lastAmountInputProbe.observedText = nodeTextSafe(node);
        }
        log("Amount set by " + info.source + " score=" + amountCandidateScore(info) + " in " + (Date.now() - startedAt) + "ms");
        return true;
    } catch (e2) {
        try {
            var focusedResult = setText(target);
            if (focusedResult === false) throw new Error("focused setText returned false");
            sleep(UI_FAST_POLL_MS);
            if (lastAmountInputProbe) {
                lastAmountInputProbe.setMethod = "focused_setText";
                lastAmountInputProbe.setMs = Date.now() - startedAt;
                lastAmountInputProbe.observedText = nodeTextSafe(node);
            }
            log("Amount set by focused setText " + info.source + " score=" + amountCandidateScore(info) + " in " + (Date.now() - startedAt) + "ms");
            return true;
        } catch (e3) {
            if (lastAmountInputProbe && lastAmountInputProbe.errors.length < 6) {
                lastAmountInputProbe.errors.push({
                    source: info.source,
                    error: String(e2) + " | " + String(e3)
                });
            }
        }
    }
    return false;
}

function setAmount(amt) {
    var findStartedAt = Date.now();
    lastAmountInputProbe = {
        target: String(amt),
        screen: { width: device.width, height: device.height },
        candidates: [],
        tried: [],
        errors: []
    };

    var candidates = findAmountInputCandidates(AMOUNT_FIND_TIMEOUT_MS);
    lastAmountInputProbe.findMs = Date.now() - findStartedAt;
    for (var i = 0; i < candidates.length && i < 10; i++) {
        lastAmountInputProbe.candidates.push(compactAmountCandidate(candidates[i]));
    }
    if (!candidates.length) {
        log("Amount input not found: no candidates");
        return false;
    }

    for (var j = 0; j < candidates.length && j < 6; j++) {
        var info = candidates[j];
        lastAmountInputProbe.tried.push(compactAmountCandidate(info));
        if (setTextOnAmountCandidate(info, amt)) return true;
    }
    log("Amount input not set after " + Math.min(candidates.length, 6) + " candidates");
    return false;
}

function dismissAmountKeyboard() {
    var startedAt = Date.now();
    try {
        var input = findOnceSafe(className("android.widget.EditText"));
        if (!input) {
            try { input = findOnceSafe(selector().editable(true).focused(true)); } catch (e1) {}
        }
        if (!input || input.focused() !== true) return false;
        back();
        while (Date.now() - startedAt < 400) {
            sleep(UI_FAST_POLL_MS);
            try {
                if (input.focused() !== true) break;
            } catch (e2) { break; }
        }
        if (lastAmountInputProbe) lastAmountInputProbe.keyboardDismissMs = Date.now() - startedAt;
        log("Amount keyboard dismissed before direction click in " + (Date.now() - startedAt) + "ms");
        return true;
    } catch (e) {
        log("Amount keyboard dismiss err: " + e);
        return false;
    }
}

// ========== Direction ==========
function directionClickableAncestor(node, expectedParentId, direction) {
    var cur = node;
    for (var depth = 0; cur && depth < 5; depth++) {
        var summary = nodeSummary(cur);
        var idValue = summary.id || "";
        var bounds = summary.bounds;
        var parentMatches = idValue === expectedParentId || idValue === PACKAGE + ":id/" + expectedParentId;
        var sideMatches = bounds && (direction === "UP"
            ? bounds.right <= Math.floor(device.width / 2)
            : bounds.left >= Math.floor(device.width / 2));
        if (parentMatches && sideMatches && summary.clickable === true && summary.enabled !== false && summary.visible !== false) {
            return { node: cur, summary: summary, depth: depth };
        }
        try { cur = cur.parent(); } catch (e) { cur = null; }
    }
    return null;
}

function clickDirectionFast(direction, labelPattern, expectedTextId, expectedParentId) {
    var startedAt = Date.now();
    while (Date.now() - startedAt < DIRECTION_FIND_TIMEOUT_MS) {
        var nodes = [];
        try { nodes = selector().textMatches(labelPattern).find(); } catch (e1) {}
        for (var i = 0; i < nodes.length; i++) {
            var textSummary = nodeSummary(nodes[i]);
            var textIdMatches = textSummary.id === expectedTextId || textSummary.id === PACKAGE + ":id/" + expectedTextId;
            if (!textIdMatches || textSummary.visible === false || textSummary.enabled === false) continue;
            var target = directionClickableAncestor(nodes[i], expectedParentId, direction);
            if (!target) continue;
            var dispatched = false;
            try { dispatched = target.node.click() === true; } catch (e2) {}
            lastDirectionProbe = {
                direction: direction,
                method: "verified_text_parent",
                labelNode: textSummary,
                parentNode: target.summary,
                ancestorDepth: target.depth,
                dispatched: dispatched,
                waitedMs: Date.now() - startedAt,
                clickedAt: dispatched ? Date.now() : null
            };
            if (dispatched) return true;
        }
        sleep(UI_FAST_POLL_MS);
    }
    lastDirectionProbe = {
        direction: direction,
        method: "verified_node_not_found",
        expectedTextId: expectedTextId,
        expectedParentId: expectedParentId,
        waitedMs: Date.now() - startedAt,
        clickedAt: null
    };
    return false;
}

function clickUp() {
    return clickDirectionFast("UP", /^(上涨|看涨|买涨|UP)$/i, "2131452723", "2131432869");
}
function clickDown() {
    return clickDirectionFast("DOWN", /^(下跌|看跌|买跌|DOWN)$/i, "2131452729", "2131432870");
}

// ========== Confirm ==========
function handleConfirm(direction) {
    return handleConfirmStrict(direction);
}

function nodeSummary(node) {
    var b = nodeBoundsSafe(node);
    return {
        text: nodeTextSafe(node),
        desc: nodeDescSafe(node),
        id: nodeIdSafe(node),
        className: nodeClassSafe(node),
        clickable: nodeBoolSafe(node, "clickable"),
        enabled: nodeBoolSafe(node, "enabled"),
        visible: nodeBoolSafe(node, "visibleToUser"),
        bounds: b
    };
}

function clickNodeCenter(node) {
    lastNodeClickProbe = null;
    var b = nodeBoundsSafe(node);
    if (b && b.width > 0 && b.height > 0) {
        var x = Math.round((b.left + b.right) / 2);
        var y = Math.round((b.top + b.bottom) / 2);
        if (x >= 0 && x < device.width && y >= 0 && y < device.height) {
            var coordinateResult = false;
            try { coordinateResult = click(x, y); } catch (e1) {}
            lastNodeClickProbe = {
                method: "screen_coordinate",
                dispatched: coordinateResult === true,
                x: x,
                y: y,
                bounds: b
            };
            return coordinateResult === true;
        }
        lastNodeClickProbe = {
            method: "coordinate_rejected",
            dispatched: false,
            reason: "bounds_outside_screen",
            screen: { width: device.width, height: device.height },
            bounds: b
        };
    }
    try {
        var nodeResult = node.click();
        lastNodeClickProbe = {
            method: "node_accessibility_fallback",
            dispatched: nodeResult === true,
            bounds: b
        };
        return nodeResult === true;
    } catch (e2) {}
    return false;
}

function clickNodeOrAncestor(node) {
    lastNodeClickProbe = null;
    var cur = node;
    for (var i = 0; cur && i < 5; i++) {
        try {
            if (nodeBoolSafe(cur, "enabled") !== false && nodeBoolSafe(cur, "visibleToUser") !== false) {
                if (nodeBoolSafe(cur, "clickable") === true) {
                    var actionResult = cur.click();
                    lastNodeClickProbe = {
                        method: i === 0 ? "node_accessibility" : "ancestor_accessibility",
                        dispatched: actionResult === true,
                        ancestorDepth: i,
                        node: nodeSummary(cur)
                    };
                    if (actionResult === true) return true;
                }
            }
        } catch (e) {}
        try { cur = cur.parent(); } catch (e2) { cur = null; }
    }
    return clickNodeCenter(node);
}

function nodeCenterInsideScreen(node) {
    var b = nodeBoundsSafe(node);
    if (!b || b.width <= 0 || b.height <= 0) return false;
    var x = Math.round((b.left + b.right) / 2);
    var y = Math.round((b.top + b.bottom) / 2);
    return x >= 0 && x < device.width && y >= 0 && y < device.height;
}

function confirmNodeReady(node) {
    if (!node || !nodeCenterInsideScreen(node)) return false;
    if (nodeBoolSafe(node, "enabled") === false) return false;
    if (nodeBoolSafe(node, "visibleToUser") === false) return false;
    return true;
}

function collectConfirmProbe(limit) {
    var out = [];
    var seen = {};
    function push(node) {
        if (!node || out.length >= limit) return;
        var b = nodeBoundsSafe(node);
        var key = nodeIdSafe(node) + "|" + nodeTextSafe(node) + "|" + nodeDescSafe(node) + "|" + (b ? [b.left, b.top, b.right, b.bottom].join(",") : "");
        if (seen[key]) return;
        seen[key] = true;
        out.push(nodeSummary(node));
    }
    try {
        var matchedText = selector().textMatches(/.*Confirm.*|.*Submit.*|.*Place.*|.*Order.*|.*Buy.*|.*Sell.*|.*\u786e\u8ba4.*|.*\u786e\u5b9a.*|.*\u4e0b\u5355.*|.*\u4e70\u5165.*|.*\u5356\u51fa.*|.*\u8d2d\u4e70.*/i).find();
        for (var i = 0; i < matchedText.length; i++) push(matchedText[i]);
    } catch (e1) {}
    try {
        var matchedDesc = selector().descMatches(/.*Confirm.*|.*Submit.*|.*Place.*|.*Order.*|.*Buy.*|.*Sell.*|.*\u786e\u8ba4.*|.*\u786e\u5b9a.*|.*\u4e0b\u5355.*|.*\u4e70\u5165.*|.*\u5356\u51fa.*|.*\u8d2d\u4e70.*/i).find();
        for (var j = 0; j < matchedDesc.length; j++) push(matchedDesc[j]);
    } catch (e2) {}
    return out;
}

function collectExpectedDirectionEvidence(direction) {
    var expectedPattern = direction === "UP" ? /^(上涨|看涨|买涨|UP)$/i : /^(下跌|看跌|买跌|DOWN)$/i;
    var oppositePattern = direction === "UP" ? /^(下跌|看跌|买跌|DOWN)$/i : /^(上涨|看涨|买涨|UP)$/i;
    var expected = [];
    var opposite = [];
    function collect(pattern, target) {
        var nodes = [];
        try { nodes = selector().textMatches(pattern).find(); } catch (e) {}
        for (var i = 0; i < nodes.length; i++) {
            var summary = nodeSummary(nodes[i]);
            if (summary.visible === false || !summary.bounds) continue;
            // 原下单按钮位于屏幕中段；确认弹窗方向证据必须来自其他区域。
            if (summary.bounds.top >= Math.floor(device.height * 0.48) && summary.bounds.bottom <= Math.floor(device.height * 0.58)) continue;
            target.push(summary);
        }
    }
    collect(expectedPattern, expected);
    collect(oppositePattern, opposite);
    return { direction: direction, expected: expected, opposite: opposite, verified: expected.length === 1 && opposite.length === 0 };
}

function handleConfirmStrict(direction) {
    var start = Date.now();
    var probe = [];
    lastConfirmProbe = null;
    sleep(CONFIRM_MIN_SETTLE_MS);
    while (Date.now() - start < CONFIRM_FIND_TIMEOUT_MS) {
        var directionEvidence = collectExpectedDirectionEvidence(direction);
        if (!directionEvidence.verified) {
            lastConfirmProbe = {
                method: "direction_verification_pending",
                direction: direction,
                directionEvidence: directionEvidence,
                waitedMs: Date.now() - start
            };
            sleep(UI_FAST_POLL_MS);
            continue;
        }
        var btn = null;
        var matchedId = null;
        for (var idIndex = 0; idIndex < CONFIRM_BUTTON_IDS.length && !btn; idIndex++) {
            var rawId = CONFIRM_BUTTON_IDS[idIndex];
            try { btn = findOnceSafe(id(rawId)); } catch (e0) {}
            if (btn) matchedId = rawId;
            if (!btn) {
                try { btn = findOnceSafe(id(PACKAGE + ":id/" + rawId)); } catch (e00) {}
                if (btn) matchedId = PACKAGE + ":id/" + rawId;
            }
        }
        if (btn) {
            var idSummary = nodeSummary(btn);
            idSummary.confirmReady = confirmNodeReady(btn);
            probe.push(idSummary);
            if (idSummary.confirmReady && clickNodeOrAncestor(btn)) {
                lastConfirmProbe = {
                    method: "id",
                    matchedId: matchedId,
                    directionEvidence: directionEvidence,
                    dispatch: lastNodeClickProbe,
                    node: idSummary,
                    waitedMs: Date.now() - start,
                    dispatchedAt: Date.now()
                };
                log("Confirm action dispatched by id/accessibility in " + (Date.now() - start) + "ms");
                return true;
            }
        }
        var btns = selector().textMatches(CONFIRM_TEXT_PATTERN).clickable(true).find();
        for (var i = 0; i < btns.length; i++) {
            var textSummary = nodeSummary(btns[i]);
            textSummary.confirmReady = confirmNodeReady(btns[i]);
            probe.push(textSummary);
            if (textSummary.confirmReady && clickNodeOrAncestor(btns[i])) {
                lastConfirmProbe = {
                    method: "clickable_text",
                    directionEvidence: directionEvidence,
                    dispatch: lastNodeClickProbe,
                    node: textSummary,
                    waitedMs: Date.now() - start,
                    dispatchedAt: Date.now()
                };
                log("Confirm action dispatched by text/accessibility in " + (Date.now() - start) + "ms");
                return true;
            }
        }
        var textNodes = selector().textMatches(CONFIRM_TEXT_PATTERN).find();
        for (var j = 0; j < textNodes.length; j++) {
            var ancestorTextSummary = nodeSummary(textNodes[j]);
            ancestorTextSummary.confirmReady = confirmNodeReady(textNodes[j]);
            probe.push(ancestorTextSummary);
            if (ancestorTextSummary.confirmReady && clickNodeOrAncestor(textNodes[j])) {
                lastConfirmProbe = {
                    method: "text_ancestor",
                    directionEvidence: directionEvidence,
                    dispatch: lastNodeClickProbe,
                    node: ancestorTextSummary,
                    waitedMs: Date.now() - start,
                    dispatchedAt: Date.now()
                };
                log("Confirm action dispatched by text ancestor in " + (Date.now() - start) + "ms");
                return true;
            }
        }
        var descNodes = selector().descMatches(CONFIRM_TEXT_PATTERN).find();
        for (var k = 0; k < descNodes.length; k++) {
            var descSummary = nodeSummary(descNodes[k]);
            descSummary.confirmReady = confirmNodeReady(descNodes[k]);
            probe.push(descSummary);
            if (descSummary.confirmReady && clickNodeOrAncestor(descNodes[k])) {
                lastConfirmProbe = {
                    method: "desc_ancestor",
                    directionEvidence: directionEvidence,
                    dispatch: lastNodeClickProbe,
                    node: descSummary,
                    waitedMs: Date.now() - start,
                    dispatchedAt: Date.now()
                };
                log("Confirm action dispatched by desc ancestor in " + (Date.now() - start) + "ms");
                return true;
            }
        }
        sleep(UI_FAST_POLL_MS);
    }
    log("Confirm button or verified direction not found");
    var finalDirectionEvidence = collectExpectedDirectionEvidence(direction);
    lastConfirmProbe = {
        method: finalDirectionEvidence.verified ? "confirm_not_found" : "direction_not_verified",
        direction: direction,
        directionEvidence: finalDirectionEvidence,
        waitedMs: Date.now() - start,
        candidates: probe.length ? probe.slice(0, 12) : collectConfirmProbe(12)
    };
    return false;
}

function findConfirmationNode() {
    for (var i = 0; i < CONFIRM_BUTTON_IDS.length; i++) {
        var rawId = CONFIRM_BUTTON_IDS[i];
        var node = null;
        try { node = findOnceSafe(id(rawId)); } catch (e1) {}
        if (!node) {
            try { node = findOnceSafe(id(PACKAGE + ":id/" + rawId)); } catch (e2) {}
        }
        if (node) return node;
    }
    try { return findOnceSafe(selector().textMatches(CONFIRM_TEXT_PATTERN)); } catch (e3) {}
    return null;
}

function recoverTradeUiAfterFailure() {
    try {
        var trigger = findConfirmationNode();
        if (trigger) {
            var summary = nodeSummary(trigger);
            back();
            sleep(500);
            log("Recovered stale trade confirmation with back");
            return { action: "back", reason: "confirmation_still_open", trigger: summary };
        }
        var input = className("android.widget.EditText").findOne(200);
        if (input && input.focused() === true) {
            back();
            sleep(400);
            log("Recovered focused amount input with back");
            return { action: "back", reason: "amount_input_still_focused", trigger: nodeSummary(input) };
        }
    } catch (e) {
        return { action: "none", reason: "recovery_error", error: String(e) };
    }
    return { action: "none", reason: "no_blocking_trade_ui_found" };
}

function closeConfirmationAfterVerifiedOrder() {
    try {
        var trigger = findConfirmationNode();
        if (!trigger) return { action: "none", reason: "confirmation_closed" };
        var summary = nodeSummary(trigger);
        back();
        sleep(300);
        return { action: "back", reason: "verified_order_confirmation_still_open", trigger: summary };
    } catch (e) {
        return { action: "none", reason: "verified_cleanup_error", error: String(e) };
    }
}

function handleConfirmLegacy() {
    sleep(1500);
    var btn = id("2131448374").findOne(3000);
    if (btn) { btn.click(); log("Confirmed"); return; }
    var btns = selector().textMatches(".*确认.*|.*确定.*").clickable(true).find();
    for (var i = 0; i < btns.length; i++) { btns[i].click(); return; }
}

function readBalanceForOrder(label) {
    var amt = scanBalance(BALANCE_FIND_TIMEOUT_MS, false);
    if (amt == null || isNaN(amt)) {
        log("[OrderVerify] " + label + " balance unavailable");
        return null;
    }
    log("[OrderVerify] " + label + " balance=" + amt);
    return Number(amt);
}

function balanceDropMatches(beforeBalance, afterBalance, amount) {
    var stake = Number(amount);
    if (!isFinite(stake) || stake <= 0) return false;
    if (!isFinite(beforeBalance) || !isFinite(afterBalance)) return false;
    var drop = beforeBalance - afterBalance;
    var tolerance = Math.max(ORDER_BALANCE_TOLERANCE_USDT, stake * 0.03);
    return drop >= stake - tolerance && drop <= stake + Math.max(1.0, stake * 0.12);
}

function waitForBalanceDrop(beforeBalance, amount) {
    var start = Date.now();
    var latest = null;
    var attempts = 0;
    while (Date.now() - start < ORDER_VERIFY_TIMEOUT_MS) {
        sleep(ORDER_VERIFY_POLL_MS);
        attempts++;
        latest = readBalanceForOrder("after");
        if (latest != null && balanceDropMatches(beforeBalance, latest, amount)) {
            return {
                ok: true,
                beforeBalance: beforeBalance,
                afterBalance: latest,
                balanceDelta: Number((latest - beforeBalance).toFixed(4)),
                waitedMs: Date.now() - start,
                attempts: attempts
            };
        }
    }
    return {
        ok: false,
        beforeBalance: beforeBalance,
        afterBalance: latest,
        balanceDelta: latest == null ? null : Number((latest - beforeBalance).toFixed(4)),
        waitedMs: Date.now() - start,
        attempts: attempts
    };
}

function hasSufficientBalanceForOrder(balance, amount) {
    var available = Number(balance);
    var stake = Number(amount);
    if (!isFinite(available) || !isFinite(stake) || stake <= 0) return false;
    return available + 0.000001 >= stake;
}

function markTradePhase(timing, name, extra) {
    if (!timing) return;
    var at = Date.now();
    var phase = { at: at, elapsedMs: at - timing.startedAt };
    if (extra) {
        for (var key in extra) phase[key] = extra[key];
    }
    timing.phases[name] = phase;
}

function tradeTimingSnapshot(timing) {
    if (!timing) return null;
    return {
        startedAt: timing.startedAt,
        elapsedMs: Date.now() - timing.startedAt,
        phases: timing.phases
    };
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
    lastTradeInteractionMs = Date.now();
    tradeInteractionActive = true;
    var tradeStartedAt = lastTradeInteractionMs;
    var tradeTiming = { startedAt: tradeStartedAt, phases: {} };
    lastDirectionProbe = null;
    var prevAmt = tradeConfig.amount;
    var prevDur = tradeConfig.duration;
    try {
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
    markTradePhase(tradeTiming, "order_attempt_reported");
    
    // STEP 1: ensure screen is on (clicks won't work otherwise)
    if (!forceWakeForTrade()) {
        log(">>> ABORT: cannot wake screen");
        reportTradeAudit("order_abort", order, { direction: dir, reason: "cannot_wake_screen" });
        tradeConfig.amount = prevAmt;
        tradeConfig.duration = prevDur;
        return false;
    }
    markTradePhase(tradeTiming, "screen_ready");
    
    // STEP 2: ensure Binance foreground
    if (currentPackage() != PACKAGE) {
        log("step2: launching " + PACKAGE);
        app.launch(PACKAGE); sleep(3000);
    } else {
        log("step2: binance already foreground");
    }
    markTradePhase(tradeTiming, "app_foreground_ready");
    
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
    markTradePhase(tradeTiming, "duration_ready");
    
    // STEP 4: set amount
    log("step4: setAmount " + tradeConfig.amount);
    if (!setAmount(tradeConfig.amount)) {
        log(">>> ABORT: amount failed");
        reportTradeAudit("order_abort", order, {
            direction: dir,
            reason: "amount_failed",
            amountInputProbe: lastAmountInputProbe
        });
        tradeConfig.amount = prevAmt;
        tradeConfig.duration = prevDur;
        return false;
    }
    markTradePhase(tradeTiming, "amount_ready", {
        findMs: lastAmountInputProbe ? lastAmountInputProbe.findMs : null,
        setMs: lastAmountInputProbe ? lastAmountInputProbe.setMs : null,
        source: lastAmountInputProbe && lastAmountInputProbe.tried.length ? lastAmountInputProbe.tried[0].source : null
    });
    var keyboardDismissed = dismissAmountKeyboard();
    if (lastAmountInputProbe) lastAmountInputProbe.keyboardDismissed = keyboardDismissed;
    markTradePhase(tradeTiming, "keyboard_ready", {
        dismissed: keyboardDismissed,
        dismissMs: lastAmountInputProbe ? lastAmountInputProbe.keyboardDismissMs : null
    });

    var beforeBalance = readBalanceForOrder("before");
    if (beforeBalance == null) {
        log(">>> ABORT: balance before click unavailable");
        reportTradeAudit("order_abort", order, { direction: dir, reason: "balance_before_unavailable" });
        tradeConfig.amount = prevAmt;
        tradeConfig.duration = prevDur;
        return false;
    }
    markTradePhase(tradeTiming, "balance_before_ready", { balance: beforeBalance });

    if (!hasSufficientBalanceForOrder(beforeBalance, tradeConfig.amount)) {
        log(">>> ABORT: insufficient balance " + beforeBalance + " < " + tradeConfig.amount);
        reportTradeAudit("order_abort", order, {
            direction: dir,
            reason: "insufficient_balance",
            beforeBalance: beforeBalance,
            requiredAmount: Number(tradeConfig.amount)
        });
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
    var directionClicked = dir == "UP" ? clickUp() : clickDown();
    if (!directionClicked) {
        markTradePhase(tradeTiming, "direction_failed", {
            method: lastDirectionProbe ? lastDirectionProbe.method : null,
            locatorWaitMs: lastDirectionProbe ? lastDirectionProbe.waitedMs : null
        });
        log(">>> ABORT: verified direction node not found");
        reportTradeAudit("order_abort", order, {
            direction: dir,
            reason: "direction_not_verified",
            beforeBalance: beforeBalance,
            directionProbe: lastDirectionProbe,
            uiTiming: tradeTimingSnapshot(tradeTiming)
        });
        tradeConfig.amount = prevAmt;
        tradeConfig.duration = prevDur;
        return false;
    }
    markTradePhase(tradeTiming, "direction_clicked", {
        method: lastDirectionProbe ? lastDirectionProbe.method : null,
        locatorWaitMs: lastDirectionProbe ? lastDirectionProbe.waitedMs : null
    });
    
    // STEP 6: confirm
    log("step6: confirm");
    var executionTime = lastDirectionProbe && lastDirectionProbe.clickedAt ? lastDirectionProbe.clickedAt : Date.now();
    if (!handleConfirm(dir)) {
        markTradePhase(tradeTiming, "confirm_failed", {
            confirmWaitMs: lastConfirmProbe ? lastConfirmProbe.waitedMs : null
        });
        log(">>> ABORT: confirm failed");
        var confirmRecovery = recoverTradeUiAfterFailure();
        reportTradeAudit("order_abort", order, {
            direction: dir,
            reason: "confirm_not_found",
            beforeBalance: beforeBalance,
            executionTime: executionTime,
            directionProbe: lastDirectionProbe,
            confirmProbe: lastConfirmProbe,
            uiRecovery: confirmRecovery,
            uiTiming: tradeTimingSnapshot(tradeTiming)
        });
        tradeConfig.amount = prevAmt;
        tradeConfig.duration = prevDur;
        return false;
    }
    markTradePhase(tradeTiming, "confirm_action_dispatched", {
        method: lastConfirmProbe ? lastConfirmProbe.method : null,
        confirmWaitMs: lastConfirmProbe ? lastConfirmProbe.waitedMs : null
    });
    reportTradeAudit("order_confirm_dispatched", order, {
        direction: dir,
        confirmationState: "action_dispatched_verification_pending",
        beforeBalance: beforeBalance,
        executionTime: executionTime,
        directionProbe: lastDirectionProbe,
        confirmProbe: lastConfirmProbe,
        uiTiming: tradeTimingSnapshot(tradeTiming)
    });

    var verify = waitForBalanceDrop(beforeBalance, tradeConfig.amount);
    markTradePhase(tradeTiming, "balance_verify_finished", {
        ok: verify.ok,
        waitedMs: verify.waitedMs,
        attempts: verify.attempts
    });
    if (!verify.ok) {
        log(">>> ORDER UNVERIFIED balanceDelta=" + verify.balanceDelta);
        var unverifiedRecovery = recoverTradeUiAfterFailure();
        reportTradeAudit("order_unverified", order, {
            direction: dir,
            reason: "balance_not_decreased",
            beforeBalance: verify.beforeBalance,
            afterBalance: verify.afterBalance,
            balanceDelta: verify.balanceDelta,
            verifyWaitMs: verify.waitedMs,
            verifyAttempts: verify.attempts,
            executionTime: executionTime,
            amountInputProbe: lastAmountInputProbe,
            directionProbe: lastDirectionProbe,
            confirmProbe: lastConfirmProbe,
            uiRecovery: unverifiedRecovery,
            uiTiming: tradeTimingSnapshot(tradeTiming)
        });
        reportBalance();
        if (order) {
            tradeConfig.amount = prevAmt;
            tradeConfig.duration = prevDur;
        }
        return "unverified";
    }

    log(">>> ORDER DONE verified balanceDelta=" + verify.balanceDelta);
    var verifiedUiCleanup = closeConfirmationAfterVerifiedOrder();
    reportBalance();
    reportTradeAudit("order_done", order, {
        direction: dir,
        verifiedBy: "balance_decrease",
        beforeBalance: verify.beforeBalance,
        afterBalance: verify.afterBalance,
        balanceDelta: verify.balanceDelta,
        verifyWaitMs: verify.waitedMs,
        verifyAttempts: verify.attempts,
        executionTime: executionTime,
        amountInputProbe: lastAmountInputProbe,
        directionProbe: lastDirectionProbe,
        confirmProbe: lastConfirmProbe,
        uiCleanup: verifiedUiCleanup,
        uiTiming: tradeTimingSnapshot(tradeTiming)
    });
    
    // STEP 7: refresh balance
    log("step7: balance verified");

    if (order) {
        tradeConfig.amount = prevAmt;
        tradeConfig.duration = prevDur;
    }
    return true;
    } finally {
        tradeInteractionActive = false;
        lastTradeInteractionMs = Date.now();
        if (order) {
            tradeConfig.amount = prevAmt;
            tradeConfig.duration = prevDur;
        }
        log("trade interaction released after " + (lastTradeInteractionMs - tradeStartedAt) + "ms");
    }
}

// ========== Main ==========
// Check for manual trade command from web
function checkManualTrade() {
    try {
        var r = http.get(authUrl(MANUAL_URL), { timeout: 3000 });
        if (r.statusCode == 200) {
            var cmd = r.body.json();
            if (cmd && cmd.direction && Date.now() - cmd.time < 30000) {
                durationSet = false;
                durationSetTarget = "";
                log("MANUAL: " + cmd.direction + " " + (cmd.amount || tradeConfig.amount) + "U x " + (cmd.duration || tradeConfig.duration) + "min");
                placeTrade(cmd.direction, cmd);
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
    activeUntilByStrategy[order.strategyId] = Date.now() + Math.max(STRATEGY_COOLDOWN_MS, dur * 60 * 1000);
    saveOrderHistory();
}

function isStrategyOverlapping(order) {
    if (!order || !order.strategyId) return false;
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
        screenOn: (function(){ try { return device.isScreenOn(); } catch(e) { return null; } })(),
        keepAlive: readKeepAliveStatus()
    });
    log("[Audit] start event " + (auditOk ? "posted" : "FAILED") + " -> " + AUDIT_URL);
    var keepAliveOk = keepScreenAlwaysOn();
    reportKeepAliveStatus("autojs_keepalive_status", { phase: "after_start", keepScreenAlwaysOnOk: keepAliveOk });
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
            if (checkManualTrade()) { sleep(POLL_INTERVAL); continue; }
            
            var payload = getSignalPayload();
            reportHeartbeat(payload);
            var queue = buildTradeQueue(payload);
            queue = filterConflictQueue(queue);
            var hasTradeableOrder = false;
            for (var nq = 0; nq < queue.length; nq++) {
                if (queue[nq] && queue[nq].signal) {
                    hasTradeableOrder = true;
                    break;
                }
            }
            if (!hasTradeableOrder) safeNudgeScreen(false);
            
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
                    order.queueOrderPolicy = "actionable_time_config_order";
                    order.queueBatchStartClientTime = queueBatchStart;
                    order.previousOrderDoneClientTime = previousDoneAt || null;
                    order.sincePreviousDoneMs = previousDoneAt ? Date.now() - previousDoneAt : null;
                    order.sinceQueueBatchStartMs = Date.now() - queueBatchStart;
                    var timing = updateOrderTimingAge(order);
                    if (!timing.ok && timing.reason == "signal_time_parse_failed") {
                        reportTradeAudit("signal_skipped", order, { reason: "signal_time_parse_failed", actionableTime: order.actionableTime });
                        continue;
                    }
                    if (!timing.ok) {
                        reportTradeAudit("signal_skipped", order, { reason: timing.reason, ageMs: timing.ageMs, maxActionableLagMs: timing.lagMs });
                        continue;
                    }
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
                    log("SIGNAL " + order.strategyId + ": " + order.signal + " " + order.confidence + "% RSI=" + order.rsi_value + " amt=" + order.amount + "U dur=" + order.duration + "min");
                    reportTradeAudit("signal_tradeable", order, {});
                    // Persist before any UI interaction. A failed/uncertain attempt must not
                    // dispatch the same signal again on the next one-second poll or restart.
                    lastSignalKeyByStrategy[order.strategyId] = key;
                    lastTradeTimeByStrategy[order.strategyId] = now;
                    rememberPersistedOrder(order);
                    lastTradeInteractionMs = Date.now();
                    var ok = placeTrade(order.signal, order);
                    if (ok === true) {
                        previousDoneAt = Date.now();
                        setStrategyActiveUntil(order);
                        lastTradeTime = now;
                        lastDirection = order.signal;
                        traded = true;
                    } else if (ok === "unverified") {
                        // 确认动作可能已成功发送，本地也按完整订单周期锁定，服务端结算门禁仍是最终依据。
                        setStrategyActiveUntil(order);
                        traded = true;
                    }
                    if (ok === true && qi < queue.length - 1) {
                        log("queue: wait before next strategy order");
                        sleep(5000);
                    }
                }
            if (Date.now() - lastWakeLock > 60000) {
                try { device.keepScreenOn(); } catch (e) {}
                lastWakeLock = Date.now();
            }
                if (!traded && Date.now() - lastLogTime > 15000) {
                    var parts = [];
                    var variants = (payload && payload._strategyVariants) || tradeConfig.strategyVariants || [];
                    for (var vi = 0; vi < variants.length; vi++) {
                        var item = variants[vi];
                        var sig = payload && payload[item.id] ? payload[item.id] : null;
                        parts.push(item.id + "=" + (sig && sig.signal ? sig.signal : "--"));
                    }
                    log("Beat | " + parts.join(" | ") + " | bal=" + (lastBalanceValue || "--"));
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
            reportTradeAudit("runtime_loop_error", null, {
                reason: String(e),
                stack: e && e.stack ? String(e.stack).substring(0, 500) : null
            });
        }
        sleep(POLL_INTERVAL);
    }
}

events.on("exit", function() { releaseLock(); log("Stopped"); });
main();
