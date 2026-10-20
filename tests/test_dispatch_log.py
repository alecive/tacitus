"""The dispatch log opens its database the way the owner trust rules require.

The bot, an owner process, writes one row per dispatch into
state/dispatch-log.db in the resident's state directory. The resident owns
that directory, so it can put a link or a hard link at the database's name
or at a name SQLite opens beside it. In an owner process _get_db opens the
database only through ledger.guarded_connect, which refuses such a link,
and never creates the directory or the file by path: a missing file is
created with the resident's access. The resident side creates what is
missing itself.

No test runs sudo: resident_io makes its calls directly here.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# Sandbox before import: DB_PATH is fixed at import from EA_DIR, and
# owner_trust counts a process whose EA_DIR is the live tree as the owner.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-dispatch-log-")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import pytest  # noqa: E402

import dispatch_log as dl  # noqa: E402
import ledger  # noqa: E402
import resident_io as rio  # noqa: E402

OWNER_ROW = "owner-only row"


def _log(**kw):
    return dl.log_claude("hello", "claude_single", [{"name": "Bash", "args_summary": "ls"}],
                         [], [], **kw)


def _victim_db(where: Path) -> Path:
    """An SQLite database only the owner should change."""
    where.mkdir(mode=0o700, parents=True)
    victim = where / "owner.db"
    conn = sqlite3.connect(str(victim))
    conn.execute("CREATE TABLE secrets (v TEXT)")
    conn.execute("INSERT INTO secrets VALUES (?)", (OWNER_ROW,))
    conn.commit()
    conn.close()
    os.chmod(victim, 0o600)
    return victim


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path, monkeypatch):
    """The dispatch log pointed at a scratch state directory, resident_io
    making its calls directly (no child)."""
    path = tmp_path / "ea" / "state" / "dispatch-log.db"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(dl, "DB_PATH", path)
    monkeypatch.setattr(ledger, "_install_tree", lambda: tmp_path / "no-tree")
    monkeypatch.setattr(rio, "_owner_check", lambda: False)

    def no_spawn(argv):
        raise AssertionError("the child was started")

    monkeypatch.setattr(rio, "_spawn", no_spawn)
    return path


@pytest.fixture
def as_owner(monkeypatch):
    """This process is an owner process for the shared-database guard."""
    monkeypatch.setattr(ledger, "_is_owner", lambda: True)


@pytest.fixture
def as_resident(monkeypatch):
    monkeypatch.setattr(ledger, "_is_owner", lambda: False)


def test_the_owner_opens_through_the_guard(db, as_owner, monkeypatch):
    calls = []
    real = ledger.guarded_connect

    def spy(path, **kw):
        calls.append(Path(path))
        return real(path, **kw)

    monkeypatch.setattr(ledger, "guarded_connect", spy)
    dl._get_db().close()
    assert calls == [db]


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_a_link_at_the_database_is_refused_and_its_target_untouched(db, as_owner, tmp_path, kind):
    victim = _victim_db(tmp_path / "owner")
    before = victim.read_bytes()
    if kind == "symlink":
        db.symlink_to(victim)
    else:
        os.link(victim, db)
    raised = []
    for call in (_log, dl.query_recent):
        try:
            call()
        except Exception as e:
            raised.append(type(e))
    assert _tables(victim) == {"secrets"}, "the dispatch schema was written into the target"
    assert victim.read_bytes() == before
    assert raised == [ledger.UnsafeDatabase, ledger.UnsafeDatabase]


@pytest.mark.parametrize("suffix", ledger.SQLITE_SIDECARS)
def test_a_link_at_a_sidecar_is_refused_and_its_target_untouched(db, as_owner, tmp_path, suffix):
    _log()                                   # the database exists, as on a live install
    victim = _victim_db(tmp_path / "owner")
    before = victim.read_bytes()
    Path(str(db) + suffix).symlink_to(victim)
    with pytest.raises(ledger.UnsafeDatabase):
        _log()
    assert victim.read_bytes() == before


def test_the_owner_never_creates_the_directory(db, as_owner, monkeypatch):
    missing = db.parent.parent / "absent" / "dispatch-log.db"
    monkeypatch.setattr(dl, "DB_PATH", missing)
    with pytest.raises(sqlite3.DatabaseError):
        _log()
    assert not missing.parent.exists()


def test_a_missing_database_is_created_with_the_residents_access(db, as_owner, monkeypatch):
    made = []
    real = rio.ensure_file
    monkeypatch.setattr(rio, "ensure_file", lambda p: made.append(p) or real(p))
    row = _log(duration_ms=12)
    assert made == [str(db)]
    assert db.is_file() and not db.is_symlink()
    assert row == 1
    assert [r["duration_ms"] for r in dl.query_recent()] == [12]
    assert made == [str(db)]                 # the second open finds the file


def test_the_owner_writes_and_reads_rows(db, as_owner):
    _log()
    assert dl.log_correction(dl._msg_hash("hello")) is True
    assert dl.log_correction(dl._msg_hash("hello")) is False
    [row] = dl.query_recent()
    assert row["user_correction"] == 1 and row["path"] == "claude_single"
    assert dl.query_precision() == {"claude-efficient": 1}


def test_the_resident_side_creates_the_directory_and_database(db, as_resident, monkeypatch):
    fresh = db.parent.parent / "fresh" / "state" / "dispatch-log.db"
    monkeypatch.setattr(dl, "DB_PATH", fresh)
    assert _log() == 1
    assert fresh.is_file()
    assert [r["path"] for r in dl.query_path_summary()] == ["claude_single"]
