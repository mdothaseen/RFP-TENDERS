"""Small operating-system helpers used by a scan: stay awake, check the internet."""

from __future__ import annotations

import logging
import socket
import sys

log = logging.getLogger(__name__)


def keep_awake(on: bool) -> None:
    """Stop Windows from going to sleep while a scan runs (closing the lid can still sleep the laptop)."""
    if sys.platform != "win32":
        return
    import ctypes

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except (AttributeError, OSError) as exc:
        log.debug("Could not change the sleep setting: %s", exc)


def internet_ok() -> bool:
    for host in ("www.google.com", "ncvet.gov.in", "1.1.1.1"):
        try:
            socket.create_connection((host, 443), timeout=8).close()
            return True
        except OSError:
            continue
    return False
