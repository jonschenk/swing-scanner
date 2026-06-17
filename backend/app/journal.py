"""Trade journal — the feedback signal for strategy iteration.

Every trade is logged with the strategy variation that produced it, the plan at
entry, and (on close) the outcome. Analytics roll up per variation so you can see
which one actually wins — with an explicit small-sample flag, because a winrate
over a handful of trades is noise, not signal. Trades you *pass* on can be logged
too (status "passed"), so the advisor's Take/Wait/Pass calls can be graded, not
just the trades you took.

Outcome math is R-multiples: risk-per-share = entry - stop, and
R = (exit - entry) / risk-per-share. Expectancy = average R across closed trades
— the single number that says whether a variation makes money over many trades.
"""

import datetime as dt
import json
import logging
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

STORE_PATH = Path(__file__).resolve().parents[1] / "journal.json"
STRATEGY_DOC = Path(__file__).resolve().parents[1] / "STRATEGY.md"

# Below this many closed trades, a variation's winrate isn't trustworthy yet.
MIN_SAMPLE = 20


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _load() -> list[dict]:
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.exception("journal.json unreadable; starting fresh")
    return []


def _save(trades: list[dict]) -> None:
    STORE_PATH.write_text(json.dumps(trades, indent=2))


# Indicators snapshotted at entry, so outcomes can be sliced by the entry conditions later
# ("we win on high-ADX names but bleed in chop"). You can only learn along what you logged.
ENTRY_SNAPSHOT_FIELDS = [
    "price", "rsi", "adx", "atr_pct", "rs_rating", "pct_from_high",
    "rel_volume", "pct_above_sma50", "sma20", "sma50", "sma200", "setup_score",
]
# Trade-case fields kept at entry, so the advisor's calls can be graded against outcomes.
_TRADE_CASE_KEEP = ["recommendation", "conviction", "thesis", "key_risks", "bottom_line"]


def _entry_snapshot(stock: dict) -> dict:
    return {k: stock.get(k) for k in ENTRY_SNAPSHOT_FIELDS}


def _trade_case_snapshot(stock: dict) -> dict | None:
    tc = stock.get("trade_case")
    if not tc or tc.get("error"):
        return None
    return {k: tc.get(k) for k in _TRADE_CASE_KEEP}


def log_trade(
    stock: dict,
    variation_id: str,
    variation_params: dict | None = None,
    decision: str | None = None,
    notes: str = "",
    market_regime: str | None = None,
) -> dict:
    """Record an opened trade with a rich entry snapshot, so it can be evolved against later.
    `stock` is a full scanner result row (indicators + plan + optional trade_case)."""
    plan = stock.get("plan", {})
    tc = _trade_case_snapshot(stock)
    trades = _load()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "ticker": stock["ticker"],
        "name": stock.get("name", ""),
        "variation_id": variation_id,
        "variation_params": variation_params,  # the strategy knobs in effect at entry
        "decision": decision or (tc.get("recommendation") if tc else None),
        "status": "open",
        "opened_at": _now(),
        # plan at entry
        "entry": plan.get("entry"),
        "stop": plan.get("stop"),
        "target": plan.get("target"),
        "shares": plan.get("shares"),
        "risk_dollars": plan.get("risk_dollars"),
        "reward_risk": plan.get("reward_risk"),
        # rich entry context (the part that makes evolution possible)
        "entry_snapshot": _entry_snapshot(stock),  # indicators at entry
        "trade_case": tc,  # the advisor's call at entry, for grading
        "market_regime": market_regime,  # SPY/QQQ trend; filled by the logging flow later
        # exit detail (filled by close_trade)
        "closed_at": None,
        "exit": None,
        "exit_reason": None,
        "hold_days": None,
        "pnl": None,
        "r_multiple": None,
        "mae": None,  # max adverse excursion (worst point while held)
        "mfe": None,  # max favorable excursion (best point while held)
        "outcome": None,
        "notes": notes,
    }
    trades.append(entry)
    _save(trades)
    return entry


def log_pass(stock_or_ticker, variation_id: str, decision: str | None = None, notes: str = "") -> dict:
    """Record a setup you passed on (with its advisor call, if any), so the advisor's
    Take/Wait/Pass calls can be graded later. Accepts a full result row or a bare ticker."""
    if isinstance(stock_or_ticker, str):
        ticker, tc = stock_or_ticker, None
    else:
        ticker, tc = stock_or_ticker["ticker"], _trade_case_snapshot(stock_or_ticker)
    trades = _load()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "ticker": ticker,
        "variation_id": variation_id,
        "decision": decision or (tc.get("recommendation") if tc else "Pass"),
        "status": "passed",
        "opened_at": _now(),
        "trade_case": tc,
        "notes": notes,
    }
    trades.append(entry)
    _save(trades)
    return entry


def close_trade(
    trade_id: str,
    exit_price: float,
    exit_reason: str | None = None,
    mae: float | None = None,
    mfe: float | None = None,
    notes: str = "",
) -> dict:
    """Close an open trade and compute P&L, R-multiple, hold time, and win/loss/scratch.
    `exit_reason` (target / stop / time / manual / trailed) and MAE/MFE are worth capturing —
    WHY you exited and how far it ran both feed strategy tuning."""
    trades = _load()
    t = next((x for x in trades if x["id"] == trade_id), None)
    if t is None:
        raise KeyError(f"No such trade: {trade_id}")
    if t["status"] != "open":
        raise ValueError(f"Trade {trade_id} is not open (status: {t['status']})")

    entry, stop, shares = t["entry"], t["stop"], t["shares"] or 0
    risk_per_share = (entry - stop) if (entry is not None and stop is not None) else None
    t["status"] = "closed"
    t["closed_at"] = _now()
    t["exit"] = exit_price
    t["exit_reason"] = exit_reason
    t["mae"] = mae
    t["mfe"] = mfe
    try:
        opened = dt.datetime.fromisoformat(t["opened_at"])
        t["hold_days"] = round((dt.datetime.fromisoformat(t["closed_at"]) - opened).total_seconds() / 86400, 1)
    except (ValueError, TypeError):
        t["hold_days"] = None
    t["pnl"] = round((exit_price - entry) * shares, 2) if entry is not None else None
    t["r_multiple"] = round((exit_price - entry) / risk_per_share, 2) if risk_per_share else None
    if t["pnl"] is None:
        t["outcome"] = None
    elif t["pnl"] > 0:
        t["outcome"] = "win"
    elif t["pnl"] < 0:
        t["outcome"] = "loss"
    else:
        t["outcome"] = "scratch"
    if notes:
        t["notes"] = (t.get("notes", "") + " | " + notes).strip(" |")
    _save(trades)
    return t


def list_trades() -> list[dict]:
    return _load()


def summary_by_variation() -> dict[str, dict]:
    """Per-variation performance over CLOSED trades. The scoreboard for deciding
    which variation to keep evolving."""
    out: dict[str, dict] = {}
    for t in _load():
        if t["status"] != "closed" or t.get("r_multiple") is None:
            continue
        s = out.setdefault(
            t["variation_id"],
            {"trades": 0, "wins": 0, "total_r": 0.0, "total_pnl": 0.0},
        )
        s["trades"] += 1
        s["wins"] += 1 if t["outcome"] == "win" else 0
        s["total_r"] += t["r_multiple"]
        s["total_pnl"] += t.get("pnl") or 0.0

    for vid, s in out.items():
        n = s["trades"]
        s["winrate"] = round(s["wins"] / n * 100, 1) if n else 0.0
        s["expectancy_r"] = round(s["total_r"] / n, 2) if n else 0.0
        s["total_pnl"] = round(s["total_pnl"], 2)
        s["low_sample"] = n < MIN_SAMPLE
    return out


def render_strategy_md(variations: dict[str, dict], active_id: str | None) -> str:
    """Build the human + Claude-readable STRATEGY.md: the variation scoreboard
    plus an iteration-notes section. This is the file the advisor appends to and
    Claude Code reads to propose the next variation."""
    perf = summary_by_variation()
    lines = [
        "# Strategy",
        "",
        "Variations of the swing scan and how each has performed. The active "
        "variation drives current scans; every trade is tagged with the variation "
        "that produced it. Iterate by deriving a new variation, running it, and "
        "comparing the scoreboard over a meaningful sample "
        f"(>= {MIN_SAMPLE} closed trades before trusting a winrate).",
        "",
        "## Scoreboard",
        "",
        "| Variation | Name | Trades | Win% | Expectancy (R) | Net P&L | Active |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for vid, v in sorted(variations.items(), key=lambda kv: int(kv[0][1:])):
        p = perf.get(vid, {})
        n = p.get("trades", 0)
        win = f"{p['winrate']}%" if n else "—"
        exp = f"{p['expectancy_r']:+}" if n else "—"
        pnl = f"${p['total_pnl']:,}" if n else "—"
        flag = " ⚠ low sample" if p.get("low_sample") and n else ""
        active = "★" if vid == active_id else ""
        lines.append(f"| {vid} | {v['name']} | {n}{flag} | {win} | {exp} | {pnl} | {active} |")

    lines += ["", "## Variations", ""]
    for vid, v in sorted(variations.items(), key=lambda kv: int(kv[0][1:])):
        parent = f" (from {v['parent']})" if v.get("parent") else ""
        lines.append(f"### {vid} — {v['name']}{parent}")
        lines.append(f"_created {v['created_at']}_")
        if v.get("notes"):
            lines.append(f"\n{v['notes']}")
        params = ", ".join(f"{k}={val}" for k, val in v["params"].items())
        lines.append(f"\n`{params}`\n")

    lines += [
        "## Iteration notes",
        "",
        "<!-- The advisor appends observations and proposed tweaks here. Claude "
        "Code reads this section in a dev session and turns the good ones into the "
        "next variation via strategy.derive(). Nothing here changes the strategy "
        "automatically — every version bump is a deliberate, reviewed commit. -->",
        "",
    ]
    return "\n".join(lines)


def write_strategy_md(variations: dict[str, dict], active_id: str | None) -> Path:
    STRATEGY_DOC.write_text(render_strategy_md(variations, active_id))
    return STRATEGY_DOC
