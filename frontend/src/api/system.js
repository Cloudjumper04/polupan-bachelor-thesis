const configuredApiBase = import.meta.env.VITE_API_BASE_URL ?? "";
const API_BASE_URL = configuredApiBase.replace(/\/$/, "");

async function requestSystem(path, { signal } = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, { signal });
  if (!response.ok) {
    throw new Error(`System API ${response.status}`);
  }
  return response.json();
}

export function fetchSystemDashboard({ at = null, signal } = {}) {
  const params = new URLSearchParams();
  if (at) params.set("at", at);
  const query = params.toString();
  return requestSystem(`/api/system/dashboard${query ? `?${query}` : ""}`, {
    signal,
  });
}

export function fetchDashboardRange(options) {
  return requestSystem("/api/dashboard/range", options);
}
