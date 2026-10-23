"""Isolated native OCR. The parent can terminate a slow/crashed job without blocking the tracker."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def read_pdf(path: Path, tessdata: Path, max_pages: int, language: str, output: Path) -> None:
    import pymupdf

    parts = []

    def save(complete: bool, error: str = "") -> None:
        pending = output.with_suffix(".tmp")
        pending.write_text(json.dumps({"text": "\n".join(parts), "complete": complete, "error": error}),
                           encoding="utf-8")
        pending.replace(output)

    try:
        with pymupdf.open(path) as doc:
            for number in range(min(doc.page_count, max_pages)):
                page = doc[number]
                textpage = page.get_textpage_ocr(language=language, dpi=200, full=True, tessdata=str(tessdata))
                parts.append(page.get_text(textpage=textpage))
                save(False)  # The parent can recover completed pages even when a later page times out.
        save(True)
    except Exception as exc:
        save(False, str(exc)[:300])
        raise


if __name__ == "__main__":
    source, tessdata_dir, pages, lang, destination = sys.argv[1:]
    read_pdf(Path(source), Path(tessdata_dir), int(pages), lang, Path(destination))
