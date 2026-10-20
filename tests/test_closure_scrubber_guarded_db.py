#!/usr/bin/env python3
"""The closure scrubber opens the shared ledger only through the guard.

The scrubber runs in the owner's briefing preprocess, and the ledger
database sits in the resident's state directory, where the resident can put
a symlink or a hard link at the database's name. Its two database opens,
the close (`_settle_entry`) and the project-index read
(`build_project_index`), go through ledger.guarded_connect as an owner
process: a link at the name is refused and the file it names is neither
written nor read. On a plain database the same opens still close the row
and read the index.

The owner branch is taken through ledger's `_is_owner` seam, on scratch
files only; resident_io runs direct, so no test starts a child or runs sudo.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

EA_TMP = Path(tempfile.mkdtemp(prefix="ea-test-scrubber-guard-"))
os.environ["EA_DIR"] = str(EA_TMP)
for _sub in ("config", "state", "logs"):
    (EA_TMP / _sub).mkdir(parents=True, exist_ok=True)

import closure_scrubber as cs  # noqa: E402
import ledger  # noqa: E402
import project_store  # noqa: E402
import resident_io as rio  # noqa: E402

ROW = "row-1"
PROJECT = "proj-1"
MEMBER = "kai.quillon21@example.edu"


@pytest.fixture
def owner_db(monkeypatch, tmp_path):
    """The ledger's path in a scratch state directory, with
    guarded_connect taking its owner branch."""
    def no_spawn(argv):
        raise AssertionError("the resident_io child was started")
    monkeypatch.setattr(rio, "_owner_check", lambda: False)
    monkeypatch.setattr(rio, "_spawn", no_spawn)
    monkeypatch.setattr(ledger, "_is_owner", lambda: True)
    monkeypatch.setattr(ledger, "_install_tree", lambda: tmp_path / "no-tree")
    monkeypatch.setattr(cs, "LOG_PATH", tmp_path / "logs" / "closure-scrubber.log")
    state = tmp_path / "state"
    state.mkdir()
    return state / "obligations-ledger.db"


def _make_ledger(path: Path) -> None:
    """A WAL ledger with one open row, filed as the sole open row of a
    project that names one member."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(ledger.SCHEMA)
    conn.executescript(project_store.SCHEMA)
    conn.execute(
        "INSERT INTO ledger (id, type, status, last_action, created_at, updated_at) "
        "VALUES (?, 'response_needed', 'open', 'filed', 'a', 'b')", (ROW,))
    conn.execute("INSERT INTO project_ledger (project_id, ledger_id) VALUES (?, ?)",
                 (PROJECT, ROW))
    conn.execute("INSERT INTO project_people (project_id, email) VALUES (?, ?)",
                 (PROJECT, MEMBER))
    conn.commit()
    conn.close()


def _row(path: Path) -> tuple:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT status, last_action FROM ledger WHERE id = ?",
                            (ROW,)).fetchone()
    finally:
        conn.close()


def _decoy(tmp_path: Path) -> Path:
    """A ledger outside the state directory, standing for a database only
    the owner should change."""
    decoy = tmp_path / "owner-only.db"
    _make_ledger(decoy)
    return decoy


def _plant(kind: str, decoy: Path, at: Path) -> None:
    if kind == "symlink":
        at.symlink_to(decoy)
    else:
        os.link(decoy, at)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_a_close_through_a_link_is_refused_and_the_linked_row_stays_open(
        owner_db, tmp_path, kind):
    decoy = _decoy(tmp_path)
    _plant(kind, decoy, owner_db)
    with pytest.raises(ledger.UnsafeDatabase):
        cs._settle_entry(ROW, "user_replied_outlook", "user_replied_outlook (test)", db_path=owner_db)
    assert _row(decoy) == ("open", "filed")


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_the_project_index_does_not_read_a_linked_ledger(owner_db, tmp_path, kind):
    decoy = _decoy(tmp_path)
    _plant(kind, decoy, owner_db)
    assert cs.build_project_index([{"id": ROW}], db_path=owner_db) == {}


def test_the_project_index_never_creates_a_missing_ledger(owner_db):
    assert cs.build_project_index([{"id": ROW}], db_path=owner_db) == {}
    assert not owner_db.exists()


def test_a_plain_ledger_is_closed_and_indexed_through_the_guard(owner_db):
    _make_ledger(owner_db)
    assert cs.build_project_index([{"id": ROW}], db_path=owner_db) == {
        ROW: (PROJECT, frozenset({MEMBER}))}
    assert cs._settle_entry(ROW, "user_replied_outlook", "user_replied_outlook (test)",
                            db_path=owner_db) == "closed"
    status, last_action = _row(owner_db)
    assert status == "completed"
    assert last_action == ("Auto-closed by closure_scrubber: user_replied_outlook "
                           "(test). Previous: filed")


def test_both_opens_go_through_the_guard(owner_db, monkeypatch):
    _make_ledger(owner_db)
    calls = []
    real = ledger.guarded_connect

    def spy(path, **kw):
        calls.append((Path(path), kw.get("read_only", False)))
        return real(path, **kw)

    monkeypatch.setattr(ledger, "guarded_connect", spy)
    cs.build_project_index([{"id": ROW}], db_path=owner_db)
    cs._settle_entry(ROW, "user_replied_outlook", "reason", db_path=owner_db)
    assert calls == [(owner_db, True), (owner_db, False)]
