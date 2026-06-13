import { useCallback, useEffect, useRef, useState } from "react";
import { getScanStatus, getHealth, getSettings, saveSettings, startScan } from "./api.js";
import StockCard from "./components/StockCard.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";

const POLL_INTERVAL_MS = 1500;

export default function App() {
  const [backendUp, setBackendUp] = useState(null);
  const [scan, setScan] = useState({ status: "idle", progress: "", results: [] });
  const [settings, setSettings] = useState(null);
  const [capital, setCapital] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const [error, setError] = useState(null);
  const pollRef = useRef(null);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const refreshSettings = useCallback(async () => {
    try {
      const s = await getSettings();
      setSettings(s);
      setCapital(String(s.capital));
    } catch {
      /* settings load is non-fatal */
    }
  }, []);

  const poll = useCallback(async () => {
    try {
      const state = await getScanStatus();
      setScan(state);
      if (state.status !== "running") stopPolling();
    } catch (e) {
      setError(e.message);
      stopPolling();
    }
  }, []);

  const beginPolling = useCallback(() => {
    stopPolling();
    pollRef.current = setInterval(poll, POLL_INTERVAL_MS);
  }, [poll]);

  // On launch: confirm the backend is up, load settings, pick up prior scan state.
  useEffect(() => {
    (async () => {
      try {
        await getHealth();
        setBackendUp(true);
        await refreshSettings();
        const state = await getScanStatus();
        setScan(state);
        if (state.status === "running") beginPolling();
      } catch {
        setBackendUp(false);
      }
    })();
    return stopPolling;
  }, [beginPolling, refreshSettings]);

  const commitCapital = async () => {
    const value = Number(capital);
    if (!settings || !value || value === settings.capital) return;
    try {
      const saved = await saveSettings({ ...settingsPayload(settings), capital: value });
      setSettings(saved);
      setCapital(String(saved.capital));
    } catch (e) {
      setError(e.message);
      setCapital(String(settings.capital)); // revert
    }
  };

  const onRunScan = async () => {
    setError(null);
    try {
      await startScan();
      setScan((s) => ({ ...s, status: "running", progress: "Starting scan…" }));
      beginPolling();
    } catch (e) {
      setError(e.message);
    }
  };

  const running = scan.status === "running";
  const results = scan.results ?? [];

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-dot" />
          <h1>Swing Scanner</h1>
          <span className="brand-sub">2–5 day setups · uptrend pullbacks</span>
        </div>
        <div className="topbar-actions">
          <label className="capital-input" title="Your trading capital — drives position sizing and the price ceiling">
            <span>$</span>
            <input
              type="number"
              min="1"
              step="100"
              value={capital}
              onChange={(e) => setCapital(e.target.value)}
              onBlur={commitCapital}
              onKeyDown={(e) => e.key === "Enter" && e.target.blur()}
              disabled={!settings}
            />
          </label>
          {settings && (
            <span className="ceiling muted small" title="Max share price = capital × max position %">
              ≤ ${settings.max_price?.toLocaleString()}/share
            </span>
          )}
          {settings && (
            <span
              className="universe-chip"
              title={
                settings.universe === "full"
                  ? "Scanning the full US market (~5,900 stocks). Change in Settings."
                  : "Scanning the curated list (~675 stocks). Change in Settings."
              }
            >
              {settings.universe === "full" ? "🌐 Full market" : "★ Curated"}
            </span>
          )}
          <button className="btn ghost" onClick={() => setShowSettings(true)}>
            Settings
          </button>
          <button className="btn primary" onClick={onRunScan} disabled={running || backendUp === false}>
            {running ? <span className="spinner" /> : null}
            {running ? "Scanning…" : "Run Scan"}
          </button>
        </div>
      </header>

      {backendUp === false && (
        <div className="banner error">
          Can't reach the backend at 127.0.0.1:8765 — start it and relaunch the app.
        </div>
      )}
      {error && <div className="banner error">{error}</div>}

      {running && (
        <div className="banner progress">
          <span className="spinner" />
          {scan.progress || "Scanning…"}
        </div>
      )}

      {scan.status === "error" && <div className="banner error">Scan failed: {scan.error}</div>}

      <main>
        {!running && scan.status === "done" && results.length === 0 && (
          <div className="empty">
            <p>No stocks passed the scan with the current criteria.</p>
            <p className="muted">
              Try raising your capital, loosening the RSI/ADX thresholds, or lowering the min ATR% in Settings.
            </p>
          </div>
        )}

        {!running && scan.status === "idle" && (
          <div className="empty">
            <p>Hit <strong>Run Scan</strong> to find leader pullbacks sized to your account.</p>
            <p className="muted">
              Market leaders (high relative strength · near 52w highs · 20&gt;50&gt;200 SMA, rising)
              taking a healthy breather (RSI 40–60 · strong ADX · tradeable ATR%).
            </p>
          </div>
        )}

        <div className="grid">
          {results.map((stock) => (
            <StockCard key={stock.ticker} stock={stock} />
          ))}
        </div>
      </main>

      {showSettings && (
        <SettingsPanel
          onClose={() => {
            setShowSettings(false);
            refreshSettings();
          }}
        />
      )}
    </div>
  );
}

// Build a full settings payload (computed fields like max_price are read-only).
function settingsPayload(s) {
  const { max_price, ...rest } = s;
  return rest;
}
