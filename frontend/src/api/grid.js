const configuredApiBase = import.meta.env.VITE_API_BASE_URL ?? "";
const API_BASE_URL = configuredApiBase.replace(/\/$/, "");

async function requestGrid(path, { signal } = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, { signal });
  if (!response.ok) {
    throw new Error(`Grid API ${response.status}`);
  }
  return response.json();
}

export function fetchGridCurrent({ at = null, signal } = {}) {
  const params = new URLSearchParams();
  if (at) params.set("at", at);
  const query = params.toString();
  return requestGrid(`/api/grid/current${query ? `?${query}` : ""}`, { signal });
}

export function fetchGridOutages(dateKey, options) {
  const params = new URLSearchParams({ date: dateKey });
  return requestGrid(`/api/grid/outages?${params.toString()}`, options);
}

export function fetchGridHistory(startIso, endIso, options) {
  const params = new URLSearchParams({
    start: startIso,
    end: endIso,
  });
  return requestGrid(`/api/grid/history?${params.toString()}`, options);
}
