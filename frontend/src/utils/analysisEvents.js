// The analysis event stream, as the UI understands it. Every event from
// /api/v1/analyses/{id}/events passes through normalizeEvent before anything renders it, so a
// change in the backend's event shape is logged and dropped here instead of throwing in render.
// The contract is backend/tests/fixtures/contracts/analysis-event-stream.json (see analysisEvents.test.js).

// The stream ends once an analysis reaches one of these states.
// AWAITING_STAGE: every deployed stage finished; the analysis resumes when the next one can run.
export const FINISHED_STATES = ["READY", "DEGRADED", "FAILED", "AWAITING_STAGE"];

// Used only until the first event arrives with the backend's own stage list.
const DEFAULT_STAGES = ["INGESTING", "EXTRACTING", "INDEXING", "PROJECTING", "SUMMARIZING"];

const KNOWN_STATES = new Set(["QUEUED", ...DEFAULT_STAGES, ...FINISHED_STATES]);

const STAGE_LABELS = {
  INGESTING: "ingest",
  EXTRACTING: "extract",
  INDEXING: "index",
  PROJECTING: "graph",
  SUMMARIZING: "summarize",
};
const STAGE_TONES = { EXTRACTING: "map", SUMMARIZING: "ai" };

const TAGS = {
  QUEUED: ["INF", "inf"],
  AWAITING_STAGE: ["WAIT", "run"],
  READY: ["OK", "ok"],
  DEGRADED: ["WARN", "warn"],
  FAILED: ["ERR", "err"],
};

const optionalString = (value) => (typeof value === "string" && value ? value : null);

/**
 * Validates one parsed event. Returns the event with every field the UI reads in a known type,
 * or null (with a warning) when it lacks the fields needed to place it in the stream.
 */
export function normalizeEvent(raw) {
  if (!raw || typeof raw !== "object" || !Number.isInteger(raw.sequence) || raw.sequence < 1
      || typeof raw.state !== "string" || !raw.state) {
    console.warn("Ignoring an analysis event of unknown shape", raw);
    return null;
  }
  if (!KNOWN_STATES.has(raw.state)) console.warn(`Unknown analysis state ${raw.state}; shown as in progress`);
  const stages = Array.isArray(raw.stages) && raw.stages.length && raw.stages.every((s) => typeof s === "string")
    ? raw.stages
    : null;
  return {
    sequence: raw.sequence,
    state: raw.state,
    message: typeof raw.message === "string" && raw.message ? raw.message : raw.state,
    analysisId: optionalString(raw.analysis_id),
    attemptId: optionalString(raw.attempt_id),
    awaitingStage: optionalString(raw.awaiting_stage),
    lastCompletedStage: optionalString(raw.last_completed_stage),
    failureStage: optionalString(raw.failure_stage),
    waitingReason: optionalString(raw.waiting_reason),
    repository: optionalString(raw.repository),
    stages,
  };
}

/** The rail: the backend's pipeline stages in order, then "ready". */
export function railStages(event) {
  const stages = event?.stages || DEFAULT_STAGES;
  return [
    ...stages.map((state) => ({ state, label: STAGE_LABELS[state] || state.toLowerCase(), tone: STAGE_TONES[state] })),
    { state: "READY", label: "ready" },
  ];
}

/**
 * The rail position of an event: the stage running now; for a parked analysis the stage it waits
 * for; for a failed one the stage that failed. -1 before the first stage, or when unknown.
 */
export function stageIndex(event, stages = railStages(event)) {
  if (!event) return -1;
  const index = (state) => stages.findIndex((stage) => stage.state === state);
  if (event.state === "AWAITING_STAGE") return index(event.awaitingStage);
  if (event.state === "DEGRADED") return index("READY");
  if (event.state === "FAILED") return index(event.failureStage || event.awaitingStage);
  return index(event.state);
}

/** The console tag and its tone: the latest in-progress event runs, earlier ones are done. */
export function eventTag(event, { latest, live }) {
  return TAGS[event.state] || (latest && live ? ["RUN", "run"] : ["OK", "ok"]);
}

/** "github:owner/name" as "github.com/owner/name". */
export function repositoryLabel(canonicalKey) {
  return canonicalKey ? canonicalKey.replace(/^github:/, "github.com/") : null;
}
