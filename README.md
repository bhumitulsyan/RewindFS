# RewindFS

A versioning filesystem for Linux. Mount a folder through RewindFS and every save keeps the previous version: you can see a file's full history, restore any earlier version, snapshot the whole folder, and see which program changed what.

RewindFS runs in user space using FUSE (Filesystem in Userspace), so it needs no kernel changes.

## Features

| Feature | Command |
| --- | --- |
| Full version history of every file | `rewindfs history <file>` |
| Restore a file to a version number or to a point in time | `rewindfs restore <file> --version N` / `--before "<time>"` |
| Snapshot the whole folder and roll back to it | `rewindfs snapshot create/list/show/restore/delete` |
| Compare two versions | `rewindfs diff <file> --v1 N --v2 M` |
| Activity log of every create, write, rename and delete | `rewindfs log` |
| Which program changed files (name, PID, counts) | `rewindfs who` |
| One program's changes only | `rewindfs log --pid <PID>` |
| Storage and deduplication statistics | `rewindfs stats` |
| Clean up old versions | `rewindfs gc --keep-last N` / `--before "<time>"` |

Deleted files are never lost: deletes are recorded, and the file can be restored from its history.

## How it works

```
 Applications (bash, vim, python, ...)
              │  normal file operations
              ▼
      Linux kernel (VFS)
              │  FUSE
              ▼
      RewindFS daemon (Python, pyfuse3)
        │                      │
        ▼                      ▼
  objects/                 metadata.db (SQLite)
  file contents,           files, versions, snapshots,
  named by SHA-256         activity log, processes
```

- **Copy-on-write:** a save never overwrites old data. The new content is stored as a new object and becomes the file's current version.
- **Content-addressed storage:** each object is named by the SHA-256 hash of its content, so identical content is stored only once (deduplication).
- **Metadata in SQLite:** paths, versions, snapshots and the activity log live in a SQLite database in write-ahead-log mode.
- **Process tracking:** for every change, RewindFS records the program that made it, read from `/proc/<pid>`. Each run of a program is identified by PID plus start time, because Linux reuses PIDs.
- **Crash safety:** objects are written to a temporary file, `fsync`ed and renamed into place. If one operation fails, only that call returns an I/O error and the filesystem stays mounted.
- **Timestamps:** stored in UTC to the microsecond, shown and entered in local time.

## Requirements

- Linux, or Windows 11 with WSL2 (Ubuntu)
- Python 3.11 or newer
- FUSE 3: `sudo apt install -y python3-venv python3-dev build-essential pkg-config libfuse3-dev fuse3`

## Install

```bash
git clone https://github.com/bhumitulsyan/RewindFS.git
cd RewindFS
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
rewindfs --help
```

## Quick demo

Use two terminals.

**Terminal 1: mount and leave it running**

```bash
mkdir -p ~/rw-storage ~/rw-mnt
rewindfs mount ~/rw-storage ~/rw-mnt
```

**Terminal 2: use the folder like any other**

```bash
# Versions and restore
echo "first draft" > ~/rw-mnt/essay.txt
echo "second draft" > ~/rw-mnt/essay.txt
rewindfs history /essay.txt --storage ~/rw-storage
rewindfs restore /essay.txt --version 1 --storage ~/rw-storage
cat ~/rw-mnt/essay.txt                      # first draft

# Recover a deleted file
rm ~/rw-mnt/essay.txt
rewindfs restore /essay.txt --version 2 --storage ~/rw-storage
cat ~/rw-mnt/essay.txt                      # second draft

# Snapshot the folder, change things, roll back
rewindfs snapshot create before-changes --storage ~/rw-storage
echo "oops" > ~/rw-mnt/essay.txt
rewindfs snapshot restore before-changes --storage ~/rw-storage

# See who changed what
rewindfs who --storage ~/rw-storage
rewindfs log --storage ~/rw-storage --limit 20
```

**Stop:** press `Ctrl+C` in Terminal 1, or run `rewindfs umount ~/rw-mnt`.

## Storage layout

```
~/rw-storage/
├── .rewindfs          # format marker
├── .rewindfs.lock     # prevents two mounts of the same storage
├── metadata.db        # SQLite: files, versions, objects, snapshots, events, processes
└── objects/
    └── a3/f9c2...     # file contents, named by SHA-256, sharded by the first 2 characters
```

## Tests

```bash
pytest tests/unit tests/integration tests/stress -q
bash tests/e2e_test.sh            # mounts a real filesystem and exercises it
```

The suite covers metadata, storage, CLI commands, restore and garbage collection, crash handling, timestamps, write and truncate behaviour, and process tracking. GitHub Actions runs the tests, linting (ruff) and type checks (mypy) on every push.

## Limitations

- Linux only (WSL2 on Windows).
- Each save stores the whole file, so very large files that change often use more space.
- Symbolic links, hard links and extended attributes are not supported yet.
