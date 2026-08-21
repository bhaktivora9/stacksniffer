import { useEffect, useMemo, useState } from "react";
import { ArrowDownAZ, BookOpen, Check, ChevronLeft, ChevronRight, Search, Sparkles, ThumbsDown, ThumbsUp, X } from "lucide-react";
import { Link } from "react-router-dom";
import AppShell from "../components/AppShell";
import { API_BASE } from "../config/api";

function repoName(row) {
  return row.repo?.full_name || row.repo?.name || row.repo_key || "unknown/repository";
}

function rowId(row) {
  return row.repo_key || row.analysis_id;
}

function relativeTime(value) {
  if (!value) return "Recently";
  const delta = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(delta)) return "Recently";
  const mins = Math.max(0, Math.floor(delta / 60000));
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins} mins ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} hr${hours === 1 ? "" : "s"} ago`;
  return `${Math.floor(hours / 24)} days ago`;
}

function Sparkline({ confidence }) {
  const end = Math.max(8, Math.round((confidence || 0.5) * 20));
  return <svg width="60" height="20" aria-hidden="true"><path d={`M0 17 L10 13 L20 15 L30 8 L40 11 L50 5 L60 ${20-end}`} fill="none" stroke="#4edea3" strokeWidth="2" /></svg>;
}

function compactValue(value) {
  return value ? String(value).replace(/_/g, " ") : "--";
}

function TypeChip({ value, muted = false }) {
  return <span className={`inline-flex max-w-[180px] items-center rounded border px-2 py-1 text-[10px] uppercase ${muted ? "border-border bg-bg text-muted" : "border-green/30 bg-green/10 text-green"}`} title={value || "No value recorded"}>
    <span className="truncate">{compactValue(value)}</span>
  </span>;
}

export default function RepositoryHistoryPage() {
  const [rows, setRows] = useState([]);
  const [softwareTypes, setSoftwareTypes] = useState([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [type, setType] = useState("");
  const [sort, setSort] = useState("time-desc");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [feedbackState, setFeedbackState] = useState({});
  const [correctionOpen, setCorrectionOpen] = useState(null);
  const [corrections, setCorrections] = useState({});
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    const [sortBy, sortOrder] = sort.split("-");
    Promise.all([
      fetch(`${API_BASE}/api/analyses?limit=500&sort_by=${sortBy}&sort_order=${sortOrder}`).then((res) => {
        if (!res.ok) throw new Error(`Could not load repository history (${res.status})`);
        return res.json();
      }),
      fetch(`${API_BASE}/api/taxonomy/software_types`).then((res) => res.ok ? res.json() : { software_types: [] }),
    ]).then(([data, taxonomy]) => {
      if (cancelled) return;
      setRows(data.analyses || []);
      setTotal(data.count || 0);
      setSoftwareTypes((taxonomy.software_types || []).filter((item) => !item.sentinel));
    }).catch((err) => {
      if (!cancelled) setError(err.message);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [sort]);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/review/session`, { credentials: "include" })
      .then((res) => {
        if (!res.ok) throw new Error("guest");
        return res.json();
      })
      .then((session) => {
        if (!cancelled) setIsAdmin(session.role === "admin");
      })
      .catch(() => {
        if (!cancelled) setIsAdmin(false);
      });
    return () => { cancelled = true; };
  }, []);

  async function submitFeedback(row, positive) {
    const key = rowId(row);
    const correction = isAdmin ? corrections[key] : null;
    if (!positive && isAdmin && correctionOpen === key && !correction) return;
    setFeedbackState((state) => ({ ...state, [key]: "submitting" }));
    try {
      const response = await fetch(`${API_BASE}/api/feedback/${key}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          software_type_correct: positive,
          correct_software_type: positive ? null : correction,
        }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Feedback failed (${response.status})`);
      }
      setRows((current) => current.map((item) => rowId(item) === key
        ? { ...item, feedback_count: (item.feedback_count || 0) + 1 }
        : item));
      setFeedbackState((state) => ({ ...state, [key]: positive ? "positive" : "negative" }));
      setCorrectionOpen(null);
    } catch (err) {
      setFeedbackState((state) => ({ ...state, [key]: `error:${err.message}` }));
    }
  }

  const types = useMemo(() => [...new Set(rows.map((row) => row.software_type).filter(Boolean))].sort(), [rows]);
  const filtered = useMemo(() => rows.filter((row) => repoName(row).toLowerCase().includes(query.toLowerCase()) && (!type || row.software_type === type)), [rows, query, type]);

  return <AppShell processingTime={total}>
    <main className="min-h-[calc(100vh-3.5rem)] bg-bg px-4 py-6 md:px-8">
      <div className="mx-auto max-w-7xl">
        <div className="mb-8 flex flex-col justify-between gap-4 md:flex-row md:items-center">
          <div><div className="font-mono text-[10px] uppercase tracking-[.18em] text-green">Registry / Analyses</div><h1 className="mt-1 text-3xl font-semibold tracking-tight text-[#d8e2ff]">Repository History</h1></div>
          <div className="flex flex-col gap-3 sm:flex-row">
            <label className="relative"><Search className="absolute left-3 top-2.5 text-muted" size={17}/><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter repositories..." className="w-full rounded-lg border border-border/60 bg-[#060e20] py-2 pl-10 pr-3 font-mono text-xs outline-none focus:border-accent sm:w-64"/></label>
            <select value={type} onChange={(event) => setType(event.target.value)} className="rounded-lg border border-border/60 bg-[#060e20] px-3 py-2 font-mono text-xs outline-none focus:border-accent"><option value="">All Types</option>{types.map((item) => <option key={item}>{item}</option>)}</select>
            <label className="relative"><ArrowDownAZ className="pointer-events-none absolute left-3 top-2.5 text-muted" size={17}/><select aria-label="Sort repositories" value={sort} onChange={(event) => setSort(event.target.value)} className="w-full rounded-lg border border-border/60 bg-[#060e20] py-2 pl-9 pr-8 font-mono text-xs outline-none focus:border-accent"><option value="time-desc">Newest first</option><option value="time-asc">Oldest first</option><option value="name-asc">Name A–Z</option><option value="name-desc">Name Z–A</option></select></label>
          </div>
        </div>

        <div className="app-glass overflow-hidden rounded-xl">
          <div className="flex items-center justify-between border-b border-border/40 px-4 py-3"><h2 className="font-mono text-[11px] uppercase tracking-wider text-muted">Analysis Registry</h2><span className="rounded-full border border-accent/20 bg-accent/10 px-3 py-1 font-mono text-[10px] uppercase text-accent">{total} total</span></div>
          {error && <div className="p-6 text-sm text-red-400">{error}</div>}
          {loading && <div className="p-8 text-center font-mono text-xs text-muted animate-pulse">Loading analysis registry...</div>}
          {!loading && !error && <div className="overflow-x-auto"><table className="w-full min-w-[1680px] border-collapse text-left">
            <thead className="border-b border-border/30 bg-[#060e20]/50 font-mono text-[10px] uppercase tracking-wider text-muted"><tr><th className="w-[23%] px-4 py-3">Repository</th><th className="px-4 py-3">Last analyzed</th><th className="px-4 py-3">Type</th><th className="px-4 py-3">Classifier L0</th><th className="px-4 py-3">AI predicted</th><th className="px-4 py-3">Specific type</th><th className="px-4 py-3">Confidence</th><th className="px-4 py-3 text-center">Feedback received</th><th className="px-4 py-3">Give feedback</th><th className="px-4 py-3 text-right">Action</th></tr></thead>
            <tbody className="divide-y divide-border/20 font-mono text-xs">{filtered.map((row) => {
              const confidence = Number(row.confidence);
              const key = rowId(row);
              const state = feedbackState[key] || "";
              return <tr key={key} className="group align-top hover:bg-[#2d3449]/30">
                <td className="px-4 py-4"><div className="flex items-center gap-3"><span className="grid h-8 w-8 place-items-center rounded border border-border/40 bg-surface text-muted"><BookOpen size={15}/></span><div><div className="font-medium text-text group-hover:text-accent">{repoName(row)}</div><div className="mt-1 text-[10px] text-muted">{row.repo?.default_branch || "main"} · {(row.commit_sha || "latest").slice(0, 7)}</div></div></div></td>
                <td className="px-4 py-4 text-muted">{relativeTime(row.analyzed_at)}</td>
                <td className="px-4 py-4"><span className="inline-flex items-center gap-1 rounded border border-green/30 bg-green/10 px-2 py-1 text-[10px] uppercase text-green">{row.software_type}{row.software_type === "unknown" && <Sparkles size={10}/>}</span></td>
                <td className="px-4 py-4"><TypeChip value={row.layer0_software_type} muted /></td>
                <td className="px-4 py-4"><TypeChip value={row.ai_software_type} muted /></td>
                <td className="px-4 py-4"><TypeChip value={row.specific_identity} muted /></td>
                <td className="px-4 py-4"><div className="flex items-center gap-2"><span className="w-9 text-text">{Number.isFinite(confidence) ? `${Math.round(confidence * (confidence <= 1 ? 100 : 1))}%` : "--"}</span><Sparkline confidence={confidence <= 1 ? confidence : confidence / 100}/></div></td>
                <td className="px-4 py-4 text-center"><span className="inline-flex min-w-8 justify-center rounded-full border border-border bg-bg px-2 py-1 text-text">{row.feedback_count || 0}</span></td>
                <td className="px-4 py-3"><FeedbackCell row={row} state={state} open={isAdmin && correctionOpen === key} softwareTypes={softwareTypes} correction={corrections[key] || ""} onPositive={() => submitFeedback(row, true)} onNegative={() => { if (isAdmin) { setCorrectionOpen(key); setFeedbackState((current) => ({ ...current, [key]: "" })); } else { submitFeedback(row, false); } }} onCorrection={(value) => setCorrections((current) => ({ ...current, [key]: value }))} onSubmitCorrection={() => submitFeedback(row, false)} onCancel={() => setCorrectionOpen(null)}/></td>
                <td className="px-4 py-4 text-right"><Link to={`/results/${encodeURIComponent(row.analysis_id || row.repo_key)}`} className="rounded border border-border/60 px-3 py-1.5 text-[10px] uppercase text-muted opacity-40 transition group-hover:opacity-100 hover:border-accent hover:text-accent">View</Link></td>
              </tr>;
            })}</tbody>
          </table>{filtered.length === 0 && <div className="p-8 text-center text-sm text-muted">No repositories match these filters.</div>}</div>}
          <div className="flex items-center justify-between border-t border-border/30 bg-surface/40 px-4 py-3 font-mono text-[10px] text-muted"><span>Showing {filtered.length ? 1 : 0}-{filtered.length} of {total}</span><div className="flex gap-1"><button disabled className="grid h-8 w-8 place-items-center rounded border border-border/50 disabled:opacity-30"><ChevronLeft size={15}/></button><button disabled={filtered.length === rows.length} className="grid h-8 w-8 place-items-center rounded border border-border/50 disabled:opacity-30"><ChevronRight size={15}/></button></div></div>
        </div>
      </div>
    </main>
  </AppShell>;
}

function FeedbackCell({ row, state, open, softwareTypes, correction, onPositive, onNegative, onCorrection, onSubmitCorrection, onCancel }) {
  const submitting = state === "submitting";
  const completed = state === "positive" || state === "negative";
  const error = state.startsWith("error:") ? state.slice(6) : "";
  if (open) return <div className="min-w-[230px] space-y-2"><div className="flex gap-1"><select aria-label={`Correction for ${repoName(row)}`} value={correction} onChange={(event) => onCorrection(event.target.value)} className="h-8 min-w-0 flex-1 rounded border border-border bg-bg px-2 text-[11px] text-text outline-none focus:border-accent"><option value="">Suggest correction…</option>{softwareTypes.filter((item) => item.id !== row.software_type).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select><button type="button" onClick={onSubmitCorrection} disabled={!correction || submitting} className="grid h-8 w-8 place-items-center rounded border border-green/30 text-green disabled:opacity-30" aria-label="Submit correction"><Check size={14}/></button><button type="button" onClick={onCancel} className="grid h-8 w-8 place-items-center rounded border border-border text-muted" aria-label="Cancel correction"><X size={14}/></button></div>{error && <div className="max-w-[230px] text-[10px] text-red-400">{error}</div>}</div>;
  return <div className="min-w-[150px]"><div className="flex items-center gap-2"><button type="button" onClick={onPositive} disabled={submitting || completed} className={`grid h-8 w-8 place-items-center rounded border transition ${state === "positive" ? "border-green/50 bg-green/15 text-green" : "border-border text-muted hover:border-green/40 hover:text-green"}`} aria-label={`Positive feedback for ${repoName(row)}`}><ThumbsUp size={14}/></button><button type="button" onClick={onNegative} disabled={submitting || completed} className={`grid h-8 w-8 place-items-center rounded border transition ${state === "negative" ? "border-red-400/50 bg-red-400/15 text-red-400" : "border-border text-muted hover:border-red-400/40 hover:text-red-400"}`} aria-label={`Negative feedback for ${repoName(row)}`}><ThumbsDown size={14}/></button>{submitting && <span className="text-[10px] text-muted animate-pulse">Saving…</span>}{completed && <span className="text-[10px] text-green">Recorded</span>}</div>{error && <div className="mt-1 max-w-[180px] text-[10px] text-red-400">{error}</div>}</div>;
}
