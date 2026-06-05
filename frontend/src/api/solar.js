const configuredApiBase = import.meta.env.VITE_API_BASE_URL ?? "";
const API_BASE_URL = configuredApiBase.replace(/\/$/, "");

async function requestSolar(path, { signal } = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, { signal });
  if (!response.ok) {
    throw new Error(`Solar API ${response.status}`);
  }
  return response.json();
}

function withAt(path, at) {
  if (!at) return path;
  const params = new URLSearchParams({ at });
  return `${path}?${params.toString()}`;
}

export function fetchSolarDashboard({ at = null, signal } = {}) {
  return requestSolar(withAt("/api/solar/dashboard", at), { signal });
}

export function fetchSolarWeatherCurrent({ at = null, signal } = {}) {
  return requestSolar(withAt("/api/solar/weather-current", at), { signal });
}
