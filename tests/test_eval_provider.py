"""tests/evals/provider.py: a fixture's recorded dates move to the run date.

The nightly full-mode evals ran April fixtures in September, and the model,
which knows the real date, reported a stale snapshot instead of working the
scenario. rebase_fixture moves every date by whole weeks.

    python3 tests/test_eval_provider.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "evals"))

import provider  # noqa: E402

TODAY = dt.date(2026, 9, 27)          # a Sunday
FIXTURE = {
    "briefing": {"generated_at": "2026-04-09T14:00:00",   # a Thursday
                 "todays_events": [{"start": "2026-04-09T15:00:00", "subject": "Colloquium"}],
                 "note": "Thursday, April 9: lecture April 22, 2026; guest on Apr 14."},
    "ledger_entries": [{"id": "x", "date_identified": "2026-04-02"}],
}


def test_every_date_moves_by_whole_weeks_to_near_the_run_date():
    moved, gen = provider.rebase_fixture(FIXTURE, TODAY)
    new = dt.date.fromisoformat(gen[:10])
    assert gen == "2026-09-24T14:00:00" and abs((new - TODAY).days) <= 3
    assert new.weekday() == dt.date(2026, 4, 9).weekday()
    b = moved["briefing"]
    assert b["todays_events"][0]["start"] == "2026-09-24T15:00:00"
    assert moved["ledger_entries"][0]["date_identified"] == "2026-09-17"
    assert b["note"] == "Thursday, September 24: lecture October 7, 2026; guest on Sep 29."
    assert FIXTURE["briefing"]["generated_at"] == "2026-04-09T14:00:00", "input untouched"


def test_a_fixture_without_generated_at_is_left_alone():
    fx = {"briefing": {"note": "April 9"}}
    assert provider.rebase_fixture(fx, TODAY) == (fx, "")


def test_run_time_names_the_moved_generated_at():
    assert provider._run_time("2026-09-24T14:00:00").startswith("2026-09-24 14:00:00")


def test_a_staging_fixture_may_write_only_its_own_pending_event():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        env = {"state_dir": f"{td}/state", "briefing": f"{td}/state/b.json",
               "ledger_db": f"{td}/state/l.db"}
        note = provider._staging_note(env)
        assert f"{td}/state/pending-event.json" in note and "never to ~/.ea/state" in note


def test_a_leak_into_production_state_is_found_and_removed_only_when_it_is_ours():
    import tempfile
    real = provider.PRODUCTION_STATE
    try:
        with tempfile.TemporaryDirectory() as td:
            provider.PRODUCTION_STATE = Path(td)
            before = provider._production_staged()
            (Path(td) / "pending-event.json").write_text('{"attendees": "avery.calloway88@example.org"}')
            (Path(td) / "pending-event-delete.json").write_text('{"event_id": "real-work"}')
            leaks = provider._production_leaks(before, "avery.calloway88@example.org")
            assert leaks == ["pending-event.json"]
            assert not (Path(td) / "pending-event.json").exists()
            assert (Path(td) / "pending-event-delete.json").exists(), "a live EA's file is left alone"
    finally:
        provider.PRODUCTION_STATE = real


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
