import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { Link as LinkIcon, Search } from "lucide-react";
import LoadingTerminal from "../components/LoadingTerminal";
import { API_BASE } from "../config/api";
import { DEMO_RESULT } from "../data/demoResult";
import HomeLanding from "../components/HomeLanding";
import AnalyzingScreen from "../components/AnalyzingScreen";

const STEPS = [
  "Fetching repository",
  "Reading file tree",
  "Running pattern detection",
  "AI software_type classification",
  "Analysis complete",
];

const GITHUB_OWNER_REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(?:\.git)?\/?$/;

function parseGitHubRepoInput(value) {
  const raw = value.trim();
  if (!raw) return { url: "", error: null };
  if (/\s/.test(raw)) return { url: "", error: "GitHub URL cannot contain spaces" };

  const normalizeRepo = (owner, repo) => {
    const cleanRepo = repo.replace(/\.git$/i, "");
    if (!owner || !cleanRepo) return { url: "", error: "Enter a GitHub URL with owner and repo" };
    return { url: `https://github.com/${owner}/${cleanRepo}`, error: null };
  };

  if (GITHUB_OWNER_REPO_RE.test(raw) && !raw.toLowerCase().startsWith("github.com/")) {
    const [owner, repo] = raw.replace(/\/$/, "").split("/");
    return normalizeRepo(owner, repo);
  }

  const withProtocol = raw.startsWith("github.com/") ? `https://${raw}` : raw;
  let parsed;
  try {
    parsed = new URL(withProtocol);
  } catch {
    return { url: "", error: "Enter a valid GitHub repository URL" };
  }

  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    return { url: "", error: "Use a GitHub URL that starts with https://" };
  }
  if (parsed.hostname.toLowerCase() !== "github.com") {
    return { url: "", error: "Only github.com repository URLs are supported" };
  }

  const [owner, repo] = parsed.pathname.split("/").filter(Boolean);
  if (!owner || !repo) return { url: "", error: "Enter a GitHub URL with owner and repo" };
  return normalizeRepo(owner, repo);
}

const HOW_IT_WORKS = [
  {
    step: "01",
    title: "Paste any GitHub URL",
    desc: "Drop in any public GitHub repository URL — org repos, personal projects, or open-source libraries.",
  },
  {
    step: "02",
    title: "Pattern rules + AI classify your stack",
    desc: "500+ pattern rules detect languages, frameworks, and infra. AI classifies the repository's software type.",
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
  const [streamEvents, setStreamEvents] = useState([]);
  const [streamStatus, setStreamStatus] = useState("idle");

  const stepTimerRef = useRef(null);
  const inputRef = useRef(null);
  const countdownRef = useRef(null);
  const pendingRetryUrl = useRef(null);
  const eventSourceRef = useRef(null);
  const jobRef = useRef(null);

  useEffect(() => {
    inputRef.current?.focus();
    return () => {
      clearInterval(stepTimerRef.current);
      clearInterval(countdownRef.current);
      eventSourceRef.current?.close();
    };
  }, []);

  function validateUrl(val) {
    return parseGitHubRepoInput(val).error;
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
    setStreamEvents([]);
    setStreamStatus("running");
    eventSourceRef.current?.close();
    const job = crypto.randomUUID();
    jobRef.current = job;
    let lastSeq = 0;
    let terminal = false;
    const source = new EventSource(`${API_BASE}/api/analyze/stream?repo=${encodeURIComponent(url)}&job=${encodeURIComponent(job)}`);
    eventSourceRef.current = source;
    source.onmessage = (message) => {
      let event;
      try { event = JSON.parse(message.data); } catch { return; }
      if (!Number.isFinite(event.seq) || event.seq <= lastSeq) return;
      lastSeq = event.seq;
      setStreamEvents((current) => [...current, event]);
      setCurrentStep({ ingest: 0, verify: 1, infer: 3, done: 4 }[event.phase] ?? 0);
      if (event.level === "error") {
        terminal = true;
        source.close();
        setStreamStatus("failed");
        setError(event.error?.detail || event.message || "Analysis failed");
      } else if (event.phase === "done") {
        terminal = true;
        source.close();
        setStreamStatus("complete");
        if (event.analysis_id) navigate(`/results/${encodeURIComponent(event.analysis_id)}`);
      }
    };
    source.onerror = () => {
      if (terminal) return;
      source.close();
      setStreamStatus("failed");
      setError("The analysis stream disconnected. You can retry safely.");
    };
  }

  async function cancelAnalysis() {
    eventSourceRef.current?.close();
    if (jobRef.current) await fetch(`${API_BASE}/api/analyze/${encodeURIComponent(jobRef.current)}/cancel`, { method: "POST" }).catch(() => {});
    setLoading(false);
    setStreamStatus("idle");
    setStreamEvents([]);
  }

  function handleSubmit(e) {
    e?.preventDefault();
    const url = repoUrl.trim();
    const { url: normalizedUrl, error: err } = parseGitHubRepoInput(url);
    if (err || !url) {
      setUrlError(err || "Enter a GitHub repository URL");
      return;
    }
    setUrlError(null);
    setRepoUrl(normalizedUrl);
    runAnalysis(normalizedUrl);
  }

  function handleTryDemo() {
    navigate(`/results/${DEMO_RESULT.analysis_id}`, { state: { result: DEMO_RESULT } });
  }

  const healthColor = health?.status === "ok" ? "text-green" : "text-amber";
  const healthDot = health?.status === "ok" ? "bg-green" : "bg-amber";

  if (loading) {
    return <AnalyzingScreen health={health} currentStep={currentStep} repoUrl={repoUrl} events={streamEvents} status={streamStatus} error={error} onCancel={cancelAnalysis} onRetry={() => runAnalysis(repoUrl)} />;
  }

  return <HomeLanding repoUrl={repoUrl} onChange={handleUrlChange} onSubmit={handleSubmit} onDemo={handleTryDemo} inputRef={inputRef} health={health} urlError={urlError} error={error} rateLimitCountdown={rateLimitCountdown} />;
  /* legacy landing retained temporarily below for reference */
  return (
    <div className="hidden min-h-screen bg-bg">
      <header className="fixed left-0 right-0 top-0 z-50 flex h-16 items-center justify-between border-b border-border/30 bg-bg/85 px-4 backdrop-blur-xl md:px-6">
        <div className="flex items-center gap-3">
          <Search size={19} className="text-accent" />
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-sm font-semibold text-accent tracking-tight">StackSniffer</span>
            <span className="font-mono text-[11px] text-muted hidden sm:block">// stack detection engine</span>
          </div>
        </div>
        <div className="flex items-center gap-4">
          <nav className="hidden items-center gap-1 lg:flex"><a href="/search" className="rounded px-3 py-1.5 text-sm text-muted hover:bg-surface hover:text-accent">Search</a></nav>
          <a
            href="/review"
            className="text-muted hover:text-text transition-colors font-sans"
          >
            Review queue
          </a>
          {health && (
            <div className="flex items-center gap-1.5">
              <span className={`w-1.5 h-1.5 rounded-full ${healthDot}`} />
              <span className={`font-mono text-[11px] ${healthColor}`}>
                API: {health.status}
              </span>
            </div>
          )}
          <button onClick={() => inputRef.current?.focus()} className="ml-1 hidden rounded border border-accent/20 bg-accent/10 px-4 py-1.5 text-sm font-semibold text-accent hover:bg-accent/20 md:block">Analyze Repo</button>
        </div>
      </header>

      <main className="relative flex flex-1 flex-col items-center overflow-hidden px-4 pb-24 pt-32 md:px-8">
        <div className="pointer-events-none absolute left-1/2 top-0 h-[440px] w-[640px] -translate-x-1/2 rounded-full bg-accent/[.045] blur-3xl" />
        <div className="relative w-full max-w-3xl space-y-8">
          <div className="text-center space-y-4">
            <div className="inline-flex items-center gap-2 rounded-full border border-border/50 bg-surface/60 px-3 py-1 font-mono text-[10px] uppercase tracking-wider text-muted">
              <span className="h-1.5 w-1.5 rounded-full bg-accent" />
              Pattern detection + Claude AI
            </div>
            <h1 className="text-4xl font-semibold leading-tight tracking-tight text-[#d8e2ff] md:text-5xl">
              Understand any codebase<br />
              <span className="text-accent">instantly</span>
            </h1>
            <p className="text-muted text-base leading-relaxed max-w-md mx-auto">
              Drop a GitHub URL. Get structured stack analysis — language, frameworks, software_type, architecture, and AI reasoning.
            </p>
          </div>

          <div className="space-y-4">
            <form onSubmit={handleSubmit} className="space-y-2">
              <div className="flex w-full flex-col gap-3 sm:flex-row">
                <div className={`app-glass relative flex flex-1 items-center rounded-lg transition focus-within:border-accent focus-within:shadow-[0_0_0_2px_rgba(173,198,255,.2)] ${urlError ? "border-red-500/60" : ""}`}><LinkIcon size={17} className="pointer-events-none absolute left-4 text-muted"/><input
                  ref={inputRef}
                  type="text"
                  value={repoUrl}
                  onChange={handleUrlChange}
                  disabled={loading || rateLimitCountdown !== null}
                  placeholder="https://github.com/owner/repo"
                  className="min-w-0 flex-1 rounded-lg border-0 bg-transparent py-3.5 pl-12 pr-4 font-mono text-sm text-text outline-none placeholder:text-muted/60 disabled:opacity-50"
                  spellCheck={false}
                /></div>
                <button
                  type="submit"
                  disabled={loading || !repoUrl.trim() || rateLimitCountdown !== null}
                  className="shrink-0 rounded-lg bg-amber px-8 py-3.5 text-sm font-semibold text-[#472a00] transition-all hover:shadow-[0_0_20px_rgba(255,185,95,.4)] active:scale-95 disabled:cursor-not-allowed disabled:opacity-40"
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
                className="inline-flex items-center gap-2 rounded-lg border border-border/40 bg-transparent px-4 py-2 text-sm text-muted transition-colors hover:bg-surface/60 hover:text-text"
              >
                <span className="h-2 w-2 rounded-full bg-amber" />
                Try demo — bhaktivora9/stacksniffer
              </button>
              <p className="text-xs text-muted font-sans">Instant preview · no API key needed</p>
            </div>
          )}

        </div>

        <div id="how-it-works" className="relative mt-24 w-full max-w-5xl">
          <div className="text-center mb-8">
            <span className="text-xs text-muted uppercase tracking-widest font-sans">How it works</span>
          </div>
          <div className="grid grid-cols-1 gap-6 sm:grid-cols-3">
            {HOW_IT_WORKS.map(({ step, title, desc }) => (
              <div key={step} className="app-glass space-y-4 rounded-xl border-t-accent/30 px-6 py-6 text-left">
                <div className="grid h-10 w-10 place-items-center rounded border border-border/40 bg-[#2d3449] font-mono text-xs text-accent">{step}</div>
                <div className="font-sans text-base font-semibold leading-snug text-text">{title}</div>
                <div className="font-sans text-xs text-muted leading-relaxed">{desc}</div>
              </div>
            ))}
          </div>
        </div>
      </main>

      <footer className="flex flex-col items-center justify-between gap-4 border-t border-border/20 bg-[#060e20] px-8 py-10 text-center font-mono text-[10px] uppercase tracking-wider text-muted md:flex-row">
        StackSniffer v1.0 · Detection engine only · genREADME calls this API
      </footer>
    </div>
  );
}
