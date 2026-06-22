"""FastAPI backend for the swing-trade scanner."""

import asyncio
import logging
import time
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root before anything reads the environment.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ai import analyze_all, analyze_single
from .config import ScanSettings, load_settings, save_settings
from . import price_cache
from . import paper
from . import journal
from . import regime as regime_mod
from . import strategy
from . import queue as review_queue
from . import alert_engine
from . import notify
from . import equity_log
from . import router
from . import risk
from . import eventlog
from . import daily_notes
from .live import live
from .scanner import refresh_results, scan_market
from .trade_case import trade_case
from .recommend import recommend, recheck

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Bellwether")

# The Electron app loads the UI from file:// (and the Vite dev server from
# localhost), so allow any origin. The server binds to 127.0.0.1 only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

scan_state: dict = {
    "status": "idle",  # idle | running | analyzing | done | error
    "progress": "",
    "results": [],
    "started_at": None,  # epoch seconds the current/last scan began
    "scanned_at": None,  # epoch seconds the technical scan finished (cards available)
    "finished_at": None,  # epoch seconds AI analysis completed
    "refreshed_at": None,  # epoch seconds of the last lightweight refresh
    "refreshing": False,
    "from_cache": False,  # whether the last scan reused cached prices
    "ai_top_n": None,  # how many setups are auto-analyzed (the rest are on-demand)
    "error": None,
}


def _set_progress(message: str) -> None:
    scan_state["progress"] = message
    log.info(message)


async def _run_scan(force_fresh: bool = False, scan_strategy: str = "leader_pullback",
                    params_override: dict | None = None) -> None:
    # Params come from one of two places. The AUTONOMOUS engine passes the validated router params
    # for the current regime (params_override) — fully automatic, no human picking. A MANUAL scan
    # (the full app's Run Scan) uses the active strategy variation (the dev/research picker).
    # Account fields (capital/universe/AI) always come from settings.json.
    if params_override is not None:
        settings = load_settings().model_copy(update=params_override)
        scan_state["variation"] = {"id": f"router-{scan_strategy}", "name": "Validated router"}
    else:
        settings = strategy.apply_active(load_settings())
        active = strategy.get_active()
        scan_state["variation"] = {"id": active["id"], "name": active["name"]} if active else None
    scan_state["strategy"] = scan_strategy
    reg = regime_mod.current_regime()
    scan_state["regime_label"] = reg.get("regime") if reg.get("available") else None
    scan_state["from_cache"] = (
        not force_fresh and price_cache.is_fresh(settings.universe, settings.cache_minutes)
    )
    try:
        _set_progress("Starting scan…")
        candidates = await asyncio.to_thread(scan_market, settings, _set_progress, force_fresh, scan_strategy)
        # Publish the technical results immediately — the cards show right away
        # with "awaiting AI" placeholders, and analyze_all fills in each card's
        # analysis in place (the dicts are the same objects polled via /status).
        scan_state["results"] = candidates
        scan_state["scanned_at"] = time.time()
        if candidates:
            scan_state["status"] = "analyzing"
            scan_state["ai_top_n"] = settings.ai_top_n
            _set_progress(f"{len(candidates)} setups found — running AI analysis…")
            await analyze_all(candidates, _set_progress, limit=settings.ai_top_n)
        scan_state["status"] = "done"
        scan_state["finished_at"] = time.time()
        _set_progress(f"Scan complete — {len(candidates)} matches")
    except Exception as e:
        log.exception("Scan failed")
        scan_state["status"] = "error"
        scan_state["error"] = str(e)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/regime")
async def get_regime() -> dict:
    """Today's market regime (bull/chop/bear) + the strategy the validated router would run.
    Decision-support display only — it never trades. Cached ~1h in the module."""
    return await asyncio.to_thread(regime_mod.current_regime)


@app.get("/api/strategies")
async def get_strategies() -> dict:
    """The strategy-variation store: the active id + every variation (id/name/params/notes).
    Drives the variation picker."""
    strategy.ensure_seeded(load_settings())
    return {"active": strategy.active_id(), "variations": strategy.list_variations()}


class ActiveStrategyRequest(BaseModel):
    id: str


@app.post("/api/strategies/active")
async def set_active_strategy(req: ActiveStrategyRequest) -> dict:
    """Switch the active strategy variation. The next scan/refresh runs under it."""
    try:
        strategy.set_active(req.id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No such variation: {req.id}")
    return {"active": strategy.active_id(), "variations": strategy.list_variations()}


def _settings_dict(settings: ScanSettings) -> dict:
    return {**settings.model_dump(), "max_price": round(settings.max_price, 2)}


@app.get("/api/settings")
async def get_settings() -> dict:
    return _settings_dict(load_settings())


@app.put("/api/settings")
async def update_settings(settings: ScanSettings) -> dict:
    if settings.max_price <= settings.min_price:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Max share price (capital x max-position-% = ${settings.max_price:,.0f}) "
                f"must exceed the min price (${settings.min_price:,.0f}). "
                "Raise your capital or max-position-%."
            ),
        )
    save_settings(settings)
    return _settings_dict(settings)


@app.post("/api/scan", status_code=202)
async def start_scan(fresh: bool = False, strategy: str = "leader_pullback") -> dict:
    if scan_state["status"] == "running":
        raise HTTPException(status_code=409, detail="A scan is already running")
    if strategy not in ("leader_pullback", "mean_reversion"):
        raise HTTPException(status_code=422, detail=f"Unknown strategy: {strategy}")
    scan_state.update(
        status="running",
        progress="Queued…",
        error=None,
        results=[],
        started_at=time.time(),
        scanned_at=None,
        finished_at=None,
        refreshed_at=None,
    )
    asyncio.create_task(_run_scan(force_fresh=fresh, scan_strategy=strategy))
    return {"status": "running"}


@app.post("/api/refresh")
async def refresh_scan() -> dict:
    """Cheap live update of the displayed setups only (no re-scan, no AI)."""
    if scan_state["status"] == "running" or scan_state["refreshing"]:
        return scan_state
    rows = scan_state.get("results") or []
    if not rows:
        return scan_state
    scan_state["refreshing"] = True
    try:
        settings = strategy.apply_active(load_settings())  # same strategy the scan used
        scan_strategy = scan_state.get("strategy", "leader_pullback")
        scan_state["results"] = await asyncio.to_thread(refresh_results, settings, rows, scan_strategy)
        scan_state["refreshed_at"] = time.time()
    except Exception:
        log.exception("Refresh failed")
    finally:
        scan_state["refreshing"] = False
    return scan_state


class AnalyzeRequest(BaseModel):
    ticker: str


@app.post("/api/analyze")
async def analyze_one(req: AnalyzeRequest) -> dict:
    """On-demand AI analysis for a single already-scanned ticker."""
    rows = scan_state.get("results") or []
    stock = next((r for r in rows if r["ticker"] == req.ticker), None)
    if stock is None:
        raise HTTPException(status_code=404, detail="Ticker is not in the current results")
    if stock.get("ai"):
        return scan_state  # already analyzed
    stock["ai_status"] = "pending"
    await analyze_single(stock)
    return scan_state


class TradeCaseRequest(BaseModel):
    ticker: str
    positions: list[dict] = []  # optional holdings: [{ticker, shares, avg_price?, sector?}]


@app.post("/api/trade-case")
async def trade_case_one(req: TradeCaseRequest) -> dict:
    """On-demand account-aware deep analysis (Claude) for one scanned ticker."""
    rows = scan_state.get("results") or []
    stock = next((r for r in rows if r["ticker"] == req.ticker), None)
    if stock is None:
        raise HTTPException(status_code=404, detail="Ticker is not in the current results")
    stock["tc_status"] = "pending"
    stock["trade_case"] = await trade_case(stock, load_settings(), req.positions or None)
    stock.pop("tc_status", None)
    return scan_state


class RecommendRequest(BaseModel):
    positions: list[dict] = []
    top_n: int = 12


@app.post("/api/recommend")
async def recommend_one(req: RecommendRequest) -> dict:
    """Triage the current scan's top-N setups into a ranked shortlist (one Claude pass)."""
    rows = scan_state.get("results") or []
    if not rows:
        raise HTTPException(status_code=400, detail="No scan results to recommend from")
    candidates = rows[: max(1, req.top_n)]  # already sorted by setup_score
    result = await recommend(candidates, load_settings(), req.positions or None)

    for r in rows:  # clear any prior pass
        r.pop("recommendation", None)
    if not result.get("error"):
        for p in result.get("picks", []):
            row = next((r for r in rows if r["ticker"] == p["ticker"]), None)
            if row:
                row["recommendation"] = {k: p.get(k) for k in ("rank", "call", "reason", "conviction")}
    scan_state["recommendation"] = {
        "summary": result.get("summary", ""),
        "skip_note": result.get("skip_note", ""),
        "error": result.get("error", False),
        "_meta": result.get("_meta"),
    }
    return scan_state


@app.get("/api/scan/status")
async def scan_status() -> dict:
    return scan_state


@app.post("/api/live/start")
async def live_start() -> dict:
    """Stream live prices for the currently displayed setups (Yahoo websocket)."""
    tickers = [r["ticker"] for r in (scan_state.get("results") or [])]
    count = await live.set_symbols(tickers)
    return {"streaming": count > 0, "count": count}


@app.post("/api/live/stop")
async def live_stop() -> dict:
    await live.stop()
    return {"streaming": False}


@app.get("/api/live")
async def live_prices() -> dict:
    """Latest streamed price per ticker: {ticker: {price, change_percent, time}}."""
    return live.prices()


class PaperBuyRequest(BaseModel):
    ticker: str


class PaperCloseRequest(BaseModel):
    trade_id: str
    exit_price: float | None = None


class PaperResetRequest(BaseModel):
    capital: float | None = None


@app.get("/api/journal")
async def journal_view() -> dict:
    """All logged trades + the per-variation scoreboard (winrate/expectancy)."""
    return {"trades": journal.list_trades(), "summary": journal.summary_by_variation()}


@app.get("/api/equity-log")
async def equity_log_view() -> dict:
    """Daily equity-curve + SPY-benchmark snapshots for the forward paper-proving period."""
    return {"rows": equity_log.rows()}


@app.get("/api/daily-notes")
async def daily_notes_view() -> dict:
    """The observational daily journal notes (newest first)."""
    return {"notes": daily_notes.list_notes()}


@app.get("/api/paper/account")
async def paper_account() -> dict:
    return paper.account()


@app.post("/api/paper/buy")
async def paper_buy(req: PaperBuyRequest) -> dict:
    """Place a paper order for a currently-scanned ticker, honouring the order-type setting
    (market fills now; moo/limit rest until their condition)."""
    rows = scan_state.get("results") or []
    stock = next((r for r in rows if r["ticker"] == req.ticker), None)
    if stock is None:
        raise HTTPException(status_code=404, detail="Ticker is not in the current results")
    return await asyncio.to_thread(paper.submit, stock, load_settings())


@app.post("/api/paper/close")
async def paper_close(req: PaperCloseRequest) -> dict:
    return await asyncio.to_thread(paper.close, req.trade_id, req.exit_price)


class PaperCancelRequest(BaseModel):
    order_id: str


@app.post("/api/paper/cancel")
async def paper_cancel(req: PaperCancelRequest) -> dict:
    """Cancel a resting MOO/limit paper order."""
    return await asyncio.to_thread(paper.cancel_order, req.order_id)


@app.post("/api/paper/reset")
async def paper_reset(req: PaperResetRequest) -> dict:
    capital = req.capital if req.capital is not None else load_settings().capital
    return paper.reset(capital)


# ---- approve/deny review queue (phase 4) ----
class QueueBuildRequest(BaseModel):
    top_n: int = review_queue.DEFAULT_TOP_N


class QueueDecisionRequest(BaseModel):
    id: str
    reason: str = ""


@app.get("/api/queue")
async def queue_view() -> dict:
    """Pending trade tickets awaiting the human's Approve/Deny + recently decided ones."""
    return review_queue.view()


@app.post("/api/queue/build")
async def queue_build(req: QueueBuildRequest) -> dict:
    """Populate the queue from the current scan's best setups (recommended picks first)."""
    rows = scan_state.get("results") or []
    if not rows:
        raise HTTPException(status_code=409, detail="No scan results to build a queue from. Run a scan first.")
    return review_queue.build(rows, scan_state.get("regime_label"), scan_state.get("strategy", "leader_pullback"), req.top_n)


@app.post("/api/queue/approve")
async def queue_approve(req: QueueDecisionRequest) -> dict:
    """The human pulls the trigger: open a paper position from this proposal's ticket."""
    return await asyncio.to_thread(review_queue.approve, req.id)


@app.post("/api/queue/deny")
async def queue_deny(req: QueueDecisionRequest) -> dict:
    """Pass on a proposal (logs it with the advisor's call for later grading)."""
    return review_queue.deny(req.id, req.reason)


class QueueDecideRequest(BaseModel):
    id: str
    decision: str  # "approve" | "deny"
    reason: str = ""


@app.post("/api/queue/decide")
async def queue_decide(req: QueueDecideRequest) -> dict:
    """Nightly-model review: record the human's overnight Approve/Deny WITHOUT executing. Approved
    tickets are re-checked and traded at the next open; denied ones are logged for grading."""
    res = review_queue.decide(req.id, req.decision, req.reason)
    if not res.get("error"):
        eventlog.log_event("review", f"human {req.decision}d {req.id}", decision=req.decision, reason=req.reason)
    return res


@app.post("/api/queue/clear")
async def queue_clear() -> dict:
    return review_queue.clear()


@app.get("/api/events")
async def events(day: str | None = None, n: int = 200) -> dict:
    """The engine's chronological audit trail — every move it made. `day` (YYYY-MM-DD ET) returns
    that day's events oldest-first; otherwise the most recent `n` events newest-first."""
    return {"events": eventlog.for_day(day) if day else eventlog.tail(n)}


# ---- alert engine (phase 1): auto-scan on a schedule + auto-fill the review queue ----
class AlertEngineRequest(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None
    mode: str | None = None
    max_positions: int | None = None
    open_buffer_min: int | None = None
    ai_picks: bool | None = None


@app.get("/api/alerts/engine")
async def alert_engine_state() -> dict:
    return alert_engine.state()


@app.post("/api/alerts/engine")
async def alert_engine_configure(req: AlertEngineRequest) -> dict:
    return alert_engine.configure(req.enabled, req.interval_minutes, req.mode, req.max_positions,
                                  req.open_buffer_min, req.ai_picks)


ALERT_TICK_SECONDS = 60  # how often to check whether a cycle is due


async def _alert_cycle() -> None:
    """One scheduled cycle: pick the regime's strategy, scan, and auto-fill the review queue.
    Honours the router — bear regime sits in cash and queues nothing (the kill-switch)."""
    if scan_state["status"] in ("running", "analyzing"):
        return  # don't collide with a user-initiated scan; retry next tick
    if not alert_engine.market_open():
        alert_engine.mark("market-closed")
        return
    if alert_engine.in_open_warmup():
        alert_engine.mark("warming-up")  # let the volatile open settle before acting
        return
    reg = await asyncio.to_thread(regime_mod.current_regime)
    if not reg.get("available"):
        alert_engine.mark("error")  # can't classify right now; retry next tick
        return
    regime = reg["regime"]
    # The router picks the strategy AND its validated params for this regime — fully automatic.
    strat, params = router.for_regime(regime)
    if strat == "cash":  # bear -> sit out, trade nothing (the kill-switch)
        alert_engine.record("bear-cash", regime=regime, strategy="cash")
        return
    # Fractional-Kelly sizing from THIS regime's measured edge (no-op until ~20 closed trades,
    # then it sizes on the evidence). ai_picks is always on, so the live variation is router-<strat>+ai.
    base_risk = load_settings().risk_pct
    perf = journal.summary_by_variation().get(f"router-{strat}+ai")
    kelly_pct, kelly_note = risk.kelly_risk_pct(base_risk, perf)
    if kelly_pct != base_risk:
        log.info("Kelly sizing: %s risk_pct %.2f -> %.2f (%s)", strat, base_risk, kelly_pct, kelly_note)
    params = {**params, "risk_pct": kelly_pct}
    await _run_scan(force_fresh=False, scan_strategy=strat, params_override=params)
    rows = scan_state.get("results") or []
    exclude = alert_engine.exclude_today()
    if alert_engine.auto_mode():
        st = alert_engine.state()
        max_pos = st.get("max_positions", 5)
        rows_to_trade, vid = rows, f"router-{strat}"
        # AI-judged selection: Claude triages the mechanical finalists and we trade only its "Take"
        # picks; the top finalists it vetoes are logged as passes so we can later grade whether its
        # judgment actually beat pure mechanical. Falls back to mechanical if the call fails.
        if st.get("ai_picks") and rows:
            positions = [{"ticker": p["ticker"], "shares": p["shares"]} for p in paper.account().get("positions", [])]
            rec = await recommend(rows, load_settings(), positions)
            if not rec.get("error"):
                picks = {p["ticker"]: p for p in rec.get("picks", [])}
                for r in rows:
                    r["recommendation"] = picks.get(r["ticker"])
                vid = f"router-{strat}+ai"
                takes = [r for r in rows if (picks.get(r["ticker"]) or {}).get("call") == "Take"]
                take_set = {r["ticker"] for r in takes}
                for r in rows[:max_pos]:  # mechanical top-N Claude vetoed -> log for grading
                    if r["ticker"] not in take_set and r["ticker"] not in exclude:
                        pk = picks.get(r["ticker"]) or {}
                        journal.log_pass(r, vid, decision=pk.get("call") or "Skip",
                                         notes=(pk.get("reason") or rec.get("skip_note") or "")[:200])
                rows_to_trade = takes
            else:
                log.warning("AI picks unavailable (%s); using mechanical selection", rec.get("summary"))
        res = await asyncio.to_thread(paper.auto_execute, rows_to_trade, max_pos, exclude, vid, params)
        bought = res.get("bought", [])
        alert_engine.record("auto-traded", regime=regime, strategy=strat, new_tickers=bought)
        if bought:
            judged = " (Claude-picked)" if vid.endswith("+ai") else ""
            notify.send(f"Opened {len(bought)} paper position{'s' if len(bought) != 1 else ''}{judged}: {', '.join(bought)}",
                        title=f"Auto-traded · {regime} → {strat}", tags="robot")
    else:
        res = review_queue.build(rows, regime, strat, exclude=exclude)
        added = res.get("added_tickers", [])
        alert_engine.record("watching", regime=regime, strategy=strat, new_tickers=added)
        if added:
            notify.send(f"{len(added)} new setup{'s' if len(added) != 1 else ''} to review: {', '.join(added)}",
                        title=f"Alerts · {regime} → {strat}", tags="bell")


async def _nightly_build() -> None:
    """Evening (post-close): scan settled daily bars, run the bull/bear debate, and PROPOSE
    tomorrow's tickets for the human to review overnight. Proposes nothing in a bear regime (the
    cash kill-switch). This is the validated cadence — signal on the close, enter at the next open."""
    if scan_state["status"] in ("running", "analyzing"):
        return  # don't collide with a manual scan; retry next tick
    trading_day = alert_engine.next_session_date()
    reg = await asyncio.to_thread(regime_mod.current_regime)
    if not reg.get("available"):
        eventlog.log_event("error", "evening build: regime unavailable — retrying next tick")
        return  # don't mark the build done; try again shortly
    regime = reg["regime"]
    strat, params = router.for_regime(regime)
    eventlog.log_event("regime", f"evening: {regime} → {strat}", regime=regime, strategy=strat)
    if strat == "cash":  # bear -> sit out (kill-switch)
        alert_engine.record("bear-cash", regime=regime, strategy="cash")
        alert_engine.mark_nightly_build()
        eventlog.log_event("propose", f"bear regime — no setups for {trading_day}", trading_day=trading_day)
        notify.send(f"Bear regime — cash, no setups for {trading_day}.", title="Nightly · cash", tags="moneybag")
        return
    # Fractional-Kelly sizing from this regime's measured edge (no-op until ~20 closed trades).
    base_risk = load_settings().risk_pct
    perf = journal.summary_by_variation().get(f"router-{strat}+ai")
    kelly_pct, kelly_note = risk.kelly_risk_pct(base_risk, perf)
    if kelly_pct != base_risk:
        eventlog.log_event("sizing", f"Kelly risk_pct {base_risk}→{kelly_pct} ({kelly_note})", risk_pct=kelly_pct)
    params = {**params, "risk_pct": kelly_pct}
    await _run_scan(force_fresh=True, scan_strategy=strat, params_override=params)  # fresh = the day's final bars
    rows = scan_state.get("results") or []
    eventlog.log_event("scan", f"evening scan: {len(rows)} setups cleared ({strat})", count=len(rows), strategy=strat)

    proposed: list[str] = []
    settings = load_settings()
    equity = paper.account().get("equity") or settings.capital
    max_pos = settings.max_concurrent_positions
    if rows:
        positions = [{"ticker": p["ticker"], "shares": p["shares"]} for p in paper.account().get("positions", [])]
        rec = await recommend(rows, settings, positions)  # bull/bear debate (budget/position-count aware)
        if not rec.get("error"):
            picks = {p["ticker"]: p for p in rec.get("picks", [])}
            for r in rows:
                r["recommendation"] = picks.get(r["ticker"])
            picked = [r for r in rows if r["ticker"] in picks]
            eventlog.log_event("propose", f"AI debate picked {len(picked)} of {len(rows)}",
                               picks={t: (p.get("call"), p.get("conviction")) for t, p in picks.items()})
        else:
            eventlog.log_event("error", f"AI debate unavailable ({rec.get('summary')}) — proposing top mechanical setups")
            picked = rows
        # Size each pick to the budget (per-position cap off equity) so the proposed plan is realistic,
        # not sized as if the full account were free for it. Propose a small buffer above the position
        # cap so the human has a couple of alternates and a morning veto doesn't leave the book short.
        sized_picks = []
        for r in picked:
            sp = risk.resize_for_budget(r.get("plan") or {}, settings, equity, equity)
            if sp:
                sized_picks.append({**r, "plan": sp})
        res = review_queue.build(sized_picks, regime, strat, top_n=max_pos + 2, trading_day=trading_day)
        proposed = res.get("added_tickers", [])
    alert_engine.record("built", regime=regime, strategy=strat, new_tickers=proposed)
    alert_engine.mark_nightly_build()
    eventlog.log_event("propose", f"proposed {len(proposed)} ticket(s) for {trading_day}: {', '.join(proposed) or 'none'}",
                       trading_day=trading_day, tickers=proposed)
    if proposed:
        notify.send(f"{len(proposed)} setup{'s' if len(proposed) != 1 else ''} for {trading_day}: {', '.join(proposed)}. Review tonight.",
                    title=f"Nightly · {regime} → {strat}", tags="memo")
    else:
        notify.send(f"No setups cleared for {trading_day}.", title="Nightly · nothing tonight", tags="zzz")


async def _nightly_morning() -> None:
    """Morning (just after the open): re-check last night's APPROVED tickets against the actual
    opening price and execute the ones that still look valid. A mechanical gate (gapped through the
    stop / already past target / no quote) plus a Claude gut-check guard against overnight tanks.
    Un-reviewed tickets expire (safe default: don't trade what the human didn't approve)."""
    if scan_state["status"] in ("running", "analyzing"):
        return
    today = alert_engine._now_et().date().isoformat()
    approved = review_queue.approved_for_execution(today)
    expired = review_queue.expire_pending(today)
    if expired:
        eventlog.log_event("review", f"expired {len(expired)} un-reviewed ticket(s): {', '.join(expired)}", tickers=expired)
    if not approved:
        alert_engine.record("executed", new_tickers=[])
        alert_engine.mark_nightly_exec()
        eventlog.log_event("execute", "morning: no approved tickets to execute")
        return

    settings = load_settings()
    acct = paper.account()
    equity, cash = acct["equity"], acct["cash"]
    # Best-conviction first, so the budget flows to the strongest picks (and weaker ones drop by rank).
    conv_rank = {"High": 0, "Medium": 1, "Low": 2}
    approved = sorted(approved, key=lambda p: (conv_rank.get(p.get("conviction"), 1), -(p.get("score") or 0)))

    prices = await asyncio.to_thread(paper._quote, [p["ticker"] for p in approved])
    payload = []
    for p in approved:
        plan = p.get("plan") or {}
        cur, entry = prices.get(p["ticker"]), plan.get("entry")
        payload.append({"ticker": p["ticker"], "strategy": p.get("strategy"), "entry": entry,
                        "stop": plan.get("stop"), "target": plan.get("target"), "current": cur,
                        "move_pct": round((cur / entry - 1) * 100, 2) if (cur and entry) else None})
    rc = await recheck(payload)  # one Claude call: proceed/stand-down per name
    verdicts = {v["ticker"]: v for v in rc.get("verdicts", [])} if not rc.get("error") else {}
    if rc.get("error"):
        eventlog.log_event("error", f"AI re-check unavailable ({rc.get('summary')}) — mechanical gate only")

    # Gate each name (mechanical + AI). Survivors carry a plan RE-PRICED to the actual open.
    survivors, skipped = [], []
    for p in approved:
        plan = p.get("plan") or {}
        cur, stop, target = prices.get(p["ticker"]), plan.get("stop"), plan.get("target")
        if not cur:
            review_queue.mark(p["id"], "skipped", "no opening quote")
            skipped.append(p["ticker"]); eventlog.log_event("skip", f"{p['ticker']}: no opening quote"); continue
        if stop and cur <= stop:
            review_queue.mark(p["id"], "skipped", f"gapped through stop (${cur} ≤ ${stop})")
            skipped.append(p["ticker"]); eventlog.log_event("recheck", f"{p['ticker']} BROKEN — gapped through stop", current=cur, stop=stop); continue
        if target and cur >= target:
            review_queue.mark(p["id"], "skipped", f"already at/above target (${cur} ≥ ${target})")
            skipped.append(p["ticker"]); eventlog.log_event("recheck", f"{p['ticker']} no R:R left — at/above target", current=cur, target=target); continue
        v = verdicts.get(p["ticker"])
        if v and not v.get("proceed"):
            review_queue.mark(p["id"], "skipped", f"AI stand-down: {v.get('reason')}")
            skipped.append(p["ticker"]); eventlog.log_event("recheck", f"{p['ticker']} AI stand-down: {v.get('reason')}"); continue
        open_plan = {**plan, "entry": round(cur, 2), "stop_distance": round(cur - stop, 2)}  # size off the real open
        survivors.append({**p, "plan": open_plan})

    # Budget-aware allocation across survivors: conviction order, off REAL cash + the per-position cap,
    # so the picks actually fit the account instead of each being sized as if full cash were free.
    taken, dropped = risk.allocate(survivors, settings, equity, cash, settings.max_concurrent_positions)
    for d in dropped:
        sp = next((s for s in survivors if s["ticker"] == d["ticker"]), None)
        if sp:
            review_queue.mark(sp["id"], "skipped", d["reason"]); skipped.append(d["ticker"])
            eventlog.log_event("skip", f"{d['ticker']} not traded: {d['reason']}")

    bought = []
    for t in taken:
        strat = t.get("strategy") or "leader_pullback"
        vid = f"router-{strat}+ai"
        params = {**router.for_regime(t.get("regime"))[1], "risk_pct": settings.risk_pct}
        stock = {**t["stock"], "plan": t["plan"]}  # the budget-sized plan drives the buy
        res = await asyncio.to_thread(paper.buy, stock, vid, params)
        if res.get("error"):
            review_queue.mark(t["id"], "skipped", f"execution failed: {res['error']}")
            skipped.append(t["ticker"]); eventlog.log_event("skip", f"{t['ticker']} execution failed: {res['error']}"); continue
        pl = t["plan"]
        review_queue.mark(t["id"], "executed", f"{pl['shares']} sh ≈ ${pl['position_cost']} ({pl['position_pct']}%)")
        bought.append(t["ticker"])
        eventlog.log_event("execute", f"bought {pl['shares']} {t['ticker']} ≈ ${pl['position_cost']} ({pl['position_pct']}% of book)",
                           ticker=t["ticker"], shares=pl["shares"], cost=pl["position_cost"])

    alert_engine.record("executed", strategy=(approved[0].get("strategy") if approved else None), new_tickers=bought)
    alert_engine.mark_nightly_exec()
    eventlog.log_event("execute", f"morning done — bought {len(bought)}, skipped {len(skipped)}",
                       bought=bought, skipped=skipped)
    msg = f"Bought {len(bought)}: {', '.join(bought)}." if bought else "Bought nothing this morning."
    if skipped:
        msg += f" Skipped {len(skipped)}: {', '.join(skipped)}."
    notify.send(msg, title="Nightly · morning execute", tags="robot")


async def _alert_loop() -> None:
    """Background scheduler. In NIGHTLY mode (the validated default) it runs the evening build once
    after the close and the morning execute once after the open. The intraday review/auto modes run
    on the interval instead. Opt-in; the human approves overnight and nothing executes without it."""
    while True:
        try:
            if alert_engine.enabled():
                m = alert_engine.mode()
                if m == "nightly":
                    if alert_engine.nightly_build_due():
                        await _nightly_build()
                    elif alert_engine.nightly_exec_due():
                        await _nightly_morning()
                elif alert_engine.due():
                    await _alert_cycle()
            # Daily equity-curve snapshot + the observational daily note (both self-dedup per ET day,
            # run post-close regardless of the engine, so the forward record keeps accruing).
            await asyncio.to_thread(equity_log.maybe_record_eod)
            await daily_notes.maybe_generate_eod()
        except Exception:
            log.exception("alert engine cycle failed")
            eventlog.log_event("error", "alert loop tick failed (see server log)")
        await asyncio.sleep(ALERT_TICK_SECONDS)


_paper_monitor: asyncio.Task | None = None
_alert_task: asyncio.Task | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _paper_monitor, _alert_task
    # Seed a fresh paper account from the user's capital on first run.
    if not paper.ACCOUNT_PATH.exists():
        paper.reset(load_settings().capital)
    # Seed the strategy-variation store (Baseline + Validated bull) so the picker has choices.
    strategy.ensure_seeded(load_settings())
    _paper_monitor = asyncio.create_task(paper.monitor_loop())
    _alert_task = asyncio.create_task(_alert_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    await live.stop()
    if _paper_monitor:
        _paper_monitor.cancel()
    if _alert_task:
        _alert_task.cancel()


# Static apps the backend serves (so the Pi hosts everything, same-origin — no per-device URL config).
# Mounted AFTER the /api/* routes so those take precedence; more-specific /app before the "/" catch-all.
# `viewer/` is the read-only monitor companion (mobile-first; "Add to Home Screen" on iOS).
_REPO = Path(__file__).resolve().parents[2]
_VIEWER = _REPO / "viewer"
if _VIEWER.is_dir():
    app.mount("/app", StaticFiles(directory=_VIEWER, html=True), name="viewer")
_DIST = _REPO / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
