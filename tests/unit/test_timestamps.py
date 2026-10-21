"""Timestamps must be precise enough to order events inside one second,
and times typed by the user are their local time, not UTC."""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rewindfs.cli import _display_time, _to_db_time, app
from rewindfs.metadata import MetadataDB, utc_now_text

MICROSECOND_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}$")


@pytest.fixture
def db(tmp_path: Path) -> MetadataDB:
    mdb = MetadataDB(tmp_path / "metadata.db")
    mdb.connect()
    mdb.initialize()
    yield mdb
    mdb.close()


@pytest.fixture
def india_time():
    """Run a test as if the computer's clock were set to India (UTC+05:30)."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Kolkata"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_versions_and_events_have_microseconds(db: MetadataDB) -> None:
    inode = db.create_file(1, "a.txt", "/a.txt")
    db.create_version(inode, "h1", 1)

    version = db.get_current_version(inode)
    event = db.list_events(limit=1)[0]
    assert MICROSECOND_TEXT.match(version["created_at"])
    assert MICROSECOND_TEXT.match(event["created_at"])
    assert MICROSECOND_TEXT.match(db.get_file(inode)["created_at"])


def test_restore_point_inside_the_same_second(db: MetadataDB) -> None:
    """Pick the version from just before a moment, even when all saves are within one second."""
    inode = db.create_file(1, "doc.txt", "/doc.txt")
    db.create_version(inode, "good", 4)
    time.sleep(0.002)
    attack_started = utc_now_text()
    time.sleep(0.002)
    db.create_version(inode, "encrypted", 4)

    before = db.get_latest_version_before(inode, attack_started)
    assert before["object_hash"] == "good"


def test_old_second_precision_rows_still_sort_correctly(db: MetadataDB) -> None:
    inode = db.create_file(1, "old.txt", "/old.txt")
    db.create_version(inode, "from_old_storage", 1)
    db.db.execute(
        "UPDATE versions SET created_at = '2000-01-01 00:00:00' WHERE object_hash = ?",
        ("from_old_storage",),
    )
    db.db.commit()
    db.create_version(inode, "new", 1)

    hashes = [v["object_hash"] for v in db.list_versions(inode)]
    assert hashes == ["from_old_storage", "new"]


def test_typed_time_is_local_time(india_time) -> None:
    # 1:00 AM on 21 Oct in India is 7:30 PM on 20 Oct in UTC
    assert _to_db_time(datetime(2026, 10, 21, 1, 0, 0)) == "2026-10-20 19:30:00.000000"


def test_times_are_shown_in_local_time(india_time) -> None:
    assert _display_time("2026-10-20 19:30:00.123456") == "2026-10-21 01:00:00.123"
    assert _display_time("2026-10-20 19:30:00") == "2026-10-21 01:00:00.000"
    assert _display_time(None) == "-"


def test_restore_before_uses_local_time(tmp_path: Path, india_time) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / ".rewindfs").write_text(json.dumps({"version": 1, "hash_algo": "sha256"}))
    db = MetadataDB(storage / "metadata.db")
    db.connect()
    db.initialize()
    inode = db.create_file(1, "r.txt", "/r.txt")
    db.create_version(inode, "v1", 2)
    time.sleep(0.01)
    local_cutoff = datetime.now()  # what the user reads off their clock
    time.sleep(0.01)
    db.create_version(inode, "v2", 2)
    db.close()

    result = CliRunner().invoke(
        app,
        [
            "restore",
            "/r.txt",
            "--before",
            local_cutoff.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "--storage",
            str(storage),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["target_hash"] == "v1"
