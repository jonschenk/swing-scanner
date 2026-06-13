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
from pydantic import BaseModel

from .ai import analyze_all, analyze_single
from .config import ScanSettings, load_settings, save_settings
from . import price_cache
from .scanner import refresh_results, scan_market

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
    "error": None,
}


def _set_progress(message: str) -> None:
    scan_state["progress"] = message
    log.info(message)


async def _run_scan(force_fresh: bool = False) -> None:
    settings = load_settings()
    scan_state["from_cache"] = (
        not force_fresh and price_cache.is_fresh(settings.universe, settings.cache_minutes)
    )
    try:
        _set_progress("Starting scan…")
        candidates = await asyncio.to_thread(scan_market, settings, _set_progress, force_fresh)
        # Publish the technical results immediately — the cards show right away
        # with "awaiting AI" placeholders, and analyze_all fills in each card's
        # analysis in place (the dicts are the same objects polled via /status).
        scan_state["results"] = candidates
        scan_state["scanned_at"] = time.time()
        if candidates:
            scan_state["status"] = "analyzing"
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
async def start_scan(fresh: bool = False) -> dict:
    if scan_state["status"] == "running":
        raise HTTPException(status_code=409, detail="A scan is already running")
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
    asyncio.create_task(_run_scan(force_fresh=fresh))
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
        settings = load_settings()
        scan_state["results"] = await asyncio.to_thread(refresh_results, settings, rows)
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


@app.get("/api/scan/status")
async def scan_status() -> dict:
    return scan_state
