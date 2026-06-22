import { useCallback, useEffect, useRef, useState } from "react";
import {
  getScanStatus,
  getHealth,
  getSettings,
  saveSettings,
  startScan,
  refreshScan,
  analyzeTicker,
  deepAnalyze,
  getRecommendations,
  startLive,
  stopLive,
  getLivePrices,
  getPaperAccount,
  paperBuy,
  paperClose,
  paperCancel,
  paperReset,
  getJournal,
  getRegime,
  getStrategies,
  setActiveStrategy,
  getQueue,
  buildQueue,
  approveProposal,
  denyProposal,
  getAlertEngine,
  setAlertEngine,
} from "./api.js";
import StockCard from "./components/StockCard.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";

const POLL_INTERVAL_MS = 1500;
const REFRESH_INTERVAL_MS = 180_000; // auto-refresh loaded setups every 3 min
const LIVE_POLL_INTERVAL_MS = 4000; // pull the latest streamed prices from our backend
const PAPER_POLL_INTERVAL_MS = 5000; // refresh the paper account (live P&L + bracket fills)

function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.round(totalSeconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${String(s % 60).padStart(2, "0")}s`;
}

function formatClock(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleTimeString();
}

// Client-side CSV export of the journal — portable, hand back to Claude for analysis.
function exportJournalCsv(trades) {
  if (!trades?.length) return;
  const cols = [
    "ticker", "variation_id", "decision", "status", "opened_at", "entry", "stop", "target",
    "shares", "closed_at", "exit", "exit_reason", "hold_days", "pnl", "r_multiple", "outcome",
    "market_regime", "notes",
  ];
  const esc = (v) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const rows = [cols.join(","), ...trades.map((t) => cols.map((c) => esc(t[c])).join(","))];
  const blob = new Blob([rows.join("\n") + "\n"], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "journal.csv";
  a.click();
  URL.revokeObjectURL(url);
}

// Dollar formatter for the paper book (always 2 decimals, thousands separators).
function usd(n) {
  if (typeof n !== "number") return "—";
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Plain-English status line for the alert engine.
function engineStatusText(e) {
  if (!e?.enabled) return "Off — turn on to auto-scan during market hours.";
  const auto = e.mode === "auto";
  const strat = { leader_pullback: "leader-pullback", mean_reversion: "mean-reversion", cash: "cash" }[e.last_strategy] || e.last_strategy;
  const ran = e.last_run ? ` · last ran ${new Date(e.last_run).toLocaleTimeString()}` : "";
  switch (e.last_status) {
    case "watching":
      return `Watching · ${e.market_open ? "market open" : "market closed"}${e.last_regime ? ` · ${e.last_regime} → ${strat}` : ""}${ran}`;
    case "auto-traded":
      return `Auto-trading (paper) · ${e.last_regime} → ${strat} · opened ${e.last_new_count} last run${ran}`;
    case "warming-up":
      return "On · letting the open settle before acting (open-buffer window)";
    case "market-closed":
      return `On · waiting for the market to open (9:30–16:00 ET, Mon–Fri)${auto ? " · will auto-trade (paper)" : ""}`;
    case "bear-cash":
      return `On · Bear regime — holding cash, doing nothing${ran}`;
    case "error":
      return "On · couldn’t read the market just now — retrying";
    default:
      return "On · first scan pending";
  }
}

// Parse the holdings textarea: one position per line, "TICKER SHARES [SECTOR]".
// Feeds the deep-analysis portfolio-fit reasoning. Replaced by Schwab later.
function parseHoldings(text) {
  const out = [];
  for (const line of text.split("\n")) {
    const parts = line.trim().split(/\s+/);
    if (!parts[0]) continue;
    const pos = { ticker: parts[0].toUpperCase() };
    const shares = Number(parts[1]);
    if (Number.isFinite(shares)) pos.shares = shares;
    if (parts.length > 2) pos.sector = parts.slice(2).join(" ");
    out.push(pos);
  }
  return out;
}

export default function App() {
  const [backendUp, setBackendUp] = useState(null);
  const [scan, setScan] = useState({ status: "idle", progress: "", results: [] });
  const [settings, setSettings] = useState(null);
  const [capital, setCapital] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const [error, setError] = useState(null);
  const [now, setNow] = useState(Date.now() / 1000); // ticks each second while running
  const [holdings, setHoldings] = useState(() => localStorage.getItem("holdings") || "");
  const [showHoldings, setShowHoldings] = useState(false);
  const [paper, setPaper] = useState(null); // paper account snapshot (cash/equity/positions)
  const [showPaper, setShowPaper] = useState(true); // paper book open by default
  const [recommending, setRecommending] = useState(false); // batch-triage in flight
  const [journal, setJournal] = useState(null); // {trades, summary} for the journal view
  const [showJournal, setShowJournal] = useState(false);
  const [liveOn, setLiveOn] = useState(true); // streaming live prices for displayed cards (on by default)
  const [livePrices, setLivePrices] = useState({}); // ticker -> {price, change_percent}
  const [regime, setRegime] = useState(null); // {regime, label, strategy, ...} the router's current call
  const [strategies, setStrategies] = useState(null); // {active, variations} for the picker
  const [showStrategy, setShowStrategy] = useState(false);
  const [scanStrategy, setScanStrategy] = useState("leader_pullback"); // which signal family the scan runs
  const userPickedStrategy = useRef(false); // true once the user manually toggles (stops regime auto-default)
  const [queue, setQueue] = useState(null); // {pending, decided} approve/deny review queue
  const [showQueue, setShowQueue] = useState(false);
  const [queueBusy, setQueueBusy] = useState(false);
  const [alertEngine, setAlertEngineState] = useState(null); // auto-scan scheduler state
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

  // Account-aware deep analysis (Claude) for one card, with your holdings as context.
  const onDeepAnalysis = useCallback(
    async (ticker) => {
      setScan((s) => ({
        ...s,
        results: s.results.map((r) => (r.ticker === ticker ? { ...r, tc_status: "pending" } : r)),
      }));
      try {
        const state = await deepAnalyze(ticker, parseHoldings(holdings));
        setScan(state);
      } catch (e) {
        setError(e.message);
        setScan((s) => ({
          ...s,
          results: s.results.map((r) =>
            r.ticker === ticker ? { ...r, tc_status: undefined } : r,
          ),
        }));
      }
    },
    [holdings],
  );

  // Batch triage: one Claude pass that ranks the top setups vs. your account/holdings.
  const onRecommend = async () => {
    setRecommending(true);
    setError(null);
    try {
      const state = await getRecommendations(parseHoldings(holdings), 12);
      setScan(state);
    } catch (e) {
      setError(e.message);
    } finally {
      setRecommending(false);
    }
  };

  // Poll the paper account so positions mark to market and bracket fills show up.
  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const a = await getPaperAccount();
        if (alive) setPaper(a);
      } catch {
        /* backend not up yet */
      }
    };
    pull();
    const id = setInterval(pull, PAPER_POLL_INTERVAL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const refreshPaper = async () => {
    try {
      setPaper(await getPaperAccount());
    } catch {
      /* ignore */
    }
  };
  const refreshJournal = async () => {
    try {
      setJournal(await getJournal());
    } catch {
      /* ignore */
    }
  };
  // Load the journal when its panel opens, and refresh it periodically while open
  // (trades land in it as brackets/closes fire).
  useEffect(() => {
    if (!showJournal) return;
    refreshJournal();
    const id = setInterval(refreshJournal, 10000);
    return () => clearInterval(id);
  }, [showJournal]);

  // Market-regime badge: fetch once on load, then refresh every 30 min. The 200-SMA regime
  // barely moves intraday (the backend caches it ~1h), so this is deliberately infrequent.
  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const r = await getRegime();
        if (!alive) return;
        setRegime(r);
        // Auto-align the scan to the regime's strategy until the user picks manually:
        // chop -> mean-reversion (buy dips), bull/bear -> leader-pullback.
        if (r?.available && !userPickedStrategy.current) {
          setScanStrategy(r.regime === "chop" ? "mean_reversion" : "leader_pullback");
        }
      } catch {
        /* leave the last value; the badge just won't update */
      }
    };
    pull();
    const id = setInterval(pull, 1_800_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  // Load the strategy variations once on mount (the picker's data).
  useEffect(() => {
    getStrategies()
      .then(setStrategies)
      .catch(() => {});
  }, []);

  const onActivateStrategy = async (id) => {
    try {
      const next = await setActiveStrategy(id);
      setStrategies(next);
    } catch (e) {
      setError(e.message);
    }
  };
  const activeVariation = strategies?.variations?.[strategies.active] || null;

  // Review queue + alert-engine state: load once on mount.
  useEffect(() => {
    getQueue().then(setQueue).catch(() => {});
    getAlertEngine().then(setAlertEngineState).catch(() => {});
  }, []);

  // Keep the queue + engine fresh: poll fast while the panel is open, slowly while the engine
  // is enabled (so the badge picks up auto-queued setups even with the panel closed).
  const engineOn = alertEngine?.enabled;
  useEffect(() => {
    if (!showQueue && !engineOn) return;
    const pull = () => {
      getQueue().then(setQueue).catch(() => {});
      getAlertEngine().then(setAlertEngineState).catch(() => {});
    };
    const id = setInterval(pull, showQueue ? 5000 : 30000);
    return () => clearInterval(id);
  }, [showQueue, engineOn]);

  const onToggleEngine = async (enabled) => {
    try {
      setAlertEngineState(await setAlertEngine({ enabled }));
      if (enabled) setShowQueue(true);
    } catch (e) {
      setError(e.message);
    }
  };
  const onSetInterval = async (interval_minutes) => {
    try {
      setAlertEngineState(await setAlertEngine({ interval_minutes }));
    } catch (e) {
      setError(e.message);
    }
  };
  const onSetEngineMode = async (mode) => {
    try {
      setAlertEngineState(await setAlertEngine({ mode }));
    } catch (e) {
      setError(e.message);
    }
  };
  const onSetMaxPositions = async (max_positions) => {
    try {
      setAlertEngineState(await setAlertEngine({ max_positions }));
    } catch (e) {
      setError(e.message);
    }
  };

  const onBuildQueue = async () => {
    setQueueBusy(true);
    setError(null);
    try {
      setQueue(await buildQueue(8));
      setShowQueue(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setQueueBusy(false);
    }
  };
  const onApprove = async (id) => {
    setQueueBusy(true);
    try {
      const res = await approveProposal(id);
      if (res.error) setError(res.error);
      else {
        setQueue({ pending: res.pending, decided: res.decided });
        if (res.account) setPaper(res.account); // a position just opened
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setQueueBusy(false);
    }
  };
  const onDeny = async (id) => {
    setQueueBusy(true);
    try {
      setQueue(await denyProposal(id));
    } catch (e) {
      setError(e.message);
    } finally {
      setQueueBusy(false);
    }
  };
  const pendingCount = queue?.pending?.length || 0;
  const onPaperBuy = async (ticker) => {
    try {
      const a = await paperBuy(ticker);
      if (a.error) setError(a.error);
      else setPaper(a);
    } catch (e) {
      setError(e.message);
    }
  };
  const onPaperClose = async (tradeId) => {
    try {
      setPaper(await paperClose(tradeId));
      if (showJournal) refreshJournal();
    } catch (e) {
      setError(e.message);
    }
  };
  const onPaperReset = async () => {
    if (!window.confirm("Reset the paper account to your capital and clear open positions?")) return;
    try {
      setPaper(await paperReset());
    } catch (e) {
      setError(e.message);
    }
  };
  const onCancelOrder = async (orderId) => {
    try {
      setPaper(await paperCancel(orderId));
    } catch (e) {
      setError(e.message);
    }
  };
  const onSetOrderType = async (paper_order_type) => {
    if (!settings) return;
    try {
      setSettings(await saveSettings({ ...settingsPayload(settings), paper_order_type }));
    } catch (e) {
      setError(e.message);
    }
  };

  const pickStrategy = (s) => {
    userPickedStrategy.current = true; // stop the regime from auto-switching it back
    setScanStrategy(s);
  };
  // What the validated router would run in today's regime (for the advisory copy).
  const routerPick =
    regime?.regime === "chop" ? "mean_reversion" : regime?.regime === "bear" ? "cash" : "leader_pullback";

  const onRunScan = async (fresh = false) => {
    setError(null);
    try {
      await startScan(fresh, scanStrategy);
      setScan((s) => ({ ...s, status: "running", progress: "Starting scan…", started_at: Date.now() / 1000 }));
      beginPolling();
    } catch (e) {
      setError(e.message);
    }
  };

  const results = scan.results ?? [];
  const heldTickers = new Set((paper?.positions || []).map((p) => p.ticker));
  // Float recommended picks to the top (by rank); everything else keeps setup-score order.
  const displayResults = [...results].sort(
    (a, b) => (a.recommendation?.rank ?? Infinity) - (b.recommendation?.rank ?? Infinity),
  );

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
          <h1>Bellwether</h1>
          {regime?.available && (
            <span
              className={`regime-badge regime-${regime.regime}`}
              title={
                `${regime.description}\n\n` +
                `SPY ${regime.spy_price} · ${regime.spy_pct_vs_sma200 >= 0 ? "+" : ""}` +
                `${regime.spy_pct_vs_sma200}% vs its 200-SMA (${regime.spy_sma200}), ` +
                `${regime.sma200_rising ? "rising" : "falling"}.\n` +
                `The validated router would run: ${regime.strategy}.`
              }
            >
              <span className="regime-key">{regime.label}</span>
              <span className="regime-strategy">→ {regime.strategy}</span>
            </span>
          )}
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
          {paper && (
            <button
              className={`btn ghost ${showPaper ? "active" : ""}`}
              onClick={() => setShowPaper((v) => !v)}
              title="Your paper account: open positions, live P&L, auto-close at stop/target"
            >
              📈 Paper book{paper.positions?.length ? ` (${paper.positions.length})` : ""}
            </button>
          )}
          <button
            className={`btn ghost ${showQueue ? "active" : ""}`}
            onClick={() => setShowQueue((v) => !v)}
            title="Review queue — proposed trade tickets you approve (paper buy) or deny"
          >
            ✅ Review{pendingCount ? ` (${pendingCount})` : ""}
          </button>
          <button
            className={`btn ghost ${showStrategy ? "active" : ""}`}
            onClick={() => setShowStrategy((v) => !v)}
            title={`Strategy variations — pick which tuned parameter set the scan runs under${activeVariation ? ` (active: ${activeVariation.name})` : ""}`}
          >
            🎛 Strategy
          </button>
          <button
            className={`btn ghost ${showJournal ? "active" : ""}`}
            onClick={() => setShowJournal((v) => !v)}
            title="Trade journal: closed trades + the per-variation scoreboard (winrate/expectancy)"
          >
            📓 Journal
          </button>
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

      <div className="strategy-bar">
        <span className="muted small">Scan for</span>
        <div className="seg strategy-seg">
          <button
            className={`seg-btn ${scanStrategy === "leader_pullback" ? "active" : ""}`}
            onClick={() => pickStrategy("leader_pullback")}
            title="Buy pullbacks in trending market leaders (the momentum strategy)"
          >
            📈 Leader pullback
          </button>
          <button
            className={`seg-btn ${scanStrategy === "mean_reversion" ? "active" : ""}`}
            onClick={() => pickStrategy("mean_reversion")}
            title="Buy quality names on a deep oversold dip and ride the snap-back (the chop strategy)"
          >
            🔄 Mean reversion
          </button>
        </div>
        {regime?.available && (
          <span className="muted small strategy-bar-note">
            {routerPick === "cash"
              ? `· the router would hold cash in today's ${regime.label} market`
              : routerPick === scanStrategy
              ? `· matches today's ${regime.label} regime`
              : `· the router would scan ${routerPick === "mean_reversion" ? "mean-reversion" : "leader-pullback"} in today's ${regime.label} market`}
          </span>
        )}
      </div>

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
          <button
            className="btn export recommend-btn"
            disabled={recommending}
            onClick={onRecommend}
            title="One Claude pass that ranks the top setups against your account and holdings"
          >
            {recommending ? (
              <><span className="spinner tiny" /> Picking…</>
            ) : (
              <>✨ Recommend top picks</>
            )}
          </button>
          <button
            className={`btn export ${showHoldings ? "on" : ""}`}
            onClick={() => setShowHoldings((v) => !v)}
            title="Your open positions — fed to Deep analysis so it can weigh portfolio fit"
          >
            Holdings{holdings.trim() ? ` (${parseHoldings(holdings).length})` : ""}
          </button>
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

      {scan.status === "done" && scan.recommendation && (
        <div className={`rec-banner ${scan.recommendation.error ? "rec-error" : ""}`}>
          <p className="rec-summary">{scan.recommendation.summary}</p>
          {scan.recommendation.skip_note && (
            <p className="rec-skip muted small">Skip: {scan.recommendation.skip_note}</p>
          )}
          {scan.recommendation._meta && (
            <p className="rec-meta muted small">
              {scan.recommendation._meta.model} · considered top {scan.recommendation._meta.considered} · $
              {scan.recommendation._meta.cost_usd}
            </p>
          )}
        </div>
      )}

      {scan.status === "done" && results.length > 0 && showHoldings && (
        <div className="holdings-panel">
          <label className="holdings-label muted small">
            Your open positions — one per line as <code>TICKER SHARES [SECTOR]</code>. Deep
            analysis uses these to judge sector concentration and overlap. (Schwab will fill
            this automatically later.)
          </label>
          <textarea
            className="holdings-input"
            value={holdings}
            placeholder={"AAPL 25 Technology\nAMD 40 Technology\nXOM 30 Energy"}
            spellCheck={false}
            onChange={(e) => {
              setHoldings(e.target.value);
              localStorage.setItem("holdings", e.target.value);
            }}
          />
        </div>
      )}

      {showPaper && paper && (
        <div className="paper-panel">
          <div className="paper-summary">
            <span>Equity <strong>${usd(paper.equity)}</strong></span>
            <span className="muted">Cash ${usd(paper.cash)}</span>
            <span className={paper.open_pnl >= 0 ? "pos" : "neg"}>
              Open {paper.open_pnl >= 0 ? "+" : "−"}${usd(Math.abs(paper.open_pnl))}
            </span>
            <span className={paper.realized_pnl >= 0 ? "pos" : "neg"}>
              Realized {paper.realized_pnl >= 0 ? "+" : "−"}${usd(Math.abs(paper.realized_pnl))}
            </span>
            {settings && (
              <label className="paper-ordertype muted small" title="How paper orders fill. Market = now (+slippage); At next open = rest until the open (matches the backtest); Limit = rest and fill only at your entry price (no chasing the open).">
                Fills
                <select value={settings.paper_order_type || "market"} onChange={(e) => onSetOrderType(e.target.value)}>
                  <option value="market">Market (now)</option>
                  <option value="moo">At next open</option>
                  <option value="limit">Limit @ entry</option>
                </select>
              </label>
            )}
            <button className="btn export ghost paper-reset" onClick={onPaperReset} title="Start the paper account fresh from your capital">
              Reset
            </button>
          </div>
          {paper.positions.length === 0 ? (
            <p className="muted small">
              No open paper positions. Hit "Paper buy" on a card to open one — it fills at the
              live price and auto-closes when it hits your stop or target.
            </p>
          ) : (
            <table className="paper-table">
              <thead>
                <tr>
                  <th>Ticker</th><th>Sh</th><th>Entry</th><th>Now</th><th>P&amp;L</th>
                  <th>R</th><th>Stop</th><th>Target</th><th></th>
                </tr>
              </thead>
              <tbody>
                {paper.positions.map((p) => (
                  <tr key={p.id}>
                    <td className="pt-ticker">{p.ticker}</td>
                    <td>{p.shares}</td>
                    <td>${usd(p.entry)}</td>
                    <td>${usd(p.current)}</td>
                    <td className={p.unrealized >= 0 ? "pos" : "neg"}>
                      {p.unrealized >= 0 ? "+" : "−"}${usd(Math.abs(p.unrealized))} ({p.unrealized_pct >= 0 ? "+" : ""}{p.unrealized_pct}%)
                    </td>
                    <td className={(p.r ?? 0) >= 0 ? "pos" : "neg"}>
                      {p.r == null ? "—" : `${p.r >= 0 ? "+" : ""}${p.r}R`}
                    </td>
                    <td>${usd(p.stop)}</td>
                    <td>${usd(p.target)}</td>
                    <td>
                      <button className="paper-close" onClick={() => onPaperClose(p.id)}>Close</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {paper.orders?.length > 0 && (
            <>
              <div className="muted small" style={{ margin: "12px 0 4px" }}>
                Resting orders ({paper.orders.length}) — fill when their condition is met
              </div>
              <table className="paper-table">
                <thead>
                  <tr><th>Ticker</th><th>Type</th><th>Sh</th><th>Limit / fill</th><th>Stop</th><th>Target</th><th></th></tr>
                </thead>
                <tbody>
                  {paper.orders.map((o) => (
                    <tr key={o.id}>
                      <td className="pt-ticker">{o.ticker}</td>
                      <td>{o.type === "moo" ? "At next open" : "Limit"}</td>
                      <td>{o.shares}</td>
                      <td>{o.type === "limit" ? `$${usd(o.limit_price)}` : "next open"}</td>
                      <td>${usd(o.stop)}</td>
                      <td>${usd(o.target)}</td>
                      <td><button className="paper-close" onClick={() => onCancelOrder(o.id)}>Cancel</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}

      {showJournal && journal && (
        <div className="paper-panel">
          <div className="paper-summary">
            <strong>Trade journal</strong>
            <span className="muted">{journal.trades.length} logged</span>
            <button
              className="btn export ghost paper-reset"
              onClick={() => exportJournalCsv(journal.trades)}
              title="Download the full journal as CSV"
            >
              Export CSV
            </button>
          </div>

          {Object.keys(journal.summary).length > 0 && (
            <table className="paper-table">
              <thead>
                <tr><th>Variation</th><th>Trades</th><th>Win%</th><th>Expectancy</th><th>Net P&amp;L</th></tr>
              </thead>
              <tbody>
                {Object.entries(journal.summary).map(([vid, s]) => (
                  <tr key={vid}>
                    <td className="pt-ticker">{vid}{s.low_sample ? " ⚠" : ""}</td>
                    <td>{s.trades}</td>
                    <td>{s.winrate}%</td>
                    <td className={s.expectancy_r >= 0 ? "pos" : "neg"}>{s.expectancy_r >= 0 ? "+" : ""}{s.expectancy_r}R</td>
                    <td className={s.total_pnl >= 0 ? "pos" : "neg"}>{s.total_pnl >= 0 ? "+" : "−"}${usd(Math.abs(s.total_pnl))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {(() => {
            const closed = journal.trades.filter((t) => t.status === "closed");
            return closed.length === 0 ? (
              <p className="muted small" style={{ marginTop: "10px" }}>
                No closed trades yet. They appear here when a paper position closes (bracket or manual),
                with the per-variation scoreboard above.
              </p>
            ) : (
              <table className="paper-table" style={{ marginTop: "10px" }}>
                <thead>
                  <tr>
                    <th>Ticker</th><th>Var</th><th>Entry→Exit</th><th>R</th>
                    <th>Outcome</th><th>Why</th><th>Days</th><th>Regime</th>
                  </tr>
                </thead>
                <tbody>
                  {closed.slice().reverse().map((t) => (
                    <tr key={t.id}>
                      <td className="pt-ticker">{t.ticker}</td>
                      <td>{t.variation_id}</td>
                      <td>${usd(t.entry)} → ${usd(t.exit)}</td>
                      <td className={(t.r_multiple ?? 0) >= 0 ? "pos" : "neg"}>
                        {t.r_multiple == null ? "—" : `${t.r_multiple >= 0 ? "+" : ""}${t.r_multiple}R`}
                      </td>
                      <td className={t.outcome === "win" ? "pos" : t.outcome === "loss" ? "neg" : ""}>{t.outcome}</td>
                      <td>{t.exit_reason}</td>
                      <td>{t.hold_days}</td>
                      <td className="muted">{t.market_regime || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            );
          })()}
        </div>
      )}

      {showQueue && (
        <div className="paper-panel">
          <div className="paper-summary">
            <strong>Review queue</strong>
            <span className="muted">{pendingCount} pending · you approve (paper buy) or deny each</span>
            <button
              className="btn export ghost paper-reset"
              disabled={queueBusy || results.length === 0}
              onClick={onBuildQueue}
              title="Fill the queue with the current scan's best setups (recommended picks first)"
            >
              {queueBusy ? "Working…" : "Build from current scan"}
            </button>
          </div>

          {alertEngine && (
            <div className="alert-engine-row">
              <label className="engine-toggle" title="Auto-scan during market hours, honouring the regime (bull→leader, chop→mean-reversion, bear→cash). Review mode queues setups for your approval; Auto-trade mode auto-opens PAPER positions — paper only, never a real order.">
                <input
                  type="checkbox"
                  checked={!!alertEngine.enabled}
                  onChange={(e) => onToggleEngine(e.target.checked)}
                />
                🔔 Auto-scan &amp; alert
              </label>
              <label className="engine-interval muted small">
                every
                <select
                  value={alertEngine.interval_minutes}
                  onChange={(e) => onSetInterval(Number(e.target.value))}
                  disabled={!alertEngine.enabled}
                >
                  {[15, 30, 60, 120].map((m) => (
                    <option key={m} value={m}>{m} min</option>
                  ))}
                </select>
              </label>
              <div className="seg engine-mode">
                <button
                  className={`seg-btn ${alertEngine.mode !== "auto" ? "active" : ""}`}
                  disabled={!alertEngine.enabled}
                  onClick={() => onSetEngineMode("review")}
                  title="Queue setups for you to Approve / Deny"
                >
                  Review
                </button>
                <button
                  className={`seg-btn ${alertEngine.mode === "auto" ? "active" : ""}`}
                  disabled={!alertEngine.enabled}
                  onClick={() => onSetEngineMode("auto")}
                  title="Auto-open PAPER positions for qualifying setups (paper only — never a real order)"
                >
                  Auto-trade (paper)
                </button>
              </div>
              {alertEngine.mode === "auto" && (
                <label className="engine-maxpos muted small">
                  max
                  <select
                    value={alertEngine.max_positions}
                    onChange={(e) => onSetMaxPositions(Number(e.target.value))}
                    disabled={!alertEngine.enabled}
                  >
                    {[3, 5, 8, 10].map((n) => (
                      <option key={n} value={n}>{n}</option>
                    ))}
                  </select>
                  positions
                </label>
              )}
              <span className="muted small engine-status">{engineStatusText(alertEngine)}</span>
            </div>
          )}

          {pendingCount === 0 ? (
            <p className="muted small" style={{ marginTop: "10px" }}>
              No pending tickets. Run a scan (optionally hit “Recommend top picks” first), then
              “Build from current scan” to queue the best setups for review. Approving opens a paper
              position; denying logs the pass so the advisor’s calls can be graded later.
            </p>
          ) : (
            <table className="paper-table" style={{ marginTop: "4px" }}>
              <thead>
                <tr>
                  <th>Ticker</th><th>Strategy</th><th>Call</th>
                  <th>Entry→Stop→Target</th><th>Size</th><th>Why</th><th></th>
                </tr>
              </thead>
              <tbody>
                {queue.pending.map((p) => (
                  <tr key={p.id}>
                    <td className="pt-ticker">
                      {p.ticker}
                      {p.regime && <span className="muted small"> · {p.regime}</span>}
                      {p.earnings_soon && (
                        <span className="queue-earnings" title="Earnings inside the hold window — gap risk">
                          {" "}⚠ ER {p.days_to_earnings}d
                        </span>
                      )}
                    </td>
                    <td>
                      <span className={`strat-chip ${p.strategy === "mean_reversion" ? "strat-meanrev" : "strat-leader"}`}>
                        {p.strategy === "mean_reversion" ? "🔄 Mean-rev" : "📈 Leader"}
                      </span>
                    </td>
                    <td>{p.call ? <span className={`badge rec-${p.call.toLowerCase()}`}>{p.call}</span> : <span className="muted">—</span>}</td>
                    <td>
                      ${usd(p.plan?.entry)} → <span className="neg">${usd(p.plan?.stop)}</span> →{" "}
                      <span className="pos">${usd(p.plan?.target)}</span>
                    </td>
                    <td>{p.plan?.shares} sh · {p.plan?.position_pct}%</td>
                    <td className="queue-why">{p.reason || <span className="muted">score {p.score}</span>}</td>
                    <td className="queue-actions">
                      <button className="paper-buy-btn queue-approve" disabled={queueBusy} onClick={() => onApprove(p.id)}>
                        ✓ Approve
                      </button>
                      <button className="paper-close" disabled={queueBusy} onClick={() => onDeny(p.id)}>
                        Deny
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {queue?.decided?.length > 0 && (
            <p className="muted small" style={{ marginTop: "10px" }}>
              Recently decided:{" "}
              {queue.decided.slice(0, 8).map((p) => (
                <span key={p.id} className={p.status === "approved" ? "pos" : "neg"}>
                  {p.ticker} {p.status === "approved" ? "✓" : "✕"}{"  "}
                </span>
              ))}
            </p>
          )}
        </div>
      )}

      {showStrategy && strategies && (
        <div className="paper-panel">
          <div className="paper-summary">
            <strong>Strategy variations</strong>
            <span className="muted">the active one drives every scan</span>
          </div>
          <table className="paper-table">
            <thead>
              <tr><th></th><th>Variation</th><th>Target</th><th>ADX</th><th>RS</th><th>RSI band</th><th></th></tr>
            </thead>
            <tbody>
              {Object.values(strategies.variations).map((v) => {
                const p = v.params;
                const isActive = v.id === strategies.active;
                const target =
                  p.reward_mult != null
                    ? `${p.reward_mult}R${p.cap_target_at_high === false ? " uncapped" : ""}`
                    : "—";
                return (
                  <tr key={v.id} title={v.notes}>
                    <td>{isActive ? <span className="strat-active-dot" /> : null}</td>
                    <td className="pt-ticker">
                      {v.name}
                      {v.id === activeVariation?.id && <span className="muted small"> · {v.id}</span>}
                    </td>
                    <td>{target}</td>
                    <td>{p.adx_min ?? "—"}</td>
                    <td>{p.min_rs_rating ?? "—"}</td>
                    <td>{p.rsi_floor != null ? `${p.rsi_floor}–${p.rsi_threshold}` : "—"}</td>
                    <td>
                      {isActive ? (
                        <span className="strat-active-tag">Active</span>
                      ) : (
                        <button className="paper-close" onClick={() => onActivateStrategy(v.id)}>
                          Activate
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {activeVariation?.notes && (
            <p className="muted small" style={{ marginTop: "10px" }}>{activeVariation.notes}</p>
          )}
          <p className="muted small" style={{ marginTop: "6px" }}>
            New variations are added deliberately in a dev session (the app never rewrites its own
            strategy). Switching here takes effect on your next scan.
          </p>
        </div>
      )}

      <main>
        {regime?.available && (scan.status === "done" || scan.status === "analyzing") && results.length > 0 && (() => {
          const scanned = scan.strategy || "leader_pullback";
          const onRegime = routerPick === scanned;
          const noteClass = regime.regime === "bear" ? "bear" : onRegime ? regime.regime : "chop";
          const scannedLabel = scanned === "mean_reversion" ? "mean-reversion dips" : "leader-pullback momentum setups";
          return (
            <div className={`regime-note regime-note-${noteClass}`}>
              {regime.regime === "bear" ? (
                <>
                  <strong>Downtrend.</strong> The validated router would be in <strong>cash</strong> today —
                  both strategies bleed in bear markets. These {scannedLabel} are shown for awareness,
                  not as a call to act.
                </>
              ) : onRegime ? (
                <>
                  <strong>{regime.label} market.</strong> You're scanning {scannedLabel} — the router's
                  active strategy for this regime. On-regime.
                </>
              ) : (
                <>
                  <strong>{regime.label} market.</strong> The router would scan{" "}
                  {routerPick === "mean_reversion" ? "mean-reversion dips" : "leader-pullback momentum"} here.
                  These {scannedLabel} are off-regime — switch above, or size down.
                </>
              )}
            </div>
          );
        })()}

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
          {displayResults.map((stock) => (
            <StockCard
              key={stock.ticker}
              stock={stock}
              onAnalyze={onAnalyze}
              onDeepAnalysis={onDeepAnalysis}
              onPaperBuy={onPaperBuy}
              held={heldTickers.has(stock.ticker)}
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
