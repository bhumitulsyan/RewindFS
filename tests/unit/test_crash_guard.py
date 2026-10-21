"""An unexpected error in one operation must not take the whole vault down."""

import errno
from pathlib import Path

import pytest

pyfuse3 = pytest.importorskip("pyfuse3")
trio = pytest.importorskip("trio")

from rewindfs.fuse_handler import RewindFS  # noqa: E402


@pytest.fixture
def fs(tmp_path: Path):
    vault = RewindFS(str(tmp_path / "storage"))
    vault.init()
    yield vault
    vault.shutdown()


def test_unexpected_error_becomes_eio(fs: RewindFS, monkeypatch) -> None:
    def broken_lookup(parent_id, name):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(fs.db, "lookup", broken_lookup)

    with pytest.raises(pyfuse3.FUSEError) as info:
        trio.run(fs.lookup, 1, b"anything", None)

    assert info.value.errno == errno.EIO
    assert fs.error_count == 1


def test_vault_keeps_working_after_an_error(fs: RewindFS, monkeypatch) -> None:
    real_lookup = fs.db.lookup
    monkeypatch.setattr(fs.db, "lookup", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(pyfuse3.FUSEError):
        trio.run(fs.lookup, 1, b"a.txt", None)

    monkeypatch.setattr(fs.db, "lookup", real_lookup)
    inode = fs.db.create_file(parent_id=1, name="a.txt", path="/a.txt")
    entry = trio.run(fs.lookup, 1, b"a.txt", None)
    assert entry.st_ino == inode


def test_normal_fuse_errors_pass_through(fs: RewindFS) -> None:
    """Expected errors such as 'file not found' are not counted as crashes."""
    with pytest.raises(pyfuse3.FUSEError) as info:
        trio.run(fs.lookup, 1, b"missing.txt", None)
    assert info.value.errno == errno.ENOENT
    assert fs.error_count == 0


def test_half_finished_changes_are_rolled_back(fs: RewindFS, monkeypatch) -> None:
    """If an operation fails midway, its partial database writes are undone."""

    def create_then_fail(*args, **kwargs):
        fs.db.begin()
        fs.db.db.execute(
            "INSERT INTO files (parent_id, name, path) VALUES (1, 'ghost.txt', '/ghost.txt')"
        )
        raise RuntimeError("failed after a partial write")

    monkeypatch.setattr(fs.db, "create_file", create_then_fail)
    with pytest.raises(pyfuse3.FUSEError):
        trio.run(fs.mkdir, 1, b"ghost.txt", 0o755, type("Ctx", (), {"uid": 0, "gid": 0})())

    assert fs.db.get_file_by_path("/ghost.txt") is None
