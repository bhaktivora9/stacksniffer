import { useState, useEffect } from "react";
import { useParams, useLocation, useNavigate, Link } from "react-router-dom";
import { GitFork as Github, RefreshCw } from "lucide-react";
import StackPanel from "../components/StackPanel";
import ChatPanel from "../components/ChatPanel";
import SoftwareTypeFeedbackBar from "../components/SoftwareTypeFeedbackBar";
import FeedbackToast from "../components/FeedbackToast";
import AppShell from "../components/AppShell";
import ProvenanceResult from "../components/ProvenanceResult";
import TopNavigation from "../components/TopNavigation";
import {
  API_BASE,
  adminFetch,
  clearAdminAccessToken,
  getAdminAccessToken,
  subscribeAdminAuth,
} from "../config/api";
import { DEMO_RESULT } from "../data/demoResult";

const DEMO_ID = "demo-stacksniffer-v1";

function useHealthStatus() {
  const [health, setHealth] = useState(null);
  useEffect(() => {
    fetch(`${API_BASE}/api/health`)
      .then((r) => r.json())
      .then(setHealth)
      .catch(() => null);
  }, []);
  return health;
}

export default function ResultsPage() {
  const { analysisId: routeAnalysisId } = useParams();
  let analysisId = routeAnalysisId || "";
  try {
    analysisId = decodeURIComponent(analysisId);
  } catch {
    // Keep the original value when a malformed percent sequence is supplied.
  }
  const location = useLocation();
  const navigate = useNavigate();
  const health = useHealthStatus();

  const isDemo = analysisId === DEMO_ID;

  const [result, setResult] = useState(() => {
    if (isDemo) return DEMO_RESULT;
    return location.state?.result ?? null;
  });
  const [loading, setLoading] = useState(!isDemo && !location.state?.result);
  const [error, setError] = useState(null);
  const [toasts, setToasts] = useState([]);
  const [feedbackState, setFeedbackState] = useState({});
  const [hardRefreshing, setHardRefreshing] = useState(false);
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    if (isDemo || result) return;
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/api/analyze/${encodeURIComponent(analysisId)}`);
        if (!res.ok) throw new Error(`Analysis not found (${res.status})`);
        const data = await res.json();
        setResult(data);
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    })();
  }, [analysisId, isDemo]);

  useEffect(() => {
    let cancelled = false;

    function checkAdminSession() {
      if (!getAdminAccessToken()) {
        setIsAdmin(false);
        return;
      }
      adminFetch("/api/review/session")
        .then((res) => {
          if (!res.ok) throw new Error("guest");
          return res.json();
        })
        .then((session) => {
          if (!cancelled) setIsAdmin(session.role === "admin");
        })
        .catch(() => {
          clearAdminAccessToken();
          if (!cancelled) setIsAdmin(false);
        });
    }

    checkAdminSession();
    const unsubscribe = subscribeAdminAuth(checkAdminSession);
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  useEffect(() => {
    if (!result?.stack) return;
    const nextState = {};
    for (const techs of Object.values(result.stack)) {
      if (!Array.isArray(techs)) continue;
      for (const tech of techs) {
        if (!tech?.name) continue;
        nextState[tech.name] = null;
      }
    }
    setFeedbackState(nextState);
  }, [result?.request_id, result?.repo_key, result?.stack]);

  function repoUrlForRefresh() {
    if (result?.repo?.html_url) return result.repo.html_url;
    if (result?.repo_key?.startsWith("github:")) {
      return `https://github.com/${result.repo_key.slice("github:".length)}`;
    }
    return null;
  }

  async function handleHardRefresh() {
    const repoUrl = repoUrlForRefresh();
    if (!repoUrl) {
      showToast("Repo URL unavailable for hard refresh", "error");
      return;
    }

    setHardRefreshing(true);
    try {
      const res = await fetch(`${API_BASE}/api/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_url: repoUrl, hard_refresh: true }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data?.detail || `Hard refresh failed (${res.status})`);
      }
      const data = await res.json();
      setResult(data);
      navigate(`/results/${data.request_id || data.analysis_id}`, {
        replace: true,
        state: { result: data },
      });
      showToast("Analysis refreshed", "success");
    } catch (err) {
      showToast(err.message || "Hard refresh failed", "error");
    } finally {
      setHardRefreshing(false);
    }
  }

  function showToast(message, type = "success") {
    const id = `${Date.now()}-${Math.random()}`;
    setToasts((items) => [...items, { id, message, type }]);
  }

  function removeToast(id) {
    setToasts((items) => items.filter((toast) => toast.id !== id));
  }

  async function postStackFeedback(url, options = {}) {
    const res = await adminFetch(url, {
      method: "POST",
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const detail = Array.isArray(body.detail)
        ? body.detail.map((item) => item.msg).filter(Boolean).join("; ")
        : body.detail;
      throw new Error(detail || `Stack feedback failed (${res.status})`);
    }
    return res.json();
  }

  async function handleTechCorrect(techName, currentRole) {
    const previous = feedbackState[techName] ?? null;
    setFeedbackState((state) => ({ ...state, [techName]: "pending" }));
    try {
      await postStackFeedback(
        currentRole
          ? `${API_BASE}/api/stack-feedback/${analysisId}/tech/${encodeURIComponent(techName)}/role`
          : `${API_BASE}/api/stack-feedback/${analysisId}/tech/${encodeURIComponent(techName)}/correct`,
        currentRole
          ? { body: JSON.stringify({ current_role: currentRole, correct: true }) }
          : {}
      );
      setFeedbackState((state) => ({ ...state, [techName]: "correct" }));
      showToast(`${techName} confirmed`, "success");
      return true;
    } catch (err) {
      setFeedbackState((state) => ({ ...state, [techName]: previous }));
      showToast(err.message || "Stack feedback failed", "error");
      return false;
    }
  }

  async function handleSoftwareTypeFeedback(correct, correctedType = null) {
    try {
      await postStackFeedback(`${API_BASE}/api/feedback/${encodeURIComponent(analysisId)}`, {
        body: JSON.stringify({ software_type_correct: correct, correct_software_type: correctedType }),
      });
      showToast(correct ? "Software type confirmed" : `Software type correction submitted: ${correctedType}`, correct ? "success" : "warn");
      return true;
    } catch (err) {
      showToast(err.message || "Software type feedback failed", "error");
      return false;
    }
  }

  async function handleTechRoleCorrection(techName, currentRole, correctedRole, reason) {
    const previous = feedbackState[techName] ?? null;
    setFeedbackState((state) => ({ ...state, [techName]: "pending" }));
    try {
      await postStackFeedback(
        `${API_BASE}/api/stack-feedback/${analysisId}/tech/${encodeURIComponent(techName)}/role`,
        { body: JSON.stringify({ current_role: currentRole, correct: false, corrected_role: correctedRole, reason }) }
      );
      setFeedbackState((state) => ({ ...state, [techName]: "correction_pending" }));
      showToast(
        `${techName}: ${currentRole} → ${correctedRole} queued for maintainer review`,
        "success",
      );
      return true;
    } catch (err) {
      setFeedbackState((state) => ({ ...state, [techName]: previous }));
      showToast(err.message || "Technology role correction failed", "error");
      return false;
    }
  }

  async function handleLayerCorrection(techName, currentLayer, correctedLayer, reason) {
    const previous = feedbackState[techName] ?? null;
    setFeedbackState((state) => ({ ...state, [techName]: "pending" }));
    try {
      await postStackFeedback(
        `${API_BASE}/api/stack-feedback/${analysisId}/tech/${encodeURIComponent(techName)}/layer`,
        {
          body: JSON.stringify({
            current_layer: currentLayer,
            corrected_layer: correctedLayer,
            reason,
          }),
        },
      );
      setFeedbackState((state) => ({ ...state, [techName]: "correction_pending" }));
      showToast(
        `${techName}: ${currentLayer || "unassigned"} → ${correctedLayer} queued for maintainer review`,
        "success",
      );
      return true;
    } catch (err) {
      setFeedbackState((state) => ({ ...state, [techName]: previous }));
      showToast(err.message || "Architectural layer correction failed", "error");
      return false;
    }
  }

  async function handleTechWrong(techName, reason) {
    const previous = feedbackState[techName] ?? null;
    setFeedbackState((state) => ({ ...state, [techName]: "pending" }));
    try {
      await postStackFeedback(
        `${API_BASE}/api/stack-feedback/${analysisId}/tech/${encodeURIComponent(techName)}/wrong`,
        { body: JSON.stringify({ reason }) }
      );
      setFeedbackState((state) => ({ ...state, [techName]: "false_positive" }));
      showToast(`${techName} flagged as false positive`, "warn");
      return true;
    } catch (err) {
      setFeedbackState((state) => ({ ...state, [techName]: previous }));
      showToast(err.message || "Stack feedback failed", "error");
      return false;
    }
  }

  async function handleMissingTech(missingTechs) {
    try {
      await postStackFeedback(
        `${API_BASE}/api/stack-feedback/by-id/${analysisId}`,
        { body: JSON.stringify({ tech_evaluations: [], missing_techs: missingTechs }) }
      );
      showToast(
        `${missingTechs.length} missing technolog${missingTechs.length === 1 ? "y" : "ies"} reported`,
        "success"
      );
      return true;
    } catch (err) {
      showToast(err.message || "Missing tech report failed", "error");
      return false;
    }
  }

  async function handlePrimaryLanguageChange(primaryLanguage) {
    try {
      await postStackFeedback(`${API_BASE}/api/stack-feedback/${analysisId}/primary-language`, {
        body: JSON.stringify({ primary_language: primaryLanguage }),
      });
      setResult((current) => ({
        ...current,
        stack: { ...current.stack, primary_language: primaryLanguage },
      }));
      showToast(`Primary language corrected to ${primaryLanguage}`, "success");
      return true;
    } catch (err) {
      showToast(err.message || "Primary language correction failed", "error");
      return false;
    }
  }

  const healthDot = health?.status === "ok" ? "bg-green" : "bg-amber";
  const healthColor = health?.status === "ok" ? "text-green" : "text-amber";

  if (loading) {
    return (
      <div className="min-h-screen bg-bg flex items-center justify-center">
        <div className="font-mono text-sm text-muted animate-pulse">Loading analysis…</div>
      </div>
    );
  }

  if (error || !result) {
    return (
      <div className="min-h-screen bg-bg flex flex-col items-center justify-center gap-4 px-6">
        <p className="text-red-400 text-sm font-sans">{error ?? "Analysis not found"}</p>
        <Link to="/" className="text-accent text-sm hover:underline font-sans">
          ← Analyze a new repository
        </Link>
      </div>
    );
  }

  const { repo, stack } = result;
  const aiUnavailable = health && !health.ai_enabled;
  const aiClassificationUsed = stack.ai_classification_used;
  const feedbackTechs = [...(stack.frameworks ?? []), ...(stack.ai_ml ?? [])];
  const classifierUsed =
    typeof stack.software_type_reasoning === "string" &&
    stack.software_type_reasoning.toLowerCase().startsWith("trained classifier:");

  return (
    <AppShell bare
      health={health}
      processingTime={stack.processing_time_ms}
      actions={!isDemo ? (
        <button
          onClick={handleHardRefresh}
          disabled={hardRefreshing}
          className="inline-flex items-center gap-1.5 rounded border border-border/60 bg-surface/60 px-2.5 py-1.5 text-[11px] text-muted transition hover:border-accent/50 hover:text-accent disabled:cursor-wait disabled:opacity-60"
          title="Re-run analysis without using the cached result"
        >
          <RefreshCw size={13} className={hardRefreshing ? "animate-spin" : ""} />
          <span className="hidden sm:inline">{hardRefreshing ? "Refreshing" : "Hard refresh"}</span>
        </button>
      ) : null}
    >
      <TopNavigation health={health} actions={!isDemo ? <button type="button" className="rbtn" onClick={handleHardRefresh} disabled={hardRefreshing}><RefreshCw size={13} className={hardRefreshing ? "animate-spin" : ""} /> {hardRefreshing ? "Refreshing" : "Hard refresh"}</button> : null} />
      <header className="result-header result-header-legacy">
        <div className="flex items-center gap-3">
          <svg className="hidden" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M10 2v7.527a2 2 0 0 1-.211.896L4.72 17.8" />
            <path d="M10 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
            <path d="M14 2v4a2 2 0 0 0 2 2h4" />
            <path d="M14 17.8c-1.1 2-3.33 2.67-5 1.2-1.67-1.47-1.67-3.93 0-5.4l3-2.6" />
          </svg>
          <div className="flex items-baseline gap-2">
            <span className="result-brand">Stack<b>Sniffer</b></span>
            <span className="font-mono text-[11px] text-muted hidden sm:block">// stack detection engine</span>
          </div>
        </div>
        <div className="flex items-center gap-4">
          {health && (
            <div className="flex items-center gap-1.5">
              <span className={`w-1.5 h-1.5 rounded-full ${healthDot}`} />
              <span className={`font-mono text-[11px] ${healthColor}`}>
                API: {health.status}
              </span>
            </div>
          )}
          <a
            href="/review"
            className="text-sm text-muted hover:text-text transition-colors font-sans"
          >
            Review queue
          </a>
          <span className="text-xs text-muted font-mono hidden sm:block">
            {stack.processing_time_ms}ms
          </span>
          {!isDemo && (
            <button
              onClick={handleHardRefresh}
              disabled={hardRefreshing}
              className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-text disabled:opacity-60 disabled:cursor-wait transition-colors font-sans"
            >
              <RefreshCw size={14} className={hardRefreshing ? "animate-spin" : ""} />
              {hardRefreshing ? "Refreshing" : "Hard refresh"}
            </button>
          )}
          <button
            onClick={() => navigate("/")}
            className="text-sm text-muted hover:text-text transition-colors font-sans"
          >
            ← New analysis
          </button>
          <a
            href="https://github.com/bhaktivora9/stacksniffer"
            target="_blank"
            rel="noopener noreferrer"
            className="text-muted hover:text-text transition-colors"
            aria-label="GitHub repository"
          >
            <Github size={16} />
          </a>
        </div>
      </header>

      <main className="result-main">
        {isDemo && (
          <div className="flex items-center gap-2 px-4 py-2.5 bg-accent/10 border border-accent/20 rounded text-xs font-mono text-accent">
            <span className="w-1.5 h-1.5 rounded-full bg-accent shrink-0" />
            Demo mode — bhaktivora9/stacksniffer · all AI fields populated · no API key required
          </div>
        )}

        {!isDemo && aiUnavailable && (
          <div className="flex items-start gap-2 px-4 py-3 bg-amber/10 border border-amber/25 rounded text-amber text-xs font-sans">
            <span className="shrink-0 font-bold mt-px">!</span>
            <span>
              AI pipeline unavailable — showing pattern detection only. Add{" "}
              <code className="font-mono">GEMINI_API_KEY</code> to enable software_type classification.
            </span>
          </div>
        )}

        {!isDemo && !aiUnavailable && !aiClassificationUsed && (
          <div className="flex items-center gap-2 px-4 py-2.5 bg-amber/10 border border-amber/25 rounded text-amber text-xs font-mono">
            <span className="w-1.5 h-1.5 rounded-full bg-amber shrink-0" />
            Pattern detection only — AI classification timed out or was skipped
          </div>
        )}

        <ProvenanceResult result={result} feedbackState={feedbackState} isAdmin={isAdmin} onSoftwareTypeFeedback={handleSoftwareTypeFeedback} onTechCorrect={handleTechCorrect} onTechWrong={handleTechWrong} onRoleCorrection={handleTechRoleCorrection} onLayerCorrection={handleLayerCorrection} />

        <div className="hidden"><StackPanel
          stack={stack}
          repo={repo}
          repositoryClassification={result.repository_classification}
          analysisId={analysisId}
          feedbackState={feedbackState}
          onTechCorrect={handleTechCorrect}
          onTechWrong={handleTechWrong}
          onTechRoleCorrection={handleTechRoleCorrection}
          onLayerCorrection={handleLayerCorrection}
          onMissingTech={handleMissingTech}
          onPrimaryLanguageChange={handlePrimaryLanguageChange}
          isAdmin={isAdmin}
          afterInsights={
            <>
              <SoftwareTypeFeedbackBar
                analysisId={analysisId}
                currentSoftwareType={stack.software_type}
                pipelineSoftwareType={result.software_type?.pipeline ?? stack.software_type}
                aiSoftwareType={result.software_type?.ai?.value ?? stack.software_type_ai ?? stack.software_type}
                aiReasoning={result.software_type?.ai?.reasoning ?? stack.software_type_ai_reasoning ?? stack.software_type_reasoning}
                specificIdentity={stack.specific_identity}
                softwareTypeOverlay={result.software_type_overlay}
                softwareTypeConfidence={stack.software_type_confidence}
                ragInfluenced={Boolean(stack.rag_influenced || stack.rag_repos_retrieved)}
                similarReposUsed={stack.similar_repos_used ?? stack.rag_repos_retrieved ?? 0}
                classifierUsed={classifierUsed}
                detectedTechs={feedbackTechs}
                isAdmin={isAdmin}
                onToast={showToast}
              />
            </>
          }
        /></div>

      </main>

        <footer className="border-t border-border px-6 py-4 text-center font-mono text-xs text-muted">
        StackSniffer v1.0 · Detection engine only · genREADME calls this API
      </footer>

      <ChatPanel
        analysisId={analysisId}
        stack={stack}
        repoName={repo?.full_name}
        floating
      />

      <div className="fixed bottom-24 right-6 z-[90] flex flex-col items-end gap-2 pointer-events-none">
        {toasts.map((toast) => (
          <FeedbackToast
            key={toast.id}
            message={toast.message}
            type={toast.type}
            onDone={() => removeToast(toast.id)}
          />
        ))}
      </div>
    </AppShell>
  );
}
