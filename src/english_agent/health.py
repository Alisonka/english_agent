"""Liveness for container healthchecks: a file touched while the event loop is alive.

    python -m english_agent.health /tmp/heartbeat 120   # exit 0 if touched in the last 120 s

A blocked or dead event loop stops touching it, and Docker restarts the container.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

HEARTBEAT = Path("/tmp/heartbeat")
INTERVAL_SECONDS = 30


async def beat(path: Path = HEARTBEAT, interval: float = INTERVAL_SECONDS) -> None:
    """Touch `path` every `interval` seconds until cancelled."""
    while True:
        try:
            path.touch()
        except OSError as error:  # a read-only filesystem must not take the process down
            log.warning("heartbeat %s: %s", path, error)
        await asyncio.sleep(interval)


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else HEARTBEAT
    max_age = float(argv[2]) if len(argv) > 2 else 4 * INTERVAL_SECONDS
    try:
        return 0 if time.time() - path.stat().st_mtime < max_age else 1
    except OSError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
