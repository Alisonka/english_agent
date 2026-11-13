"""Operational alerts: always logged at ERROR, also sent to ADMIN_CHAT_ID when it is set.

The same kind of alert is sent at most once per `quiet_seconds` per process, so an outage that
fails every job produces one message, not a thousand.
"""

from __future__ import annotations

import html
import logging
import time

from english_agent.metrics import ALERTS
from english_agent.telegram.sender import Sender, TelegramError

log = logging.getLogger(__name__)


class Alerter:
    def __init__(self, sender: Sender | None, admin_chat_id: str, *, quiet_seconds: float = 600) -> None:
        self._sender, self._chat, self._quiet = sender, admin_chat_id, quiet_seconds
        self._last: dict[str, float] = {}

    async def alert(self, kind: str, text: str) -> None:
        ALERTS.labels(kind).inc()
        log.error("alert %s: %s", kind, text)
        if self._sender is None or not self._chat:
            return
        now = time.monotonic()
        if now - self._last.get(kind, float("-inf")) < self._quiet:
            return
        self._last[kind] = now
        try:
            await self._sender.send_text(self._chat, f"⚠️ <b>{html.escape(kind)}</b>\n{html.escape(text[:3000])}")
        except TelegramError as error:
            log.warning("could not send the alert: %s", error)
