import React from "react";
import { RefreshCcw } from "lucide-react";

export default function OpsPanel({ runtime, tablet, onRefreshData, onRefreshReports }) {
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
    </section>
  );
}
