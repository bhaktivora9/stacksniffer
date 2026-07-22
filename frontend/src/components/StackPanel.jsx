import { useEffect, useState } from "react";
import { Check, Plus, Trash2, X } from "lucide-react";
import AiInsightsCard from "./AiInsightsCard";
import { API_BASE } from "../config/api";

const FALLBACK_CATEGORIES = [
  "languages", "frameworks", "databases", "messaging",
  "ai_ml", "infra", "testing", "library",
].map((id) => ({ id, label: id.replace(/_/g, " ") }));

const LONG_TAIL_COLLAPSE_THRESHOLD = 8;

function isExpandedByDefault(category, techCount) {
  return !(
    (category === "testing" || category === "library")
    && techCount > LONG_TAIL_COLLAPSE_THRESHOLD
  );
}

function humanizeCategory(category) {
  return category
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

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

function TechPill({ tech, status, onCorrect, onWrong, isPrimary = false, showConfidence = true }) {
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
        className={`group flex items-center gap-2 px-2.5 py-1.5 border rounded transition-all duration-150 ${
          confirmed
            ? "border-border border-l-green border-l-2"
            : falsePositive
            ? "border-border opacity-50"
            : isPrimary
            ? "border-accent/40 bg-accent/10 hover:border-accent/60"
            : "border-border bg-bg hover:border-muted"
        }`}
      >
        <div className="flex items-center gap-1.5 min-w-0">
          <SourceDot source={tech.detection_source} />
          <span className={`text-sm text-text truncate font-sans ${falsePositive ? "line-through" : ""}`}>
            {tech.name}
          </span>
          {isPrimary && (
            <span className="text-[9px] font-mono font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-accent/15 text-accent border border-accent/25 shrink-0">
              Primary
            </span>
          )}
          {tech.version && (
            <span className="text-xs text-muted font-mono shrink-0">{tech.version}</span>
          )}
        </div>
        {showConfidence && (
          <div className="shrink-0 flex items-center gap-1">
            <div className="w-10 h-1 bg-border rounded-full overflow-hidden">
              <div
                className="h-full bg-accent rounded-full"
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        )}
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

function colorForLanguage(name) {
  let hash = 0;
  for (const character of name) {
    hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  }
  return `hsl(${hash % 360} 65% 55%)`;
}

function LanguageBreakdown({ languages = [] }) {
  const sortedLanguages = [...languages].sort(
    (left, right) => (right.byte_share ?? -1) - (left.byte_share ?? -1)
  );
  const measuredLanguages = sortedLanguages.filter(
    (language) => language.byte_share != null
  );

  return (
    <div>
      {measuredLanguages.length > 0 && (
        <div className="flex h-2 w-full overflow-hidden rounded bg-border">
          {measuredLanguages.map((language) => (
            <div
              key={language.name}
              className="h-full"
              style={{
                width: `${language.byte_share * 100}%`,
                background: colorForLanguage(language.name),
              }}
              title={`${language.name} ${(language.byte_share * 100).toFixed(1)}%`}
            />
          ))}
        </div>
      )}
      <div className={`${measuredLanguages.length ? "mt-3" : ""} flex flex-wrap gap-x-4 gap-y-2`}>
        {sortedLanguages.map((language) => (
          <span key={language.name} className="inline-flex items-center gap-1.5 text-sm text-text">
            <span
              className="h-2.5 w-2.5 shrink-0 rounded-full"
              style={{ background: colorForLanguage(language.name) }}
            />
            {language.name}
            {language.byte_share != null && (
              <span className="font-mono text-xs text-muted">
                {(language.byte_share * 100).toFixed(1)}%
              </span>
            )}
          </span>
        ))}
      </div>
    </div>
  );
}

function MissingTechPanel({ onMissingTech, categories }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState([{ tech_name: "", category: "frameworks" }]);
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submitMissing() {
    const missingTechs = items
      .map((item) => ({ ...item, tech_name: item.tech_name.trim() }))
      .filter((item) => item.tech_name);
    if (!missingTechs.length) return;
    setSubmitting(true);
    const ok = await onMissingTech?.(missingTechs);
    setSubmitting(false);
    if (ok) {
      setMessage(`${missingTechs.length} technolog${missingTechs.length === 1 ? "y" : "ies"} reported. Will be added to pattern discovery queue.`);
      setItems([{ tech_name: "", category: "frameworks" }]);
    }
  }

  function updateItem(index, field, value) {
    setItems((current) => current.map((item, i) => i === index ? { ...item, [field]: value } : item));
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
          <div className="space-y-2">
            {items.map((item, index) => (
              <div key={index} className="flex flex-wrap gap-2">
                <input
                  value={item.tech_name}
                  onChange={(event) => updateItem(index, "tech_name", event.target.value)}
                  placeholder="e.g. Redis"
                  className="min-w-[160px] flex-1 rounded border border-border bg-surface px-3 py-2 text-sm text-text outline-none focus:border-accent"
                />
                <select
                  value={item.category}
                  onChange={(event) => updateItem(index, "category", event.target.value)}
                  className="rounded border border-border bg-surface px-3 py-2 text-sm text-text outline-none focus:border-accent"
                >
                  {categories.map(({ id, label }) => <option key={id} value={id}>{label}</option>)}
                </select>
                {items.length > 1 && (
                  <button type="button" onClick={() => setItems((current) => current.filter((_, i) => i !== index))} className="p-2 text-muted hover:text-red-400" aria-label={`Remove technology ${index + 1}`}>
                    <Trash2 size={15} />
                  </button>
                )}
              </div>
            ))}
            <button type="button" onClick={() => setItems((current) => [...current, { tech_name: "", category: "frameworks" }])} className="inline-flex items-center gap-1 text-xs text-accent hover:text-accent/80">
              <Plus size={13} /> Add another
            </button>
            <div>
            <button
              type="button"
              onClick={submitMissing}
              disabled={submitting || !items.some((item) => item.tech_name.trim())}
              className="rounded border border-accent/30 bg-accent/10 px-3 py-2 text-sm text-accent transition-colors hover:bg-accent/15 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {submitting ? "Reporting..." : "Report"}
            </button>
            </div>
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
  onPrimaryLanguageChange,
}) {
  const [correctingPrimary, setCorrectingPrimary] = useState(false);
  const [selectedPrimary, setSelectedPrimary] = useState(stack?.primary_language ?? "");
  const [savingPrimary, setSavingPrimary] = useState(false);
  const [categoryOptions, setCategoryOptions] = useState(FALLBACK_CATEGORIES);
  const [expandedSections, setExpandedSections] = useState({});

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/taxonomy/categories`)
      .then((response) => response.ok ? response.json() : Promise.reject(response.status))
      .then((data) => { if (!cancelled && data.categories?.length) setCategoryOptions(data.categories); })
      .catch(() => { if (!cancelled) setCategoryOptions(FALLBACK_CATEGORIES); });
    return () => { cancelled = true; };
  }, []);

  const builtinCategoryIds = categoryOptions.map((category) => category.id);
  const visibleEmergentCategories = (stack?.emergent_categories ?? []).filter(
    (key) => Array.isArray(stack?.[key]) && stack[key].length > 0
  );
  const visibleCategoryIds = [
    ...builtinCategoryIds.filter((key) => Array.isArray(stack?.[key]) && stack[key].length > 0),
    ...visibleEmergentCategories,
  ];
  const categoryStateSignature = visibleCategoryIds
    .map((key) => `${key}:${stack?.[key]?.length ?? 0}`)
    .join("|");

  useEffect(() => {
    if (!stack) return;
    setExpandedSections(
      Object.fromEntries(
        visibleCategoryIds.map((key) => [
          key,
          isExpandedByDefault(key, stack[key]?.length ?? 0),
        ])
      )
    );
  // Reset disclosure state for a new analysis or a materially different result set.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [analysisId, categoryStateSignature]);

  if (!stack || !repo) return null;

  const complexity = stack.complexity_score ?? 0;
  const builtinOrder = categoryOptions.map((category) => category.id);
  const categoryLabels = Object.fromEntries(
    categoryOptions.map((category) => [category.id, category.label])
  );
  const emergentCategories = (stack.emergent_categories ?? []).filter(
    (key) => Array.isArray(stack[key]) && stack[key].length > 0
  );
  const presentCategories = [
    ...builtinOrder.filter((key) => Array.isArray(stack[key]) && stack[key].length > 0),
    ...emergentCategories,
  ];
  const orderedCategories = [
    ...builtinOrder.filter((key) => presentCategories.includes(key)),
    ...emergentCategories,
  ];
  const expandedCount = orderedCategories.filter((key) => expandedSections[key]).length;
  const majorityExpanded = expandedCount >= Math.ceil(orderedCategories.length / 2);
  const primaryLanguage = stack.primary_language
    ? stack.languages?.find(
        (tech) => tech.name.toLowerCase() === stack.primary_language.toLowerCase()
      ) ?? { name: stack.primary_language, confidence: 0 }
    : null;
  async function savePrimaryLanguage() {
    if (!selectedPrimary || selectedPrimary === stack.primary_language) {
      setCorrectingPrimary(false);
      return;
    }
    setSavingPrimary(true);
    const ok = await onPrimaryLanguageChange?.(selectedPrimary);
    setSavingPrimary(false);
    if (ok) setCorrectingPrimary(false);
  }

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
            {orderedCategories.length > 0 && (
              <button
                type="button"
                onClick={() => {
                  const nextExpanded = !majorityExpanded;
                  setExpandedSections(
                    Object.fromEntries(orderedCategories.map((key) => [key, nextExpanded]))
                  );
                }}
                className="text-xs text-muted hover:text-accent transition-colors"
              >
                {majorityExpanded ? "Collapse all" : "Expand all"}
              </button>
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
          {primaryLanguage && (
            <div className="px-4 py-3 bg-accent/[0.03]">
              <div className="text-xs text-accent uppercase tracking-wider mb-2 font-sans font-medium">
                Primary language
              </div>
              <div className="flex flex-wrap gap-2">
                <TechPill
                  tech={primaryLanguage}
                  status={feedbackState[primaryLanguage.name] ?? null}
                  onCorrect={onTechCorrect}
                  onWrong={onTechWrong}
                  isPrimary
                  showConfidence={false}
                />
                <button type="button" onClick={() => setCorrectingPrimary((value) => !value)} className="text-xs text-muted hover:text-accent transition-colors">
                  Correct
                </button>
              </div>
              {correctingPrimary && (
                <div className="mt-3 flex flex-wrap gap-2">
                  <select value={selectedPrimary} onChange={(event) => setSelectedPrimary(event.target.value)} className="rounded border border-border bg-bg px-3 py-1.5 text-sm text-text outline-none focus:border-accent">
                    {stack.languages?.map((tech) => <option key={tech.name} value={tech.name}>{tech.name}</option>)}
                  </select>
                  <button type="button" onClick={savePrimaryLanguage} disabled={savingPrimary} className="rounded border border-accent/30 bg-accent/10 px-3 py-1.5 text-xs text-accent disabled:opacity-60">
                    {savingPrimary ? "Saving..." : "Save correction"}
                  </button>
                </div>
              )}
            </div>
          )}
          {orderedCategories.map((key) => {
            const label = categoryLabels[key] ?? humanizeCategory(key);
            const isEmergent = !builtinOrder.includes(key);
            const techs = stack[key];
            if (!techs?.length) return null;
            const isExpanded = Boolean(expandedSections[key]);
            const flaggedTechNames = new Set(
              (stack.flags ?? [])
                .map((flag) => flag.tech?.toLowerCase())
                .filter(Boolean)
            );
            const hasFlaggedTech = techs.some((tech) => (
              tech.flagged
              || tech.quality_flag
              || flaggedTechNames.has(tech.name.toLowerCase())
              || feedbackState[tech.name] === "false_positive"
            ));
            const panelId = `stack-category-${key.replace(/[^a-z0-9_-]/gi, "-")}`;
            return (
              <div key={key}>
                <button
                  type="button"
                  aria-expanded={isExpanded}
                  aria-controls={panelId}
                  onClick={() => setExpandedSections((current) => ({
                    ...current,
                    [key]: !isExpanded,
                  }))}
                  className="flex w-full items-center gap-2 px-4 py-3 text-left transition-colors hover:bg-bg/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
                >
                  <span className="w-3 text-xs text-muted" aria-hidden="true">
                    {isExpanded ? "▾" : "▸"}
                  </span>
                  <span className="text-xs text-muted uppercase tracking-wider font-sans">{label}</span>
                  <span className="rounded-full border border-border bg-bg px-1.5 py-0.5 text-[10px] font-mono text-muted">
                    {techs.length}
                  </span>
                  {isEmergent && (
                    <span className="rounded-full border border-ai-purple/30 bg-ai-purple/10 px-1.5 py-0.5 text-[9px] font-mono uppercase text-ai-purple">
                      new category
                    </span>
                  )}
                  {!isExpanded && hasFlaggedTech && (
                    <span className="ml-auto rounded-full border border-amber/30 bg-amber/10 px-1.5 py-0.5 text-[9px] font-mono uppercase text-amber">
                      flagged
                    </span>
                  )}
                </button>
                {isExpanded && (
                  <div
                    id={panelId}
                    className="max-h-80 overflow-y-auto overscroll-contain px-4 pb-3 pl-9 pr-3"
                    tabIndex={techs.length > LONG_TAIL_COLLAPSE_THRESHOLD ? 0 : undefined}
                    aria-label={`${label} technologies`}
                  >
                    {key === "languages" ? (
                      <LanguageBreakdown languages={techs} />
                    ) : (
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
                    )}
                  </div>
                )}
              </div>
            );
          })}
          <MissingTechPanel onMissingTech={onMissingTech} categories={categoryOptions} />
        </div>
      </div>
    </div>
  );
}
