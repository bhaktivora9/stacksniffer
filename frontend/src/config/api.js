export const API_BASE =
  import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "") || "";

let adminAccessToken = null;
const authListeners = new Set();

function notifyAuthListeners() {
  authListeners.forEach((listener) => listener(adminAccessToken));
}

export function getAdminAccessToken() {
  return adminAccessToken;
}

export function setAdminAccessToken(token) {
  adminAccessToken = token || null;
  notifyAuthListeners();
}

export function clearAdminAccessToken() {
  setAdminAccessToken(null);
}

export function subscribeAdminAuth(listener) {
  authListeners.add(listener);
  return () => authListeners.delete(listener);
}

function adminAuthHeaders(headers = {}) {
  if (!adminAccessToken) return headers;
  return {
    ...headers,
    Authorization: `Bearer ${adminAccessToken}`,
  };
}

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

export function adminFetch(path, options = {}) {
  const { headers, ...rest } = options;
  return fetch(apiUrl(path), {
    ...rest,
    headers: adminAuthHeaders(headers),
  });
}
