"""Daily equity-curve + SPY-benchmark log for the forward paper-proving phase.

Per-trade stats (journal.py) measure the edge PER TRADE. This measures the SYSTEM over time —
the equity curve, max drawdown, and whether it actually beats just holding SPY. One row per ET
trading day, captured after the close, appended to equity_log.json (gitignored). Missing days
can't be backfilled, so this logs from day one of the forward test.
"""

import logging
from pathlib import Path
import json

log = logging.getLogger(__name__)

STORE_PATH = Path(__file__).resolve().parents[1] / "equity_log.json"


def _load() -> list[dict]:
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.exception("equity_log.json unreadable; starting fresh")
    return []


def _save(rows: list[dict]) -> None:
    STORE_PATH.write_text(json.dumps(rows, indent=2))


def rows() -> list[dict]:
    return _load()


def maybe_record_eod() -> None:
    """Append one snapshot per ET trading day, after ~16:00 ET (post-close). Self-dedups per date,
    so it's safe to call every loop tick. Captures paper equity + the day's regime + SPY level —
    enough to plot the forward equity curve, compute drawdown, and benchmark against buy-and-hold SPY."""
    try:
        from . import alert_engine, paper, regime
        now_et = alert_engine._now_et()
        if now_et.weekday() >= 5 or now_et.hour < 16:
            return  # weekdays, post-close only
        today = now_et.date().isoformat()
        data = _load()
        if data and data[-1].get("date") == today:
            return  # already logged today
        acct = paper.account()
        reg = regime.current_regime()
        avail = reg.get("available")
        data.append({
            "date": today,
            "equity": acct.get("equity"),
            "cash": acct.get("cash"),
            "open_pnl": acct.get("open_pnl"),
            "realized_pnl": acct.get("realized_pnl"),
            "open_positions": len(acct.get("positions", [])),
            "regime": reg.get("regime") if avail else None,        # the day's router regime
            "spy": reg.get("spy_price") if avail else None,        # benchmark: SPY close-ish level
            "logged_at": now_et.isoformat(timespec="seconds"),
        })
        _save(data)
        log.info("equity snapshot %s: $%s (%d open)", today, acct.get("equity"), len(acct.get("positions", [])))
    except Exception:
        log.exception("equity_log snapshot failed")
