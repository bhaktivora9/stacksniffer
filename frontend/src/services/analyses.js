import { API_BASE, fetchApi } from "../config/api";
import { FINISHED_STATES, normalizeEvent } from "../utils/analysisEvents";

export { FINISHED_STATES };

function errorDetail(body, fallback) {
  const detail = body?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return detail.map((item) => item.msg).join("; ");
  return fallback;
}

/** POST /api/v1/analyses — creates (or reuses) the analysis for the repository's resolved commit. */
export async function createAnalysis(repositoryUrl, { reference } = {}) {
  const response = await fetchApi("/api/v1/analyses", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      repository_url: repositoryUrl,
      ...(reference ? { reference } : {}),
      client_request_id: crypto.randomUUID(),
    }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(errorDetail(body, `Analysis request failed (${response.status})`));
  }
  return body;
}

/** GET /api/v1/analyses/{id}/overview — repository, counts, graph and the community map. */
export async function getOverview(analysisId) {
  const response = await fetchApi(`/api/v1/analyses/${encodeURIComponent(analysisId)}/overview`);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorDetail(body, `Overview request failed (${response.status})`));
  return body;
}

/** POST /api/v1/analyses/{id}/ask — a grounded answer; mode "local", "global" or null (routed). */
export async function askQuestion(analysisId, question, mode = null) {
  const response = await fetchApi(`/api/v1/analyses/${encodeURIComponent(analysisId)}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, ...(mode ? { mode } : {}) }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorDetail(body, `Question failed (${response.status})`));
  return body;
}

// Consecutive connection failures tolerated before giving up; the browser waits between retries.
const MAX_RECONNECTS = 3;

/**
 * Subscribes to /api/v1/analyses/{analysis_id}/events. Events arrive normalized (see
 * utils/analysisEvents), once each and in order; the stream closes itself after a finished state.
 * A dropped connection is retried by the browser: the server resumes numbering from Last-Event-ID
 * and resends the current state, which is skipped when nothing changed. onDisconnect runs only
 * after MAX_RECONNECTS failures in a row. Returns a function that closes the stream early.
 */
export function subscribeToAnalysisEvents(analysisId, { onEvent, onDisconnect }) {
  const source = new EventSource(`${API_BASE}/api/v1/analyses/${encodeURIComponent(analysisId)}/events`);
  let lastSequence = 0;
  let lastState = null;
  let failures = 0;
  let closed = false;

  const close = () => {
    closed = true;
    source.close();
  };

  source.onopen = () => { failures = 0; };
  source.onmessage = (message) => {
    let raw;
    try { raw = JSON.parse(message.data); } catch { console.warn("Ignoring a malformed analysis event", message.data); return; }
    const event = normalizeEvent(raw);
    if (!event || event.sequence <= lastSequence) return;
    lastSequence = event.sequence;
    const state = `${event.state}/${event.attemptId}`;
    if (FINISHED_STATES.includes(event.state)) close();
    if (state === lastState) return; // the current state, resent after a reconnect
    lastState = state;
    onEvent(event);
  };
  source.onerror = () => {
    if (closed) return;
    failures += 1;
    if (source.readyState === EventSource.CONNECTING && failures <= MAX_RECONNECTS) return;
    close();
    onDisconnect?.();
  };
  return close;
}
