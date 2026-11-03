"""The run lock blocks a second scan only while the first one is really running."""

import os
import subprocess
import sys
import time
import pytest

from tender_tracker import locking


@pytest.fixture(autouse=True)
def release_test_lock(monkeypatch):
    locking.release()
    yield
    locking.release()


def test_lock_of_a_closed_window_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(locking, "LOCK", tmp_path / "run.lock")
    assert not locking.run_in_progress()

    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # a scan whose window was closed
    proc.wait()
    locking.LOCK.write_text(f"{proc.pid} 2026-10-03T10:00:00", encoding="utf-8")
    assert not locking.run_in_progress()
    assert locking.acquire()  # the stale lock is taken over
    assert locking.LOCK.read_text(encoding="utf-8").split()[0] == str(os.getpid())


def test_lock_of_a_running_scan_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(locking, "LOCK", tmp_path / "run.lock")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        locking.LOCK.write_text(f"{proc.pid} 2026-10-03T10:00:00", encoding="utf-8")
        assert locking.run_in_progress()
        assert not locking.acquire()
        old = time.time() - 4 * 3600  # Age cannot override a process that is still alive.
        os.utime(locking.LOCK, (old, old))
        assert locking.run_in_progress()
    finally:
        proc.kill()
        proc.wait()
