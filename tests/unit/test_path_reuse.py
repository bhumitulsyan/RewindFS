"""Deleted file names must be reusable, and old databases must be upgraded.

Regression tests for the crash where re-creating a deleted file's name raised
``UNIQUE constraint failed: files.path`` and took the whole mount down.
"""

import sqlite3
from pathlib import Path

import pytest

from rewindfs.metadata import MetadataDB


@pytest.fixture
def db(tmp_path: Path) -> MetadataDB:
    mdb = MetadataDB(tmp_path / "metadata.db")
    mdb.connect()
    mdb.initialize()
    yield mdb
    mdb.close()


def test_recreate_after_delete(db: MetadataDB) -> None:
    old = db.create_file(1, "a.txt", "/a.txt")
    db.create_version(old, "h_old", 3)
    db.soft_delete_file(old)

    new = db.create_file(1, "a.txt", "/a.txt")

    assert new != old
    assert db.lookup(1, "a.txt")["id"] == new
    # The deleted file and its history are still there
    assert db.get_file_by_path("/a.txt", include_deleted=True)["id"] == new
    assert len(db.list_versions(old)) == 1


def test_delete_and_recreate_many_times(db: MetadataDB) -> None:
    for _ in range(5):
        inode = db.create_file(1, "loop.txt", "/loop.txt")
        db.soft_delete_file(inode)
    rows = db.db.execute("SELECT COUNT(*) AS c FROM files WHERE path = '/loop.txt'").fetchone()
    assert rows["c"] == 5


def test_rename_over_deleted_file(db: MetadataDB) -> None:
    """Editors save by writing a temp file and renaming it over the original."""
    original = db.create_file(1, "doc.txt", "/doc.txt")
    tmp = db.create_file(1, "doc.txt.swp", "/doc.txt.swp")
    db.soft_delete_file(original)

    db.rename_file(tmp, 1, "doc.txt", "/doc.txt")

    assert db.lookup(1, "doc.txt")["id"] == tmp


def test_two_live_files_cannot_share_a_path(db: MetadataDB) -> None:
    db.create_file(1, "x.txt", "/x.txt")
    with pytest.raises(sqlite3.IntegrityError):
        db.create_file(1, "x.txt", "/x.txt")


def test_mkdir_after_rmdir(db: MetadataDB) -> None:
    d = db.create_file(1, "photos", "/photos", is_dir=True)
    db.soft_delete_file(d)
    assert db.create_file(1, "photos", "/photos", is_dir=True) != d


def test_lookup_with_deleted_prefers_latest_deleted(db: MetadataDB) -> None:
    first = db.create_file(1, "r.txt", "/r.txt")
    db.soft_delete_file(first)
    second = db.create_file(1, "r.txt", "/r.txt")
    db.soft_delete_file(second)
    assert db.get_file_by_path("/r.txt", include_deleted=True)["id"] == second


def test_undelete_replaces_newer_live_file(db: MetadataDB) -> None:
    """Restoring an old file over a newer one keeps the newer one recoverable."""
    old = db.create_file(1, "s.txt", "/s.txt")
    db.soft_delete_file(old)
    newer = db.create_file(1, "s.txt", "/s.txt")

    db.set_file_deleted(old, False)

    assert db.lookup(1, "s.txt")["id"] == old
    assert db.get_file(newer) is None  # soft-deleted, not gone
    assert db.get_file_by_path("/s.txt", include_deleted=True)["id"] == old


def test_old_database_is_upgraded(tmp_path: Path) -> None:
    """Storage created by the old schema (UNIQUE path) is migrated on mount."""
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER NOT NULL DEFAULT 1,
            name TEXT NOT NULL,
            path TEXT UNIQUE NOT NULL,
            is_dir BOOLEAN DEFAULT FALSE,
            current_version_id INTEGER,
            is_deleted BOOLEAN DEFAULT FALSE,
            mode INTEGER DEFAULT 33188,
            uid INTEGER DEFAULT 0,
            gid INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO files (id, parent_id, name, path, is_dir, mode)
            VALUES (1, 1, '', '/', TRUE, 16877);
        INSERT INTO files (parent_id, name, path, is_deleted)
            VALUES (1, 'gone.txt', '/gone.txt', TRUE);
        """
    )
    raw.commit()
    raw.close()

    mdb = MetadataDB(path)
    mdb.connect()
    mdb.initialize()
    try:
        assert mdb.create_file(1, "gone.txt", "/gone.txt") > 2
        assert mdb.get_file(1)["path"] == "/"
        # Running initialize again is harmless
        mdb.initialize()
    finally:
        mdb.close()
