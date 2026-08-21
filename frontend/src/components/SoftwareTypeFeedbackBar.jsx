import { useEffect, useMemo, useState } from "react";
import { Check, X } from "lucide-react";
import { API_BASE } from "../config/api";

const SOFTWARE_TYPE_STYLES = {
  web_api: "bg-accent/10 text-accent border-accent/25",
  data_pipeline: "bg-amber/10 text-amber border-amber/25",
  ml_platform: "bg-ai-purple/10 text-ai-purple border-ai-purple/25",
  microservice: "bg-green/10 text-green border-green/25",
  fullstack: "bg-accent/10 text-accent border-accent/25",
  cli_tool: "bg-muted/10 text-muted border-muted/25",
  library: "bg-green/10 text-green border-green/25",
  unknown: "bg-border/50 text-muted border-border",
};

const SOFTWARE_TYPE_BORDER_COLORS = {
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

function prettySoftwareType(software_type) {
  return (software_type || "unknown").replace(/_/g, " ");
}

export default function SoftwareTypeFeedbackBar({
  analysisId,
  currentSoftwareType,
  pipelineSoftwareType,
  aiSoftwareType,
  aiReasoning,
  specificIdentity,
  softwareTypeOverlay,
  softwareTypeConfidence = 0,
  ragInfluenced,
  similarReposUsed = 0,
  classifierUsed,
  detectedTechs = [],
  isAdmin = false,
  onToast,
}) {
  const [selected, setSelected] = useState(null);
  const [formOpen, setFormOpen] = useState(false);
  const [selectedSoftwareType, setSelectedSoftwareType] = useState(currentSoftwareType || "unknown");
  const [wrongTechs, setWrongTechs] = useState([]);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [corrected, setCorrected] = useState(false);
  const [softwareTypeOptions, setSoftwareTypeOptions] = useState([]);
  const [taxonomyError, setTaxonomyError] = useState("");

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/taxonomy/software_types`)
      .then((response) => response.ok ? response.json() : Promise.reject(response.status))
      .then((data) => {
        if (!cancelled && data.software_types?.length) {
          setSoftwareTypeOptions(data.software_types);
          setSelectedSoftwareType((selectedType) =>
            data.software_types.some((option) => option.id === selectedType)
              ? selectedType
              : data.software_types[0].id
          );
          setTaxonomyError("");
        } else if (!cancelled) {
          setSoftwareTypeOptions([]);
          setTaxonomyError("No software types are configured");
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSoftwareTypeOptions([]);
          setTaxonomyError("Software types could not be loaded");
        }
      });
    return () => { cancelled = true; };
  }, []);

  const confidencePct = Math.round((softwareTypeConfidence ?? 0) * 100);
  const overlayValue = softwareTypeOverlay?.corrected_value
    ? softwareTypeOverlay.corrected_value
    : currentSoftwareType;
  const overlayShown = Boolean(softwareTypeOverlay?.pipeline_value && softwareTypeOverlay?.corrected_value);
  const softwareTypeClass = corrected || overlayShown
    ? "bg-amber/10 text-amber border-amber/30"
    : SOFTWARE_TYPE_STYLES[currentSoftwareType] ?? SOFTWARE_TYPE_STYLES.unknown;
  const leftBorderColor = corrected || overlayShown
    ? "#d29922"
    : SOFTWARE_TYPE_BORDER_COLORS[currentSoftwareType] ?? SOFTWARE_TYPE_BORDER_COLORS.unknown;

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
      overlayShown && isAdmin
        ? {
            software_type_correct: false,
            correct_software_type: overlayValue,
            approved_overlay_confirmed: true,
          }
        : { software_type_correct: true },
      overlayShown && isAdmin
        ? `Confirmed corrected software type: ${prettySoftwareType(overlayValue)}`
        : "Confirmed pipeline software type",
      "success"
    );
    if (!ok) setSelected(null);
  }

  function handleDown() {
    if (submitted || submitting) return;
    setSelected("down");
    if (!isAdmin) {
      submitFeedback(
        { software_type_correct: false },
        "Feedback recorded",
        "warn"
      ).then((ok) => {
        if (!ok) setSelected(null);
      });
      return;
    }
    setFormOpen(true);
  }

  async function handleSubmitCorrection() {
    if (!softwareTypeOptions.some((option) => option.id === selectedSoftwareType)) return;
    const ok = await submitFeedback(
      {
        software_type_correct: false,
        correct_software_type: selectedSoftwareType,
        techs_wrong: wrongTechs,
      },
      "Correction recorded - patterns penalized",
      "warn"
    );
    if (ok) {
      setCorrected(true);
    }
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
              <span className={`text-xs font-mono px-2.5 py-1 rounded-full border ${softwareTypeClass}`}>
                Software type: {prettySoftwareType(pipelineSoftwareType || currentSoftwareType)} (pipeline)
                {overlayShown && (
                  <> &rarr; {prettySoftwareType(overlayValue)} (corrected)</>
                )}
                {corrected && !overlayShown ? " (submitted for review)" : ""}
              </span>
              <span className="text-xs font-mono px-2.5 py-1 rounded-full border border-ai-purple/25 bg-ai-purple/10 text-ai-purple">
                AI: {prettySoftwareType(aiSoftwareType)}
              </span>
              {specificIdentity && (
                <span
                  className="text-xs font-mono px-2.5 py-1 rounded-full border border-teal-300/25 bg-teal-400/10 text-teal-300"
                  title="Observed finer-grained identity; this does not change the canonical software type"
                >
                  Specific identity: {prettySoftwareType(specificIdentity)}
                </span>
              )}
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
            {aiReasoning && (
              <div className="max-w-3xl rounded border border-border/60 bg-bg/50 px-3 py-2 text-xs leading-relaxed text-muted">
                <span className="mr-2 font-mono uppercase tracking-wider text-ai-purple">AI reasoning</span>
                {aiReasoning}
              </div>
            )}
            <div className="flex items-center gap-2">
              <div className="h-1.5 flex-1 max-w-xs rounded-full bg-border overflow-hidden">
                <div
                  className={`h-full rounded-full ${confidenceColor(softwareTypeConfidence)}`}
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
              aria-label="SoftwareType is correct"
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
              aria-label="SoftwareType is incorrect"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        {isAdmin && <div
          className="overflow-y-auto transition-all duration-300"
          style={{ maxHeight: formOpen ? "280px" : "0px", opacity: formOpen ? 1 : 0 }}
        >
          <div className="pt-3 mt-3 border-t border-border space-y-3">
            <div className="flex flex-wrap items-end gap-2">
              <label className="block shrink-0">
                <span className="mb-1 block text-[10px] font-mono uppercase tracking-wider text-muted">
                  Correct software type
                </span>
                <select
                  value={selectedSoftwareType}
                  onChange={(e) => setSelectedSoftwareType(e.target.value)}
                  disabled={submitted || submitting}
                  className="block h-8 w-44 appearance-auto rounded border border-border bg-bg px-2 text-xs font-mono text-text outline-none focus:border-accent disabled:opacity-60"
                  style={{ width: "11rem", backgroundColor: "#0d1117", color: "#e6edf3" }}
                >
                  {softwareTypeOptions.length === 0 && <option value="">Unavailable</option>}
                  {softwareTypeOptions.map((software_type) => (
                    <option key={software_type.id} value={software_type.id}>
                      {software_type.label}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                onClick={handleSubmitCorrection}
                disabled={submitted || submitting || !softwareTypeOptions.some((option) => option.id === selectedSoftwareType)}
                className="h-8 rounded border border-amber/30 bg-amber/10 px-3 text-xs font-mono text-amber transition-colors hover:bg-amber/15 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {submitting ? "Submitting..." : submitted ? "Recorded" : "Submit correction"}
              </button>
              {taxonomyError && <span className="text-[10px] text-red-400">{taxonomyError}</span>}
            </div>

            <div>
              <div className="mb-1.5 text-[10px] font-mono uppercase tracking-wider text-muted">
                Incorrect technologies (optional)
              </div>
              <div className="flex flex-wrap gap-1.5">
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
            </div>
          </div>
        </div>}
      </div>
    </div>
  );
}
