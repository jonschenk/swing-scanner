import { useCallback, useEffect, useRef, useState } from "react";
import {
  getScanStatus,
  getHealth,
  getSettings,
  saveSettings,
  startScan,
  refreshScan,
  analyzeTicker,
  startLive,
  stopLive,
  getLivePrices,
} from "./api.js";
import StockCard from "./components/StockCard.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";

const POLL_INTERVAL_MS = 1500;
const REFRESH_INTERVAL_MS = 180_000; // auto-refresh loaded setups every 3 min
const LIVE_POLL_INTERVAL_MS = 4000; // pull the latest streamed prices from our backend

function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.round(totalSeconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${String(s % 60).padStart(2, "0")}s`;
}

function formatClock(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleTimeString();
}

// ThinkorSwim uses a dot for class shares (BRK.B); we store Yahoo's dash (BRK-B).
function tosSymbols(results) {
  return results.map((r) => r.ticker.replace(/-/g, "."));
}

export default function App() {
  const [backendUp, setBackendUp] = useState(null);
  const [scan, setScan] = useState({ status: "idle", progress: "", results: [] });
  const [settings, setSettings] = useState(null);
  const [capital, setCapital] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const [error, setError] = useState(null);
  const [now, setNow] = useState(Date.now() / 1000); // ticks each second while running
  const [exportNote, setExportNote] = useState(""); // transient "copied"/"saved" confirmation
  const [liveOn, setLiveOn] = useState(false); // streaming live prices for displayed cards
  const [livePrices, setLivePrices] = useState({}); // ticker -> {price, change_percent}
  const pollRef = useRef(null);
  const liveRef = useRef(null);
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

  const flashExportNote = (msg) => {
    setExportNote(msg);
    setTimeout(() => setExportNote(""), 2000);
  };

  // Copy all displayed tickers to the clipboard for ThinkorSwim's watchlist
  // "Paste symbols from clipboard" import (the most reliable path). One per line
  // also makes the list handy to paste anywhere else.
  const copyForToS = async () => {
    const syms = tosSymbols(results);
    if (!syms.length) return;
    try {
      await navigator.clipboard.writeText(syms.join("\n"));
      flashExportNote(`Copied ${syms.length} tickers`);
    } catch {
      flashExportNote("Clipboard unavailable");
    }
  };

  // Download the tickers as a .csv for ThinkorSwim's file import (Watchlist menu
  // -> Import). One symbol per line, no header — ToS detects the symbols.
  const downloadWatchlist = () => {
    const syms = tosSymbols(results);
    if (!syms.length) return;
    const blob = new Blob([syms.join("\n") + "\n"], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "swing-scanner-watchlist.csv";
    a.click();
    URL.revokeObjectURL(url);
    flashExportNote(`Saved ${syms.length} tickers`);
  };

  // Signature of the tickers on screen — re-subscribe the stream when it changes.
  const tickerKey = results.map((r) => r.ticker).join(",");

  // Stream live prices for the displayed cards: subscribe the backend to them,
  // then poll the latest streamed prices over localhost while live mode is on.
  useEffect(() => {
    const stopLivePolling = () => {
      if (liveRef.current) {
        clearInterval(liveRef.current);
        liveRef.current = null;
      }
    };
    if (!liveOn || scan.status !== "done" || !tickerKey) {
      stopLivePolling();
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        await startLive();
        const pull = async () => {
          try {
            const prices = await getLivePrices();
            if (!cancelled) setLivePrices(prices);
          } catch {
            /* transient; keep the last values */
          }
        };
        await pull();
        liveRef.current = setInterval(pull, LIVE_POLL_INTERVAL_MS);
      } catch (e) {
        if (!cancelled) setError(e.message);
      }
    })();
    return () => {
      cancelled = true;
      stopLivePolling();
    };
  }, [liveOn, tickerKey, scan.status]);

  // Turning live off: drop the backend stream and clear the displayed prices.
  useEffect(() => {
    if (!liveOn) {
      stopLive().catch(() => {});
      setLivePrices({});
    }
  }, [liveOn]);

  const elapsed = busy && scan.started_at ? now - scan.started_at : 0;
  const analyzedCount = results.filter((r) => r.ai).length;
  // Only the top-N setups are auto-analyzed; the rest are on-demand. Cap the
  // denominator to that count so it matches the backend's "analyzed X/N" message.
  const autoAnalyzeCount = Math.min(scan.ai_top_n ?? results.length, results.length);
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
          <span> · <span className="spinner tiny" /> AI analyzing {analyzedCount}/{autoAnalyzeCount}…</span>
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

      {scan.status === "done" && results.length > 0 && (
        <div className="export-bar">
          <button className="btn export" onClick={copyForToS} title="Copy all tickers for ThinkorSwim's 'Paste symbols from clipboard' import">
            Copy tickers for ThinkorSwim
          </button>
          <button className="btn export ghost" onClick={downloadWatchlist} title="Download a .csv for ThinkorSwim's Watchlist → Import">
            Export .csv
          </button>
          {exportNote && <span className="export-note muted small">{exportNote} ✓</span>}
          <button
            className={`btn export live-toggle ${liveOn ? "on" : ""}`}
            onClick={() => setLiveOn((on) => !on)}
            title="Stream live prices for these cards from Yahoo (free, no key). Updates every few seconds."
          >
            <span className={`live-dot ${liveOn ? "on" : ""}`} />
            {liveOn ? "Live prices on" : "Live prices off"}
          </button>
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
            <StockCard
              key={stock.ticker}
              stock={stock}
              onAnalyze={onAnalyze}
              live={liveOn ? livePrices[stock.ticker] : null}
            />
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
