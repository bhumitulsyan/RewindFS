"""SQLite metadata layer for RewindFS.

Manages all metadata: files (inodes), versions, objects, snapshots.
Uses synchronous sqlite3 — pyfuse3 runs on Trio, and SQLite on local disk
is fast enough that thread offloading is only needed for bulk operations.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

# Timestamps are stored as UTC text with microseconds, e.g. "2026-10-21 01:20:05.123456".
# Text in this format sorts in time order, and older rows stored without the
# fraction ("2026-10-21 01:20:05") still sort correctly among the new ones.
TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def utc_now_text() -> str:
    """Current UTC time in TIME_FORMAT (available inside SQL as tv_now())."""
    return datetime.now(UTC).strftime(TIME_FORMAT)


SCHEMA_SQL = """
-- Format version tracking (checked on mount)
CREATE TABLE IF NOT EXISTS format_version (
    version INTEGER NOT NULL DEFAULT 1,
    created_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
);

-- Logical files (paths in the mounted filesystem)
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    is_dir BOOLEAN DEFAULT FALSE,
    current_version_id INTEGER,
    is_deleted BOOLEAN DEFAULT FALSE,
    mode INTEGER DEFAULT 33188,
    uid INTEGER DEFAULT 0,
    gid INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
    updated_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
);

-- Version history per file
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id),
    object_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
    is_deleted BOOLEAN DEFAULT FALSE,
    FOREIGN KEY (object_hash) REFERENCES objects(hash)
);

-- Content-addressable objects
CREATE TABLE IF NOT EXISTS objects (
    hash TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    ref_count INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
);

-- Named filesystem snapshots
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    created_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
);

-- Maps each snapshot to the version of each file at snapshot time
CREATE TABLE IF NOT EXISTS snapshot_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    file_id INTEGER NOT NULL REFERENCES files(id),
    version_id INTEGER NOT NULL REFERENCES versions(id)
);

-- Chronological activity feed
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    path TEXT,
    version_id INTEGER,
    object_hash TEXT,
    created_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
);

-- Programs that changed files (one row per run of a program: PID + start time)
CREATE TABLE IF NOT EXISTS processes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pid INTEGER NOT NULL,
    start_ticks INTEGER NOT NULL,
    comm TEXT NOT NULL,
    exe TEXT,
    cmdline TEXT,
    uid INTEGER,
    ppid INTEGER,
    first_seen_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
    UNIQUE (pid, start_ticks)
);

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_versions_file_id ON versions(file_id);
CREATE INDEX IF NOT EXISTS idx_versions_object_hash ON versions(object_hash);
CREATE INDEX IF NOT EXISTS idx_snapshot_entries_snapshot_id ON snapshot_entries(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
-- Only one LIVE file per path; deleted files keep their path so they can be restored
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_live_path ON files(path) WHERE is_deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent_id, name);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at, id);
"""

ROOT_INODE_SQL = """
INSERT OR IGNORE INTO files (id, parent_id, name, path, is_dir, mode)
    VALUES (1, 1, '', '/', TRUE, 16877);
"""

# Columns added after the first release. Older storage gets them on mount.
ADDED_COLUMNS = (
    ("versions", "process_id", "INTEGER REFERENCES processes(id)"),
    ("events", "process_id", "INTEGER REFERENCES processes(id)"),
    ("events", "old_path", "TEXT"),
)

ADDED_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_versions_process ON versions(process_id);
CREATE INDEX IF NOT EXISTS idx_events_process ON events(process_id, created_at);
"""

FORMAT_VERSION_SQL = """
INSERT INTO format_version (version) SELECT 1
    WHERE NOT EXISTS (SELECT 1 FROM format_version);
"""


class MetadataDB:
    """Synchronous SQLite wrapper for RewindFS metadata operations."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db: sqlite3.Connection | None = None
        self._manual_tx = False

    def connect(self) -> None:
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("tv_now", 0, utc_now_text)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=NORMAL")

    def initialize(self) -> None:
        assert self.db is not None, "Call connect() first"
        self._migrate_unique_path()
        self.db.executescript(SCHEMA_SQL)
        self._add_missing_columns()
        self.db.executescript(ADDED_INDEXES_SQL)
        self.db.execute(ROOT_INODE_SQL)
        self.db.execute(FORMAT_VERSION_SQL)
        self.db.commit()

    def _add_missing_columns(self) -> None:
        assert self.db is not None
        for table, column, decl in ADDED_COLUMNS:
            existing = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.db.commit()

    def _migrate_unique_path(self) -> None:
        """Upgrade storage created before the live-path fix.

        Old databases declared ``files.path`` as UNIQUE, so a deleted file's
        name could never be reused. Rebuild the table without that constraint;
        the partial index in SCHEMA_SQL then enforces uniqueness for live files.
        """
        assert self.db is not None
        has_files = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'files'"
        ).fetchone()
        old_unique = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'files' AND name LIKE 'sqlite_autoindex_files%'"
        ).fetchone()
        if not has_files or not old_unique:
            return

        self.db.commit()
        self.db.execute("PRAGMA foreign_keys=OFF")
        try:
            self.db.executescript(
                """
                BEGIN;
                CREATE TABLE files_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_id INTEGER NOT NULL DEFAULT 1,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    is_dir BOOLEAN DEFAULT FALSE,
                    current_version_id INTEGER,
                    is_deleted BOOLEAN DEFAULT FALSE,
                    mode INTEGER DEFAULT 33188,
                    uid INTEGER DEFAULT 0,
                    gid INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
                    updated_at DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
                );
                INSERT INTO files_new SELECT
                    id, parent_id, name, path, is_dir, current_version_id, is_deleted,
                    mode, uid, gid, created_at, updated_at
                FROM files;
                DROP TABLE files;
                ALTER TABLE files_new RENAME TO files;
                COMMIT;
                """
            )
        finally:
            self.db.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        if self.db:
            self.db.close()
            self.db = None
            self._manual_tx = False

    def begin(self) -> None:
        assert self.db is not None
        if self._manual_tx:
            return
        self.db.execute("BEGIN")
        self._manual_tx = True

    def commit(self) -> None:
        assert self.db is not None
        self.db.commit()
        self._manual_tx = False

    def rollback(self) -> None:
        assert self.db is not None
        self.db.rollback()
        self._manual_tx = False

    def _commit_if_needed(self) -> None:
        assert self.db is not None
        if not self._manual_tx:
            self.db.commit()

    # ──────────────────────────── File operations ────────────────────────────

    def lookup(self, parent_id: int, name: str) -> sqlite3.Row | None:
        assert self.db is not None
        cursor = self.db.execute(
            "SELECT * FROM files WHERE parent_id = ? AND name = ? AND is_deleted = FALSE",
            (parent_id, name),
        )
        return cast(sqlite3.Row | None, cursor.fetchone())

    def get_file(self, inode: int) -> sqlite3.Row | None:
        assert self.db is not None
        cursor = self.db.execute(
            "SELECT * FROM files WHERE id = ? AND is_deleted = FALSE", (inode,)
        )
        return cast(sqlite3.Row | None, cursor.fetchone())

    def get_file_by_path(self, path: str, include_deleted: bool = False) -> sqlite3.Row | None:
        assert self.db is not None
        if include_deleted:
            # Several deleted files can share a path: prefer the live one,
            # otherwise the most recently created deleted one.
            cursor = self.db.execute(
                "SELECT * FROM files WHERE path = ? ORDER BY is_deleted ASC, id DESC LIMIT 1",
                (path,),
            )
        else:
            cursor = self.db.execute(
                "SELECT * FROM files WHERE path = ? AND is_deleted = FALSE", (path,)
            )
        return cast(sqlite3.Row | None, cursor.fetchone())

    def list_children(self, parent_id: int) -> list[sqlite3.Row]:
        assert self.db is not None
        cursor = self.db.execute(
            "SELECT * FROM files WHERE parent_id = ? AND is_deleted = FALSE AND id != ?",
            (parent_id, parent_id),
        )
        return cursor.fetchall()

    def create_file(
        self,
        parent_id: int,
        name: str,
        path: str,
        is_dir: bool = False,
        mode: int = 33188,
        uid: int = 0,
        gid: int = 0,
    ) -> int:
        assert self.db is not None
        cursor = self.db.execute(
            """INSERT INTO files (parent_id, name, path, is_dir, mode, uid, gid,
                                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, tv_now(), tv_now())""",
            (parent_id, name, path, is_dir, mode, uid, gid),
        )
        self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def soft_delete_file(
        self,
        inode: int,
        *,
        commit: bool = True,
        action: str = "DELETE",
        process_id: int | None = None,
    ) -> None:
        assert self.db is not None
        file_row = self.get_file(inode)
        path = file_row["path"] if file_row is not None else None
        self.db.execute(
            "UPDATE files SET is_deleted = TRUE, updated_at = tv_now() WHERE id = ?",
            (inode,),
        )
        self.record_event(action, path=path, process_id=process_id, commit=False)
        if commit:
            self._commit_if_needed()

    def set_file_deleted(self, inode: int, is_deleted: bool, *, commit: bool = True) -> None:
        assert self.db is not None
        if not is_deleted:
            # Un-deleting: if a newer live file now uses the same path, it gives way
            # (soft-deleted, so its versions stay recoverable).
            row = self.db.execute("SELECT path FROM files WHERE id = ?", (inode,)).fetchone()
            if row is not None:
                clash = self.db.execute(
                    "SELECT id FROM files WHERE path = ? AND is_deleted = FALSE AND id != ?",
                    (row["path"], inode),
                ).fetchone()
                if clash is not None:
                    self.soft_delete_file(clash["id"], commit=False, action="REPLACED")
        self.db.execute(
            "UPDATE files SET is_deleted = ?, updated_at = tv_now() WHERE id = ?",
            (is_deleted, inode),
        )
        if commit:
            self._commit_if_needed()

    def rename_file(
        self, inode: int, new_parent_id: int, new_name: str, new_path: str
    ) -> None:
        assert self.db is not None
        row = self.get_file(inode)
        if row is None:
            return
        old_path = row["path"]
        is_dir = row["is_dir"]

        self.db.execute(
            """UPDATE files SET parent_id = ?, name = ?, path = ?,
               updated_at = tv_now() WHERE id = ?""",
            (new_parent_id, new_name, new_path, inode),
        )
        if is_dir:
            self.db.execute(
                """UPDATE files SET path = ? || substr(path, ?),
                   updated_at = tv_now()
                   WHERE path LIKE ? || '/%'""",
                (new_path, len(old_path) + 1, old_path),
            )
        self.db.commit()

    def update_attrs(
        self,
        inode: int,
        mode: int | None = None,
        uid: int | None = None,
        gid: int | None = None,
    ) -> None:
        assert self.db is not None
        if mode is not None:
            self.db.execute(
                "UPDATE files SET mode = ?, updated_at = tv_now() WHERE id = ?",
                (mode, inode),
            )
        if uid is not None:
            self.db.execute(
                "UPDATE files SET uid = ?, updated_at = tv_now() WHERE id = ?",
                (uid, inode),
            )
        if gid is not None:
            self.db.execute(
                "UPDATE files SET gid = ?, updated_at = tv_now() WHERE id = ?",
                (gid, inode),
            )
        self.db.commit()

    def set_current_version(self, inode: int, version_id: int) -> None:
        assert self.db is not None
        self.db.execute(
            "UPDATE files SET current_version_id = ?, updated_at = tv_now() WHERE id = ?",
            (version_id, inode),
        )
        self.db.commit()

    # ──────────────────────────── Version operations ─────────────────────────

    def create_version(
        self,
        file_id: int,
        object_hash: str,
        size_bytes: int,
        *,
        commit: bool = True,
        action: str = "WRITE",
        process_id: int | None = None,
    ) -> int:
        assert self.db is not None
        self.db.execute(
            """INSERT INTO objects (hash, size_bytes, ref_count, created_at)
               VALUES (?, ?, 1, tv_now())
               ON CONFLICT(hash) DO UPDATE SET ref_count = ref_count + 1""",
            (object_hash, size_bytes),
        )
        cursor = self.db.execute(
            """INSERT INTO versions (file_id, object_hash, size_bytes, created_at, process_id)
               VALUES (?, ?, ?, tv_now(), ?)""",
            (file_id, object_hash, size_bytes, process_id),
        )
        version_id = cursor.lastrowid
        assert version_id is not None
        self.db.execute(
            "UPDATE files SET current_version_id = ?, updated_at = tv_now() WHERE id = ?",
            (version_id, file_id),
        )
        file_row = self.get_file(file_id)
        path = file_row["path"] if file_row is not None else None
        self.record_event(
            action,
            path=path,
            version_id=version_id,
            object_hash=object_hash,
            process_id=process_id,
            commit=False,
        )
        if commit:
            self._commit_if_needed()
        return version_id

    def get_current_version(self, inode: int) -> sqlite3.Row | None:
        assert self.db is not None
        cursor = self.db.execute(
            """SELECT v.* FROM versions v
               JOIN files f ON f.current_version_id = v.id
               WHERE f.id = ? AND f.is_deleted = FALSE""",
            (inode,),
        )
        return cast(sqlite3.Row | None, cursor.fetchone())

    def get_version(self, version_id: int) -> sqlite3.Row | None:
        assert self.db is not None
        cursor = self.db.execute(
            "SELECT * FROM versions WHERE id = ?", (version_id,)
        )
        return cast(sqlite3.Row | None, cursor.fetchone())

    def list_versions(self, file_id: int) -> list[sqlite3.Row]:
        assert self.db is not None
        cursor = self.db.execute(
            """SELECT v.*, p.pid AS proc_pid, p.comm AS proc_comm
               FROM versions v
               LEFT JOIN processes p ON p.id = v.process_id
               WHERE v.file_id = ? AND v.is_deleted = FALSE
               ORDER BY v.created_at ASC, v.id ASC""",
            (file_id,),
        )
        return cursor.fetchall()

    def get_latest_version_before(self, file_id: int, before: str) -> sqlite3.Row | None:
        assert self.db is not None
        cursor = self.db.execute(
            """SELECT * FROM versions
               WHERE file_id = ? AND is_deleted = FALSE AND created_at <= ?
               ORDER BY created_at DESC, id DESC
               LIMIT 1""",
            (file_id, before),
        )
        return cast(sqlite3.Row | None, cursor.fetchone())

    def list_prunable_versions(self, keep_last: int) -> list[sqlite3.Row]:
        assert self.db is not None
        cursor = self.db.execute(
            """WITH ranked AS (
                   SELECT v.*,
                          ROW_NUMBER() OVER (
                              PARTITION BY v.file_id
                              ORDER BY v.created_at DESC, v.id DESC
                          ) AS rn
                   FROM versions v
                   WHERE v.is_deleted = FALSE
               )
               SELECT id, file_id, object_hash, size_bytes, created_at
               FROM ranked
               WHERE rn > ?
               ORDER BY file_id ASC, created_at ASC, id ASC""",
            (keep_last,),
        )
        return cursor.fetchall()

    def prune_versions_keep_last(self, keep_last: int, *, commit: bool = True) -> list[sqlite3.Row]:
        assert self.db is not None
        rows = self.list_prunable_versions(keep_last)
        for row in rows:
            self.db.execute(
                "DELETE FROM versions WHERE id = ?",
                (row["id"],),
            )
            self.db.execute(
                "UPDATE objects SET ref_count = ref_count - 1 WHERE hash = ?",
                (row["object_hash"],),
            )
        if commit:
            self._commit_if_needed()
        return rows

    def list_prunable_versions_before(self, before: str) -> list[sqlite3.Row]:
        assert self.db is not None
        cursor = self.db.execute(
            """SELECT v.id, v.file_id, v.object_hash, v.size_bytes, v.created_at
               FROM versions v
               LEFT JOIN files f ON f.current_version_id = v.id
               WHERE v.is_deleted = FALSE
                 AND v.created_at < ?
                 AND f.id IS NULL
               ORDER BY v.file_id ASC, v.created_at ASC, v.id ASC""",
            (before,),
        )
        return cursor.fetchall()

    def prune_versions_before(self, before: str, *, commit: bool = True) -> list[sqlite3.Row]:
        assert self.db is not None
        rows = self.list_prunable_versions_before(before)
        for row in rows:
            self.db.execute(
                "DELETE FROM versions WHERE id = ?",
                (row["id"],),
            )
            self.db.execute(
                "UPDATE objects SET ref_count = ref_count - 1 WHERE hash = ?",
                (row["object_hash"],),
            )
        if commit:
            self._commit_if_needed()
        return rows

    # ──────────────────────────── Object operations ──────────────────────────

    def get_object(self, obj_hash: str) -> sqlite3.Row | None:
        assert self.db is not None
        cursor = self.db.execute(
            "SELECT * FROM objects WHERE hash = ?", (obj_hash,)
        )
        return cast(sqlite3.Row | None, cursor.fetchone())

    def decrement_ref_count(self, obj_hash: str) -> int:
        assert self.db is not None
        self.db.execute(
            "UPDATE objects SET ref_count = ref_count - 1 WHERE hash = ?",
            (obj_hash,),
        )
        self.db.commit()
        cursor = self.db.execute(
            "SELECT ref_count FROM objects WHERE hash = ?", (obj_hash,)
        )
        row = cursor.fetchone()
        return row["ref_count"] if row else 0

    def get_orphaned_objects(self) -> list[sqlite3.Row]:
        assert self.db is not None
        cursor = self.db.execute(
            "SELECT * FROM objects WHERE ref_count <= 0"
        )
        return cursor.fetchall()

    def delete_object_record(self, obj_hash: str, *, commit: bool = True) -> None:
        assert self.db is not None
        self.db.execute("DELETE FROM objects WHERE hash = ?", (obj_hash,))
        if commit:
            self._commit_if_needed()

    # ──────────────────────────── Snapshot operations ───────────────────────

    def create_snapshot(self, name: str, description: str | None = None) -> int:
        assert self.db is not None
        cursor = self.db.execute(
            "INSERT INTO snapshots (name, description, created_at) VALUES (?, ?, tv_now())",
            (name, description),
        )
        snapshot_id = cursor.lastrowid
        assert snapshot_id is not None
        self.db.execute(
            """INSERT INTO snapshot_entries (snapshot_id, file_id, version_id)
               SELECT ?, id, current_version_id
               FROM files
               WHERE is_deleted = FALSE AND is_dir = FALSE AND current_version_id IS NOT NULL""",
            (snapshot_id,),
        )
        self.db.commit()
        return snapshot_id

    def list_snapshots(self) -> list[sqlite3.Row]:
        assert self.db is not None
        cursor = self.db.execute(
            """SELECT s.*, COUNT(se.id) AS file_count
               FROM snapshots s
               LEFT JOIN snapshot_entries se ON se.snapshot_id = s.id
               GROUP BY s.id
               ORDER BY s.created_at ASC, s.id ASC"""
        )
        return cursor.fetchall()

    def get_snapshot_by_name(self, name: str) -> sqlite3.Row | None:
        assert self.db is not None
        cursor = self.db.execute("SELECT * FROM snapshots WHERE name = ?", (name,))
        return cast(sqlite3.Row | None, cursor.fetchone())

    def get_snapshot_entries(self, snapshot_id: int) -> list[sqlite3.Row]:
        assert self.db is not None
        cursor = self.db.execute(
            """SELECT se.file_id, se.version_id
               FROM snapshot_entries se
               WHERE se.snapshot_id = ?""",
            (snapshot_id,),
        )
        return cursor.fetchall()

    def get_snapshot_entries_detailed(self, snapshot_id: int) -> list[sqlite3.Row]:
        assert self.db is not None
        cursor = self.db.execute(
            """SELECT se.file_id, se.version_id, f.path, v.object_hash, v.size_bytes, v.created_at
               FROM snapshot_entries se
               JOIN files f ON f.id = se.file_id
               JOIN versions v ON v.id = se.version_id
               WHERE se.snapshot_id = ?
               ORDER BY f.path ASC""",
            (snapshot_id,),
        )
        return cursor.fetchall()

    def delete_snapshot(self, snapshot_id: int) -> None:
        assert self.db is not None
        self.db.execute("DELETE FROM snapshot_entries WHERE snapshot_id = ?", (snapshot_id,))
        self.db.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
        self.db.commit()

    def list_active_file_ids(self) -> list[int]:
        assert self.db is not None
        cursor = self.db.execute(
            "SELECT id FROM files WHERE is_deleted = FALSE AND is_dir = FALSE"
        )
        return [row["id"] for row in cursor.fetchall()]

    # ──────────────────────────── Event operations ──────────────────────────

    def record_event(
        self,
        action: str,
        *,
        path: str | None = None,
        version_id: int | None = None,
        object_hash: str | None = None,
        process_id: int | None = None,
        old_path: str | None = None,
        commit: bool = True,
    ) -> int:
        assert self.db is not None
        cursor = self.db.execute(
            """INSERT INTO events
                   (action, path, version_id, object_hash, created_at, process_id, old_path)
               VALUES (?, ?, ?, ?, tv_now(), ?, ?)""",
            (action, path, version_id, object_hash, process_id, old_path),
        )
        event_id = cursor.lastrowid
        assert event_id is not None
        if commit:
            self._commit_if_needed()
        return event_id

    def list_events(
        self,
        limit: int = 50,
        *,
        action: str | None = None,
        path_prefix: str | None = None,
        since: str | None = None,
        until: str | None = None,
        pid: int | None = None,
    ) -> list[sqlite3.Row]:
        assert self.db is not None
        where_parts: list[str] = []
        params: list[str | int] = []
        if action is not None:
            where_parts.append("e.action = ?")
            params.append(action)
        if path_prefix is not None:
            where_parts.append("e.path LIKE ?")
            params.append(path_prefix + "%")
        if since is not None:
            where_parts.append("e.created_at >= ?")
            params.append(since)
        if until is not None:
            where_parts.append("e.created_at <= ?")
            params.append(until)
        if pid is not None:
            where_parts.append("p.pid = ?")
            params.append(pid)

        where_sql = ""
        if where_parts:
            where_sql = "WHERE " + " AND ".join(where_parts)
        params.append(limit)
        cursor = self.db.execute(
            f"""SELECT e.created_at, e.action, e.path, e.old_path, e.version_id,
                      e.object_hash, e.process_id, p.pid AS proc_pid, p.comm AS proc_comm
               FROM events e
               LEFT JOIN processes p ON p.id = e.process_id
               {where_sql}
               ORDER BY e.created_at DESC, e.id DESC
               LIMIT ?""",
            tuple(params),
        )
        rows = cursor.fetchall()
        rows.reverse()
        return rows

    # ──────────────────────────── Process operations ────────────────────────

    def get_or_create_process(
        self,
        *,
        pid: int,
        start_ticks: int,
        comm: str,
        exe: str | None = None,
        cmdline: str | None = None,
        uid: int | None = None,
        ppid: int | None = None,
    ) -> int:
        """Return the id for this run of a program, adding it on first sight."""
        assert self.db is not None
        row = self.db.execute(
            "SELECT id FROM processes WHERE pid = ? AND start_ticks = ?", (pid, start_ticks)
        ).fetchone()
        if row is not None:
            return int(row["id"])
        cursor = self.db.execute(
            """INSERT INTO processes
                   (pid, start_ticks, comm, exe, cmdline, uid, ppid, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, tv_now())""",
            (pid, start_ticks, comm, exe, cmdline, uid, ppid),
        )
        self._commit_if_needed()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def get_process(self, process_id: int) -> sqlite3.Row | None:
        assert self.db is not None
        cursor = self.db.execute("SELECT * FROM processes WHERE id = ?", (process_id,))
        return cast(sqlite3.Row | None, cursor.fetchone())

    def summarize_processes(
        self, *, since: str | None = None, until: str | None = None
    ) -> list[sqlite3.Row]:
        """Per program: how many files it wrote, created, deleted and renamed."""
        assert self.db is not None
        where_parts = ["e.process_id IS NOT NULL"]
        params: list[str] = []
        if since is not None:
            where_parts.append("e.created_at >= ?")
            params.append(since)
        if until is not None:
            where_parts.append("e.created_at <= ?")
            params.append(until)
        cursor = self.db.execute(
            f"""SELECT p.id AS process_id, p.pid, p.comm, p.exe, p.cmdline, p.uid,
                       COUNT(DISTINCT CASE WHEN e.action = 'WRITE' THEN e.path END)
                           AS files_written,
                       SUM(CASE WHEN e.action = 'WRITE' THEN 1 ELSE 0 END) AS writes,
                       SUM(CASE WHEN e.action IN ('CREATE', 'MKDIR') THEN 1 ELSE 0 END)
                           AS created,
                       SUM(CASE WHEN e.action IN ('DELETE', 'RMDIR') THEN 1 ELSE 0 END)
                           AS deleted,
                       SUM(CASE WHEN e.action = 'RENAME' THEN 1 ELSE 0 END) AS renamed,
                       MIN(e.created_at) AS first_at,
                       MAX(e.created_at) AS last_at
                FROM events e
                JOIN processes p ON p.id = e.process_id
                WHERE {" AND ".join(where_parts)}
                GROUP BY p.id
                ORDER BY files_written DESC, writes DESC, p.id ASC""",
            tuple(params),
        )
        return cursor.fetchall()

    # ──────────────────────────── Stats ───────────────────────────────────────

    def get_stats(self) -> dict:
        assert self.db is not None
        stats = {}

        cursor = self.db.execute(
            "SELECT COUNT(*) as c FROM files WHERE is_deleted = FALSE AND is_dir = FALSE"
        )
        row = cursor.fetchone()
        stats["total_files"] = row["c"] if row else 0

        cursor = self.db.execute(
            "SELECT COUNT(*) as c FROM versions WHERE is_deleted = FALSE"
        )
        row = cursor.fetchone()
        stats["total_versions"] = row["c"] if row else 0

        cursor = self.db.execute("SELECT COUNT(*) as c FROM objects")
        row = cursor.fetchone()
        stats["total_objects"] = row["c"] if row else 0

        cursor = self.db.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) as s FROM objects"
        )
        row = cursor.fetchone()
        stats["actual_size_bytes"] = row["s"] if row else 0

        cursor = self.db.execute(
            """SELECT COALESCE(SUM(v.size_bytes), 0) as s
               FROM versions v WHERE v.is_deleted = FALSE"""
        )
        row = cursor.fetchone()
        stats["logical_size_bytes"] = row["s"] if row else 0

        cursor = self.db.execute(
            "SELECT COUNT(*) as c FROM objects WHERE ref_count <= 0"
        )
        row = cursor.fetchone()
        stats["orphaned_objects"] = row["c"] if row else 0

        return stats
