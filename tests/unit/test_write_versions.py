"""One real save = one version, and overwriting really replaces the content.

These call the filesystem operations in the order the Linux kernel sends them
for common commands (traced from a real mount).
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


CTX = Ctx()
WRITE_TRUNC = os.O_WRONLY | os.O_CREAT | os.O_TRUNC


@pytest.fixture
def fs(tmp_path: Path):
    vault = RewindFS(str(tmp_path / "storage"))
    vault.init()
    yield vault
    vault.shutdown()


def run(fn, *args):
    return trio.run(fn, *args)


def shell_redirect_new(fs: RewindFS, name: bytes, data: bytes) -> int:
    """`echo ... > newfile`: create, close a duplicate, write, close, release."""
    fi, entry = run(fs.create, 1, name, 0o100644, WRITE_TRUNC, CTX)
    run(fs.flush, fi.fh)  # the shell closes its first file descriptor before writing
    run(fs.write, fi.fh, 0, data)
    run(fs.flush, fi.fh)
    run(fs.release, fi.fh)
    return entry.st_ino


def shell_redirect_existing(fs: RewindFS, inode: int, data: bytes) -> None:
    """`echo ... > existingfile`: open with truncate, then the same dance."""
    fi = run(fs.open, inode, WRITE_TRUNC, CTX)
    run(fs.flush, fi.fh)
    run(fs.write, fi.fh, 0, data)
    run(fs.flush, fi.fh)
    run(fs.release, fi.fh)


def read_all(fs: RewindFS, inode: int) -> bytes:
    fi = run(fs.open, inode, os.O_RDONLY, CTX)
    data = run(fs.read, fi.fh, 0, 1 << 20)
    run(fs.release, fi.fh)
    return data


def sizes(fs: RewindFS, inode: int) -> list[int]:
    return [v["size_bytes"] for v in fs.db.list_versions(inode)]


def test_new_file_gets_one_version_not_empty_ones(fs: RewindFS) -> None:
    inode = shell_redirect_new(fs, b"t.txt", b"hello\n")
    assert sizes(fs, inode) == [6]


def test_overwrite_with_shorter_content_replaces_it(fs: RewindFS) -> None:
    """Regression: `echo hi > f` after `echo "hello world" > f` gave "hi\\nlo world\\n"."""
    inode = shell_redirect_new(fs, b"f.txt", b"hello world\n")
    shell_redirect_existing(fs, inode, b"hi\n")

    assert read_all(fs, inode) == b"hi\n"
    assert sizes(fs, inode) == [12, 3]


def test_identical_save_does_not_add_a_version(fs: RewindFS) -> None:
    inode = shell_redirect_new(fs, b"same.txt", b"two\n")
    shell_redirect_existing(fs, inode, b"two\n")
    assert sizes(fs, inode) == [4]


def test_emptying_a_file_on_purpose_is_recorded(fs: RewindFS) -> None:
    """`: > file` truncates without writing; that is a real change."""
    inode = shell_redirect_new(fs, b"e.txt", b"data\n")
    fi = run(fs.open, inode, WRITE_TRUNC, CTX)
    run(fs.flush, fi.fh)
    run(fs.release, fi.fh)

    assert read_all(fs, inode) == b""
    assert sizes(fs, inode) == [5, 0]


def test_touch_creates_one_empty_version(fs: RewindFS) -> None:
    fi, entry = run(fs.create, 1, b"new.txt", 0o100644, os.O_WRONLY | os.O_CREAT, CTX)
    run(fs.flush, fi.fh)
    run(fs.release, fi.fh)
    assert sizes(fs, entry.st_ino) == [0]


def test_fsync_saves_a_version_immediately(fs: RewindFS) -> None:
    fi, entry = run(fs.create, 1, b"p.txt", 0o100644, WRITE_TRUNC, CTX)
    run(fs.write, fi.fh, 0, b"aaaa")
    run(fs.fsync, fi.fh, False)
    run(fs.write, fi.fh, 4, b"bbbb")
    run(fs.flush, fi.fh)
    run(fs.release, fi.fh)
    assert sizes(fs, entry.st_ino) == [4, 8]


def test_open_without_truncate_keeps_content(fs: RewindFS) -> None:
    """Opening for append or partial edits must not wipe the file."""
    inode = shell_redirect_new(fs, b"keep.txt", b"abcdef")
    fi = run(fs.open, inode, os.O_WRONLY, CTX)
    run(fs.write, fi.fh, 0, b"XY")
    run(fs.flush, fi.fh)
    run(fs.release, fi.fh)
    assert read_all(fs, inode) == b"XYcdef"
