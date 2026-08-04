const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "..", "auto_btc.js"), "utf8");

function functionSource(name, nextName) {
  const start = script.indexOf(`function ${name}(`);
  const end = script.indexOf(`function ${nextName}(`, start + 1);
  assert.notEqual(start, -1, `${name} should exist`);
  assert.notEqual(end, -1, `${nextName} should exist after ${name}`);
  return script.slice(start, end);
}

function bounds(top, bottom) {
  return {
    left: 887,
    top,
    right: 2505,
    bottom,
    width: 1618,
    height: bottom - top
  };
}

test("confirmation readiness rejects the transition window outside the tablet screen", () => {
  const context = {
    device: { width: 3392, height: 2400 },
    nodeBoundsSafe: node => node.bounds,
    nodeBoolSafe: (node, key) => node[key]
  };
  vm.createContext(context);
  vm.runInContext(
    `${functionSource("nodeCenterInsideScreen", "confirmNodeReady")}
     ${functionSource("confirmNodeReady", "collectConfirmProbe")}`,
    context
  );

  const transitionNode = { bounds: bounds(4614, 4719), enabled: true, visibleToUser: true };
  const readyNode = { bounds: bounds(2214, 2319), enabled: true, visibleToUser: true };
  const disabledNode = { bounds: bounds(2214, 2319), enabled: false, visibleToUser: true };

  assert.equal(context.confirmNodeReady(transitionNode), false);
  assert.equal(context.confirmNodeReady(readyNode), true);
  assert.equal(context.confirmNodeReady(disabledNode), false);
});

test("confirmation waits for an on-screen node and dispatches exactly once", () => {
  let now = 0;
  let lookupCount = 0;
  let dispatchCount = 0;
  let dispatchedNode = null;
  const transitionNode = { bounds: bounds(4614, 4719), enabled: true, visibleToUser: true };
  const readyNode = { bounds: bounds(2214, 2319), enabled: true, visibleToUser: true };
  const directionEvidenceNode = {
    text: "上涨",
    bounds: { left: 1400, top: 900, right: 1500, bottom: 960, width: 100, height: 60 },
    enabled: true,
    visibleToUser: true
  };
  const context = {
    Date: { now: () => now },
    device: { width: 3392, height: 2400 },
    CONFIRM_MIN_SETTLE_MS: 700,
    CONFIRM_FIND_TIMEOUT_MS: 6000,
    CONFIRM_BUTTON_IDS: ["2131448753", "2131448374"],
    CONFIRM_TEXT_PATTERN: /Confirm/,
    PACKAGE: "com.binance.dev",
    UI_FAST_POLL_MS: 80,
    lastConfirmProbe: null,
    lastNodeClickProbe: null,
    sleep: ms => { now += ms; },
    id: value => value,
    findOnceSafe: () => (lookupCount++ === 0 ? transitionNode : readyNode),
    selector: () => ({
      textMatches: pattern => ({
        clickable: () => ({ find: () => [] }),
        find: () => pattern.test("上涨") ? [directionEvidenceNode] : []
      }),
      descMatches: () => ({ find: () => [] })
    }),
    nodeBoundsSafe: node => node.bounds,
    nodeBoolSafe: (node, key) => node[key],
    nodeSummary: node => ({ text: node.text || "", visible: node.visibleToUser, bounds: node.bounds }),
    log: () => {}
  };
  context.clickNodeOrAncestor = node => {
    dispatchCount += 1;
    dispatchedNode = node;
    context.lastNodeClickProbe = { method: "node_accessibility", dispatched: true };
    return true;
  };
  vm.createContext(context);
  vm.runInContext(
    `${functionSource("nodeCenterInsideScreen", "confirmNodeReady")}
     ${functionSource("confirmNodeReady", "collectConfirmProbe")}
     ${functionSource("collectExpectedDirectionEvidence", "handleConfirmStrict")}
     ${functionSource("handleConfirmStrict", "findConfirmationNode")}`,
    context
  );

  assert.equal(context.handleConfirmStrict("UP"), true);
  assert.equal(dispatchCount, 1);
  assert.equal(dispatchedNode, readyNode);
  assert.equal(now, 780);
  assert.equal(context.lastConfirmProbe.dispatch.dispatched, true);
});

test("balance verification rejects payout text and accepts the configured stake drop", () => {
  const context = { ORDER_BALANCE_TOLERANCE_USDT: 0.25, isFinite };
  vm.createContext(context);
  vm.runInContext(functionSource("balanceDropMatches", "waitForBalanceDrop"), context);

  assert.equal(context.balanceDropMatches(102.35, 9, 5), false);
  assert.equal(context.balanceDropMatches(102.35, 102.35, 5), false);
  assert.equal(context.balanceDropMatches(102.35, 97.36, 5), true);
});

test("pre-dispatch balance guard accepts exact funds and rejects insufficient funds", () => {
  const context = { isFinite };
  vm.createContext(context);
  vm.runInContext(functionSource("hasSufficientBalanceForOrder", "markTradePhase"), context);

  assert.equal(context.hasSufficientBalanceForOrder(5.83, 5), true);
  assert.equal(context.hasSufficientBalanceForOrder(5, 5), true);
  assert.equal(context.hasSufficientBalanceForOrder(4.99, 5), false);
  assert.equal(context.hasSufficientBalanceForOrder(null, 5), false);
});
