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
export const startScan = (fresh = false, strategy = "leader_pullback") =>
  request(`/api/scan?fresh=${fresh ? "true" : "false"}&strategy=${strategy}`, { method: "POST" });
export const refreshScan = () => request("/api/refresh", { method: "POST" });
export const analyzeTicker = (ticker) =>
  request("/api/analyze", { method: "POST", body: JSON.stringify({ ticker }) });
export const deepAnalyze = (ticker, positions = []) =>
  request("/api/trade-case", { method: "POST", body: JSON.stringify({ ticker, positions }) });
export const getRecommendations = (positions = [], top_n = 12) =>
  request("/api/recommend", { method: "POST", body: JSON.stringify({ positions, top_n }) });
export const getScanStatus = () => request("/api/scan/status");
export const getHealth = () => request("/api/health");
export const getRegime = () => request("/api/regime");
export const getStrategies = () => request("/api/strategies");
export const setActiveStrategy = (id) =>
  request("/api/strategies/active", { method: "POST", body: JSON.stringify({ id }) });
export const getPaperAccount = () => request("/api/paper/account");
export const getJournal = () => request("/api/journal");
export const paperBuy = (ticker) =>
  request("/api/paper/buy", { method: "POST", body: JSON.stringify({ ticker }) });
export const paperClose = (trade_id) =>
  request("/api/paper/close", { method: "POST", body: JSON.stringify({ trade_id }) });
export const paperReset = (capital) =>
  request("/api/paper/reset", { method: "POST", body: JSON.stringify({ capital }) });
export const startLive = () => request("/api/live/start", { method: "POST" });
export const stopLive = () => request("/api/live/stop", { method: "POST" });
export const getLivePrices = () => request("/api/live");
