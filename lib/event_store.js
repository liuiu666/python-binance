const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

function safeJsonParse(line) {
  try {
    return JSON.parse(line);
  } catch (e) {
    return null;
  }
}

function sha256(text) {
  return crypto.createHash("sha256").update(String(text)).digest("hex");
}

function compactParts(parts) {
  return parts.map(v => (v === undefined || v === null ? "" : String(v))).join("|");
}

function deterministicEventId(row, fallbackServerId) {
  if (row.eventId) return String(row.eventId);
  const parts = [
    row.event,
    row.serverTime,
    row.clientTime,
    row.device,
    row.version,
    row.strategyId,
    row.direction,
    row.signalTime,
    row.actionableTime,
    row.queueBatchId,
    row.queuePosition,
    row.amount,
    row.duration,
    row.reason,
    row.tradeId,
    row.openTime,
    row.time,
    row.price,
    row.originServerId || row.serverId || fallbackServerId
  ];
  return "evt_" + sha256(compactParts(parts)).slice(0, 24);
}

class EventStore {
  constructor(options = {}) {
    this.serverId = String(options.serverId || process.env.SERVER_ID || os.hostname() || "unknown");
  }

  normalizeEvent(row, options = {}) {
    const now = Date.now();
    const out = { ...(row || {}) };
    const idSource = { ...out };
    const incomingServerId = out.serverId || out.originServerId || null;
    if (!out.serverTime) out.serverTime = now;
    if (!out.receivedAt) out.receivedAt = now;
    if (!out.serverId) out.serverId = this.serverId;
    if (options.importSource) {
      out.imported = true;
      out.importSource = String(options.importSource);
      if (incomingServerId && incomingServerId !== this.serverId) {
        out.originServerId = String(incomingServerId);
        if (!idSource.originServerId) idSource.originServerId = String(incomingServerId);
      } else if (!out.originServerId) {
        out.originServerId = String(options.importSource);
        if (!idSource.originServerId) idSource.originServerId = String(options.importSource);
      }
    }
    out.eventStoreVersion = 1;
    out.eventId = deterministicEventId(idSource, this.serverId);
    return out;
  }

  appendJsonl(file, obj, options = {}) {
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      const row = options.normalize ? this.normalizeEvent(obj, options) : obj;
      fs.appendFileSync(file, JSON.stringify(row) + "\n", "utf8");
      return row;
    } catch (e) {
      return null;
    }
  }

  tailJsonl(file, limit) {
    try {
      if (!fs.existsSync(file)) return [];
      const raw = fs.readFileSync(file, "utf8").trim();
      if (!raw) return [];
      return raw.split(/\r?\n/).filter(Boolean).slice(-limit).map(line => safeJsonParse(line) || { raw: line });
    } catch (e) {
      return [];
    }
  }

  readJsonl(file) {
    try {
      if (!fs.existsSync(file)) return [];
      const raw = fs.readFileSync(file, "utf8").trim();
      if (!raw) return [];
      return raw.split(/\r?\n/).filter(Boolean).map(safeJsonParse).filter(Boolean);
    } catch (e) {
      return [];
    }
  }

  importJsonl(file, rows, options = {}) {
    const input = Array.isArray(rows) ? rows : [];
    const existingIds = new Set(
      this.readJsonl(file)
        .map(row => row.eventId || deterministicEventId(row, this.serverId))
        .filter(Boolean)
    );
    let imported = 0;
    let skipped = 0;
    const samples = [];
    for (const row of input) {
      if (!row || typeof row !== "object") {
        skipped += 1;
        continue;
      }
      const normalized = this.normalizeEvent(row, {
        normalize: true,
        importSource: options.importSource || "manual_import"
      });
      if (existingIds.has(normalized.eventId)) {
        skipped += 1;
        continue;
      }
      existingIds.add(normalized.eventId);
      const written = this.appendJsonl(file, normalized, { normalize: false });
      if (written) {
        imported += 1;
        if (samples.length < 5) samples.push(written);
      } else {
        skipped += 1;
      }
    }
    return {
      serverId: this.serverId,
      received: input.length,
      imported,
      skipped,
      samples
    };
  }
}

module.exports = { EventStore, deterministicEventId };
