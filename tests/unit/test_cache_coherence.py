"""The kernel must not cache sizes or contents of files in the vault.

Restores and snapshot restores change files from outside the mounted process.
With caching on, `cat` right after restoring a longer version printed a cut-off
file (the kernel still used the old, shorter size).
"""

import os
from pathlib import Path

import pytest

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


def test_names_and_sizes_are_never_cached(fs: RewindFS) -> None:
    fi, entry = trio.run(fs.create, 1, b"a.txt", 0o100644, os.O_WRONLY | os.O_CREAT, Ctx())
    assert entry.entry_timeout == 0
    assert entry.attr_timeout == 0
    assert fi.keep_cache is False
    trio.run(fs.release, fi.fh)

    looked_up = trio.run(fs.lookup, 1, b"a.txt", Ctx())
    assert looked_up.attr_timeout == 0


def test_file_pages_are_not_reused_between_opens(fs: RewindFS) -> None:
    fi, entry = trio.run(fs.create, 1, b"b.txt", 0o100644, os.O_WRONLY | os.O_CREAT, Ctx())
    trio.run(fs.release, fi.fh)
    for flags in (os.O_RDONLY, os.O_WRONLY, os.O_WRONLY | os.O_TRUNC):
        opened = trio.run(fs.open, entry.st_ino, flags, Ctx())
        assert opened.keep_cache is False
        trio.run(fs.release, opened.fh)


def test_size_follows_an_outside_restore(fs: RewindFS) -> None:
    """getattr reports the restored size, not the size it last saw."""
    inode = fs.db.create_file(parent_id=1, name="c.txt", path="/c.txt")
    long_hash = fs.objects.store_sync(b"second draft\n")
    short_hash = fs.objects.store_sync(b"oops\n")
    fs.db.create_version(inode, long_hash, 13)
    fs.db.create_version(inode, short_hash, 5)
    assert trio.run(fs.getattr, inode, Ctx()).st_size == 5

    fs.db.create_version(inode, long_hash, 13, action="RESTORE")  # what the CLI does

    assert trio.run(fs.getattr, inode, Ctx()).st_size == 13
