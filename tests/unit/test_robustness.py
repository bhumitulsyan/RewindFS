"""Problems found by stress-testing a real mount (git, vim, parallel writers)."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rewindfs import cli
from rewindfs.metadata import MetadataDB
from rewindfs.object_store import ObjectStore

# ─────────────── the database is safe to share between threads ───────────────


CONTENTION_SCRIPT = """
import sys, threading
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from rewindfs.metadata import MetadataDB

db = MetadataDB(Path(sys.argv[2]))
db.connect()
db.initialize()
inode = db.create_file(1, "a.txt", "/a.txt")
errors = []

def saver():
    try:
        for i in range(300):
            db.create_version(inode, "hash%d" % i, i)
    except BaseException as exc:
        errors.append(exc)

def reader():
    try:
        for _ in range(3000):
            db.get_file(inode)
            db.get_current_version(inode)
    except BaseException as exc:
        errors.append(exc)

threads = [threading.Thread(target=fn) for fn in (saver, reader, reader)]
for t in threads:
    t.start()
for t in threads:
    t.join()
assert errors == [], errors
assert len(db.list_versions(inode)) == 300
print("ok")
"""


def test_two_threads_sharing_the_database_do_not_deadlock(tmp_path: Path) -> None:
    """Regression: `git init` froze the mount.

    A worker thread saving a version held SQLite's lock while waiting for
    Python's lock (to run tv_now()), while another thread held Python's lock
    waiting for SQLite's. A deadlock freezes the whole interpreter, so the
    threads run in a separate process that must finish within the timeout.
    """
    src = Path(__file__).resolve().parents[2] / "src"
    try:
        result = subprocess.run(
            [sys.executable, "-c", CONTENTION_SCRIPT, str(src), str(tmp_path / "m.db")],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("threads sharing the database deadlocked")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# ─────────────── parallel saves of identical content ───────────────


def test_parallel_identical_objects_do_not_collide(tmp_path: Path) -> None:
    """Regression: every writer used "<hash>.tmp", so the second rename failed."""
    store = ObjectStore(tmp_path / "objects")
    data = b"same bytes from many programs\n"
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def write() -> None:
        try:
            start.wait()
            for _ in range(50):
                path = store.object_path(store.compute_hash(data))
                store._write_object(path, data)  # bypass the exists() shortcut
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert store.read_sync(store.compute_hash(data)) == data
    leftovers = [p for p in (tmp_path / "objects").rglob("*.tmp")]
    assert leftovers == []


# ─────────────── real, stable file times ───────────────


@pytest.fixture
def db(tmp_path: Path) -> MetadataDB:
    mdb = MetadataDB(tmp_path / "metadata.db")
    mdb.connect()
    mdb.initialize()
    yield mdb
    mdb.close()


def test_new_file_gets_times(db: MetadataDB) -> None:
    before = time.time_ns()
    row = db.get_file(db.create_file(1, "t.txt", "/t.txt"))
    assert before <= row["mtime_ns"] <= time.time_ns()
    assert row["atime_ns"] == row["mtime_ns"] == row["ctime_ns"]


def test_saving_moves_mtime_and_set_times_works(db: MetadataDB) -> None:
    inode = db.create_file(1, "t.txt", "/t.txt")
    first = db.get_file(inode)["mtime_ns"]
    time.sleep(0.002)
    db.create_version(inode, "h", 1)
    assert db.get_file(inode)["mtime_ns"] > first

    db.set_times(inode, mtime_ns=1_577_836_800_000_000_000)  # 2020-01-01 UTC
    row = db.get_file(inode)
    assert row["mtime_ns"] == 1_577_836_800_000_000_000
    assert row["ctime_ns"] > row["mtime_ns"]


pyfuse3 = pytest.importorskip("pyfuse3")
trio = pytest.importorskip("trio")

from rewindfs.fuse_handler import RewindFS  # noqa: E402


class Ctx:
    uid = os.getuid()
    gid = os.getgid()
    pid = os.getpid()


@pytest.fixture
def fs(tmp_path: Path):
    vault = RewindFS(str(tmp_path / "storage"))
    vault.init()
    yield vault
    vault.shutdown()


def test_mtime_is_stable_between_calls(fs: RewindFS) -> None:
    """Regression: times were "now" on every call, so vim refused to save."""
    fi, entry = trio.run(fs.create, 1, b"v.txt", 0o100644, os.O_WRONLY | os.O_CREAT, Ctx())
    trio.run(fs.write, fi.fh, 0, b"line one\n")
    trio.run(fs.release, fi.fh)

    first = trio.run(fs.getattr, entry.st_ino, Ctx())
    time.sleep(0.01)
    second = trio.run(fs.getattr, entry.st_ino, Ctx())
    assert first.st_mtime_ns == second.st_mtime_ns
    assert first.st_ctime_ns == second.st_ctime_ns


def test_touch_with_a_date(fs: RewindFS) -> None:
    fi, entry = trio.run(fs.create, 1, b"d.txt", 0o100644, os.O_WRONLY | os.O_CREAT, Ctx())
    trio.run(fs.release, fi.fh)

    class Fields:
        update_mode = update_uid = update_gid = update_size = False
        update_atime = update_mtime = True

    attr = pyfuse3.EntryAttributes()
    attr.st_atime_ns = 1_500_000_000_000_000_000
    attr.st_mtime_ns = 1_577_836_800_000_000_000
    result = trio.run(fs.setattr, entry.st_ino, attr, Fields(), None, Ctx())
    assert result.st_mtime_ns == 1_577_836_800_000_000_000
    assert result.st_atime_ns == 1_500_000_000_000_000_000


def test_old_rows_without_times_use_a_stable_fallback(fs: RewindFS) -> None:
    inode = fs.db.create_file(parent_id=1, name="old.txt", path="/old.txt")
    fs.db.db.execute(
        "UPDATE files SET mtime_ns = NULL, atime_ns = NULL, ctime_ns = NULL, "
        "updated_at = '2026-01-02 03:04:05.000000' WHERE id = ?",
        (inode,),
    )
    fs.db.db.commit()
    a = trio.run(fs.getattr, inode, Ctx())
    b = trio.run(fs.getattr, inode, Ctx())
    assert a.st_mtime_ns == b.st_mtime_ns == 1_767_323_045_000_000_000


# ─────────────── Ctrl+C unmounts quietly ───────────────


def test_ctrl_c_inside_trio_exits_cleanly(tmp_path: Path, monkeypatch) -> None:
    """Regression: Ctrl+C printed a long 'Exception Group' traceback."""
    storage = tmp_path / "storage"
    mount = tmp_path / "mnt"
    mount.mkdir()
    monkeypatch.setattr(cli, "_acquire_lock", lambda _s: 1)
    monkeypatch.setattr(cli, "_release_lock", lambda _fd: None)

    def interrupted(*_args, **_kwargs):
        raise BaseExceptionGroup("workers", [KeyboardInterrupt()])

    monkeypatch.setattr(cli.trio, "run", interrupted)
    result = CliRunner().invoke(cli.app, ["mount", str(storage), str(mount)])
    assert result.exit_code == 0
    assert "unmounted" in result.output


def test_real_errors_during_mount_are_not_hidden(tmp_path: Path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    mount = tmp_path / "mnt"
    mount.mkdir()
    monkeypatch.setattr(cli, "_acquire_lock", lambda _s: 1)
    monkeypatch.setattr(cli, "_release_lock", lambda _fd: None)

    def broken(*_args, **_kwargs):
        raise BaseExceptionGroup("workers", [KeyboardInterrupt(), RuntimeError("boom")])

    monkeypatch.setattr(cli.trio, "run", broken)
    result = CliRunner().invoke(cli.app, ["mount", str(storage), str(mount)])
    assert result.exit_code != 0
