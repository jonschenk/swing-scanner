import { useCallback, useEffect, useRef, useState } from "react";
import {
  getScanStatus,
  getHealth,
  getSettings,
  saveSettings,
  startScan,
  refreshScan,
  analyzeTicker,
} from "./api.js";
import StockCard from "./components/StockCard.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";

const POLL_INTERVAL_MS = 1500;
const REFRESH_INTERVAL_MS = 180_000; // auto-refresh loaded setups every 3 min

function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.round(totalSeconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${String(s % 60).padStart(2, "0")}s`;
}

function formatClock(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleTimeString();
}

export default function App() {
  const [backendUp, setBackendUp] = useState(null);
  const [scan, setScan] = useState({ status: "idle", progress: "", results: [] });
  const [settings, setSettings] = useState(null);
  const [capital, setCapital] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const [error, setError] = useState(null);
  const [now, setNow] = useState(Date.now() / 1000); // ticks each second while running
  const pollRef = useRef(null);
  const refreshingRef = useRef(false);

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
      if (state.status !== "running" && state.status !== "analyzing") stopPolling();
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
        if (state.status === "running" || state.status === "analyzing") beginPolling();
      } catch {
        setBackendUp(false);
      }
    })();
    return stopPolling;
  }, [beginPolling, refreshSettings]);

  const running = scan.status === "running"; // downloading + filtering (no cards yet)
  const analyzing = scan.status === "analyzing"; // cards shown, AI streaming in
  const busy = running || analyzing;

  // Live elapsed clock — ticks while the scan or AI phase is working.
  useEffect(() => {
    if (!busy) return;
    setNow(Date.now() / 1000);
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, [busy]);

  // Auto-refresh the loaded setups every few minutes (cheap: only the displayed
  // tickers, no AI, no re-scan). Active only when a finished scan has results.
  const doRefresh = useCallback(async () => {
    if (refreshingRef.current) return;
    refreshingRef.current = true;
    try {
      const state = await refreshScan();
      setScan(state);
    } catch {
      /* refresh is best-effort */
    } finally {
      refreshingRef.current = false;
    }
  }, []);

  useEffect(() => {
    if (scan.status !== "done" || (scan.results ?? []).length === 0) return;
    const id = setInterval(doRefresh, REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [scan.status, scan.results, doRefresh]);

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

  // On-demand AI for a single card (the ones beyond the auto-analyzed top N).
  const onAnalyze = useCallback(async (ticker) => {
    setScan((s) => ({
      ...s,
      results: s.results.map((r) => (r.ticker === ticker ? { ...r, ai_status: "pending" } : r)),
    }));
    try {
      const state = await analyzeTicker(ticker);
      setScan(state);
    } catch {
      setScan((s) => ({
        ...s,
        results: s.results.map((r) =>
          r.ticker === ticker && !r.ai ? { ...r, ai_status: "idle" } : r,
        ),
      }));
    }
  }, []);

  const onRunScan = async (fresh = false) => {
    setError(null);
    try {
      await startScan(fresh);
      setScan((s) => ({ ...s, status: "running", progress: "Starting scan…", started_at: Date.now() / 1000 }));
      beginPolling();
    } catch (e) {
      setError(e.message);
    }
  };

  const results = scan.results ?? [];
  const elapsed = busy && scan.started_at ? now - scan.started_at : 0;
  const analyzedCount = results.filter((r) => r.ai).length;
  const scanDuration =
    scan.started_at && scan.scanned_at ? scan.scanned_at - scan.started_at : null;
  const loadDuration =
    scan.status === "done" && scan.started_at && scan.finished_at
      ? scan.finished_at - scan.started_at
      : null;
  const lastUpdated = scan.refreshed_at ?? scan.finished_at;

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
          <button
            className="btn ghost"
            onClick={() => onRunScan(true)}
            disabled={busy || backendUp === false}
            title="Force a full re-download of fresh prices (ignore the cache)"
          >
            ↻ Fresh
          </button>
          <button className="btn primary" onClick={() => onRunScan(false)} disabled={busy || backendUp === false}>
            {busy ? <span className="spinner" /> : null}
            {running ? "Scanning…" : analyzing ? "Analyzing…" : "Run Scan"}
          </button>
        </div>
      </header>

      {backendUp === false && (
        <div className="banner error">
          Can't reach the backend at 127.0.0.1:8765 — start it and relaunch the app.
        </div>
      )}
      {error && <div className="banner error">{error}</div>}

      {busy && (
        <div className="banner progress">
          <span className="spinner" />
          <span>{scan.progress || "Scanning…"}</span>
          <span className="elapsed">{formatDuration(elapsed)} elapsed</span>
        </div>
      )}

      {scan.status === "error" && <div className="banner error">Scan failed: {scan.error}</div>}

      {analyzing && results.length > 0 && (
        <div className="scan-meta muted small">
          {scanDuration != null && <span>Found {results.length} setups in {formatDuration(scanDuration)}</span>}
          <span> · <span className="spinner tiny" /> AI analyzing {analyzedCount}/{results.length}…</span>
        </div>
      )}

      {scan.status === "done" && results.length > 0 && (
        <div className="scan-meta muted small">
          {loadDuration != null && <span>Loaded {results.length} setups in {formatDuration(loadDuration)}</span>}
          {scan.from_cache && <span className="cache-tag"> · ⚡ cached prices</span>}
          {lastUpdated && <span> · updated {formatClock(lastUpdated)}</span>}
          {scan.refreshing ? (
            <span className="refreshing"> · <span className="spinner tiny" /> refreshing…</span>
          ) : (
            <span> · auto-refreshes every 3 min</span>
          )}
        </div>
      )}

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
            <StockCard key={stock.ticker} stock={stock} onAnalyze={onAnalyze} />
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
