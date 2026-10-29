import { useState, useEffect, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { API_BASE } from "../config/api";
import { createAnalysis, subscribeToAnalysisEvents } from "../services/analyses";
import HomeLanding from "../components/HomeLanding";
import AnalyzingScreen, { AnalyzingScreenFallback } from "../components/AnalyzingScreen";
import ErrorBoundary from "../components/ErrorBoundary";
import { repositoryLabel } from "../utils/analysisEvents";

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
  // The analysis being followed is in the URL (?analysis=<id>), so a refresh or a shared link resumes it.
  const [searchParams, setSearchParams] = useSearchParams();
  const followedId = searchParams.get("analysis");

  const [repoUrl, setRepoUrl] = useState("");
  const [urlError, setUrlError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [analysisId, setAnalysisId] = useState(null);
  const [error, setError] = useState(null);
  const [streamEvents, setStreamEvents] = useState([]);
  const [streamStatus, setStreamStatus] = useState("idle");

  const inputRef = useRef(null);
  const closeStreamRef = useRef(null);
  const runRef = useRef(0);

  useEffect(() => {
    inputRef.current?.focus();
    if (followedId) {
      setLoading(true);
      followAnalysis(followedId, ++runRef.current);
    }
    return () => {
      runRef.current += 1;
      closeStreamRef.current?.();
    };
  }, []);

  // Back (or any navigation) to the bare home page stops following the analysis.
  useEffect(() => {
    if (!followedId && analysisId) cancelAnalysis();
  }, [followedId]); // eslint-disable-line react-hooks/exhaustive-deps

  function validateUrl(val) {
    return parseGitHubRepoInput(val).error;
  }

  function handleUrlChange(e) {
    const val = e.target.value;
    setRepoUrl(val);
    if (urlError) setUrlError(validateUrl(val));
  }

  async function runAnalysis(url) {
    setError(null);
    setLoading(true);
    setStreamEvents([]);
    setStreamStatus("running");
    setAnalysisId(null);
    closeStreamRef.current?.();
    const run = ++runRef.current;

    let analysis;
    try {
      analysis = await createAnalysis(url);
    } catch (err) {
      if (run !== runRef.current) return;
      setLoading(false);
      setStreamStatus("failed");
      setError(err.message || "Unable to create analysis");
      return;
    }
    if (run !== runRef.current) return; // cancelled or superseded while the request was in flight
    setSearchParams({ analysis: analysis.analysis_id }); // a new history entry, so back returns home
    followAnalysis(analysis.analysis_id, run);
  }

  function followAnalysis(id, run) {
    setAnalysisId(id);
    setStreamEvents([]);
    setStreamStatus("running");
    closeStreamRef.current?.();
    closeStreamRef.current = subscribeToAnalysisEvents(id, {
      onEvent: (event) => {
        if (run !== runRef.current) return;
        setStreamEvents((current) => [...current, event]);
        if (event.state === "AWAITING_STAGE") {
          setStreamStatus("waiting");
        } else if (event.state === "FAILED") {
          setStreamStatus("failed");
          setError(event.message || "Analysis failed");
        } else if (event.state === "READY" || event.state === "DEGRADED") {
          // A degraded analysis finished with some results missing; its page shows what there is.
          setStreamStatus("complete");
          // Replaces the progress entry: going back must not land on it and be forwarded again.
          navigate(`/analyses/${encodeURIComponent(id)}`, { replace: true });
        }
      },
      onDisconnect: () => {
        if (run !== runRef.current) return;
        setStreamStatus("failed");
        setError("The analysis stream disconnected. You can retry safely.");
      },
    });
  }

  function cancelAnalysis() {
    runRef.current += 1;
    closeStreamRef.current?.();
    setLoading(false);
    setStreamStatus("idle");
    setStreamEvents([]);
    setAnalysisId(null);
    setSearchParams({}, { replace: true });
  }

  // A followed link carries no URL of its own; retry with the repository the stream reported.
  function retryAnalysis() {
    const reported = repositoryLabel(streamEvents[streamEvents.length - 1]?.repository);
    const url = repoUrl || (reported ? `https://${reported}` : "");
    if (url) runAnalysis(url);
    else cancelAnalysis();
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

  if (loading) {
    return <ErrorBoundary resetKey={analysisId} fallback={({ error: renderError, reset }) => <AnalyzingScreenFallback analysisId={analysisId} error={renderError} reset={reset} />}>
      <AnalyzingScreen health={health} repoUrl={repoUrl} analysisId={analysisId} events={streamEvents} status={streamStatus} error={error} onCancel={cancelAnalysis} onRetry={retryAnalysis} />
    </ErrorBoundary>;
  }

  return <HomeLanding repoUrl={repoUrl} onChange={handleUrlChange} onSubmit={handleSubmit} inputRef={inputRef} health={health} urlError={urlError} error={error} />;
}
