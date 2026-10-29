import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { API_BASE } from "../config/api";
import { createAnalysis, subscribeToAnalysisEvents } from "../services/analyses";
import HomeLanding from "../components/HomeLanding";
import AnalyzingScreen from "../components/AnalyzingScreen";

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

  const [repoUrl, setRepoUrl] = useState("");
  const [urlError, setUrlError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [currentStep, setCurrentStep] = useState(0);
  const [error, setError] = useState(null);
  const [streamEvents, setStreamEvents] = useState([]);
  const [streamStatus, setStreamStatus] = useState("idle");

  const inputRef = useRef(null);
  const closeStreamRef = useRef(null);
  const runRef = useRef(0);

  useEffect(() => {
    inputRef.current?.focus();
    return () => {
      runRef.current += 1;
      closeStreamRef.current?.();
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

  async function runAnalysis(url) {
    setError(null);
    setLoading(true);
    setStreamEvents([]);
    setStreamStatus("running");
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

    closeStreamRef.current = subscribeToAnalysisEvents(analysis.analysis_id, {
      onEvent: (event) => {
        setStreamEvents((current) => [...current, event]);
        const steps = { INGESTING: 0, EXTRACTING: 1, INDEXING: 2, PROJECTING: 3, SUMMARIZING: 3, READY: 4 };
        // A parked analysis has finished its last completed stage; show the rail up to the awaited one.
        setCurrentStep(event.state === "AWAITING_STAGE" ? steps[event.awaiting_stage] ?? 0 : steps[event.state] ?? 0);
        if (event.state === "AWAITING_STAGE") {
          setStreamStatus("waiting");
        } else if (["FAILED", "DEGRADED"].includes(event.state)) {
          setStreamStatus("failed");
          setError(event.message || "Analysis failed");
        } else if (event.state === "READY") {
          setStreamStatus("complete");
          navigate(`/analyses/${encodeURIComponent(analysis.analysis_id)}`);
        }
      },
      onDisconnect: () => {
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
    return <AnalyzingScreen health={health} currentStep={currentStep} repoUrl={repoUrl} events={streamEvents} status={streamStatus} error={error} onCancel={cancelAnalysis} onRetry={() => runAnalysis(repoUrl)} />;
  }

  return <HomeLanding repoUrl={repoUrl} onChange={handleUrlChange} onSubmit={handleSubmit} inputRef={inputRef} health={health} urlError={urlError} error={error} />;
}
