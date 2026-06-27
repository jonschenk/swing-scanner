"""Backtester (phase 1) — strict no-lookahead replay of the leader-pullback scan.

The R&D workbench: run a strategy variation over years of daily history and measure
its edge (win rate + R-multiple expectancy). Claude Code runs it in dev sessions; we
review the numbers and decide the next variation. Results here are a HYPOTHESIS to
validate forward in paper, never proof (survivorship bias, idealized fills — see below).

No-lookahead discipline (the thing that makes results valid):
  * every indicator at day T uses only bars <= T (they're backward-looking by construction)
  * cross-sectional RS rank at day T uses every name's momentum as-of T (past prices only)
  * a signal on day T's close is ENTERED at day T+1's OPEN — never T's close
  * the exit walk only ever looks at bars on/after the entry

Phase-1 scope: curated universe, R-multiple stats (capital-independent). Equity curve,
drawdown, profit-factor slicing, CSV/STRATEGY.md output, variation comparison, and the
train/test split come in later phases. Honest limits: daily bars only; the universe is
TODAY's listings (survivorship-biased -> results overstated); fills are idealized (stop/
target fill exactly at the level, no slippage/gaps).
"""

import argparse
import csv
import datetime as dt
import hashlib
import logging
import math
import pickle
from dataclasses import dataclass, fields
from pathlib import Path

import pandas as pd
import yfinance as yf

from .config import ScanSettings
from .indicators import adx, atr, rsi, sma
from .risk import position_plan
from .universe import load_universe

log = logging.getLogger(__name__)

MIN_BARS = 220          # need ~1y of history before a name is eligible (matches the live scan)
DEFAULT_MAX_HOLD = 10   # time-stop: close at this many trading days if neither stop nor target hits
# High default capital so the affordability/price ceiling doesn't distort a strategy backtest.
# R-multiple stats are capital-independent anyway; capital only affects the max_price filter.
DEFAULT_CAPITAL = 1_000_000

# Cache the (slow) raw download so re-runs and variation sweeps don't re-fetch.
CACHE_DIR = Path(__file__).resolve().parents[1] / ".backtest_cache"
# The readable R&D briefing the backtester writes its results to (gitignored).
STRATEGY_MD = Path(__file__).resolve().parents[1] / "STRATEGY.md"


def export_trades_csv(trades: list, path: str) -> str:
    cols = [f.name for f in fields(Trade)]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for t in trades:
            w.writerow([getattr(t, c) for c in cols])
    return path


def write_strategy_md(title: str, table_md: str, note: str = "") -> Path:
    """Persist a backtest run/sweep to STRATEGY.md — the doc any session reads to see where the
    strategy R&D stands (what's been tried, what won, what to try next)."""
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    STRATEGY_MD.write_text(
        f"# Strategy backtest\n\n_generated {stamp}_\n\n## {title}\n\n{table_md}\n\n{note}\n"
    )
    return STRATEGY_MD


# ----------------------------------------------------------------- data

_DL_BATCH = 200  # download in chunks so the full universe (~5,900 names) is robust, not one giant call


def _download(tickers: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """Daily OHLCV per ticker over [start, end], split/div-adjusted. Downloaded in batches so the
    full-market universe doesn't hinge on a single huge request, with progress logged per batch."""
    out: dict[str, pd.DataFrame] = {}
    total = len(tickers)
    for i in range(0, total, _DL_BATCH):
        batch = tickers[i : i + _DL_BATCH]
        try:
            raw = yf.download(
                batch, start=start, end=end, interval="1d",
                auto_adjust=True, group_by="ticker", progress=False, threads=True,
            )
        except Exception:
            log.exception("Batch download failed for %s..%s", batch[0], batch[-1])
            continue
        if raw is None or raw.empty:
            continue
        for t in batch:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
                if len(df) >= MIN_BARS:
                    out[t] = df
            except (KeyError, Exception):
                continue
        if total > _DL_BATCH:
            log.info("  downloaded %d/%d names (%d with usable history)…", min(i + _DL_BATCH, total), total, len(out))
    return out


_SPY_CACHE: dict[tuple[str, str], pd.Series | None] = {}


def _spy_close(start: str, end: str) -> pd.Series | None:
    """SPY daily close over the window (memoized per run so the regime helpers share one
    download). None if SPY can't be fetched."""
    key = (start, end)
    if key not in _SPY_CACHE:
        try:
            spy = yf.download("SPY", start=start, end=end, interval="1d", auto_adjust=True, progress=False)
            close = spy["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            _SPY_CACHE[key] = close
        except Exception:
            log.exception("SPY download for the regime classifier failed")
            _SPY_CACHE[key] = None
    return _SPY_CACHE[key]


def _market_up_series(start: str, end: str) -> pd.Series | None:
    """Boolean series: is the broad market (SPY) in an uptrend (close > its 200-SMA AND the
    200-SMA rising) on each day? Used as an as-of regime gate. None if SPY can't be fetched."""
    close = _spy_close(start, end)
    if close is None:
        return None
    sma200 = close.rolling(200).mean()
    # Bull regime = price above the 200-SMA AND the 200-SMA rising (over ~1mo). The "rising"
    # condition excludes bear-market bounces (falling 200-SMA) a plain price>200SMA gate lets
    # through as bull traps.
    return (close > sma200) & (sma200 > sma200.shift(21))


def _regime_series(start: str, end: str) -> pd.Series | None:
    """Classify each day's MARKET regime from SPY (as-of correct), into one of three states the
    router maps to strategies:
      * "bull"  — SPY above a RISING 200-SMA            -> trend-following (leader-pullback)
      * "bear"  — SPY below a FALLING 200-SMA           -> CASH (no dip-buying, no momentum)
      * "chop"  — anything in between (range / transition) -> mean-reversion
    The two signals (location vs the 200-SMA, slope of the 200-SMA) are exactly the leader-pullback
    bull definition split into a 3-way taxonomy. Warmup days (no 200-SMA yet) are treated as bear
    (= cash), so the router never trades on an unclassifiable day. None if SPY can't be fetched."""
    close = _spy_close(start, end)
    if close is None:
        return None
    sma200 = close.rolling(200).mean()
    above = close > sma200
    rising = sma200 > sma200.shift(21)
    regime = pd.Series("chop", index=close.index)
    regime[above & rising] = "bull"
    regime[(~above) & (~rising)] = "bear"
    regime[sma200.isna()] = "bear"  # warmup -> cash (unclassifiable)
    return regime


# ----------------------------------------------------------------- as-of indicator frames

def _indicator_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Per-ticker frame of every series the filter needs, indexed by date. Each value
    at row T uses only bars <= T, so reading row T is inherently as-of correct."""
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    sma200 = sma(close, 200)
    f = pd.DataFrame(
        {
            "open": df["Open"], "high": high, "low": low, "close": close,
            "sma5": sma(close, 5),  # mean-reversion: reversion-exit reference
            "rsi2": rsi(close, 2),  # mean-reversion: fast oversold trigger (Connors-style)
            "sma20": sma(close, 20), "sma50": sma(close, 50), "sma200": sma200,
            "sma200_prior": sma200.shift(22),               # ~1 month ago (200-SMA slope)
            "avgvol": vol.rolling(21).mean(),
            "rsi": rsi(close, 14), "atr": atr(high, low, close, 14), "adx": adx(high, low, close, 14),
            "high52": high.rolling(252, min_periods=200).max(),
            "low52": low.rolling(252, min_periods=200).min(),
            # blended multi-timeframe momentum (the RS-rank input), as a series
            "mom": 0.2 * (close / close.shift(21) - 1)
            + 0.5 * (close / close.shift(63) - 1)
            + 0.3 * (close / close.shift(126) - 1),
        }
    )
    return f


def _rs_table(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Cross-sectional RS rating (0-100) per date: each name's momentum percentile-ranked
    against the whole universe AS OF that day. dates x tickers."""
    mom = pd.DataFrame({t: f["mom"] for t, f in frames.items()})
    return mom.rank(axis=1, pct=True) * 100


def _breadth_series(frames: dict[str, pd.DataFrame]) -> pd.Series:
    """Market-internals breadth per date: % of the universe trading above its OWN 200-SMA
    (among names with a valid 200-SMA). As-of correct. This is what catches a stealth rotation
    — breadth decays as leaders roll over, even while the index holds up."""
    above = pd.DataFrame({t: (f["close"] > f["sma200"]) for t, f in frames.items()})
    valid = pd.DataFrame({t: f["sma200"].notna() for t, f in frames.items()})
    return (above.sum(axis=1) / valid.sum(axis=1).replace(0, pd.NA)) * 100


# ----------------------------------------------------------------- signals + simulation

@dataclass
class Trade:
    ticker: str
    signal_date: str
    entry_date: str
    entry: float
    stop: float
    target: float
    exit_date: str
    exit: float
    exit_reason: str   # target | stop | time
    r_multiple: float
    hold_days: int
    outcome: str       # win | loss | scratch


def _signal_mask(ind: pd.DataFrame, rs: pd.Series, s: ScanSettings) -> pd.Series:
    """Boolean series: does this name pass the full leader-pullback filter on each day?
    Same conditions as the live scanner.evaluate_ticker, vectorized. NaN warmup rows
    compare False, so they're excluded automatically."""
    atr_pct = ind["atr"] / ind["close"] * 100
    pct_from_high = (ind["high52"] - ind["close"]) / ind["high52"] * 100
    pct_above_low = (ind["close"] / ind["low52"] - 1) * 100
    return (
        (ind["close"] > s.min_price) & (ind["close"] <= s.max_price) & (ind["avgvol"] > s.min_avg_volume)
        & (ind["close"] > ind["sma50"]) & (ind["close"] > ind["sma200"])
        & (ind["sma20"] > ind["sma50"]) & (ind["sma50"] > ind["sma200"])
        & (ind["sma200"] > ind["sma200_prior"])
        & (rs >= s.min_rs_rating)
        & (pct_from_high <= s.near_high_pct) & (pct_above_low >= s.min_above_low_pct)
        & (ind["adx"] >= s.adx_min) & (atr_pct >= s.atr_pct_min)
        & (ind["rsi"] >= s.rsi_floor) & (ind["rsi"] < s.rsi_threshold)
    )


def _apply_costs(entry: float, exit_price: float, stop: float, slippage_bps: float) -> tuple[float, float, float]:
    """Apply per-side slippage (bps of price) to the entry (buy higher) and exit (sell lower),
    then recompute the R-multiple off the actual fills. Stock commissions are ~$0 now, so
    slippage is the dominant modelable cost. Conservative: applied to every exit type."""
    k = slippage_bps / 10000.0
    entry_fill = entry * (1 + k)
    exit_fill = exit_price * (1 - k)
    rps = entry_fill - stop
    r = (exit_fill - entry_fill) / rps if rps > 0 else 0.0
    return entry_fill, exit_fill, r


def _simulate(ind: pd.DataFrame, loc: int, s: ScanSettings, max_hold: int, slippage_bps: float = 0.0,
              exit_mode: str = "fixed", trail_mult: float | None = None) -> Trade | None:
    """Enter at the NEXT bar's open after the signal at position `loc`; walk forward up to
    max_hold bars applying the bracket (stop/target) then a time-stop. Stop is checked before
    target on a same-day touch of both (conservative).

    `exit_mode` controls stop management (all no-lookahead — the stop active during bar j is set
    only from bars <= j-1; the moving stop is updated AFTER that bar's low/high are tested):
      - "fixed":     the original static stop + capped/uncapped target (the baseline).
      - "breakeven": once a bar's high tags entry +1R, raise the stop to entry; target unchanged.
      - "trail":     chandelier ATR trail — stop = highest_high_since_entry - trail_mult*ATR,
                     ratcheting up only. The fixed target is REMOVED so winners can run (the trail
                     and time-stop become the only exits), matching the 'uncap the target' finding.
    R is always measured against the INITIAL stop (the risk actually taken)."""
    if loc + 1 >= len(ind):
        return None  # no next bar to enter on
    entry = float(ind["open"].iloc[loc + 1])
    atrv = float(ind["atr"].iloc[loc])           # ATR as of the signal day
    high52 = float(ind["high52"].iloc[loc])
    plan = position_plan(entry, atrv, s, high52)
    if plan is None:
        return None
    init_stop, target = plan["stop"], plan["target"]
    rps = entry - init_stop
    if trail_mult is None:
        trail_mult = s.atr_stop_mult   # reuse the initial stop's multiple — no new tuned knob
    use_target = exit_mode != "trail"  # pure trail lets winners run (no fixed cap)

    cur_stop, highest, be_armed = init_stop, entry, False
    exit_price = exit_reason = exit_loc = None
    last = min(loc + max_hold, len(ind) - 1)
    for j in range(loc + 1, last + 1):
        lo, hi = float(ind["low"].iloc[j]), float(ind["high"].iloc[j])
        if lo <= cur_stop:  # cur_stop reflects only prior bars (no lookahead)
            reason = "stop" if cur_stop <= init_stop else ("trail" if exit_mode == "trail" else "breakeven")
            exit_price, exit_reason, exit_loc = cur_stop, reason, j
            break
        if use_target and hi >= target:
            exit_price, exit_reason, exit_loc = target, "target", j
            break
        # ratchet the moving stop AFTER testing this bar (effective from the next bar)
        if exit_mode == "trail":
            highest = max(highest, hi)
            cur_stop = max(cur_stop, highest - trail_mult * atrv)
        elif exit_mode == "breakeven" and not be_armed and hi >= entry + rps:
            cur_stop, be_armed = max(cur_stop, entry), True
    if exit_price is None:  # time-stop at the close of the last bar in the window
        exit_price, exit_reason, exit_loc = float(ind["close"].iloc[last]), "time", last

    entry_fill, exit_fill, r = _apply_costs(entry, exit_price, init_stop, slippage_bps)
    return Trade(
        ticker="",  # filled by caller
        signal_date=str(ind.index[loc].date()),
        entry_date=str(ind.index[loc + 1].date()),
        entry=round(entry_fill, 2),
        stop=round(init_stop, 2),
        target=round(target, 2),
        exit_date=str(ind.index[exit_loc].date()),
        exit=round(exit_fill, 2),
        exit_reason=exit_reason,
        r_multiple=round(r, 2),
        hold_days=exit_loc - (loc + 1),
        outcome="win" if r > 0 else ("loss" if r < 0 else "scratch"),
    )


# ---- mean-reversion strategy: buy a quality name when it's deeply oversold, exit on the bounce.
# Genuinely different from leader-pullback: oversold (not healthy) entry, condition-based exit
# (reversion to the 5-SMA), no RS/52w-high/ADX requirements. Targets the chop where momentum dies.

def _signal_mask_meanrev(ind: pd.DataFrame, s: ScanSettings) -> pd.Series:
    # base: a quality name (above its 200-SMA), deeply oversold short-term, stretched below
    # the 5-SMA. The thresholds are now tunable so we can trade FEWER, BETTER dips — the fix
    # for a high-frequency edge that costs were eating alive.
    stretch_pct = (ind["sma5"] - ind["close"]) / ind["sma5"] * 100
    mask = (
        (ind["close"] > s.min_price) & (ind["avgvol"] > s.min_avg_volume)
        & (ind["close"] > ind["sma200"])         # quality: long-term uptrend (no broken stocks)
        & (ind["rsi2"] < s.mr_rsi2_max)          # deeply oversold short-term (Connors-style)
        & (ind["close"] < ind["sma5"])           # stretched below the short MA
        & (stretch_pct >= s.mr_min_stretch_pct)  # ...by at least this much (a real dip)
    )
    if s.mr_require_uptrend:                      # don't catch knives: dip must sit in a real uptrend
        mask = mask & (ind["sma50"] > ind["sma200"]) & (ind["sma200"] > ind["sma200_prior"])
    return mask


def _signal_mask_breakout(ind: pd.DataFrame, s: ScanSettings) -> pd.Series:
    """Donchian / turtle breakout: the close makes a new high vs the highest high of the prior
    `donchian_lookback` bars, in a liquid name that's above its 200-SMA (only breakouts inside a
    long-term uptrend). The channel uses `.shift(1)` so it's built from PRIOR bars only (as-of
    correct). This is the deliberate OPPOSITE of leader-pullback: it buys strength / new highs
    rather than dips, so it selects different entries and pairs with the ATR trailing exit (ride
    the trend, no fixed target). A genuinely different, trend-following sleeve."""
    n = s.donchian_lookback
    channel_high = ind["high"].rolling(n, min_periods=n).max().shift(1)
    return (
        (ind["close"] > s.min_price) & (ind["close"] <= s.max_price) & (ind["avgvol"] > s.min_avg_volume)
        & (ind["close"] > ind["sma200"])    # quality: only breakouts in a long-term uptrend
        & (ind["close"] >= channel_high)     # the breakout: a new `n`-day closing high
    )


def _simulate_meanrev(ind: pd.DataFrame, loc: int, s: ScanSettings, max_hold: int, slippage_bps: float = 0.0,
                      exit_mode: str = "fixed", trail_mult: float | None = None) -> Trade | None:
    """Enter next open; exit on the reversion (a close back above the 5-SMA), a protective ATR
    stop, or a time-stop. Exit is condition-based, not a fixed target. `exit_mode`/`trail_mult` are
    accepted for a uniform signature but IGNORED here: trailing/breakeven make no sense for a
    mean-reversion thesis whose whole edge is the snap-back to the 5-SMA (you want that exit, not a
    trail past it). So this leg always uses its reversion/stop/time exits."""
    if loc + 1 >= len(ind):
        return None
    entry = float(ind["open"].iloc[loc + 1])
    atrv = float(ind["atr"].iloc[loc])
    stop = entry - s.atr_stop_mult * atrv
    if stop <= 0 or entry <= 0:
        return None
    exit_price = exit_reason = exit_loc = None
    last = min(loc + max_hold, len(ind) - 1)
    for j in range(loc + 1, last + 1):
        lo, c, m5 = float(ind["low"].iloc[j]), float(ind["close"].iloc[j]), float(ind["sma5"].iloc[j])
        if lo <= stop:
            exit_price, exit_reason, exit_loc = stop, "stop", j
            break
        if c > m5:  # reverted — the bounce happened
            exit_price, exit_reason, exit_loc = c, "reversion", j
            break
    if exit_price is None:
        exit_price, exit_reason, exit_loc = float(ind["close"].iloc[last]), "time", last
    target_nominal = entry + s.reward_mult * (entry - stop)
    entry_fill, exit_fill, r = _apply_costs(entry, exit_price, stop, slippage_bps)
    return Trade(
        ticker="", signal_date=str(ind.index[loc].date()), entry_date=str(ind.index[loc + 1].date()),
        entry=round(entry_fill, 2), stop=round(stop, 2), target=round(target_nominal, 2),
        exit_date=str(ind.index[exit_loc].date()), exit=round(exit_fill, 2), exit_reason=exit_reason,
        r_multiple=round(r, 2), hold_days=exit_loc - (loc + 1),
        outcome="win" if r > 0 else ("loss" if r < 0 else "scratch"),
    )


def _trades_for(
    ticker: str, ind: pd.DataFrame, rs: pd.Series, s: ScanSettings, max_hold: int,
    market_up: pd.Series | None = None, breadth: pd.Series | None = None,
    strategy: str = "leader_pullback", slippage_bps: float = 0.0,
    regime_gate: pd.Series | None = None, exit_mode: str = "fixed",
    trail_mult: float | None = None,
) -> list[Trade]:
    """All non-overlapping trades for one ticker: take each signal, but don't re-enter the
    same name while a position in it is still open. Dispatches on `strategy`. `regime_gate`
    (a boolean date series) is the router's market-regime admission filter — a signal is only
    taken on a day the gate is True (the exit still walks freely into other regimes)."""
    if strategy == "mean_reversion":
        sig = _signal_mask_meanrev(ind, s)
        simulate = _simulate_meanrev
    elif strategy == "breakout":
        sig = _signal_mask_breakout(ind, s)
        simulate = _simulate
        if exit_mode == "fixed":
            exit_mode = "trail"   # turtle-style: breakouts ride the ATR trailing stop (no fixed cap)
    else:
        sig = _signal_mask(ind, rs.reindex(ind.index), s)
        if s.require_market_uptrend and market_up is not None:
            sig = sig & market_up.reindex(ind.index).fillna(False)
        if s.min_breadth_pct > 0 and breadth is not None:
            sig = sig & (breadth.reindex(ind.index) >= s.min_breadth_pct).fillna(False)
        simulate = _simulate
    if regime_gate is not None:
        sig = sig & regime_gate.reindex(ind.index).fillna(False)

    trades: list[Trade] = []
    in_until_loc = -1
    locs = [ind.index.get_loc(d) for d in sig.index[sig.fillna(False)]]
    for loc in locs:
        if loc < MIN_BARS or loc <= in_until_loc:
            continue
        t = simulate(ind, loc, s, max_hold, slippage_bps, exit_mode, trail_mult)
        if t is None:
            continue
        t.ticker = ticker
        trades.append(t)
        in_until_loc = ind.index.get_loc(pd.Timestamp(t.exit_date))
    return trades


# ----------------------------------------------------------------- stats

def _stats(trades: list[Trade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    wins = [t for t in trades if t.r_multiple > 0]
    losses = [t for t in trades if t.r_multiple < 0]
    gross_win = sum(t.r_multiple for t in wins)
    gross_loss = -sum(t.r_multiple for t in losses)
    total_r = sum(t.r_multiple for t in trades)
    reasons = {r: sum(1 for t in trades if t.exit_reason == r) for r in ("target", "reversion", "stop", "time", "trail", "breakeven")}

    # R-multiple equity curve (fixed-risk units, ordered by exit) + peak-to-trough drawdown.
    cum = peak = maxdd = 0.0
    for t in sorted(trades, key=lambda x: x.exit_date):
        cum += t.r_multiple
        peak = max(peak, cum)
        maxdd = max(maxdd, peak - cum)

    return {
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "expectancy_r": round(total_r / n, 3),
        "total_r": round(total_r, 1),
        "max_drawdown_r": round(maxdd, 1),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else float("inf"),
        "avg_win_r": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss_r": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "avg_hold_days": round(sum(t.hold_days for t in trades) / n, 1),
        "exits": reasons,
    }


# ----------------------------------------------------------------- dataset (cached) + run

def _cache_key(names: list[str], start: str, end: str) -> str:
    return hashlib.sha1(("|".join(sorted(names)) + start + end).encode()).hexdigest()[:16]


def _load_bars(key: str) -> dict | None:
    p = CACHE_DIR / f"{key}.pkl"
    if p.exists():
        try:
            return pickle.loads(p.read_bytes())
        except Exception:
            return None
    return None


def _save_bars(key: str, frames_raw: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    (CACHE_DIR / f"{key}.pkl").write_bytes(pickle.dumps(frames_raw))


def build_dataset(start: str, end: str, universe: str = "curated", tickers: list[str] | None = None) -> dict:
    """Download (or load cached) bars and precompute indicator frames + the cross-sectional
    RS table ONCE. Variations then run against this cheaply, no re-download — that's what makes
    sweeping many variations practical."""
    names = tickers or load_universe(universe)
    key = _cache_key(names, start, end)
    frames_raw = _load_bars(key)
    if frames_raw is None:
        log.info("Downloading %d names %s -> %s …", len(names), start, end)
        frames_raw = _download(names, start, end)
        if frames_raw:
            _save_bars(key, frames_raw)
    else:
        log.info("Loaded %d names from cache", len(frames_raw))
    frames = {t: _indicator_frame(df) for t, df in frames_raw.items()}
    rs = _rs_table(frames) if frames else pd.DataFrame()
    market_up = _market_up_series(start, end)
    breadth = _breadth_series(frames) if frames else None
    regime = _regime_series(start, end)
    return {"frames": frames, "rs": rs, "market_up": market_up, "breadth": breadth,
            "regime": regime, "start": start, "end": end, "names": len(frames)}


def run_on_dataset(ds: dict, settings: ScanSettings, max_hold: int = DEFAULT_MAX_HOLD,
                   strategy: str = "leader_pullback", slippage_bps: float = 0.0,
                   exit_mode: str = "fixed", trail_mult: float | None = None) -> dict:
    """Run ONE variation against a prebuilt dataset — the cheap, repeatable part."""
    trades: list[Trade] = []
    market_up, breadth = ds.get("market_up"), ds.get("breadth")
    for t, ind in ds["frames"].items():
        trades.extend(_trades_for(t, ind, ds["rs"][t], settings, max_hold, market_up, breadth, strategy,
                                  slippage_bps, exit_mode=exit_mode, trail_mult=trail_mult))
    trades.sort(key=lambda x: x.entry_date)
    return {"stats": _stats(trades), "trades": trades}


def run_backtest(
    settings: ScanSettings, start: str, end: str,
    universe: str = "curated", tickers: list[str] | None = None, max_hold: int = DEFAULT_MAX_HOLD,
) -> dict:
    ds = build_dataset(start, end, universe, tickers)
    if not ds["frames"]:
        return {"error": "No data downloaded."}
    r = run_on_dataset(ds, settings, max_hold)
    return {"names_with_data": ds["names"], "start": start, "end": end, "max_hold": max_hold, **r}


# ----------------------------------------------------------------- variation sweep

# Candidate variations to compare against one dataset. Each: (name, ScanSettings overrides,
# max_hold). Designed to probe what the baseline flagged: thin/capped wins, lots of stops and
# time-stops. The cap toggle + reward multiple directly test the R:R question.
CANDIDATES = [
    ("baseline (2R capped)", {}, 10),
    ("2R uncapped", {"cap_target_at_high": False}, 10),
    ("3R capped", {"reward_mult": 3.0}, 10),
    ("3R uncapped", {"reward_mult": 3.0, "cap_target_at_high": False}, 10),
    ("1.5R capped", {"reward_mult": 1.5}, 10),
    ("tight RS>=85", {"min_rs_rating": 85.0}, 10),
    ("strong trend ADX>=30", {"adx_min": 30.0}, 10),
    ("deeper pullback RSI 35-55", {"rsi_floor": 35.0, "rsi_threshold": 55.0}, 10),
    ("longer hold 20d", {}, 20),
    ("wider stop 2.5xATR", {"atr_stop_mult": 2.5}, 10),
    # combined winners from the first sweep: uncap the target + be selective on trend
    ("2.5R uncapped", {"reward_mult": 2.5, "cap_target_at_high": False}, 10),
    ("2R uncapped + ADX>=30", {"cap_target_at_high": False, "adx_min": 30.0}, 10),
    ("3R uncapped + ADX>=30", {"reward_mult": 3.0, "cap_target_at_high": False, "adx_min": 30.0}, 10),
    ("3R uncapped + ADX30 + RS80", {"reward_mult": 3.0, "cap_target_at_high": False, "adx_min": 30.0, "min_rs_rating": 80.0}, 10),
    # regime-gated: only enter when SPY is in an uptrend (the bear-market fix to test)
    ("baseline + mkt-up", {"require_market_uptrend": True}, 10),
    ("2R uncapped + mkt-up", {"cap_target_at_high": False, "require_market_uptrend": True}, 10),
    ("3R uncapped + ADX30 + mkt-up", {"reward_mult": 3.0, "cap_target_at_high": False, "adx_min": 30.0, "require_market_uptrend": True}, 10),
    # breadth-gated: only enter when >= X% of the universe is above its own 200-SMA (catches the
    # 2021 stealth rotation an index gate misses). The "other strategy" to test.
    ("baseline + breadth>=50", {"min_breadth_pct": 50.0}, 10),
    ("2R uncapped + breadth>=50", {"cap_target_at_high": False, "min_breadth_pct": 50.0}, 10),
    ("2R uncapped + breadth>=60", {"cap_target_at_high": False, "min_breadth_pct": 60.0}, 10),
    ("3R uncapped + ADX30 + breadth>=50", {"reward_mult": 3.0, "cap_target_at_high": False, "adx_min": 30.0, "min_breadth_pct": 50.0}, 10),
]

# Mean-reversion candidates. Run with --strategy mean_reversion. The first block is the loose
# baseline (RSI2<10, any dip) we already proved costs eat alive; the rest tighten the entry to
# trade FEWER, BETTER dips so each trade's edge can clear the slippage drag. Levers: mr_rsi2_max
# (oversold depth), mr_min_stretch_pct (how far below the 5-SMA), mr_require_uptrend (quality gate).
# Stop=2.5xATR / hold=10 was the prior best, so it's held fixed while we vary selectivity.
# NOTE: mr_min_stretch_pct now defaults to 4.0 (the validated selective entry is the app default),
# so candidates that want the LOOSE/no-stretch entry must set it back to 0.0 explicitly.
_MR_BASE = {"atr_stop_mult": 2.5}
MEANREV_CANDIDATES = [
    ("loose RSI2<10 (baseline)", {**_MR_BASE, "mr_min_stretch_pct": 0.0}, 10),
    ("RSI2<5", {**_MR_BASE, "mr_rsi2_max": 5.0, "mr_min_stretch_pct": 0.0}, 10),
    ("RSI2<3", {**_MR_BASE, "mr_rsi2_max": 3.0, "mr_min_stretch_pct": 0.0}, 10),
    ("RSI2<10 + stretch>=2%", {**_MR_BASE, "mr_min_stretch_pct": 2.0}, 10),
    ("RSI2<10 + stretch>=4%", {**_MR_BASE, "mr_min_stretch_pct": 4.0}, 10),
    ("RSI2<5 + stretch>=3%", {**_MR_BASE, "mr_rsi2_max": 5.0, "mr_min_stretch_pct": 3.0}, 10),
    ("RSI2<5 + uptrend", {**_MR_BASE, "mr_rsi2_max": 5.0, "mr_min_stretch_pct": 0.0, "mr_require_uptrend": True}, 10),
    ("RSI2<5 + stretch>=3% + uptrend", {**_MR_BASE, "mr_rsi2_max": 5.0, "mr_min_stretch_pct": 3.0, "mr_require_uptrend": True}, 10),
    ("RSI2<3 + stretch>=4% + uptrend", {**_MR_BASE, "mr_rsi2_max": 3.0, "mr_min_stretch_pct": 4.0, "mr_require_uptrend": True}, 10),
    ("RSI2<5 + uptrend, hold5", {**_MR_BASE, "mr_rsi2_max": 5.0, "mr_min_stretch_pct": 0.0, "mr_require_uptrend": True}, 5),
]


def compare(ds: dict, candidates=CANDIDATES, capital: float = DEFAULT_CAPITAL,
            strategy: str = "leader_pullback", slippage_bps: float = 0.0) -> list[tuple[str, dict]]:
    """Run each candidate variation against the same dataset; return rows sorted by expectancy."""
    rows = [(name, run_on_dataset(ds, ScanSettings(capital=capital, **ov), mh, strategy, slippage_bps)["stats"]) for name, ov, mh in candidates]
    rows.sort(key=lambda r: r[1].get("expectancy_r", -99), reverse=True)
    return rows


def compare_exits(ds: dict, split: str | None, capital: float = DEFAULT_CAPITAL,
                  slippage_bps: float = 0.0, params: dict | None = None,
                  max_hold: int = DEFAULT_MAX_HOLD) -> list[tuple]:
    """Head-to-head of exit-MANAGEMENT modes on the SAME leader-pullback variation (the router's
    validated bull leg by default): fixed stop+target vs breakeven-after-+1R vs ATR trailing. The
    entry signal + sizing are held identical so the ONLY thing that varies is how the trade is
    managed. Returns [(mode, full_stats, train_stats, test_stats)]."""
    params = params if params is not None else DEFAULT_ROUTER["bull"][1]
    out = []
    for mode in ("fixed", "breakeven", "trail"):
        trades = run_on_dataset(ds, ScanSettings(capital=capital, **params), max_hold,
                                "leader_pullback", slippage_bps, exit_mode=mode)["trades"]
        tr, te = _split_stats(trades, split) if split else ({}, {})
        out.append((mode, _stats(trades), tr, te))
    return out


def sweep_trail(ds: dict, split: str | None, capital: float = DEFAULT_CAPITAL,
                slippage_bps: float = 0.0, mults=(1.0, 1.5, 2.0, 2.5, 3.0),
                params: dict | None = None, max_hold: int = DEFAULT_MAX_HOLD) -> list[tuple]:
    """Vary ONLY the trailing distance (in ATRs) while holding the entry, sizing, and the INITIAL
    stop fixed — so this isolates whether the trail's edge is a knife-edge on the distance I picked
    (which just reused the stop multiple, untuned) or is stable across a range. Returns
    [(mult, full_stats, train_stats, test_stats)]."""
    params = params if params is not None else DEFAULT_ROUTER["bull"][1]
    out = []
    for m in mults:
        trades = run_on_dataset(ds, ScanSettings(capital=capital, **params), max_hold,
                                "leader_pullback", slippage_bps, exit_mode="trail", trail_mult=m)["trades"]
        tr, te = _split_stats(trades, split) if split else ({}, {})
        out.append((m, _stats(trades), tr, te))
    return out


def _split_stats(trades: list[Trade], split: str) -> tuple[dict, dict]:
    """Bucket trades by entry date into train (< split) and test (>= split). Strategy params
    are fixed and the run is continuous, so this is a clean out-of-sample split with no leakage."""
    train = [t for t in trades if t.signal_date < split]
    test = [t for t in trades if t.signal_date >= split]
    return _stats(train), _stats(test)


def compare_oos(ds: dict, split: str, candidates=CANDIDATES, capital: float = DEFAULT_CAPITAL,
                strategy: str = "leader_pullback", slippage_bps: float = 0.0) -> list:
    """Out-of-sample comparison: each variation's train vs test expectancy. Sorted by TEST
    expectancy — that's the number that matters (does the edge hold on unseen data?)."""
    rows = []
    for name, ov, mh in candidates:
        trades = run_on_dataset(ds, ScanSettings(capital=capital, **ov), mh, strategy, slippage_bps)["trades"]
        tr, te = _split_stats(trades, split)
        rows.append((name, tr, te))
    rows.sort(key=lambda r: r[2].get("expectancy_r", -99), reverse=True)
    return rows


# ----------------------------------------------------------------- regime router

# The routing policy: which strategy (+ its best known variation) trades in each market regime.
# "cash" = sit out. This is the first attempt to turn two bull/normal-regime edges into an
# all-weather system: trend-follow when SPY trends, mean-revert in the chop, hold cash in a
# confirmed downtrend (where BOTH strategies bled in backtests — momentum has nothing to ride
# and dip-buying catches knives). Tweak the variations here; the regime taxonomy is in
# _regime_series. Bull -> the cost-robust uncapped/ADX leader-pullback; chop -> the selective
# (deep-dip) mean-reversion that survives costs; bear -> cash.
DEFAULT_ROUTER = {
    "bull": ("leader_pullback", {"reward_mult": 3.0, "cap_target_at_high": False, "adx_min": 30.0}),
    "chop": ("mean_reversion", {"atr_stop_mult": 2.5, "mr_min_stretch_pct": 4.0}),
    "bear": ("cash", {}),
}


def run_router(ds: dict, policy: dict = DEFAULT_ROUTER, capital: float = DEFAULT_CAPITAL,
               max_hold: int = DEFAULT_MAX_HOLD, slippage_bps: float = 0.0) -> dict:
    """Replay the regime router: each day, only the strategy assigned to that day's regime may
    OPEN a trade (cash regimes open nothing). Returns the blended trade list + per-regime legs."""
    regime = ds.get("regime")
    legs: dict[str, dict] = {}
    combined: list[Trade] = []
    if regime is not None:
        for reg, (strat, ov) in policy.items():
            if strat == "cash":
                continue
            s = ScanSettings(capital=capital, **ov)
            gate = (regime == reg)
            leg: list[Trade] = []
            for t, ind in ds["frames"].items():
                leg.extend(_trades_for(t, ind, ds["rs"][t], s, max_hold,
                                       ds.get("market_up"), ds.get("breadth"), strat, slippage_bps,
                                       regime_gate=gate))
            legs[reg] = {"strategy": strat, "stats": _stats(leg)}
            combined.extend(leg)
    combined.sort(key=lambda x: x.entry_date)
    return {"stats": _stats(combined), "trades": combined, "legs": legs}


def _monthly_r(trades: list[Trade]) -> dict[str, float]:
    """Summed R-multiple per calendar month (keyed 'YYYY-MM', by exit date) — a sleeve's return
    stream, used to measure how correlated two strategies' month-to-month results are."""
    import collections
    m: dict[str, float] = collections.defaultdict(float)
    for t in trades:
        m[t.exit_date[:7]] += t.r_multiple
    return dict(m)


def run_sleeves(ds: dict, capital: float, slippage_bps: float, max_hold: int) -> dict[str, list[Trade]]:
    """Run the regime router AND each standalone strategy on the SAME dataset, so they can be
    compared head-to-head. Each standalone uses its router-leg variation (the tuned version), so
    'breakout' is the only genuinely new sleeve here. Returns {name: trades}."""
    sleeves = {"router": run_router(ds, DEFAULT_ROUTER, capital, max_hold, slippage_bps)["trades"]}
    specs = {
        "leader_pullback": DEFAULT_ROUTER["bull"][1],
        "mean_reversion": DEFAULT_ROUTER["chop"][1],
        "breakout": {},  # default breakout params (donchian_lookback default)
    }
    for name, ov in specs.items():
        s = ScanSettings(capital=capital, **ov)
        sleeves[name] = run_on_dataset(ds, s, max_hold, name, slippage_bps)["trades"]
    return sleeves


def run_dual_momentum(ds: dict, settings: ScanSettings, slippage_bps: float = 0.0) -> dict:
    """Monthly DUAL-MOMENTUM rotation: rank the universe by trailing `momentum_lookback`-day return,
    hold the top `rotation_top_n` names that ALSO have a positive trailing return (absolute momentum),
    equal-weight, rebalanced on the first trading day of each month. Slots whose name fails the absolute
    filter sit in CASH (the 'dual' downside protection) — so in a broad downturn the book goes largely
    to cash on its own. This is a portfolio ROTATION, not a stop/target trade list, so it returns a
    monthly-return series + the average number held. The signal is lagged one day (decided on the prior
    close) so there is no lookahead."""
    frames = ds["frames"]
    lb, topn = settings.momentum_lookback, settings.rotation_top_n
    k = slippage_bps / 10000.0
    closes = pd.DataFrame({t: f["close"] for t, f in frames.items()}).sort_index()
    mom = closes / closes.shift(lb) - 1.0
    idx = closes.index
    periods = idx.to_period("M")
    rebal = [idx[periods == p][0] for p in periods.unique()]   # first trading day of each month
    monthly: dict[str, float] = {}
    held_counts: list[int] = []
    for i in range(len(rebal) - 1):
        r, nxt = rebal[i], rebal[i + 1]
        rloc = idx.get_loc(r)
        if rloc - 1 < lb:
            continue
        sig = mom.iloc[rloc - 1]                       # decided on the PRIOR close (no lookahead)
        sig = sig[sig.notna() & (sig > 0)]             # absolute-momentum filter: positive trailing return only
        picks = list(sig.sort_values(ascending=False).head(topn).index)
        held_counts.append(len(picks))
        key = f"{r.year}-{r.month:02d}"
        rets = []
        for t in picks:
            c0, c1 = closes.at[r, t], closes.at[nxt, t]
            if pd.notna(c0) and pd.notna(c1) and c0 > 0:
                rets.append((c1 * (1 - k)) / (c0 * (1 + k)) - 1.0)
        # unfilled slots (fewer than topn qualify) are cash (0) — the defensive feature
        monthly[key] = sum(rets + [0.0] * (topn - len(rets))) / topn
    return {"monthly": monthly, "avg_held": round(sum(held_counts) / len(held_counts), 1) if held_counts else 0.0}


def _rotation_stats(monthly: dict) -> dict:
    """Performance of a monthly-return series: CAGR, annualized Sharpe, max drawdown %, monthly win rate."""
    rets = [r for _, r in sorted(monthly.items())]
    n = len(rets)
    if n == 0:
        return {"months": 0}
    eq, curve = 1.0, []
    for r in rets:
        eq *= (1 + r); curve.append(eq)
    mean = sum(rets) / n
    std = (sum((x - mean) ** 2 for x in rets) / (n - 1)) ** 0.5 if n > 1 else 0.0
    peak = maxdd = curve[0]
    maxdd = 0.0
    for v in curve:
        peak = max(peak, v)
        maxdd = max(maxdd, (peak - v) / peak)
    return {
        "months": n,
        "total_return": round((eq - 1) * 100, 1),
        "cagr": round((eq ** (12.0 / n) - 1) * 100, 1),
        "sharpe": round(mean / std * (12 ** 0.5), 2) if std > 0 else 0.0,
        "max_drawdown_pct": round(maxdd * 100, 1),
        "monthly_winrate": round(sum(1 for r in rets if r > 0) / n * 100, 1),
    }


def _split_monthly(monthly: dict, split: str) -> tuple[dict, dict]:
    sp = split[:7]   # compare on 'YYYY-MM'
    return ({m: r for m, r in monthly.items() if m < sp}, {m: r for m, r in monthly.items() if m >= sp})


def _regime_day_counts(regime: pd.Series | None, start: str, split: str | None = None) -> dict:
    """How the trading days split across regimes (overall, and train/test if a split is given)."""
    if regime is None:
        return {}
    r = regime[regime.index >= pd.Timestamp(start)]
    out = {"all": r.value_counts().to_dict()}
    if split:
        out["train"] = r[r.index < pd.Timestamp(split)].value_counts().to_dict()
        out["test"] = r[r.index >= pd.Timestamp(split)].value_counts().to_dict()
    return out


def run_portfolio(ds: dict, settings: ScanSettings, max_positions: int, start: str, end: str,
                  max_hold: int = DEFAULT_MAX_HOLD, slippage_bps: float = 0.0,
                  strategy: str = "leader_pullback", starting_cash: float | None = None,
                  regime_match: str | None = None, fractional: bool = False) -> dict:
    """Day-by-day PORTFOLIO simulation with the live constraints the per-trade backtest ignores:
    shared cash, a position-count cap, and a per-position allocation cap. Each day, candidate trades
    compete for the limited capital (ranked best-first by RS), so the book captures a realistic SUBSET
    of all signals — which is what shows how the slot/cap choices actually perform on equity + drawdown.
    `regime_match` gates ENTRIES to days in that regime (the router's kill-switch: leader only opens in
    'bull'; cash otherwise) — open positions still ride/exit freely.

    DESIGN: the candidate trades (and their exits/R) come straight from the validated per-trade engine
    (run_on_dataset), so this layer NEVER re-implements the exit walk — it only decides which candidates
    to take given cash/slots, and sizes them. By construction, taking every candidate reproduces the
    per-trade expectancy exactly (verified), so the constrained results are trustworthy."""
    frames = ds["frames"]
    rs_tab = ds["rs"]
    regime = ds.get("regime")
    start_cash = starting_cash if starting_cash is not None else settings.capital
    buf = 1 + 0.005  # cash headroom for slippage (matches risk.ALLOC_CASH_BUFFER)
    lo_b, hi_b = pd.Timestamp(start), pd.Timestamp(end)

    # Candidate trades from the VALIDATED per-trade engine — identical entries/exits/R.
    cand = run_on_dataset(ds, settings, max_hold, strategy, slippage_bps, exit_mode="trail")["trades"]
    by_entry: dict = {}
    for t in cand:
        ed = pd.Timestamp(t.entry_date)
        if not (lo_b <= ed <= hi_b):
            continue
        ind = frames[t.ticker]
        try:
            loc = ind.index.get_loc(ed) - 1  # the signal bar (entry is the next open)
        except KeyError:
            continue
        rsv = 0.0
        if strategy != "mean_reversion" and t.ticker in rs_tab.columns and 0 <= loc < len(rs_tab):
            v = rs_tab[t.ticker].iloc[loc]
            rsv = float(v) if pd.notna(v) else 0.0
        by_entry.setdefault(t.entry_date, []).append((rsv, t))

    all_dates = sorted({d for f in frames.values() for d in f.index if lo_b <= d <= hi_b})
    cash = start_cash
    positions: dict = {}   # ticker -> {shares, exit_date, exit_price, r}
    taken: list = []
    eq: list = []
    for T in all_dates:
        Tstr = str(T.date())
        # 1) exits — close any candidate whose exit_date is today (R + price come from the per-trade engine)
        for tk in [t for t, p in positions.items() if p["exit_date"] == Tstr]:
            p = positions.pop(tk)
            cash += p["shares"] * p["exit_price"]
            taken.append(p["r"])
        # 2) entries — today's candidates, ranked, into free slots + cash (off current equity).
        equity_now = cash + sum(p["shares"] * float(frames[t].at[T, "close"])
                                for t, p in positions.items() if T in frames[t].index)
        in_regime = regime_match is None or regime is None or (T in regime.index and regime.at[T] == regime_match)
        for rsv, t in (sorted(by_entry.get(Tstr, []), key=lambda x: x[0], reverse=True) if in_regime else []):
            if len(positions) >= max_positions or t.ticker in positions:
                continue
            entry, stop_dist = t.entry, t.entry - t.stop
            if stop_dist <= 0:
                continue
            rnd = (lambda x: x) if fractional else math.floor   # fractional = no whole-share rounding
            shares = min(rnd(equity_now * settings.risk_pct / 100 / stop_dist),
                         rnd(cash / (entry * buf)),
                         rnd(equity_now * settings.max_alloc_pct / 100 / entry))
            if shares < (1e-9 if fractional else 1) or shares * entry > cash:
                continue
            cash -= shares * entry
            positions[t.ticker] = {"shares": shares, "exit_date": t.exit_date, "exit_price": t.exit,
                                   "r": t.r_multiple}
        mkt = sum(p["shares"] * float(frames[t].at[T, "close"])
                  for t, p in positions.items() if T in frames[t].index)
        eq.append(cash + mkt)

    final = eq[-1] if eq else start_cash
    peak = maxdd = 0.0
    for v in eq:
        peak = max(peak, v)
        maxdd = max(maxdd, (peak - v) / peak if peak > 0 else 0.0)
    wins = [r for r in taken if r > 0]
    rets = pd.Series(eq).pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * (252 ** 0.5)) if len(rets) > 1 and rets.std() > 0 else 0.0
    return {"max_positions": max_positions, "final_equity": round(final, 0),
            "return_pct": round((final / start_cash - 1) * 100, 1),
            "max_drawdown_pct": round(maxdd * 100, 1), "sharpe": round(sharpe, 2),
            "trades": len(taken), "win_rate": round(len(wins) / len(taken) * 100, 1) if taken else 0.0,
            "expectancy_r": round(sum(taken) / len(taken), 3) if taken else 0.0}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="Backtest the leader-pullback strategy.")
    p.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    p.add_argument("--to", dest="end", default=dt.date.today().isoformat(), help="YYYY-MM-DD")
    p.add_argument("--universe", default="curated", choices=["curated", "full"])
    p.add_argument("--tickers", default=None, help="comma list to override the universe (quick tests)")
    p.add_argument("--variation", default=None, help="strategy variation id (default: active, else baseline)")
    p.add_argument("--max-hold", type=int, default=DEFAULT_MAX_HOLD)
    p.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    p.add_argument("--compare", action="store_true", help="sweep the built-in candidate variations")
    p.add_argument("--split", default=None, help="YYYY-MM-DD: out-of-sample split (train < split, test >=)")
    p.add_argument("--csv", default=None, help="write trades to this CSV path (single-variation run)")
    p.add_argument("--strategy", default="leader_pullback", choices=["leader_pullback", "mean_reversion", "breakout"])
    p.add_argument("--slippage-bps", type=float, default=5.0, help="per-side slippage in bps (default 5; 0 = frictionless)")
    p.add_argument("--router", action="store_true", help="run the regime router (bull->leader, chop->meanrev, bear->cash)")
    p.add_argument("--exit-mode", default="fixed", choices=["fixed", "breakeven", "trail"],
                   help="stop management for leader_pullback: fixed | breakeven (BE after +1R) | trail (ATR chandelier, no cap)")
    p.add_argument("--exit-compare", action="store_true",
                   help="head-to-head: fixed vs breakeven vs trail on the bull-leg variation (use with --split for OOS)")
    p.add_argument("--trail-sweep", action="store_true",
                   help="vary ONLY the trail distance (1.0-3.0xATR) on the bull leg — overfit/knife-edge check")
    p.add_argument("--portfolio", action="store_true",
                   help="portfolio simulation: shared cash + slots + per-position cap + trailing exits (real constraints)")
    p.add_argument("--sweep-positions", action="store_true",
                   help="with --portfolio: sweep max-positions 2..8 to see how the slot count drives return/drawdown")
    p.add_argument("--sleeves", action="store_true",
                   help="compare the router vs each standalone strategy (leader/meanrev/breakout/dual-momentum) OOS, "
                        "with a monthly-return correlation matrix (the 'beat or diversify' decision support; use --split)")
    p.add_argument("--dual-momentum", dest="dual_momentum", action="store_true",
                   help="run the monthly dual-momentum rotation standalone (rank by trailing return, hold top-N with "
                        "positive absolute momentum, else cash); reports CAGR/Sharpe/maxDD%% (use --split for OOS)")
    args = p.parse_args()
    cands = MEANREV_CANDIDATES if args.strategy == "mean_reversion" else CANDIDATES
    bps = args.slippage_bps

    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    ds = build_dataset(args.start, args.end, args.universe, tickers)  # downloaded once, cached
    if not ds["frames"]:
        print("No data downloaded."); return

    if args.portfolio:
        base = ScanSettings(capital=args.capital, **DEFAULT_ROUTER["bull"][1])
        counts = [2, 3, 4, 5, 6, 8] if args.sweep_positions else [base.max_concurrent_positions]
        print(f"\n=== Portfolio sim | leader_pullback bull leg | start ${args.capital:,.0f} | {ds['names']} names | {bps}bps | {args.start} -> {args.end} ===")
        print(f"per-position cap {base.max_alloc_pct}% · risk {base.risk_pct}% · trail {base.atr_stop_mult}xATR · max-hold {args.max_hold}d")
        print(f"\n{'maxPos':>7}{'finalEq':>12}{'return%':>9}{'maxDD%':>8}{'Sharpe':>8}{'trades':>8}{'win%':>7}{'expR':>8}")
        for kpos in counts:
            r = run_portfolio(ds, base, kpos, args.start, args.end, args.max_hold, bps, "leader_pullback",
                              regime_match="bull")
            print(f"{r['max_positions']:>7}{r['final_equity']:>12,.0f}{r['return_pct']:>+9.1f}{r['max_drawdown_pct']:>8.1f}"
                  f"{r['sharpe']:>8.2f}{r['trades']:>8}{r['win_rate']:>7}{r['expectancy_r']:>+8.3f}")
        print("\n(Reconciles with the per-trade engine: candidates + their exits/R come straight from it, so")
        print(" unconstrained it converges to the per-trade expR. Breadth effect (Fundamental Law): per-trade")
        print(" edge falls with more positions but total/risk-adjusted return rise then plateau ~4-5.")
        print(" CAVEAT: survivorship-biased universe, idealized fills, LOW trade count (bull-gated) — a")
        print(" directional read, not proof. Validate forward in paper.)")
        return

    if args.trail_sweep:
        rows = sweep_trail(ds, args.split, args.capital, bps, max_hold=args.max_hold)
        print(f"\n=== Trail-distance sweep | leader_pullback bull leg | {ds['names']} names | {bps}bps | {args.start} -> {args.end} ===")
        print("(initial stop fixed at 1.5xATR; ONLY the trail distance varies)")
        print(f"\n{'trailxATR':<10}{'trades':>7}{'win%':>7}{'expR':>9}{'PF':>7}{'maxDD':>8}{'totR':>9}", end="")
        print(f"{'  teExpR':>9}{'tePF':>7}{'teDD':>7}" if args.split else "")
        for m, s, tr, te in rows:
            line = (f"{m:<10}{s['trades']:>7}{s['win_rate']:>7}{s['expectancy_r']:>+9.3f}"
                    f"{s['profit_factor']:>7}{s['max_drawdown_r']:>8.1f}{s['total_r']:>+9.1f}")
            if args.split and te.get("trades"):
                line += f"{te['expectancy_r']:>+9.3f}{te['profit_factor']:>7}{te['max_drawdown_r']:>7.1f}"
            print(line)
        print("\nStable across the range = robust (not a knife-edge on the distance). (Survivorship-biased + idealized.)")
        return

    if args.exit_compare:
        rows = compare_exits(ds, args.split, args.capital, bps, max_hold=args.max_hold)
        print(f"\n=== Exit-management compare | leader_pullback bull leg ({DEFAULT_ROUTER['bull'][1]}) ===")
        _trail = ScanSettings(**DEFAULT_ROUTER['bull'][1]).atr_stop_mult
        print(f"{ds['names']} names | {bps}bps slippage | {args.start} -> {args.end} | trail = {_trail}xATR (= the initial stop's multiple)")
        print(f"\n{'mode':<12}{'trades':>7}{'win%':>7}{'expR':>9}{'PF':>7}{'maxDD':>8}{'totR':>9}  exits")
        for mode, s, tr, te in rows:
            ex = s.get("exits", {})
            exits = f"tgt {ex.get('target',0)} | stop {ex.get('stop',0)} | trail {ex.get('trail',0)} | be {ex.get('breakeven',0)} | time {ex.get('time',0)}"
            print(f"{mode:<12}{s['trades']:>7}{s['win_rate']:>7}{s['expectancy_r']:>+9.3f}"
                  f"{s['profit_factor']:>7}{s['max_drawdown_r']:>8.1f}{s['total_r']:>+9.1f}  {exits}")
        if args.split:
            print(f"\n--- Out-of-sample @ {args.split} (train < split, test >=) ---")
            print(f"{'mode':<12}{'trN':>6}{'trainExpR':>11}{'teN':>6}{'testExpR':>10}{'testPF':>8}{'testDD':>8}")
            for mode, s, tr, te in rows:
                trx = f"{tr['expectancy_r']:+.3f}" if tr.get("trades") else "—"
                tex = f"{te['expectancy_r']:+.3f}" if te.get("trades") else "—"
                tepf = te.get("profit_factor", "—") if te.get("trades") else "—"
                tedd = f"{te['max_drawdown_r']:.1f}" if te.get("trades") else "—"
                print(f"{mode:<12}{tr.get('trades',0):>6}{trx:>11}{te.get('trades',0):>6}{tex:>10}{str(tepf):>8}{tedd:>8}")
        print("\nRead: does breakeven/trail beat fixed on TEST expR AND drawdown? Trail removes the target")
        print("(lets winners run) so watch whether it trades fewer-but-bigger. (Survivorship-biased + idealized.)")
        return

    if args.dual_momentum:
        dm = run_dual_momentum(ds, ScanSettings(capital=args.capital), bps)
        print(f"\n=== Dual-momentum rotation | top {ScanSettings().rotation_top_n} of {ds['names']} names | "
              f"{ScanSettings().momentum_lookback}d lookback | {bps}bps | {args.start} -> {args.end} ===")
        print(f"avg names held: {dm['avg_held']} (cash fills the rest when fewer qualify)")
        def _dm_line(label, m):
            st = _rotation_stats(m)
            if not st.get("months"):
                print(f"{label:<8} (no months)"); return
            print(f"{label:<8} months {st['months']:>3} | CAGR {st['cagr']:>6}% | Sharpe {st['sharpe']:>5} | "
                  f"maxDD {st['max_drawdown_pct']:>5}% | monthly win {st['monthly_winrate']:>5}% | total {st['total_return']:+}%")
        if args.split:
            tr, te = _split_monthly(dm["monthly"], args.split)
            _dm_line("train", tr); _dm_line("TEST", te)
        else:
            _dm_line("all", dm["monthly"])
        print("\n(Hypothesis only — survivorship-biased universe, idealized fills. Validate forward in paper.)")
        return

    if args.sleeves:
        sleeves = run_sleeves(ds, args.capital, bps, args.max_hold)
        dm = run_dual_momentum(ds, ScanSettings(capital=args.capital), bps)
        order = ["router", "leader_pullback", "mean_reversion", "breakout"]
        print(f"\n=== Sleeve comparison | {ds['names']} names | {bps}bps slippage | {args.start} -> {args.end} ===")
        if args.split:
            print(f"Out-of-sample @ {args.split} (train < split, test >=)\n")
            print(f"{'sleeve':<18}{'trN':>6}{'trainExpR':>11}{'teN':>6}{'testExpR':>10}{'testPF':>8}{'testDD':>8}{'testTotR':>9}")
            for name in order:
                tr, te = _split_stats(sleeves[name], args.split)
                trx = f"{tr['expectancy_r']:+.3f}" if tr.get("trades") else "—"
                tex = f"{te['expectancy_r']:+.3f}" if te.get("trades") else "—"
                tepf = te.get("profit_factor", "—") if te.get("trades") else "—"
                tedd = f"{te['max_drawdown_r']:.1f}" if te.get("trades") else "—"
                tetot = f"{te['total_r']:+.1f}" if te.get("trades") else "—"
                print(f"{name:<18}{tr.get('trades',0):>6}{trx:>11}{te.get('trades',0):>6}{tex:>10}{str(tepf):>8}{tedd:>8}{tetot:>9}")
        else:
            print(f"{'sleeve':<18}{'trades':>7}{'win%':>7}{'expR':>8}{'PF':>6}{'maxDD':>7}{'totR':>8}")
            for name in order:
                s = _stats(sleeves[name])
                if not s.get("trades"):
                    print(f"{name:<18}{'0':>7}"); continue
                print(f"{name:<18}{s['trades']:>7}{s['win_rate']:>7}{s['expectancy_r']:>+8.3f}{s['profit_factor']:>6}{s['max_drawdown_r']:>7}{s['total_r']:>+8.1f}")
        # dual-momentum is a rotation (no per-trade R), so report its own metrics
        print(f"\ndual_momentum (rotation, top {ScanSettings().rotation_top_n}, {ScanSettings().momentum_lookback}d, avg held {dm['avg_held']}):")
        def _dm_line(label, m):
            st = _rotation_stats(m)
            if not st.get("months"):
                print(f"  {label:<6} (no months)"); return
            print(f"  {label:<6} CAGR {st['cagr']:>6}% | Sharpe {st['sharpe']:>5} | maxDD {st['max_drawdown_pct']:>5}% | monthly win {st['monthly_winrate']:>5}%")
        if args.split:
            tr, te = _split_monthly(dm["monthly"], args.split)
            _dm_line("train", tr); _dm_line("TEST", te)
        else:
            _dm_line("all", dm["monthly"])
        # monthly-return correlation across ALL sleeves (rotation vs trade-sleeves: scale-invariant)
        corr_order = order + ["dual_momentum"]
        mr = {name: _monthly_r(sleeves[name]) for name in order}
        mr["dual_momentum"] = dm["monthly"]
        months = sorted(set().union(*[set(d) for d in mr.values()]))
        corr = pd.DataFrame({name: [mr[name].get(m, 0.0) for m in months] for name in corr_order}, index=months).corr()
        print("\nMonthly-return correlation (lower vs router = more diversifying):")
        print(f"{'':<16}" + "".join(f"{n[:9]:>11}" for n in corr_order))
        for n in corr_order:
            print(f"{n:<16}" + "".join(f"{corr.loc[n, m]:>11.2f}" for m in corr_order))
        print("\nKeep a sleeve if it BEATS the router on test risk-adjusted return, OR is comparable but lowly")
        print("correlated (adds diversification). Survivorship-biased + idealized fills — paper-prove the survivors.")
        return

    if args.router:
        if ds.get("regime") is None:
            print("Regime classifier unavailable (SPY download failed)."); return

        def _line(label, st):
            if not st.get("trades"):
                return f"{label:<34}{'0':>6}"
            return (f"{label:<34}{st['trades']:>6}{st['win_rate']:>7}{st['expectancy_r']:>+9.3f}"
                    f"{st['profit_factor']:>7}{st['total_r']:>+9.1f}{st['max_drawdown_r']:>8.1f}")

        counts = _regime_day_counts(ds["regime"], args.start, args.split)
        router = run_router(ds, DEFAULT_ROUTER, args.capital, args.max_hold, bps)
        # Standalone baselines (no regime gate) over the same window, for contrast.
        leader = run_on_dataset(ds, ScanSettings(capital=args.capital, **DEFAULT_ROUTER["bull"][1]),
                                args.max_hold, "leader_pullback", bps)
        meanrev = run_on_dataset(ds, ScanSettings(capital=args.capital, **DEFAULT_ROUTER["chop"][1]),
                                 args.max_hold, "mean_reversion", bps)

        print(f"\n=== Regime router | {ds['names']} names | {bps}bps slippage | {args.start} -> {args.end} ===")
        print("Policy: bull->leader_pullback  chop->mean_reversion  bear->CASH")
        print(f"Regime days: {counts.get('all', {})}")
        print(f"\n{'':<34}{'trades':>6}{'win%':>7}{'expR':>9}{'PF':>7}{'totR':>9}{'maxDD':>8}")
        print(_line("ROUTER (blended)", router["stats"]))
        for reg in ("bull", "chop", "bear"):
            if reg in router["legs"]:
                lg = router["legs"][reg]
                print(_line(f"  └ {reg} leg ({lg['strategy']})", lg["stats"]))
        print(_line("leader_pullback ALONE (all regimes)", leader["stats"]))
        print(_line("mean_reversion ALONE (all regimes)", meanrev["stats"]))

        if args.split:
            print(f"\n--- Out-of-sample @ {args.split} (train < split, test >=) ---")
            print(f"Train regime days: {counts.get('train', {})}")
            print(f"Test  regime days: {counts.get('test', {})}")
            print(f"\n{'':<34}{'trN':>6}{'trainExpR':>10}{'teN':>6}{'testExpR':>10}{'testPF':>8}{'testDD':>8}")

            def _oos_line(label, trades):
                tr, te = _split_stats(trades, args.split)
                trx = f"{tr['expectancy_r']:+.3f}" if tr.get("trades") else "—"
                tex = f"{te['expectancy_r']:+.3f}" if te.get("trades") else "—"
                tepf = te.get("profit_factor", "—") if te.get("trades") else "—"
                tedd = f"{te['max_drawdown_r']:.1f}" if te.get("trades") else "—"
                print(f"{label:<34}{tr.get('trades', 0):>6}{trx:>10}{te.get('trades', 0):>6}{tex:>10}{str(tepf):>8}{tedd:>8}")

            _oos_line("ROUTER (blended)", router["trades"])
            _oos_line("leader_pullback ALONE", leader["trades"])
            _oos_line("mean_reversion ALONE", meanrev["trades"])
            print("\nThe question: does the router avoid the 2022 bleed that sinks each strategy run 24/7?")

        st = router["stats"]
        md = ["| leg | trades | win% | expR | PF | totR |", "| --- | --- | --- | --- | --- | --- |"]
        md.append(f"| ROUTER (blended) | {st.get('trades',0)} | {st.get('win_rate','—')} | "
                  f"{st.get('expectancy_r','—')} | {st.get('profit_factor','—')} | {st.get('total_r','—')} |")
        for reg in ("bull", "chop", "bear"):
            if reg in router["legs"]:
                s2 = router["legs"][reg]["stats"]
                md.append(f"| {reg} ({router['legs'][reg]['strategy']}) | {s2.get('trades',0)} | {s2.get('win_rate','—')} | "
                          f"{s2.get('expectancy_r','—')} | {s2.get('profit_factor','—')} | {s2.get('total_r','—')} |")
        write_strategy_md(
            f"Regime router | {ds['names']} names | {bps}bps | {args.start} → {args.end}",
            "\n".join(md),
            f"Policy: bull→leader_pullback, chop→mean_reversion, bear→cash. Regime days: {counts.get('all', {})}. "
            "Survivorship-biased + idealized fills — validate forward in paper.",
        )
        print(f"\n(written to {STRATEGY_MD.name})")
        print("(Hypothesis only — survivorship-biased universe, idealized fills. Validate forward in paper.)")
        return

    if args.compare and args.split:
        rows = compare_oos(ds, args.split, cands, args.capital, args.strategy, bps)
        print(f"\n=== Train/Test @ {args.split} | {args.strategy} | {ds['names']} names | {bps}bps slippage | {args.start} -> {args.end} ===")
        print(f"{'variation':<27}{'trN':>6}{'trainExpR':>10}{'teN':>6}{'testExpR':>10}{'testPF':>8}")
        for name, tr, te in rows:
            trx = f"{tr['expectancy_r']:+.3f}" if tr.get("trades") else "—"
            tex = f"{te['expectancy_r']:+.3f}" if te.get("trades") else "—"
            tepf = te.get("profit_factor", "—") if te.get("trades") else "—"
            print(f"{name:<27}{tr.get('trades', 0):>6}{trx:>10}{te.get('trades', 0):>6}{tex:>10}{str(tepf):>8}")
        print("\nRobust = positive on BOTH train and test. Strong train + weak/negative test = overfit.")
        print("(Still survivorship-biased + idealized fills — a holding-up test is necessary, not sufficient.)")
        md = ["| variation | trN | train expR | teN | test expR | test PF |", "| --- | --- | --- | --- | --- | --- |"]
        for name, tr, te in rows:
            md.append(f"| {name} | {tr.get('trades', 0)} | {tr.get('expectancy_r', '—')} | "
                      f"{te.get('trades', 0)} | {te.get('expectancy_r', '—')} | {te.get('profit_factor', '—')} |")
        write_strategy_md(
            f"Train/Test @ {args.split} | {ds['names']} names | {args.start} → {args.end}",
            "\n".join(md),
            "Robust = positive on BOTH train and test. Survivorship-biased + idealized fills — validate forward in paper.",
        )
        print(f"(written to {STRATEGY_MD.name})")
        return

    if args.compare:
        rows = compare(ds, cands, args.capital, args.strategy, bps)
        print(f"\n=== Variation sweep | {args.strategy} | {ds['names']} names | {bps}bps slippage | {args.start} -> {args.end} ===")
        print(f"{'variation':<27}{'trades':>7}{'win%':>7}{'expR':>8}{'PF':>6}{'maxDD':>7}{'totR':>8}")
        for name, s in rows:
            if not s.get("trades"):
                print(f"{name:<27}{'0':>7}"); continue
            print(f"{name:<27}{s['trades']:>7}{s['win_rate']:>7}{s['expectancy_r']:>+8.3f}"
                  f"{s['profit_factor']:>6}{s['max_drawdown_r']:>7}{s['total_r']:>+8.1f}")
        print("\n(Hypothesis only — survivorship-biased universe, idealized fills. Validate forward in paper.)")
        md = ["| variation | trades | win% | expR | PF | maxDD | totR |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for name, s in rows:
            if s.get("trades"):
                md.append(f"| {name} | {s['trades']} | {s['win_rate']} | {s['expectancy_r']} | "
                          f"{s['profit_factor']} | {s['max_drawdown_r']} | {s['total_r']} |")
        write_strategy_md(f"Variation sweep | {ds['names']} names | {args.start} → {args.end}", "\n".join(md),
                          "Hypothesis only — survivorship-biased + idealized fills. Validate forward in paper.")
        print(f"(written to {STRATEGY_MD.name})")
        return

    # single-variation run
    params: dict = {}
    try:
        from . import strategy
        v = strategy.list_variations().get(args.variation) if args.variation else strategy.get_active()
        if v:
            params = v["params"]; print(f"Variation {v['id']} ({v['name']})")
    except Exception:
        pass
    settings = ScanSettings(capital=args.capital, **params)
    res = run_on_dataset(ds, settings, args.max_hold, args.strategy, bps, exit_mode=args.exit_mode)
    s = res["stats"]
    print(f"\n=== Backtest {args.start} -> {args.end} | {ds['names']} names | max-hold {args.max_hold}d ===")
    if not s.get("trades"):
        print("No trades generated."); return
    print(f"Trades:        {s['trades']}")
    print(f"Win rate:      {s['win_rate']}%")
    print(f"Expectancy:    {s['expectancy_r']:+}R per trade")
    print(f"Profit factor: {s['profit_factor']}")
    print(f"Total:         {s['total_r']:+}R   max drawdown: {s['max_drawdown_r']}R")
    print(f"Avg win/loss:  {s['avg_win_r']:+}R / {s['avg_loss_r']:+}R   avg hold {s['avg_hold_days']}d")
    ex = s["exits"]
    print(f"Exits:         target {ex['target']} | reversion {ex['reversion']} | stop {ex['stop']} | "
          f"trail {ex['trail']} | breakeven {ex['breakeven']} | time {ex['time']}")
    if args.csv:
        export_trades_csv(res["trades"], args.csv)
        print(f"(wrote {len(res['trades'])} trades to {args.csv})")
    print("\n(Hypothesis only — survivorship-biased universe, idealized fills. Validate forward in paper.)")


if __name__ == "__main__":
    main()
