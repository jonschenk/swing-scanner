# Swing Scanner

A native macOS app for finding 2–5 day swing trade setups. Scans the S&P 500 for **uptrend pullbacks** using free yFinance data, then runs each match through Claude for an AI read on news sentiment, risks, and confidence. Built for scanning + research only — charting and execution stay in ThinkorSwim (use each card's Copy button to grab the ticker).

**Stack:** Python/FastAPI backend · React frontend · Electron wrapper. Everything runs locally.

## Scan criteria — the "leader pullback"

This isn't a generic moving-average screen. It implements the setup that elite swing
traders actually use — **buy a short-term pullback in a market-leading uptrend** —
drawn from Mark Minervini's Trend Template and Kristjan "Qullamaggie" Kullamägi's
momentum method. The core idea both share: *only trade the strongest stocks, and only
when they take a healthy breather.*

A stock passes when **all** of these are true (daily bars, 2 years of history):

| Group | Criterion | Default | Source / rationale |
|---|---|---|---|
| **Leadership** | Relative-strength rank (vs scanned universe) above | 70 / 100 | Minervini RS ≥ 70; Qullamaggie trades top-RS names |
| | Within X% of the 52-week high | 30% | Minervini "within 25% of high"; leaders stay near highs |
| | At least X% above the 52-week low | 25% | Minervini "≥ 25–30% off the lows" |
| **Trend** | Price above 50 **and** 200 SMA | — | confirmed long-term uptrend |
| | 20 SMA > 50 SMA > 200 SMA (full stack) | — | Minervini MA alignment |
| | 200 SMA rising (vs ~1 month ago) | — | Minervini criterion #6 |
| | ADX(14) above | 25 | 25–30 = a genuinely trending stock, not chop |
| **Pullback** | RSI(14) between | 40–60 | documented "healthy pullback" band |
| **Volatility** | ATR% above | 2.0% | enough daily range to profit in 2–5 days (Qullamaggie ADR) |
| **Liquidity** | 21-day avg volume above | 500,000 | tradeable |
| | Price between $15 and capital × max-position-% | $15–$500 | no penny stocks; affordable for the account |

**Relative-strength rank** is computed IBD-style: each stock's blended 1/3/6-month
return is ranked across the entire scanned universe into a 0–100 percentile, so a rank
of 90 means the stock out-momentumed 90% of the ~680 names. Results are ordered by a
**setup score** that weights relative strength most heavily, then trend strength,
pullback depth, and volatility — matching the research consensus that *leadership
matters most*.

Every threshold is adjustable in **Settings** if you want a stricter or looser net.

## Risk management & position sizing

Every match comes pre-sized to your account using the **1–2% rule with an ATR-based stop** — the standard professional approach:

- **Capital** drives everything. Set it in the header (defaults to $1,000); the price ceiling becomes capital × max-position-% so you're never shown a stock you can't sensibly buy.
- **Stop** = entry − (1.5 × ATR) — placed beyond normal daily noise.
- **Shares** = the most you can buy while risking ≤ your risk-% of capital (default 2%) if stopped out, capped by what you can afford.
- **Target** = a 3:1 reward-to-risk level (configurable).
- Each card shows exact shares, position cost, entry/stop/target, and dollars at risk. Setups too volatile to size safely for your account get a **⚠ high risk** flag instead of being hidden.

All of these (risk %, stop multiple, R:R, ADX/ATR/RSI thresholds) are adjustable in **Settings**.

## AI analysis (free, runs locally)

Each passing stock gets recent Yahoo Finance headlines fed to an AI model, which returns:

- A 2–3 sentence plain-English summary of what's going on
- Sentiment: **Bullish / Neutral / Bearish** (for a 2–5 day hold)
- Notable risks and catalysts (earnings, lawsuits, launches, macro)
- Confidence: **High / Medium / Low** based on technical + sentiment alignment

By default this uses a **free local model via [Ollama](https://ollama.com)** — nothing leaves your Mac and it costs $0. `start.sh` handles setup automatically; manual install is just:

```bash
brew install ollama
ollama pull llama3.2:3b
```

Set `AI_PROVIDER` in `.env` to switch:

| Provider | Cost | Notes |
|---|---|---|
| `ollama` (default) | Free | Local model. Default `llama3.2:3b` runs comfortably on 8 GB Macs; set `OLLAMA_MODEL=llama3.1:8b` if you have 16+ GB for better analysis. |
| `anthropic` | ~1–2¢/stock | Claude (claude-sonnet-4-6) — sharper analysis. Needs `ANTHROPIC_API_KEY`. |
| `none` | Free | Skip AI entirely, technicals only. |

If Ollama isn't installed or running, the scan still works — cards just show a hint instead of AI analysis.

**Performance:** news for all setups is prefetched concurrently (it used to be the hidden bottleneck), analyses run in parallel (`OLLAMA_CONCURRENCY`, default 2 — safe on 8 GB), and only the top **N** setups (Settings → *Auto-analyze top N*, default 10) are analyzed automatically; the rest are one click away. To go faster still, lower N, raise concurrency on a bigger machine, or switch to a hosted provider.

## Setup

Prereqs: macOS, Python 3.10+, Node 18+.

```bash
./start.sh
```

That's it — no API keys, no accounts. `start.sh` creates the Python venv, installs dependencies, sets up the free local AI (installs nothing without telling you; the model download is ~5 GB one-time), builds the frontend, and launches the Electron app — which spawns the backend automatically and connects to it.

> First run needs `chmod +x start.sh` if the execute bit didn't survive.

## Export as a Dock app

To turn this into a real `Swing Scanner.app` (custom icon, double-click to launch, lives in your Dock):

```bash
./build-app.sh
```

This builds the app into `dist-app/` and opens it in Finder. Drag **Swing Scanner.app** into your Applications folder, then drag it from there to your Dock. Double-clicking it starts the backend and opens the window automatically — no Terminal needed.

Notes:
- The `.app` is a lightweight launcher around the backend/frontend **in this folder**, so keep this project where it is after building. (If you move it, just rerun `./build-app.sh`.)
- On launch it starts the Python backend **and** ensures Ollama is running (only if `AI_PROVIDER=ollama` and it isn't already up). If you have the Ollama menubar app installed, that keeps Ollama running anyway.
- It's signed ad-hoc (no paid Apple Developer account needed). The first launch may take a few seconds while the backend boots.
- To change the icon, edit `electron/build/make_icon.py`, then rebuild.

### Running pieces manually

**Backend only** (single command):

```bash
cd backend && .venv/bin/uvicorn app.main:app --port 8765
```

API: `GET /api/health` · `GET|PUT /api/settings` · `POST /api/scan` · `GET /api/scan/status`

**Frontend dev mode** (hot reload):

```bash
cd frontend && npm run dev          # terminal 1 — Vite on :5173
cd electron && npm run dev          # terminal 2 — Electron pointed at Vite
```

The Electron app reuses an already-running backend if one is listening on 8765, so you can run uvicorn with `--reload` in a third terminal while developing.

## Usage

1. Hit **Run Scan**. The technical results appear as soon as the scan finishes (~20s curated, ~3min full market) — you don't wait for the AI. A live **elapsed timer** runs in the progress bar.
2. Cards show sorted by **setup score**: price, RS rank, distance from 52w high, RSI, ADX, ATR% (all color-coded), the sized trade plan (shares/stop/target/risk). **Click anywhere on a card to copy its ticker** for ThinkorSwim.
3. **AI streams in afterward.** Only the top **N** setups (default 10) are analyzed automatically — they fill in per-card with sentiment, summary, risks, and confidence. Lower-ranked cards show an **"⚡ Analyze with AI"** button so you only spend AI on what interests you. The AI runs in parallel with news prefetched concurrently, so it's far faster than one-at-a-time.
4. Loaded results **auto-refresh every 3 minutes** — a lightweight update that re-pulls live prices and recomputes indicators + position sizing for just the displayed tickers (no re-scan, no new AI calls, ~1s). The header shows the last-updated time.
5. Adjust the universe, **how many to auto-analyze**, risk rules, and all filter thresholds in **Settings** — saved to `backend/settings.json`.

## Scan universe

Two modes, switchable in **Settings → Scan Scope**:

- **Full US market (default)** — dynamically pulls *every* US common stock (~5,900 names) from the [NASDAQ Trader symbol directory](https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs), filtering out ETFs, warrants, rights, units, preferreds, and test issues. This is what finds the small/mid-cap movers that curated lists miss. The relative-strength rank is computed across this whole universe, so it's a *true* market-wide RS rating. The symbol list is cached for 7 days; a full scan takes ~3 minutes (downloading price history for thousands of names is the bottleneck).
- **Curated (~675 names)** — the built-in S&P 500 + hand-picked movers in [backend/app/tickers.txt](backend/app/tickers.txt). Fast (~20s), works fully offline, and the automatic fallback if the live symbol fetch ever fails.

Either way, the **Max setups** setting caps how many top-ranked results are kept and sent to the AI (default 30), so a full-market scan stays responsive. To customize the curated list, edit `tickers.txt` — one symbol per line, Yahoo format (`BRK-B` not `BRK.B`), `#` for comments; invalid symbols are skipped silently.

### Price cache (fast rescans)

A full-market download is the slow part (~3 min). To avoid repeating it, the scanner caches the **raw price bars for the whole universe** on disk and reuses them for a configurable window (Settings → *Reuse cached prices*, default 30 min). When the cache is warm, a rescan finishes in **~1 second** instead of 3 minutes.

The cache is designed so it **can never cause a missed stock**: it stores only the downloaded bars, never which stocks passed. *Every* rescan re-evaluates all ~5,900 tickers from scratch — so changing a filter (RSI, ADX, RS, etc.) instantly re-screens the entire market on cached data and surfaces any newly-qualifying names. The 30-minute window also sidesteps stock-split adjustment drift (splits take effect at the open, so any cache old enough to straddle one is already expired). Hit **↻ Fresh** anytime to force a full re-download, or set the window to 0 to disable caching.

## Project structure

```
├── start.sh                  # one-command setup + launch
├── .env                      # ANTHROPIC_API_KEY (gitignored)
├── backend/
│   ├── requirements.txt
│   └── app/
│       ├── main.py           # FastAPI app + scan orchestration
│       ├── scanner.py        # yfinance download + filter criteria
│       ├── indicators.py     # SMA, Wilder RSI
│       ├── ai.py             # AI news analysis (Ollama local / Claude optional)
│       ├── config.py         # settings persistence
│       └── tickers.txt       # scan universe (editable)
├── frontend/                 # React + Vite, dark theme
│   └── src/
│       ├── App.jsx
│       └── components/       # StockCard, SettingsPanel
└── electron/
    └── main.js               # spawns backend, waits for health, opens window
```

## Notes & disclaimers

- yFinance is unofficial and rate-limited; if a scan errors mid-download, just rerun it.
- Local AI analysis takes roughly 5–15 s per stock on Apple Silicon; a typical scan (10–20 matches) adds a couple of minutes after the data download.
- This is a research tool, not financial advice. You're trading a $1k paper account — keep it that way until the edge is proven. 📈

## Methodology sources

The scan criteria are based on established swing-trading research, not invented:

- [Mark Minervini's Trend Template (8 criteria)](https://deepvue.com/screener/minervini-trend-template/) — MA stack, 52-week-high proximity, relative strength
- [Qullamaggie's momentum method](https://qullamaggie.net/) — trading top relative-strength leaders, high-ADR movers, surfing the 10/20 MA
- [Scanz — Swing trading scans](https://scanz.com/swing-trading-scans/) — ADX ≥ 30 for trend strength
- [ChartMill — technical swing screener](https://www.chartmill.com/getting-started/technical-stock-screener) — RSI 40–60 healthy-pullback band, volume/ATR filters
- [VectorVest — how to scan for swing trading](https://www.vectorvest.com/blog/swing-trading/how-to-scan-stocks-for-swing-trading/) — MA alignment and momentum filtering
