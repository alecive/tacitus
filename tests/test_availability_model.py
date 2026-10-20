#!/usr/bin/env python3
"""One availability model, and it says when it could not read a calendar.

Two invariants on one seam:

  The briefing's `availability_merged` comes from
  scheduling_engine.compute_free_slots, which reads the same tokened
  EA-Managed events the solver enforces. There is no second model, no
  hardcoded window, and all-day events are handled as whole days rather
  than timed busy intervals.

  `propose-meeting` and `validate-draft` report a calendar account that
  failed to load. A calendar that cannot be read otherwise looks exactly
  like an empty one, and the skills tell the resident to trust the solver
  output verbatim.

Hermetic: every calendar read is patched. Run with either
`python3 tests/test_availability_model.py` or pytest.
"""

import argparse
import io
import json
import os
import sys
import tempfile
import traceback
from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

# No test may touch production state.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-availmodel-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))

import scheduling_engine as se  # noqa: E402

MT = ZoneInfo("America/Denver")

# A Friday whose working-hours window is 09:00-11:45, shorter than a
# default day.
FRIDAY = date(2026, 8, 21)
MONDAY = date(2026, 8, 24)


def _tokens(spec: dict):
    """Build a _load_token_constraints stand-in from
    {date: [(token, label, start_min, end_min, all_day)]}."""
    def _fn(earliest, latest):
        instances = []
        for d, entries in spec.items():
            if d < earliest or d > latest:
                continue
            for token, label, s, e, all_day in entries:
                instances.append({"token": token, "label": label, "date": d,
                                  "start_min": s, "end_min": e,
                                  "all_day": all_day})
        return {"instances": instances,
                "has_wh": any(i["token"] == "wh" for i in instances)}
    return _fn


FRIDAY_945 = _tokens({
    FRIDAY: [("wh", "wh Friday working hours", 540, 705, False)],
    MONDAY: [("wh", "wh Working Hours", 540, 1020, False),
             ("nm", "nm Lunch", 720, 780, False)],
})


def _timed_event(subject, start_local, end_local, source="Outlook", **over):
    ev = {
        "subject": subject,
        "start": {"dateTime": start_local.astimezone(timezone.utc)
                  .strftime("%Y-%m-%dT%H:%M:%S")},
        "end": {"dateTime": end_local.astimezone(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S")},
        "attendees": [],
    }
    if source != "Outlook":
        ev["source_calendar"] = source
    ev.update(over)
    return ev


def _google_all_day(subject, first: date, exclusive_end: date, email):
    """The shape google_calendar_client.get_events actually emits: naive
    local midnight, an exclusive end date with a 23:59:59 tail, snake-case
    is_all_day."""
    return {
        "subject": subject,
        "start": {"dateTime": first.isoformat() + "T00:00:00"},
        "end": {"dateTime": exclusive_end.isoformat() + "T23:59:59"},
        "attendees": [],
        "source_calendar": email,
        "is_all_day": True,
    }


@contextmanager
def _calendars(outlook=None, google=None, tokens=FRIDAY_945,
               accounts=None, fetch_errors=None):
    """Patch every calendar read scheduling_engine performs."""
    accounts = accounts if accounts is not None else [
        {"email": "jo.ambrose4@example.com", "blocks_availability": True}]
    saved_errors = list(se.gcal.LAST_FETCH_ERRORS)
    se.gcal.LAST_FETCH_ERRORS[:] = list(fetch_errors or [])
    try:
        with patch.object(se.graph, "get_calendar_view",
                          return_value=list(outlook or [])), \
             patch.object(se.gcal, "get_all_events",
                          return_value=list(google or [])), \
             patch.object(se.gcal, "_get_google_accounts",
                          return_value=accounts), \
             patch.object(se, "_load_prefs",
                          return_value={"timezone": "America/Denver"}), \
             patch.object(se, "_load_token_constraints", side_effect=tokens):
            yield
    finally:
        se.gcal.LAST_FETCH_ERRORS[:] = saved_errors


def _free(**kw):
    """compute_free_slots over the FRIDAY..MONDAY window."""
    start = datetime(FRIDAY.year, FRIDAY.month, FRIDAY.day, 0, 0, tzinfo=MT)
    end = datetime(MONDAY.year, MONDAY.month, MONDAY.day, 23, 59, tzinfo=MT)
    return se.compute_free_slots(outlook_events=kw.pop("outlook", []),
                                 google_events=kw.pop("google", []),
                                 start=start, end=end, **kw)


def _local(slot, which="start"):
    return datetime.fromisoformat(slot[which]).astimezone(MT)


def _propose(earliest=FRIDAY, latest=FRIDAY, duration=30, num_options=5):
    args = argparse.Namespace(
        duration=duration, days_ahead=7, num_options=num_options,
        attendees=json.dumps([{"email": "outside@example.com",
                               "name": "Outside", "type": "external"}]),
        constraints="[]",
        earliest_date=earliest.isoformat(), latest_date=latest.isoformat())
    buf = io.StringIO()
    with redirect_stdout(buf):
        se.cmd_propose_meeting(args)
    out = buf.getvalue()
    return json.loads(out[out.index("{"):])


def _validate(times, duration=30):
    args = argparse.Namespace(times=json.dumps(times), duration=duration)
    buf = io.StringIO()
    with redirect_stdout(buf):
        se.cmd_validate_draft(args)
    out = buf.getvalue()
    return json.loads(out[out.index("{"):])


# ---------------------------------------------------------------------------
# Availability comes from the token calendar, like the solver
# ---------------------------------------------------------------------------

def test_availability_obeys_the_tokened_working_hours_window():
    """Published availability stays inside the tokened wh window, whole:
    no slot outside it, and no buffer haircut off a gap with no preceding
    meeting."""
    with _calendars():
        result = _free()
    friday = [s for s in result["slots"] if _local(s).date() == FRIDAY]
    assert friday, "a Friday with a wh window must publish availability"
    for s in friday:
        assert _local(s).time() >= datetime(2026, 8, 21, 9, 0).time()
        assert _local(s, "end").time() <= datetime(2026, 8, 21, 11, 45).time()
    assert not any(_local(s).hour >= 12 for s in friday), \
        "no slot may fall outside the wh window"
    # ...and the window is published whole: no 30-minute haircut off the top.
    assert friday[0]["duration_minutes"] == 165


def test_availability_and_solver_agree_on_the_same_day():
    """Every time the solver offers must be inside a slot the briefing
    calls free. Two models is how a slot the solver rejects ends up in an
    email."""
    busy = [_timed_event("Standing 1:1",
                         datetime(2026, 8, 21, 9, 30, tzinfo=MT),
                         datetime(2026, 8, 21, 10, 0, tzinfo=MT))]
    with _calendars(outlook=busy):
        free = _free(outlook=busy)["slots"]
        proposal = _propose()
    windows = [(_local(s), _local(s, "end")) for s in free
               if _local(s).date() == FRIDAY]
    assert proposal["options"], "solver must find times on this day"
    for opt in proposal["options"]:
        o_start = datetime.fromisoformat(opt["start"]).astimezone(MT)
        o_end = datetime.fromisoformat(opt["end"]).astimezone(MT)
        assert any(w0 <= o_start and o_end <= w1 for w0, w1 in windows), \
            f"solver offered {opt['start']}, which availability calls busy"


def test_a_meeting_removes_its_time_from_availability():
    busy = [_timed_event("Standing 1:1",
                         datetime(2026, 8, 21, 9, 30, tzinfo=MT),
                         datetime(2026, 8, 21, 10, 0, tzinfo=MT))]
    with _calendars(outlook=busy):
        slots = [s for s in _free(outlook=busy)["slots"]
                 if _local(s).date() == FRIDAY]
    assert [(_local(s).strftime("%H:%M"), _local(s, "end").strftime("%H:%M"))
            for s in slots] == [("09:00", "09:30"), ("10:00", "11:45")]


def test_an_nm_block_is_carved_out_and_p_tokens_only_annotate():
    with _calendars():
        monday = [s for s in _free()["slots"] if _local(s).date() == MONDAY]
    assert [(_local(s).strftime("%H:%M"), _local(s, "end").strftime("%H:%M"))
            for s in monday] == [("09:00", "12:00"), ("13:00", "17:00")]


def test_a_multi_day_all_day_conference_removes_every_day_it_covers():
    """A multi-day all-day event removes every day it covers, with a
    reason, and the solver must agree those days are infeasible."""
    conf = [_google_all_day("IROS conference travel", FRIDAY,
                            MONDAY + timedelta(days=1), "jo.ambrose4@example.com")]
    with _calendars(google=conf):
        result = _free(google=conf)
        proposal = _propose(earliest=FRIDAY, latest=MONDAY)
    assert result["slots"] == []
    assert FRIDAY.isoformat() in result["unavailable_days"]
    assert MONDAY.isoformat() in result["unavailable_days"]
    assert "IROS" in result["unavailable_days"][FRIDAY.isoformat()]
    assert [e["subject"] for e in result["all_day_events"]] == \
        ["IROS conference travel"]
    assert proposal["options"] == []
    for d in (FRIDAY, MONDAY):
        assert "IROS" in proposal["infeasible_days"][d.isoformat()]


def test_an_informational_all_day_event_does_not_block_the_day():
    birthday = [_google_all_day("Someone's birthday", FRIDAY,
                                FRIDAY + timedelta(days=1),
                                "jo.ambrose4@example.com")]
    with _calendars(google=birthday):
        slots = _free(google=birthday)["slots"]
    assert any(_local(s).date() == FRIDAY for s in slots)


def test_a_non_blocking_calendar_never_removes_time():
    """blocks_availability: false is honoured here exactly as in the
    solver."""
    spouse = [_timed_event("Spouse dentist",
                           datetime(2026, 8, 21, 9, 30, tzinfo=MT),
                           datetime(2026, 8, 21, 10, 0, tzinfo=MT),
                           source="wren.abernathy86@example.com")]
    accounts = [{"email": "jo.ambrose4@example.com", "blocks_availability": True},
                {"email": "wren.abernathy86@example.com", "blocks_availability": False}]
    with _calendars(google=spouse, accounts=accounts):
        slots = [s for s in _free(google=spouse)["slots"]
                 if _local(s).date() == FRIDAY]
    assert len(slots) == 1 and slots[0]["duration_minutes"] == 165


def test_a_day_with_no_wh_window_publishes_nothing():
    with _calendars():
        result = _free()
    saturday = date(2026, 8, 22)
    assert not any(_local(s).date() == saturday for s in result["slots"])
    assert "no working-hours" in result["unavailable_days"][saturday.isoformat()]


def test_published_slot_shape_is_unchanged():
    """CLAUDE.md documents `availability_merged`; prompt_logic dumps it
    verbatim into the prompt. The keys must not move."""
    with _calendars():
        slot = _free()["slots"][0]
    assert set(slot) >= {"start", "end", "duration_minutes", "day"}
    assert slot["day"] == "Friday Aug 21"
    assert datetime.fromisoformat(slot["start"]).tzinfo is not None
    assert isinstance(slot["duration_minutes"], int)


# ---------------------------------------------------------------------------
# A dead account must not read as "free"
# ---------------------------------------------------------------------------

_DEAD = [{"account": "jo.ambrose4@example.com", "error": "HTTP 401 Unauthorized"}]


def test_propose_meeting_reports_and_warns_on_a_failed_calendar():
    with _calendars(fetch_errors=_DEAD):
        proposal = _propose()
    assert proposal["calendar_fetch_errors"] == _DEAD
    assert proposal["warnings"], "a failed source must produce a warning"
    assert "jo.ambrose4@example.com" in proposal["warnings"][0]
    assert proposal["options"], "warn rather than refuse"
    for opt in proposal["options"]:
        assert "WARNING" in opt["notes"], \
            "the warning must ride on the option the resident reads"


def test_propose_meeting_is_quiet_when_every_calendar_answered():
    with _calendars():
        proposal = _propose()
    assert proposal["calendar_fetch_errors"] == []
    assert proposal["warnings"] == []
    assert all("WARNING" not in o["notes"] for o in proposal["options"])


def test_propose_meeting_reports_fetch_errors_even_when_it_refuses():
    no_tokens = _tokens({})
    with _calendars(tokens=no_tokens, fetch_errors=_DEAD):
        proposal = _propose()
    assert proposal["status"] == "error"
    assert proposal["calendar_fetch_errors"] == _DEAD


def test_validate_draft_raises_a_warning_class_issue_per_time():
    """The bot's send-time preflight prints every non-conflict issue as a
    warning line, so the draft cannot go out silently during an outage.
    `valid` stays true: an unreadable calendar is not a known conflict."""
    with _calendars(fetch_errors=_DEAD):
        result = _validate(["2026-08-21T09:30:00-06:00"])
    fetch_issues = [i for i in result["issues"]
                    if i["type"] == "calendar_fetch_error"]
    assert len(fetch_issues) == 1
    assert fetch_issues[0]["proposed_time"] == "2026-08-21T09:30:00-06:00"
    assert "jo.ambrose4@example.com" in fetch_issues[0]["detail"]
    assert result["calendar_fetch_errors"] == _DEAD
    assert result["valid"] is True


def test_validate_draft_is_quiet_when_every_calendar_answered():
    with _calendars():
        result = _validate(["2026-08-21T09:30:00-06:00"])
    assert result["calendar_fetch_errors"] == []
    assert not [i for i in result["issues"]
                if i["type"] == "calendar_fetch_error"]


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
