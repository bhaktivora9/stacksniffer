import { useEffect, useState } from "react";
import { API_BASE } from "../config/api";

function percent(value) {
  return `${Math.round((value ?? 0) * 100)}%`;
}

export default function StackAccuracyPanel() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [available, setAvailable] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/stack-feedback/accuracy`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed (${res.status})`);
        return res.json();
      })
      .then((payload) => {
        if (cancelled) return;
        setData(payload);
        setAvailable(Boolean(payload));
      })
      .catch(() => {
        if (!cancelled) setAvailable(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function toggleOpen() {
    setOpen((value) => !value);
    if (data || loading) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/stack-feedback/accuracy`);
      if (!res.ok) throw new Error(`Failed (${res.status})`);
      setData(await res.json());
    } catch {
      setData({ status: "no_data" });
    } finally {
      setLoading(false);
    }
  }

  if (!available) return null;

  const reliable = data?.high_precision_techs ?? [];
  const needsImprovement = [
    ...(data?.top_false_positives ?? []),
    ...(data?.top_missed_techs ?? []),
  ]
    .filter((tech, index, items) => items.findIndex((item) => item.tech === tech.tech) === index)
    .filter((tech) => (tech.precision ?? 1) < 0.7);

  return (
    <div className="bg-surface border border-border rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={toggleOpen}
        className="w-full px-5 py-3 text-left text-sm text-accent hover:text-accent/80 transition-colors font-sans"
      >
        {open ? "Hide stack accuracy" : "Stack accuracy"}
      </button>

      <div
        className="overflow-hidden transition-all duration-150"
        style={{ maxHeight: open ? "720px" : "0px", opacity: open ? 1 : 0 }}
      >
        <div className="border-t border-border px-5 py-4">
          <h3 className="text-sm font-medium text-text">Detection accuracy from feedback</h3>

          {loading && (
            <p className="mt-3 font-mono text-sm text-muted animate-pulse">Loading accuracy...</p>
          )}

          {!loading && data?.status === "no_data" && (
            <p className="mt-3 text-sm text-muted">
              Submit feedback on tech detections to see accuracy stats
            </p>
          )}

          {!loading && data?.status !== "no_data" && (
            <div className="mt-4 grid gap-4 md:grid-cols-2">
              <AccuracyColumn title="Reliable detections">
                {reliable.length === 0 ? (
                  <p className="text-sm text-muted">No high-precision techs yet.</p>
                ) : (
                  reliable.map((tech) => (
                    <div key={tech.tech} className="flex items-center gap-2 text-xs">
                      <span className="w-24 truncate font-mono text-text">{tech.tech}</span>
                      <div className="h-1 flex-1 rounded-full bg-border overflow-hidden">
                        <div
                          className="h-full rounded-full bg-green"
                          style={{ width: percent(tech.precision) }}
                        />
                      </div>
                      <span className="w-12 text-right font-mono text-muted">
                        F1 {percent(tech.f1)}
                      </span>
                    </div>
                  ))
                )}
              </AccuracyColumn>

              <AccuracyColumn title="Needs improvement">
                {needsImprovement.length === 0 ? (
                  <p className="text-sm text-muted">No low-precision techs yet.</p>
                ) : (
                  needsImprovement.map((tech) => {
                    const issue = (tech.false_positive ?? 0) >= (tech.false_negative ?? 0) ? "FP" : "FN";
                    const count = issue === "FP" ? tech.false_positive : tech.false_negative;
                    return (
                      <div key={tech.tech} className="flex items-center gap-2 text-xs">
                        <span className="flex-1 truncate font-mono text-text">{tech.tech}</span>
                        <span className="rounded border border-amber/25 bg-amber/10 px-1.5 py-0.5 font-mono text-amber">
                          {issue}
                        </span>
                        <span className="font-mono text-muted">{count}</span>
                      </div>
                    );
                  })
                )}
              </AccuracyColumn>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function AccuracyColumn({ title, children }) {
  return (
    <section>
      <h4 className="mb-2 text-xs text-muted uppercase tracking-wider font-sans">{title}</h4>
      <div className="space-y-2">{children}</div>
    </section>
  );
}
