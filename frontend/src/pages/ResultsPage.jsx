import { useState, useEffect } from "react";
import { useParams, useLocation, useNavigate, Link } from "react-router-dom";
import { GitFork as Github, RefreshCw } from "lucide-react";
import StackPanel from "../components/StackPanel";
import ExplainabilityDrawer from "../components/ExplainabilityDrawer";
import ChatPanel from "../components/ChatPanel";
import DomainFeedbackBar from "../components/DomainFeedbackBar";
import FeedbackToast from "../components/FeedbackToast";
import LearningStatsDrawer from "../components/LearningStatsDrawer";
import SimilarReposCard from "../components/SimilarReposCard";
import StackAccuracyPanel from "../components/StackAccuracyPanel";
import PendingCategoryReview from "../components/PendingCategoryReview";
import { API_BASE } from "../config/api";
import { DEMO_RESULT } from "../data/demoResult";

const DEMO_ID = "demo-stacksniffer-v1";

const SNIPPETS = {
  curl: (id) => `curl ${API_BASE}/api/analyze/${id}`,
  python: (id) =>
    `import httpx\nr = httpx.get("${API_BASE}/api/analyze/${id}")\ndata = r.json()`,
  node: (id) =>
    `const r = await fetch('${API_BASE}/api/analyze/${id}');\nconst data = await r.json();`,
  similar: (_id, domain) =>
    `curl ${API_BASE}/api/analyses/domain/${domain ?? "{domain}"}`,
};

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
  const { analysisId } = useParams();
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
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [learningOpen, setLearningOpen] = useState(false);
  const [toasts, setToasts] = useState([]);
  const [feedbackState, setFeedbackState] = useState({});
  const [copied, setCopied] = useState(false);
  const [idCopied, setIdCopied] = useState(false);
  const [snippetLang, setSnippetLang] = useState("curl");
  const [snippetCopied, setSnippetCopied] = useState(false);
  const [hardRefreshing, setHardRefreshing] = useState(false);

  useEffect(() => {
    if (isDemo || result) return;
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/api/analyze/${analysisId}`);
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

  async function copyJson() {
    try {
      await navigator.clipboard.writeText(JSON.stringify(result, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {}
  }

  async function copyId() {
    try {
      await navigator.clipboard.writeText(analysisId);
      setIdCopied(true);
      setTimeout(() => setIdCopied(false), 2000);
    } catch {}
  }

  async function copySnippet() {
    try {
      await navigator.clipboard.writeText(SNIPPETS[snippetLang](analysisId, result?.stack?.domain));
      setSnippetCopied(true);
      setTimeout(() => setSnippetCopied(false), 2000);
    } catch {}
  }

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
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!res.ok) throw new Error(`Stack feedback failed (${res.status})`);
    return res.json();
  }

  async function handleTechCorrect(techName) {
    const previous = feedbackState[techName] ?? null;
    setFeedbackState((state) => ({ ...state, [techName]: "pending" }));
    try {
      await postStackFeedback(
        `${API_BASE}/api/stack-feedback/${analysisId}/tech/${encodeURIComponent(techName)}/correct`
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
        `${API_BASE}/api/stack-feedback/${analysisId}`,
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
    typeof stack.domain_reasoning === "string" &&
    stack.domain_reasoning.toLowerCase().startsWith("trained classifier:");

  return (
    <div className="min-h-screen bg-bg flex flex-col">
      <header className="fixed top-0 left-0 right-0 z-50 border-b border-border bg-bg/95 backdrop-blur-sm px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M10 2v7.527a2 2 0 0 1-.211.896L4.72 17.8" />
            <path d="M10 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
            <path d="M14 2v4a2 2 0 0 0 2 2h4" />
            <path d="M14 17.8c-1.1 2-3.33 2.67-5 1.2-1.67-1.47-1.67-3.93 0-5.4l3-2.6" />
          </svg>
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-sm font-semibold text-accent tracking-tight">StackSniffer</span>
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

      <main className="flex-1 px-6 pt-20 pb-8 max-w-3xl mx-auto w-full space-y-4">
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
              <code className="font-mono">GEMINI_API_KEY</code> to enable domain classification.
            </span>
          </div>
        )}

        {!isDemo && !aiUnavailable && !aiClassificationUsed && (
          <div className="flex items-center gap-2 px-4 py-2.5 bg-amber/10 border border-amber/25 rounded text-amber text-xs font-mono">
            <span className="w-1.5 h-1.5 rounded-full bg-amber shrink-0" />
            Pattern detection only — AI classification timed out or was skipped
          </div>
        )}

        <StackPanel
          stack={stack}
          repo={repo}
          analysisId={analysisId}
          feedbackState={feedbackState}
          onTechCorrect={handleTechCorrect}
          onTechWrong={handleTechWrong}
          onMissingTech={handleMissingTech}
          onPrimaryLanguageChange={handlePrimaryLanguageChange}
          afterInsights={
            <>
              <DomainFeedbackBar
                analysisId={analysisId}
                currentDomain={stack.domain}
                domainConfidence={stack.domain_confidence}
                ragInfluenced={Boolean(stack.rag_influenced || stack.rag_repos_retrieved)}
                similarReposUsed={stack.similar_repos_used ?? stack.rag_repos_retrieved ?? 0}
                classifierUsed={classifierUsed}
                detectedTechs={feedbackTechs}
                onToast={showToast}
              />
              <SimilarReposCard analysisId={analysisId} />
            </>
          }
        />

        <PendingCategoryReview
          triggered={result.emergent_categories ?? stack.emergent_categories ?? []}
          onToast={showToast}
        />

        <div>
          <div className="flex items-center gap-2 flex-wrap">
          <button
            onClick={() => setDrawerOpen((v) => !v)}
            className="text-sm text-accent hover:text-accent/80 transition-colors font-sans"
          >
            {drawerOpen ? "Hide detection details ↑" : "How was this detected? ↓"}
          </button>
            <span className="text-sm text-muted">·</span>
            <button
              onClick={() => setLearningOpen(true)}
              className="text-sm text-accent hover:text-accent/80 transition-colors font-sans"
            >
              Learning stats
            </button>
          </div>
          <ExplainabilityDrawer
            isOpen={drawerOpen}
            onClose={() => setDrawerOpen(false)}
            analysisId={analysisId}
            demoData={isDemo ? { ...stack, analysis_id: analysisId } : null}
          />
          <LearningStatsDrawer open={learningOpen} onClose={() => setLearningOpen(false)} />
        </div>

        <ChatPanel
          analysisId={analysisId}
          stack={stack}
          repoName={repo?.full_name}
        />

        <div className="bg-surface border border-border rounded-lg overflow-hidden">
          <div className="px-5 py-3 border-b border-border flex items-center gap-2">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
              <polyline points="15 3 21 3 21 9" />
              <line x1="10" x2="21" y1="14" y2="3" />
            </svg>
            <span className="text-sm font-medium text-text">Use this analysis</span>
          </div>

          <div className="px-5 py-4 space-y-4">
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-xs text-muted uppercase tracking-wider font-sans">Analysis ID</span>
                <button
                  onClick={copyId}
                  className="text-xs text-muted hover:text-text transition-colors font-mono"
                >
                  {idCopied ? "Copied!" : "Copy"}
                </button>
              </div>
              <code className="block font-mono text-sm text-accent break-all bg-bg border border-border rounded px-3 py-2">
                {analysisId}
              </code>
            </div>

            <div>
              <div className="flex items-center justify-between mb-2">
                <div className="flex gap-1 flex-wrap">
                  {["curl", "python", "node", "similar"].map((lang) => (
                    <button
                      key={lang}
                      onClick={() => setSnippetLang(lang)}
                      className={`text-xs font-mono px-2.5 py-1 rounded transition-colors ${
                        snippetLang === lang
                          ? "bg-accent/15 text-accent border border-accent/30"
                          : "text-muted hover:text-text border border-transparent"
                      }`}
                    >
                      {lang === "similar" ? "find similar" : lang}
                    </button>
                  ))}
                </div>
                <button
                  onClick={copySnippet}
                  className="text-xs text-muted hover:text-text transition-colors font-mono"
                >
                  {snippetCopied ? "Copied!" : "Copy"}
                </button>
              </div>
              <pre className="bg-bg border border-border rounded px-3 py-3 font-mono text-xs text-text overflow-x-auto whitespace-pre-wrap leading-relaxed">
                {SNIPPETS[snippetLang](analysisId, result?.stack?.domain)}
              </pre>
            </div>

            <div className="flex items-center justify-between">
              <p className="text-xs text-muted font-sans">
                Pass this request_id to genREADME or any tool that needs stack context
              </p>
              {!isDemo && (
                health?.storage === "memory" ? (
                  <span className="text-xs font-mono text-amber border border-amber/30 bg-amber/10 px-2 py-0.5 rounded">
                    In-memory storage — analyses lost on restart
                  </span>
                ) : health?.storage === "mongodb" ? (
                  <span className="text-xs font-mono text-muted">
                    Cached in MongoDB · expires in 7 days
                  </span>
                ) : null
              )}
            </div>
          </div>
        </div>

        <StackAccuracyPanel />

        <div className="flex items-center justify-between pt-1">
          <button
            onClick={copyJson}
            className="flex items-center gap-2 text-sm text-muted hover:text-text border border-border hover:border-muted px-3 py-2 rounded transition-colors font-sans"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect width="8" height="4" x="8" y="2" rx="1" ry="1" />
              <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
            </svg>
            {copied ? "Copied!" : "Copy analysis JSON"}
          </button>
          <span className="font-mono text-xs text-muted">
            {new Date().toISOString().slice(0, 10)}
          </span>
        </div>
      </main>

      <footer className="border-t border-border px-6 py-4 text-center font-mono text-xs text-muted">
        StackSniffer v1.0 · Detection engine only · genREADME calls this API
      </footer>

      <div className="fixed bottom-6 right-6 z-[90] flex flex-col items-end gap-2 pointer-events-none">
        {toasts.map((toast) => (
          <FeedbackToast
            key={toast.id}
            message={toast.message}
            type={toast.type}
            onDone={() => removeToast(toast.id)}
          />
        ))}
      </div>
    </div>
  );
}
