import sqlite3
import zipfile
from pathlib import Path

from snapback import DB_NAME, SNAPSHOT_KIND_FULL, SNAPSHOT_KIND_INCREMENTAL, do_backup, do_restore, init_db


def test_full_backup_anchors_independent_incremental_restore(tmp_path: Path) -> None:
    """Verify that a full backup starts an independently restorable chain

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        None
    """
    # ARRANGE
    source_path = tmp_path / "source"
    project_path = source_path / "project"
    project_path.mkdir(parents=True)
    changed_path = project_path / "changed.py"
    stable_path = project_path / "stable.py"
    changed_path.write_text("historic\n", encoding="utf-8")
    stable_path.write_text("stable\n", encoding="utf-8")
    destination_path = tmp_path / "backup"
    do_backup(source_path, destination_path)
    changed_path.write_text("full\n", encoding="utf-8")
    do_backup(source_path, destination_path, full_backup=True)

    # ACT
    do_backup(source_path, destination_path, full_backup=True)
    changed_path.write_text("historic\n", encoding="utf-8")
    do_backup(source_path, destination_path)

    with sqlite3.connect(str(destination_path / DB_NAME)) as conn:
        conn.row_factory = sqlite3.Row
        full_snapshot = conn.execute(
            "SELECT id, zip_path, base_snapshot_id FROM snapshots WHERE kind = ? ORDER BY id DESC LIMIT 1",
            (SNAPSHOT_KIND_FULL,),
        ).fetchone()
        assert full_snapshot is not None
        full_snapshot_id = full_snapshot["id"]
        full_zip_path = Path(full_snapshot["zip_path"])
        old_archives = conn.execute(
            "SELECT zip_path FROM snapshots WHERE id < ? ORDER BY id",
            (full_snapshot_id,),
        ).fetchall()
        incremental_base = conn.execute(
            "SELECT base_snapshot_id FROM snapshots WHERE kind = ? ORDER BY id DESC LIMIT 1",
            (SNAPSHOT_KIND_INCREMENTAL,),
        ).fetchone()
        full_count = conn.execute(
            "SELECT COUNT(*) AS value FROM snapshots WHERE kind = ?",
            (SNAPSHOT_KIND_FULL,),
        ).fetchone()
        assert incremental_base is not None
        assert full_count is not None

    for old_archive in old_archives:
        Path(old_archive["zip_path"]).unlink()

    restore_path = tmp_path / "restore"
    do_restore(destination_path, "project", "9999", restore_path, None, backup_chain=True)

    # ASSERT
    assert full_snapshot["base_snapshot_id"] == full_snapshot_id
    assert incremental_base["base_snapshot_id"] == full_snapshot_id
    assert full_count["value"] == 2
    assert full_zip_path.name == "full.zip"

    with zipfile.ZipFile(full_zip_path, "r") as full_archive:
        assert full_archive.read("project/changed.py") == b"full\n"
        assert full_archive.read("project/stable.py") == b"stable\n"

    assert (restore_path / "changed.py").read_text(encoding="utf-8") == "historic\n"
    assert (restore_path / "stable.py").read_text(encoding="utf-8") == "stable\n"


def test_init_db_migrates_legacy_snapshots(tmp_path: Path) -> None:
    """Verify that existing snapshot metadata remains readable after migration

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        None
    """
    # ARRANGE
    database_path = tmp_path / "legacy.db"

    with sqlite3.connect(str(database_path)) as conn:
        conn.executescript("""
            CREATE TABLE snapshots (
                id          INTEGER PRIMARY KEY,
                timestamp   TEXT    NOT NULL,
                zip_path    TEXT    NOT NULL,
                files_added INTEGER NOT NULL DEFAULT 0,
                files_del   INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO snapshots(id, timestamp, zip_path, files_added, files_del)
            VALUES (1, '2026-01-01_00-00-00', '/backup/data.zip', 2, 0);
        """)

    # ACT
    migrated_conn = init_db(database_path)
    migrated_conn.row_factory = sqlite3.Row

    # ASSERT
    snapshot = migrated_conn.execute(
        "SELECT kind, base_snapshot_id FROM snapshots WHERE id = 1",
    ).fetchone()
    column_count = migrated_conn.execute("""
        SELECT COUNT(*) AS value
        FROM pragma_table_info('snapshots')
        WHERE name IN ('kind', 'base_snapshot_id')
    """).fetchone()
    table_count = migrated_conn.execute("""
        SELECT COUNT(*) AS value FROM sqlite_master WHERE type = 'table' AND name = 'blob_copy'
    """).fetchone()
    migrated_conn.close()

    assert snapshot is not None
    assert snapshot["kind"] == SNAPSHOT_KIND_INCREMENTAL
    assert snapshot["base_snapshot_id"] is None
    assert column_count is not None
    assert column_count["value"] == 2
    assert table_count is not None
    assert table_count["value"] == 1
