"""Batch "recommend top picks": one Claude pass that triages the scan shortlist
against the user's account + holdings and returns a ranked set of trades to focus on.

This is the comparative, portfolio-aware layer the per-card trade_case can't be: it
sees all the candidates at once and picks the best few *relative to each other and
your existing positions* (e.g. avoid piling into one sector). Cheap by design — one
call over the whole shortlist, Sonnet by default (a ranking is lighter than the full
deep case). The first concrete step of the semi-auto "Claude suggests" goal: it
recommends, you decide and execute.
"""

import json
import logging
import os

from .ai import _anthropic_key
from .config import ScanSettings
from .trade_case import _PRICES  # shared per-model price table for the cost estimate

log = logging.getLogger(__name__)

# The nightly selection (bull/bear debate + ranking) is the highest-consequence autonomous
# call in the loop — it decides what gets queued for the human to approve. That comparative,
# steelman-both-sides reasoning is where Opus genuinely beats Sonnet, and the delta is ~$0.01/call
# (~$0.25/mo). The morning recheck is a narrow gap/R:R gate, so it stays on the cheaper Sonnet.
DEFAULT_RECOMMEND_MODEL = "claude-opus-4-8"
DEFAULT_RECHECK_MODEL = "claude-sonnet-4-6"


def _model() -> str:
    return os.environ.get("RECOMMEND_MODEL", DEFAULT_RECOMMEND_MODEL).strip()


def _recheck_model() -> str:
    return os.environ.get("RECHECK_MODEL", DEFAULT_RECHECK_MODEL).strip()


RECOMMEND_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "description": "The 2-4 setups worth focusing on, best first. Only tickers from the list.",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "rank": {"type": "integer", "description": "1 = top pick"},
                    "bull": {"type": "string", "description": "The strongest one-sentence case FOR taking this trade."},
                    "bear": {"type": "string", "description": "The strongest one-sentence case AGAINST it (what kills the trade)."},
                    "call": {"type": "string", "enum": ["Take", "Watch"]},
                    "reason": {"type": "string", "description": "One sentence: the verdict — why the bull case beats the bear case for THIS account."},
                    "conviction": {"type": "string", "enum": ["High", "Medium", "Low"]},
                },
                "required": ["ticker", "rank", "bull", "bear", "call", "reason", "conviction"],
                "additionalProperties": False,
            },
        },
        "summary": {
            "type": "string",
            "description": "One or two sentences: the overall read on today's list for this account.",
        },
        "skip_note": {
            "type": "string",
            "description": "Brief note on what to skip and why (extended, poor fit, weak, duplicative).",
        },
    },
    "required": ["picks", "summary", "skip_note"],
    "additionalProperties": False,
}


def _candidate_line(s: dict) -> str:
    plan = s.get("plan") or {}
    return (
        f"- {s['ticker']} ({s.get('name', '')}): ${s['price']}, RS {s.get('rs_rating', '?')}/100, "
        f"{s.get('pct_from_high', '?')}% below 52w high, RSI {s['rsi']}, ADX {s['adx']}, "
        f"ATR% {s['atr_pct']}, rel-vol {s['rel_volume']}x, setup score {s['setup_score']}, "
        f"plan {plan.get('shares', '?')} sh, {plan.get('reward_risk', '?')}:1 R:R "
        f"(entry ${plan.get('entry', '?')}, stop ${plan.get('stop', '?')}, target ${plan.get('target', '?')})"
    )


def _positions_block(positions: list[dict] | None) -> str:
    if not positions:
        return "(no open positions)"
    out = []
    for p in positions:
        bits = [str(p.get("shares", "?")), p["ticker"]]
        if p.get("sector"):
            bits.append(f"[{p['sector']}]")
        out.append(" ".join(bits))
    return "; ".join(out)


def _build_prompt(candidates: list[dict], settings: ScanSettings, positions: list[dict] | None) -> str:
    lines = "\n".join(_candidate_line(s) for s in candidates)
    n = getattr(settings, "max_concurrent_positions", 4)
    return f"""You are triaging a swing-trade scan for a trader who holds 2-5 days. Every name below already passed the technical screen (market leader, uptrend, healthy pullback). Your job is to pick the few worth focusing on TODAY and say what to skip — judging them against each other AND this specific account.

ACCOUNT: ${settings.capital:,.0f} capital, {settings.risk_pct}% risk per trade. This is a SMALL, SHARED account that can hold about {n} positions at once — every pick consumes cash the next one can't use, so do not treat the full balance as available for each. Propose a focused set that fits (up to ~{n}), prioritized best-first; quality and fit beat quantity.
OPEN POSITIONS: {_positions_block(positions)}

CANDIDATES (already ranked by the scanner's setup score):
{lines}

Pick the best set you'd actually focus on (up to ~{n}), best first. Weigh: cleanliness of the entry, reward:risk, trend/volume quality, AND portfolio fit — don't recommend piling into a sector the account is already heavy in, and prefer names that diversify or have the best standalone setup. Remember the account can only hold ~{n} positions, so be selective.

For EACH pick, argue it like a desk would before committing capital: write the strongest BULL case for the trade, then the strongest BEAR case against it (what would actually make it fail — extended into resistance, thin volume, sector already heavy, fragile pattern), and only then your verdict. A name earns "Take" only when the bull case genuinely survives the bear case; if the bear case has real teeth, call it "Watch" or leave it out. This adversarial step is the point — do not skip the bear case or make it a throwaway. Give each pick a one-sentence reason (the verdict), a Take or Watch call, and a conviction. Then a short overall summary and a note on what to skip and why. Only use tickers from the list. Be selective — if only one or two survive the debate, recommend only those."""


RECHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "proceed": {"type": "boolean", "description": "true = still a valid entry at the open; false = stand down."},
                    "reason": {"type": "string", "description": "One sentence: why proceed or why stand down."},
                },
                "required": ["ticker", "proceed", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


async def recheck(tickets: list[dict]) -> dict:
    """Morning gut-check before executing last night's approved tickets. Each `tickets` item:
    {ticker, strategy, entry, stop, target, current, move_pct}. Claude re-judges each at the open
    — did it gap badly, blow the R:R, or look broken? — and returns proceed/reason per name. This
    is the 're-analyze before the trade in case it tanked' guard. Returns {verdicts:[...]} keyed by
    ticker, or {error} (on which the caller falls back to the mechanical gate alone)."""
    if not _anthropic_key():
        return {"error": True, "summary": "No ANTHROPIC_API_KEY set."}
    if not tickets:
        return {"verdicts": []}

    import anthropic

    def line(t: dict) -> str:
        return (f"- {t['ticker']} [{t.get('strategy','?')}]: approved last night entry ${t.get('entry')}, "
                f"stop ${t.get('stop')}, target ${t.get('target')}. Opened today ${t.get('current')} "
                f"({'+' if (t.get('move_pct') or 0) >= 0 else ''}{t.get('move_pct')}% overnight).")

    prompt = (
        "You are the morning execution gate for an autonomous PAPER swing-trading system. Last night the "
        "system proposed and the human APPROVED the trades below (entry at today's open). Before any order "
        "fires, re-judge each one with this morning's actual opening price:\n\n"
        + "\n".join(line(t) for t in tickets)
        + "\n\nFor each ticker, decide proceed (true/false). Stand DOWN (false) if it gapped down through or "
        "near the stop, gapped up so far the reward:risk is now poor (chasing), or the move looks like a "
        "broken setup. Proceed (true) if the entry still makes sense at the open. Be decisive and brief — one "
        "sentence each. A modest overnight drift is normal and fine; only veto genuine breakdowns or blown R:R."
    )
    model = _recheck_model()
    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=model, max_tokens=900,
            output_config={"format": {"type": "json_schema", "schema": RECHECK_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        log.error("Morning recheck failed: %s", e)
        return {"error": True, "summary": f"recheck failed ({e.__class__.__name__})"}
    result = json.loads(next(b.text for b in resp.content if b.type == "text"))
    tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
    price_in, price_out = _PRICES.get(model, _PRICES["claude-sonnet-4-6"])
    result["_meta"] = {"model": model, "cost_usd": round(tin / 1e6 * price_in + tout / 1e6 * price_out, 4)}
    return result


async def recommend(candidates: list[dict], settings: ScanSettings, positions: list[dict] | None = None) -> dict:
    """Triage the candidate setups into a ranked shortlist. Returns the structured
    result plus a `_meta` block with token usage and estimated cost."""
    if not _anthropic_key():
        return {"error": True, "summary": "No ANTHROPIC_API_KEY set in .env — add one to use recommendations."}
    if not candidates:
        return {"error": True, "summary": "No candidates to recommend from — run a scan first."}

    import anthropic

    model = _model()
    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=1800,  # bull + bear + verdict per pick is wordier than a bare ranking
            output_config={"format": {"type": "json_schema", "schema": RECOMMEND_SCHEMA}},
            messages=[{"role": "user", "content": _build_prompt(candidates, settings, positions)}],
        )
    except anthropic.APIError as e:
        log.error("Recommend failed: %s", e)
        return {"error": True, "summary": f"Recommendation failed ({e.__class__.__name__}). Check your API key and credits."}

    result = json.loads(next(b.text for b in resp.content if b.type == "text"))
    tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
    price_in, price_out = _PRICES.get(model, _PRICES["claude-sonnet-4-6"])
    result["_meta"] = {
        "model": model,
        "input_tokens": tin,
        "output_tokens": tout,
        "cost_usd": round(tin / 1e6 * price_in + tout / 1e6 * price_out, 4),
        "considered": len(candidates),
    }
    return result
