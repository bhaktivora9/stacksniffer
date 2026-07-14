import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { GitFork as Github } from "lucide-react";
import LoadingTerminal from "../components/LoadingTerminal";
import { API_BASE } from "../config/api";
import { DEMO_RESULT } from "../data/demoResult";

const STEPS = [
  "Fetching repository",
  "Reading file tree",
  "Running pattern detection",
  "AI domain classification",
  "Generating stack insights",
  "Analysis complete",
];

const GITHUB_URL_RE = /^https?:\/\/github\.com\/[\w.-]+\/[\w.-]+(\/.*)?$/;

const HOW_IT_WORKS = [
  {
    step: "01",
    title: "Paste any GitHub URL",
    desc: "Drop in any public GitHub repository URL — org repos, personal projects, or open-source libraries.",
  },
  {
    step: "02",
    title: "Pattern rules + AI classify your stack",
    desc: "500+ pattern rules detect languages, frameworks, and infra. Claude AI infers domain, architecture, and hidden signals.",
  },
  {
    step: "03",
    title: "Get structured JSON for any tool",
    desc: "Get structured analysis · cached in MongoDB · queryable by downstream tools via REST.",
  },
];

function useHealthStatus() {
  const [health, setHealth] = useState(null);
  useEffect(() => {
    fetch(`${API_BASE}/api/health`)
      .then((r) => r.json())
      .then(setHealth)
      .catch(() => setHealth({ status: "offline" }));
  }, []);
  return health;
}

export default function HomePage() {
  const navigate = useNavigate();
  const health = useHealthStatus();

  const [repoUrl, setRepoUrl] = useState("");
  const [urlError, setUrlError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [currentStep, setCurrentStep] = useState(0);
  const [error, setError] = useState(null);
  const [rateLimitCountdown, setRateLimitCountdown] = useState(null);

  const stepTimerRef = useRef(null);
  const inputRef = useRef(null);
  const countdownRef = useRef(null);
  const pendingRetryUrl = useRef(null);

  useEffect(() => {
    inputRef.current?.focus();
    return () => {
      clearInterval(stepTimerRef.current);
      clearInterval(countdownRef.current);
    };
  }, []);

  function validateUrl(val) {
    if (!val.trim()) return null;
    if (!GITHUB_URL_RE.test(val.trim())) return "Enter a valid GitHub repository URL";
    return null;
  }

  function handleUrlChange(e) {
    const val = e.target.value;
    setRepoUrl(val);
    if (urlError) setUrlError(validateUrl(val));
  }

  function startStepAnimation() {
    setCurrentStep(0);
    let step = 0;
    stepTimerRef.current = setInterval(() => {
      step += 1;
      if (step >= STEPS.length - 1) clearInterval(stepTimerRef.current);
      setCurrentStep(step);
    }, 800);
  }

  function startRateLimitCountdown(seconds, url) {
    setRateLimitCountdown(seconds);
    pendingRetryUrl.current = url;
    countdownRef.current = setInterval(() => {
      setRateLimitCountdown((prev) => {
        if (prev <= 1) {
          clearInterval(countdownRef.current);
          setRateLimitCountdown(null);
          runAnalysis(pendingRetryUrl.current);
          return null;
        }
        return prev - 1;
      });
    }, 1000);
  }

  async function runAnalysis(url) {
    setError(null);
    setLoading(true);
    startStepAnimation();

    try {
      const res = await fetch(`${API_BASE}/api/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_url: url }),
      });

      clearInterval(stepTimerRef.current);

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        if (res.status === 429) {
          const retryAfter = data?.detail?.retry_after ?? 60;
          setLoading(false);
          setCurrentStep(0);
          startRateLimitCountdown(retryAfter, url);
          return;
        }
        throw new Error(data?.detail || `Request failed (${res.status})`);
      }

      const result = await res.json();
      setCurrentStep(STEPS.length - 1);
      setTimeout(() => {
        navigate(`/results/${result.analysis_id}`, { state: { result } });
      }, 400);
    } catch (err) {
      clearInterval(stepTimerRef.current);
      setLoading(false);
      setCurrentStep(0);
      setError(err.message);
    }
  }

  function handleSubmit(e) {
    e?.preventDefault();
    const url = repoUrl.trim();
    const err = validateUrl(url);
    if (err || !url) {
      setUrlError(err || "Enter a GitHub repository URL");
      return;
    }
    setUrlError(null);
    runAnalysis(url);
  }

  function handleTryDemo() {
    navigate(`/results/${DEMO_RESULT.analysis_id}`, { state: { result: DEMO_RESULT } });
  }

  const healthColor = health?.status === "ok" ? "text-green" : "text-amber";
  const healthDot = health?.status === "ok" ? "bg-green" : "bg-amber";

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

      <main className="flex-1 flex flex-col items-center px-6 pt-32 pb-16">
        <div className="w-full max-w-2xl space-y-10">
          <div className="text-center space-y-4">
            <div className="inline-flex items-center gap-2 px-3 py-1 bg-accent/10 border border-accent/20 rounded-full text-xs font-mono text-accent">
              <span className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
              Pattern detection + Claude AI
            </div>
            <h1 className="text-4xl font-semibold text-text tracking-tight leading-tight">
              Understand any codebase<br />
              <span className="text-accent">instantly</span>
            </h1>
            <p className="text-muted text-base leading-relaxed max-w-md mx-auto">
              Drop a GitHub URL. Get structured stack analysis — language, frameworks, domain, architecture, and AI reasoning.
            </p>
          </div>

          <div className="space-y-3">
            <form onSubmit={handleSubmit} className="space-y-2">
              <div className="flex gap-2">
                <input
                  ref={inputRef}
                  type="text"
                  value={repoUrl}
                  onChange={handleUrlChange}
                  disabled={loading || rateLimitCountdown !== null}
                  placeholder="https://github.com/owner/repo"
                  className={`flex-1 font-mono text-sm bg-surface border text-text placeholder-muted rounded-md px-4 py-3 outline-none transition-colors disabled:opacity-50 ${
                    urlError ? "border-red-500/60 focus:border-red-400" : "border-border focus:border-accent"
                  }`}
                  spellCheck={false}
                />
                <button
                  type="submit"
                  disabled={loading || !repoUrl.trim() || rateLimitCountdown !== null}
                  className="px-6 py-3 bg-accent text-bg font-semibold text-sm rounded-md hover:bg-accent/90 active:scale-95 transition-all disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
                >
                  {loading ? "Analyzing…" : "Analyze"}
                </button>
              </div>

              {urlError && (
                <p className="text-red-400 text-xs font-sans pl-1">{urlError}</p>
              )}
            </form>

            {error && !rateLimitCountdown && (
              <div className="flex items-start gap-2 px-3 py-2.5 bg-red-500/10 border border-red-500/25 rounded text-red-400 text-sm font-sans">
                <span className="shrink-0 mt-0.5">⚠</span>
                <span>{error}</span>
              </div>
            )}

            {rateLimitCountdown !== null && (
              <div className="flex items-center gap-3 px-4 py-3 bg-amber/10 border border-amber/25 rounded font-mono text-sm text-amber">
                <span className="shrink-0">→</span>
                <span>
                  GitHub rate limit hit. Retrying in{" "}
                  <span className="font-bold tabular-nums">{rateLimitCountdown}s</span>
                  …
                </span>
              </div>
            )}
          </div>

          {loading && (
            <div className="flex justify-center">
              <LoadingTerminal steps={STEPS} currentStep={currentStep} />
            </div>
          )}

          {!loading && !rateLimitCountdown && (
            <div className="flex flex-col items-center gap-2">
              <button
                onClick={handleTryDemo}
                className="inline-flex items-center gap-2 px-5 py-2.5 bg-surface border border-border hover:border-accent/50 text-text text-sm rounded-md transition-colors font-sans"
              >
                <span className="w-1.5 h-1.5 rounded-full bg-accent" />
                Try demo — bhaktivora9/stacksniffer
              </button>
              <p className="text-xs text-muted font-sans">Instant preview · no API key needed</p>
            </div>
          )}
        </div>

        <div className="w-full max-w-2xl mt-20">
          <div className="text-center mb-8">
            <span className="text-xs text-muted uppercase tracking-widest font-sans">How it works</span>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-px bg-border rounded-lg overflow-hidden">
            {HOW_IT_WORKS.map(({ step, title, desc }) => (
              <div key={step} className="bg-surface px-6 py-6 space-y-3">
                <div className="font-mono text-xs text-accent/60">{step}</div>
                <div className="font-sans text-sm font-semibold text-text leading-snug">{title}</div>
                <div className="font-sans text-xs text-muted leading-relaxed">{desc}</div>
              </div>
            ))}
          </div>
        </div>
      </main>

      <footer className="border-t border-border px-6 py-4 text-center font-mono text-xs text-muted">
        StackSniffer v1.0 · Detection engine only · genREADME calls this API
      </footer>
    </div>
  );
}
