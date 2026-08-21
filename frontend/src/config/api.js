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

export function adminAuthHeaders(headers = {}) {
  if (!adminAccessToken) return headers;
  return {
    ...headers,
    Authorization: `Bearer ${adminAccessToken}`,
  };
}

export function apiUrl(path) {
  return path.startsWith("http") ? path : `${API_BASE}${path}`;
}

export function adminFetch(path, options = {}) {
  const { headers, ...rest } = options;
  return fetch(apiUrl(path), {
    ...rest,
    headers: adminAuthHeaders(headers),
  });
}
