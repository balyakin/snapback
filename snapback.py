#!/usr/bin/env python3
"""
snapback — incremental project backup with per-file deduplication.

Usage:
    snapback backup  --src ~/Projects --dest /Volumes/Backup/snapback
    snapback list                      --dest /Volumes/Backup/snapback
    snapback list    --project myapp   --dest /Volumes/Backup/snapback
    snapback show    --project myapp --date 2026-05-12  --dest /Volumes/Backup/snapback
    snapback restore --project myapp --date 2026-05-12  --dest /Volumes/Backup/snapback --out ./restored
    snapback restore --project myapp --date 2026-05-12 --file src/main.py --dest /Volumes/Backup/snapback --out ./restored
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Default ignore patterns ─────────────────────────────────────────────────
IGNORE_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", ".git", ".idea",
    ".vscode", ".mypy_cache", ".pytest_cache", ".tox", ".eggs",
    "dist", "build", ".next", ".nuxt", ".output", ".turbo",
    "target",          # Rust / Java
    "Pods",            # iOS CocoaPods
    ".gradle",
    ".DS_Store",
    "env", ".env",
}

IGNORE_EXTENSIONS = {
    ".pyc", ".pyo", ".o", ".so", ".dylib", ".dll", ".class",
}

DB_NAME = "snapback.db"


# ── Helpers ──────────────────────────────────────────────────────────────────

def sha256_file(path: Path, buf_size: int = 1 << 16) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(buf_size):
            h.update(chunk)
    return h.hexdigest()


def should_ignore(path: Path) -> bool:
    parts = path.parts
    for part in parts:
        if part in IGNORE_DIRS:
            return True
    if path.suffix in IGNORE_EXTENSIONS:
        return True
    return False


def scan_project(project_root: Path) -> dict[str, tuple[str, int]]:
    """Return {relative_path: (sha256, size)} for every tracked file."""
    files: dict[str, tuple[str, int]] = {}
    for dirpath, dirnames, filenames in os.walk(project_root):
        # prune ignored dirs in-place so os.walk skips them
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for fname in filenames:
            full = Path(dirpath) / fname
            rel = full.relative_to(project_root)
            if should_ignore(rel):
                continue
            try:
                h = sha256_file(full)
                sz = full.stat().st_size
                files[str(rel)] = (h, sz)
            except (PermissionError, OSError):
                pass
    return files


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id          INTEGER PRIMARY KEY,
            timestamp   TEXT    NOT NULL,
            zip_path    TEXT    NOT NULL,
            files_added INTEGER NOT NULL DEFAULT 0,
            files_del   INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS file_states (
            id          INTEGER PRIMARY KEY,
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
            project     TEXT    NOT NULL,
            rel_path    TEXT    NOT NULL,
            sha256      TEXT    NOT NULL,
            size        INTEGER NOT NULL,
            deleted     INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_fs_proj_path
            ON file_states(project, rel_path, snapshot_id DESC);
        CREATE INDEX IF NOT EXISTS idx_fs_snap
            ON file_states(snapshot_id);

        -- blob dedup: maps sha256 → first zip that stored it
        CREATE TABLE IF NOT EXISTS blobs (
            sha256   TEXT PRIMARY KEY,
            zip_path TEXT NOT NULL,
            zip_key  TEXT NOT NULL       -- entry name inside the zip
        );
    """)
    conn.commit()
    return conn


# ── Backup ───────────────────────────────────────────────────────────────────

def do_backup(src: Path, dest: Path) -> None:
    if not src.is_dir():
        sys.exit(f"Source directory not found: {src}")
    dest.mkdir(parents=True, exist_ok=True)

    db_path = dest / DB_NAME
    conn = init_db(db_path)
    cur = conn.cursor()

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    # ensure unique dir even if called multiple times per second
    snap_dir = dest / "snapshots" / ts
    suffix = 0
    while snap_dir.exists():
        suffix += 1
        snap_dir = dest / "snapshots" / f"{ts}_{suffix}"
    snap_dir.mkdir(parents=True, exist_ok=True)
    zip_path = snap_dir / "data.zip"

    # discover projects (top-level dirs in src)
    projects = sorted(
        p for p in src.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )

    total_added = 0
    total_deleted = 0

    zf = zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6)

    for proj_dir in projects:
        project = proj_dir.name
        current = scan_project(proj_dir)

        # last known state for this project
        prev: dict[str, str] = {}  # rel_path → sha256
        rows = cur.execute("""
            SELECT fs.rel_path, fs.sha256, fs.deleted
            FROM file_states fs
            WHERE fs.project = ?
              AND fs.snapshot_id = (
                  SELECT MAX(fs2.snapshot_id)
                  FROM file_states fs2
                  WHERE fs2.project = fs.project
                    AND fs2.rel_path = fs.rel_path
              )
        """, (project,)).fetchall()
        for rp, h, deleted in rows:
            if not deleted:
                prev[rp] = h

        # ── new / changed files ──
        need_store: list[tuple[str, str, int]] = []  # (rel_path, sha256, size)
        for rp, (h, sz) in current.items():
            if rp not in prev or prev[rp] != h:
                need_store.append((rp, h, sz))

        # ── deleted files ──
        deleted_paths = set(prev.keys()) - set(current.keys())

        if not need_store and not deleted_paths:
            continue  # project unchanged

        # placeholder snapshot id — we'll update later
        # first, store blobs
        for rp, h, sz in need_store:
            blob_row = cur.execute(
                "SELECT zip_path, zip_key FROM blobs WHERE sha256 = ?", (h,)
            ).fetchone()
            if blob_row is None:
                # store in this zip
                zip_key = f"{project}/{rp}"
                zf.write(str(proj_dir / rp), zip_key)
                cur.execute(
                    "INSERT INTO blobs(sha256, zip_path, zip_key) VALUES (?,?,?)",
                    (h, str(zip_path), zip_key),
                )

        total_added += len(need_store)
        total_deleted += len(deleted_paths)

        # we'll write file_states after we have the snapshot id
        # stash them
        if not hasattr(do_backup, "_pending"):
            do_backup._pending = []  # type: ignore[attr-defined]
        for rp, h, sz in need_store:
            do_backup._pending.append((project, rp, h, sz, 0))  # type: ignore[attr-defined]
        for rp in deleted_paths:
            do_backup._pending.append((project, rp, prev[rp], 0, 1))  # type: ignore[attr-defined]

    zf.close()

    if total_added == 0 and total_deleted == 0:
        # nothing changed — remove empty snapshot
        zip_path.unlink(missing_ok=True)
        snap_dir.rmdir()
        print("Nothing changed since last backup.")
        conn.close()
        return

    cur.execute(
        "INSERT INTO snapshots(timestamp, zip_path, files_added, files_del) VALUES (?,?,?,?)",
        (ts, str(zip_path), total_added, total_deleted),
    )
    snap_id = cur.lastrowid

    for project, rp, h, sz, deleted in do_backup._pending:  # type: ignore[attr-defined]
        cur.execute(
            "INSERT INTO file_states(snapshot_id, project, rel_path, sha256, size, deleted) VALUES (?,?,?,?,?,?)",
            (snap_id, project, rp, h, sz, deleted),
        )
    do_backup._pending = []  # type: ignore[attr-defined]

    conn.commit()
    conn.close()

    zip_size = zip_path.stat().st_size
    print(f"Snapshot {ts}")
    print(f"  added/changed : {total_added}")
    print(f"  deleted marks : {total_deleted}")
    print(f"  zip size      : {zip_size / 1024:.1f} KB")


# ── List snapshots / projects ────────────────────────────────────────────────

def do_list(dest: Path, project: Optional[str]) -> None:
    conn = init_db(dest / DB_NAME)
    cur = conn.cursor()

    if project is None:
        # list all projects + snapshot count
        rows = cur.execute("""
            SELECT DISTINCT project FROM file_states ORDER BY project
        """).fetchall()
        if not rows:
            print("No backups yet.")
            return
        print("Projects:")
        for (p,) in rows:
            cnt = cur.execute(
                "SELECT COUNT(DISTINCT snapshot_id) FROM file_states WHERE project=?", (p,)
            ).fetchone()[0]
            print(f"  {p}  ({cnt} snapshots)")
    else:
        rows = cur.execute("""
            SELECT s.id, s.timestamp, s.files_added, s.files_del
            FROM snapshots s
            WHERE s.id IN (SELECT DISTINCT snapshot_id FROM file_states WHERE project=?)
            ORDER BY s.timestamp
        """, (project,)).fetchall()
        if not rows:
            print(f"No snapshots for project '{project}'.")
            return
        print(f"Snapshots for '{project}':")
        for sid, ts, added, deleted in rows:
            print(f"  {ts}  +{added} -{deleted}")
    conn.close()


# ── Show: list files in a project at a given date ───────────────────────────

def do_show(dest: Path, project: str, date_str: str) -> None:
    conn = init_db(dest / DB_NAME)
    cur = conn.cursor()

    # find latest snapshot ≤ date
    snap_id = _resolve_snapshot(cur, project, date_str)
    if snap_id is None:
        print(f"No snapshot found for '{project}' on or before {date_str}.")
        conn.close()
        return

    # reconstruct file tree at that snapshot
    files = _reconstruct_tree(cur, project, snap_id)
    print(f"Files in '{project}' as of snapshot ≤ {date_str}  ({len(files)} files):")
    for rp in sorted(files):
        print(f"  {rp}")
    conn.close()


# ── Restore ──────────────────────────────────────────────────────────────────

def do_restore(
    dest: Path,
    project: str,
    date_str: str,
    out: Path,
    single_file: Optional[str],
) -> None:
    conn = init_db(dest / DB_NAME)
    cur = conn.cursor()

    snap_id = _resolve_snapshot(cur, project, date_str)
    if snap_id is None:
        sys.exit(f"No snapshot found for '{project}' on or before {date_str}.")

    tree = _reconstruct_tree(cur, project, snap_id)

    if single_file:
        if single_file not in tree:
            sys.exit(f"File '{single_file}' not found in project at that date.")
        tree = {single_file: tree[single_file]}

    out.mkdir(parents=True, exist_ok=True)
    restored = 0

    for rp, sha in tree.items():
        blob_row = cur.execute(
            "SELECT zip_path, zip_key FROM blobs WHERE sha256=?", (sha,)
        ).fetchone()
        if blob_row is None:
            print(f"  WARNING: blob missing for {rp} (sha256={sha[:12]}…)")
            continue
        zpath, zkey = blob_row
        target = out / rp
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zpath, "r") as zf:
            data = zf.read(zkey)
        target.write_bytes(data)
        restored += 1

    print(f"Restored {restored} file(s) → {out}")
    conn.close()


# ── Internal helpers ─────────────────────────────────────────────────────────

def _resolve_snapshot(
    cur: sqlite3.Cursor, project: str, date_str: str
) -> Optional[int]:
    """Find the latest snapshot id for `project` whose timestamp starts with `date_str` or is ≤ it."""
    # If user passes just a date like 2026-05-12, match any snapshot that day
    # otherwise treat as prefix / upper bound
    row = cur.execute("""
        SELECT s.id FROM snapshots s
        WHERE s.id IN (SELECT DISTINCT snapshot_id FROM file_states WHERE project=?)
          AND s.timestamp <= ? || '~'
        ORDER BY s.timestamp DESC LIMIT 1
    """, (project, date_str)).fetchone()
    return row[0] if row else None


def _reconstruct_tree(
    cur: sqlite3.Cursor, project: str, up_to_snap: int
) -> dict[str, str]:
    """Replay file_states up to `up_to_snap` and return {rel_path: sha256} of live files."""
    rows = cur.execute("""
        SELECT rel_path, sha256, deleted
        FROM file_states
        WHERE project = ? AND snapshot_id <= ?
        ORDER BY snapshot_id ASC, id ASC
    """, (project, up_to_snap)).fetchall()
    tree: dict[str, str] = {}
    for rp, sha, deleted in rows:
        if deleted:
            tree.pop(rp, None)
        else:
            tree[rp] = sha
    return tree


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        prog="snapback",
        description="Incremental project backups with per-file dedup into zip archives.",
    )
    sub = p.add_subparsers(dest="cmd")

    # backup
    bp = sub.add_parser("backup", help="Create a new snapshot")
    bp.add_argument("--src", required=True, type=Path, help="Root directory with projects")
    bp.add_argument("--dest", required=True, type=Path, help="Backup destination (external drive)")

    # list
    lp = sub.add_parser("list", help="List projects or snapshots")
    lp.add_argument("--dest", required=True, type=Path)
    lp.add_argument("--project", type=str, default=None)

    # show
    sp = sub.add_parser("show", help="Show files in a project at a date")
    sp.add_argument("--dest", required=True, type=Path)
    sp.add_argument("--project", required=True)
    sp.add_argument("--date", required=True, help="Date prefix, e.g. 2026-05-12")

    # restore
    rp = sub.add_parser("restore", help="Restore a project (or a single file) at a date")
    rp.add_argument("--dest", required=True, type=Path)
    rp.add_argument("--project", required=True)
    rp.add_argument("--date", required=True)
    rp.add_argument("--file", default=None, help="Restore a single file (relative path)")
    rp.add_argument("--out", required=True, type=Path, help="Where to restore to")

    args = p.parse_args()

    if args.cmd == "backup":
        do_backup(args.src, args.dest)
    elif args.cmd == "list":
        do_list(args.dest, args.project)
    elif args.cmd == "show":
        do_show(args.dest, args.project, args.date)
    elif args.cmd == "restore":
        do_restore(args.dest, args.project, args.date, args.out, args.file)
    else:
        p.print_help()


if __name__ == "__main__":
    do_backup._pending = []  # type: ignore[attr-defined]
    main()
