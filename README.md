# Swing Scanner

A desktop and self-hosted app that scans the whole US stock market for short-term swing trade setups, the kind you'd hold for two to five days. It reads the overall market regime, runs the strategy that fits that regime, uses AI to judge the best candidates, sizes each trade to your account, and can run as a hands-light advisor that proposes trades for you to approve. Everything trades on paper through a built-in simulator. It places no real orders, and a human approves every trade. The scan and its local AI run free on your own machine, with no API keys or accounts required for the core features.

![Scanner dashboard](docs/screenshots/dashboard.png)

## What it does

Hit Run Scan and it pulls live data for thousands of US stocks, narrows them to the setups that fit the current market, and shows each one as a card with the price, the key indicators, and a full trade plan: how many shares, where the stop goes, and the profit objective. From there you can research each name, get an AI read on it, paper-trade it, or let the app watch the market for you and propose trades on a schedule.

It started as a pure scanner and grew into a small, regime-aware paper-trading advisor. The scanner is still the core, and you can use it on its own. The rest is optional and layers on top.

## The two strategies and the regime router

Markets behave differently in different conditions, so the app carries two strategies and picks between them based on the regime it reads from a broad market index:

- Uptrend: a leader pullback. A stock in a confirmed uptrend that ranks high on relative strength and has dipped just enough to offer an entry.
- Choppy: a mean reversion. A quality name that has stretched well below its short moving average on an oversold reading, played for the bounce back.
- Downtrend: cash. The app sits out, since both strategies tend to bleed when the broad market is falling.

This router was developed and validated on years of historical data with an out-of-sample split, so the periods used to judge it were never used to build it. The honest caveats apply: the data favors stocks that still exist today, fills in a backtest are idealized, and a backtest is a hypothesis, not a promise. The forward paper-trading run is the real test. Strategy parameters are kept light here on purpose.

## AI analysis

AI is layered, with the cheap and free work done locally and the expensive judgment reserved for the moments that matter:

- Per card: a free local model (through [Ollama](https://ollama.com)) reads recent headlines for a name and gives a sentence or two plus a sentiment and confidence rating. Nothing leaves your computer.
- Deep analysis: on demand, a hosted model (Claude by default) writes a fuller, account-aware case for one setup. The thesis, the bull case, the main risks, and how it fits your other holdings, ending in a Take, Wait, or Pass call. This is the one part that uses a paid model and runs only when you ask.
- Pick triage: when the app proposes trades, a hosted model ranks the candidates against each other and your account, and it has to argue both the bull case and the bear case for each name before it is allowed to recommend it. A name is only taken when the bull case survives the bear case.
- Daily notes: after the close, a hosted model writes a short, observational note on the day. It summarizes what happened and flags things to watch, and it is deliberately barred from proposing strategy changes, since the sample is far too small early on.

The local layer is free. The hosted layers need an Anthropic API key in your `.env` and cost a few cents per call, shown on each result.

## The nightly advisor

The app can watch the market for you instead of waiting for you to run a scan. In its nightly mode, which matches how the strategy was actually backtested:

1. After the close, it scans on the day's finished bars, picks the regime's strategy, has the AI judge the candidates, and proposes a short list of fully specified trade tickets for the next day.
2. You review them on your phone or desktop and approve or skip each one. Anything you do not review simply expires, so nothing trades without you.
3. The next morning, just after the open, it re-checks each approved name against the actual opening price. If a name gapped down through its stop, blew its reward-to-risk, or otherwise broke, it is skipped. The ones that still look good are placed.

You get a push notification when there are setups to review and again when the morning run is done. Every step is written to an event log you can read back later.

There is a hard boundary here that does not move: the app prepares trades and makes them easy to act on, but it trades on paper only, and a human approves every one. There is no real-money autopilot.

## Paper trading and the journal

Trades fill through a built-in paper broker modeled on a real broker's order shape, so the plumbing is faithful to how a live account would work without touching real money. Fills include a small slippage haircut so the record is not rosier than reality. Open positions are managed with an ATR trailing stop that ratchets up as the trade works, plus a time stop, the same exit logic that won on the backtester. Every trade is logged to a journal with its full entry snapshot and outcome, scored by win rate and expectancy per strategy variation, so there is real evidence behind any decision to trust the system with more.

## Position sizing

Every result comes pre-sized to your account, the way a careful trader would do it by hand:

- You set your capital, and the app will not show you stocks priced too high to buy a sensible position.
- The stop sits below the recent noise, based on the stock's average true range.
- Share count is whatever keeps your loss within a set percentage of capital (two percent by default) if the stop gets hit.
- Exits are managed by a trailing stop and a time stop rather than a fixed target, so winners are given room to run.

If a stock is too volatile to size safely for your account, it gets flagged instead of quietly dropped.

![A single setup card](docs/screenshots/stock-card.png)

## Backtesting

A separate command-line backtester is where strategies earn their place before they ever go live. It replays the scan over years of history with strict no-lookahead rules, models slippage, supports out-of-sample train and test splits, and compares strategy variations head to head. This is the research bench. The running app never rewrites its own strategy. Changes are made deliberately, reviewed, and committed to version control, with the backtester and the live journal as the evidence.

## Monitor app

A lightweight, mobile-friendly Monitor page gives you a read-only view of the account, open positions, the regime, the journal, the daily note, and the event log, plus the nightly review where you approve or skip proposed trades. It is built to run against a self-hosted backend so you can check in from your phone.

## Setup

You'll need Python 3.10 or newer and Node 18 or newer.

macOS:

```bash
./start.sh
```

Windows (PowerShell):

```powershell
.\start.ps1
```

The script creates a Python environment, installs the dependencies, sets up the local AI, builds the frontend, and launches the app. The first run takes a few minutes, mostly the one-time AI model download.

### Building a standalone app

To package it into a real app you can pin to your Dock or Start menu:

```bash
./build-app.sh      # macOS, produces Swing Scanner.app in /Applications
.\build-app.ps1     # Windows, produces Swing Scanner.exe in dist-app\win-unpacked
```

One caveat: the Windows build has to run on a Windows machine. You cannot cross-build it from a Mac.

### Running headless on a small server

The backend is a plain web server, so it can run headless on something like a Raspberry Pi on your own network, with the Monitor page reached from your phone or laptop. A full market scan is light enough to run comfortably on modest hardware. Keep it on a private network behind a secure tunnel, not exposed to the open internet.

## How it's built

A FastAPI backend does the scanning, the indicator math, the regime read, the paper broker, and the scheduling. A React frontend renders the dashboard, with a separate static page for the Monitor. Electron wraps the desktop version into a native window. Market data comes from yfinance (free and unofficial), the local AI runs through Ollama, and the hosted analysis uses the Anthropic API. Everything talks over localhost or your private network.

```
electron (native window, desktop)
   spawns the backend and local AI on launch, loads the React UI

FastAPI backend
   scanner    download, indicators, filter, ranking
   regime     reads the market regime and routes the strategy
   risk       ATR-stop position sizing
   paper      paper broker, trailing-stop exits, bracket monitor
   journal    trade log and per-variation scoreboard
   ai         local and hosted analysis
   alerts     nightly schedule, review queue, push notifications
   backtest   research tool, validated offline
```

## What this is not

This is a research and decision-support tool. It finds, analyzes, and paper-trades setups so you can study them and build a track record. It does not place real-money trades, it does not connect to a live brokerage to move money, and a human approves every proposed trade. It is not financial advice. Market data is from a free, unofficial source and will occasionally rate-limit or error, in which case you just run the scan again. Do your own homework before putting real money at risk.
