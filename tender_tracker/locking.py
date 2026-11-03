"""OS-owned nonblocking locks; a shutdown releases them without stale-age guesses.

The scan lock is a pair: `<name>.guard` is the OS lock, `<name>` holds the owner's PID for the hourly popup.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger(__name__)
LOCK = DATA_DIR / "run.lock"


class RunLock:
    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.file = handle
        return True

    def release(self) -> None:
        if self.file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


_guard: RunLock | None = None
_held: Path | None = None


def pid_alive(pid: int) -> bool:
    """True if a Python process with this PID is running (Windows reuses PIDs for other programs)."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not (kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259):  # STILL_ACTIVE
            return False
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower().startswith("python")
        return True
    finally:
        kernel32.CloseHandle(handle)


def held_path() -> Path | None:
    """The lock this process holds, if any."""
    return _held


def run_in_progress(path: Path | None = None) -> bool:
    """True while another run holds the lock. A lock left behind by a closed window or a crash is ignored."""
    path = path or LOCK
    if _guard is not None and _held == path:
        return True
    try:
        pid = int(path.read_text(encoding="utf-8").split()[0])
        if pid != os.getpid() and pid_alive(pid):
            return True
    except FileNotFoundError:
        pass
    except (OSError, ValueError, IndexError) as exc:
        log.debug("Unreadable lock file %s: %s", path, exc)
    probe = RunLock(path.with_suffix(".guard"))
    if not probe.acquire():
        return True
    probe.release()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("Could not remove stale lock %s: %s", path, exc)
    return False


def acquire(path: Path | None = None) -> bool:
    global _guard, _held
    path = path or LOCK
    if _guard is not None or run_in_progress(path):
        return False
    guard = RunLock(path.with_suffix(".guard"))
    if not guard.acquire():
        return False
    path.write_text(f"{os.getpid()} {datetime.now().isoformat()}", encoding="utf-8")
    _guard, _held = guard, path
    return True


def release() -> None:
    global _guard, _held
    try:
        if _guard is not None:
            _held.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("Could not remove lock %s: %s", _held, exc)
    finally:
        if _guard is not None:
            _guard.release()
        _guard, _held = None, None
