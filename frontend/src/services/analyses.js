import { API_BASE, fetchApi } from "../config/api";

// The backend stops streaming once an analysis reaches one of these states.
export const FINISHED_STATES = ["READY", "DEGRADED", "FAILED"];

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

/**
 * Subscribes to /api/v1/analyses/{analysis_id}/events. Events are delivered once each, in order;
 * the stream closes itself after a finished state. Returns a function that closes it early.
 */
export function subscribeToAnalysisEvents(analysisId, { onEvent, onDisconnect }) {
  const source = new EventSource(`${API_BASE}/api/v1/analyses/${encodeURIComponent(analysisId)}/events`);
  let lastSequence = 0;
  let closed = false;

  const close = () => {
    closed = true;
    source.close();
  };

  source.onmessage = (message) => {
    let event;
    try { event = JSON.parse(message.data); } catch { return; }
    if (!Number.isFinite(event.sequence) || event.sequence <= lastSequence) return;
    lastSequence = event.sequence;
    if (FINISHED_STATES.includes(event.state)) close();
    onEvent(event);
  };
  source.onerror = () => {
    if (closed) return;
    close();
    onDisconnect?.();
  };
  return close;
}
