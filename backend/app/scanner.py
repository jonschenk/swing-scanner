"""Market scanner for swing trades (2-5 day holds).

Strategy: buy a short-term pullback inside a confirmed, leading uptrend — the
setup elite swing traders (Minervini's Trend Template, Qullamaggie's momentum
method) actually use. A stock qualifies only if it is:

  * structurally trending up   (price > 50/200 SMA, 20 > 50 > 200, 200 SMA rising)
  * a market leader            (high relative-strength rank, near its 52-week high)
  * a real mover               (ADX trend strength + ATR volatility)
  * currently pulled back       (RSI in a healthy band, not extended, not broken)

Each match is then sized to the trader's account with an ATR stop + the risk rule.
"""

import logging
import math
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import yfinance as yf

from . import price_cache
from .config import ScanSettings
from .indicators import adx, atr, rsi, sma
from .risk import position_plan
from .universe import load_universe

log = logging.getLogger(__name__)

BATCH_SIZE = 100
HISTORY_PERIOD = "1y"  # ~252 bars: enough for 200-SMA, its slope, and 6-month momentum
MIN_BARS = 220  # enough for 200-SMA + a month of slope


def _extract(data: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
    if isinstance(data.columns, pd.MultiIndex):
        if ticker not in data.columns.get_level_values(0):
            return None
        return data[ticker]
    return data  # single-ticker batch returns flat columns


def _blended_momentum(close: pd.Series) -> Optional[float]:
    """IBD-style relative-strength input: weighted multi-timeframe return
    (heaviest on the most recent quarter). Returns a raw number; it's the
    cross-sectional RANK of this that becomes the 0-100 RS rating."""
    def ret(n: int) -> Optional[float]:
        if len(close) <= n:
            return None
        prior = close.iloc[-1 - n]
        return close.iloc[-1] / prior - 1 if prior > 0 else None

    r1, r3, r6 = ret(21), ret(63), ret(126)  # 1, 3, 6 months
    if r1 is None or r3 is None or r6 is None:
        return None
    return 0.5 * r3 + 0.3 * r6 + 0.2 * r1


def _core_metrics(df: pd.DataFrame) -> Optional[dict]:
    """Raw indicator values shared by the scan filter and the live refresh."""
    if df is None or df.empty:
        return None
    df = df.dropna(subset=["High", "Low", "Close", "Volume"])
    if len(df) < MIN_BARS:
        return None

    close, high, low, volume = df["Close"], df["High"], df["Low"], df["Volume"]
    sma200_series = sma(close, 200)
    m = {
        "price": float(close.iloc[-1]),
        "sma20": float(sma(close, 20).iloc[-1]),
        "sma50": float(sma(close, 50).iloc[-1]),
        "sma200": float(sma200_series.iloc[-1]),
        "sma200_prior": float(sma200_series.iloc[-22]),  # ~1 month ago
        "avg_volume": float(volume.rolling(21).mean().iloc[-1]),
        "last_volume": float(volume.iloc[-1]),
        "rsi": float(rsi(close, 14).iloc[-1]),
        "atr": float(atr(high, low, close, 14).iloc[-1]),
        "adx": float(adx(high, low, close, 14).iloc[-1]),
        "high_52w": float(high.tail(252).max()),
        "low_52w": float(low.tail(252).min()),
    }
    if any(math.isnan(v) for v in m.values()) or m["high_52w"] <= 0 or m["low_52w"] <= 0:
        return None
    return m


def _build_row(ticker: str, m: dict, settings: ScanSettings, rs_rating: float) -> Optional[dict]:
    """Assemble a sized result row from metrics. No pass/fail — used by both the
    scan (after filtering) and the live refresh (without filtering)."""
    price, atr14 = m["price"], m["atr"]
    plan = position_plan(price, atr14, settings)
    if plan is None:  # can't afford even one share within the rules
        return None

    atr_pct = atr14 / price * 100
    rel_volume = m["last_volume"] / m["avg_volume"] if m["avg_volume"] else 0.0
    pct_from_high = (m["high_52w"] - price) / m["high_52w"] * 100

    # Composite ranking. Relative strength leads (that's what the research says
    # matters most), then trend strength, pullback depth, and volatility.
    setup_score = round(
        rs_rating * 0.6 + min(m["adx"], 40) + (settings.rsi_threshold - m["rsi"]) + min(atr_pct, 8) * 2,
        1,
    )

    return {
        "ticker": ticker,
        "price": round(price, 2),
        "rsi": round(m["rsi"], 1),
        "avg_volume": int(m["avg_volume"]),
        "rel_volume": round(rel_volume, 2),
        "adx": round(m["adx"], 1),
        "atr": round(atr14, 2),
        "atr_pct": round(atr_pct, 1),
        "rs_rating": round(rs_rating, 0),
        "pct_from_high": round(pct_from_high, 1),
        "sma20": round(m["sma20"], 2),
        "sma50": round(m["sma50"], 2),
        "sma200": round(m["sma200"], 2),
        "pct_above_sma50": round((price / m["sma50"] - 1) * 100, 1),
        "setup_score": setup_score,
        "plan": plan,
    }


def evaluate_ticker(
    ticker: str,
    df: pd.DataFrame,
    settings: ScanSettings,
    rs_rating: float,
) -> Optional[dict]:
    """Return a sized scan-result dict if the ticker passes every criterion, else None."""
    m = _core_metrics(df)
    if m is None:
        return None

    price = m["price"]
    atr_pct = m["atr"] / price * 100
    pct_from_high = (m["high_52w"] - price) / m["high_52w"] * 100
    pct_above_low = (price / m["low_52w"] - 1) * 100

    passes = (
        # liquidity / affordability
        price > settings.min_price
        and price <= settings.max_price  # capital-aware ceiling
        and m["avg_volume"] > settings.min_avg_volume
        # confirmed long-term uptrend (Minervini MA stack)
        and price > m["sma50"]
        and price > m["sma200"]
        and m["sma20"] > m["sma50"] > m["sma200"]
        and m["sma200"] > m["sma200_prior"]  # 200-SMA rising
        # market leadership
        and rs_rating >= settings.min_rs_rating
        and pct_from_high <= settings.near_high_pct  # near the 52-week high
        and pct_above_low >= settings.min_above_low_pct  # well off the lows
        # strong, tradeable trend
        and m["adx"] >= settings.adx_min
        and atr_pct >= settings.atr_pct_min
        # currently pulled back, but healthy (not extended, not broken)
        and settings.rsi_floor <= m["rsi"] < settings.rsi_threshold
    )
    if not passes:
        return None
    return _build_row(ticker, m, settings, rs_rating)


def recompute_row(
    ticker: str,
    df: pd.DataFrame,
    settings: ScanSettings,
    rs_rating: float,
) -> Optional[dict]:
    """Refresh a displayed setup's live numbers (price, indicators, position
    plan) WITHOUT re-applying the scan filters or recomputing universe-wide RS.
    Used by the lightweight auto-refresh."""
    m = _core_metrics(df)
    if m is None:
        return None
    return _build_row(ticker, m, settings, rs_rating)


def scan_market(
    settings: ScanSettings,
    progress: Callable[[str], None] = lambda msg: None,
    force_fresh: bool = False,
) -> list[dict]:
    """Blocking scan over the whole universe. Run in a worker thread.

    Bars come from the on-disk cache when it's fresh enough (instant), else from
    a full download. Either way EVERY cached ticker is re-evaluated from scratch,
    so the cache only saves the download — it never decides what passes."""
    universe = settings.universe
    if not force_fresh and price_cache.is_fresh(universe, settings.cache_minutes):
        age = price_cache.age_minutes() or 0
        progress(f"Using cached prices ({age:.0f} min old) — re-evaluating every stock…")
        frames_all = price_cache.load()
    else:
        frames_all = _download_universe(universe, progress)
        price_cache.save(frames_all, universe)

    if not frames_all:
        return []
    return _evaluate(frames_all, settings, progress)


def _download_universe(universe: str, progress) -> dict[str, pd.DataFrame]:
    """Download bars for the whole universe, returning every VALID ticker's frame
    (not pre-filtered — the cache must hold all of them so changing a filter can
    never miss a stock)."""
    tickers = load_universe(universe)
    total = len(tickers)
    scope = "full US market" if universe == "full" else "curated list"
    frames: dict[str, pd.DataFrame] = {}

    for start in range(0, total, BATCH_SIZE):
        batch = tickers[start : start + BATCH_SIZE]
        progress(f"Scanning {scope}: downloaded {min(start + BATCH_SIZE, total)}/{total} tickers…")
        try:
            data = yf.download(
                batch,
                period=HISTORY_PERIOD,
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False,
            )
        except Exception:
            log.exception("Batch download failed for %s..%s", batch[0], batch[-1])
            continue
        if data is None or data.empty:
            continue

        for ticker in batch:
            try:
                df = _extract(data, ticker)
                if df is None:
                    continue
                df = df.dropna(subset=["High", "Low", "Close", "Volume"])
                if len(df) >= MIN_BARS:
                    frames[ticker] = df
            except Exception:
                log.exception("Failed preparing %s", ticker)
    return frames


def _evaluate(frames_all: dict[str, pd.DataFrame], settings: ScanSettings, progress) -> list[dict]:
    """Rank relative strength across the whole universe and apply the full filter
    set. Runs on every cached/downloaded ticker — nothing is skipped."""
    momentum: dict[str, float] = {}
    liquid: dict[str, pd.DataFrame] = {}  # passes the cheap price/volume pre-gate

    for ticker, df in frames_all.items():
        try:
            mom = _blended_momentum(df["Close"])
            if mom is None:
                continue
            momentum[ticker] = mom  # ranked across the whole universe
            price = float(df["Close"].iloc[-1])
            avg_vol = float(df["Volume"].tail(21).mean())
            if price > settings.min_price and price <= settings.max_price and avg_vol > settings.min_avg_volume:
                liquid[ticker] = df
        except Exception:
            log.exception("Failed preparing %s", ticker)

    if not momentum:
        return []

    rs_ratings = (pd.Series(momentum).rank(pct=True) * 100).round(1).to_dict()
    progress(f"Ranking {len(momentum)} stocks, evaluating {len(liquid)} liquid candidates…")

    results: list[dict] = []
    for ticker, df in liquid.items():
        try:
            hit = evaluate_ticker(ticker, df, settings, rs_ratings[ticker])
            if hit:
                results.append(hit)
        except Exception:
            log.exception("Failed evaluating %s", ticker)

    results.sort(key=lambda r: r["setup_score"], reverse=True)
    if len(results) > settings.max_results:
        progress(f"{len(results)} setups found — keeping the top {settings.max_results}")
        results = results[: settings.max_results]
    return results


def refresh_results(settings: ScanSettings, prior_results: list[dict]) -> list[dict]:
    """Cheaply re-pull live data for the currently-displayed setups only and
    recompute their numbers + position plans. Keeps the existing AI analysis and
    relative-strength rating (a few minutes stale is fine); no universe scan, no
    AI calls — so it's a fast (~seconds) update, not a re-scan."""
    if not prior_results:
        return prior_results

    prior_by_ticker = {r["ticker"]: r for r in prior_results}
    tickers = list(prior_by_ticker.keys())
    updated: list[dict] = []

    for start in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[start : start + BATCH_SIZE]
        try:
            data = yf.download(
                batch,
                period=HISTORY_PERIOD,
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False,
            )
        except Exception:
            log.exception("Refresh download failed")
            data = None

        for ticker in batch:
            prior = prior_by_ticker[ticker]
            row = None
            if data is not None and not data.empty:
                try:
                    df = _extract(data, ticker)
                    if df is not None:
                        row = recompute_row(ticker, df, settings, prior.get("rs_rating", 0))
                except Exception:
                    log.exception("Refresh recompute failed for %s", ticker)
            if row is None:
                updated.append(prior)  # keep the prior row if the refresh failed
            else:
                row["ai"] = prior.get("ai")  # preserve the AI analysis from the scan
                if prior.get("ai_status"):  # preserve on-demand "idle" flag
                    row["ai_status"] = prior["ai_status"]
                updated.append(row)

    updated.sort(key=lambda r: r["setup_score"], reverse=True)
    return updated
