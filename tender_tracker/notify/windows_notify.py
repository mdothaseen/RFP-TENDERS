"""Windows 10/11 popup notification; clicking it opens the Excel file."""

from __future__ import annotations

import sys
from pathlib import Path


def send(digest, excel: Path | None) -> str:
    if sys.platform != "win32":
        return "skipped (not Windows)"
    from winotify import Notification, audio

    toast = Notification(
        app_id="SSC Tender Tracker",
        title="SSC Tender Tracker",
        msg=digest.toast[:250],
        duration="long",
        launch=Path(excel).resolve().as_uri() if excel else "",
    )
    toast.set_audio(audio.Default, loop=False)
    if excel:
        toast.add_actions(label="Excel kholo", launch=Path(excel).resolve().as_uri())
    toast.show()
    return "shown"


PROTOCOL = "ssctracker"


def ask_to_scan(last_run: str, sites: int) -> str:
    """Hourly question: 'Scan now?' with Yes / Not now buttons (buttons open ssctracker:// links)."""
    if sys.platform != "win32":
        return "skipped (not Windows)"
    from winotify import Notification, audio

    toast = Notification(
        app_id="SSC Tender Tracker",
        title="SSC websites scan karun?",
        msg=f"Tenders / RFP / EOI ke liye {sites} websites check hongi (lagbhag 25-30 min).\nPichhla scan: {last_run}",
        duration="long",
    )
    toast.set_audio(audio.Default, loop=False)
    toast.add_actions(label="Haan, scan karo", launch=f"{PROTOCOL}://scan")
    toast.add_actions(label="Abhi nahi", launch=f"{PROTOCOL}://skip")
    toast.show()
    return "asked"


def say(title: str, message: str) -> None:
    if sys.platform != "win32":
        return
    from winotify import Notification

    Notification(app_id="SSC Tender Tracker", title=title, msg=message[:250]).show()
