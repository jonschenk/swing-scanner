import { useEffect, useState } from "react";
import { getSettings, saveSettings } from "../api.js";

const FIELDS = {
  scan: [{ key: "max_results", label: "Max setups to show", min: 1, step: 5, max: 200 }],
  account: [
    { key: "capital", label: "Capital ($)", min: 1, step: 100 },
    { key: "risk_pct", label: "Risk per trade (%)", min: 0.1, step: 0.5, max: 100 },
    { key: "max_position_pct", label: "Max position (% of capital)", min: 1, step: 5, max: 100 },
    { key: "atr_stop_mult", label: "Stop distance (× ATR)", min: 0.5, step: 0.25 },
    { key: "reward_mult", label: "Target (× risk, R:R)", min: 0.5, step: 0.5 },
  ],
  leadership: [
    { key: "min_rs_rating", label: "Relative-strength rank above (0–100)", min: 0, step: 5, max: 100 },
    { key: "near_high_pct", label: "Within % of 52-week high", min: 0, step: 5, max: 100 },
    { key: "min_above_low_pct", label: "At least % above 52-week low", min: 0, step: 5 },
  ],
  technical: [
    { key: "adx_min", label: "ADX(14) above (trend strength)", min: 0, step: 1 },
    { key: "rsi_floor", label: "RSI(14) above (not broken)", min: 0, step: 1, max: 100 },
    { key: "rsi_threshold", label: "RSI(14) below (pulled back)", min: 1, step: 1, max: 100 },
    { key: "atr_pct_min", label: "Min ATR % (volatility)", min: 0, step: 0.5 },
    { key: "min_price", label: "Min price ($)", min: 0, step: 1 },
    { key: "min_avg_volume", label: "Min avg volume (21d)", min: 0, step: 50000 },
  ],
};

export default function SettingsPanel({ onClose }) {
  const [form, setForm] = useState(null);
  const [status, setStatus] = useState("");

  useEffect(() => {
    getSettings()
      .then(setForm)
      .catch((e) => setStatus(`Failed to load settings: ${e.message}`));
  }, []);

  const update = (key) => (e) => setForm({ ...form, [key]: e.target.value });

  const onSave = async () => {
    setStatus("");
    const payload = { universe: form.universe };
    for (const group of Object.values(FIELDS)) {
      for (const f of group) payload[f.key] = Number(form[f.key]);
    }
    try {
      const saved = await saveSettings(payload);
      setForm({ ...form, ...saved });
      setStatus("Saved ✓");
      setTimeout(onClose, 600);
    } catch (e) {
      setStatus(`Save failed: ${e.message}`);
    }
  };

  const maxPrice =
    form && form.capital && form.max_position_pct
      ? (Number(form.capital) * Number(form.max_position_pct)) / 100
      : null;

  const renderField = (f) => (
    <label className="field" key={f.key}>
      <span>{f.label}</span>
      <input
        type="number"
        min={f.min}
        max={f.max}
        step={f.step}
        value={form[f.key]}
        onChange={update(f.key)}
      />
    </label>
  );

  return (
    <div className="overlay" onClick={onClose}>
      <div className="panel" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2>Scan Settings</h2>
          <button className="btn ghost" onClick={onClose}>✕</button>
        </div>

        {!form ? (
          <p className="muted">{status || "Loading…"}</p>
        ) : (
          <>
            <h3 className="group-title">Scan Scope</h3>
            <div className="field">
              <span>Universe</span>
              <div className="seg">
                <button
                  type="button"
                  className={`seg-btn ${form.universe === "full" ? "active" : ""}`}
                  onClick={() => setForm({ ...form, universe: "full" })}
                >
                  Full US market
                </button>
                <button
                  type="button"
                  className={`seg-btn ${form.universe === "curated" ? "active" : ""}`}
                  onClick={() => setForm({ ...form, universe: "curated" })}
                >
                  Curated list
                </button>
              </div>
            </div>
            <p className="hint">
              {form.universe === "full"
                ? "Scans every US common stock (~5,900 names). Finds the most setups but takes ~3 minutes."
                : "Scans the built-in S&P 500 + hand-picked movers (~675 names). Fast (~20s), works offline."}
            </p>
            {FIELDS.scan.map(renderField)}

            <h3 className="group-title">Account &amp; Risk</h3>
            {FIELDS.account.map(renderField)}

            {maxPrice != null && (
              <p className="hint">
                Only scanning stocks priced ≤ <strong>${maxPrice.toLocaleString()}</strong>{" "}
                (capital × max position %). Risking ${((Number(form.capital) * Number(form.risk_pct)) / 100).toFixed(0)} max per trade.
              </p>
            )}

            <h3 className="group-title">Leadership (relative strength)</h3>
            {FIELDS.leadership.map(renderField)}

            <h3 className="group-title">Trend &amp; Pullback</h3>
            {FIELDS.technical.map(renderField)}

            <div className="panel-foot">
              <span className="muted small">{status}</span>
              <button className="btn primary" onClick={onSave}>Save</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
