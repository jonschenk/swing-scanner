"""Approve/deny review queue — phase 4 of the semi-auto vision.

The app PROPOSES fully-specified trade tickets (symbol / shares / entry / stop / target, tagged
with the regime and strategy they came from) and surfaces them for review. The human pulls the
trigger: Approve -> a paper buy opens the position (the existing safe plumbing); Deny -> the pass
is logged with the advisor's call so those calls can be graded later. This is the seed of the
alert engine (which will later auto-fill this queue) and the approve-before-execute workflow.

HARD BOUNDARY (unchanged): the app sets trades up and makes them executable; nothing is opened
until the human clicks Approve. No auto-approve. Paper only for now — going live is a broker swap
(phase 5), and the human click stays load-bearing.

Proposals persist to proposals.json (gitignored) so the queue survives a backend restart.
"""

import datetime as dt
import json
import logging
import uuid
from pathlib import Path

from . import journal, paper, strategy

log = logging.getLogger(__name__)

STORE_PATH = Path(__file__).resolve().parents[1] / "proposals.json"
DEFAULT_TOP_N = 8


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _load() -> list[dict]:
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.exception("proposals.json unreadable; starting fresh")
    return []


def _save(proposals: list[dict]) -> None:
    STORE_PATH.write_text(json.dumps(proposals, indent=2))


def _pending(proposals: list[dict]) -> list[dict]:
    return [p for p in proposals if p["status"] == "pending"]


def _summarize(p: dict) -> dict:
    """The compact ticket the UI renders (without the heavy snapshotted stock row)."""
    return {k: p[k] for k in (
        "id", "ticker", "name", "strategy", "regime", "score", "call", "reason",
        "conviction", "plan", "status", "created_at", "decided_at",
    )}


def view() -> dict:
    """Pending tickets (newest scan first) + the recently decided ones, for the panel."""
    proposals = _load()
    pending = [_summarize(p) for p in proposals if p["status"] == "pending"]
    decided = [_summarize(p) for p in proposals if p["status"] != "pending"]
    decided.sort(key=lambda p: p.get("decided_at") or "", reverse=True)
    return {"pending": pending, "decided": decided[:20]}


def build(results: list[dict], regime: str | None, scan_strategy: str,
          top_n: int = DEFAULT_TOP_N, exclude: set[str] | None = None) -> dict:
    """Populate the queue from the current scan's best setups. Recommended picks (if a
    'Recommend top picks' pass was run) lead, by rank; otherwise the top setup scores. Tickers
    already pending in the queue or already held as a paper position are skipped (no dupes);
    `exclude` skips more (the alert engine passes today's already-alerted names). Returns the
    view plus `added` and the tickers added (so the alert engine can record them)."""
    proposals = _load()
    already = {p["ticker"] for p in proposals if p["status"] == "pending"} | (exclude or set())
    held = {pos["ticker"] for pos in paper.account().get("positions", [])}

    # Rank: recommended rows first (by their rank), then by setup score.
    ranked = sorted(
        results,
        key=lambda r: (r.get("recommendation") is None, r.get("recommendation", {}).get("rank", 1e9), -r.get("setup_score", 0)),
    )

    added_tickers: list[str] = []
    for r in ranked:
        if len(added_tickers) >= top_n:
            break
        ticker = r["ticker"]
        if ticker in already or ticker in held:
            continue
        if not (r.get("plan") or {}).get("shares"):
            continue
        rec = r.get("recommendation") or {}
        proposals.append({
            "id": uuid.uuid4().hex[:8],
            "ticker": ticker,
            "name": r.get("name", ""),
            "strategy": r.get("strategy", scan_strategy),
            "regime": regime,
            "score": r.get("setup_score"),
            "call": rec.get("call"),                # Claude's Take/Wait/Pass, if recommended
            "reason": rec.get("reason"),            # Claude's one-liner, if recommended
            "conviction": rec.get("conviction"),
            "plan": r.get("plan"),
            "stock": r,                             # full snapshot so Approve can paper-buy / journal it
            "status": "pending",
            "created_at": _now(),
            "decided_at": None,
        })
        already.add(ticker)
        added_tickers.append(ticker)

    _save(proposals)
    return {**view(), "added": len(added_tickers), "added_tickers": added_tickers}


def approve(proposal_id: str) -> dict:
    """The human pulls the trigger: open a paper position from the proposal's ticket. The buy
    fills at the live price and logs to the journal; the proposal is marked approved."""
    proposals = _load()
    p = next((x for x in proposals if x["id"] == proposal_id and x["status"] == "pending"), None)
    if p is None:
        return {"error": "No such pending proposal."}
    acct = paper.buy(p["stock"])
    if acct.get("error"):
        return {"error": acct["error"]}  # leave it pending so the user can retry
    p["status"] = "approved"
    p["decided_at"] = _now()
    _save(proposals)
    return {**view(), "account": acct}


def deny(proposal_id: str, reason: str = "") -> dict:
    """Pass on a proposal: log it (with the advisor's call) so the call can be graded later."""
    proposals = _load()
    p = next((x for x in proposals if x["id"] == proposal_id and x["status"] == "pending"), None)
    if p is None:
        return {"error": "No such pending proposal."}
    try:
        vid = (strategy.get_active() or {}).get("id", "v1")
        journal.log_pass(p["stock"], vid, decision=p.get("call") or "Pass", notes=reason)
    except Exception:
        log.exception("logging the passed trade failed for %s", p["ticker"])
    p["status"] = "denied"
    p["decided_at"] = _now()
    _save(proposals)
    return view()


def clear() -> dict:
    """Drop every still-pending proposal (decided ones stay as history)."""
    proposals = [p for p in _load() if p["status"] != "pending"]
    _save(proposals)
    return view()
