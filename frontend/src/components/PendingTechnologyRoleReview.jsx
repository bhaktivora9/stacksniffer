import { useEffect, useState } from "react";
import { API_BASE } from "../config/api";

const FALLBACK_TECHNOLOGY_ROLES = [
  "languages", "frameworks", "databases", "messaging",
  "ai_ml", "infra", "testing", "library",
].map((id) => ({ id, label: id.replace(/_/g, " ") }));

export default function PendingTechnologyRoleReview({ triggered = [], onToast }) {
  const [pending, setPending] = useState([]);
  const [mergeTargets, setMergeTargets] = useState({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [technologyRoles, setTechnologyRoles] = useState(FALLBACK_TECHNOLOGY_ROLES);
  const [softwareTypes, setSoftwareTypes] = useState([]);
  const [suggestionKind, setSuggestionKind] = useState("software_type");
  const [suggestionName, setSuggestionName] = useState("");
  const [suggesting, setSuggesting] = useState(false);

  async function refreshPending() {
    const response = await fetch(`${API_BASE}/api/taxonomy/pending`);
    if (!response.ok) throw new Error(`Taxonomy review failed (${response.status})`);
    const data = await response.json();
    setPending([
      ...(data.technology_roles ?? []).map((item) => ({ ...item, kind: "technology_role" })),
      ...(data.software_types ?? []).map((item) => ({ ...item, kind: "software_type" })),
    ]);
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [pendingResponse, taxonomyResponse, softwareTypesResponse] = await Promise.all([
          fetch(`${API_BASE}/api/taxonomy/pending`),
          fetch(`${API_BASE}/api/taxonomy/technology_roles`),
          fetch(`${API_BASE}/api/taxonomy/software_types`),
        ]);
        if (!pendingResponse.ok) throw new Error(`TechnologyRole review failed (${pendingResponse.status})`);
        const pendingData = await pendingResponse.json();
        const taxonomyData = taxonomyResponse.ok ? await taxonomyResponse.json() : null;
        if (!cancelled) {
          setPending([
            ...(pendingData.technology_roles ?? []).map((item) => ({ ...item, kind: "technology_role" })),
            ...(pendingData.software_types ?? []).map((item) => ({ ...item, kind: "software_type" })),
          ]);
          setTechnologyRoles(taxonomyData?.technology_roles?.length ? taxonomyData.technology_roles : FALLBACK_TECHNOLOGY_ROLES);
          if (softwareTypesResponse.ok) {
            const software_typeData = await softwareTypesResponse.json();
            setSoftwareTypes(software_typeData.software_types ?? []);
          }
        }
      } catch (requestError) {
        if (!cancelled) setError(requestError.message || "TechnologyRole review failed");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  async function submit(kind, technology_role, action) {
    setError("");
    try {
      const body = { action };
      if (action === "merge") body.merge_into = mergeTargets[technology_role] ?? "library";
      const response = await fetch(
        `${API_BASE}/api/taxonomy/${kind}/${encodeURIComponent(technology_role)}/action`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      );
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`TechnologyRole ${action} failed (${response.status}): ${detail}`);
      }
      const refreshed = await fetch(`${API_BASE}/api/taxonomy/pending`);
      const refreshedData = refreshed.ok ? await refreshed.json() : { technology_roles: [], software_types: [] };
      setPending([
        ...(refreshedData.technology_roles ?? []).map((item) => ({ ...item, kind: "technology_role" })),
        ...(refreshedData.software_types ?? []).map((item) => ({ ...item, kind: "software_type" })),
      ]);
      onToast?.(`${technology_role} ${action === "promote" ? "promoted" : action === "merge" ? "merged" : "discarded"} — future analyses will use this decision.`, "success");
    } catch (requestError) {
      setError(requestError.message || `TechnologyRole ${action} failed`);
    }
  }

  async function suggest(event) {
    event.preventDefault();
    const name = suggestionName.trim();
    if (!name) return;
    setError("");
    setSuggesting(true);
    try {
      const response = await fetch(`${API_BASE}/api/taxonomy/pending`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: suggestionKind, name }),
      });
      if (!response.ok) throw new Error(`Suggestion failed (${response.status}): ${await response.text()}`);
      const created = await response.json();
      setSuggestionName("");
      await refreshPending();
      onToast?.(`${created.name} added for ${suggestionKind} review.`, "success");
    } catch (requestError) {
      setError(requestError.message || "Suggestion failed");
    } finally {
      setSuggesting(false);
    }
  }

  return (
    <section className="rounded-lg border border-border bg-surface px-4 py-3">
      <div className="text-xs font-medium uppercase tracking-wider text-muted">Taxonomy review</div>
      <p className="mt-1 text-xs text-muted">Decisions apply to the next analysis, not this stored result.</p>
      {triggered.length > 0 && (
        <p className="mt-2 text-xs text-ai-purple">Triggered here: {triggered.join(", ")}</p>
      )}
      <form onSubmit={suggest} className="mt-3 flex flex-wrap items-end gap-2 rounded border border-border bg-bg px-3 py-2">
        <label>
          <span className="mb-1 block text-[10px] uppercase tracking-wider text-muted">Type</span>
          <select value={suggestionKind} onChange={(event) => setSuggestionKind(event.target.value)} className="h-8 rounded border border-border bg-surface px-2 text-xs text-text">
            <option value="software_type">Software type</option>
            <option value="technology_role">Technology role</option>
          </select>
        </label>
        <label className="min-w-[260px] flex-1">
          <span className="mb-1 block text-[10px] uppercase tracking-wider text-muted">Emergent name</span>
          <input value={suggestionName} onChange={(event) => setSuggestionName(event.target.value)} placeholder={suggestionKind === "software_type" ? "e.g. observability_platform" : "e.g. build_tooling"} className="h-8 w-full rounded border border-border bg-surface px-2 text-xs text-text outline-none focus:border-accent" />
        </label>
        <button type="submit" disabled={suggesting || !suggestionName.trim()} className="h-8 rounded border border-ai-purple/30 bg-ai-purple/10 px-3 text-xs text-ai-purple disabled:opacity-50">
          {suggesting ? "Adding..." : "Add for review"}
        </button>
      </form>
      {loading && <p className="mt-3 text-sm text-muted">Loading pending technology roles…</p>}
      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
      {!loading && pending.length === 0 && !error && (
        <p className="mt-3 text-sm text-muted">No technology roles awaiting review.</p>
      )}
      <div className="mt-3 space-y-3">
        {pending.map((item) => {
          const technology_role = item._id ?? item.technology_role;
          const kind = item.kind ?? "technology_role";
          const targets = kind === "software_type" ? softwareTypes : technologyRoles;
          return (
            <div key={`${kind}:${technology_role}`} className="rounded border border-border bg-bg px-3 py-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <span className="mr-2 rounded border border-border px-1.5 py-0.5 text-[9px] uppercase text-muted">{kind}</span>
                  <span className="font-mono text-sm text-text">{technology_role}</span>
                  <span className="ml-2 text-xs text-muted">seen {item.sightings ?? item.seen_count ?? 0} times</span>
                  {item.last_example_tech && <span className="ml-2 text-xs text-muted">example: {item.last_example_tech}</span>}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <button onClick={() => submit(kind, technology_role, "promote")} className="rounded border border-green/30 bg-green/10 px-2 py-1 text-xs text-green">Promote</button>
                  <select
                    value={mergeTargets[technology_role] ?? "library"}
                    onChange={(event) => setMergeTargets((current) => ({ ...current, [technology_role]: event.target.value }))}
                    className="rounded border border-border bg-surface px-2 py-1 text-xs text-text"
                  >
                    {targets.map((target) => <option key={target.id} value={target.id}>{target.label}</option>)}
                  </select>
                  <button onClick={() => submit(kind, technology_role, "merge")} className="rounded border border-accent/30 bg-accent/10 px-2 py-1 text-xs text-accent">Merge</button>
                  <button onClick={() => submit(kind, technology_role, "discard")} className="rounded border border-red-400/30 bg-red-400/10 px-2 py-1 text-xs text-red-400">Discard</button>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
