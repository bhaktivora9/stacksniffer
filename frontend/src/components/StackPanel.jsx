import { useState } from "react";
import { Check, X } from "lucide-react";
import AiInsightsCard from "./AiInsightsCard";

const CATEGORIES = [
  { key: "languages", label: "Languages" },
  { key: "frameworks", label: "Frameworks" },
  { key: "databases", label: "Databases" },
  { key: "messaging", label: "Messaging" },
  { key: "ai_ml", label: "AI / ML" },
  { key: "infra", label: "Infrastructure" },
  { key: "testing", label: "Testing" },
  { key: "library", label: "Libraries" },
];

function SourceDot({ source }) {
  if (source === "ai_inferred") {
    return (
      <span
        title="AI inferred"
        className="inline-flex items-center justify-center w-3.5 h-3.5 rounded-full bg-ai-purple/20 border border-ai-purple/50 text-[8px] font-bold text-ai-purple shrink-0"
      >
        A
      </span>
    );
  }
  if (source === "both") {
    return (
      <span
        title="Pattern + AI confirmed"
        className="inline-flex items-center justify-center w-3.5 h-3.5 rounded-full bg-teal-400/20 border border-teal-300/50 text-[9px] text-teal-300 shrink-0"
      >
        ✓
      </span>
    );
  }
  return null;
}

function TechPill({ tech, status, onCorrect, onWrong }) {
  const [wrongOpen, setWrongOpen] = useState(false);
  const [wrongType, setWrongType] = useState("false_positive");
  const [reason, setReason] = useState("");
  const pct = Math.round((tech.confidence ?? 0) * 100);
  const confirmed = status === "correct";
  const falsePositive = status === "false_positive";
  const pending = status === "pending";

  async function confirmWrong() {
    const label =
      wrongType === "source_wrong"
        ? "Detection source is wrong"
        : "This tech is not actually used";
    const ok = await onWrong?.(tech.name, [label, reason].filter(Boolean).join(": "));
    if (ok) {
      setWrongOpen(false);
      setReason("");
    }
  }

  return (
    <div className="min-w-0">
      <div
        className={`group flex items-center gap-2 px-2.5 py-1.5 bg-bg border rounded transition-all duration-150 ${
          confirmed
            ? "border-border border-l-green border-l-2"
            : falsePositive
            ? "border-border opacity-50"
            : "border-border hover:border-muted"
        }`}
      >
        <div className="flex items-center gap-1.5 min-w-0">
          <SourceDot source={tech.detection_source} />
          <span className={`text-sm text-text truncate font-sans ${falsePositive ? "line-through" : ""}`}>
            {tech.name}
          </span>
          {tech.version && (
            <span className="text-xs text-muted font-mono shrink-0">{tech.version}</span>
          )}
        </div>
        <div className="shrink-0 flex items-center gap-1">
          <div className="w-10 h-1 bg-border rounded-full overflow-hidden">
            <div
              className="h-full bg-accent rounded-full"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
        {falsePositive && (
          <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-red-400/10 text-red-400 border border-red-400/25">
            FP
          </span>
        )}
        <div className={`flex items-center gap-1 transition-opacity duration-150 ${
          confirmed || falsePositive ? "opacity-100" : "opacity-0 group-hover:opacity-100"
        }`}>
          <button
            type="button"
            onClick={() => onCorrect?.(tech.name)}
            disabled={confirmed || falsePositive || pending}
            className={`inline-flex h-[14px] w-[14px] items-center justify-center border-0 p-0 transition-colors duration-150 ${
              confirmed ? "text-green" : "text-muted hover:text-green"
            } disabled:cursor-not-allowed`}
            aria-label={`Confirm ${tech.name}`}
          >
            <Check size={14} />
          </button>
          {!confirmed && !falsePositive && (
            <button
              type="button"
              onClick={() => setWrongOpen((value) => !value)}
              disabled={pending}
              className="inline-flex h-[14px] w-[14px] items-center justify-center border-0 p-0 text-muted transition-colors duration-150 hover:text-[#ff4757] disabled:cursor-not-allowed"
              aria-label={`Flag ${tech.name}`}
            >
              <X size={14} />
            </button>
          )}
        </div>
      </div>

      <div
        className="overflow-hidden transition-all duration-150"
        style={{ maxHeight: wrongOpen ? "150px" : "0px", opacity: wrongOpen ? 1 : 0 }}
      >
        <div className="mt-2 rounded border border-border bg-surface px-3 py-2 space-y-2">
          <label className="flex items-center gap-2 text-xs text-muted">
            <input
              type="radio"
              name={`${tech.name}-wrong-type`}
              value="false_positive"
              checked={wrongType === "false_positive"}
              onChange={(event) => setWrongType(event.target.value)}
              className="accent-red-400"
            />
            This tech is not actually used (false positive)
          </label>
          <label className="flex items-center gap-2 text-xs text-muted">
            <input
              type="radio"
              name={`${tech.name}-wrong-type`}
              value="source_wrong"
              checked={wrongType === "source_wrong"}
              onChange={(event) => setWrongType(event.target.value)}
              className="accent-amber"
            />
            Detection source is wrong (still used)
          </label>
          <div className="flex gap-2">
            <input
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="e.g. only in comments"
              className="min-w-0 flex-1 rounded border border-border bg-bg px-2 py-1 text-xs text-text outline-none focus:border-accent"
            />
            <button
              type="button"
              onClick={confirmWrong}
              disabled={pending}
              className="rounded border border-red-400/30 bg-red-400/10 px-2 py-1 text-xs font-mono text-red-400 transition-colors hover:bg-red-400/15 disabled:cursor-wait disabled:opacity-60"
            >
              Confirm
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function MissingTechPanel({ onMissingTech }) {
  const [open, setOpen] = useState(false);
  const [techName, setTechName] = useState("");
  const [category, setCategory] = useState("frameworks");
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submitMissing() {
    const trimmed = techName.trim();
    if (!trimmed) return;
    setSubmitting(true);
    const ok = await onMissingTech?.(trimmed, category);
    setSubmitting(false);
    if (ok) {
      setMessage(`${trimmed} reported as missing. Will be added to pattern discovery queue.`);
      setTechName("");
    }
  }

  return (
    <div className="px-4 py-3">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="text-sm text-accent hover:text-accent/80 transition-colors font-sans"
      >
        + Report a technology that was missed
      </button>
      <div
        className="overflow-hidden transition-all duration-150"
        style={{ maxHeight: open ? "150px" : "0px", opacity: open ? 1 : 0 }}
      >
        <div className="mt-3 rounded border border-border bg-bg px-3 py-3">
          <div className="flex flex-wrap gap-2">
            <input
              value={techName}
              onChange={(event) => setTechName(event.target.value)}
              placeholder="e.g. Redis"
              className="min-w-[160px] flex-1 rounded border border-border bg-surface px-3 py-2 text-sm text-text outline-none focus:border-accent"
            />
            <select
              value={category}
              onChange={(event) => setCategory(event.target.value)}
              className="rounded border border-border bg-surface px-3 py-2 text-sm text-text outline-none focus:border-accent"
            >
              {CATEGORIES.map(({ key }) => (
                <option key={key} value={key}>
                  {key}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={submitMissing}
              disabled={submitting || !techName.trim()}
              className="rounded border border-accent/30 bg-accent/10 px-3 py-2 text-sm text-accent transition-colors hover:bg-accent/15 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {submitting ? "Reporting..." : "Report"}
            </button>
          </div>
          {message && <p className="mt-2 text-xs text-green">{message}</p>}
        </div>
      </div>
    </div>
  );
}

export default function StackPanel({
  stack,
  repo,
  analysisId,
  afterInsights,
  feedbackState = {},
  onTechCorrect,
  onTechWrong,
  onMissingTech,
}) {
  if (!stack || !repo) return null;

  const complexity = stack.complexity_score ?? 0;

  return (
    <div className="space-y-5">
      <div className="bg-surface border border-border rounded-lg px-5 py-4">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0">
            <h1 className="text-lg font-semibold text-text">{repo.full_name}</h1>
            {repo.description && (
              <p className="mt-1 text-sm text-muted leading-relaxed">{repo.description}</p>
            )}
          </div>
          <div className="flex items-center gap-3 shrink-0 text-sm text-muted">
            {repo.stars > 0 && (
              <span className="flex items-center gap-1">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" className="text-amber">
                  <path d="M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 .416 1.279l-3.046 2.97.719 4.192a.751.751 0 0 1-1.088.791L8 12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L.818 6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 8 .25Z" />
                </svg>
                {repo.stars.toLocaleString()}
              </span>
            )}
            {repo.license && (
              <span className="font-mono text-xs px-2 py-0.5 bg-bg border border-border rounded">
                {repo.license}
              </span>
            )}
          </div>
        </div>

        {repo.topics?.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {repo.topics.map((t) => (
              <span
                key={t}
                className="text-xs font-mono px-2 py-0.5 bg-accent/10 text-accent border border-accent/20 rounded-full"
              >
                {t}
              </span>
            ))}
          </div>
        )}
      </div>

      <AiInsightsCard stack={stack} analysisId={analysisId} />

      {afterInsights}

      <div className="bg-surface border border-border rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-3">
            {stack.domain && stack.domain !== "unknown" && (
              <span className="text-xs font-mono px-2.5 py-1 bg-green/10 text-green border border-green/25 rounded-full font-medium">
                {stack.domain.replace(/_/g, " ")}
              </span>
            )}
            {stack.architecture_style && stack.architecture_style !== "unknown" && (
              <span className="text-xs font-mono px-2.5 py-1 bg-accent/10 text-accent border border-accent/25 rounded-full">
                {stack.architecture_style}
              </span>
            )}
          </div>
          <div
            className="flex items-center gap-2 text-xs text-muted"
            title="Detection breadth — how many technology categories have confident signal"
          >
            <span>complexity</span>
            <div className="flex gap-0.5">
              {Array.from({ length: 10 }).map((_, i) => (
                <div
                  key={i}
                  className={`w-2 h-3 rounded-sm ${
                    i < complexity
                      ? complexity <= 3
                        ? "bg-green"
                        : complexity <= 6
                        ? "bg-amber"
                        : "bg-red-400"
                      : "bg-border"
                  }`}
                />
              ))}
            </div>
            <span className="font-mono">{complexity}/10</span>
          </div>
        </div>

        <div className="divide-y divide-border">
          {CATEGORIES.map(({ key, label }) => {
            const techs = stack[key];
            if (!techs?.length) return null;
            return (
              <div key={key} className="px-4 py-3">
                <div className="text-xs text-muted uppercase tracking-wider mb-2 font-sans">{label}</div>
                <div className="flex flex-wrap gap-2">
                  {techs.map((tech, i) => (
                    <TechPill
                      key={`${tech.name}-${i}`}
                      tech={tech}
                      status={feedbackState[tech.name] ?? null}
                      onCorrect={onTechCorrect}
                      onWrong={onTechWrong}
                    />
                  ))}
                </div>
              </div>
            );
          })}
          <MissingTechPanel onMissingTech={onMissingTech} />
        </div>
      </div>
    </div>
  );
}
