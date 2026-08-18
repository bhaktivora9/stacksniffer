import { Link, useLocation } from "react-router-dom";

const LINKS = [
  ["/", "Overview"],
  ["/repositories", "Repositories"],
  ["/search", "Search"],
  ["/analytics", "Analytics"],
  ["/review", "Review Queue"],
];

export default function TopNavigation({ health, actions, onNewAnalysis }) {
  const location = useLocation();
  const status = health?.status || "ok";
  const pipelineVersion = health?.pipeline_version;
  return <header className="site-header">
    <div className="topnav">
      <Link className="brand" to="/"><span className="wm"><span className="s">STACK</span>SNIFFER</span></Link>
      <nav>{LINKS.map(([to, label]) => <Link key={to} to={to} className={location.pathname === to ? "on" : ""}>[{label}]</Link>)}</nav>
      <div className="statusbar">
        <span className={status === "offline" ? "api-state offline" : "api-state ok"}>API: {status.toUpperCase()}</span>
        {pipelineVersion && <span className="pipeline-version-chip">pipeline v{pipelineVersion}</span>}
        {actions}
        {onNewAnalysis ? <button type="button" className="newbtn" onClick={onNewAnalysis}>＋ New Analysis</button> : <Link className="newbtn" to="/">＋ New Analysis</Link>}
      </div>
    </div>
  </header>;
}
