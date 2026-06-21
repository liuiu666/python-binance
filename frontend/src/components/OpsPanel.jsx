import React from "react";
import { RefreshCcw } from "lucide-react";

export default function OpsPanel({ runtime, tablet, dataHealth, secondDataHealth, onRefreshData, onRefreshReports }) {
  const tabletVersion = tablet?.latestHeartbeat?.version || "";
  const serverVersion = runtime?.scriptVersion || "";
  const versionMismatch = tabletVersion && serverVersion && tabletVersion !== serverVersion;
  const keepAlive = tablet?.keepAliveStatus || tablet?.latestHeartbeat?.keepAlive || {};
  const yesNo = value => value === true ? "是" : value === false ? "否" : "--";
  const timeoutText = value => {
    const n = Number(value);
    if (!Number.isFinite(n) || n < 0) return "--";
    if (n >= 2147483000) return "永不";
    if (n >= 60000) return `${Math.round(n / 60000)} 分钟`;
    return `${Math.round(n / 1000)} 秒`;
  };
  const ageText = value => {
    const n = Number(value);
    if (!Number.isFinite(n)) return "--";
    if (n < 60000) return `${Math.round(n / 1000)} 秒`;
    return `${Math.round(n / 60000)} 分钟`;
  };
  const secondStatus = secondDataHealth?.status || {};
  const nodeSelection = secondStatus.node_selection || {};
  const activeNode = nodeSelection.active_node || "--";
  const activeProxy = nodeSelection.active_proxy || "未使用";
  const dataOk = dataHealth?.allow === true || dataHealth?.blocked === false;
  const secondOk = secondDataHealth?.ok === true;
  const links = [
    { label: "平板页", url: runtime?.tabletPageUrl },
    { label: "Loader", url: runtime?.loaderUrl },
    { label: "脚本", url: runtime?.scriptUrl },
    { label: "信号", url: runtime?.signalUrl }
  ];

  return (
    <section className="panel ops-panel">
      <header className="panel-header">
        <span><RefreshCcw size={15} /> 操作与链接</span>
      </header>
      <div className="ops-grid">
        <button className="secondary-button" type="button" onClick={onRefreshData}>刷新数据</button>
        <button className="secondary-button" type="button" onClick={onRefreshReports}>生成新报告</button>
      </div>
      <div className="link-list">
        {links.map(item => (
          <a href={item.url} target="_blank" rel="noreferrer" key={item.label}>
            {item.label}
          </a>
        ))}
      </div>
      <div className="ops-status-box">
        <strong>数据采集</strong>
        <div className={dataOk ? "up" : "down"}>1分钟/资金数据 {dataOk ? "正常" : "拦截"} {dataHealth?.reasons?.length ? `(${dataHealth.reasons.join(", ")})` : ""}</div>
        <div className={secondOk ? "up" : "down"}>秒级成交 {secondOk ? "正常" : "异常"}，延迟 {ageText(secondDataHealth?.ageMs)}</div>
        <div>活跃节点 {activeNode}</div>
        <div>代理 {activeProxy}</div>
        <div>累计行数 {secondStatus.rows || "--"}，最新 {secondStatus.last_ts_shanghai || secondStatus.last_ts || "--"}</div>
      </div>
      <div style={{
        marginTop: "10px",
        padding: "8px",
        borderRadius: "4px",
        border: `1px solid ${versionMismatch ? "rgba(228, 88, 88, 0.35)" : "var(--line)"}`,
        background: versionMismatch ? "var(--red-soft)" : "rgba(255,255,255,0.02)",
        color: versionMismatch ? "var(--red)" : "var(--muted)",
        fontSize: "11px",
        lineHeight: 1.5
      }}>
        <strong>Pad 脚本</strong>
        <div>服务器 {serverVersion || "--"}</div>
        <div>平板 {tabletVersion || "等待心跳"}</div>
        <div>保活事件 {tablet?.latestKeepAliveStatus?.event || "--"}</div>
        <div>屏幕点亮 {yesNo(keepAlive.screenOn)} | 修改系统设置 {yesNo(keepAlive.writeSettingsGranted)}</div>
        <div>熄屏时间 {timeoutText(keepAlive.screenOffTimeoutMs)} | 忽略省电 {yesNo(keepAlive.batteryOptimizationIgnored)}</div>
        {versionMismatch ? <div>版本不一致，请在平板重新运行 Loader 或脚本链接。</div> : null}
        {tablet?.checks?.keepAliveFailureRecent ? <div>最近有保活失败，请检查锁屏和后台权限。</div> : null}
      </div>
    </section>
  );
}
