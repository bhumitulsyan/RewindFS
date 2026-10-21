"""Identify the program behind a filesystem request.

FUSE tells us the process ID (PID) of the caller. A PID alone is not enough:
Linux reuses PIDs, so we also record the process start time, which together
with the PID identifies one specific run of one program. Everything is read
from /proc while the process is still alive (at open time), because by the
time a file is closed and saved the program may already have exited.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")
MAX_CMDLINE = 500


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    start_ticks: int  # process start time in clock ticks since boot (/proc/<pid>/stat field 22)
    comm: str  # short program name, e.g. "python3", "bash", "vim"
    exe: str | None  # full program path; None if not readable (other user's process)
    cmdline: str | None  # command line, truncated
    uid: int | None
    ppid: int | None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _parse_stat(stat_text: str) -> tuple[int, int] | None:
    """Return (ppid, start_ticks) from /proc/<pid>/stat.

    The program name (field 2) is in parentheses and may itself contain
    spaces or parentheses, so split after the LAST ')'.
    """
    try:
        rest = stat_text[stat_text.rindex(")") + 2 :].split()
        # rest[0] is field 3 (state); ppid is field 4, starttime is field 22
        return int(rest[1]), int(rest[19])
    except (ValueError, IndexError):
        return None


def read_process(pid: int, proc_root: Path = PROC) -> ProcessInfo | None:
    """Look up a running process. Returns None if it is gone or pid is 0."""
    if pid <= 0:
        return None
    base = proc_root / str(pid)
    stat_text = _read_text(base / "stat")
    if stat_text is None:
        return None
    parsed = _parse_stat(stat_text)
    if parsed is None:
        return None
    ppid, start_ticks = parsed

    comm = (_read_text(base / "comm") or "?").strip() or "?"

    try:
        exe: str | None = os.readlink(base / "exe")
    except OSError:
        exe = None

    raw_cmd = _read_text(base / "cmdline")
    cmdline = None
    if raw_cmd:
        cmdline = " ".join(part for part in raw_cmd.split("\0") if part)[:MAX_CMDLINE] or None

    uid = None
    status = _read_text(base / "status")
    if status:
        for line in status.splitlines():
            if line.startswith("Uid:"):
                try:
                    uid = int(line.split()[1])
                except (IndexError, ValueError):
                    uid = None
                break

    return ProcessInfo(
        pid=pid,
        start_ticks=start_ticks,
        comm=comm,
        exe=exe,
        cmdline=cmdline,
        uid=uid,
        ppid=ppid,
    )


def read_start_ticks(pid: int, proc_root: Path = PROC) -> int | None:
    """Only the start time; a cheap check of whether a PID is the run we already know."""
    if pid <= 0:
        return None
    stat_text = _read_text(proc_root / str(pid) / "stat")
    if stat_text is None:
        return None
    parsed = _parse_stat(stat_text)
    return parsed[1] if parsed else None
