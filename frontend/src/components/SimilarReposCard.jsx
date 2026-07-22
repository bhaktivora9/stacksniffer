import { useEffect, useState } from "react";
import { API_BASE } from "../config/api";

function getNested(item, path, fallback = "") {
  return path.split(".").reduce((value, key) => value?.[key], item) ?? fallback;
}

function repoName(item) {
  const repoKey = item.repo_key || item.analysis_id || "";
  return getNested(item, "repo.full_name")
    || item.repo_name
    || item.full_name
    || repoKey.replace(/^[^:]+:/, "")
    || "unknown/repo";
}

function domain(item) {
  return getNested(item, "stack.domain") || item.domain || "unknown";
}

function stackPattern(item) {
  return getNested(item, "stack.stack_pattern") || item.stack_pattern || "Custom";
}

function whyThisStack(item) {
  return getNested(item, "stack.why_this_stack") || item.why_this_stack || item.domain_reasoning || "";
}

function truncate(text, length = 80) {
  if (!text || text.length <= length) return text;
  return `${text.slice(0, length - 3)}...`;
}

export default function SimilarReposCard({ analysisId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [hidden, setHidden] = useState(false);

  useEffect(() => {
    if (!analysisId) return;
    let cancelled = false;
    setLoading(true);
    setHidden(false);

    fetch(`${API_BASE}/api/analyses/similar/${analysisId}`)
      .then((res) => {
        if (!res.ok) throw new Error(`Similar repos failed (${res.status})`);
        return res.json();
      })
      .then((payload) => {
        if (cancelled) return;
        if (!payload?.count) setHidden(true);
        setData(payload);
      })
      .catch(() => {
        if (!cancelled) setHidden(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [analysisId]);

  if (hidden) return null;

  const method = data?.method;
  const similar = data?.similar ?? [];
  const isVector = method === "vector_search";
  const visibleRepos = expanded ? similar : similar.slice(0, 3);

  if (!loading && similar.length === 0) return null;

  return (
    <div className="bg-surface border border-border rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-border flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 rounded-full ${isVector ? "bg-teal-300" : "bg-amber"}`} />
          <span className="text-sm font-medium text-text">
            Similar repos {isVector ? "(vector search)" : "(domain match)"}
          </span>
        </div>
        {!loading && similar.length > 0 && (
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            className="text-xs font-mono text-accent hover:text-accent/80 transition-colors"
          >
            {expanded ? "Collapse" : `${similar.length} similar repos found`}
          </button>
        )}
      </div>

      <div className="divide-y divide-border">
        {loading &&
          Array.from({ length: 3 }).map((_, index) => (
            <div key={index} className="px-4 py-3 animate-pulse">
              <div className="h-3 w-44 rounded bg-border" />
              <div className="mt-2 h-2 w-full max-w-md rounded bg-border/70" />
            </div>
          ))}

        {!loading &&
          visibleRepos.map((item, index) => {
            const name = repoName(item);
            const score = Math.round((item.score ?? item.similarity ?? 0) * 100);
            const reason = truncate(whyThisStack(item));

            return (
              <div key={`${name}-${index}`} className="px-4 py-3">
                <div className="flex items-center gap-2 flex-wrap">
                  <a
                    href={`https://github.com/${name}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="font-mono text-sm text-accent hover:underline"
                  >
                    {name}
                  </a>
                  <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-green/10 text-green border border-green/25">
                    {domain(item).replace(/_/g, " ")}
                  </span>
                  <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-ai-purple/10 text-ai-purple border border-ai-purple/25">
                    {stackPattern(item)}
                  </span>
                </div>

                <div className="mt-2 flex items-center gap-3">
                  {isVector && (
                    <div className="w-20 h-1 bg-border rounded-full overflow-hidden shrink-0">
                      <div
                        className="h-full bg-teal-300 rounded-full"
                        style={{ width: `${Math.max(4, score)}%` }}
                      />
                    </div>
                  )}
                  {reason && <p className="text-xs text-muted leading-relaxed">{reason}</p>}
                </div>
              </div>
            );
          })}
      </div>
    </div>
  );
}
