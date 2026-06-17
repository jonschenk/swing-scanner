const BASE_URL = "http://127.0.0.1:8765";

async function request(path, options = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json();
}

export const getSettings = () => request("/api/settings");
export const saveSettings = (settings) =>
  request("/api/settings", { method: "PUT", body: JSON.stringify(settings) });
export const startScan = (fresh = false) =>
  request(`/api/scan?fresh=${fresh ? "true" : "false"}`, { method: "POST" });
export const refreshScan = () => request("/api/refresh", { method: "POST" });
export const analyzeTicker = (ticker) =>
  request("/api/analyze", { method: "POST", body: JSON.stringify({ ticker }) });
export const getScanStatus = () => request("/api/scan/status");
export const getHealth = () => request("/api/health");
export const startLive = () => request("/api/live/start", { method: "POST" });
export const stopLive = () => request("/api/live/stop", { method: "POST" });
export const getLivePrices = () => request("/api/live");
