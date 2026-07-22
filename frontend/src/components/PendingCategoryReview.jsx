import { useEffect, useState } from "react";
import { API_BASE } from "../config/api";

const FALLBACK_CATEGORIES = [
  "languages", "frameworks", "databases", "messaging",
  "ai_ml", "infra", "testing", "library",
].map((id) => ({ id, label: id.replace(/_/g, " ") }));

export default function PendingCategoryReview({ triggered = [], onToast }) {
  const [pending, setPending] = useState([]);
  const [mergeTargets, setMergeTargets] = useState({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [categories, setCategories] = useState(FALLBACK_CATEGORIES);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [pendingResponse, taxonomyResponse] = await Promise.all([
          fetch(`${API_BASE}/api/dep-categories/pending`),
          fetch(`${API_BASE}/api/taxonomy/categories`),
        ]);
        if (!pendingResponse.ok) throw new Error(`Category review failed (${pendingResponse.status})`);
        const pendingData = await pendingResponse.json();
        const taxonomyData = taxonomyResponse.ok ? await taxonomyResponse.json() : null;
        if (!cancelled) {
          setPending(pendingData.pending ?? []);
          setCategories(taxonomyData?.categories?.length ? taxonomyData.categories : FALLBACK_CATEGORIES);
        }
      } catch (requestError) {
        if (!cancelled) setError(requestError.message || "Category review failed");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  async function submit(category, action) {
    setError("");
    try {
      const body = { action };
      if (action === "merge") body.merge_into = mergeTargets[category] ?? "library";
      const response = await fetch(
        `${API_BASE}/api/dep-categories/${encodeURIComponent(category)}/feedback`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      );
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Category ${action} failed (${response.status}): ${detail}`);
      }
      setPending((current) => current.filter((item) => (item._id ?? item.category) !== category));
      onToast?.(`${category} ${action === "promote" ? "promoted" : action === "merge" ? "merged" : "discarded"} — future analyses will use this decision.`, "success");
    } catch (requestError) {
      setError(requestError.message || `Category ${action} failed`);
    }
  }

  if (!loading && !error && pending.length === 0 && triggered.length === 0) return null;

  return (
    <section className="rounded-lg border border-border bg-surface px-4 py-3">
      <div className="text-xs font-medium uppercase tracking-wider text-muted">Category review</div>
      <p className="mt-1 text-xs text-muted">Decisions apply to the next analysis, not this stored result.</p>
      {triggered.length > 0 && (
        <p className="mt-2 text-xs text-ai-purple">Triggered here: {triggered.join(", ")}</p>
      )}
      {loading && <p className="mt-3 text-sm text-muted">Loading pending categories…</p>}
      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
      {!loading && pending.length === 0 && !error && (
        <p className="mt-3 text-sm text-muted">No categories awaiting review.</p>
      )}
      <div className="mt-3 space-y-3">
        {pending.map((item) => {
          const category = item._id ?? item.category;
          return (
            <div key={category} className="rounded border border-border bg-bg px-3 py-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <span className="font-mono text-sm text-text">{category}</span>
                  <span className="ml-2 text-xs text-muted">seen {item.sightings ?? item.seen_count ?? 0} times</span>
                  {item.last_example_tech && <span className="ml-2 text-xs text-muted">example: {item.last_example_tech}</span>}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <button onClick={() => submit(category, "promote")} className="rounded border border-green/30 bg-green/10 px-2 py-1 text-xs text-green">Promote</button>
                  <select
                    value={mergeTargets[category] ?? "library"}
                    onChange={(event) => setMergeTargets((current) => ({ ...current, [category]: event.target.value }))}
                    className="rounded border border-border bg-surface px-2 py-1 text-xs text-text"
                  >
                    {categories.map((category) => <option key={category.id} value={category.id}>{category.label}</option>)}
                  </select>
                  <button onClick={() => submit(category, "merge")} className="rounded border border-accent/30 bg-accent/10 px-2 py-1 text-xs text-accent">Merge</button>
                  <button onClick={() => submit(category, "discard")} className="rounded border border-red-400/30 bg-red-400/10 px-2 py-1 text-xs text-red-400">Discard</button>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
