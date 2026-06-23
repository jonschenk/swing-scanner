"""Charles Schwab Trader API integration — OAuth 2.0, encrypted token storage,
automatic token refresh, and portfolio reads (balances, positions, orders).

Setup (one-time, done by the user at https://developer.schwab.com):
  1. Create an app, get the App Key + App Secret.
  2. Set the callback URL to exactly  https://127.0.0.1
  3. Put the credentials in .env:
       SCHWAB_APP_KEY=...
       SCHWAB_APP_SECRET=...
       SCHWAB_REDIRECT_URI=https://127.0.0.1   (optional; this is the default)

Note: Schwab's Trader API serves funded brokerage accounts. ThinkorSwim
paperMoney accounts are generally not exposed through this API.
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import httpx
from cryptography.fernet import Fernet

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent
KEY_PATH = APP_DIR / ".schwab_key"
TOKEN_PATH = APP_DIR / ".schwab_token.enc"

AUTH_BASE = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
API_BASE = "https://api.schwabapi.com/trader/v1"
DEFAULT_REDIRECT = "https://127.0.0.1"

ACCESS_TTL = 1800  # Schwab access tokens last 30 min
REFRESH_SKEW = 120  # refresh this many seconds before expiry


# ---------------------------------------------------------------- config


def app_key() -> str:
    return os.environ.get("SCHWAB_APP_KEY", "")


def app_secret() -> str:
    return os.environ.get("SCHWAB_APP_SECRET", "")


def redirect_uri() -> str:
    return os.environ.get("SCHWAB_REDIRECT_URI", DEFAULT_REDIRECT).rstrip("/")


def is_configured() -> bool:
    return bool(app_key() and app_secret())


# ---------------------------------------------------------------- encrypted token store


def _fernet() -> Fernet:
    if KEY_PATH.exists():
        key = KEY_PATH.read_bytes()
    else:
        key = Fernet.generate_key()
        KEY_PATH.write_bytes(key)
        os.chmod(KEY_PATH, 0o600)
    return Fernet(key)


def _save_tokens(tokens: dict) -> None:
    try:
        blob = _fernet().encrypt(json.dumps(tokens).encode())
        TOKEN_PATH.write_bytes(blob)
        os.chmod(TOKEN_PATH, 0o600)
    except Exception:
        log.exception("Failed to persist Schwab tokens")


def _load_tokens() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        return json.loads(_fernet().decrypt(TOKEN_PATH.read_bytes()).decode())
    except Exception:
        log.exception("Failed to read Schwab tokens (corrupt or key changed)")
        return None


def disconnect() -> None:
    """Forget the stored tokens (user clicked Disconnect)."""
    TOKEN_PATH.unlink(missing_ok=True)


def is_connected() -> bool:
    return _load_tokens() is not None


# ---------------------------------------------------------------- OAuth flow


def authorize_url() -> str:
    params = {
        "client_id": app_key(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
    }
    return f"{AUTH_BASE}?{urlencode(params)}"


def _basic_auth_header() -> dict:
    raw = f"{app_key()}:{app_secret()}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


def _store_token_response(data: dict) -> None:
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": time.time() + data.get("expires_in", ACCESS_TTL),
        "obtained_at": time.time(),
    }
    _save_tokens(tokens)


def exchange_code(callback_url_or_code: str) -> None:
    """Exchange an authorization code (or the full callback URL) for tokens."""
    code = callback_url_or_code
    if "code=" in callback_url_or_code:
        qs = parse_qs(urlparse(callback_url_or_code).query)
        code = qs.get("code", [""])[0]
    if not code:
        raise ValueError("No authorization code found in the callback.")

    resp = httpx.post(
        TOKEN_URL,
        headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
        },
        timeout=30,
    )
    resp.raise_for_status()
    _store_token_response(resp.json())


def _refresh(tokens: dict) -> dict | None:
    try:
        resp = httpx.post(
            TOKEN_URL,
            headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        # Schwab returns a fresh refresh_token sometimes; keep the old one if absent.
        data.setdefault("refresh_token", tokens["refresh_token"])
        _store_token_response(data)
        return _load_tokens()
    except Exception:
        log.exception("Schwab token refresh failed (refresh token may have expired)")
        return None


def _valid_access_token() -> str | None:
    """Return a usable access token, refreshing if needed. None if reconnect required."""
    tokens = _load_tokens()
    if not tokens:
        return None
    if time.time() >= tokens["expires_at"] - REFRESH_SKEW:
        tokens = _refresh(tokens)
        if not tokens:
            return None
    return tokens["access_token"]


# ---------------------------------------------------------------- API client


class SchwabError(Exception):
    pass


def _get(path: str, params: dict | None = None) -> object:
    token = _valid_access_token()
    if not token:
        raise SchwabError("not_connected")
    resp = httpx.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params,
        timeout=30,
    )
    if resp.status_code == 401:
        raise SchwabError("unauthorized")
    resp.raise_for_status()
    return resp.json()


def _account_hashes() -> list[dict]:
    return _get("/accounts/accountNumbers") or []


def _safe(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def _parse_orders(raw_orders: list, limit: int | None = None) -> list[dict]:
    out = []
    for o in raw_orders or []:
        legs = o.get("orderLegCollection") or [{}]
        leg = legs[0]
        instrument = leg.get("instrument", {})
        # execution price: average of activity executions if present, else order price
        price = o.get("price")
        for act in o.get("orderActivityCollection", []) or []:
            execs = act.get("executionLegs", []) or []
            if execs:
                price = execs[0].get("price", price)
        out.append(
            {
                "date": o.get("closeTime") or o.get("enteredTime"),
                "symbol": instrument.get("symbol", "?"),
                "side": leg.get("instruction", "?"),
                "shares": leg.get("quantity") or o.get("filledQuantity"),
                "price": price,
                "status": o.get("status"),
            }
        )
    out.sort(key=lambda r: r["date"] or "", reverse=True)
    return out[:limit] if limit else out


def _entry_dates(orders: list[dict]) -> dict[str, str]:
    """Earliest filled BUY date per symbol — a best-effort 'time in trade'."""
    dates: dict[str, str] = {}
    for o in orders:
        if o["status"] == "FILLED" and (o["side"] or "").upper().startswith("BUY") and o["date"]:
            sym = o["symbol"]
            if sym not in dates or o["date"] < dates[sym]:
                dates[sym] = o["date"]
    return dates


def _days_held(entry_iso: str | None) -> int | None:
    if not entry_iso:
        return None
    try:
        dt = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except Exception:
        return None


def get_portfolio() -> dict:
    """Aggregate balances, positions, and recent orders for the first account."""
    hashes = _account_hashes()
    if not hashes:
        raise SchwabError("no_accounts")
    account_hash = hashes[0]["hashValue"]
    account_number = hashes[0].get("accountNumber", "")

    acct = _get(f"/accounts/{account_hash}", params={"fields": "positions"})
    sec = (acct or {}).get("securitiesAccount", {})
    balances = sec.get("currentBalances", {})

    raw_orders = []
    try:
        # Schwab's /orders endpoint REQUIRES an entered-time range (ISO-8601, ms + Z); maxResults
        # alone returns 400. Look back 90 days, which covers recent fills for entry-date inference.
        now = datetime.now(timezone.utc)
        raw_orders = _get(f"/accounts/{account_hash}/orders", params={
            "maxResults": 100,
            "fromEnteredTime": (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "toEnteredTime": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }) or []
    except Exception:
        log.exception("Failed to fetch Schwab orders")
    orders = _parse_orders(raw_orders)
    entry_dates = _entry_dates(orders)

    positions = []
    day_pl_total = 0.0
    total_pl = 0.0
    for p in sec.get("positions", []) or []:
        instrument = p.get("instrument", {})
        symbol = instrument.get("symbol", "?")
        qty = _safe(p, "longQuantity", default=0) or -(_safe(p, "shortQuantity", default=0) or 0)
        avg = _safe(p, "averagePrice", default=0) or 0
        mkt_val = _safe(p, "marketValue", default=0) or 0
        current = (mkt_val / qty) if qty else 0
        cost = avg * qty
        unreal = _safe(p, "longOpenProfitLoss", "currentDayProfitLoss", default=mkt_val - cost)
        if _safe(p, "longOpenProfitLoss") is None:
            unreal = mkt_val - cost
        day_pl = _safe(p, "currentDayProfitLoss", default=0) or 0
        day_pl_total += day_pl
        total_pl += unreal
        entry_iso = entry_dates.get(symbol)
        positions.append(
            {
                "symbol": symbol,
                "shares": qty,
                "entry": round(avg, 2),
                "current": round(current, 2),
                "market_value": round(mkt_val, 2),
                "pl": round(unreal, 2),
                "pl_pct": round((unreal / cost * 100) if cost else 0, 2),
                "day_pl": round(day_pl, 2),
                "day_pl_pct": round(_safe(p, "currentDayProfitLossPercentage", default=0) or 0, 2),
                "entry_date": entry_iso,
                "days_held": _days_held(entry_iso),
            }
        )
    positions.sort(key=lambda r: r["market_value"], reverse=True)

    value = _safe(balances, "liquidationValue", "equity", default=0) or 0
    day_pl_pct = (day_pl_total / (value - day_pl_total) * 100) if (value - day_pl_total) else 0
    total_cost = value - total_pl
    return {
        "account_mask": f"…{account_number[-4:]}" if account_number else "",
        "value": round(value, 2),
        "buying_power": round(_safe(balances, "buyingPower", "cashAvailableForTrading", "availableFunds", default=0) or 0, 2),
        "cash": round(_safe(balances, "cashBalance", "totalCash", default=0) or 0, 2),
        "day_pl": round(day_pl_total, 2),
        "day_pl_pct": round(day_pl_pct, 2),
        "total_pl": round(total_pl, 2),
        "total_pl_pct": round((total_pl / total_cost * 100) if total_cost else 0, 2),
        "positions": positions,
        "orders": [o for o in orders if o["status"] == "FILLED"][:25],
        "as_of": time.time(),
    }


def account_value() -> float | None:
    """Just the live total account value (for the scanner's capital field)."""
    try:
        return get_portfolio()["value"]
    except Exception:
        return None
