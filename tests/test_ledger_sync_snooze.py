#!/usr/bin/env python3
"""Tests for the ledger_sync candidate set — which rows may be auto-closed.

Two deferral rules live in that SELECT, both for the same reason: when the
EA ITSELF moved the mail out of the inbox, the mail's absence says nothing
about the user.

  * snooze: a snoozed entry with a future `snooze_until` must NOT be a
    candidate. Treating 'snoozed' as unconditionally active closes a
    snoozed-and-archived email the moment ledger_sync runs.
  * follow-up: the documented follow-up flow archives the mail and then
    parks the row with a future `follow_up_after`. Without that guard the
    row closes before its date arrives, silently and unrecoverably, since
    resurrect_due_follow_ups only reopens rows still in status
    'follow_up'.

These tests call `ledger_sync.candidate_rows` directly. An inline COPY of
the SELECT cannot be failed by a change to ledger_sync.
"""

import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import ledger as ledger_mod  # noqa: E402
import ledger_sync  # noqa: E402


def _new_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "ledger.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.executescript(ledger_mod.SCHEMA)
    for m in ledger_mod.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    return db_path


def _insert(db_path: Path, entry_id: str, status: str,
            snooze_until: str | None,
            follow_up_after: str | None = None) -> None:
    conn = sqlite3.connect(str(db_path), timeout=10)
    now = ledger_mod._now()
    conn.execute(
        """
        INSERT INTO ledger (id, type, status, description,
                           source_message_id, snooze_until, follow_up_after,
                           date_identified, last_action, notes,
                           metadata, created_at, updated_at, source)
        VALUES (?, 'response_needed', ?, 'test', 'msg-1', ?, ?, ?, '', '', '{}', ?, ?, NULL)
        """,
        (entry_id, status, snooze_until, follow_up_after, now, now, now),
    )
    conn.commit()
    conn.close()


def _select_rows(db_path: Path) -> list:
    """Call the REAL candidate query, so the test pins shipped behavior."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        return [r[0] for r in ledger_sync.candidate_rows(conn)]
    finally:
        conn.close()


def test_snoozed_with_future_unsnooze_is_skipped():
    """Snoozed entries whose snooze has not fired yet must NOT appear in
    the ledger_sync candidate set."""
    with tempfile.TemporaryDirectory() as td:
        db_path = _new_db(Path(td))
        _insert(db_path, "snoozed-future", "snoozed", "2099-12-31T00:00:00Z")
        _insert(db_path, "open-normal", "open", None)
        ids = _select_rows(db_path)
        assert "snoozed-future" not in ids, (
            f"snoozed entry with future snooze_until leaked into "
            f"candidate set: {ids}"
        )
        assert "open-normal" in ids, ids


def test_snoozed_with_past_unsnooze_is_eligible():
    """Once the snooze window has passed, the entry becomes eligible
    again (matches the existing wake-snoozed pathway)."""
    with tempfile.TemporaryDirectory() as td:
        db_path = _new_db(Path(td))
        _insert(db_path, "snoozed-past", "snoozed", "2020-01-01T00:00:00Z")
        ids = _select_rows(db_path)
        assert "snoozed-past" in ids, ids


def test_snoozed_with_null_unsnooze_is_eligible():
    """Legacy entries with `status='snoozed'` but no snooze_until are
    treated as immediately eligible (fail-open)."""
    with tempfile.TemporaryDirectory() as td:
        db_path = _new_db(Path(td))
        _insert(db_path, "snoozed-null", "snoozed", None)
        ids = _select_rows(db_path)
        assert "snoozed-null" in ids, ids


def test_open_and_follow_up_are_candidates():
    """The open statuses (ledger.OPEN_STATUSES); snoozed rows are tested
    above, by their wake date."""
    with tempfile.TemporaryDirectory() as td:
        db_path = _new_db(Path(td))
        _insert(db_path, "e-open", "open", None)
        _insert(db_path, "e-followup", "follow_up", None)
        ids = set(_select_rows(db_path))
        assert {"e-open", "e-followup"} <= ids, ids


def test_completed_and_closed_excluded():
    with tempfile.TemporaryDirectory() as td:
        db_path = _new_db(Path(td))
        _insert(db_path, "e-completed", "completed", None)
        _insert(db_path, "e-closed", "closed", None)
        ids = _select_rows(db_path)
        assert "e-completed" not in ids
        assert "e-closed" not in ids


# ── follow_up_after deferral ───────────────────────────────────────────

def test_future_follow_up_after_is_excluded():
    """A row parked for a future date is not a candidate. The follow-up
    flow archives the mail as its first step, so the archive is not
    evidence that the row is done."""
    with tempfile.TemporaryDirectory() as td:
        db_path = _new_db(Path(td))
        _insert(db_path, "parked-future", "follow_up", None, "2099-12-31")
        _insert(db_path, "open-normal", "open", None, None)
        ids = _select_rows(db_path)
        assert "parked-future" not in ids, (
            f"row parked for a future date leaked into the candidate "
            f"set: {ids}")
        assert "open-normal" in ids, ids


def test_future_follow_up_after_excluded_regardless_of_status():
    """The guard is on the date, not on the status. A row can carry a
    follow_up_after while sitting in 'open' or 'response_needed'."""
    with tempfile.TemporaryDirectory() as td:
        db_path = _new_db(Path(td))
        for i, status in enumerate(
                ("open", "response_needed", "pending_confirm", "snoozed")):
            _insert(db_path, f"parked-{i}", status, None, "2099-12-31")
        ids = _select_rows(db_path)
        assert ids == [], f"future follow_up_after must defer every status: {ids}"


def test_past_follow_up_after_is_eligible():
    with tempfile.TemporaryDirectory() as td:
        db_path = _new_db(Path(td))
        _insert(db_path, "parked-past", "follow_up", None, "2020-01-01")
        assert "parked-past" in _select_rows(db_path)


def test_today_follow_up_after_is_eligible():
    """Same boundary as ledger.resurrect_due_follow_ups (`<= date('now')`),
    so the row becomes eligible on the day it resurfaces, not the day
    after."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with tempfile.TemporaryDirectory() as td:
        db_path = _new_db(Path(td))
        _insert(db_path, "parked-today", "follow_up", None, today)
        assert "parked-today" in _select_rows(db_path)


def test_null_follow_up_after_is_eligible():
    """The overwhelming majority of rows carry no follow-up date; the new
    guard must not touch them."""
    with tempfile.TemporaryDirectory() as td:
        db_path = _new_db(Path(td))
        _insert(db_path, "e-plain", "open", None, None)
        _insert(db_path, "e-empty", "open", None, "")
        ids = set(_select_rows(db_path))
        assert {"e-plain", "e-empty"} <= ids, ids


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
