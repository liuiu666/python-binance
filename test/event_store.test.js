const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { EventStore } = require("../lib/event_store");

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "event-store-test-"));
}

test("event store normalizes trade audit events", () => {
  const dir = tempDir();
  let store;
  try {
    const file = path.join(dir, "trade_audit.jsonl");
    store = new EventStore({ serverId: "server-a", dataDir: dir });
    const row = store.appendJsonl(file, { event: "order_done", clientTime: 1710000000000 }, { normalize: true });
    assert.equal(row.serverId, "server-a");
    assert.equal(row.eventStoreVersion, 2);
    assert.ok(row.eventId);
    assert.ok(row.receivedAt);
    assert.equal(store.readJsonl(file).length, 1);
  } finally {
    if (store) store.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("event store import dedupes rows without original serverTime", () => {
  const dir = tempDir();
  let store;
  try {
    const file = path.join(dir, "trade_audit.jsonl");
    store = new EventStore({ serverId: "server-b", dataDir: dir });
    const rows = [
      { event: "order_done", clientTime: 1710000000000, device: "tablet", direction: "DOWN", amount: 5 },
      { event: "order_done", clientTime: 1710000001000, device: "tablet", direction: "UP", amount: 5 }
    ];
    const first = store.importJsonl(file, rows, { importSource: "local" });
    const second = store.importJsonl(file, rows, { importSource: "local" });
    assert.equal(first.imported, 2);
    assert.equal(first.skipped, 0);
    assert.equal(second.imported, 0);
    assert.equal(second.skipped, 2);
  } finally {
    if (store) store.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
