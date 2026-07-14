import { useMemo, useState } from "react";
import { Check, X } from "lucide-react";
import { API_BASE } from "../config/api";

const DOMAINS = [
  "web_api",
  "data_pipeline",
  "ml_platform",
  "microservice",
  "fullstack",
  "cli_tool",
  "library",
  "unknown",
];

const DOMAIN_STYLES = {
  web_api: "bg-accent/10 text-accent border-accent/25",
  data_pipeline: "bg-amber/10 text-amber border-amber/25",
  ml_platform: "bg-ai-purple/10 text-ai-purple border-ai-purple/25",
  microservice: "bg-green/10 text-green border-green/25",
  fullstack: "bg-accent/10 text-accent border-accent/25",
  cli_tool: "bg-muted/10 text-muted border-muted/25",
  library: "bg-green/10 text-green border-green/25",
  unknown: "bg-border/50 text-muted border-border",
};

const DOMAIN_BORDER_COLORS = {
  web_api: "#58a6ff",
  data_pipeline: "#d29922",
  ml_platform: "#a371f7",
  microservice: "#3fb950",
  fullstack: "#58a6ff",
  cli_tool: "#7d8590",
  library: "#3fb950",
  unknown: "#30363d",
};

function confidenceColor(confidence) {
  if (confidence >= 0.75) return "bg-green";
  if (confidence >= 0.45) return "bg-amber";
  return "bg-red-400";
}

function prettyDomain(domain) {
  return (domain || "unknown").replace(/_/g, " ");
}

export default function DomainFeedbackBar({
  analysisId,
  currentDomain,
  domainConfidence = 0,
  ragInfluenced,
  similarReposUsed = 0,
  classifierUsed,
  detectedTechs = [],
  onToast,
}) {
  const [selected, setSelected] = useState(null);
  const [formOpen, setFormOpen] = useState(false);
  const [selectedDomain, setSelectedDomain] = useState(currentDomain || "unknown");
  const [wrongTechs, setWrongTechs] = useState([]);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [corrected, setCorrected] = useState(false);

  const confidencePct = Math.round((domainConfidence ?? 0) * 100);
  const domainClass = corrected
    ? "bg-amber/10 text-amber border-amber/30"
    : DOMAIN_STYLES[currentDomain] ?? DOMAIN_STYLES.unknown;
  const leftBorderColor = corrected
    ? "#d29922"
    : DOMAIN_BORDER_COLORS[currentDomain] ?? DOMAIN_BORDER_COLORS.unknown;

  const techOptions = useMemo(() => {
    const names = detectedTechs.map((tech) => tech?.name).filter(Boolean);
    return Array.from(new Set(names));
  }, [detectedTechs]);

  async function submitFeedback(payload, successMessage, type) {
    setSubmitting(true);
    try {
      const res = await fetch(`${API_BASE}/api/feedback/${analysisId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(`Feedback failed (${res.status})`);
      setSubmitted(true);
      onToast?.(successMessage, type);
      return true;
    } catch (err) {
      onToast?.(err.message || "Feedback failed", "error");
      return false;
    } finally {
      setSubmitting(false);
    }
  }

  async function handleUp() {
    if (submitted || submitting) return;
    setSelected("up");
    const ok = await submitFeedback(
      { domain_correct: true },
      "Thanks - pattern confidence updated",
      "success"
    );
    if (!ok) setSelected(null);
  }

  function handleDown() {
    if (submitted || submitting) return;
    setSelected("down");
    setFormOpen(true);
  }

  async function handleSubmitCorrection() {
    const ok = await submitFeedback(
      {
        domain_correct: false,
        correct_domain: selectedDomain,
        techs_wrong: wrongTechs,
      },
      "Correction recorded - patterns penalized",
      "warn"
    );
    if (ok) setCorrected(true);
  }

  function toggleWrongTech(tech) {
    setWrongTechs((items) =>
      items.includes(tech) ? items.filter((item) => item !== tech) : [...items, tech]
    );
  }

  return (
    <div
      className="bg-surface border border-border border-l-[3px] rounded-lg overflow-hidden transition-all duration-300"
      style={{ borderLeftColor: leftBorderColor }}
    >
      <div className="px-4 py-3">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 flex-1 space-y-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className={`text-xs font-mono px-2.5 py-1 rounded-full border ${domainClass}`}>
                Domain: {prettyDomain(currentDomain)}
                {corrected ? " (corrected)" : ""}
              </span>
              {ragInfluenced && (
                <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-teal-400/10 text-teal-300 border border-teal-300/25">
                  RAG-informed - {similarReposUsed} repos retrieved
                </span>
              )}
              {classifierUsed && (
                <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-ai-purple/10 text-ai-purple border border-ai-purple/25">
                  Classifier - Layer 0
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <div className="h-1.5 flex-1 max-w-xs rounded-full bg-border overflow-hidden">
                <div
                  className={`h-full rounded-full ${confidenceColor(domainConfidence)}`}
                  style={{ width: `${Math.max(2, confidencePct)}%` }}
                />
              </div>
              <span className="font-mono text-[11px] text-muted">{confidencePct}%</span>
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={handleUp}
              disabled={submitted || submitting}
              className={`inline-flex h-8 w-8 items-center justify-center rounded border transition-colors ${
                selected === "up"
                  ? "border-green/40 bg-green/15 text-green"
                  : "border-border text-muted hover:border-green/40 hover:text-green"
              } disabled:cursor-not-allowed disabled:opacity-70`}
              aria-label="Domain is correct"
            >
              <Check size={16} />
            </button>
            <button
              type="button"
              onClick={handleDown}
              disabled={submitted || submitting}
              className={`inline-flex h-8 w-8 items-center justify-center rounded border transition-colors ${
                selected === "down"
                  ? "border-red-400/40 bg-red-400/15 text-red-400"
                  : "border-border text-muted hover:border-red-400/40 hover:text-red-400"
              } disabled:cursor-not-allowed disabled:opacity-70`}
              aria-label="Domain is incorrect"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        <div
          className="transition-all duration-300 overflow-hidden"
          style={{ maxHeight: formOpen ? "120px" : "0px", opacity: formOpen ? 1 : 0 }}
        >
          <div className="pt-3 mt-3 border-t border-border flex flex-wrap items-center gap-2">
            <select
              value={selectedDomain}
              onChange={(e) => setSelectedDomain(e.target.value)}
              disabled={submitted || submitting}
              className="h-8 rounded border border-border bg-bg px-2 text-xs font-mono text-text outline-none focus:border-accent disabled:opacity-60"
            >
              {DOMAINS.map((domain) => (
                <option key={domain} value={domain}>
                  {domain}
                </option>
              ))}
            </select>

            <div className="flex min-w-0 flex-1 flex-wrap gap-1.5">
              {techOptions.map((tech) => (
                <button
                  key={tech}
                  type="button"
                  onClick={() => toggleWrongTech(tech)}
                  disabled={submitted || submitting}
                  className={`rounded-full border px-2 py-1 text-[11px] font-mono transition-colors ${
                    wrongTechs.includes(tech)
                      ? "border-amber/40 bg-amber/15 text-amber"
                      : "border-border bg-bg text-muted hover:text-text"
                  } disabled:cursor-not-allowed disabled:opacity-60`}
                >
                  {tech}
                </button>
              ))}
            </div>

            <button
              type="button"
              onClick={handleSubmitCorrection}
              disabled={submitted || submitting}
              className="h-8 rounded border border-amber/30 bg-amber/10 px-3 text-xs font-mono text-amber transition-colors hover:bg-amber/15 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {submitting ? "Submitting..." : submitted ? "Recorded" : "Submit correction"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
