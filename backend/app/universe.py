"""Scan universe — either the full US market (fetched live) or the curated list.

"full" pulls the entire NASDAQ/NYSE/AMEX common-stock universe from the NASDAQ
Trader symbol directory (the canonical free source), filters out ETFs, warrants,
rights, units, preferreds and test issues, and caches the result. The scanner's
own liquidity/price/trend filters then prune the thousands of micro-caps, so a
broad universe just means nothing good gets missed.

"curated" uses tickers.txt (S&P 500 + hand-picked movers) — fast, offline, and
the fallback when the live fetch fails.
"""

import datetime as dt
import json
import logging
import re
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent
CURATED_PATH = APP_DIR / "tickers.txt"
CACHE_PATH = APP_DIR / "universe_cache.txt"
NAMES_CACHE_PATH = APP_DIR / "names_cache.json"
CACHE_MAX_AGE_DAYS = 7

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# Drop derivative/non-common securities by name (word-boundary so "Bright",
# "United", "Granite" etc. are safe).
_EXCLUDE = re.compile(
    r"\b(warrants?|rights?|units?|preferred|debentures?|notes?|when[- ]?issued)\b",
    re.I,
)
# Yahoo-style symbol: up to 5 letters, optional single class suffix (BRK-B).
_SYMBOL_OK = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")


def _clean_symbol(sym: str) -> str | None:
    sym = sym.strip().upper().replace(".", "-")  # BRK.B -> BRK-B (Yahoo format)
    return sym if _SYMBOL_OK.match(sym) else None


def _parse(text: str, sym_idx: int, name_idx: int, etf_idx: int, test_idx: int) -> list[str]:
    out = []
    for line in text.splitlines():
        if not line or line.startswith(("Symbol|", "ACT Symbol|", "File Creation Time")):
            continue
        f = line.split("|")
        if len(f) <= max(sym_idx, name_idx, etf_idx, test_idx):
            continue
        if f[etf_idx].strip() != "N" or f[test_idx].strip() != "N":  # skip ETFs & test issues
            continue
        name = f[name_idx]
        if _EXCLUDE.search(name) or "%" in name:
            continue
        sym = _clean_symbol(f[sym_idx])
        if sym:
            out.append(sym)
    return out


def fetch_full_universe(timeout: float = 20) -> list[str]:
    """Download + filter the full US common-stock universe. Raises on network error."""
    symbols: set[str] = set()
    with httpx.Client(timeout=timeout, headers={"User-Agent": "bellwether/1.0"}) as client:
        r = client.get(NASDAQ_LISTED)
        r.raise_for_status()
        # Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot|ETF|NextShares
        symbols.update(_parse(r.text, sym_idx=0, name_idx=1, etf_idx=6, test_idx=3))

        r = client.get(OTHER_LISTED)
        r.raise_for_status()
        # ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot|Test Issue|NASDAQ Symbol
        symbols.update(_parse(r.text, sym_idx=0, name_idx=1, etf_idx=4, test_idx=6))
    return sorted(symbols)


# Trim the boilerplate the directory appends to security names, e.g.
# "Apple Inc. - Common Stock" / "Alcoa Corporation Common Stock" -> the company.
_NAME_CUT = re.compile(
    r"\s+(-\s|common stock|common shares|ordinary shares|class\s+[a-z]\b|"
    r"american depositary|depositary shar|warrant|right|unit\b|preferred).*$",
    re.I,
)


def _clean_name(name: str) -> str:
    return _NAME_CUT.sub("", name.strip()).strip().rstrip(",").strip()


def _parse_names(text: str, sym_idx: int, name_idx: int, etf_idx: int, test_idx: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith(("Symbol|", "ACT Symbol|", "File Creation Time")):
            continue
        f = line.split("|")
        if len(f) <= max(sym_idx, name_idx, etf_idx, test_idx):
            continue
        if f[etf_idx].strip() != "N" or f[test_idx].strip() != "N":
            continue
        sym = _clean_symbol(f[sym_idx])
        if sym:
            cleaned = _clean_name(f[name_idx])
            if cleaned:
                out[sym] = cleaned
    return out


def _fetch_names(timeout: float = 20) -> dict[str, str]:
    names: dict[str, str] = {}
    with httpx.Client(timeout=timeout, headers={"User-Agent": "bellwether/1.0"}) as client:
        r = client.get(NASDAQ_LISTED)
        r.raise_for_status()
        names.update(_parse_names(r.text, sym_idx=0, name_idx=1, etf_idx=6, test_idx=3))
        r = client.get(OTHER_LISTED)
        r.raise_for_status()
        names.update(_parse_names(r.text, sym_idx=0, name_idx=1, etf_idx=4, test_idx=6))
    return names


def _names_cache_fresh() -> bool:
    if not NAMES_CACHE_PATH.exists():
        return False
    age = dt.date.today() - dt.date.fromtimestamp(NAMES_CACHE_PATH.stat().st_mtime)
    return age.days < CACHE_MAX_AGE_DAYS


def company_names() -> dict[str, str]:
    """Map of ticker -> clean company name, cached 7 days. {} if unavailable offline."""
    if _names_cache_fresh():
        try:
            return json.loads(NAMES_CACHE_PATH.read_text())
        except Exception:
            log.exception("Reading names cache failed; refetching")
    try:
        names = _fetch_names()
        if len(names) > 1000:
            NAMES_CACHE_PATH.write_text(json.dumps(names))
            return names
    except Exception:
        log.exception("Company-name fetch failed; falling back")
    if NAMES_CACHE_PATH.exists():
        try:
            return json.loads(NAMES_CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


QUOTE_URL = "https://query2.finance.yahoo.com/v7/finance/quote"


def bulk_quote(symbols: list[str], batch: int = 200) -> dict[str, tuple]:
    """Fetch (price, 3-month avg volume) for many symbols at once via Yahoo's
    quote endpoint. ~200 symbols per request, so the whole market is ~30 requests
    in a few seconds. Returns {} if the endpoint is unavailable (caller falls back
    to downloading full history for everything)."""
    try:
        from yfinance.data import YfData  # handles Yahoo's crumb/cookie auth

        yfd = YfData()
    except Exception:
        log.exception("yfinance session unavailable for bulk quote")
        return {}

    out: dict[str, tuple] = {}
    for i in range(0, len(symbols), batch):
        chunk = symbols[i : i + batch]
        try:
            resp = yfd.get(QUOTE_URL, params={"symbols": ",".join(chunk)})
            for q in resp.json().get("quoteResponse", {}).get("result", []):
                sym = q.get("symbol")
                if sym:
                    # Use the higher of the 3-month and 10-day average volume so a
                    # recent volume surge doesn't get pre-screened out.
                    vol = max(q.get("averageDailyVolume3Month") or 0, q.get("averageDailyVolume10Day") or 0)
                    out[sym] = (q.get("regularMarketPrice"), vol or None)
        except Exception:
            log.exception("Bulk quote batch failed at offset %d", i)
    return out


def _read_list(path: Path) -> list[str]:
    out = []
    for line in path.read_text().splitlines():
        s = line.strip().upper()
        if s and not s.startswith("#"):
            out.append(s)
    return sorted(set(out))


def _cache_fresh() -> bool:
    if not CACHE_PATH.exists():
        return False
    age = dt.date.today() - dt.date.fromtimestamp(CACHE_PATH.stat().st_mtime)
    return age.days < CACHE_MAX_AGE_DAYS


def load_universe(mode: str = "full") -> list[str]:
    """Return the list of tickers to scan. mode: "full" (US market) or "curated"."""
    if mode == "curated":
        return _read_list(CURATED_PATH)

    if _cache_fresh():
        try:
            return _read_list(CACHE_PATH)
        except Exception:
            log.exception("Reading universe cache failed; refetching")

    try:
        symbols = fetch_full_universe()
        if len(symbols) > 1000:  # sanity check the fetch actually worked
            header = f"# full US common-stock universe, generated {dt.date.today().isoformat()}\n"
            CACHE_PATH.write_text(header + "\n".join(symbols) + "\n")
            return symbols
        log.warning("Full-universe fetch returned only %d symbols; falling back", len(symbols))
    except Exception:
        log.exception("Full-universe fetch failed; falling back to cache/curated list")

    # Fallbacks: a stale cache beats nothing, and the curated list always works offline.
    if CACHE_PATH.exists():
        return _read_list(CACHE_PATH)
    return _read_list(CURATED_PATH)
