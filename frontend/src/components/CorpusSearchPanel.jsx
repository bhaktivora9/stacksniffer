import { useState } from "react";
import { API_BASE } from "../config/api";

const SEARCH_TYPES = [
  { id: "technology", label: "Tech stack", placeholder: "e.g. FastAPI, PostgreSQL" },
  { id: "software_type", label: "SoftwareType", placeholder: "e.g. data_pipeline" },
  { id: "technology_role", label: "TechnologyRole", placeholder: "e.g. messaging" },
];

export default function CorpusSearchPanel() {
  const [kind, setKind] = useState("technology");
  const [query, setQuery] = useState("");
  const [examples, setExamples] = useState([]);
  const [searched, setSearched] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const selectedType = SEARCH_TYPES.find((type) => type.id === kind);

  async function search(event) {
    event.preventDefault();
    const value = query.trim();
    if (!value) return;
    setLoading(true);
    setError("");
    setSearched(false);
    try {
      const params = new URLSearchParams({ kind, q: value, limit: "10" });
      const response = await fetch(`${API_BASE}/api/taxonomy/search?${params}`);
      if (!response.ok) throw new Error(`Corpus search failed (${response.status})`);
      const data = await response.json();
      setExamples(data.examples ?? []);
      setSearched(true);
    } catch (requestError) {
      setExamples([]);
      setError(requestError.message || "Corpus search failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="rounded-lg border border-border bg-surface px-4 py-3">
      <div className="text-xs font-medium uppercase tracking-wider text-muted">
        Search learned stacks
      </div>
      <p className="mt-1 text-xs text-muted">
        Find example repositories by technology combination, software_type, or technology_role.
      </p>

      <form onSubmit={search} className="mt-3 flex flex-wrap gap-2">
        <select
          value={kind}
          onChange={(event) => {
            setKind(event.target.value);
            setExamples([]);
            setSearched(false);
          }}
          className="h-9 rounded border border-border bg-bg px-3 text-sm text-text outline-none focus:border-accent"
        >
          {SEARCH_TYPES.map((type) => (
            <option key={type.id} value={type.id}>{type.label}</option>
          ))}
        </select>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={selectedType?.placeholder}
          className="h-9 min-w-[260px] flex-1 rounded border border-border bg-bg px-3 text-sm text-text outline-none focus:border-accent"
        />
        <button
          type="submit"
          disabled={loading || !query.trim()}
          className="h-9 rounded border border-accent/30 bg-accent/10 px-4 text-sm text-accent transition-colors hover:bg-accent/15 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? "Searching..." : "Search"}
        </button>
      </form>

      {kind === "technology" && (
        <p className="mt-2 text-[11px] text-muted">
          Separate technologies with commas to search for a combination.
        </p>
      )}
      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
      {searched && examples.length === 0 && (
        <div className="mt-3 rounded border border-amber/25 bg-amber/10 px-3 py-2 text-sm text-amber">
          This combination is not available yet. The system is still learning.
        </div>
      )}
      {examples.length > 0 && (
        <div className="mt-3 divide-y divide-border rounded border border-border bg-bg">
          {examples.map((example) => (
            <div key={example.repo_key || example.repo} className="px-3 py-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <a
                  href={`https://github.com/${example.repo}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="font-mono text-sm text-accent hover:underline"
                >
                  {example.repo}
                </a>
                <span className="rounded-full border border-green/25 bg-green/10 px-2 py-0.5 text-[10px] text-green">
                  {(example.software_type || "unknown").replace(/_/g, " ")}
                </span>
                {example.matched_technology_role && (
                  <span className="rounded border border-ai-purple/25 bg-ai-purple/10 px-2 py-0.5 text-[10px] text-ai-purple">
                    {example.matched_technology_role.replace(/_/g, " ")}
                  </span>
                )}
              </div>
              {example.matched_technologies?.length > 0 && (
                <p className="mt-1 text-xs text-muted">
                  {example.matched_technologies.join(", ")}
                </p>
              )}
              <div className="mt-1 flex flex-wrap gap-3 text-[11px] text-muted">
                {example.primary_language && <span>{example.primary_language}</span>}
                {example.stack_pattern && <span>{example.stack_pattern}</span>}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
