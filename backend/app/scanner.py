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


def evaluate_ticker(
    ticker: str,
    df: pd.DataFrame,
    settings: ScanSettings,
    rs_rating: float,
) -> Optional[dict]:
    """Return a sized scan-result dict if the ticker passes every criterion, else None."""
    if df is None or df.empty:
        return None
    df = df.dropna(subset=["High", "Low", "Close", "Volume"])
    if len(df) < MIN_BARS:
        return None

    close, high, low, volume = df["Close"], df["High"], df["Low"], df["Volume"]

    price = float(close.iloc[-1])
    sma20 = float(sma(close, 20).iloc[-1])
    sma50 = float(sma(close, 50).iloc[-1])
    sma200_series = sma(close, 200)
    sma200 = float(sma200_series.iloc[-1])
    sma200_prior = float(sma200_series.iloc[-22])  # ~1 month ago
    avg_volume = float(volume.rolling(21).mean().iloc[-1])
    rsi14 = float(rsi(close, 14).iloc[-1])
    atr14 = float(atr(high, low, close, 14).iloc[-1])
    adx14 = float(adx(high, low, close, 14).iloc[-1])

    metrics = [price, sma20, sma50, sma200, sma200_prior, avg_volume, rsi14, atr14, adx14]
    if any(math.isnan(v) for v in metrics):
        return None

    high_52w = float(high.tail(252).max())
    low_52w = float(low.tail(252).min())
    if math.isnan(high_52w) or math.isnan(low_52w) or high_52w <= 0:
        return None

    atr_pct = atr14 / price * 100
    rel_volume = float(volume.iloc[-1]) / avg_volume if avg_volume else 0.0
    pct_from_high = (high_52w - price) / high_52w * 100  # how far below the 52w high
    pct_above_low = (price / low_52w - 1) * 100 if low_52w > 0 else 0.0

    passes = (
        # liquidity / affordability
        price > settings.min_price
        and price <= settings.max_price  # capital-aware ceiling
        and avg_volume > settings.min_avg_volume
        # confirmed long-term uptrend (Minervini MA stack)
        and price > sma50
        and price > sma200
        and sma20 > sma50 > sma200
        and sma200 > sma200_prior  # 200-SMA rising
        # market leadership
        and rs_rating >= settings.min_rs_rating
        and pct_from_high <= settings.near_high_pct  # near the 52-week high
        and pct_above_low >= settings.min_above_low_pct  # well off the lows
        # strong, tradeable trend
        and adx14 >= settings.adx_min
        and atr_pct >= settings.atr_pct_min
        # currently pulled back, but healthy (not extended, not broken)
        and settings.rsi_floor <= rsi14 < settings.rsi_threshold
    )
    if not passes:
        return None

    plan = position_plan(price, atr14, settings)
    if plan is None:  # can't afford even one share within the rules
        return None

    # Composite ranking. Relative strength leads (that's what the research says
    # matters most), then trend strength, pullback depth, and volatility.
    setup_score = round(
        rs_rating * 0.6
        + min(adx14, 40)
        + (settings.rsi_threshold - rsi14)
        + min(atr_pct, 8) * 2,
        1,
    )

    return {
        "ticker": ticker,
        "price": round(price, 2),
        "rsi": round(rsi14, 1),
        "avg_volume": int(avg_volume),
        "rel_volume": round(rel_volume, 2),
        "adx": round(adx14, 1),
        "atr": round(atr14, 2),
        "atr_pct": round(atr_pct, 1),
        "rs_rating": round(rs_rating, 0),
        "pct_from_high": round(pct_from_high, 1),
        "sma20": round(sma20, 2),
        "sma50": round(sma50, 2),
        "sma200": round(sma200, 2),
        "pct_above_sma50": round((price / sma50 - 1) * 100, 1),
        "setup_score": setup_score,
        "plan": plan,
    }


def scan_market(
    settings: ScanSettings,
    progress: Callable[[str], None] = lambda msg: None,
) -> list[dict]:
    """Blocking scan over the whole universe. Run in a worker thread.

    Two phases: (1) download everything and compute each name's momentum (the
    relative-strength rating is its percentile across the WHOLE universe), while
    retaining only liquid/affordable names for detailed evaluation; (2) apply the
    full filter set using that rating, and keep the top-N setups."""
    tickers = load_universe(settings.universe)
    total = len(tickers)
    scope = "full US market" if settings.universe == "full" else "curated list"
    frames: dict[str, pd.DataFrame] = {}  # only liquid/affordable names (bounds memory)
    momentum: dict[str, float] = {}  # every valid name, for a market-wide RS rating

    # --- phase 1: download, momentum, cheap pre-gate ---
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
                if len(df) < MIN_BARS:
                    continue
                mom = _blended_momentum(df["Close"])
                if mom is None:
                    continue
                momentum[ticker] = mom  # ranked across the whole universe

                # Cheap liquidity/price pre-gate: only keep frames worth fully
                # evaluating. Drops the long tail of sub-$15 / illiquid names so
                # we don't hold thousands of DataFrames in memory.
                price = float(df["Close"].iloc[-1])
                avg_vol = float(df["Volume"].tail(21).mean())
                if price > settings.min_price and price <= settings.max_price and avg_vol > settings.min_avg_volume:
                    frames[ticker] = df
            except Exception:
                log.exception("Failed preparing %s", ticker)

    if not momentum:
        return []

    # --- relative-strength rating: percentile rank of momentum across universe ---
    mom_series = pd.Series(momentum)
    rs_ratings = (mom_series.rank(pct=True) * 100).round(1).to_dict()

    # --- phase 2: full filter set on the liquid subset ---
    progress(f"Ranking {len(momentum)} stocks, evaluating {len(frames)} liquid candidates…")
    results: list[dict] = []
    for ticker, df in frames.items():
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
