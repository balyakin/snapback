#!/usr/bin/env python3
"""snapback - incremental project backup with per-file deduplication"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

IGNORE_FILE_NAME = "snapback.ignore"
DB_NAME = "snapback.db"
SNAPSHOT_DIR_NAME = "snapshots"
SNAPSHOT_ZIP_NAME = "data.zip"
SNAPSHOT_DATE_FORMAT = "%Y-%m-%d_%H-%M-%S"
ZIP_COMPRESSION_LEVEL = 6
HASH_BUFFER_SIZE = 1 << 16

DEFAULT_IGNORE_NAMES = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    ".idea",
    ".vscode",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".eggs",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".output",
    ".turbo",
    "target",
    "Pods",
    ".gradle",
    ".DS_Store",
    "env",
    ".env",
}

DEFAULT_IGNORE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".o",
    ".so",
    ".dylib",
    ".dll",
    ".class",
}


class _BackupReadError(Exception):
    pass


class _FileState(NamedTuple):
    project: str
    rel_path: str
    sha256: str
    size: int
    deleted: int


class _NewBlob(NamedTuple):
    sha256: str
    source_path: Path
    zip_key: str


class _BackupPlan(NamedTuple):
    file_states: List[_FileState]
    new_blobs: List[_NewBlob]
    files_added: int
    files_deleted: int


class _SnapshotPaths(NamedTuple):
    snapshot_name: str
    snapshot_dir: Path
    zip_path: Path


def sha256_file(path: Path, buf_size: int = HASH_BUFFER_SIZE) -> str:
    """Calculate SHA-256 for a file

    Args:
        path: File path.
        buf_size: Read buffer size.

    Returns:
        Hex encoded SHA-256 digest.

    Raises:
        OSError: If the file cannot be read
    """
    digest = hashlib.sha256()

    with open(str(path), "rb") as source_file:
        chunk = source_file.read(buf_size)
        while chunk:
            digest.update(chunk)
            chunk = source_file.read(buf_size)

    return digest.hexdigest()


def should_ignore(path: Path, ignore_names: Set[str], ignore_extensions: Set[str]) -> bool:
    """Check whether a relative path must be ignored

    Args:
        path: Relative path inside a project.
        ignore_names: Ignored file or directory names.
        ignore_extensions: Ignored file suffixes.

    Returns:
        True when the path should be skipped, otherwise False
    """
    for path_part in path.parts:
        if path_part in ignore_names:
            return True

    for extension in ignore_extensions:
        if path.name.endswith(extension):
            return True

    return False


def scan_project(
    project_root: Path,
    ignore_names: Set[str],
    ignore_extensions: Set[str],
) -> Dict[str, Tuple[str, int]]:
    """Scan a project and return current file hashes

    Args:
        project_root: Project directory.
        ignore_names: Ignored file or directory names.
        ignore_extensions: Ignored file suffixes.

    Returns:
        Mapping from relative path to SHA-256 and file size.

    Raises:
        _BackupReadError: If any project file cannot be read
    """
    files: Dict[str, Tuple[str, int]] = {}

    for dirpath, dirnames, filenames in os.walk(str(project_root)):
        dirnames[:] = sorted(
            dirname for dirname in dirnames
            if dirname not in ignore_names
        )

        for file_name in sorted(filenames):
            file_path = Path(dirpath) / file_name
            rel_path = file_path.relative_to(project_root)

            if should_ignore(rel_path, ignore_names, ignore_extensions):
                continue

            if file_path.is_symlink():
                continue

            try:
                file_hash = sha256_file(file_path)
                file_size = file_path.stat().st_size
            except OSError as error:
                message = "Cannot read file during backup: {}".format(file_path)
                raise _BackupReadError(message) from error

            files[str(rel_path)] = (file_hash, file_size)

    return files


def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize the SQLite database

    Args:
        db_path: SQLite database path.

    Returns:
        Open SQLite connection
    """
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
        CREATE TABLE IF NOT EXISTS blobs (
            sha256   TEXT PRIMARY KEY,
            zip_path TEXT NOT NULL,
            zip_key  TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


def do_backup(src: Path, dest: Path) -> None:
    """Create a new backup snapshot

    Args:
        src: Root directory that contains project directories.
        dest: Backup destination directory.

    Returns:
        None
    """
    source_path = src.expanduser()
    destination_path = dest.expanduser()

    if not source_path.is_dir():
        sys.exit("Source directory not found: {}".format(source_path))

    source_path = source_path.resolve()
    destination_path = destination_path.resolve()

    if _is_relative_to(destination_path, source_path):
        sys.exit("Backup destination must not be inside the source directory: {}".format(destination_path))

    destination_path.mkdir(parents=True, exist_ok=True)

    conn = init_db(destination_path / DB_NAME)
    try:
        ignore_rules = _load_ignore_rules(destination_path)
        backup_plan = _build_backup_plan(source_path, conn.cursor(), ignore_rules)

        if backup_plan.files_added == 0 and backup_plan.files_deleted == 0:
            _write_line("Nothing changed since last backup.")
            return

        snapshot_paths = _create_snapshot_paths(destination_path)

        try:
            _write_new_blobs(backup_plan.new_blobs, snapshot_paths.zip_path)
            _save_snapshot(conn.cursor(), snapshot_paths, backup_plan)
            conn.commit()
        except Exception:
            conn.rollback()
            _remove_snapshot_dir(snapshot_paths.snapshot_dir)
            raise

        zip_size = snapshot_paths.zip_path.stat().st_size
        zip_size_kb = zip_size / 1024

        _write_line("Snapshot {}".format(snapshot_paths.snapshot_name))
        _write_line("  added/changed : {}".format(backup_plan.files_added))
        _write_line("  deleted marks : {}".format(backup_plan.files_deleted))
        _write_line("  zip size      : {:.1f} KB".format(zip_size_kb))
    except _BackupReadError as error:
        conn.rollback()
        sys.exit(str(error))
    finally:
        conn.close()


def do_list(dest: Path, project: Optional[str]) -> None:
    """List backed up projects or snapshots

    Args:
        dest: Backup destination directory.
        project: Optional project name.

    Returns:
        None
    """
    conn = init_db(dest.expanduser() / DB_NAME)
    cur = conn.cursor()

    try:
        if project is None:
            rows = cur.execute("""
                SELECT DISTINCT project FROM file_states ORDER BY project
            """).fetchall()

            if not rows:
                _write_line("No backups yet.")
                return

            _write_line("Projects:")
            for row in rows:
                project_name = row[0]
                count_row = cur.execute(
                    "SELECT COUNT(DISTINCT snapshot_id) FROM file_states WHERE project = ?",
                    (project_name,),
                ).fetchone()
                snapshot_count = count_row[0]
                _write_line("  {}  ({} snapshots)".format(project_name, snapshot_count))
            return

        rows = cur.execute("""
            SELECT s.id, s.timestamp, s.files_added, s.files_del
            FROM snapshots s
            WHERE s.id IN (SELECT DISTINCT snapshot_id FROM file_states WHERE project = ?)
            ORDER BY s.timestamp
        """, (project,)).fetchall()

        if not rows:
            _write_line("No snapshots for project '{}'.".format(project))
            return

        _write_line("Snapshots for '{}':".format(project))
        for row in rows:
            timestamp = row[1]
            files_added = row[2]
            files_deleted = row[3]
            _write_line("  {}  +{} -{}".format(timestamp, files_added, files_deleted))
    finally:
        conn.close()


def do_show(dest: Path, project: str, date_str: str) -> None:
    """Show files in a project at a date

    Args:
        dest: Backup destination directory.
        project: Project name.
        date_str: Date prefix or full snapshot timestamp.

    Returns:
        None
    """
    conn = init_db(dest.expanduser() / DB_NAME)
    cur = conn.cursor()

    try:
        snapshot_id = _resolve_snapshot(cur, project, date_str)
        if snapshot_id is None:
            _write_line("No snapshot found for '{}' on or before {}.".format(project, date_str))
            return

        files = _reconstruct_tree(cur, project, snapshot_id)
        _write_line("Files in '{}' as of snapshot <= {}  ({} files):".format(project, date_str, len(files)))

        for rel_path in sorted(files):
            _write_line("  {}".format(rel_path))
    finally:
        conn.close()


def do_restore(
    dest: Path,
    project: str,
    date_str: str,
    out: Path,
    single_file: Optional[str],
) -> None:
    """Restore a project or a single file from a snapshot

    Args:
        dest: Backup destination directory.
        project: Project name.
        date_str: Date prefix or full snapshot timestamp.
        out: Output directory.
        single_file: Optional relative file path to restore.

    Returns:
        None
    """
    conn = init_db(dest.expanduser() / DB_NAME)
    cur = conn.cursor()

    try:
        snapshot_id = _resolve_snapshot(cur, project, date_str)
        if snapshot_id is None:
            sys.exit("No snapshot found for '{}' on or before {}.".format(project, date_str))

        tree = _reconstruct_tree(cur, project, snapshot_id)

        if single_file:
            if single_file not in tree:
                sys.exit("File '{}' not found in project at that date.".format(single_file))

            tree = {single_file: tree[single_file]}

        output_path = out.expanduser()
        output_path.mkdir(parents=True, exist_ok=True)
        restored_count = 0

        for rel_path in sorted(tree):
            file_hash = tree[rel_path]
            blob_row = cur.execute(
                "SELECT zip_path, zip_key FROM blobs WHERE sha256 = ?",
                (file_hash,),
            ).fetchone()

            if blob_row is None:
                _write_line("  WARNING: blob missing for {} (sha256={})".format(rel_path, file_hash[:12]))
                continue

            zip_path = blob_row[0]
            zip_key = blob_row[1]
            target_path = output_path / rel_path
            target_path.parent.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(zip_path, "r") as zip_archive:
                data = zip_archive.read(zip_key)

            target_path.write_bytes(data)
            restored_count += 1

        _write_line("Restored {} file(s) -> {}".format(restored_count, output_path))
    finally:
        conn.close()


def main() -> None:
    """Run the command-line interface

    Returns:
        None
    """
    parser = argparse.ArgumentParser(
        prog="snapback",
        description="Incremental project backups with per-file dedup into zip archives.",
    )
    subparsers = parser.add_subparsers(dest="cmd")

    backup_parser = subparsers.add_parser("backup", help="Create a new snapshot")
    backup_parser.add_argument("--src", required=True, type=Path, help="Root directory with projects")
    backup_parser.add_argument("--dest", required=True, type=Path, help="Backup destination")

    list_parser = subparsers.add_parser("list", help="List projects or snapshots")
    list_parser.add_argument("--dest", required=True, type=Path)
    list_parser.add_argument("--project", type=str, default=None)

    show_parser = subparsers.add_parser("show", help="Show files in a project at a date")
    show_parser.add_argument("--dest", required=True, type=Path)
    show_parser.add_argument("--project", required=True)
    show_parser.add_argument("--date", required=True, help="Date prefix, e.g. 2026-05-12")

    restore_parser = subparsers.add_parser("restore", help="Restore a project or a single file at a date")
    restore_parser.add_argument("--dest", required=True, type=Path)
    restore_parser.add_argument("--project", required=True)
    restore_parser.add_argument("--date", required=True)
    restore_parser.add_argument("--file", default=None, help="Restore a single file")
    restore_parser.add_argument("--out", required=True, type=Path, help="Where to restore to")

    args = parser.parse_args()

    if args.cmd == "backup":
        do_backup(args.src, args.dest)
        return

    if args.cmd == "list":
        do_list(args.dest, args.project)
        return

    if args.cmd == "show":
        do_show(args.dest, args.project, args.date)
        return

    if args.cmd == "restore":
        do_restore(args.dest, args.project, args.date, args.out, args.file)
        return

    parser.print_help()


def _write_line(message: str) -> None:
    sys.stdout.write("{}\n".format(message))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False

    return True


def _load_ignore_rules(dest: Path) -> Tuple[Set[str], Set[str]]:
    ignore_names = set(DEFAULT_IGNORE_NAMES)
    ignore_extensions = set(DEFAULT_IGNORE_EXTENSIONS)

    script_ignore_path = Path(__file__).resolve().with_name(IGNORE_FILE_NAME)
    destination_ignore_path = dest / IGNORE_FILE_NAME

    _apply_ignore_file(script_ignore_path, ignore_names, ignore_extensions)

    if destination_ignore_path.resolve() != script_ignore_path.resolve():
        _apply_ignore_file(destination_ignore_path, ignore_names, ignore_extensions)

    return ignore_names, ignore_extensions


def _apply_ignore_file(ignore_path: Path, ignore_names: Set[str], ignore_extensions: Set[str]) -> None:
    if not ignore_path.is_file():
        return

    try:
        with open(str(ignore_path), "r", encoding="utf-8") as ignore_file:
            for raw_line in ignore_file:
                pattern = raw_line.strip()

                if not pattern:
                    continue

                if pattern.startswith("#"):
                    continue

                normalized_pattern = pattern.rstrip("/")

                if not normalized_pattern:
                    continue

                if normalized_pattern.startswith("*."):
                    extension = normalized_pattern[1:]
                    ignore_extensions.add(extension)
                    continue

                ignore_names.add(normalized_pattern)
    except OSError as error:
        message = "Cannot read ignore file: {}".format(ignore_path)
        raise _BackupReadError(message) from error


def _build_backup_plan(
    src: Path,
    cur: sqlite3.Cursor,
    ignore_rules: Tuple[Set[str], Set[str]],
) -> _BackupPlan:
    ignore_names = ignore_rules[0]
    ignore_extensions = ignore_rules[1]
    projects = sorted(
        project_path for project_path in src.iterdir()
        if project_path.is_dir() and not project_path.name.startswith(".")
    )
    file_states: List[_FileState] = []
    new_blobs_by_sha: Dict[str, _NewBlob] = {}
    files_added = 0
    files_deleted = 0

    for project_path in projects:
        project = project_path.name
        current = scan_project(project_path, ignore_names, ignore_extensions)
        previous = _get_previous_project_state(cur, project)

        for rel_path in sorted(current):
            file_info = current[rel_path]
            file_hash = file_info[0]
            file_size = file_info[1]

            if rel_path in previous and previous[rel_path] == file_hash:
                continue

            file_states.append(_FileState(project, rel_path, file_hash, file_size, 0))
            files_added += 1

            if _has_blob(cur, file_hash):
                continue

            if file_hash in new_blobs_by_sha:
                continue

            source_path = project_path / rel_path
            zip_key = "{}/{}".format(project, rel_path)
            new_blobs_by_sha[file_hash] = _NewBlob(file_hash, source_path, zip_key)

        current_paths = set(current.keys())
        previous_paths = set(previous.keys())
        deleted_paths = previous_paths - current_paths

        for rel_path in sorted(deleted_paths):
            file_hash = previous[rel_path]
            file_states.append(_FileState(project, rel_path, file_hash, 0, 1))
            files_deleted += 1

    new_blobs = list(new_blobs_by_sha.values())
    return _BackupPlan(file_states, new_blobs, files_added, files_deleted)


def _get_previous_project_state(cur: sqlite3.Cursor, project: str) -> Dict[str, str]:
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
    previous: Dict[str, str] = {}

    for row in rows:
        deleted = row[2]

        if deleted:
            continue

        rel_path = row[0]
        file_hash = row[1]
        previous[rel_path] = file_hash

    return previous


def _has_blob(cur: sqlite3.Cursor, file_hash: str) -> bool:
    row = cur.execute(
        "SELECT 1 FROM blobs WHERE sha256 = ?",
        (file_hash,),
    ).fetchone()
    return row is not None


def _create_snapshot_paths(dest: Path) -> _SnapshotPaths:
    base_name = datetime.now().strftime(SNAPSHOT_DATE_FORMAT)
    snapshot_name = base_name
    snapshot_dir = dest / SNAPSHOT_DIR_NAME / snapshot_name
    suffix = 0

    while snapshot_dir.exists():
        suffix += 1
        snapshot_name = "{}_{}".format(base_name, suffix)
        snapshot_dir = dest / SNAPSHOT_DIR_NAME / snapshot_name

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    zip_path = snapshot_dir / SNAPSHOT_ZIP_NAME
    return _SnapshotPaths(snapshot_name, snapshot_dir, zip_path)


def _write_new_blobs(new_blobs: List[_NewBlob], zip_path: Path) -> None:
    with zipfile.ZipFile(
        str(zip_path),
        "w",
        zipfile.ZIP_DEFLATED,
        compresslevel=ZIP_COMPRESSION_LEVEL,
    ) as zip_archive:
        for new_blob in new_blobs:
            written_hash = _write_file_to_zip(zip_archive, new_blob.source_path, new_blob.zip_key)

            if written_hash != new_blob.sha256:
                message = "File changed while backup was being written: {}".format(new_blob.source_path)
                raise _BackupReadError(message)


def _write_file_to_zip(
    zip_archive: zipfile.ZipFile,
    source_path: Path,
    zip_key: str,
    buf_size: int = HASH_BUFFER_SIZE,
) -> str:
    digest = hashlib.sha256()

    try:
        with open(str(source_path), "rb") as source_file:
            with zip_archive.open(zip_key, "w") as target_file:
                chunk = source_file.read(buf_size)

                while chunk:
                    digest.update(chunk)
                    target_file.write(chunk)
                    chunk = source_file.read(buf_size)
    except OSError as error:
        message = "Cannot write file into backup: {}".format(source_path)
        raise _BackupReadError(message) from error

    return digest.hexdigest()


def _save_snapshot(cur: sqlite3.Cursor, snapshot_paths: _SnapshotPaths, backup_plan: _BackupPlan) -> None:
    cur.execute(
        "INSERT INTO snapshots(timestamp, zip_path, files_added, files_del) VALUES (?, ?, ?, ?)",
        (
            snapshot_paths.snapshot_name,
            str(snapshot_paths.zip_path),
            backup_plan.files_added,
            backup_plan.files_deleted,
        ),
    )
    snapshot_id = cur.lastrowid

    for new_blob in backup_plan.new_blobs:
        cur.execute(
            "INSERT INTO blobs(sha256, zip_path, zip_key) VALUES (?, ?, ?)",
            (new_blob.sha256, str(snapshot_paths.zip_path), new_blob.zip_key),
        )

    for file_state in backup_plan.file_states:
        cur.execute(
            "INSERT INTO file_states(snapshot_id, project, rel_path, sha256, size, deleted) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                file_state.project,
                file_state.rel_path,
                file_state.sha256,
                file_state.size,
                file_state.deleted,
            ),
        )


def _remove_snapshot_dir(snapshot_dir: Path) -> None:
    if not snapshot_dir.exists():
        return

    shutil.rmtree(str(snapshot_dir))


def _resolve_snapshot(cur: sqlite3.Cursor, project: str, date_str: str) -> Optional[int]:
    row = cur.execute("""
        SELECT s.id FROM snapshots s
        WHERE s.id IN (SELECT DISTINCT snapshot_id FROM file_states WHERE project = ?)
          AND s.timestamp <= ? || '~'
        ORDER BY s.timestamp DESC LIMIT 1
    """, (project, date_str)).fetchone()

    if row is None:
        return None

    return row[0]


def _reconstruct_tree(cur: sqlite3.Cursor, project: str, up_to_snap: int) -> Dict[str, str]:
    rows = cur.execute("""
        SELECT rel_path, sha256, deleted
        FROM file_states
        WHERE project = ? AND snapshot_id <= ?
        ORDER BY snapshot_id ASC, id ASC
    """, (project, up_to_snap)).fetchall()
    tree: Dict[str, str] = {}

    for row in rows:
        rel_path = row[0]
        file_hash = row[1]
        deleted = row[2]

        if deleted:
            tree.pop(rel_path, None)
            continue

        tree[rel_path] = file_hash

    return tree


if __name__ == "__main__":
    main()
