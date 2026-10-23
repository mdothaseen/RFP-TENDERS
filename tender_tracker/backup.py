"""Consistent SQLite snapshots + deduplicated file blobs, verification and restore to a new folder."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

_SNAPSHOT = re.compile(r"snapshot-\d{8}T\d{6}-[a-f0-9]{8}$")


def sha(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def inside(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    target.relative_to(root.resolve())
    return target


def create_backup(store, root: Path, destination: Path, keep: int = 7) -> Path:
    root, destination = root.resolve(), destination.resolve()
    if destination == root or destination in root.parents:
        raise ValueError("Backup directory must not be the project root or its parent")
    assets = destination / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    snapshot = destination / f"snapshot-{datetime.now():%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"
    snapshot.mkdir()
    database = snapshot / "tracker.db"
    store.commit()
    with closing(sqlite3.connect(str(database))) as target:
        store.db.backup(target)
    paths = set()
    with closing(sqlite3.connect(str(database))) as source:
        for local, text in source.execute("SELECT local_path,text_path FROM items"):
            paths.update(p for p in (local, text) if p)
        paths.update(r[0] for r in source.execute("SELECT local_path FROM document_versions WHERE local_path IS NOT NULL"))
    for pattern in ("config/*.yaml", "data/ocr_cache/*.txt", "data/tessdata/*.traineddata"):
        paths.update(p.relative_to(root).as_posix() for p in root.glob(pattern))
    files = []
    for relative in sorted(paths):
        path = inside(root, relative)
        actual = path.relative_to(root).as_posix()
        if path.name.startswith(".env") or not actual.startswith(("output/", "data/text/", "data/ocr_cache/", "data/tessdata/", "config/")):
            raise ValueError(f"Unexpected backup asset: {actual}")
        if not path.is_file():
            raise FileNotFoundError(f"Backup incomplete: missing {actual}")
        digest = sha(path)
        blob = assets / digest
        if not blob.exists() or sha(blob) != digest:
            temporary = assets / f".{digest}.{uuid4().hex}.tmp"
            shutil.copyfile(path, temporary)
            if sha(temporary) != digest:
                raise RuntimeError(f"File changed during backup: {actual}; retry backup")
            temporary.replace(blob)
        files.append({"path": actual, "sha256": digest, "size": path.stat().st_size})
    manifest = {"format": 1, "created": datetime.now().isoformat(timespec="seconds"),
                "database_sha256": sha(database), "files": files}
    manifest_path = snapshot / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (snapshot / "manifest.sha256").write_text(sha(manifest_path), encoding="ascii")
    verify_backup(snapshot)
    store.kv_set("last_backup_at", manifest["created"])
    store.kv_set("last_backup_path", str(snapshot))
    prune(destination, max(1, keep))
    return snapshot


def verify_backup(snapshot: Path) -> dict:
    snapshot = snapshot.resolve()
    manifest_path = snapshot / "manifest.json"
    if sha(manifest_path) != (snapshot / "manifest.sha256").read_text(encoding="ascii").strip():
        raise ValueError("Backup manifest checksum mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or sha(snapshot / "tracker.db") != manifest["database_sha256"]:
        raise ValueError("Backup database checksum mismatch or unsupported format")
    with closing(sqlite3.connect(f"{(snapshot / 'tracker.db').as_uri()}?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Backup database failed SQLite integrity check")
    checked = set()
    for asset in manifest["files"]:
        inside(snapshot, asset["path"])  # Reject absolute/traversal paths before any restore.
        digest = asset["sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid asset hash")
        if digest not in checked:
            if sha(snapshot.parent / "assets" / digest) != digest:
                raise ValueError(f"Backup asset checksum mismatch: {asset['path']}")
            checked.add(digest)
    return manifest


def restore_backup(snapshot: Path, target: Path) -> None:
    manifest = verify_backup(snapshot)
    target = target.resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError("Restore target must be a new or empty directory; existing project is never overwritten")
    target.mkdir(parents=True, exist_ok=True)
    (target / "data").mkdir(exist_ok=True)
    shutil.copyfile(snapshot / "tracker.db", target / "data" / "tracker.db")
    for asset in manifest["files"]:
        path = inside(target, asset["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(snapshot.parent / "assets" / asset["sha256"], path)


def prune(destination: Path, keep: int) -> None:
    """Only files produced by this backup format are removed; unexpected content is preserved."""
    snapshots = sorted(p for p in destination.iterdir() if p.is_dir() and not p.is_symlink()
                       and _SNAPSHOT.fullmatch(p.name) and (p / "manifest.sha256").is_file())
    for old in snapshots[:-keep]:
        if {p.name for p in old.iterdir()} != {"manifest.json", "manifest.sha256", "tracker.db"}:
            continue
        old.resolve().relative_to(destination.resolve())
        for name in ("manifest.json", "manifest.sha256", "tracker.db"):
            inside(destination, f"{old.name}/{name}").unlink()
        old.rmdir()
    # Blobs are retained: document history and off-site sync may still refer to them.
