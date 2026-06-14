import React from "react";
import { RefreshCcw } from "lucide-react";

export default function OpsPanel({ runtime, tablet, onRefreshData, onRefreshReports }) {
  const tabletVersion = tablet?.latestHeartbeat?.version || "";
  const serverVersion = runtime?.scriptVersion || "";
  const versionMismatch = tabletVersion && serverVersion && tabletVersion !== serverVersion;
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
        {versionMismatch ? <div>版本不一致，请在平板重新运行 Loader 或脚本链接。</div> : null}
      </div>
    </section>
  );
}
