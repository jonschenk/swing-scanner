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

DEFAULT_RECOMMEND_MODEL = "claude-sonnet-4-6"  # triage is comparative but light; Sonnet is plenty


def _model() -> str:
    return os.environ.get("RECOMMEND_MODEL", DEFAULT_RECOMMEND_MODEL).strip()


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
                    "call": {"type": "string", "enum": ["Take", "Watch"]},
                    "reason": {"type": "string", "description": "One sentence: why this one, for this account."},
                    "conviction": {"type": "string", "enum": ["High", "Medium", "Low"]},
                },
                "required": ["ticker", "rank", "call", "reason", "conviction"],
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
    return f"""You are triaging a swing-trade scan for a trader who holds 2-5 days. Every name below already passed the technical screen (market leader, uptrend, healthy pullback). Your job is to pick the few worth focusing on TODAY and say what to skip — judging them against each other AND this specific account.

ACCOUNT: ${settings.capital:,.0f} capital, {settings.risk_pct}% risk per trade.
OPEN POSITIONS: {_positions_block(positions)}

CANDIDATES (already ranked by the scanner's setup score):
{lines}

Pick the 2-4 you'd actually focus on, best first. Weigh: cleanliness of the entry, reward:risk, trend/volume quality, AND portfolio fit — don't recommend piling into a sector the account is already heavy in, and prefer names that diversify or have the best standalone setup. Give each pick a one-sentence reason specific to this account, a Take or Watch call, and a conviction. Then a short overall summary and a note on what to skip and why. Only use tickers from the list. Be selective — if only one or two are genuinely worth it, recommend only those."""


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
            max_tokens=1200,
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
