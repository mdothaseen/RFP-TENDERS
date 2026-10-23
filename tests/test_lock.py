"""The run lock blocks a second scan only while the first one is really running."""

import os
import subprocess
import sys
import time
import pytest

from tender_tracker import main


@pytest.fixture(autouse=True)
def release_test_lock(monkeypatch):
    main._release_lock()
    yield
    main._release_lock()


def test_lock_of_a_closed_window_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LOCK", tmp_path / "run.lock")
    assert not main._run_in_progress()

    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # a scan whose window was closed
    proc.wait()
    main.LOCK.write_text(f"{proc.pid} 2026-10-03T10:00:00", encoding="utf-8")
    assert not main._run_in_progress()
    assert main._acquire_lock()  # the stale lock is taken over
    assert main.LOCK.read_text(encoding="utf-8").split()[0] == str(os.getpid())


def test_lock_of_a_running_scan_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LOCK", tmp_path / "run.lock")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        main.LOCK.write_text(f"{proc.pid} 2026-10-03T10:00:00", encoding="utf-8")
        assert main._run_in_progress()
        assert not main._acquire_lock()
        old = time.time() - 4 * 3600  # Age cannot override a process that is still alive.
        os.utime(main.LOCK, (old, old))
        assert main._run_in_progress()
    finally:
        proc.kill()
        proc.wait()
