"""Live price streaming from Yahoo's websocket (free, no API key).

Holds ONE persistent Yahoo websocket connection and tracks the latest streamed
price for a small, changing set of tickers — the setups currently on screen. The
scan still pulls daily bars the usual way; this only makes the displayed prices
tick in real time. Yahoo's feed is unofficial, same caveats as the rest of
yfinance: it can lag on thin names or drop, and we just keep the last value.

The frontend polls /api/live over localhost for the latest map; the actual
streaming (the part that matters) happens here, backend <-> Yahoo.
"""

import asyncio
import logging

import yfinance as yf

log = logging.getLogger(__name__)

# Defensive cap — we only ever stream the handful of displayed cards.
MAX_SYMBOLS = 50


class LiveStream:
    """A single Yahoo websocket whose subscription set we swap as cards change."""

    def __init__(self) -> None:
        self._ws: yf.AsyncWebSocket | None = None
        self._listen_task: asyncio.Task | None = None
        self._symbols: set[str] = set()
        self._latest: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def _handler(self, msg: dict) -> None:
        sym, price = msg.get("id"), msg.get("price")
        if sym and price is not None:
            self._latest[sym] = {
                "price": price,
                "change_percent": msg.get("change_percent"),
                "time": msg.get("time"),
            }

    async def _listen(self) -> None:
        try:
            await self._ws.listen(self._handler)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Live stream listen loop ended unexpectedly")

    async def set_symbols(self, symbols: list[str]) -> int:
        """Stream exactly this set of tickers (subscribe new, drop the rest)."""
        async with self._lock:
            wanted = {s for s in symbols if s}
            if len(wanted) > MAX_SYMBOLS:
                wanted = set(list(wanted)[:MAX_SYMBOLS])
            if not wanted:
                return 0

            # First subscriber: connect + subscribe BEFORE starting the listen
            # loop so the two coroutines don't race to open the connection.
            if self._ws is None:
                self._ws = yf.AsyncWebSocket(verbose=False)
                await self._ws.subscribe(list(wanted))
                self._listen_task = asyncio.create_task(self._listen())
                self._symbols = wanted
                return len(wanted)

            add = wanted - self._symbols
            remove = self._symbols - wanted
            if add:
                await self._ws.subscribe(list(add))
            if remove:
                await self._ws.unsubscribe(list(remove))
                for s in remove:
                    self._latest.pop(s, None)
            self._symbols = wanted
            return len(wanted)

    def prices(self) -> dict[str, dict]:
        return dict(self._latest)

    async def stop(self) -> None:
        async with self._lock:
            if self._listen_task:
                self._listen_task.cancel()
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    log.exception("Error closing live stream")
            self._ws = None
            self._listen_task = None
            self._symbols = set()
            self._latest = {}


live = LiveStream()
