"""On-disk cache of raw price bars for the whole universe.

This caches only the expensive part — the downloaded OHLCV history — never which
stocks passed. Every scan re-evaluates all cached tickers from scratch, so the
cache can never cause a qualifying stock to be skipped.

Freshness is bounded by a short window (default 30 min), which also sidesteps
stock-split adjustment drift: a split takes effect at the open, so any cache old
enough to straddle a split (yesterday's) is already past the window and gets a
full re-download.
"""

import json
import logging
import time
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent
CACHE_PATH = APP_DIR / "price_cache.pkl"  # pickle keeps it dependency-free
META_PATH = APP_DIR / "price_cache.json"
COLUMNS = ["High", "Low", "Close", "Volume"]


def _meta() -> dict | None:
    try:
        return json.loads(META_PATH.read_text())
    except Exception:
        return None


def is_fresh(universe: str, max_age_minutes: float) -> bool:
    """True if a cache exists for this universe and is younger than the window."""
    if max_age_minutes <= 0:
        return False
    m = _meta()
    if not m or m.get("universe") != universe or not CACHE_PATH.exists():
        return False
    return (time.time() - m.get("created_at", 0)) / 60 < max_age_minutes


def age_minutes() -> float | None:
    m = _meta()
    return None if not m else (time.time() - m.get("created_at", 0)) / 60


def save(frames: dict[str, pd.DataFrame], universe: str) -> None:
    """Persist every ticker's bars as one pickled long-format frame."""
    if not frames:
        return
    try:
        combined = pd.concat(
            {t: df[COLUMNS] for t, df in frames.items()},
            names=["ticker", "Date"],
        )
        combined.to_pickle(CACHE_PATH)
        META_PATH.write_text(
            json.dumps({"created_at": time.time(), "universe": universe, "tickers": len(frames)})
        )
    except Exception:
        log.exception("Failed to write price cache")


def load() -> dict[str, pd.DataFrame]:
    """Rebuild the per-ticker frames from the cache, or {} if unavailable."""
    if not CACHE_PATH.exists():
        return {}
    try:
        combined = pd.read_pickle(CACHE_PATH)
        return {ticker: group.droplevel(0) for ticker, group in combined.groupby(level=0)}
    except Exception:
        log.exception("Failed to read price cache")
        return {}
