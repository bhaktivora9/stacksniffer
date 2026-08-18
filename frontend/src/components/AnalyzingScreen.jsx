const STAGES = [
  { label: "ingest", step: 0 },
  { label: "verify · map", step: 1, tone: "map" },
  { label: "infer · ai", step: 3, tone: "ai" },
  { label: "done", step: 4 },
];

const LOGS = [
  ["INF", "Initializing analysis engine", "inf", 0],
  ["RUN", "Cloning repository", "run", 0],
  ["OK", "Repository metadata received", "ok", 0],
  ["RUN", "Parsing file tree · skipping vendor / build", "run", 1],
  ["MAP", "Manifest and dependency signals verified", "map", 2],
  ["OK", "Manifest detection complete", "ok", 2],
  ["AI", "Classifying software type and technology role", "ai", 3],
  ["AI", "Inferring architectural layer and identity", "ai", 3],
  ["OK", "Analysis complete · generating taxonomy", "ok", 4],
];

function StageRail({ currentStep }) {
  return <div className="an-rail">{STAGES.map((stage) => {
    const state = currentStep > stage.step ? "done" : currentStep === stage.step ? "active" : "";
    return <div key={stage.label} className={`an-stage ${stage.tone || ""}`} data-state={state}><span className="an-mk" />{stage.label}</div>;
  })}</div>;
}

function AnalysisConsole({ events }) {
  const visible = events;
  return <div className="an-console">
    <div className="an-console-bar"><span>analysis engine</span><div className="an-dots"><i /><i /><i /></div></div>
    <div className="an-log" aria-live="polite">{visible.map((event, index) => {
      const active = index === visible.length - 1 && event.phase !== "done" && event.level !== "error";
      return <div className="an-line" key={event.seq}><span className="car">&gt;</span><span className={`an-tag ${event.level}`}>[{event.level.toUpperCase()}]</span><span className="an-msg">{event.message}{event.provenance === "ai" && event.confidence != null && <span className="dim"> · confidence {Math.round(event.confidence * 100)}%</span>}{active && <span className="an-cursor" />}</span></div>;
    })}</div>
  </div>;
}

function ProgressFooter({ event }) {
  const progress = Math.round((event?.progress || 0) * 100);
  const verified = event?.verified_total || 0;
  const inferred = event?.inferred_total || 0;
  return <div className="an-foot">
    <div className="an-stat v"><span className="an-box" />verified <b>{verified}</b></div>
    <div className="an-stat i"><span className="an-box" />inferred <b>{inferred}</b></div>
    <div className="an-progress"><div className="an-track"><div className={`an-fill ${event?.phase === "infer" ? "ai" : ""}`} style={{ width: `${progress}%` }} /></div><span>{progress}%</span></div>
  </div>;
}

export default function AnalyzingScreen({ health, currentStep, repoUrl, events = [], status, error, onCancel, onRetry }) {
  const repo = (repoUrl || "github.com/owner/repo").replace(/^https?:\/\//, "").replace(/\/$/, "");
  const complete = status === "complete";
  const failed = status === "failed";
  const latest = events[events.length - 1];
  return <div className="analyzing-screen">
    <TopNavigation health={health} />
    <main className="an-screen-main">
      <div className="an-titlerow"><div><h1>{failed ? "Analysis failed" : "Analyzing"}</h1><div className="an-repo">{repo.split("/").slice(0, -2).join("/") || "github.com"}/<b>{repo.split("/").slice(-2).join("/")}</b></div></div><div className="an-status"><span className={`an-pill ${complete || failed ? "" : "live"} ${failed ? "failed" : ""}`}><i />{failed ? "failed" : complete ? "complete" : "analyzing"}</span><button type="button" onClick={onCancel}>{failed ? "back" : "cancel"}</button></div></div>
      <StageRail currentStep={currentStep} />
      <AnalysisConsole events={events} />
      {failed && <div className="an-error"><span>{error}</span><button type="button" onClick={onRetry}>retry analysis</button></div>}
      <ProgressFooter event={latest} />
    </main>
  </div>;
}
import TopNavigation from "./TopNavigation";
