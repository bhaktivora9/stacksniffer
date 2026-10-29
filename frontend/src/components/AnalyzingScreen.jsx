import { Link } from "react-router-dom";
import TopNavigation from "./TopNavigation";
import { eventTag, railStages, repositoryLabel, stageIndex } from "../utils/analysisEvents";

function StageRail({ stages, current, complete }) {
  return <div className="an-rail">{stages.map((stage, index) => {
    const state = index < current || (complete && index === current) ? "done" : index === current ? "active" : "";
    return <div key={stage.state} className={`an-stage ${stage.tone || ""}`} data-state={state}><span className="an-mk" />{stage.label}</div>;
  })}</div>;
}

function AnalysisConsole({ events, live }) {
  return <div className="an-console">
    <div className="an-console-bar"><span>analysis pipeline</span><div className="an-dots"><i /><i /><i /></div></div>
    <div className="an-log" aria-live="polite">{events.map((event, index) => {
      const latest = index === events.length - 1;
      const [tag, tone] = eventTag(event, { latest, live });
      return <div className="an-line" key={event.sequence}><span className="car">&gt;</span><span className={`an-tag ${tone}`}>[{tag}]</span><span className="an-msg">{event.message}{latest && live && <span className="an-cursor" />}</span></div>;
    })}</div>
  </div>;
}

// Stage-based only: the backend reports which stage runs, not how far through it is.
function ProgressFooter({ stages, current, complete, failed }) {
  const done = complete ? stages.length : Math.max(current, 0);
  const label = current < 0 ? "queued" : stages[Math.min(current, stages.length - 1)].label;
  return <div className="an-foot">
    <div className="an-stat v"><span className="an-box" />stage <b>{label}</b></div>
    <div className="an-progress"><div className="an-track"><div className={`an-fill ${failed ? "failed" : ""}`} style={{ width: `${(done / stages.length) * 100}%` }} /></div><span>{done} of {stages.length} stages</span></div>
  </div>;
}

export default function AnalyzingScreen({ health, repoUrl, analysisId, events = [], status, error, onCancel, onRetry }) {
  const latest = events[events.length - 1];
  // A followed link has no typed URL; the stream names the repository (older servers do not).
  const repo = (repoUrl || repositoryLabel(latest?.repository) || "").replace(/^https?:\/\//, "").replace(/\/$/, "");
  const [host, ...path] = repo.split("/");
  const complete = status === "complete";
  const failed = status === "failed";
  const waiting = status === "waiting";
  const finished = complete || failed || waiting;
  const stages = railStages(latest);
  const current = stageIndex(latest, stages);
  const analysisLink = analysisId && `/analyses/${encodeURIComponent(analysisId)}`;
  return <div className="analyzing-screen">
    <TopNavigation health={health} />
    <main className="an-screen-main">
      <div className="an-titlerow"><div><h1>{failed ? "Analysis failed" : waiting ? "Analysis paused" : "Analyzing"}</h1><div className="an-repo">{repo ? <>{host}/<b>{path.join("/")}</b></> : "repository"}</div></div><div className="an-status"><span className={`an-pill ${finished ? "" : "live"} ${failed ? "failed" : ""}`}><i />{failed ? "failed" : complete ? "complete" : waiting ? "waiting" : "analyzing"}</span><button type="button" onClick={onCancel}>{finished ? "back" : "cancel"}</button></div></div>
      <StageRail stages={stages} current={current} complete={complete} />
      <AnalysisConsole events={events} live={!finished} />
      {failed && <div className="an-error"><span>{error}</span><button type="button" onClick={onRetry}>retry analysis</button></div>}
      {waiting && <div className="an-notice">
        <div><p>{latest?.message}. What has been built so far can be explored now.</p>{latest?.waitingReason && <p className="an-reason">{latest.waitingReason}</p>}</div>
        {analysisLink && <Link to={analysisLink}>open analysis</Link>}
      </div>}
      <ProgressFooter stages={stages} current={current} complete={complete} failed={failed} />
    </main>
  </div>;
}

/** Shown in place of the progress screen if it fails to render: the analysis itself is still reachable. */
export function AnalyzingScreenFallback({ analysisId, error, reset }) {
  return <div className="app-crash" role="alert">
    <h1>The progress view failed to render</h1>
    <pre>{String(error?.message || error)}</pre>
    <div>
      {analysisId && <Link to={`/analyses/${encodeURIComponent(analysisId)}`}>open analysis</Link>}
      <button type="button" onClick={reset}>try again</button>
      <a href="/">back to home</a>
    </div>
  </div>;
}
