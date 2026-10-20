#!/usr/bin/env python3
"""Tests for calendar fetch-failure surfacing.

A Google account whose token expired returns [] from get_events, which
the availability solver would otherwise read as "completely free". The
failure is carried instead:
  - google_calendar_client.LAST_FETCH_ERRORS records per-account errors
  - scheduling_engine.compute_free_slots returns them alongside the slots
  - preprocess.compute_merged_availability snapshots them into
    preprocess.LAST_CALENDAR_FETCH_ERRORS
  - briefing dicts carry them as `calendar_fetch_errors` for the skills
    to surface as a :warning: line.

The solver half of the same channel (`propose-meeting` /
`validate-draft`) is pinned in test_availability_model.py.
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

# No test may touch production state. google_calendar_client does
# AUTH_DIR.mkdir() and preprocess does STATE_DIR.mkdir() at import, so EA_DIR
# must be a sandbox BEFORE the imports below; unset, it resolves to the live
# ~/.ea.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-calfetch-")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import google_calendar_client as gcal  # noqa: E402
import scheduling_engine as sched_engine  # noqa: E402
import preprocess  # noqa: E402


def test_token_color_polarity_mapping():
    """Tokened events are colored by polarity so the calendar reads at a glance:
    green wh, red nm/p0, amber p1/p2, graphite wr; plain events get no color."""
    assert gcal._token_color("wh Working Hours") == "10"     # green
    assert gcal._token_color("nm Lunch") == "11"             # red
    assert gcal._token_color("p0 Board meeting") == "11"     # red
    assert gcal._token_color("p1 Avoid if possible") == "6"  # amber
    assert gcal._token_color("p2 Prefer mornings") == "6"    # amber
    assert gcal._token_color("wr Remote day") == "8"         # graphite
    assert gcal._token_color("HOLD: prep time") is None      # plain -> no color
    assert gcal._token_color("") is None


def test_get_all_events_resets_error_journal():
    gcal.LAST_FETCH_ERRORS.append({"account": "stale@x", "error": "old"})
    orig_accounts = gcal._get_google_accounts
    orig_ea = gcal._ea_managed_source
    gcal._get_google_accounts = lambda: []
    gcal._ea_managed_source = lambda: None  # isolate from the EA-Managed source
    try:
        gcal.get_all_events(days_ahead=1)
        assert gcal.LAST_FETCH_ERRORS == [], "journal must reset per fetch"
    finally:
        gcal._get_google_accounts = orig_accounts
        gcal._ea_managed_source = orig_ea


def test_failed_account_lands_in_journal():
    orig_accounts = gcal._get_google_accounts
    orig_build = gcal._build_service
    orig_ea = gcal._ea_managed_source
    gcal._get_google_accounts = lambda: [{"email": "gray.isenberg27@example.com"}]
    gcal._build_service = lambda email: None  # simulate auth failure
    gcal._ea_managed_source = lambda: None  # isolate from the EA-Managed source
    try:
        events = gcal.get_all_events(days_ahead=1)
        assert events == []
        assert len(gcal.LAST_FETCH_ERRORS) == 1
        assert gcal.LAST_FETCH_ERRORS[0]["account"] == "gray.isenberg27@example.com"
        assert "auth" in gcal.LAST_FETCH_ERRORS[0]["error"]
    finally:
        gcal._get_google_accounts = orig_accounts
        gcal._build_service = orig_build
        gcal._ea_managed_source = orig_ea


def test_ea_managed_fetch_failure_lands_in_journal():
    """The EA-Managed hold calendar is a first-class busy source: if it fails to
    load, that must surface too (else the EA could double-book its own holds)."""
    orig_accounts = gcal._get_google_accounts
    orig_build = gcal._build_service
    orig_ea = gcal._ea_managed_source
    gcal._get_google_accounts = lambda: []
    gcal._build_service = lambda email: None  # EA-Managed fetch also fails
    gcal._ea_managed_source = lambda: {"email": "jo.ambrose4@example.com",
                                       "calendar_id": "tam.merriweather48@example.com"}
    try:
        gcal.get_all_events(days_ahead=1)
        accounts = [e["account"] for e in gcal.LAST_FETCH_ERRORS]
        assert "jo.ambrose4@example.com" in accounts or "ea-managed" in accounts
    finally:
        gcal._get_google_accounts = orig_accounts
        gcal._build_service = orig_build
        gcal._ea_managed_source = orig_ea


def _wh_tokens(earliest, latest):
    """A plain 09:00-17:00 wh window on every day of the horizon, so
    compute_free_slots has a preference model and does not report the
    'no wh' pseudo-error."""
    from datetime import timedelta
    instances, d = [], earliest
    while d <= latest:
        instances.append({"token": "wh", "label": "wh Working Hours",
                          "date": d, "start_min": 540, "end_min": 1020,
                          "all_day": False})
        d += timedelta(days=1)
    return {"instances": instances, "has_wh": True}


def test_compute_merged_availability_snapshots_errors():
    """The briefing's availability now comes from the solver's model, so
    the fetch-error channel runs through it too."""
    orig_all = gcal.get_all_events
    orig_tokens = sched_engine._load_token_constraints
    orig_accounts = gcal._get_google_accounts

    def fake_all_events(**kw):
        gcal.LAST_FETCH_ERRORS.clear()
        gcal.LAST_FETCH_ERRORS.append(
            {"account": "gray.isenberg27@example.com", "error": "401 token expired"})
        return []

    gcal.get_all_events = fake_all_events
    gcal._get_google_accounts = lambda: []
    sched_engine._load_token_constraints = _wh_tokens
    try:
        preprocess.compute_merged_availability([])
        errs = preprocess.LAST_CALENDAR_FETCH_ERRORS
        assert len(errs) == 1
        assert errs[0]["account"] == "gray.isenberg27@example.com"
        # Snapshot is a copy, not an alias of the live journal
        gcal.LAST_FETCH_ERRORS.clear()
        assert len(preprocess.LAST_CALENDAR_FETCH_ERRORS) == 1
    finally:
        gcal.get_all_events = orig_all
        gcal._get_google_accounts = orig_accounts
        sched_engine._load_token_constraints = orig_tokens


def test_healthy_fetch_clears_stale_errors():
    orig_all = gcal.get_all_events
    orig_tokens = sched_engine._load_token_constraints
    orig_accounts = gcal._get_google_accounts
    preprocess.LAST_CALENDAR_FETCH_ERRORS = [{"account": "x", "error": "old"}]

    def fake_all_events(**kw):
        gcal.LAST_FETCH_ERRORS.clear()
        return []

    gcal.get_all_events = fake_all_events
    gcal._get_google_accounts = lambda: []
    sched_engine._load_token_constraints = _wh_tokens
    try:
        preprocess.compute_merged_availability([])
        assert preprocess.LAST_CALENDAR_FETCH_ERRORS == []
    finally:
        gcal.get_all_events = orig_all
        gcal._get_google_accounts = orig_accounts
        sched_engine._load_token_constraints = orig_tokens


def test_unreadable_ea_managed_calendar_is_itself_a_fetch_error():
    """No wh events readable means no availability model at all. The
    briefing must publish nothing and SAY so in the field the skills
    already treat as 'availability is not authoritative', rather than
    publish a day of invented free time."""
    orig_all = gcal.get_all_events
    orig_tokens = sched_engine._load_token_constraints
    orig_accounts = gcal._get_google_accounts

    gcal.get_all_events = lambda **kw: (gcal.LAST_FETCH_ERRORS.clear() or [])
    gcal._get_google_accounts = lambda: []
    sched_engine._load_token_constraints = lambda a, b: {"instances": [],
                                                         "has_wh": False}
    try:
        slots = preprocess.compute_merged_availability([])
        assert slots == []
        errs = preprocess.LAST_CALENDAR_FETCH_ERRORS
        assert [e["account"] for e in errs] == ["ea-managed"]
        assert "working-hours" in errs[0]["error"]
    finally:
        gcal.get_all_events = orig_all
        gcal._get_google_accounts = orig_accounts
        sched_engine._load_token_constraints = orig_tokens


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
