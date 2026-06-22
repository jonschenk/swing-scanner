"""Scan settings with JSON-file persistence."""

import json
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

SETTINGS_PATH = Path(__file__).resolve().parents[1] / "settings.json"


class ScanSettings(BaseModel):
    # --- scan scope ---
    universe: str = Field(default="full")  # "full" (whole US market) or "curated" (tickers.txt)
    max_results: int = Field(default=30, ge=1, le=200)  # top-N setups to keep
    ai_top_n: int = Field(default=10, ge=1, le=200)  # auto-analyze top-N; rest are on-demand
    cache_minutes: int = Field(default=30, ge=0, le=1440)  # reuse cached prices this long (0 = off)

    # --- account & risk ---
    capital: float = Field(default=1000.0, gt=0)  # your trading capital ($)
    risk_pct: float = Field(default=2.0, gt=0, le=100)  # max % of capital to risk per trade
    max_position_pct: float = Field(default=50.0, gt=0, le=100)  # cap on price as % of capital
    # Budget-aware allocation across a batch of picks (so the account isn't sized as if full cash
    # were free for EACH entry). max_alloc_pct caps any one position's COST as % of equity (lets a
    # small account hold several names + stops one tight-stop pick eating the whole book).
    max_alloc_pct: float = Field(default=30.0, gt=0, le=100)
    max_concurrent_positions: int = Field(default=4, ge=1, le=50)  # breadth cap (Fundamental Law: breadth extracts a modest edge)
    atr_stop_mult: float = Field(default=1.5, gt=0)  # stop = entry - mult * ATR
    reward_mult: float = Field(default=2.0, gt=0)  # target = entry + mult * stop distance
    cap_target_at_high: bool = Field(default=True)  # cap the target at the 52w high (off = pure R:R)
    require_market_uptrend: bool = Field(default=False)  # only enter when SPY > its 200-SMA (regime filter; backtest)
    min_breadth_pct: float = Field(default=0.0, ge=0, le=100)  # only enter when >= this % of the universe is above its 200-SMA (0 = off; backtest)

    # --- mean-reversion strategy knobs (backtest --strategy mean_reversion) ---
    mr_rsi2_max: float = Field(default=10.0, gt=0, le=100)  # oversold trigger: RSI(2) must be BELOW this (lower = more selective)
    mr_min_stretch_pct: float = Field(default=4.0, ge=0)  # require close >= this % below the 5-SMA (the validated selective lever; 0 = off)
    mr_require_uptrend: bool = Field(default=False)  # quality: only buy dips when 50>200 SMA stack AND the 200-SMA is rising

    # --- liquidity / price ---
    min_price: float = Field(default=15.0, ge=0)
    min_avg_volume: int = Field(default=500_000, ge=0)

    # --- paper execution ---
    # How paper orders fill: "market" = immediately at the live quote (+slippage); "moo" =
    # rest until the next market open and fill there (faithful to the backtest's T+1-open entry);
    # "limit" = rest and fill only at/below the planned entry (no chasing the open, no slippage).
    paper_order_type: str = Field(default="market")
    open_buffer_minutes: int = Field(default=0, ge=0, le=120)  # MOO: wait this long after 9:30 ET to fill (skip the squirrely open)

    # --- exit management (validated on the backtester: an ATR trailing stop beat the fixed
    # stop+target on BOTH out-of-sample expectancy AND drawdown, full universe, incl. unseen bears) ---
    trailing_stop: bool = Field(default=True)  # True = ratcheting ATR trail (no fixed target); False = fixed stop+target
    max_hold_days: int = Field(default=10, ge=1)  # time-stop: close at market after this many trading days held

    # --- earnings risk ---
    # Flag a setup whose next earnings report falls within this many days (a binary gap the ATR
    # stop can't cover). 0 = don't check earnings. Flagged setups are surfaced with a warning and
    # skipped by the auto-trade gate; the manual scan still shows them (your call).
    avoid_earnings_within_days: int = Field(default=5, ge=0, le=60)

    # --- trend strength ---
    adx_min: float = Field(default=25.0, ge=0)  # ADX trend-strength floor (25 = trending)

    # --- pullback timing (the entry window) ---
    # Research consensus is a 40-60 "healthy pullback" band: cooled off from
    # overbought, but not broken down. Leaders near their highs rarely dip below 50.
    rsi_threshold: float = Field(default=60.0, gt=0, le=100)  # RSI must be BELOW this (pulled back)
    rsi_floor: float = Field(default=40.0, ge=0, le=100)  # ...but ABOVE this (healthy, not broken)

    # --- volatility ---
    atr_pct_min: float = Field(default=2.0, ge=0)  # min daily range as % of price

    # --- leadership: relative strength & proximity to highs (Minervini / Qullamaggie) ---
    near_high_pct: float = Field(default=30.0, ge=0, le=100)  # max % below 52-week high
    min_above_low_pct: float = Field(default=25.0, ge=0)  # min % above 52-week low
    min_rs_rating: float = Field(default=70.0, ge=0, le=100)  # relative-strength percentile (0-100)

    @field_validator("universe")
    @classmethod
    def _valid_universe(cls, v: str) -> str:
        return v if v in ("full", "curated") else "full"

    @field_validator("paper_order_type")
    @classmethod
    def _valid_order_type(cls, v: str) -> str:
        return v if v in ("market", "moo", "limit") else "market"

    @property
    def max_price(self) -> float:
        """Highest share price you'd buy: capital x max-position-%."""
        return self.capital * self.max_position_pct / 100


def load_settings() -> ScanSettings:
    if SETTINGS_PATH.exists():
        try:
            return ScanSettings(**json.loads(SETTINGS_PATH.read_text()))
        except (json.JSONDecodeError, ValueError):
            pass
    return ScanSettings()


def save_settings(settings: ScanSettings) -> None:
    SETTINGS_PATH.write_text(settings.model_dump_json(indent=2))
