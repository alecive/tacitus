#!/usr/bin/env python3
"""Tests for booking_link.py, the read-only cal.com reader.

The link arrives in untrusted email, so these tests pin what it may make
the tool do: unwrap a redirect, name a cal.com user and event, and nothing
else. The rest pins the slot-to-window conversion the solver depends on and
the propose-meeting call it builds. No test contacts cal.com: the HTTP
reads and the solver run are replaced.

Usage:
    python3 tests/test_booking_link.py
"""

import json
import os
import re
import sys
import tempfile
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-bookinglink-")
# The user's own domain makes an attendee internal (preprocessor/institution.py).
os.makedirs(os.path.join(os.environ["EA_DIR"], "config"), exist_ok=True)
with open(os.path.join(os.environ["EA_DIR"], "config", "user-profile.json"), "w") as _f:
    _f.write('{"email": "owner@example.edu"}')

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "preprocessor"))

import booking_link as bl  # noqa: E402

DENVER = ZoneInfo("America/Denver")
SAFELINK = ("https://nam10.safelinks.protection.outlook.com/?url=https%3A%2F%2F"
            "cal.com%2Fgalehargrove%2Fvip%3Flayout%3Dmobile%26duration%3D30%26date"
            "%3D2026-08-10&data=05%7C02%7Cx%40example.edu&reserved=0")


def _raises(fn, *args):
    try:
        fn(*args)
    except bl.LinkError as e:
        return str(e)
    raise AssertionError(f"{fn.__name__}{args} did not raise LinkError")


# --- the link -------------------------------------------------------------

def test_safelinks_wrapper_is_unwrapped():
    link = bl.parse_link(SAFELINK)
    assert link == {"url": "https://cal.com/galehargrove/vip", "username": "galehargrove",
                    "event_slug": "vip", "duration": 30}, link


def test_google_redirect_is_unwrapped():
    url = "https://www.google.com/url?q=https://cal.com/guscorvino/60min&sa=D&ust=1"
    assert bl.parse_link(url)["event_slug"] == "60min"


def test_plain_event_link():
    link = bl.parse_link("https://cal.com/guscorvino/60min")
    assert (link["username"], link["event_slug"], link["duration"]) == \
        ("guscorvino", "60min", None)


def test_profile_link_has_no_event():
    assert bl.parse_link("https://cal.com/guscorvino")["event_slug"] is None


def test_links_off_cal_com_are_refused():
    for url in ("https://evil.example/cal.com/a/b",
                "https://cal.com.evil.example/a/b",
                "http://cal.com/a/b",
                "https://cal.eu/a/b",
                "javascript:alert(1)",
                ""):
        _raises(bl.parse_link, url)


def test_odd_paths_are_refused():
    for url in ("https://cal.com/a/b/c",
                "https://cal.com/team/acme/intro",
                "https://cal.com/a/..%2F..%2Fv2%2Fbookings",
                "https://cal.com/a+b/30min",
                "https://cal.com/a/b%3Fx"):
        _raises(bl.parse_link, url)


def test_duration_outside_reason_is_ignored():
    assert bl.parse_link("https://cal.com/a/b?duration=0")["duration"] is None
    assert bl.parse_link("https://cal.com/a/b?duration=9999")["duration"] is None
    assert bl.parse_link("https://cal.com/a/b?duration=45")["duration"] == 45


def test_module_only_reads():
    src = (PROJECT_ROOT / "preprocessor" / "booking_link.py").read_text()
    assert not re.search(r"requests\.(post|put|patch|delete|request|Session)\b", src)
    assert bl.API == "https://api.cal.com/v2"
    assert "/bookings" not in src


# --- slots to windows -----------------------------------------------------

def _slot(day, hh, mm, minutes, tz=DENVER):
    s = datetime(2026, 10, day, hh, mm, tzinfo=tz)
    return (s, s + timedelta(minutes=minutes))


def test_overlapping_and_touching_slots_merge():
    slots = [_slot(13, 9, 0, 60), _slot(13, 9, 30, 60), _slot(13, 10, 30, 60),
             _slot(13, 14, 0, 60)]
    assert bl.windows(slots, DENVER) == [
        {"day": "2026-10-13", "start": "09:00", "end": "11:30"},
        {"day": "2026-10-13", "start": "14:00", "end": "15:00"},
    ]


def test_slots_convert_to_local_time():
    utc = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)     # 09:00 MDT
    assert bl.windows([(utc, utc + timedelta(hours=1))], DENVER) == [
        {"day": "2026-10-13", "start": "09:00", "end": "10:00"}]


def test_windows_shrink_to_the_quarter_hour():
    assert bl.windows([_slot(13, 9, 10, 55)], DENVER) == [
        {"day": "2026-10-13", "start": "09:15", "end": "10:00"}]
    assert bl.windows([_slot(13, 9, 5, 5)], DENVER) == []


def test_a_window_across_midnight_splits_and_stops_at_2345():
    out = bl.windows([_slot(13, 23, 0, 120)], DENVER)
    assert out == [{"day": "2026-10-13", "start": "23:00", "end": "23:45"},
                   {"day": "2026-10-14", "start": "00:00", "end": "01:00"}], out


def test_solver_window_matches_propose_meeting():
    assert bl.solver_window(14, date(2026, 10, 7)) == (date(2026, 10, 8), date(2026, 10, 22))
    # Friday: the solver starts on Monday
    assert bl.solver_window(7, date(2026, 10, 9)) == (date(2026, 10, 12), date(2026, 10, 19))


# --- propose --------------------------------------------------------------

class _FakeCal:
    """fetch_slots/event_info stand-ins: per (user, slug), the slots offered
    at the asked duration; records each read."""

    def __init__(self, offers):
        self.offers, self.reads = offers, []

    def fetch_slots(self, username, slug, start, end, tz, duration):
        self.reads.append((username, slug, duration))
        return [_slot(d, h, m, duration or 60) for d, h, m in self.offers[(username, slug)]]

    @staticmethod
    def event_info(username, slug):
        return {}


def _patched(offers):
    cal = _FakeCal(offers)
    saved = (bl.fetch_slots, bl.event_info)
    bl.fetch_slots, bl.event_info = cal.fetch_slots, cal.event_info
    return cal, saved


def _restore(saved):
    bl.fetch_slots, bl.event_info = saved


def test_build_attendees_attaches_windows_and_types():
    cal, saved = _patched({("gus", "60min"): [(13, 13, 0)],
                           ("gale", "vip"): [(13, 12, 0), (13, 13, 0)]})
    try:
        atts, links, duration = bl.build_attendees([
            {"email": "Gus@Example.EDU", "name": "Gus",
             "booking_link": "https://cal.com/gus/60min"},
            {"email": "gale@example.edu", "name": "Gale",
             "booking_link": SAFELINK.replace("galehargrove", "gale")},
            {"email": "bo@example.edu", "name": "Bo"},
            {"email": "x@example.org", "name": "X"},
        ], 14, None, DENVER, "@example.edu")
    finally:
        _restore(saved)
    assert duration == 60
    # the first link fixed the length; the second was read at it, not at
    # its own ?duration=30
    assert cal.reads == [("gus", "60min", None), ("gale", "vip", 60)], cal.reads
    assert [a["type"] for a in atts] == ["internal", "internal", "internal", "external"]
    assert all("booking_link" not in a for a in atts)
    assert atts[0]["availability"] == [{"day": "2026-10-13", "start": "13:00", "end": "14:00"}]
    assert atts[1]["availability"] == [{"day": "2026-10-13", "start": "12:00", "end": "14:00"}]
    assert "availability" not in atts[2]
    assert [l["attendee"] for l in links] == ["Gus", "Gale"]


def test_explicit_duration_wins():
    cal, saved = _patched({("gus", "60min"): [(13, 13, 0)]})
    try:
        _, _, duration = bl.build_attendees(
            [{"email": "gus@example.edu", "booking_link": "https://cal.com/gus/60min"}],
            14, 30, DENVER, "@example.edu")
    finally:
        _restore(saved)
    assert duration == 30 and cal.reads == [("gus", "60min", 30)]


def test_a_link_with_no_open_time_stops_the_run():
    # An external attendee with empty availability would be unconstrained
    # in the solver, the opposite of what an empty link means.
    cal, saved = _patched({("gus", "60min"): []})
    try:
        msg = _raises(bl.build_attendees,
                      [{"email": "gus@example.org", "booking_link": "https://cal.com/gus/60min"}],
                      14, None, DENVER, "@example.edu")
    finally:
        _restore(saved)
    assert "no open time" in msg, msg


def test_no_link_points_to_the_engine():
    msg = _raises(bl.build_attendees, [{"email": "gus@example.edu"}], 14, None,
                  DENVER, "@example.edu")
    assert "propose-meeting" in msg


def test_propose_passes_everything_to_the_engine():
    cal, saved = _patched({("gus", "60min"): [(13, 13, 0)]})
    calls = []
    real_engine = bl._run_engine
    bl._run_engine = lambda argv: calls.append(argv) or {"status": "ok", "options": []}
    try:
        args = SimpleNamespace(
            attendees=json.dumps([{"email": "gus@example.edu", "name": "Gus",
                                   "booking_link": "https://cal.com/gus/60min"}]),
            duration=None, days_ahead=14, num_options=7,
            constraints='[{"type":"preferred_slot"}]', ignore_event=["E1"])
        assert bl.cmd_propose(args) == 0
    finally:
        bl._run_engine = real_engine
        _restore(saved)
    argv = calls[0]
    assert argv[:7] == ["propose-meeting", "--duration", "60", "--days-ahead", "14",
                        "--num-options", "7"], argv
    sent = json.loads(argv[argv.index("--attendees") + 1])
    assert sent[0]["availability"] and sent[0]["type"] == "internal"
    assert argv[-4:] == ["--constraints", '[{"type":"preferred_slot"}]',
                         "--ignore-event", "E1"], argv


# --- routing --------------------------------------------------------------

def test_cal_com_mail_is_a_scheduling_request():
    import preprocess
    body = ("If your assistant can utilize https://cal.com/guscorvino/60min "
            "that would be great.")
    assert preprocess.SCHEDULING_PATTERN.search(body)
    assert preprocess.SCHEDULING_PATTERN.search(SAFELINK)
    assert not preprocess.SCHEDULING_PATTERN.search("see local.com/news for details")


def test_cal_com_message_loads_the_scheduling_guide():
    import prompt_logic
    needed = prompt_logic.detect_needed_modules(
        "Gus sent https://cal.com/guscorvino/60min, find us a time")
    assert "scheduling" in needed, needed
    assert "scheduling" in prompt_logic.detect_needed_modules(f"use this: {SAFELINK}")


def main():
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


if __name__ == "__main__":
    main()
