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

export default function StockCard({ stock }) {
  const [copied, setCopied] = useState(false);
  const ai = stock.ai ?? {};
  const plan = stock.plan ?? {};
  const analyzing = !stock.ai; // technicals loaded, AI not back yet
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
    <article className="card">
      <div className="card-head">
        <div className="ticker-row">
          <h2 className="ticker">{stock.ticker}</h2>
          <button
            className={`btn copy ${copied ? "copied" : ""}`}
            onClick={copyTicker}
            title="Copy ticker for ThinkorSwim"
          >
            {copied ? "Copied ✓" : "Copy"}
          </button>
        </div>
        <div className="head-badges">
          <span className="score" title="Setup score: relative strength + trend + pullback + volatility">
            {stock.setup_score}
          </span>
          {analyzing ? (
            <span className="badge analyzing">
              <span className="spinner tiny" /> Analyzing
            </span>
          ) : (
            <span className={`badge sentiment-${sentiment.toLowerCase()}`}>{sentiment}</span>
          )}
        </div>
      </div>

      <div className="stats">
        <div className="stat">
          <span className="stat-label">Price</span>
          <span className="stat-value">${stock.price.toFixed(2)}</span>
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
          <strong>${plan.position_cost?.toLocaleString()}</strong>{" "}
          <span className="muted">({plan.position_pct}% of capital)</span>
        </div>
        <div className="plan-levels">
          <span className="lvl">
            <span className="lvl-label">Entry</span>${plan.entry}
          </span>
          <span className="lvl lvl-stop">
            <span className="lvl-label">Stop</span>${plan.stop}
          </span>
          <span className="lvl lvl-target">
            <span className="lvl-label">Target</span>${plan.target}
          </span>
        </div>
        <div className="plan-risk muted small">
          Risking ${plan.risk_dollars} ({plan.risk_pct}% of capital) if stopped out
        </div>
      </div>

      <p className={`summary ${analyzing ? "summary-pending" : ai.error ? "muted" : ""}`}>
        {analyzing ? "Awaiting AI analysis…" : ai.summary}
      </p>

      {!analyzing && ai.risks_catalysts && (
        <p className="risks">
          <span className="risks-label">Risks / Catalysts:</span> {ai.risks_catalysts}
        </p>
      )}

      <div className="card-foot">
        {analyzing ? (
          <span className="muted small">AI analysis pending…</span>
        ) : (
          <span className={`badge confidence-${(ai.confidence ?? "low").toLowerCase()}`}>
            {ai.confidence ?? "—"} confidence
          </span>
        )}
        {!analyzing && typeof ai.news_count === "number" && (
          <span className="muted small">{ai.news_count} news items</span>
        )}
      </div>
    </article>
  );
}
