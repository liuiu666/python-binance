const fs = require("fs");

// 通用时间解析工具：CSV/ISO 时间字符串 -> 毫秒时间戳。
// 纯函数，无状态依赖，提到模块级以便外部（如 signal_response）在创建服务前直接复用。
function parseCsvTimeMs(value) {
  if (value === undefined || value === null) return null;
  let s = String(value).trim();
  if (!s) return null;
  s = s.replace(" ", "T").replace(/(\.\d{3})\d+/, "$1");
  const ms = Date.parse(s);
  return Number.isFinite(ms) ? ms : null;
}

// 上海时区（Asia/Shanghai）可读时间格式化，纯函数。
function shanghaiTime(ms) {
  if (!Number.isFinite(ms)) return null;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(new Date(ms));
}

// 本模块集中处理数据文件的新鲜度计算；调用方负责注入路径和信号时间解析规则。
function createDataHealthService({
  signalFile,
  dataUpdateStatusFile,
  secondDataStatusFile,
  secondDataFile,
  orderbookStatusFile,
  orderbookFile,
  orderbookPredictionStatusFile,
  auctionDataStatusFile,
  dataHealthFiles,
  signalSnapshotMaxAgeMs,
  signalTimeMs,
  getDataUpdate
}) {
  function readJsonFile(file, fallback = null) {
    try { return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : fallback; }
    catch (e) { return fallback; }
  }

  function readCsvHeader(file) {
    try {
      const fd = fs.openSync(file, "r");
      const stat = fs.fstatSync(fd);
      const len = Math.min(stat.size, 4096);
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, 0);
      fs.closeSync(fd);
      return buf.toString("utf8").split(/\r?\n/)[0].split(",");
    } catch (e) {
      return [];
    }
  }

  function readLastCsvRows(file, limit = 200, bytes = 512 * 1024) {
    try {
      if (!fs.existsSync(file)) return [];
      const header = readCsvHeader(file);
      if (!header.length) return [];
      const stat = fs.statSync(file);
      const len = Math.min(stat.size, bytes);
      const fd = fs.openSync(file, "r");
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, stat.size - len);
      fs.closeSync(fd);
      let lines = buf.toString("utf8").split(/\r?\n/).filter(Boolean);
      if (stat.size > len && lines.length) lines = lines.slice(1);
      const dataLines = lines.filter(line => line !== header.join(",") && !line.startsWith(header[0] + ",")).slice(-limit);
      return dataLines.map(line => {
        const cells = line.split(",");
        const row = {};
        header.forEach((col, idx) => { row[col] = cells[idx]; });
        return row;
      });
    } catch (e) {
      return [];
    }
  }

  // parseCsvTimeMs / shanghaiTime 复用模块级定义（纯函数，无需重复实现）。

  function csvDataHealth(name, spec, now) {
    const exists = fs.existsSync(spec.file);
    const rows = readLastCsvRows(spec.file, name === "klines1m" ? 300 : 120);
    const times = rows
      .map(row => parseCsvTimeMs(row[spec.timeCol]))
      .filter(ms => Number.isFinite(ms))
      .sort((a, b) => a - b);
    const reasons = [];
    if (!exists) reasons.push(`${name}_missing`);
    if (exists && !times.length) reasons.push(`${name}_empty_or_unparseable`);

    const lastMs = times.length ? times[times.length - 1] : null;
    const ageMs = lastMs === null ? null : now - lastMs;
    if (ageMs !== null && ageMs > spec.maxAgeMs) reasons.push(`${name}_stale`);

    let maxObservedGapMs = null;
    for (let i = 1; i < times.length; i += 1) {
      const gap = times[i] - times[i - 1];
      if (maxObservedGapMs === null || gap > maxObservedGapMs) maxObservedGapMs = gap;
    }
    if (maxObservedGapMs !== null && maxObservedGapMs > spec.maxRecentGapMs) reasons.push(`${name}_recent_gap`);

    return {
      ok: reasons.length === 0,
      reasons,
      lastTime: lastMs === null ? null : new Date(lastMs).toISOString(),
      lastTimeMs: lastMs,
      lastTimeShanghai: lastMs === null ? null : shanghaiTime(lastMs),
      displayTimeZone: "Asia/Shanghai",
      ageMs,
      maxAgeMs: spec.maxAgeMs,
      rowsChecked: times.length,
      maxObservedGapMs,
      maxRecentGapMs: spec.maxRecentGapMs
    };
  }

  function normalizeEpochMs(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return null;
    return n < 1000000000000 ? n * 1000 : n;
  }

  function signalSnapshotTimeMs(signals) {
    if (!signals || typeof signals !== "object") return null;
    const directMs = (
      signals._snapshot_time_ms ??
      signals._snapshotTimeMs ??
      signals._updated_at_ms ??
      signals.updatedAtMs
    );
    const normalized = normalizeEpochMs(directMs);
    if (normalized !== null) return normalized;
    const directTime = (
      signals._snapshot_time ??
      signals._snapshotTime ??
      signals._updated_at ??
      signals.updatedAt
    );
    return parseCsvTimeMs(directTime);
  }

  function signalFileMtimeMs() {
    try {
      return fs.statSync(signalFile).mtimeMs;
    } catch (e) {
      return null;
    }
  }

  function dataHealthGate(signals) {
    const now = Date.now();
    const files = {};
    const reasons = [];
    for (const [name, spec] of Object.entries(dataHealthFiles)) {
      files[name] = csvDataHealth(name, spec, now);
      if (name === "klines1m") reasons.push(...files[name].reasons);
    }

    const updateStatus = readJsonFile(dataUpdateStatusFile, null);
    const updateFailed = !!(updateStatus && updateStatus.ok === false);

    const realSignals = Object.entries(signals || {})
      .filter(([key, sig]) => !key.startsWith("_") && sig && typeof sig === "object" && !sig.shadow);
    const signalTimes = realSignals
      .map(([strategyId, sig]) => ({ strategyId, ms: signalTimeMs(sig), blocked: !!sig.data_health_blocked }))
      .filter(row => Number.isFinite(row.ms));
    const signalFileExists = fs.existsSync(signalFile);
    const snapshotMs = signalSnapshotTimeMs(signals);
    const fileMtimeMs = signalFileExists ? signalFileMtimeMs() : null;
    const latestSignal = signalTimes.sort((a, b) => b.ms - a.ms)[0] || null;
    let freshnessMs = snapshotMs ?? fileMtimeMs ?? (latestSignal ? latestSignal.ms : null);
    let freshnessSource = null;
    if (snapshotMs !== null) freshnessSource = "snapshot_time";
    else if (fileMtimeMs !== null) freshnessSource = "file_mtime";
    else if (latestSignal) freshnessSource = "signal_time";
    const signalAgeMs = freshnessMs === null ? null : now - freshnessMs;

    if (!signalFileExists) {
      reasons.push("signal_file_missing");
    } else if (!realSignals.length || freshnessMs === null) {
      reasons.push("signal_snapshot_missing");
    }
    if (signalAgeMs !== null && signalAgeMs > signalSnapshotMaxAgeMs) reasons.push("signal_snapshot_stale");
    if (realSignals.some(([, sig]) => sig && sig.data_health_blocked)) reasons.push("signal_process_data_health_blocked");

    const uniqueReasons = [...new Set(reasons)];
    const dataUpdate = getDataUpdate();
    return {
      allow: uniqueReasons.length === 0,
      blocked: uniqueReasons.length > 0,
      reasons: uniqueReasons,
      files,
      update: {
        running: dataUpdate.running,
        lastStart: dataUpdate.lastStart,
        lastFinish: dataUpdate.lastFinish,
        lastExitCode: dataUpdate.lastExitCode,
        lastError: dataUpdate.lastError,
        runs: dataUpdate.runs,
        failedButFilesFresh: updateFailed && uniqueReasons.length === 0,
        status: updateStatus
      },
      signal: {
        strategies: realSignals.map(([strategyId]) => strategyId),
        latestTime: freshnessMs === null ? null : new Date(freshnessMs).toISOString(),
        latestTimeMs: freshnessMs,
        latestTimeShanghai: freshnessMs === null ? null : shanghaiTime(freshnessMs),
        latestStrategyTime: latestSignal ? new Date(latestSignal.ms).toISOString() : null,
        latestStrategyTimeMs: latestSignal ? latestSignal.ms : null,
        latestStrategyTimeShanghai: latestSignal ? shanghaiTime(latestSignal.ms) : null,
        freshnessSource,
        displayTimeZone: "Asia/Shanghai",
        ageMs: signalAgeMs,
        maxAgeMs: signalSnapshotMaxAgeMs
      }
    };
  }

  // 这些快照直接对应 HTTP 响应体，字段名和阈值保持原接口契约。
  function dataHealthSnapshot() {
    let signals = {};
    try { signals = fs.existsSync(signalFile) ? JSON.parse(fs.readFileSync(signalFile, "utf8")) : {}; } catch (e) {}
    return dataHealthGate(signals);
  }

  function secondDataHealthSnapshot() {
    const status = readJsonFile(secondDataStatusFile, {});
    let file = { exists: false, size: 0, mtime: null };
    try {
      const stat = fs.statSync(secondDataFile);
      file = { exists: true, size: stat.size, mtime: stat.mtime.toISOString() };
    } catch (e) {}
    const lastTs = status.last_ts ? Date.parse(status.last_ts) : null;
    const ageMs = Number.isFinite(lastTs) ? Date.now() - lastTs : null;
    const maxAgeMs = Number(process.env.SECOND_DATA_MAX_AGE_MS || 120000);
    return {
      ok: !!status.ok && file.exists && ageMs !== null && ageMs <= maxAgeMs,
      ageMs,
      maxAgeMs,
      status: {
        ...status,
        last_ts_ms: status.last_ts ? Date.parse(status.last_ts) : null,
        last_ts_shanghai: status.last_ts ? shanghaiTime(Date.parse(status.last_ts)) : null,
        display_time_zone: "Asia/Shanghai"
      },
      file
    };
  }

  function orderbookHealthSnapshot() {
    const status = readJsonFile(orderbookStatusFile, {});
    let file = { exists: false, size: 0, mtime: null };
    try {
      const stat = fs.statSync(orderbookFile);
      file = { exists: true, size: stat.size, mtime: stat.mtime.toISOString() };
    } catch (e) {}
    const lastTs = status.last_ts ? Date.parse(status.last_ts) : null;
    const ageMs = Number.isFinite(lastTs) ? Date.now() - lastTs : null;
    const maxAgeMs = Number(process.env.ORDERBOOK_MAX_AGE_MS || 30000);
    return {
      ok: !!status.ok && file.exists && ageMs !== null && ageMs <= maxAgeMs,
      ageMs,
      maxAgeMs,
      status: {
        ...status,
        last_ts_ms: status.last_ts ? Date.parse(status.last_ts) : null,
        last_ts_shanghai: status.last_ts ? shanghaiTime(Date.parse(status.last_ts)) : null,
        display_time_zone: "Asia/Shanghai"
      },
      file
    };
  }

  function auctionDataHealthSnapshot() {
    const status = readJsonFile(auctionDataStatusFile, {});
    const streams = status && typeof status.streams === "object" ? status.streams : {};
    const eventAgeMs = Number(status.event_age_ms);
    const depthAgeMs = Number(streams.depth_updates && streams.depth_updates.age_ms);
    const maxAgeMs = Number(process.env.AUCTION_DATA_MAX_AGE_MS || 15000);
    const depthMaxAgeMs = Number(process.env.AUCTION_DEPTH_MAX_AGE_MS || 15000);
    const statusTime = status.updated_at ? Date.parse(status.updated_at) : null;
    const statusAgeMs = Number.isFinite(statusTime) ? Date.now() - statusTime : null;
    return {
      ok: !!status.ok
        && Number.isFinite(eventAgeMs) && eventAgeMs <= maxAgeMs
        && Number.isFinite(depthAgeMs) && depthAgeMs <= depthMaxAgeMs
        && Number.isFinite(statusAgeMs) && statusAgeMs <= maxAgeMs,
      eventAgeMs: Number.isFinite(eventAgeMs) ? eventAgeMs : null,
      depthAgeMs: Number.isFinite(depthAgeMs) ? depthAgeMs : null,
      statusAgeMs,
      maxAgeMs,
      depthMaxAgeMs,
      status: {
        ...status,
        updated_at_shanghai: Number.isFinite(statusTime) ? shanghaiTime(statusTime) : null,
        display_time_zone: "Asia/Shanghai"
      }
    };
  }

  function orderbookPredictionResponse() {
    const status = readJsonFile(orderbookPredictionStatusFile, {});
    const lastTs = status.last_ts ? Date.parse(status.last_ts) : null;
    const ageMs = Number.isFinite(lastTs) ? Date.now() - lastTs : null;
    const maxAgeMs = Number(process.env.ORDERBOOK_PREDICTION_MAX_AGE_MS || 30000);
    return {
      ok: !!status.ok && ageMs !== null && ageMs <= maxAgeMs,
      ageMs,
      maxAgeMs,
      status: {
        ...status,
        last_ts_shanghai: status.last_ts ? shanghaiTime(Date.parse(status.last_ts)) : null,
        display_time_zone: "Asia/Shanghai"
      }
    };
  }

  function orderbookPredictionSnapshot() {
    const status = readJsonFile(orderbookPredictionStatusFile, {});
    const lastTs = status.last_ts ? Date.parse(status.last_ts) : null;
    const ageMs = Number.isFinite(lastTs) ? Date.now() - lastTs : null;
    const maxAgeMs = Number(process.env.ORDERBOOK_PREDICTION_MAX_AGE_MS || 30000);
    return {
      ok: !!status.ok && ageMs !== null && ageMs <= maxAgeMs,
      ageMs,
      maxAgeMs,
      status
    };
  }

  return {
    readCsvHeader,
    readLastCsvRows,
    parseCsvTimeMs,
    shanghaiTime,
    csvDataHealth,
    normalizeEpochMs,
    signalSnapshotTimeMs,
    signalFileMtimeMs,
    dataHealthGate,
    dataHealthSnapshot,
    secondDataHealthSnapshot,
    orderbookHealthSnapshot,
    auctionDataHealthSnapshot,
    orderbookPredictionResponse,
    orderbookPredictionSnapshot
  };
}

module.exports = { createDataHealthService, parseCsvTimeMs, shanghaiTime };
