const Database = require("better-sqlite3");
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
    
    // Determine database storage directory
    const dataDir = options.dataDir || process.env.DATA_DIR || path.join(__dirname, "..", "data");
    fs.mkdirSync(dataDir, { recursive: true });
    this.dbPath = path.join(dataDir, "codex.db");
    
    // Initialize SQLite Database
    this.db = new Database(this.dbPath);
    
    // Enable WAL (Write-Ahead Log) mode for maximum concurrency speed and power-loss resilience
    this.db.pragma("journal_mode = WAL");
    
    // Initialize Tables
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS trade_audits (
        eventId TEXT PRIMARY KEY,
        serverTime INTEGER,
        receivedAt INTEGER,
        event TEXT,
        serverId TEXT,
        payload TEXT
      );
      
      CREATE TABLE IF NOT EXISTS price_ticks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time INTEGER,
        price REAL
      );
      
      CREATE INDEX IF NOT EXISTS idx_trade_audits_event ON trade_audits(event);
      CREATE INDEX IF NOT EXISTS idx_trade_audits_serverTime ON trade_audits(serverTime);
      CREATE INDEX IF NOT EXISTS idx_price_ticks_time ON price_ticks(time);
    `);
    
    // Prepare SQL Statements
    this.insertAuditStmt = this.db.prepare(`
      INSERT INTO trade_audits (eventId, serverTime, receivedAt, event, serverId, payload)
      VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(eventId) DO UPDATE SET payload = excluded.payload
    `);
    
    this.insertTickStmt = this.db.prepare(`
      INSERT INTO price_ticks (time, price)
      VALUES (?, ?)
    `);
    
    // Run White-Glove Legacy Migration
    this.autoMigrateLegacyFiles(dataDir);
  }

  autoMigrateLegacyFiles(dataDir) {
    const legacyAuditFile = path.join(dataDir, "trade_audit.jsonl");
    const legacyTicksFile = path.join(dataDir, "price_ticks.jsonl");

    // 1. Migrate legacy trade_audit.jsonl to SQLite table
    if (fs.existsSync(legacyAuditFile)) {
      try {
        const raw = fs.readFileSync(legacyAuditFile, "utf8").trim();
        if (raw) {
          const lines = raw.split(/\r?\n/).filter(Boolean);
          console.log(`[Database Migration] Importing ${lines.length} trade audits from legacy JSONL...`);
          
          const migrate = this.db.transaction((rows) => {
            for (const line of rows) {
              const row = safeJsonParse(line);
              if (row) {
                const normalized = this.normalizeEvent(row);
                this.insertAuditStmt.run(
                  normalized.eventId,
                  normalized.serverTime,
                  normalized.receivedAt,
                  normalized.event,
                  normalized.serverId,
                  JSON.stringify(normalized)
                );
              }
            }
          });
          migrate(lines);
          
          fs.renameSync(legacyAuditFile, legacyAuditFile + ".bak");
          console.log("[Database Migration] Legacy trade audits imported successfully and backed up.");
        }
      } catch (err) {
        console.log("[Database Migration] Failed migrating legacy trade audits:", err);
      }
    }

    // 2. Migrate legacy price_ticks.jsonl to SQLite table
    if (fs.existsSync(legacyTicksFile)) {
      try {
        const raw = fs.readFileSync(legacyTicksFile, "utf8").trim();
        if (raw) {
          const lines = raw.split(/\r?\n/).filter(Boolean);
          console.log(`[Database Migration] Importing ${lines.length} price ticks from legacy JSONL...`);
          
          const migrate = this.db.transaction((rows) => {
            for (const line of rows) {
              const row = safeJsonParse(line);
              if (row && row.time && row.price) {
                this.insertTickStmt.run(Number(row.time), Number(row.price));
              }
            }
          });
          migrate(lines);
          
          fs.renameSync(legacyTicksFile, legacyTicksFile + ".bak");
          console.log("[Database Migration] Legacy price ticks imported successfully and backed up.");
        }
      } catch (err) {
        console.log("[Database Migration] Failed migrating legacy price ticks:", err);
      }
    }
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
    out.eventStoreVersion = 2; // Version 2 represents SQLite backing storage
    out.eventId = deterministicEventId(idSource, this.serverId);
    return out;
  }

  appendJsonl(file, obj, options = {}) {
    try {
      const isTicks = String(file).includes("price_ticks");
      if (isTicks) {
        this.insertTickStmt.run(Number(obj.time || Date.now()), Number(obj.price));
        return obj;
      } else {
        const row = options.normalize ? this.normalizeEvent(obj, options) : obj;
        this.insertAuditStmt.run(
          row.eventId,
          row.serverTime,
          row.receivedAt,
          row.event,
          row.serverId,
          JSON.stringify(row)
        );
        return row;
      }
    } catch (e) {
      console.log("[Database] Append error:", e);
      return null;
    }
  }

  tailJsonl(file, limit) {
    try {
      const isTicks = String(file).includes("price_ticks");
      if (isTicks) {
        const rows = this.db.prepare(`
          SELECT time, price FROM price_ticks
          ORDER BY id DESC LIMIT ?
        `).all(limit);
        return rows.reverse();
      } else {
        const rows = this.db.prepare(`
          SELECT payload FROM trade_audits
          ORDER BY serverTime DESC LIMIT ?
        `).all(limit);
        return rows.reverse().map(row => JSON.parse(row.payload));
      }
    } catch (e) {
      console.log("[Database] Tail error:", e);
      return [];
    }
  }

  readJsonl(file) {
    try {
      const isTicks = String(file).includes("price_ticks");
      if (isTicks) {
        return this.db.prepare(`
          SELECT time, price FROM price_ticks
          ORDER BY id ASC
        `).all();
      } else {
        const rows = this.db.prepare(`
          SELECT payload FROM trade_audits
          ORDER BY serverTime ASC
        `).all();
        return rows.map(row => JSON.parse(row.payload));
      }
    } catch (e) {
      console.log("[Database] Read error:", e);
      return [];
    }
  }

  readJsonlRange(file, startMs, endMs) {
    try {
      const isTicks = String(file).includes("price_ticks");
      const start = Number(startMs);
      const end = Number(endMs);
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return [];
      if (isTicks) {
        return this.db.prepare(`
          SELECT time, price FROM price_ticks
          WHERE time >= ? AND time < ?
          ORDER BY time ASC
        `).all(start, end);
      }
      const rows = this.db.prepare(`
        SELECT payload FROM trade_audits
        WHERE serverTime >= ? AND serverTime < ?
        ORDER BY serverTime ASC
      `).all(start, end);
      return rows.map(row => JSON.parse(row.payload));
    } catch (e) {
      console.log("[Database] Range read error:", e);
      return [];
    }
  }

  readTradeAuditTimes(limit = 5000) {
    try {
      const safeLimit = Math.min(50000, Math.max(100, Number(limit) || 5000));
      return this.db.prepare(`
        SELECT serverTime, payload FROM trade_audits
        WHERE event IN ('order_done', 'order_abort', 'order_unverified', 'shadow_trade_open')
        ORDER BY serverTime DESC
        LIMIT ?
      `).all(safeLimit).map(row => {
        try {
          const payload = JSON.parse(row.payload);
          return Number(payload.openTime || payload.serverTime || row.serverTime);
        } catch (e) {
          return Number(row.serverTime);
        }
      }).filter(time => Number.isFinite(time) && time > 0);
    } catch (e) {
      console.log("[Database] Day list read error:", e);
      return [];
    }
  }

  importJsonl(file, rows, options = {}) {
    const input = Array.isArray(rows) ? rows : [];
    let imported = 0;
    let skipped = 0;
    const samples = [];
    
    try {
      const migrate = this.db.transaction((items) => {
        for (const row of items) {
          if (!row || typeof row !== "object") {
            skipped += 1;
            continue;
          }
          const normalized = this.normalizeEvent(row, {
            normalize: true,
            importSource: options.importSource || "manual_import"
          });
          
          const existing = this.db.prepare(`
            SELECT 1 FROM trade_audits WHERE eventId = ?
          `).get(normalized.eventId);
          
          if (existing) {
            skipped += 1;
            continue;
          }
          
          const result = this.insertAuditStmt.run(
            normalized.eventId,
            normalized.serverTime,
            normalized.receivedAt,
            normalized.event,
            normalized.serverId,
            JSON.stringify(normalized)
          );
          
          if (result.changes > 0) {
            imported += 1;
            if (samples.length < 5) samples.push(normalized);
          } else {
            skipped += 1;
          }
        }
      });
      
      migrate(input);
    } catch (err) {
      console.log("[Database] Import transaction error:", err);
    }
    
    return {
      serverId: this.serverId,
      received: input.length,
      imported,
      skipped,
      samples
    };
  }

  close() {
    if (this.db) {
      this.db.close();
    }
  }
}

module.exports = { EventStore, deterministicEventId };
