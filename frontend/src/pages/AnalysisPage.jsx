import { Fragment, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import AppShell from "../components/AppShell";
import { askQuestion, getOverview } from "../services/analyses";

const GROUNDING = {
  SUPPORTED: { label: "Grounded", tone: "border-green/25 bg-green/10 text-green" },
  PARTIALLY_SUPPORTED: { label: "Partly grounded", tone: "border-amber/25 bg-amber/10 text-amber" },
  UNSUPPORTED: { label: "Not grounded", tone: "border-red-400/30 bg-red-400/10 text-red-400" },
  INSUFFICIENT_EVIDENCE: { label: "Insufficient evidence", tone: "border-amber/25 bg-amber/10 text-amber" },
};

function location(item) {
  if (!item?.file_path && !item?.file) return null;
  const path = item.file_path || item.file;
  return item.start_line ? `${path}:${item.start_line}-${item.end_line}` : path;
}

/** Answer text with each [E1]/[C2] marker as a button that opens that citation. */
function AnswerText({ text, citations, onSelect }) {
  const known = new Set(citations.map((c) => c.label));
  const parts = text.split(/(\[(?:E|C)\d+\])/g);
  return (
    <p className="whitespace-pre-wrap text-sm leading-relaxed text-text">
      {parts.map((part, index) => {
        const match = part.match(/^\[((?:E|C)\d+)\]$/);
        if (!match) return <Fragment key={index}>{part}</Fragment>;
        const label = match[1];
        return known.has(label) ? (
          <button key={index} type="button" onClick={() => onSelect(label)}
            className="mx-0.5 rounded border border-accent/30 bg-accent/10 px-1 font-mono text-[11px] text-accent hover:bg-accent/20">
            {label}
          </button>
        ) : (
          <span key={index} title="Not in the retrieved context" className="mx-0.5 rounded border border-red-400/30 px-1 font-mono text-[11px] text-red-400 line-through">
            {label}
          </span>
        );
      })}
    </p>
  );
}

function Citation({ citation, open, onToggle }) {
  const where = location(citation);
  return (
    <li id={`citation-${citation.label}`} className="px-3 py-2.5">
      <button type="button" onClick={onToggle} className="flex w-full items-start gap-3 text-left">
        <span className="mt-0.5 rounded border border-accent/30 bg-accent/10 px-1.5 font-mono text-[11px] text-accent">{citation.label}</span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm text-text">
            {citation.kind === "community" ? citation.title : citation.name}
          </span>
          <span className="block truncate font-mono text-[11px] text-muted">
            {citation.kind === "community" ? `community · ${(citation.files || []).slice(0, 2).join(", ")}` : where}
          </span>
        </span>
      </button>
      {open && (citation.kind === "community" ? (
        <p className="mt-2 text-xs leading-relaxed text-muted">{citation.summary}</p>
      ) : (
        <pre className="mt-2 max-h-80 overflow-auto rounded border border-border bg-bg p-3 font-mono text-[11px] leading-relaxed text-text">
          {citation.snippet}
        </pre>
      ))}
    </li>
  );
}

function AskPanel({ analysisId, enabled }) {
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [answer, setAnswer] = useState(null);
  const [openLabel, setOpenLabel] = useState(null);

  async function submit(event) {
    event.preventDefault();
    if (!question.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const result = await askQuestion(analysisId, question.trim(), mode || null);
      setAnswer(result);
      setOpenLabel(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  function select(label) {
    setOpenLabel(label);
    document.getElementById(`citation-${label}`)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  const grounding = answer && GROUNDING[answer.grounding_status];
  return (
    <section className="rounded-lg border border-border bg-surface p-4">
      <div className="text-xs font-medium uppercase tracking-wider text-muted">Ask the repository</div>
      <form onSubmit={submit} className="mt-3 flex flex-wrap gap-2">
        <input value={question} onChange={(e) => setQuestion(e.target.value)} disabled={!enabled || loading}
          placeholder={enabled ? "How does login work? What is the overall architecture?" : "Answers are not configured on the server"}
          className="h-9 min-w-[260px] flex-1 rounded border border-border bg-bg px-3 text-sm text-text outline-none focus:border-accent" />
        <select value={mode} onChange={(e) => setMode(e.target.value)} disabled={!enabled || loading}
          className="h-9 rounded border border-border bg-bg px-2 text-sm text-text outline-none focus:border-accent"
          title="Local: specific code. Global: the repository as a whole.">
          <option value="">Auto</option>
          <option value="local">Local</option>
          <option value="global">Global</option>
        </select>
        <button type="submit" disabled={!enabled || loading || !question.trim()}
          className="h-9 rounded border border-accent/30 bg-accent/10 px-4 text-sm text-accent transition-colors hover:bg-accent/15 disabled:cursor-not-allowed disabled:opacity-50">
          {loading ? "Thinking…" : "Ask"}
        </button>
      </form>
      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
      {answer && (
        <div className="mt-4 space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            <span className="rounded border border-ai-purple/25 bg-ai-purple/10 px-2 py-0.5 text-ai-purple">{answer.mode.toLowerCase()} graphrag</span>
            {grounding && <span className={`rounded border px-2 py-0.5 ${grounding.tone}`}>{grounding.label}</span>}
            <span className="text-muted">{answer.latency_ms} ms · {answer.tokens.input + answer.tokens.output} tokens · {answer.context.length} context items</span>
          </div>
          <AnswerText text={answer.answer} citations={answer.citations} onSelect={select} />
          {answer.citations.length > 0 && (
            <ul className="divide-y divide-border rounded border border-border bg-bg">
              {answer.citations.map((citation) => (
                <Citation key={citation.label} citation={citation} open={openLabel === citation.label}
                  onToggle={() => setOpenLabel(openLabel === citation.label ? null : citation.label)} />
              ))}
            </ul>
          )}
          <details className="text-xs text-muted">
            <summary className="cursor-pointer">Retrieved context ({answer.context.length})</summary>
            <ul className="mt-2 space-y-1 font-mono text-[11px]">
              {answer.context.map((item) => (
                <li key={item.label}>
                  <span className="text-accent">{item.label}</span> {item.kind === "community" ? item.title : location(item)} <span className="text-muted">· {item.reason}</span>
                </li>
              ))}
            </ul>
          </details>
        </div>
      )}
    </section>
  );
}

// Partial results must not read as complete: say what is missing and, when parked, how to resume.
function StatusBanner({ analysis }) {
  const stage = (value) => value?.toLowerCase();
  if (analysis.status === "DEGRADED") {
    const where = analysis.failure_stage ? ` The ${stage(analysis.failure_stage)} stage did not finish cleanly${analysis.failure_code ? ` (${analysis.failure_code})` : ""}.` : "";
    return <div role="status" className="rounded border border-amber/40 bg-amber/10 px-4 py-3 text-sm text-amber">
      <b>Completed with degraded results.</b>{where} Counts, components and answers below may be incomplete.
    </div>;
  }
  if (analysis.status === "AWAITING_STAGE") {
    return <div role="status" className="rounded border border-amber/40 bg-amber/10 px-4 py-3 text-sm text-amber">
      <b>Paused before the {stage(analysis.awaiting_stage) || "next"} stage.</b> Shown: what was built through {stage(analysis.last_completed_stage) || "the last completed stage"}.
      {analysis.waiting_reason && <p className="mt-1 text-xs">{analysis.waiting_reason}</p>}
    </div>;
  }
  if (analysis.status === "FAILED") {
    return <div role="status" className="rounded border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-400">
      <b>Analysis failed{analysis.failure_stage ? ` during ${stage(analysis.failure_stage)}` : ""}.</b> Anything shown below is from the stages that completed.
    </div>;
  }
  return null;
}

function CommunityCard({ community }) {
  return (
    <li className="rounded-lg border border-border bg-surface px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium text-text">{community.title || community.key}</h3>
        <span className="font-mono text-[11px] text-muted">{community.member_count} entities{community.children ? ` · ${community.children} parts` : ""}</span>
      </div>
      {community.summary ? (
        <p className="mt-1 text-xs leading-relaxed text-muted">{community.summary}</p>
      ) : (
        <p className="mt-1 text-xs text-amber">Summary {community.summary_status ? community.summary_status.toLowerCase() : "not generated"}</p>
      )}
      {community.key_points.length > 0 && (
        <ul className="mt-2 space-y-1">
          {community.key_points.slice(0, 4).map((point, index) => (
            <li key={index} className="text-xs text-text">
              • {point.text.replace(/\s*\[(?:E|C)\d+\]/g, "")}
              {point.references.filter((r) => r.file).slice(0, 2).map((ref) => (
                <span key={ref.label} className="ml-1 font-mono text-[10px] text-muted">{location(ref)}</span>
              ))}
            </li>
          ))}
        </ul>
      )}
      <div className="mt-2 flex flex-wrap gap-1.5">
        {community.files.slice(0, 4).map((file) => (
          <span key={file} className="rounded bg-bg px-1.5 py-0.5 font-mono text-[10px] text-muted">{file.split("/").slice(-2).join("/")}</span>
        ))}
      </div>
    </li>
  );
}

export default function AnalysisPage() {
  const { analysisId } = useParams();
  const [overview, setOverview] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let active = true;
    getOverview(analysisId)
      .then((data) => active && setOverview(data))
      .catch((err) => active && setError(err.message));
    return () => { active = false; };
  }, [analysisId]);

  if (error) {
    return (
      <AppShell>
        <main className="mx-auto max-w-6xl px-4 py-10 text-sm">
          <p className="text-red-400">{error}</p>
          <Link to="/" className="mt-3 inline-block text-accent hover:underline">Analyze a repository</Link>
        </main>
      </AppShell>
    );
  }
  if (!overview) {
    return <AppShell><main className="mx-auto max-w-6xl px-4 py-10 text-sm text-muted">Loading analysis…</main></AppShell>;
  }

  const { repository, analysis, counts, graph, communities, languages } = overview;
  const metrics = graph?.communities;
  return (
    <AppShell>
      <main className="min-h-[calc(100vh-3.5rem)] bg-bg px-4 py-6 md:px-8">
        <div className="mx-auto max-w-6xl space-y-6">
          <header>
            <div className="font-mono text-[10px] uppercase tracking-[.18em] text-green">Analysis · {analysis.status}</div>
            <h1 className="mt-1 text-3xl font-semibold tracking-tight text-[#d8e2ff]">{repository.owner}/{repository.name}</h1>
            <p className="mt-1 font-mono text-xs text-muted">
              commit {repository.commit_sha.slice(0, 12)} · {counts.files} files · {counts.entities} entities · {counts.chunks} chunks
              {languages.length > 0 && ` · ${languages.slice(0, 4).map((l) => l.language).join(", ")}`}
            </p>
            {graph && (
              <p className="mt-1 font-mono text-[11px] text-muted">
                graph {graph.nodes} nodes / {graph.edges} edges
                {metrics && ` · ${metrics.communities?.["0"] ?? 0} components (modularity ${metrics.modularity})`}
              </p>
            )}
          </header>

          <StatusBanner analysis={analysis} />

          <AskPanel analysisId={analysisId} enabled={overview.answers_available && analysis.status === "READY"} />

          <section>
            <div className="text-xs font-medium uppercase tracking-wider text-muted">Components</div>
            {communities.length === 0 ? (
              <p className="mt-2 text-sm text-muted">No components yet: the analysis has not been projected and summarized.</p>
            ) : (
              <ul className="mt-3 grid gap-3 md:grid-cols-2">
                {communities.map((community) => <CommunityCard key={community.id} community={community} />)}
              </ul>
            )}
          </section>
        </div>
      </main>
    </AppShell>
  );
}
