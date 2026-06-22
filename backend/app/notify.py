"""Push notifications via ntfy (https://ntfy.sh) — dead-simple phone push.

Setup: pick an unguessable topic, put NTFY_TOPIC in .env, install the ntfy app (iOS/Android),
and subscribe to that topic. The backend POSTs the message to <NTFY_SERVER>/<topic> and it lands
on your phone. No topic set -> no-op (so it's safe to leave unconfigured). Self-host ntfy on the Pi
later and point NTFY_SERVER at it; nothing else changes.

Fire-and-forget on a daemon thread so it never blocks the scan/monitor loops (callers are a mix of
sync and async).
"""

import logging
import os
import threading

import httpx

log = logging.getLogger(__name__)


def _topic() -> str:
    return os.environ.get("NTFY_TOPIC", "").strip()


def _server() -> str:
    return os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


def send(message: str, title: str = "Bellwether", tags: str = "", priority: str = "default") -> None:
    """Push a notification to the configured ntfy topic. No-op if NTFY_TOPIC is unset."""
    topic = _topic()
    if not topic:
        return
    url, headers = f"{_server()}/{topic}", {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags  # ntfy renders these as emoji (e.g. "rocket,chart")

    def _post() -> None:
        try:
            httpx.post(url, content=message.encode("utf-8"), headers=headers, timeout=8)
        except Exception:
            log.warning("ntfy push failed", exc_info=True)

    threading.Thread(target=_post, daemon=True).start()
