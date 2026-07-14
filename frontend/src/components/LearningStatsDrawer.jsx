import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, Loader2, X } from "lucide-react";
import { API_BASE } from "../config/api";

function pct(value) {
  if (value == null) return "0%";
  return `${Math.round(value * 100)}%`;
}

export default function LearningStatsDrawer({ open, onClose }) {
  const [stats, setStats] = useState(null);
  const [statsLoading, setStatsLoading] = useState(false);
  const [statsError, setStatsError] = useState(null);
  const [accuracyOpen, setAccuracyOpen] = useState(false);
  const [accuracy, setAccuracy] = useState(null);
  const [accuracyLoading, setAccuracyLoading] = useState(false);
  const [trainLoading, setTrainLoading] = useState(false);
  const [trainResult, setTrainResult] = useState(null);

  useEffect(() => {
    if (!open) return;
    setStatsLoading(true);
    setStatsError(null);
    fetch(`${API_BASE}/api/learning/stats`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load (${res.status})`);
        return res.json();
      })
      .then(setStats)
      .catch((err) => setStatsError(err.message))
      .finally(() => setStatsLoading(false));
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handler = (event) => {
      if (event.key === "Escape") onClose?.();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, onClose]);

  useEffect(() => {
    if (!accuracyOpen || accuracy || accuracyLoading) return;
    setAccuracyLoading(true);
    fetch(`${API_BASE}/api/learning/pattern-accuracy`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load (${res.status})`);
        return res.json();
      })
      .then(setAccuracy)
      .catch(() => setAccuracy({ low_accuracy_patterns: [] }))
      .finally(() => setAccuracyLoading(false));
  }, [accuracyOpen, accuracy, accuracyLoading]);

  async function trainClassifier() {
    setTrainLoading(true);
    setTrainResult(null);
    try {
      const res = await fetch(`${API_BASE}/api/learning/train-classifier`, {
        method: "POST",
      });
      if (!res.ok) throw new Error(`Training failed (${res.status})`);
      setTrainResult(await res.json());
    } catch (err) {
      setTrainResult({ status: "error", message: err.message });
    } finally {
      setTrainLoading(false);
    }
  }

  const feedbackCollected = stats?.feedback_collected ?? 0;
  const training = stats?.training_pipeline ?? {};
  const needed = training.needed_for_classifier_training ?? training.needed ?? Math.max(0, 50 - feedbackCollected);
  const progress = Math.min(100, Math.round((feedbackCollected / 50) * 100));
  const changedPatterns = stats?.top_changed_patterns ?? [];
  const lowAccuracy = accuracy?.low_accuracy_patterns ?? [];
  const trained = trainResult?.status === "trained";

  return (
    <div className={`fixed inset-0 z-[70] ${open ? "pointer-events-auto" : "pointer-events-none"}`}>
      <div
        className={`absolute inset-0 bg-bg/60 transition-opacity duration-300 ${
          open ? "opacity-100" : "opacity-0"
        }`}
        onClick={onClose}
      />
      <aside
        className={`absolute right-0 top-0 h-full w-full max-w-xl bg-surface border-l border-border shadow-2xl transition-transform duration-300 ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
      >
        <div className="h-full overflow-y-auto">
          <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-surface px-5 py-4">
            <div>
              <h2 className="text-base font-semibold text-text">Learning stats</h2>
              <p className="mt-0.5 text-xs text-muted font-mono">feedback loop status</p>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="inline-flex h-8 w-8 items-center justify-center rounded border border-border text-muted hover:text-text"
              aria-label="Close learning stats"
            >
              <X size={16} />
            </button>
          </div>

          {statsLoading && (
            <div className="px-5 py-10 text-center font-mono text-sm text-muted animate-pulse">
              Loading learning pipeline...
            </div>
          )}

          {statsError && <div className="px-5 py-4 text-sm text-red-400">{statsError}</div>}

          {!statsLoading && !statsError && stats && (
            <div className="p-5 space-y-6">
              <section className="space-y-3">
                <h3 className="text-xs text-muted uppercase tracking-wider font-sans">
                  Corpus & Feedback
                </h3>
                <div className="grid grid-cols-2 gap-3">
                  <Metric label="Corpus size" value={`${stats.corpus_size ?? 0} repos analyzed`} />
                  <Metric label="Feedback collected" value={`${feedbackCollected} signals`} />
                </div>
                <div>
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-xs text-muted">Classifier minimum</span>
                    <span className="font-mono text-xs text-muted">{progress}%</span>
                  </div>
                  <div className="h-1.5 rounded-full bg-border overflow-hidden">
                    <div className="h-full rounded-full bg-accent" style={{ width: `${progress}%` }} />
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-sm text-text">Training pipeline:</span>
                  <span
                    className={`text-xs font-mono rounded border px-2 py-0.5 ${
                      training.status === "active"
                        ? "bg-green/10 text-green border-green/25"
                        : "bg-amber/10 text-amber border-amber/25"
                    }`}
                  >
                    {training.status ?? "collecting_data"}
                  </span>
                </div>
              </section>

              <section>
                <h3 className="text-xs text-muted uppercase tracking-wider mb-3 font-sans">
                  Pattern changes from learning
                </h3>
                {changedPatterns.length === 0 ? (
                  <p className="text-sm text-muted">
                    No pattern changes yet - submit feedback to start learning
                  </p>
                ) : (
                  <div className="overflow-x-auto border border-border rounded">
                    <table className="w-full text-sm">
                      <thead className="bg-bg">
                        <tr className="text-xs text-muted">
                          <th className="text-left px-3 py-2 font-normal">Tech</th>
                          <th className="text-left px-3 py-2 font-normal">Category</th>
                          <th className="text-left px-3 py-2 font-normal">Confidence</th>
                          <th className="text-left px-3 py-2 font-normal">Direction</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border">
                        {changedPatterns.map((pattern, index) => (
                          <tr key={`${pattern.tech}-${index}`}>
                            <td className="px-3 py-2 font-mono text-xs text-text">{pattern.tech}</td>
                            <td className="px-3 py-2 text-muted">{pattern.category}</td>
                            <td className="px-3 py-2 font-mono text-xs text-muted">
                              {pct(pattern.confidence)}
                            </td>
                            <td className="px-3 py-2">
                              {pattern.direction === "increased" ? (
                                <span className="inline-flex items-center gap-1 text-green">
                                  <ArrowUp size={13} /> increased
                                </span>
                              ) : (
                                <span className="inline-flex items-center gap-1 text-red-400">
                                  <ArrowDown size={13} /> decreased
                                </span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>

              <section className="space-y-3">
                <h3 className="text-xs text-muted uppercase tracking-wider font-sans">
                  Classifier status
                </h3>
                {trained ? (
                  <p className="text-sm text-text">
                    Active <span className="text-green font-mono">CV accuracy: {pct(trainResult.cv_accuracy)}</span>
                  </p>
                ) : (
                  <p className="text-sm text-muted">
                    Classifier not trained yet - Need {needed} more feedback samples
                  </p>
                )}
                <button
                  type="button"
                  onClick={trainClassifier}
                  disabled={trainLoading}
                  className="inline-flex items-center gap-2 rounded border border-accent/30 bg-accent/10 px-3 py-2 text-sm text-accent transition-colors hover:bg-accent/15 disabled:cursor-wait disabled:opacity-70"
                >
                  {trainLoading && <Loader2 size={14} className="animate-spin" />}
                  Train classifier now
                </button>
                {trainResult && (
                  <p className={`text-sm ${trained ? "text-green" : "text-amber"}`}>
                    {trained
                      ? `Trained on ${trainResult.samples} samples - ${pct(trainResult.cv_accuracy)} accuracy`
                      : trainResult.message}
                  </p>
                )}
              </section>

              <section>
                <button
                  type="button"
                  onClick={() => setAccuracyOpen((value) => !value)}
                  className="flex w-full items-center justify-between text-left"
                >
                  <span className="text-xs text-muted uppercase tracking-wider font-sans">
                    Low accuracy patterns (&lt; 70%)
                  </span>
                  <span className="font-mono text-xs text-accent">
                    {accuracyOpen ? "Hide" : "Show"}
                  </span>
                </button>
                <div
                  className="overflow-hidden transition-all duration-300"
                  style={{ maxHeight: accuracyOpen ? "420px" : "0px", opacity: accuracyOpen ? 1 : 0 }}
                >
                  <div className="pt-3 space-y-2">
                    {accuracyLoading && (
                      <div className="font-mono text-sm text-muted animate-pulse">
                        Loading pattern accuracy...
                      </div>
                    )}
                    {!accuracyLoading && lowAccuracy.length === 0 && (
                      <p className="text-sm text-muted">No low accuracy patterns yet.</p>
                    )}
                    {!accuracyLoading &&
                      lowAccuracy.map((pattern, index) => (
                        <div
                          key={`${pattern.keyword}-${index}`}
                          className="grid grid-cols-[1fr_auto_auto_auto] gap-3 rounded border border-border bg-bg px-3 py-2 text-xs"
                        >
                          <span className="font-mono text-accent truncate">{pattern.keyword}</span>
                          <span className="text-text">{pattern.tech}</span>
                          <span className="font-mono text-amber">{pct(pattern.accuracy)}</span>
                          <span className="font-mono text-muted">{pattern.fires ?? pattern.samples} samples</span>
                        </div>
                      ))}
                  </div>
                </div>
              </section>
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}

function Metric({ label, value }) {
  return (
    <div className="rounded border border-border bg-bg px-3 py-2">
      <div className="text-xs text-muted uppercase tracking-wider">{label}</div>
      <div className="mt-1 text-sm text-text">{value}</div>
    </div>
  );
}
