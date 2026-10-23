"""Started by Windows when a button in the tracker's popup is clicked (ssctracker://scan or ssctracker://skip)."""

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
# pythonw has no console: keep stray output away, and write crashes to a log file.
if sys.stdout is None or sys.stderr is None:
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")

link = sys.argv[1] if len(sys.argv) > 1 else ""
if "scan" in link.lower():
    try:
        from tender_tracker.main import main
        raise SystemExit(main(["--from-popup"]))
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        (ROOT / "logs").mkdir(exist_ok=True)
        with open(ROOT / "logs" / "popup_errors.log", "a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc() + "\n")
        raise
