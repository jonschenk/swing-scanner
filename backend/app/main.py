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
from .live import live
from .scanner import refresh_results, scan_market
from .trade_case import trade_case
from .recommend import recommend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Swing Scanner")

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


async def _run_scan(force_fresh: bool = False, scan_strategy: str = "leader_pullback") -> None:
    # The scan runs under the ACTIVE strategy variation (its knobs overlaid on the account
    # settings), so the picker actually drives the scan. Account fields (capital/universe/AI)
    # stay from settings.json. `scan_strategy` picks the signal family (leader-pullback vs
    # mean-reversion); the variation's knobs (e.g. atr_stop_mult) still apply.
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


@app.post("/api/queue/clear")
async def queue_clear() -> dict:
    return review_queue.clear()


# ---- alert engine (phase 1): auto-scan on a schedule + auto-fill the review queue ----
class AlertEngineRequest(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None
    mode: str | None = None
    max_positions: int | None = None


@app.get("/api/alerts/engine")
async def alert_engine_state() -> dict:
    return alert_engine.state()


@app.post("/api/alerts/engine")
async def alert_engine_configure(req: AlertEngineRequest) -> dict:
    return alert_engine.configure(req.enabled, req.interval_minutes, req.mode, req.max_positions)


ALERT_TICK_SECONDS = 60  # how often to check whether a cycle is due


async def _alert_cycle() -> None:
    """One scheduled cycle: pick the regime's strategy, scan, and auto-fill the review queue.
    Honours the router — bear regime sits in cash and queues nothing (the kill-switch)."""
    if scan_state["status"] in ("running", "analyzing"):
        return  # don't collide with a user-initiated scan; retry next tick
    if not alert_engine.market_open():
        alert_engine.mark("market-closed")
        return
    reg = await asyncio.to_thread(regime_mod.current_regime)
    if not reg.get("available"):
        alert_engine.mark("error")  # can't classify right now; retry next tick
        return
    regime = reg["regime"]
    if regime == "bear":
        alert_engine.record("bear-cash", regime=regime, strategy="cash")  # sit out, queue nothing
        return
    strat = "mean_reversion" if regime == "chop" else "leader_pullback"
    await _run_scan(force_fresh=False, scan_strategy=strat)
    rows = scan_state.get("results") or []
    exclude = alert_engine.exclude_today()
    if alert_engine.auto_mode():
        # PAPER-ONLY auto-execute: open positions for the gated setups (the kill-switch already
        # returned for bear above). Nothing here can reach a real broker.
        st = alert_engine.state()
        res = await asyncio.to_thread(paper.auto_execute, rows, st.get("max_positions", 5), exclude)
        alert_engine.record("auto-traded", regime=regime, strategy=strat, new_tickers=res.get("bought", []))
    else:
        res = review_queue.build(rows, regime, strat, exclude=exclude)
        alert_engine.record("watching", regime=regime, strategy=strat, new_tickers=res.get("added_tickers", []))


async def _alert_loop() -> None:
    """Background scheduler: when the engine is enabled and a cycle is due, run one. Opt-in;
    nothing it does opens a trade — it only fills the review queue for the human to act on."""
    while True:
        try:
            if alert_engine.enabled() and alert_engine.due():
                await _alert_cycle()
        except Exception:
            log.exception("alert engine cycle failed")
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


# Serve the built frontend (repo/frontend/dist) so the backend hosts the whole app — browse to the
# server's address and you get the UI + API same-origin (no per-device URL config). Mounted LAST so
# the /api/* routes above take precedence; only present when dist/ has been built (e.g. on the Pi).
_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
