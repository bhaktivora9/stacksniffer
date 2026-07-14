import { useEffect, useState } from "react";
import { API_BASE } from "../config/api";

export default function ExplainabilityDrawer({ isOpen, onClose, analysisId, demoData }) {
  const [data, setData] = useState(demoData ?? null);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState(null);

  useEffect(() => {
    if (!isOpen || !analysisId || data || demoData) return;
    setLoading(true);
    setFetchError(null);
    fetch(`${API_BASE}/api/explain/${analysisId}`)
      .then((r) => {
        if (!r.ok) throw new Error(`Failed to load (${r.status})`);
        return r.json();
      })
      .then((d) => setData(d))
      .catch((e) => setFetchError(e.message))
      .finally(() => setLoading(false));
  }, [isOpen, analysisId, demoData]);

  useEffect(() => {
    const handler = (e) => {
      if (e.key === "Escape") onClose?.();
    };
    if (isOpen) document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [isOpen, onClose]);

  const patternMatches = data?.pattern_matches ?? [];
  const aiInferences = data?.ai_inferences ?? [];

  return (
    <div
      className="transition-all duration-300 overflow-hidden"
      style={{ maxHeight: isOpen ? "2000px" : "0px", opacity: isOpen ? 1 : 0 }}
    >
      <div className="mt-4 bg-surface border border-border rounded-lg overflow-hidden">
        <div className="flex items-center justify-between px-5 py-3 border-b border-border">
          <span className="text-sm font-medium text-text">How was this detected?</span>
          <button
            onClick={onClose}
            className="text-muted hover:text-text transition-colors text-lg leading-none"
          >
            ×
          </button>
        </div>

        {loading && (
          <div className="px-5 py-8 text-center font-mono text-sm text-muted animate-pulse">
            Loading audit trail…
          </div>
        )}

        {fetchError && (
          <div className="px-5 py-4 text-sm text-red-400 font-sans">{fetchError}</div>
        )}

        {!loading && !fetchError && data && (
          <div className="p-5 space-y-6">
            {data.domain_reasoning && (
              <section>
                <h3 className="text-xs text-muted uppercase tracking-wider mb-2 font-sans">
                  Domain reasoning
                </h3>
                <p className="text-sm text-text leading-relaxed">{data.domain_reasoning}</p>
              </section>
            )}

            {patternMatches.length > 0 && (
              <section>
                <h3 className="text-xs text-muted uppercase tracking-wider mb-3 font-sans">
                  Pattern matches
                </h3>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-xs text-muted border-b border-border">
                        <th className="text-left pb-2 pr-4 font-normal">Tech</th>
                        <th className="text-left pb-2 pr-4 font-normal">File</th>
                        <th className="text-left pb-2 pr-4 font-normal">Keyword</th>
                        <th className="text-left pb-2 font-normal">Confidence</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {patternMatches.slice(0, 30).map((pm, i) => (
                        <tr key={i} className="hover:bg-bg transition-colors">
                          <td className="py-1.5 pr-4 text-text font-mono text-xs font-medium">
                            {pm.tech}
                          </td>
                          <td className="py-1.5 pr-4 text-muted font-mono text-xs max-w-[200px] truncate">
                            {pm.matched_file}
                          </td>
                          <td className="py-1.5 pr-4">
                            {pm.matched_keyword ? (
                              <span className="font-mono text-xs px-1.5 py-0.5 bg-bg border border-border rounded text-accent">
                                {pm.matched_keyword}
                              </span>
                            ) : (
                              <span className="text-muted text-xs">—</span>
                            )}
                          </td>
                          <td className="py-1.5">
                            <div className="flex items-center gap-2">
                              <div className="w-12 h-1 bg-border rounded-full overflow-hidden">
                                <div
                                  className="h-full bg-accent rounded-full"
                                  style={{ width: `${Math.round((pm.confidence ?? 0) * 100)}%` }}
                                />
                              </div>
                              <span className="text-xs font-mono text-muted">
                                {Math.round((pm.confidence ?? 0) * 100)}%
                              </span>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {patternMatches.length > 30 && (
                    <p className="mt-2 text-xs text-muted">
                      +{patternMatches.length - 30} more matches not shown
                    </p>
                  )}
                </div>
              </section>
            )}

            {aiInferences.length > 0 && (
              <section>
                <h3 className="text-xs text-muted uppercase tracking-wider mb-3 font-sans">
                  AI inferred
                </h3>
                <div className="space-y-2">
                  {aiInferences.map((inf, i) => (
                    <div
                      key={i}
                      className="flex items-start gap-3 px-3 py-2.5 bg-bg border border-border rounded"
                    >
                      <div className="shrink-0 pt-0.5">
                        <span className="text-[10px] font-mono font-medium px-1.5 py-0.5 rounded bg-ai-purple/15 text-ai-purple border border-ai-purple/30">
                          AI
                        </span>
                      </div>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-sm font-medium text-text">{inf.tech}</span>
                          <span className="text-xs text-muted font-mono">{inf.category}</span>
                        </div>
                        {inf.reasoning && (
                          <p className="mt-0.5 text-xs text-muted">{inf.reasoning}</p>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            )}
          </div>
        )}

        {!loading && data && (
          <div className="px-5 py-3 border-t border-border font-mono text-xs text-muted flex gap-3 flex-wrap">
            <span>{data.patterns_checked ?? 0} patterns checked</span>
            <span className="text-border">·</span>
            <span>{data.files_analyzed ?? 0} files analyzed</span>
            <span className="text-border">·</span>
            <span>{data.ai_calls_made ?? 0} Claude calls</span>
          </div>
        )}
      </div>
    </div>
  );
}
