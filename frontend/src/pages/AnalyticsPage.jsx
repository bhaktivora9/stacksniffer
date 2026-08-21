import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  BrainCircuit,
  ChevronDown,
  ChevronRight,
  Database,
  Info,
  Layers3,
  Loader2,
  MessageSquareText,
  RefreshCw,
  ShieldCheck,
  Tags,
  UserRound,
} from "lucide-react";
import AppShell from "../components/AppShell";
import { API_BASE, adminAuthHeaders } from "../config/api";

const pct = (value) => (
  value === null || value === undefined
    ? "n/a"
    : `${Math.round((Number(value) || 0) * 100)}%`
);

function formatTime(value) {
  if (!value) return "Unknown time";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export default function AnalyticsPage() {
  const [stats, setStats] = useState(null);
  const [events, setEvents] = useState([]);
  const [feedbackCounts, setFeedbackCounts] = useState({ software_type: 0, technology_role: 0, architectural_layer: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [trainLoading, setTrainLoading] = useState(false);
  const [trainResult, setTrainResult] = useState(null);
  const [showDisagreements, setShowDisagreements] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    Promise.all([
      fetch(`${API_BASE}/api/learning/stats`).then((res) => {
        if (!res.ok) throw new Error(`Could not load learning stats (${res.status})`);
        return res.json();
      }),
      fetch(`${API_BASE}/api/learning/events?limit=500`).then((res) => {
        if (!res.ok) throw new Error(`Could not load learning events (${res.status})`);
        return res.json();
      }),
    ]).then(([nextStats, ledger]) => {
      if (!cancelled) {
        setStats(nextStats);
        setEvents(ledger.events || []);
        setFeedbackCounts(ledger.feedback_counts || { software_type: 0, technology_role: 0, architectural_layer: 0 });
      }
    }).catch((err) => {
      if (!cancelled) setError(err.message);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [refreshKey]);

  async function trainClassifier() {
    setTrainLoading(true);
    setTrainResult(null);
    try {
      const res = await fetch(`${API_BASE}/api/learning/train-classifier`, {
        method: "POST",
        headers: adminAuthHeaders(),
      });
      const result = await res.json();
      if (!res.ok) throw new Error(result.detail || `Training failed (${res.status})`);
      setTrainResult(result);
      setRefreshKey((value) => value + 1);
    } catch (err) {
      setTrainResult({ status: "error", message: err.message });
    } finally {
      setTrainLoading(false);
    }
  }

  const training = stats?.training_pipeline ?? {};
  const trainableSamples = stats?.trainable_samples ?? training.trainable_samples ?? 0;
  const feedback = stats?.feedback_collected ?? 0;
  const needed = training.needed_for_classifier_training ?? Math.max(0, 50 - trainableSamples);
  const layer0 = stats?.layer0 ?? {};
  const layer0Total = stats?.layer0_predictions_total ?? layer0.predictions_total ?? 0;
  const layer0Disagreements = stats?.layer0_disagreements ?? layer0.disagreements ?? 0;
  const layer0Agreements = stats?.layer0_agreements ?? layer0.agreements ?? Math.max(0, layer0Total - layer0Disagreements);
  const rawAgreementRate = stats?.layer0_agreement_rate ?? layer0.agreement_rate;
  const agreementRate = rawAgreementRate ?? (layer0Total ? layer0Agreements / layer0Total : null);
  const confidenceBands = stats?.layer0_confidence_bands ?? layer0.confidence_bands ?? {};
  const confidenceBandRows = ["high", "medium", "low", "unknown"]
    .map((key) => ({ key, ...(confidenceBands[key] || {}) }))
    .filter((band) => band.total || band.disagreements);
  const disagreementExamples = stats?.layer0_disagreement_examples ?? layer0.disagreement_examples ?? [];
  const counts = useMemo(() => events.reduce((result, event) => {
    if (event.event_kind === "system_coercion") result.system += 1;
    else result.human += 1;
    return result;
  }, { human: 0, system: 0 }), [events]);

  return <AppShell processingTime={stats?.total_analyses ?? stats?.corpus_size} actions={
    <button type="button" onClick={() => setRefreshKey((value) => value + 1)} disabled={loading} className="inline-flex items-center gap-2 rounded border border-border px-3 py-1.5 font-mono text-[10px] text-muted hover:border-accent hover:text-accent disabled:opacity-50">
      <RefreshCw size={13} className={loading ? "animate-spin" : ""}/> Refresh
    </button>
  }>
    <main className="min-h-[calc(100vh-3.5rem)] bg-bg px-4 py-6 md:px-8">
      <div className="mx-auto max-w-7xl">
        <div className="mb-8">
          <div className="font-mono text-[10px] uppercase tracking-[.18em] text-green">Analytics / Classifier</div>
          <h1 className="mt-1 text-3xl font-semibold tracking-tight text-[#d8e2ff]">Classifier Analytics</h1>
          <p className="mt-2 max-w-3xl text-sm text-muted">
            Provenance-bearing approved maintainer corrections and system coercions. Static pattern configuration is excluded. This page also tracks the advisory Layer 0 classifier running in shadow mode.
          </p>
        </div>

        {loading && <AnalyticsSkeleton />}
        {error && <div className="app-glass rounded-xl border-red-400/30 p-6 text-sm text-red-400">{error}</div>}

        {!loading && !error && stats && <div className="space-y-5">
          <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
            <Metric icon={Database} label="Repositories analyzed" value={stats.total_analyses ?? stats.corpus_size ?? 0} detail="total stored analyses" />
            <Metric icon={MessageSquareText} label="Type feedback" value={feedbackCounts.software_type} detail="signals received" />
            <Metric icon={Tags} label="Role feedback" value={feedbackCounts.technology_role} detail="signals received" />
            <Metric icon={Layers3} label="Layer feedback" value={feedbackCounts.architectural_layer} detail="signals received" />
            <Metric icon={ShieldCheck} label="System coercions" value={counts.system} detail="machine guard events" tooltip="Counts times the system caught and corrected its own output." />
          </section>

          <section className="app-glass rounded-xl p-5 md:p-6">
            <div className="flex flex-col justify-between gap-4 lg:flex-row lg:items-start">
              <div className="max-w-3xl">
                <div className="flex items-center gap-2 text-accent">
                  <BrainCircuit size={18}/>
                  <h2 className="text-lg font-semibold text-text">Advisory classifier (Layer 0)</h2>
                </div>
                <p className="mt-2 text-sm text-muted">
                  Trained model active in shadow mode - runs on every analysis, records its prediction, does not override the primary classification.
                </p>
              </div>
              <button type="button" onClick={trainClassifier} disabled={trainLoading} className="inline-flex items-center justify-center gap-2 rounded border border-accent/30 bg-accent/10 px-4 py-2 text-sm text-accent hover:bg-accent/15 disabled:opacity-60">
                {trainLoading && <Loader2 size={15} className="animate-spin"/>} Retrain Layer 0 from feedback snapshots
              </button>
            </div>

            <div className="mt-6 grid gap-4 lg:grid-cols-[1.5fr_1fr_1fr]">
              <div className="rounded-lg border border-border/70 bg-[#070c16]/60 p-5">
                <div className="font-mono text-[10px] uppercase tracking-widest text-muted">Agreement rate</div>
                <div className="mt-3 text-4xl font-semibold text-text">{pct(agreementRate)}</div>
                <p className="mt-2 text-sm text-muted">
                  {layer0Total
                    ? <>Layer 0 agrees with primary classification in <span className="text-text">{pct(agreementRate)}</span> of <span className="text-text">{layer0Total}</span> current analyses where it predicted.</>
                    : "No Layer 0 predictions have been recorded yet. The model remains advisory when it appears in the live path."}
                </p>
              </div>
              <div className="rounded-lg border border-border/70 bg-[#070c16]/60 p-5">
                <div className="font-mono text-[10px] uppercase tracking-widest text-muted">Disagreements</div>
                <div className="mt-3 text-3xl font-semibold text-amber">{layer0Disagreements}</div>
                <p className="mt-2 text-sm text-muted">Shadow predictions that diverged from the primary Gemini path.</p>
              </div>
              <div className="rounded-lg border border-border/70 bg-[#070c16]/60 p-5">
                <div className="font-mono text-[10px] uppercase tracking-widest text-muted">Training feedback</div>
                <div className="mt-3 text-3xl font-semibold text-text">{trainableSamples}</div>
                <p className="mt-2 text-sm text-muted">{needed ? `${needed} trainable samples until the baseline threshold.` : `${feedback} total feedback signals available.`}</p>
              </div>
            </div>

            <div className="mt-5 rounded-lg border border-border/60 bg-[#060b13]/50 p-4">
              <div className="font-mono text-[10px] uppercase tracking-widest text-muted">Agreement by Layer 0 confidence</div>
              {confidenceBandRows.length === 0
                ? <p className="mt-3 text-sm text-muted">No confidence-banded Layer 0 predictions are available yet.</p>
                : <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                  {confidenceBandRows.map((band) => <ConfidenceBand key={band.key} band={band}/>)}
                </div>}
            </div>

            <div className="mt-5 rounded-lg border border-border/60 bg-[#060b13]/50">
              <button type="button" onClick={() => setShowDisagreements((value) => !value)} className="flex w-full items-center justify-between px-4 py-3 text-left text-sm text-muted hover:text-text">
                <span className="inline-flex items-center gap-2">
                  {showDisagreements ? <ChevronDown size={16}/> : <ChevronRight size={16}/>}
                  Review Layer 0 disagreement cases
                </span>
                <span className="font-mono text-[10px] uppercase">{layer0Disagreements} logged</span>
              </button>
              {showDisagreements && <div className="border-t border-border/40 p-4">
                {disagreementExamples.length === 0
                  ? <div className="text-sm text-muted">No disagreement rows are available in this environment.</div>
                  : <div className="grid gap-3 lg:grid-cols-2">{disagreementExamples.map((row, index) => <DisagreementRow key={`${row.repo_key || "repo"}-${index}`} row={row}/>)}</div>}
              </div>}
            </div>

            <div className="mt-4 flex flex-col gap-3 text-sm text-muted md:flex-row md:items-center md:justify-between">
              <p className="inline-flex items-center gap-2"><AlertTriangle size={15} className="text-amber"/> Promotion of Layer 0 from advisory to primary is gated on disagreement analysis - future scope.</p>
              {trainResult && <p className={trainResult.status === "trained" ? "text-green" : "text-amber"}>
                {trainResult.status === "trained"
                  ? `Layer 0 retrained on ${trainResult.samples} samples - remains advisory; ${pct(trainResult.cv_accuracy)} CV accuracy.`
                  : trainResult.message}
              </p>}
            </div>
          </section>

          <section className="app-glass overflow-hidden rounded-xl">
            <PanelHeader title="Learning event ledger" badge={`${events.length} events`} />
            {events.length === 0 ? <Empty>No approved correction or system-coercion events have been recorded.</Empty> : <div className="overflow-x-auto"><table className="w-full min-w-[900px] text-left text-sm">
              <thead className="border-b border-border/40 bg-[#060e20]/50 font-mono text-[10px] uppercase tracking-wider text-muted"><tr><th className="px-4 py-3">Event</th><th className="px-4 py-3">Change</th><th className="px-4 py-3">Repository</th><th className="px-4 py-3">Actor</th><th className="px-4 py-3">Time</th><th className="px-4 py-3">Trigger</th></tr></thead>
              <tbody className="divide-y divide-border/30">{events.map((event) => {
                const system = event.event_kind === "system_coercion";
                return <tr key={event.event_id || event._id}><td className="px-4 py-3"><span className={`inline-flex items-center gap-1.5 rounded border px-2 py-1 font-mono text-[10px] uppercase ${system ? "border-amber/30 bg-amber/10 text-amber" : "border-green/30 bg-green/10 text-green"}`}>{system ? <ShieldCheck size={12}/> : <UserRound size={12}/>} {system ? "coercion" : "correction"}</span></td><td className="px-4 py-3 font-mono text-xs"><span className="text-muted">{event.pipeline_value || "unknown"}</span><span className="mx-2 text-accent">-&gt;</span><span className="text-text">{event.proposed_value || "unknown"}</span></td><td className="px-4 py-3 text-muted">{event.repo || event.repo_key || "unknown"}</td><td className="px-4 py-3 font-mono text-xs text-muted">{event.actor || (event.actor_kind === "user" ? "anonymous user" : event.actor_kind || "unknown")}</td><td className="px-4 py-3 font-mono text-xs text-muted">{formatTime(event.approved_at || event.submitted_at)}</td><td className="max-w-[220px] truncate px-4 py-3 font-mono text-xs text-muted" title={event.trigger_ref}>{event.trigger_ref || "unknown"}</td></tr>;
              })}</tbody>
            </table></div>}
          </section>
        </div>}
      </div>
    </main>
  </AppShell>;
}

function AnalyticsSkeleton() {
  return <div className="space-y-5 animate-pulse">
    <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
      {Array.from({ length: 5 }).map((_, index) => <div key={index} className="app-glass rounded-xl p-5">
        <div className="h-3 w-24 rounded bg-border/70"/>
        <div className="mt-5 h-9 w-16 rounded bg-border/60"/>
        <div className="mt-3 h-3 w-28 rounded bg-border/50"/>
      </div>)}
    </section>
    <section className="app-glass rounded-xl p-6">
      <div className="h-5 w-60 rounded bg-border/70"/>
      <div className="mt-3 h-3 w-3/5 rounded bg-border/50"/>
      <div className="mt-6 grid gap-4 lg:grid-cols-3">
        {Array.from({ length: 3 }).map((_, index) => <div key={index} className="h-32 rounded-lg border border-border/60 bg-border/20"/>)}
      </div>
    </section>
    <section className="app-glass rounded-xl p-5">
      <div className="h-4 w-48 rounded bg-border/70"/>
      <div className="mt-5 space-y-3">
        {Array.from({ length: 4 }).map((_, index) => <div key={index} className="h-10 rounded bg-border/30"/>)}
      </div>
    </section>
  </div>;
}

function DisagreementRow({ row }) {
  return <div className="rounded border border-border/60 bg-[#070c16]/70 p-3">
    <div className="flex items-start justify-between gap-3">
      <div>
        <div className="font-mono text-xs text-accent">{row.repo_key || "unknown repo"}</div>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="rounded border border-purple-300/30 bg-purple-300/10 px-2 py-1 text-purple-200">Layer 0: {row.layer0_software_type || "unknown"}</span>
          <span className="text-muted">-&gt;</span>
          <span className="rounded border border-green/30 bg-green/10 px-2 py-1 text-green">Primary: {row.primary_software_type || "unknown"}</span>
        </div>
      </div>
      {row.guard_corrected && <span className="rounded border border-amber/30 bg-amber/10 px-2 py-1 font-mono text-[9px] uppercase text-amber">guard</span>}
    </div>
    <div className="mt-3 flex flex-wrap gap-3 font-mono text-[10px] text-muted">
      <span>Layer 0 confidence: {pct(row.layer0_confidence)}</span>
      <span>Primary confidence: {pct(row.primary_confidence)}</span>
      <span>{formatTime(row.created_at)}</span>
    </div>
  </div>;
}

function ConfidenceBand({ band }) {
  return <div className="rounded border border-border/60 bg-[#070c16]/70 p-3">
    <div className="flex items-center justify-between gap-2">
      <span className="font-mono text-[10px] uppercase tracking-wider text-muted">{band.label || band.key}</span>
      <span className="font-mono text-xs text-text">{pct(band.agreement_rate)}</span>
    </div>
    <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
      <div className="h-full rounded-full bg-accent" style={{ width: band.agreement_rate == null ? "0%" : pct(band.agreement_rate) }}/>
    </div>
    <div className="mt-2 flex justify-between font-mono text-[10px] text-muted">
      <span>{band.total || 0} predicted</span>
      <span>{band.disagreements || 0} disagreed</span>
    </div>
  </div>;
}

function Metric({ icon: Icon, label, value, detail, tooltip }) {
  return <div className="app-glass rounded-xl p-5" title={tooltip}>
    <div className="flex items-center justify-between gap-3">
      <span className="font-mono text-[10px] uppercase tracking-wider text-muted">{label}</span>
      <span className="inline-flex items-center gap-2">
        {tooltip && <Info size={13} className="text-muted"/>}
        <Icon size={17} className="text-green"/>
      </span>
    </div>
    <div className="mt-3 text-3xl font-semibold text-text">{value}</div>
    <div className="mt-1 text-xs text-muted">{detail}</div>
  </div>;
}

function PanelHeader({ title, badge }) {
  return <div className="flex items-center justify-between border-b border-border/40 px-4 py-3"><h2 className="text-sm font-semibold text-text">{title}</h2><span className="rounded-full border border-accent/20 bg-accent/10 px-2.5 py-1 font-mono text-[9px] uppercase text-accent">{badge}</span></div>;
}

function Empty({ children }) {
  return <div className="p-8 text-center text-sm text-muted">{children}</div>;
}
