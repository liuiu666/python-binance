const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const assert = require("node:assert/strict");

const script = fs.readFileSync(path.join(__dirname, "..", "auto_btc.js"), "utf8");

function functionBody(name, nextName) {
  const start = script.indexOf(`function ${name}(`);
  const end = script.indexOf(`function ${nextName}(`, start + 1);
  assert.notEqual(start, -1, `${name} should exist`);
  assert.notEqual(end, -1, `${nextName} should exist after ${name}`);
  return script.slice(start, end);
}

test("tablet low-latency script keeps one-second polling and guarded touch keepalive", () => {
  assert.match(script, /SCRIPT_VERSION = "2026-08-05-llm-3min-live"/);
  assert.match(script, /POLL_INTERVAL = 1000/);
  assert.match(script, /SIGNAL_MAX_AGE_MS = 3 \* 60 \* 1000/);
  assert.match(script, /TOUCH_NUDGE_ENABLED = true/);
  assert.match(script, /POST_TRADE_NUDGE_GUARD_MS = 120000/);

  const nudge = functionBody("safeNudgeScreen", "debugDumpUI");
  assert.ok(nudge.indexOf("SCREEN_NUDGE_INTERVAL_MS") < nudge.indexOf("tradeUiBlocksNudge()"));
});

test("live order guards enforce actionable time, sufficient balance, and at-most-once dispatch", () => {
  const timing = functionBody("updateOrderTimingAge", "reportHeartbeat");
  const placeTrade = functionBody("placeTrade", "checkManualTrade");

  assert.match(timing, /localActionableAgeMs < 0/);
  assert.match(timing, /actionable_time_not_reached/);
  assert.doesNotMatch(script, /actionableMs > Date\.now\(\) \+ 30000/);
  assert.match(placeTrade, /hasSufficientBalanceForOrder\(beforeBalance, tradeConfig\.amount\)/);
  assert.match(placeTrade, /reason: "insufficient_balance"/);

  const persistAt = script.indexOf("rememberPersistedOrder(order);", script.indexOf('reportTradeAudit("signal_tradeable"'));
  const dispatchAt = script.indexOf("placeTrade(order.signal, order);", persistAt);
  assert.ok(persistAt > 0 && persistAt < dispatchAt, "signal must be persisted before UI dispatch");
});

test("tablet critical UI locators avoid serialized long waits", () => {
  const balance = functionBody("scanBalance", "reportBalance");
  const amount = functionBody("collectAmountInputCandidatesOnce", "findAmountInputCandidates");
  const confirm = functionBody("handleConfirmStrict", "recoverTradeUiAfterFailure");

  assert.doesNotMatch(balance, /findOne\(1500\)/);
  assert.doesNotMatch(amount, /findOne\(700\)/);
  assert.doesNotMatch(confirm, /findOne\(800\)/);
  assert.match(confirm, /UI_FAST_POLL_MS/);
});

test("tablet confirmation and queue guards cannot treat generic trade controls as confirmation", () => {
  const patternLine = script.split(/\r?\n/).find(line => line.includes("var CONFIRM_TEXT_PATTERN"));
  assert.ok(patternLine);
  assert.doesNotMatch(patternLine, /Buy\.\*/i);
  assert.doesNotMatch(patternLine, /Sell\.\*/i);
  assert.doesNotMatch(patternLine, /\|\.\*Order\.\*/i);
  assert.match(script, /if \(!sig \|\| !sig\.signal\) continue;/);
  assert.match(script, /tradeInteractionActive = true;/);
  assert.match(script, /tradeInteractionActive = false;/);
});

test("tablet confirmation dispatch uses the current button id and accessibility action", () => {
  assert.match(script, /CONFIRM_BUTTON_IDS = \["2131448753", "2131448374"\]/);
  assert.match(script, /CONFIRM_MIN_SETTLE_MS = 700/);

  const clickNode = functionBody("clickNodeOrAncestor", "collectConfirmProbe");
  assert.ok(clickNode.indexOf("cur.click()") < clickNode.indexOf("clickNodeCenter(node)"));
  assert.match(clickNode, /actionResult === true/);

  const center = functionBody("clickNodeCenter", "clickNodeOrAncestor");
  assert.match(center, /x < device\.width/);
  assert.match(center, /y < device\.height/);
  assert.match(center, /bounds_outside_screen/);

  const confirm = functionBody("handleConfirmStrict", "findConfirmationNode");
  assert.ok(confirm.indexOf("sleep(CONFIRM_MIN_SETTLE_MS)") < confirm.indexOf("CONFIRM_BUTTON_IDS"));
  assert.match(confirm, /confirmNodeReady\(btn\)/);
  assert.match(confirm, /confirmReady && clickNodeOrAncestor/);

  const ready = functionBody("nodeCenterInsideScreen", "collectConfirmProbe");
  assert.match(ready, /x < device\.width/);
  assert.match(ready, /y < device\.height/);
  assert.match(ready, /function confirmNodeReady/);
});

test("order balance verification excludes generic payout text", () => {
  const orderBalance = functionBody("readBalanceForOrder", "balanceDropMatches");
  assert.match(orderBalance, /scanBalance\(BALANCE_FIND_TIMEOUT_MS, false\)/);
  assert.match(script, /if \(allowGenericFallback === false\) return null;/);
});
