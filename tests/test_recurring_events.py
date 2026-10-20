#!/usr/bin/env python3
"""Tests for recurring calendar events (create-event --repeat ...).

The case throughout: a Tue/Thu 2-4pm lecture across a semester must land
as ONE Outlook series of 31 occurrences, so free/busy shows the user
booked instead of listing 31 separate events.

Hermetic — no Graph calls. graph_client.graph_post is patched, and the CLI
layer is driven with an argparse-style Namespace and a stub `graph`
module (the seam tests/test_draft_chain_e2e.py already uses).
"""

import argparse
import io
import json
import sys
import tempfile
import traceback
import types
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))

import graph_client as gc
import graph_actions as ga


LECTURE_START = datetime(2026, 8, 20, 14, 0)
LECTURE_END = datetime(2026, 8, 20, 16, 0)


# --------------------------------------------------------------------------
# build_recurrence — the Graph recurrence object
# --------------------------------------------------------------------------

def test_weekly_days_end_date_builds_graph_recurrence():
    """A Tue/Thu weekly pattern with an end date, in the shape Graph
    expects."""
    rec = gc.build_recurrence(
        repeat="weekly", start=LECTURE_START, days="TU,TH",
        until="2026-12-03", timezone_name="America/Denver")
    assert rec == {
        "pattern": {
            "type": "weekly",
            "interval": 1,
            "daysOfWeek": ["tuesday", "thursday"],
        },
        "range": {
            "type": "endDate",
            "startDate": "2026-08-20",
            "endDate": "2026-12-03",
            "recurrenceTimeZone": "America/Denver",
        },
    }


def test_range_start_date_comes_from_the_event_start():
    """range.startDate anchors on the event's own start date, not 'today'."""
    rec = gc.build_recurrence(repeat="weekly", start=LECTURE_START,
                              days="TU", until="2026-09-01")
    assert rec["range"]["startDate"] == "2026-08-20"


def test_timezone_defaults_to_the_users_timezone():
    """No explicit timezone: the range carries America/Denver."""
    with patch.object(gc, "_get_user_timezone", return_value="America/Denver"):
        rec = gc.build_recurrence(repeat="weekly", start=LECTURE_START,
                                  days="TU,TH", until="2026-12-03")
    assert rec["range"]["recurrenceTimeZone"] == "America/Denver"


def test_daily_with_count_builds_numbered_range():
    rec = gc.build_recurrence(repeat="daily", start=LECTURE_START, count=5,
                              timezone_name="America/Denver")
    assert rec["pattern"] == {"type": "daily", "interval": 1}
    assert rec["range"] == {
        "type": "numbered",
        "startDate": "2026-08-20",
        "numberOfOccurrences": 5,
        "recurrenceTimeZone": "America/Denver",
    }


def test_interval_carries_into_the_pattern():
    """Every other week."""
    rec = gc.build_recurrence(repeat="weekly", start=LECTURE_START,
                              days="FR", interval=2, until="2026-12-03")
    assert rec["pattern"]["interval"] == 2


def test_day_codes_are_case_and_space_insensitive():
    assert gc.parse_repeat_days(" tu , Th ") == ["tuesday", "thursday"]


def test_duplicate_day_codes_collapse():
    assert gc.parse_repeat_days("TU,TU,TH") == ["tuesday", "thursday"]


# --------------------------------------------------------------------------
# build_recurrence — invalid combinations must fail loudly
# --------------------------------------------------------------------------

def _rejects(fn, *args, **kwargs) -> str:
    """Call fn expecting ValueError; return the message."""
    try:
        fn(*args, **kwargs)
    except ValueError as e:
        return str(e)
    raise AssertionError("expected ValueError, got a result")


def test_invalid_day_token_rejected():
    """'TUES' is not a day code — must raise, not silently drop the day."""
    msg = _rejects(gc.parse_repeat_days, "TU,TUES")
    assert "TUES" in msg
    msg = _rejects(gc.build_recurrence, repeat="weekly", start=LECTURE_START,
                   days="TU,XX", until="2026-12-03")
    assert "XX" in msg


def test_empty_day_list_rejected():
    _rejects(gc.parse_repeat_days, " , ")


def test_weekly_without_days_rejected():
    msg = _rejects(gc.build_recurrence, repeat="weekly", start=LECTURE_START,
                   until="2026-12-03")
    assert "--repeat-days" in msg


def test_daily_with_days_rejected():
    msg = _rejects(gc.build_recurrence, repeat="daily", start=LECTURE_START,
                   days="TU,TH", until="2026-12-03")
    assert "weekly" in msg


def test_unknown_repeat_type_rejected():
    msg = _rejects(gc.build_recurrence, repeat="monthly", start=LECTURE_START,
                   until="2026-12-03")
    assert "monthly" in msg


def test_missing_end_of_series_rejected():
    """Neither --repeat-until nor --repeat-count: no open-ended series."""
    msg = _rejects(gc.build_recurrence, repeat="weekly", start=LECTURE_START,
                   days="TU,TH")
    assert "--repeat-until" in msg


def test_until_and_count_together_rejected():
    _rejects(gc.build_recurrence, repeat="weekly", start=LECTURE_START,
             days="TU,TH", until="2026-12-03", count=10)


def test_until_before_start_rejected():
    msg = _rejects(gc.build_recurrence, repeat="weekly", start=LECTURE_START,
                   days="TU,TH", until="2026-01-05")
    assert "before" in msg


def test_malformed_until_date_rejected():
    msg = _rejects(gc.build_recurrence, repeat="weekly", start=LECTURE_START,
                   days="TU,TH", until="12/03/2026")
    assert "YYYY-MM-DD" in msg


def test_zero_interval_and_zero_count_rejected():
    _rejects(gc.build_recurrence, repeat="daily", start=LECTURE_START,
             interval=0, count=5)
    _rejects(gc.build_recurrence, repeat="daily", start=LECTURE_START, count=0)


# --------------------------------------------------------------------------
# graph_client.create_event — recurrence on the posted event body
# --------------------------------------------------------------------------

def _posted_event(**kwargs) -> dict:
    """Run create_event with the network stubbed; return the POSTed body."""
    seen = {}

    def fake_post(endpoint, body):
        seen["endpoint"] = endpoint
        seen["body"] = body
        return {"id": "AAMkEVENT="}

    with patch.object(gc, "graph_post", side_effect=fake_post), \
         patch.object(gc, "_get_user_timezone", return_value="America/Denver"):
        gc.create_event(
            confirm_token=gc.issue_inprocess_approval("event", kwargs["subject"]),
            **kwargs)
    assert seen["endpoint"] == "/me/events"
    return seen["body"]


def test_create_event_attaches_recurrence():
    rec = gc.build_recurrence(repeat="weekly", start=LECTURE_START,
                              days="TU,TH", until="2026-12-03",
                              timezone_name="America/Denver")
    body = _posted_event(subject="ROBO 5100 Lecture", start=LECTURE_START,
                         end=LECTURE_END, location="MBE 155", recurrence=rec)
    assert body["recurrence"] == rec
    # start/end still describe ONE occurrence
    assert body["start"]["dateTime"] == "2026-08-20T14:00:00"
    assert body["end"]["dateTime"] == "2026-08-20T16:00:00"
    assert body["start"]["timeZone"] == "America/Denver"


def test_create_event_without_recurrence_has_no_recurrence_key():
    """The single-event path is untouched: no `recurrence` in the payload."""
    body = _posted_event(subject="1:1", start=LECTURE_START, end=LECTURE_END)
    assert "recurrence" not in body


# --------------------------------------------------------------------------
# CLI layer: cmd_create_event flag handling
# --------------------------------------------------------------------------

def _args(**overrides) -> argparse.Namespace:
    base = {
        "subject": "ROBO 5100 Lecture",
        "start": "2026-08-20T14:00",
        "end": "2026-08-20T16:00",
        "attendees": None,
        "body": "",
        "location": "MBE 155",
        "online": False,
        "confirm_token": None,
        "repeat": None,
        "repeat_days": None,
        "repeat_interval": 1,
        "repeat_until": None,
        "repeat_count": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _run_create_event(tmp: Path, **overrides):
    """Drive cmd_create_event with a real token and a stub graph module.

    Returns (parsed stdout JSON, list of create_event kwargs seen).
    """
    ga.PENDING_CONFIRMS_FILE = tmp / "pending-confirmations.json"
    ga.PENDING_CONFIRMS_LOCK = tmp / "pending-confirmations.lock"
    calls = []
    real_graph = ga.graph
    ga.graph = types.SimpleNamespace(
        build_recurrence=real_graph.build_recurrence,
        to_user_local=real_graph.to_user_local,
        create_event=lambda **kw: calls.append(kw) or {"id": "AAMkEVENT="},
        issue_inprocess_approval=real_graph.issue_inprocess_approval,
    )
    try:
        args = _args(**overrides)
        if overrides.get("confirm_token", "mint") == "mint":
            args.confirm_token = ga._create_confirmation(
                "create_event", args.subject, "test")
        buf = io.StringIO()
        with redirect_stdout(buf):
            ga.cmd_create_event(args)
        return json.loads(buf.getvalue()), calls
    finally:
        ga.graph = real_graph


def test_cli_weekly_series_passes_recurrence_through():
    """The weekly series, end to end through the CLI layer."""
    with tempfile.TemporaryDirectory() as td:
        out, calls = _run_create_event(
            Path(td), repeat="weekly", repeat_days="TU,TH",
            repeat_until="2026-12-03")
    assert out["status"] == "event_created"
    assert len(calls) == 1
    rec = calls[0]["recurrence"]
    assert rec["pattern"]["daysOfWeek"] == ["tuesday", "thursday"]
    assert rec["range"]["startDate"] == "2026-08-20"
    assert rec["range"]["endDate"] == "2026-12-03"
    assert rec["range"]["recurrenceTimeZone"] == "America/Denver"
    # One call = one series, not 31 events
    assert calls[0]["subject"] == "ROBO 5100 Lecture"
    assert calls[0]["attendees"] is None


def test_cli_without_repeat_flags_creates_a_single_event():
    """No --repeat: recurrence is None and the output shape is unchanged."""
    with tempfile.TemporaryDirectory() as td:
        out, calls = _run_create_event(Path(td))
    assert out == {"status": "event_created", "id": "AAMkEVENT="}
    assert calls[0]["recurrence"] is None


def test_cli_invalid_day_token_rejected_and_token_not_consumed():
    """A typo fails loudly, creates nothing, and leaves the token usable."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ga.PENDING_CONFIRMS_FILE = tmp / "pending-confirmations.json"
        ga.PENDING_CONFIRMS_LOCK = tmp / "pending-confirmations.lock"
        args = _args(repeat="weekly", repeat_days="TU,TUES",
                     repeat_until="2026-12-03")
        args.confirm_token = ga._create_confirmation(
            "create_event", args.subject, "test")
        calls = []
        real_graph = ga.graph
        ga.graph = types.SimpleNamespace(
            build_recurrence=real_graph.build_recurrence,
            to_user_local=real_graph.to_user_local,
            create_event=lambda **kw: calls.append(kw) or {"id": "X"},
            issue_inprocess_approval=real_graph.issue_inprocess_approval,
        )
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                ga.cmd_create_event(args)
            out = json.loads(buf.getvalue())
        finally:
            ga.graph = real_graph
        assert calls == []
        assert "TUES" in out["error"]
        # token survives a rejected call
        assert ga._validate_and_consume_token(
            args.confirm_token, "create_event", args.subject)


def test_cli_repeat_flags_without_repeat_rejected():
    """--repeat-days alone must not silently create one event."""
    with tempfile.TemporaryDirectory() as td:
        out, calls = _run_create_event(
            Path(td), repeat_days="TU,TH", repeat_until="2026-12-03")
    assert calls == []
    assert "--repeat" in out["error"]


def test_cli_series_end_in_end_flag_rejected():
    """--end is one occurrence's end; passing the series end is a footgun."""
    with tempfile.TemporaryDirectory() as td:
        out, calls = _run_create_event(
            Path(td), end="2026-12-03T16:00", repeat="weekly",
            repeat_days="TU,TH", repeat_until="2026-12-03")
    assert calls == []
    assert "same day" in out["error"]


def test_cli_recurrence_still_requires_confirm_token():
    """The button gate is unchanged: no token, no event, recurring or not."""
    with tempfile.TemporaryDirectory() as td:
        out, calls = _run_create_event(
            Path(td), confirm_token=None, repeat="weekly",
            repeat_days="TU,TH", repeat_until="2026-12-03")
    assert calls == []
    assert out["error"].startswith("BLOCKED: create-event requires --confirm-token")


def test_cli_bad_token_still_blocks_a_recurring_event():
    with tempfile.TemporaryDirectory() as td:
        out, calls = _run_create_event(
            Path(td), confirm_token="not-a-real-token", repeat="weekly",
            repeat_days="TU,TH", repeat_until="2026-12-03")
    assert calls == []
    assert "invalid, expired, or already-used" in out["error"]


# --------------------------------------------------------------------------
# argparse wiring — the flags are registered on create-event
# (graph_actions builds its parser under `if __name__ == "__main__"`, so
#  drive the real CLI the way tests/test_gmail_client.py does)
# --------------------------------------------------------------------------

PREPROC = Path(__file__).resolve().parent.parent / "preprocessor"


def _cli(*argv) -> "subprocess.CompletedProcess":
    """Run the real CLI. PYTHONPATH is pinned to the parent's sys.path because
    another test module rewrites HOME at import time, which would otherwise
    hide the user site-packages (msal) from the child interpreter."""
    import os
    import subprocess
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return subprocess.run(
        [sys.executable, str(PREPROC / "graph_actions.py"), *argv],
        capture_output=True, text=True, timeout=60, env=env,
    )


def test_create_event_help_lists_the_repeat_flags():
    r = _cli("create-event", "--help")
    assert r.returncode == 0, r.stderr
    for flag in ("--repeat", "--repeat-days", "--repeat-interval",
                 "--repeat-until", "--repeat-count"):
        assert flag in r.stdout, r.stdout


def test_cli_rejects_unknown_repeat_type_at_parse_time():
    """--repeat hourly dies in argparse (choices), before any Graph call."""
    r = _cli("create-event", "--subject", "x",
             "--start", "2026-08-20T14:00", "--end", "2026-08-20T16:00",
             "--repeat", "hourly")
    assert r.returncode == 2, r.stdout
    assert "hourly" in r.stderr


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✅ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
