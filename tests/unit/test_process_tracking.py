"""Every change records which program made it (Phase 2)."""

import json
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rewindfs.metadata import MetadataDB
from rewindfs.process_info import _parse_stat, read_process, read_start_ticks

# ─────────────────────────── reading /proc ───────────────────────────


def test_read_own_process() -> None:
    info = read_process(os.getpid())
    assert info is not None
    assert info.pid == os.getpid()
    assert info.start_ticks > 0
    assert info.comm
    assert info.uid == os.getuid()
    assert info.ppid == os.getppid()
    assert read_start_ticks(os.getpid()) == info.start_ticks


def test_missing_or_kernel_process_is_none() -> None:
    assert read_process(0) is None
    assert read_process(-1) is None
    assert read_process(2**22 + 12345) is None  # beyond Linux's PID limit


def test_stat_parsing_survives_odd_program_names() -> None:
    # A program can be named "evil) S 1 2" to confuse naive parsers.
    fields = " ".join(["S", "4242"] + ["0"] * 17 + ["987654"] + ["0"] * 10)
    text = f"1234 (evil) S 1 2) {fields}"
    assert _parse_stat(text) == (4242, 987654)


def test_fake_proc_directory(tmp_path: Path) -> None:
    proc = tmp_path / "proc" / "77"
    proc.mkdir(parents=True)
    fields = " ".join(["S", "1"] + ["0"] * 17 + ["5555"] + ["0"] * 10)
    (proc / "stat").write_text(f"77 (encryptor) {fields}")
    (proc / "comm").write_text("encryptor\n")
    (proc / "cmdline").write_text("python3\0encrypt.py\0/home/me/docs\0")
    (proc / "status").write_text("Name:\tencryptor\nUid:\t1000\t1000\t1000\t1000\n")

    info = read_process(77, proc_root=tmp_path / "proc")
    assert info is not None
    assert info.comm == "encryptor"
    assert info.cmdline == "python3 encrypt.py /home/me/docs"
    assert info.uid == 1000
    assert info.start_ticks == 5555
    assert info.exe is None  # no exe link in the fake directory


# ─────────────────────────── database ───────────────────────────


@pytest.fixture
def db(tmp_path: Path) -> MetadataDB:
    mdb = MetadataDB(tmp_path / "metadata.db")
    mdb.connect()
    mdb.initialize()
    yield mdb
    mdb.close()


def _proc(db: MetadataDB, pid: int, comm: str, start: int = 100) -> int:
    return db.get_or_create_process(pid=pid, start_ticks=start, comm=comm)


def test_same_run_of_a_program_is_one_row(db: MetadataDB) -> None:
    a = _proc(db, 500, "vim")
    assert _proc(db, 500, "vim") == a
    # Same PID reused later by another program = a different run
    assert _proc(db, 500, "ransom", start=999) != a


def test_versions_and_events_remember_the_program(db: MetadataDB) -> None:
    pid_row = _proc(db, 321, "editor")
    inode = db.create_file(1, "a.txt", "/a.txt")
    db.create_version(inode, "h1", 2, process_id=pid_row)
    db.soft_delete_file(inode, process_id=pid_row)

    version = db.list_versions(inode)[0]
    assert (version["proc_comm"], version["proc_pid"]) == ("editor", 321)
    actions = [(e["action"], e["proc_comm"]) for e in db.list_events()]
    assert ("WRITE", "editor") in actions
    assert ("DELETE", "editor") in actions


def test_summary_ranks_the_busiest_program_first(db: MetadataDB) -> None:
    user = _proc(db, 10, "bash")
    attacker = _proc(db, 20, "python3")
    for i in range(3):
        inode = db.create_file(1, f"f{i}.txt", f"/f{i}.txt")
        db.create_version(inode, f"orig{i}", 5, process_id=user)
    for i in range(3):
        inode = db.get_file_by_path(f"/f{i}.txt")["id"]
        db.create_version(inode, f"enc{i}", 5, process_id=attacker)
        db.record_event("RENAME", path=f"/f{i}.locked", old_path=f"/f{i}.txt",
                        process_id=attacker)
    db.create_version(db.get_file_by_path("/f0.txt")["id"], "again", 5, process_id=attacker)

    rows = db.summarize_processes()
    assert rows[0]["comm"] == "python3"
    assert rows[0]["files_written"] == 3
    assert rows[0]["writes"] == 4
    assert rows[0]["renamed"] == 3
    assert rows[1]["comm"] == "bash"


def test_log_can_filter_by_pid(db: MetadataDB) -> None:
    a = _proc(db, 1, "one")
    b = _proc(db, 2, "two")
    db.record_event("CREATE", path="/x", process_id=a)
    db.record_event("CREATE", path="/y", process_id=b)
    assert [e["path"] for e in db.list_events(pid=2)] == ["/y"]


def test_old_storage_gets_the_new_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE versions (id INTEGER PRIMARY KEY AUTOINCREMENT, file_id INTEGER NOT NULL,
            object_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, is_deleted BOOLEAN DEFAULT FALSE);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
            path TEXT, version_id INTEGER, object_hash TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO events (action, path) VALUES ('WRITE', '/old.txt');
        """
    )
    raw.commit()
    raw.close()

    mdb = MetadataDB(path)
    mdb.connect()
    mdb.initialize()
    try:
        cols = {r["name"] for r in mdb.db.execute("PRAGMA table_info(events)")}
        assert {"process_id", "old_path"} <= cols
        old = mdb.list_events()[0]
        assert old["path"] == "/old.txt" and old["proc_comm"] is None
        mdb.initialize()  # running again is harmless
    finally:
        mdb.close()


# ─────────────────────────── filesystem operations ───────────────────────────

pyfuse3 = pytest.importorskip("pyfuse3")
trio = pytest.importorskip("trio")

from rewindfs.fuse_handler import RewindFS  # noqa: E402


class Ctx:
    uid = os.getuid()
    gid = os.getgid()
    pid = os.getpid()


class KernelCtx(Ctx):
    pid = 0


@pytest.fixture
def fs(tmp_path: Path):
    vault = RewindFS(str(tmp_path / "storage"))
    vault.init()
    yield vault
    vault.shutdown()


def run(fn, *args):
    return trio.run(fn, *args)


WRITE_TRUNC = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
ME = read_process(os.getpid())


def test_saves_are_attributed_to_the_writing_program(fs: RewindFS) -> None:
    fi, entry = run(fs.create, 1, b"a.txt", 0o100644, WRITE_TRUNC, Ctx())
    run(fs.write, fi.fh, 0, b"hello")
    run(fs.flush, fi.fh)
    run(fs.release, fi.fh)  # release arrives from the kernel with pid 0

    version = fs.db.list_versions(entry.st_ino)[0]
    assert version["proc_pid"] == os.getpid()
    assert version["proc_comm"] == ME.comm
    create = fs.db.list_events(action="CREATE")[0]
    assert create["proc_pid"] == os.getpid()


def test_delete_rename_and_mkdir_are_attributed(fs: RewindFS) -> None:
    fi, _ = run(fs.create, 1, b"doc.txt", 0o100644, WRITE_TRUNC, Ctx())
    run(fs.write, fi.fh, 0, b"x")
    run(fs.release, fi.fh)
    run(fs.mkdir, 1, b"folder", 0o40755, Ctx())
    run(fs.rename, 1, b"doc.txt", 1, b"doc.txt.locked", 0, Ctx())
    run(fs.unlink, 1, b"doc.txt.locked", Ctx())

    events = {e["action"]: e for e in fs.db.list_events()}
    assert events["MKDIR"]["proc_pid"] == os.getpid()
    assert events["RENAME"]["old_path"] == "/doc.txt"
    assert events["RENAME"]["path"] == "/doc.txt.locked"
    assert events["RENAME"]["proc_pid"] == os.getpid()
    assert events["DELETE"]["proc_pid"] == os.getpid()


def test_rename_over_a_file_records_it_as_replaced(fs: RewindFS) -> None:
    for name in (b"orig.txt", b"orig.tmp"):
        fi, _ = run(fs.create, 1, name, 0o100644, WRITE_TRUNC, Ctx())
        run(fs.write, fi.fh, 0, name)
        run(fs.release, fi.fh)
    run(fs.rename, 1, b"orig.tmp", 1, b"orig.txt", 0, Ctx())
    actions = [e["action"] for e in fs.db.list_events()]
    assert "REPLACED" in actions and "RENAME" in actions


def test_unknown_program_still_records_the_change(fs: RewindFS) -> None:
    """If the caller can't be identified (pid 0), the save must not fail."""
    fi, entry = run(fs.create, 1, b"k.txt", 0o100644, WRITE_TRUNC, KernelCtx())
    run(fs.write, fi.fh, 0, b"data")
    run(fs.release, fi.fh)
    version = fs.db.list_versions(entry.st_ino)[0]
    assert version["size_bytes"] == 4
    assert version["proc_pid"] is None


def test_proc_is_read_once_per_program_run(fs: RewindFS, monkeypatch) -> None:
    import rewindfs.fuse_handler as handler

    calls = []
    real = handler.read_process
    monkeypatch.setattr(handler, "read_process", lambda pid: calls.append(pid) or real(pid))
    for i in range(5):
        fi, _ = run(fs.create, 1, f"n{i}.txt".encode(), 0o100644, WRITE_TRUNC, Ctx())
        run(fs.release, fi.fh)
    assert calls == [os.getpid()]


def test_who_command(tmp_path: Path, fs: RewindFS) -> None:
    from rewindfs.cli import app

    fi, _ = run(fs.create, 1, b"w.txt", 0o100644, WRITE_TRUNC, Ctx())
    run(fs.write, fi.fh, 0, b"abc")
    run(fs.release, fi.fh)
    storage = tmp_path / "storage"
    (storage / ".rewindfs").write_text(json.dumps({"version": 1, "hash_algo": "sha256"}))

    result = CliRunner().invoke(app, ["who", "--storage", str(storage), "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert rows[0]["pid"] == os.getpid()
    assert rows[0]["files_written"] == 1
    assert rows[0]["created"] == 1
