import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  API_BASE,
  adminFetch,
  clearAdminAccessToken,
  getAdminAccessToken,
  setAdminAccessToken,
} from "../config/api";
import FeedbackToast from "../components/FeedbackToast";
import AppShell from "../components/AppShell";

const TABS = [
  { id: "correction", label: "Corrections" },
  { id: "emergent_taxonomy", label: "Emergent Taxonomy" },
];

function normalizeRepoUrl(repoName) {
  if (!repoName) return null;
  if (repoName.startsWith("http://") || repoName.startsWith("https://")) {
    return repoName;
  }
  return `https://github.com/${repoName}`;
}

function EvidenceBlock({ evidence, seenCount: itemSeenCount }) {
  const repo = evidence?.repo || "unknown";
  const analysisId = evidence?.analysis_id || "unknown";
  const source = evidence?.source || "pipeline";
  const seenCount = itemSeenCount ?? evidence?.seen_count ?? 0;
  const example = evidence?.example;

  return (
    <div className="space-y-1 text-xs text-muted">
      <div className="font-mono">source: {source}</div>
      <div>seen {seenCount} time{seenCount === 1 ? "" : "s"}</div>
      {example && <div>example: {example}</div>}
      <div>
        repo:
        <a
          className="ml-2 text-accent hover:text-accent/80 underline"
          href={normalizeRepoUrl(repo)}
          target="_blank"
          rel="noreferrer"
        >
          {repo}
        </a>
      </div>
      <div>analysis_id: <span className="font-mono">{analysisId}</span></div>
    </div>
  );
}

function ItemCard({
  item,
  selectedTarget,
  onSelectTarget,
  options,
  onApprove,
  onReject,
  onMerge,
  onDiscard,
  canMutate,
}) {
  const assignmentMethod = (item.assignment_method || "").toLowerCase();
  const deterministic = assignmentMethod === "deterministic";
  const kind = item.kind;
  const diagnosis = kind === "classifier_diagnosis";
  const isEmergent = kind === "emergent_type" || kind === "emergent_role";
  const isSpecificIdentity = kind === "specific_identity";
  const isInactiveCanonical = assignmentMethod === "inactive_canonical";
  const canMerge = isEmergent;
  const pipelineValue = item.pipeline_value || "unknown";
  const proposedValue = item.proposed_value || "unresolved";

  const rowClass = deterministic ? "border-amber/40" : "border-border";
  return (
    <div className={`rounded border ${rowClass} bg-surface p-3 space-y-3`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1 min-w-[220px]">
          <div className="text-xs uppercase tracking-wide text-muted">
            {isInactiveCanonical ? "inactive canonical role" : isSpecificIdentity ? "observed specific identity" : kind}
          </div>
          <div className="font-mono">
            <span className="text-muted">{isSpecificIdentity ? "identity:" : "pipeline:"}</span>{" "}
            <span className="text-text">{pipelineValue}</span>
          </div>
          <div className="font-mono">
            <span className="text-muted">proposed:</span>{" "}
            <span className="text-text">{proposedValue}</span>
          </div>
        </div>
        <div className="text-right">
          <div className="text-xs text-muted">assignment</div>
          <span
            className={`inline-block text-[11px] px-2 py-0.5 rounded border ${
              deterministic
                ? "border-amber/40 bg-amber/10 text-amber"
                : "border-border bg-bg text-muted"
            }`}
          >
            {assignmentMethod || "unknown"}
          </span>
          {deterministic && (
            <div className="text-[11px] text-amber mt-1">
              deterministic classifier defect
            </div>
          )}
        </div>
      </div>

      <EvidenceBlock evidence={item.evidence || {}} seenCount={item.seen_count} />

      {diagnosis && <div className="rounded border border-amber/30 bg-amber/10 px-3 py-2 text-xs text-amber">Global role contract: diagnose and fix the classifier, then prove the change against regression tests. This item cannot create a repository overlay.</div>}
      {isSpecificIdentity && <div className="rounded border border-accent/30 bg-accent/10 px-3 py-2 text-xs text-accent">Observed repeatedly across analyses. Promotion requires maintainer review and a canonical taxonomy decision.</div>}

      <div className="flex flex-wrap items-center gap-2">
        {canMerge && (
          <select
            className="h-8 rounded border border-border bg-bg px-2 text-xs text-text"
            value={selectedTarget}
            disabled={!canMutate}
            onChange={(event) => onSelectTarget(item._id, event.target.value)}
          >
            <option value="">select target...</option>
            {options.map((option) => (
              <option value={option.id} key={option.id}>
                {option.id}
              </option>
            ))}
          </select>
        )}
        {isSpecificIdentity && <button
            onClick={() => onApprove(item._id, selectedTarget)}
            disabled={!canMutate}
            title={canMutate ? undefined : "Admin login required"}
            className="px-3 py-1.5 rounded border border-accent/35 bg-accent/10 text-accent text-xs disabled:opacity-40"
          >
            Promote
          </button>}
        {!diagnosis && !isSpecificIdentity && <button
            onClick={() => onApprove(item._id, selectedTarget)}
            disabled={!canMutate}
            title={canMutate ? undefined : "Admin login required"}
            className="px-3 py-1.5 rounded border border-green/35 bg-green/10 text-green text-xs disabled:opacity-40"
          >
            {isInactiveCanonical ? "Activate" : isEmergent ? "Promote" : "Approve"}
          </button>}
        {!isEmergent && !isSpecificIdentity && (
          <button
            onClick={() => onReject(item._id)}
            disabled={!canMutate}
            title={canMutate ? undefined : "Admin login required"}
            className="px-3 py-1.5 rounded border border-red-400/35 bg-red-400/10 text-red-400 text-xs disabled:opacity-40"
          >
            Reject
          </button>
        )}
        {canMerge && (
          <button
            onClick={() => onMerge(item._id, selectedTarget)}
            disabled={!canMutate || !selectedTarget}
            title={canMutate ? undefined : "Admin login required"}
            className="px-3 py-1.5 rounded border border-accent/35 bg-accent/10 text-accent text-xs disabled:opacity-40"
          >
            Merge
          </button>
        )}
        {isEmergent && (
          <button
            onClick={() => onDiscard(item._id)}
            disabled={!canMutate}
            title={canMutate ? undefined : "Admin login required"}
            className="px-3 py-1.5 rounded border border-red-400/35 bg-red-400/10 text-red-400 text-xs disabled:opacity-40"
          >
            Discard
          </button>
        )}
      </div>
    </div>
  );
}

export default function ReviewQueuePage() {
  const [activeTab, setActiveTab] = useState("correction");
  const [items, setItems] = useState([]);
  const [softwareTypes, setSoftwareTypes] = useState([]);
  const [roles, setRoles] = useState([]);
  const [status, setStatus] = useState("pending");
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedTargets, setSelectedTargets] = useState({});
  const [toasts, setToasts] = useState([]);
  const [adminUser, setAdminUser] = useState("admin");
  const [adminPass, setAdminPass] = useState("");
  const [authenticated, setAuthenticated] = useState(false);
  const [authError, setAuthError] = useState("");

  const limit = 50;
  const filteredItems = useMemo(
    () => items.filter((item) => (
      item.status === status
      && (
        activeTab === "correction"
          ? item.kind === "correction" || item.kind === "classifier_diagnosis"
          : item.kind === "emergent_type"
            || item.kind === "emergent_role"
            || item.kind === "specific_identity"
      )
    )),
    [items, activeTab, status],
  );

  function showToast(message, type = "success") {
    const id = `${Date.now()}-${Math.random()}`;
    setToasts((current) => [...current, { id, message, type }]);
  }

  function removeToast(id) {
    setToasts((current) => current.filter((item) => item.id !== id));
  }

  async function callReviewApi(url, method = "POST", body) {
    const headers = {};
    if (body) {
      headers["Content-Type"] = "application/json";
    }

    const response = await adminFetch(url, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        clearAdminAccessToken();
        setAuthenticated(false);
        setAuthError("Admin session required");
        throw new Error("Maintainer authentication required");
      }
      const detail = (await response.json().catch(() => ({}))).detail;
      throw new Error(detail || `Request failed (${response.status})`);
    }
    return response.json();
  }

  async function refreshQueue() {
    setLoading(true);
    setError("");
    try {
      const kinds = activeTab === "correction"
        ? ["correction", "classifier_diagnosis"]
        : ["emergent_type", "emergent_role"];
      const responses = await Promise.all(kinds.map((kind) => callReviewApi(
        `/api/review/queue?kind=${kind}&status=${status}&page=${page}&limit=${limit}`,
        "GET",
      )));
      const identityResponse = activeTab === "emergent_taxonomy"
        ? await fetch(`${API_BASE}/api/taxonomy/specific_identities`)
        : null;
      const identityData = identityResponse?.ok ? await identityResponse.json() : { specific_identities: [] };
      const identityItems = (identityData.specific_identities || []).map((entry) => ({
        _id: `specific_identity:${entry.specific_identity}`,
        kind: "specific_identity",
        pipeline_value: entry.specific_identity,
        proposed_value: entry.specific_identity,
        assignment_method: "frequency_observation",
        seen_count: entry.count || 0,
        evidence: {
          source: "persisted analyses",
          seen_count: entry.count || 0,
          example: "specific_identity recurrence",
        },
        status: "pending",
      }));
      const queueItems = responses.flatMap((response) => response.items || []);
      setItems([...queueItems, ...identityItems]);
      setTotal(responses.reduce((sum, response) => sum + (response.total || 0), 0) + identityItems.length);
    } catch (err) {
      setError(err.message || "Failed to load review queue");
      setItems([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }

  async function loadTaxonomies() {
    try {
      const [softwareTypeResponse, roleResponse] = await Promise.all([
        fetch(`${API_BASE}/api/taxonomy/software_types`),
        fetch(`${API_BASE}/api/taxonomy/technology_roles`),
      ]);
      const softwareData = softwareTypeResponse.ok ? await softwareTypeResponse.json() : null;
      const roleData = roleResponse.ok ? await roleResponse.json() : null;
      setSoftwareTypes((softwareData?.software_types || []).map((entry) => ({
        id: entry.id,
      })));
      setRoles((roleData?.technology_roles || []).map((entry) => ({ id: entry.id })));
    } catch {
      setSoftwareTypes([]);
      setRoles([]);
    }
  }

  useEffect(() => {
    refreshQueue();
  }, [activeTab, status, page, authenticated]);

  useEffect(() => {
    if (!getAdminAccessToken()) {
      setAuthenticated(false);
      return;
    }
    adminFetch("/api/review/session")
      .then((response) => {
        if (!response.ok) throw new Error("guest");
        setAuthenticated(true);
      })
      .catch(() => {
        clearAdminAccessToken();
        setAuthenticated(false);
      });
  }, []);

  useEffect(() => {
    loadTaxonomies();
  }, []);

  function getTargets(kind) {
    if (kind === "emergent_role") return roles;
    return softwareTypes;
  }

  async function runAction(itemId, action, body) {
    const previous = [...items];
    setItems((current) => current.filter((item) => item._id !== itemId));
    try {
      const item = previous.find((entry) => entry._id === itemId) || {};
      const kind = item.kind;
      if (action === "approve") {
        await callReviewApi(`/api/review/${encodeURIComponent(itemId)}/approve`, "POST", body);
        await loadTaxonomies();
        showToast("Applied to knowledge base - affects future analyses");
      } else if (action === "merge") {
        await callReviewApi(`/api/review/${encodeURIComponent(itemId)}/merge`, "POST", body);
        await loadTaxonomies();
        showToast("Applied to knowledge base - affects future analyses");
      } else if (action === "reject") {
        await callReviewApi(`/api/review/${encodeURIComponent(itemId)}/reject`, "POST", body);
        showToast("review item rejected");
      } else if (action === "discard") {
        await callReviewApi(`/api/review/${encodeURIComponent(itemId)}/discard`, "POST", body);
        showToast("Emergent proposal discarded");
      }
      return true;
    } catch (err) {
      setItems(previous);
      showToast(err.message || "Action failed", "error");
      return false;
    }
  }

  function onSelectTarget(itemId, value) {
    setSelectedTargets((current) => ({ ...current, [itemId]: value }));
  }

  async function handleApprove(itemId) {
    const item = items.find((entry) => entry._id === itemId);
    if (!item) return;

    if (item.kind === "specific_identity") {
      await callReviewApi(
        `/api/taxonomy/specific_identities/${encodeURIComponent(item.pipeline_value)}/promote`,
        "POST",
      );
      showToast(`${item.pipeline_value} promoted to the software taxonomy`);
      await refreshQueue();
      return;
    }

    if (item.kind === "correction") {
      await runAction(itemId, "approve", {
        target_value: item.proposed_value || null,
      });
      return;
    }

    if (item.kind === "emergent_type" || item.kind === "emergent_role") {
      await runAction(itemId, "approve", {});
      return;
    }
  }

  async function handleReject(itemId) {
    await runAction(itemId, "reject", {});
  }

  async function handleMerge(itemId, mergeInto) {
    if (!mergeInto) {
      showToast("Select a target before merge", "warn");
      return;
    }
    await runAction(itemId, "merge", { merge_target: mergeInto });
  }

  async function handleDiscard(itemId) {
    await runAction(itemId, "discard", {});
  }

  async function handleLogin(event) {
    event.preventDefault();
    setAuthError("");
    setError("");
    try {
      const response = await fetch(`${API_BASE}/api/review/login`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ username: adminUser, password: adminPass }),
      });
      if (!response.ok) throw new Error("Invalid admin username or password");
      const session = await response.json();
      if (!session.access_token) throw new Error("Admin login did not return a bearer token");
      setAdminAccessToken(session.access_token);
      setAdminPass("");
      setPage(1);
      setAuthenticated(true);
    } catch (err) {
      setAuthError(err.message || "Authentication failed");
    }
  }

  return (
    <AppShell processingTime={total}>
    <main className="min-h-[calc(100vh-3.5rem)] bg-bg px-4 py-6 text-text md:px-8 md:py-8">
      <div className="max-w-5xl mx-auto space-y-5">
        <header className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.18em] text-green">System / Maintainer</div>
            <h1 className="text-3xl font-semibold tracking-tight text-[#d8e2ff]">Maintainer Review Queue</h1>
            <p className="text-sm text-muted">
              Review pending corrections and emergent taxonomy proposals before they are applied.
            </p>
          </div>
          <Link to="/" className="text-sm text-accent hover:text-accent/80">
              ← Home
            </Link>
        </header>

        {!authenticated && (
          <section className="app-glass rounded-xl p-5">
            <div className="mb-4">
              <h2 className="text-lg font-semibold text-text">Admin login</h2>
              <p className="text-sm text-muted">
                Queue is visible to guests. Actions require admin username and password.
              </p>
            </div>
            <form onSubmit={handleLogin} className="grid gap-3 md:grid-cols-[minmax(180px,260px)_minmax(180px,260px)_auto] md:items-end">
              <label className="text-xs text-muted">
                <span className="mb-1 block">Username</span>
                <input
                  type="text"
                  value={adminUser}
                  onChange={(event) => setAdminUser(event.target.value)}
                  autoComplete="username"
                  required
                  className="h-9 w-full rounded border border-border bg-bg px-3 font-mono text-sm text-text"
                />
              </label>
                <label className="text-xs text-muted">
                  <span className="mb-1 block">Password</span>
                  <input
                    type="password"
                    value={adminPass}
                    onChange={(event) => setAdminPass(event.target.value)}
                    autoComplete="current-password"
                    required
                    className="h-9 w-full rounded border border-border bg-bg px-3 font-mono text-sm text-text"
                  />
                </label>
                <button
                  type="submit"
                  className="h-9 rounded border border-accent/35 bg-accent/10 px-4 text-sm text-accent"
                >
                  Log in as admin
                </button>
              </form>
              {authError && <p className="mt-2 text-sm text-red-400">{authError}</p>}
              <p className="mt-2 text-xs text-muted">
                Sign-in is kept in memory for this browser tab and expires after 8 hours.
              </p>
            </section>
        )}

        {!authenticated && (
          <section className="app-glass rounded-xl p-3 text-xs uppercase tracking-wide text-muted">
            Guest view: review queue is read-only. Action controls are disabled.
          </section>
        )}

            <div className="inline-flex rounded-lg border border-border/70 bg-[#131b2e] p-1">
              {TABS.map((tab) => (
                <button
                  key={tab.id}
                  onClick={() => {
                    setActiveTab(tab.id);
                    setPage(1);
                    setSelectedTargets({});
                  }}
                  className={`px-4 py-2 text-sm ${
                    activeTab === tab.id
                      ? "bg-accent/15 text-accent"
                      : "text-muted hover:text-text"
                  }`}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            <section className="app-glass overflow-hidden rounded-xl">
              <div className="p-3 border-b border-border text-sm text-muted">
                {loading ? "Loading queue..." : `${filteredItems.length} pending item(s)`}
              </div>

              {error && (
                <p className="p-3 text-sm text-red-400">{error}</p>
              )}

              {!loading && !error && filteredItems.length === 0 && (
                <p className="p-3 text-sm text-muted">nothing awaiting review</p>
              )}

              <div className="p-3 space-y-3">
                {filteredItems.map((item) => (
                  <ItemCard
                    key={item._id}
                    item={item}
                    selectedTarget={selectedTargets[item._id] || ""}
                    onSelectTarget={onSelectTarget}
                    options={getTargets(item.kind)}
                    onApprove={handleApprove}
                    onReject={handleReject}
                  onMerge={handleMerge}
                  onDiscard={handleDiscard}
                  canMutate={authenticated}
                />
                ))}
              </div>
            </section>

            <div className="flex items-center gap-3">
              <button
                onClick={() => setPage((current) => Math.max(1, current - 1))}
                disabled={page <= 1 || loading}
                className="px-3 py-1.5 rounded border border-border text-xs disabled:opacity-50"
              >
                Prev
              </button>
              <span className="text-sm text-muted">
                Page {page}
              </span>
              <button
                onClick={() => setPage((current) => (current * limit >= total ? current : current + 1))}
                disabled={loading || page * limit >= total}
                className="px-3 py-1.5 rounded border border-border text-xs disabled:opacity-50"
              >
                Next
              </button>
            </div>

            <div className="space-x-3">
              <label className="text-sm text-muted">
                status:
                <select
                  className="ml-2 h-8 rounded border border-border bg-surface px-2 text-xs text-text"
                  value={status}
                  onChange={(event) => {
                    setStatus(event.target.value);
                    setPage(1);
                  }}
                >
                  <option value="pending">pending</option>
                  <option value="approved">approved</option>
                  <option value="rejected">rejected</option>
                </select>
              </label>
            </div>
      </div>

      <div className="fixed right-4 bottom-4 flex flex-col gap-2">
        {toasts.map((toast) => (
          <FeedbackToast
            key={toast.id}
            message={toast.message}
            type={toast.type}
            onDone={() => removeToast(toast.id)}
          />
        ))}
      </div>
    </main>
    </AppShell>
  );
}
