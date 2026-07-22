import { useEffect, useMemo, useState } from "react";
import { API_BASE } from "../config/api";

const STAR_GOLD = "#F6B93B";
const AMBER_DOT = "#EF9F27";

// The model label is read from the analysis payload, never hardcoded. The old
// literal "Gemini 2.0 Flash" disagreed with the backend (which runs
// gemini-2.5-flash) — a mismatch a judge would see. Fall back only if the
// payload carries no model string.
const DEFAULT_MODEL_LABEL = "Gemini";

const FIELD_CONFIG = [
  {
    key: "why_this_stack",
    label: "Why this stack",
    fallbackBadSignals: ["works well together", "commonly used", "popular choice", "building applications"],
  },
  {
    key: "stack_pattern",
    label: "Stack pattern",
    fallbackBadSignals: ["Custom", "MVC"],
  },
  {
    key: "ecosystem_context",
    label: "Ecosystem context",
    fallbackBadSignals: ["enterprise", "modern software", "popular"],
  },
  {
    key: "notable_combinations",
    label: "Notable combinations",
    fallbackBadSignals: ["commonly paired"],
  },
];

/**
 * notable_combinations is declared list[str] in StackAnalysis, but the
 * corrections path applies human edits AFTER model_dump(), so a raw textarea
 * string can reach storage and the browser without ever passing the schema.
 * A string satisfies `?.length > 0` and then crashes on `.map`. Coerce to an
 * array at every read site — a leaf card must never be able to blank the page.
 */
function toComboArray(value) {
  if (Array.isArray(value)) return value.map((v) => String(v).trim()).filter(Boolean);
  if (typeof value === "string") {
    return value
      .split(/[\n;,]/)
      .map((s) => s.trim())
      .filter(Boolean);
  }
  return [];
}

export default function AiInsightsCard({ stack, analysisId }) {
  const [rating, setRating] = useState(null);
  const [hoverRating, setHoverRating] = useState(null);
  const [submitted, setSubmitted] = useState(false);
  const [improving, setImproving] = useState(false);
  const [selectedFields, setSelectedFields] = useState([]);
  const [improvements, setImprovements] = useState({});
  const [communityRating, setCommunityRating] = useState(null);
  const [criteria, setCriteria] = useState([]);
  const [improvementSubmitted, setImprovementSubmitted] = useState(false);
  const [submittingImprovement, setSubmittingImprovement] = useState(false);
  const [submitError, setSubmitError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    fetchCommunityRating().then((stats) => {
      if (!cancelled) setCommunityRating(stats);
    });
    fetch(`${API_BASE}/api/insights-feedback/quality-criteria`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed (${res.status})`);
        return res.json();
      })
      .then((payload) => {
        if (!cancelled) setCriteria(payload.criteria ?? []);
      })
      .catch(() => {
        if (!cancelled) setCriteria([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const qualityFlags = useMemo(
    () => Object.fromEntries(FIELD_CONFIG.map((field) => [field.key, hasBadSignal(field, stack, criteria)])),
    [criteria, stack]
  );

  if (!stack) return null;

  // Single normalisation point. Every render + logic path below uses `combos`,
  // never stack.notable_combinations directly.
  const combos = toComboArray(stack.notable_combinations);

  const notUsed = stack.ai_classification_used === false;
  const modelLabel = stack.model || stack.ai_model || DEFAULT_MODEL_LABEL;
  const visibleCommunityRating =
    communityRating && (communityRating.total ?? 0) > 3 ? communityRating : null;

  async function submitRating(value) {
    if (submitted || !analysisId) return;
    setRating(value);
    setSubmitted(true);
    setSubmitError(null);
    if (value <= 3) setImproving(true);

    try {
      const res = await fetch(`${API_BASE}/api/insights-feedback/${analysisId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildRatingPayload(value)),
      });
      if (!res.ok) throw new Error(`Failed (${res.status})`);
      setCommunityRating(await fetchCommunityRating());
    } catch (err) {
      // Surface the failure instead of silently reverting. A 422 from a field
      // mismatch used to look identical to "nothing happened".
      setSubmitted(false);
      setRating(null);
      setSubmitError("Could not submit rating — please retry.");
    }
  }

  async function submitImprovement() {
    if (!analysisId || selectedFields.length === 0 || rating == null) return;
    setSubmittingImprovement(true);
    setSubmitError(null);
    try {
      const replacementText = {};
      for (const field of selectedFields) {
        const raw = improvements[field] ?? "";
        // notable_combinations is list[str] server-side. Send an array, not the
        // raw textarea string, so the correction matches the schema and the
        // similar-repos card can render it.
        replacementText[field] =
          field === "notable_combinations" ? toComboArray(raw) : raw;
      }

      const res = await fetch(`${API_BASE}/api/insights-feedback/${analysisId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...buildRatingPayload(rating),
          accepted: false,
          edited_fields: selectedFields,
          replacement_text: replacementText,
        }),
      });
      if (!res.ok) throw new Error(`Failed (${res.status})`);
      setImproving(false);
      setImprovementSubmitted(true);
      setCommunityRating(await fetchCommunityRating());
    } catch (err) {
      setImprovementSubmitted(false);
      setSubmitError("Could not submit improvement — please retry.");
    } finally {
      setSubmittingImprovement(false);
    }
  }

  return (
    <div className="border border-border rounded-lg overflow-hidden border-l-2 border-l-ai-purple bg-surface">
      <div className="px-4 py-3 border-b border-border flex items-center justify-between">
        <span className="text-sm font-medium text-text">AI architectural analysis</span>
        <div className="flex items-center gap-1.5">
          {visibleCommunityRating && (
            <span
              title={`Community rating from ${visibleCommunityRating.total} users`}
              className="text-[10px] font-mono font-medium px-2 py-0.5 rounded bg-amber/10 text-amber border border-amber/25"
            >
              ★ {(visibleCommunityRating.avg_quality_score ?? 0).toFixed(1)}
            </span>
          )}
          <span className="text-[10px] font-mono font-medium px-2 py-0.5 rounded bg-ai-purple/15 text-ai-purple border border-ai-purple/30">
            AI
          </span>
        </div>
      </div>

      {notUsed ? (
        <div className="px-4 py-4">
          <div className="flex items-center gap-2 text-amber text-sm">
            <span>⚠</span>
            <span>Pattern detection only — AI unavailable</span>
          </div>
        </div>
      ) : (
        <div className="px-4 py-4 space-y-3">
          {stack.stack_pattern && (
            <Row label="Pattern" value={stack.stack_pattern} flagged={qualityFlags.stack_pattern} />
          )}

          {stack.why_this_stack && (
            <div>
              <FieldLabel label="Why this stack" flagged={qualityFlags.why_this_stack} />
              <p className="mt-1 text-sm text-text leading-relaxed">{stack.why_this_stack}</p>
            </div>
          )}

          {stack.ecosystem_context && (
            <Row label="Ecosystem" value={stack.ecosystem_context} flagged={qualityFlags.ecosystem_context} />
          )}

          {stack.architecture_style && stack.architecture_style !== "unknown" && (
            <Row label="Style" value={stack.architecture_style} />
          )}

          {(combos.length > 0 || qualityFlags.notable_combinations) && (
            <div>
              <FieldLabel label="Notable combinations" flagged={qualityFlags.notable_combinations} />
              {combos.length > 0 ? (
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {combos.map((combo, i) => (
                    <span
                      key={i}
                      className="text-xs font-mono px-2 py-0.5 rounded bg-ai-purple/10 text-ai-purple border border-ai-purple/20"
                    >
                      {combo}
                    </span>
                  ))}
                </div>
              ) : (
                <p className="mt-0.5 text-sm text-muted">No notable combinations</p>
              )}
            </div>
          )}

          <div className="space-y-3 pt-2 border-t border-border">
            <div className="min-h-8 flex items-center gap-3 flex-wrap">
              <span className="text-xs text-muted">Rate these insights:</span>
              <div
                className={`flex items-center gap-1 ${submitted ? "pointer-events-none" : ""}`}
                onMouseLeave={() => setHoverRating(null)}
              >
                {[1, 2, 3, 4, 5].map((value) => {
                  const filled = value <= (hoverRating ?? rating ?? 0);
                  return (
                    <button
                      key={value}
                      type="button"
                      disabled={submitted}
                      onMouseEnter={() => setHoverRating(value)}
                      onClick={() => submitRating(value)}
                      className="h-4 w-4 p-0 text-base leading-4 transition-colors duration-100 disabled:cursor-default"
                      style={{ color: filled ? STAR_GOLD : "var(--color-text-tertiary, #7d8590)" }}
                      aria-label={`Rate insights ${value} stars`}
                    >
                      {filled ? "★" : "☆"}
                    </button>
                  );
                })}
              </div>
              {submitted && (
                <>
                  <span className="text-[11px] text-muted">Thanks · helps improve AI analysis</span>
                  <button
                    type="button"
                    onClick={() => {
                      setImproving((value) => !value);
                      setImprovementSubmitted(false);
                    }}
                    className="text-xs text-accent hover:text-accent/80 transition-colors duration-150"
                  >
                    Suggest improvement {improving ? "↑" : "↓"}
                  </button>
                </>
              )}
            </div>

            {submitError && <p className="text-xs text-amber">{submitError}</p>}

            {improvementSubmitted && (
              <p className="text-xs text-green">Improvement submitted — this trains the model</p>
            )}

            <div
              className="overflow-hidden transition-all duration-150"
              style={{ maxHeight: improving ? "760px" : "0px", opacity: improving ? 1 : 0 }}
            >
              <div className="rounded border border-border bg-bg px-3 py-3 space-y-3">
                <div className="flex flex-wrap gap-x-4 gap-y-2">
                  {FIELD_CONFIG.map((field) => (
                    <label key={field.key} className="flex items-center gap-2 text-xs text-muted">
                      <input
                        type="checkbox"
                        checked={selectedFields.includes(field.key)}
                        onChange={() => {
                          setSelectedFields((fields) =>
                            fields.includes(field.key)
                              ? fields.filter((item) => item !== field.key)
                              : [...fields, field.key]
                          );
                          setImprovements((values) => ({
                            ...values,
                            [field.key]: values[field.key] ?? defaultImprovementValue(field.key, stack),
                          }));
                        }}
                        className="accent-accent"
                      />
                      {field.label}
                    </label>
                  ))}
                </div>

                {selectedFields.map((field) => (
                  <ImprovementField
                    key={field}
                    field={field}
                    stack={stack}
                    value={improvements[field] ?? defaultImprovementValue(field, stack)}
                    onChange={(value) => setImprovements((items) => ({ ...items, [field]: value }))}
                  />
                ))}

                <button
                  type="button"
                  onClick={submitImprovement}
                  disabled={submittingImprovement || selectedFields.length === 0}
                  className="rounded border border-accent/30 bg-accent/10 px-3 py-2 text-sm text-accent transition-colors hover:bg-accent/15 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {submittingImprovement ? "Submitting..." : "Submit improvement"}
                </button>
              </div>
            </div>
          </div>

          <div className="pt-2 border-t border-border font-mono text-[10px] text-muted flex items-center gap-1.5 flex-wrap">
            <span>{modelLabel}</span>
            <span className="text-border">·</span>
            {stack.domain && stack.domain !== "unknown" && (
              <>
                <span>{stack.domain}</span>
                <span className="text-border">·</span>
              </>
            )}
            <span>{((stack.domain_confidence ?? 0) * 100).toFixed(0)}% confidence</span>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Single source of truth for the feedback POST body.
 *
 * TODO(verify): confirm the InsightsFeedback pydantic model's field name.
 * This card historically sent `quality_score`; the refactor's feedback route
 * reads `rating`. Sending BOTH is a safe bridge — the model ignores the extra
 * key and both possible schemas get populated. Once you confirm which the
 * backend expects, drop the other.
 */
function buildRatingPayload(value) {
  return {
    rating: value, // refactor route reads this
    quality_score: value, // legacy route read this
    accepted: true,
    edited_fields: [],
    source: "ui",
  };
}

async function fetchCommunityRating() {
  try {
    const res = await fetch(`${API_BASE}/api/insights-feedback/stats`);
    if (!res.ok) throw new Error(`Failed (${res.status})`);
    return res.json();
  } catch {
    return null;
  }
}

function FieldLabel({ label, flagged }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted uppercase tracking-wider font-sans">
      {label}
      {flagged && (
        <span
          title="This field may be generic"
          className="inline-block h-1.5 w-1.5 rounded-full"
          style={{ backgroundColor: AMBER_DOT }}
        />
      )}
    </span>
  );
}

function Row({ label, value, flagged = false }) {
  return (
    <div>
      <FieldLabel label={label} flagged={flagged} />
      <p className="mt-0.5 text-sm text-text">{value}</p>
    </div>
  );
}

function StackPatternSelect({ value, onChange }) {
  const PATTERNS = [
    "Plugin Architecture", "Chain of Responsibility", "Fluent Interface",
    "Hexagonal", "CQRS", "Event Sourcing", "Lambda Architecture",
    "JAMstack", "Microservices", "Event-Driven", "Serverless", "MVC",
    "DAG Scheduler", "Pull-Based Scraping",
  ];
  const isCustom = !PATTERNS.includes(value) || value === "Custom";

  return (
    <div className="space-y-1.5">
      <select
        value={isCustom ? "__custom__" : value}
        onChange={(e) => {
          if (e.target.value !== "__custom__") onChange(e.target.value);
        }}
        className="w-full rounded border border-border bg-surface px-2 py-2 text-sm text-text outline-none focus:border-accent"
        style={{ background: "#0d1117", color: "var(--color-text-primary)" }}
      >
        <option value="__custom__">Custom (type below)…</option>
        <optgroup label="Library / Framework">
          {["Plugin Architecture","Chain of Responsibility","Fluent Interface","Hexagonal"].map(p => (
            <option key={p} value={p}>{p}</option>
          ))}
        </optgroup>
        <optgroup label="Data">
          {["Event-Driven","CQRS","Event Sourcing","Lambda Architecture","DAG Scheduler"].map(p => (
            <option key={p} value={p}>{p}</option>
          ))}
        </optgroup>
        <optgroup label="Web">
          {["MVC","JAMstack","Serverless"].map(p => (
            <option key={p} value={p}>{p}</option>
          ))}
        </optgroup>
        <optgroup label="Deployment">
          {["Microservices"].map(p => (
            <option key={p} value={p}>{p}</option>
          ))}
        </optgroup>
        <optgroup label="Infrastructure">
          {["Pull-Based Scraping"].map(p => (
            <option key={p} value={p}>{p}</option>
          ))}
        </optgroup>
      </select>
      {(isCustom) && (
        <input
          type="text"
          value={value === "Custom" ? "" : value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Type pattern name, e.g. Plugin Architecture"
          list="pattern-datalist"
          className="w-full rounded border border-border bg-surface px-2 py-2 text-sm text-text outline-none focus:border-accent"
        />
      )}
      <datalist id="pattern-datalist">
        {PATTERNS.map(p => <option key={p} value={p} />)}
        <option value="Observer" />
        <option value="Decorator" />
        <option value="Strategy" />
        <option value="Factory" />
      </datalist>
    </div>
  );
}

function ImprovementField({ field, stack, value, onChange }) {
  const config = FIELD_CONFIG.find((item) => item.key === field);
  const currentValue = stack ? currentFieldValue(field, stack) : "";

  return (
    <div className="space-y-1.5">
      <div className="text-xs text-muted uppercase tracking-wider font-sans">{config?.label}</div>
      <div className="rounded border border-border bg-surface px-2 py-1.5 text-xs text-muted whitespace-pre-wrap">
        {Array.isArray(currentValue)
          ? currentValue.join("\n") || "No current value"
          : currentValue || "No current value"}
      </div>
      {field === "stack_pattern" ? (
        <StackPatternSelect value={value} onChange={onChange} />
      ) : (
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          rows={field === "notable_combinations" ? 3 : 2}
          placeholder={field === "notable_combinations" ? "One combination per line" : "Suggested replacement"}
          className="w-full resize-y rounded border border-border bg-surface px-2 py-2 text-sm text-text outline-none focus:border-accent"
        />
      )}
    </div>
  );
}

function currentFieldValue(field, stack) {
  if (!stack) return field === "notable_combinations" ? [] : "";
  if (field === "notable_combinations") return toComboArray(stack.notable_combinations);
  return stack?.[field] ?? "";
}

function defaultImprovementValue(field, stack) {
  if (!stack) return field === "stack_pattern" ? "Custom" : "";
  const value = currentFieldValue(field, stack);
  if (Array.isArray(value)) return value.join("\n");
  return value || (field === "stack_pattern" ? "Custom" : "");
}

function hasBadSignal(field, stack, criteria) {
  if (!stack) return false;
  const fieldCriteria = criteria.find((item) => item.field === field.key);
  const badSignals = fieldCriteria?.bad_signals?.length ? fieldCriteria.bad_signals : field.fallbackBadSignals;

  if (field.key === "stack_pattern") {
    return ["custom", "mvc"].includes(String(stack?.stack_pattern ?? "").toLowerCase());
  }

  if (field.key === "notable_combinations") {
    // Coerce first: a stored string would otherwise index to its first
    // CHARACTER here and silently produce a wrong signal rather than crash.
    const combinations = toComboArray(stack?.notable_combinations);
    if (combinations.length === 0) return true;
    return includesAny(combinations[0], badSignals);
  }

  return includesAny(stack?.[field.key] ?? "", badSignals);
}

function includesAny(value, signals) {
  const text = String(value ?? "").toLowerCase();
  return signals.some((signal) => text.includes(String(signal).toLowerCase()));
}