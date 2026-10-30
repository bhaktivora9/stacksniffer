const configuredApiBase =
  import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "") || "";

function shouldUseDevProxy(apiBase) {
  if (!import.meta.env.DEV || !apiBase) return false;

  try {
    const url = new URL(apiBase);
    return ["localhost", "127.0.0.1", "::1", "[::1]"].includes(url.hostname)
      && url.port === "8000";
  } catch {
    return false;
  }
}

export const API_BASE = shouldUseDevProxy(configuredApiBase)
  ? ""
  : configuredApiBase;

function apiBaseUrl() {
  const appOrigin = typeof window === "undefined" ? "http://localhost" : window.location.origin;
  return new URL(API_BASE || "/", appOrigin);
}

export function apiUrl(path) {
  const baseUrl = apiBaseUrl();
  const targetUrl = new URL(String(path), baseUrl);

  if (targetUrl.origin !== baseUrl.origin) {
    throw new Error("Refusing to call an untrusted API origin");
  }

  return targetUrl.toString();
}

async function logFailedResponse(url, response) {
  const body = await response.clone().text().catch(() => "");
  console.error("[api] request failed", {
    url,
    status: response.status,
    statusText: response.statusText,
    body: body.slice(0, 500),
  });
}

export async function fetchApi(path, options = {}) {
  const url = apiUrl(path);
  try {
    const response = await fetch(url, options);
    if (!response.ok) await logFailedResponse(url, response);
    return response;
  } catch (error) {
    console.error("[api] request failed", { url, error });
    throw error;
  }
}
