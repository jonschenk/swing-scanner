import { useState } from "react";

function rsiClass(rsi) {
  // In a 40–60 pullback band, a lower RSI = a deeper, better-value entry.
  if (rsi < 45) return "rsi-deep";
  if (rsi < 52) return "rsi-low";
  return "rsi-mid";
}

function adxClass(adx) {
  if (adx >= 30) return "val-strong"; // strong trend
  if (adx >= 20) return "val-ok";
  return "val-weak";
}

function rsClass(rs) {
  if (rs >= 90) return "val-strong"; // elite relative strength
  if (rs >= 80) return "val-ok";
  return "val-weak";
}

function formatVolume(v) {
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(0)}K`;
  return String(v);
}

// Always show cents, including trailing zeros (63.3 -> 63.30), with separators.
function money(v) {
  if (typeof v !== "number") return v;
  return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function StockCard({ stock, onAnalyze, onDeepAnalysis, onPaperBuy, held, live }) {
  const [copied, setCopied] = useState(false);
  const [buying, setBuying] = useState(false);
  const [showCase, setShowCase] = useState(false); // expanded deep-analysis details
  const tc = stock.trade_case;
  const tcPending = stock.tc_status === "pending";
  const rec = stock.recommendation; // batch-triage pick: {rank, call, reason, conviction}
  const livePrice = live && typeof live.price === "number" ? live.price : null;
  const liveDir =
    live && typeof live.change_percent === "number"
      ? live.change_percent >= 0
        ? "up"
        : "down"
      : "";
  const ai = stock.ai ?? {};
  const plan = stock.plan ?? {};
  const hasAi = !!stock.ai;
  const idle = !hasAi && stock.ai_status === "idle"; // on-demand: not analyzed yet
  const analyzing = !hasAi && !idle; // queued/in-progress
  const sentiment = ai.sentiment ?? "Neutral";

  const copyTicker = async () => {
    try {
      await navigator.clipboard.writeText(stock.ticker);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard unavailable */
    }
  };

  return (
    <article
      className={`card ${copied ? "card-copied" : ""} ${rec ? "card-recommended" : ""}`}
      onClick={copyTicker}
      title="Click anywhere to copy the ticker"
    >
      {rec && (
        <div className="rec-strip">
          <span className="rec-badge">★ Pick #{rec.rank} · {rec.call}</span>
          <span className="rec-card-reason">{rec.reason}</span>
        </div>
      )}
      <div className="card-head">
        <div className="ticker-row">
          <div className="ticker-block">
            <h2 className="ticker">{stock.ticker}</h2>
            {stock.name && <span className="company-name" title={stock.name}>{stock.name}</span>}
          </div>
          <button
            className={`btn copy ${copied ? "copied" : ""}`}
            onClick={(e) => {
              e.stopPropagation();
              copyTicker();
            }}
            title="Copy ticker for ThinkorSwim"
          >
            {copied ? "Copied ✓" : "Copy"}
          </button>
        </div>
        <div className="head-badges">
          <span className="score" title="Setup score: relative strength + trend + pullback + volatility">
            {stock.setup_score}
          </span>
          {hasAi ? (
            <span className={`badge sentiment-${sentiment.toLowerCase()}`}>{sentiment}</span>
          ) : idle ? (
            <span className="badge idle-badge">AI on-demand</span>
          ) : (
            <span className="badge analyzing">
              <span className="spinner tiny" /> Analyzing
            </span>
          )}
        </div>
      </div>

      <div className="stats">
        <div className="stat">
          <span className="stat-label">
            Price
            {livePrice != null && <span className="live-dot on" title="Live price" />}
          </span>
          <span className={`stat-value ${livePrice != null ? `live-price ${liveDir}` : ""}`}>
            ${money(livePrice != null ? livePrice : stock.price)}
          </span>
        </div>
        <div className="stat">
          <span
            className="stat-label"
            title="Relative strength rank vs the scanned universe (0–100). Higher = stronger leader."
          >
            RS Rank
          </span>
          <span className={`stat-value ${rsClass(stock.rs_rating)}`}>{stock.rs_rating}</span>
        </div>
        <div className="stat">
          <span className="stat-label" title="How far below the 52-week high">52w High</span>
          <span className="stat-value">−{stock.pct_from_high}%</span>
        </div>
        <div className="stat">
          <span className="stat-label">RSI(14)</span>
          <span className={`stat-value ${rsiClass(stock.rsi)}`}>{stock.rsi}</span>
        </div>
        <div className="stat">
          <span className="stat-label" title="Trend strength (ADX)">ADX</span>
          <span className={`stat-value ${adxClass(stock.adx)}`}>{stock.adx}</span>
        </div>
        <div className="stat">
          <span className="stat-label" title="Daily volatility (Average True Range)">ATR%</span>
          <span className="stat-value">{stock.atr_pct}%</span>
        </div>
      </div>

      <div className="liquidity muted small">
        Avg vol {formatVolume(stock.avg_volume)} · {stock.rel_volume}× today
      </div>

      {/* ---- position plan ---- */}
      <div className={`plan ${plan.undersized ? "plan-warn" : ""}`}>
        <div className="plan-head">
          <span className="plan-title">Trade Plan</span>
          {plan.undersized ? (
            <span className="plan-flag">⚠ high risk for account</span>
          ) : (
            <span className="muted small">{plan.reward_risk}:1 reward / risk</span>
          )}
        </div>
        <div className="plan-row">
          <strong>{plan.shares}</strong> share{plan.shares === 1 ? "" : "s"} ≈{" "}
          <strong>${money(plan.position_cost)}</strong>{" "}
          <span className="muted">({plan.position_pct}% of capital)</span>
        </div>
        <div className="plan-levels">
          <span className="lvl">
            <span className="lvl-label">Entry</span>${money(plan.entry)}
          </span>
          <span className="lvl lvl-stop">
            <span className="lvl-label">Stop</span>${money(plan.stop)}
          </span>
          <span className="lvl lvl-target">
            <span className="lvl-label">Target</span>${money(plan.target)}
          </span>
        </div>
        <div className="plan-risk muted small">
          Risking ${money(plan.risk_dollars)} ({plan.risk_pct}% of capital) if stopped out
        </div>
      </div>

      {idle ? (
        <button
          className="analyze-btn"
          onClick={(e) => {
            e.stopPropagation();
            onAnalyze?.(stock.ticker);
          }}
        >
          ⚡ Analyze with AI
        </button>
      ) : (
        <>
          <p className={`summary ${analyzing ? "summary-pending" : ai.error ? "muted" : ""}`}>
            {analyzing ? "Awaiting AI analysis…" : ai.summary}
          </p>

          {hasAi && ai.risks_catalysts && (
            <p className="risks">
              <span className="risks-label">Risks / Catalysts:</span> {ai.risks_catalysts}
            </p>
          )}

          <div className="card-foot">
            {hasAi ? (
              <span className={`badge confidence-${(ai.confidence ?? "low").toLowerCase()}`}>
                {ai.confidence ?? "—"} confidence
              </span>
            ) : (
              <span className="badge confidence-pending">confidence pending</span>
            )}
            {hasAi && typeof ai.news_count === "number" && (
              <span className="muted small">{ai.news_count} news items</span>
            )}
          </div>
        </>
      )}

      {/* ---- paper trade ---- */}
      <div className="paper-buy-row" onClick={(e) => e.stopPropagation()}>
        {held ? (
          <span className="paper-held">✓ In paper book</span>
        ) : (
          <button
            className="paper-buy-btn"
            disabled={buying}
            title="Open a simulated position at the live price; auto-closes at the stop or target"
            onClick={async () => {
              setBuying(true);
              try {
                await onPaperBuy?.(stock.ticker);
              } finally {
                setBuying(false);
              }
            }}
          >
            {buying ? "Buying…" : `📈 Paper buy ${stock.plan?.shares ?? ""} sh`}
          </button>
        )}
      </div>

      {/* ---- account-aware deep analysis (Claude, on demand) ---- */}
      <div className="deep" onClick={(e) => e.stopPropagation()}>
        {!tc && (
          <button
            className="deep-btn"
            disabled={tcPending}
            onClick={() => onDeepAnalysis?.(stock.ticker)}
          >
            {tcPending ? (
              <>
                <span className="spinner tiny" /> Analyzing trade…
              </>
            ) : (
              <>🔍 Deep analysis</>
            )}
          </button>
        )}

        {tc && tc.error && <p className="deep-error muted small">{tc.bottom_line}</p>}

        {tc && !tc.error && (
          <div className="trade-case">
            <div className="tc-head">
              <span className={`badge rec-${(tc.recommendation || "").toLowerCase()}`}>
                {tc.recommendation}
              </span>
              <span className="muted small">{tc.conviction} conviction</span>
              <button className="tc-toggle muted small" onClick={() => setShowCase((v) => !v)}>
                {showCase ? "Hide details" : "Details"}
              </button>
            </div>
            <p className="tc-bottom">{tc.bottom_line}</p>

            {showCase && (
              <div className="tc-details">
                <p><span className="tc-label">Thesis</span> {tc.thesis}</p>
                <p><span className="tc-label">Bull case</span> {tc.bull_case}</p>
                <p><span className="tc-label">Risks</span> {tc.key_risks}</p>
                <p><span className="tc-label">Portfolio fit</span> {tc.portfolio_fit}</p>
                {tc._meta && (
                  <p className="muted small tc-meta">
                    {tc._meta.model} · {tc._meta.news_count} news · analyzed for $
                    {tc._meta.cost_usd}
                  </p>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </article>
  );
}
