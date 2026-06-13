# Swing Scanner

A desktop app that scans the whole US stock market for short-term swing trade setups, the kind you'd hold for 2 to 5 days. It sizes each one to your account and adds a quick AI read on the news. Everything runs locally and it's free to use. No API keys, no accounts, nothing leaves your machine.

![Scanner dashboard](docs/screenshots/dashboard.png)

## What it does

Hit Run Scan and it pulls live data for thousands of US stocks, narrows them down to the ones in a strong uptrend that have just pulled back a little (a common spot to enter a swing trade), and shows each match as a card. Every card has the price, the key indicators, and a full trade plan: how many shares to buy, where to put your stop, and a profit target. A local AI model reads the recent headlines for each name and gives you a sentence or two on what's happening, along with a sentiment and confidence rating.

It's made for finding and researching trades, not placing them. Each card has a copy button so you can drop the ticker straight into your broker or charting tool.

## Features

- Scans the full US market (around 5,900 stocks), or a faster curated list if you'd rather.
- Ranks stocks by relative strength across the whole universe, so the strongest names float to the top.
- Sizes every trade to your capital using a fixed-risk rule and an ATR-based stop.
- Free local AI analysis through [Ollama](https://ollama.com). You can swap in a hosted model like Claude if you want sharper writing.
- Smart caching, so re-running a scan or tweaking a filter takes about a second instead of three minutes.
- Adjustable filters for price, volume, RSI, trend strength, and more.
- Runs on macOS and Windows.

![A single setup card](docs/screenshots/stock-card.png)

## The scan

The default strategy looks for a "leader pullback": a stock in a confirmed uptrend (above its 50 and 200 day moving averages, with the averages stacked in order) that ranks high on relative strength, but has dipped enough on its RSI to offer a decent entry. It also checks for enough daily volatility and volume to be worth trading on a short timeframe.

Everything is adjustable in Settings, the RSI band, the trend-strength floor, the relative-strength cutoff, the volume minimum, and so on. Saving and switching between multiple strategy presets is on the roadmap.

Each match gets a setup score (weighted toward relative strength), and results are sorted by it.

## Position sizing

Every result comes pre-sized to your account, the way a careful trader would do it by hand:

- You set your capital, and the app won't show you stocks priced too high to buy a sensible position.
- The stop sits below the recent noise, based on the stock's average true range.
- Share count is whatever keeps your loss within a set percentage of capital (2% by default) if the stop gets hit.
- The target defaults to three times the risk.

If a stock is too volatile to size safely for your account, it gets flagged instead of quietly dropped.

![Settings](docs/screenshots/settings.png)

## AI analysis

Each setup's recent news gets boiled down by an AI model into a couple of sentences, a Bullish, Neutral, or Bearish call, the main risks or catalysts, and a confidence rating. By default this runs on a free local model through Ollama, so nothing leaves your computer. If you'd prefer a hosted model for better writing, set `AI_PROVIDER` in your `.env`.

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

One caveat: the Windows build has to run on a Windows machine. You can't cross-build it from a Mac.

## How it's built

A FastAPI backend does the scanning and indicator math, a React frontend renders the dashboard, and Electron wraps the two into a native window. Market data comes from yfinance (free and unofficial), and the AI runs locally through Ollama. Everything talks over localhost.

```
electron (native window)
   spawns the backend and local AI on launch, loads the React UI

FastAPI backend
   scanner   download + indicators + filter + ranking
   risk      ATR-stop position sizing
   ai        local LLM news analysis
   cache     on-disk price cache for fast rescans
```

## Notes

yfinance is unofficial and will rate-limit you now and then. If a scan errors partway through, just run it again (the cache makes the retry quick). Local AI runs about 5 to 15 seconds per stock on a recent machine, and only the top setups are analyzed automatically so the dashboard stays responsive.

This is a tool for research and learning, not financial advice. It finds and analyzes setups. It does not place trades, and you should do your own homework before putting real money at risk.
