#!/usr/bin/env python3
"""Tests for the send-time scheduling preflight time-extraction
(_extract_proposed_times in ea-chat-bot.py).

The resident can propose a slot the OR-Tools solver never returned, or
one that goes stale before send, so the preflight validates proposed
draft times against the live calendar. This tests the parser that feeds
it: it must capture DATED proposals and skip declines like "I can't make
10 AM tomorrow", which carry no date anchor.

Helpers are exec'd out of ea-chat-bot.py (the scheduler-trace test
pattern) to avoid importing the module (slack_bolt + isolation gate).

Every case runs on a pinned clock (NOW, or its own `now`): the parser drops
times in the past, so a case read against the real clock fails once its
dates go by.
"""

import json
import os
import re
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
BOT_SRC = (HERE.parent / "ea-chat-bot.py").read_text()
DENVER = ZoneInfo("America/Denver")
NOW = datetime(2026, 6, 1, 9, 0, tzinfo=DENVER)        # a Monday

# The parser reads the user's zone (preprocessor/user_tz.py) from EA_DIR;
# a config of its own pins it to the zone these cases are written in.
_TZ_EA_DIR = Path(tempfile.mkdtemp(prefix="preflight-tz-"))
(_TZ_EA_DIR / "config").mkdir()
(_TZ_EA_DIR / "config" / "user-profile.json").write_text(json.dumps({"timezone": "America/Denver"}))


def _load_raw():
    start = BOT_SRC.index("_MONTHS = {m: i for i, m in enumerate(")
    end = BOT_SRC.index("def _scheduling_preflight(")
    ns = {"re": re, "datetime": datetime, "timedelta": timedelta}
    exec(compile(BOT_SRC[start:end], "ea-chat-bot.py[preflight]", "exec"), ns)
    return ns["_extract_proposed_times"]


def _load():
    """The parser on the pinned clock unless a case passes its own `now`,
    in the pinned zone."""
    fn = _load_raw()

    def call(body, now=NOW):
        before = os.environ.get("EA_DIR")
        os.environ["EA_DIR"] = str(_TZ_EA_DIR)
        try:
            return fn(body, now)
        finally:
            if before is None:
                os.environ.pop("EA_DIR", None)
            else:
                os.environ["EA_DIR"] = before
    return call


ELI_BODY = """Hi Eli,

Apologies for the short notice, but a conflict just came up and I can not make 10 AM tomorrow. A few alternatives that would work:

- Tuesday Jun 23: 1:00 PM MT
- Wednesday Jun 24: 10:45 AM MT
- Thursday Jun 25: 9:15 AM MT

Let me know which works and I will update the invite.

Best,
Kit"""


def test_eli_draft_extracts_three_dated_proposals_only():
    extract = _load()
    times = extract(ELI_BODY)
    # the three dated options, NOT "10 AM tomorrow" (no date anchor)
    assert len(times) == 3, times
    hhmm = sorted(t[11:16] for t in times)
    assert hhmm == ["09:15", "10:45", "13:00"], hhmm
    # none should be the 10:00 decline
    assert not any(t[11:16] == "10:00" for t in times)


def test_decline_line_without_anchor_skipped():
    extract = _load()
    assert extract("I can't make 10 AM tomorrow.") == []
    assert extract("Let's talk at 3pm sometime.") == []


def test_month_day_proposal():
    extract = _load()
    times = extract("How about Aug 4: 2:30 PM?")
    assert len(times) == 1
    assert times[0][5:10] == "08-04"
    assert times[0][11:16] == "14:30"


def test_weekday_only_next_occurrence():
    extract = _load()
    assert NOW.weekday() == 0
    assert extract("Wednesday at 9 AM works.") == ["2026-06-03T09:00:00-06:00"]


def test_weekday_already_gone_today_is_next_week():
    extract = _load()
    wed_afternoon = datetime(2026, 10, 7, 16, 30, tzinfo=DENVER)
    assert wed_afternoon.weekday() == 2
    assert extract("Wednesday at 9 AM works.", wed_afternoon) == \
        ["2026-10-14T09:00:00-06:00"]


def test_weekday_later_today_is_today():
    extract = _load()
    wed_morning = datetime(2026, 10, 7, 8, 0, tzinfo=DENVER)
    assert extract("Wednesday at 9 AM works.", wed_morning) == \
        ["2026-10-07T09:00:00-06:00"]
    # within the hour after it started, it is still today's meeting
    assert extract("Wednesday at 9 AM works.", wed_morning.replace(hour=9, minute=30)) == \
        ["2026-10-07T09:00:00-06:00"]


def test_the_default_clock_is_the_real_one():
    fn = _load_raw()
    times = fn("Wednesday at 9 AM works.")
    assert len(times) == 1
    dt = datetime.fromisoformat(times[0])
    assert dt.weekday() == 2 and dt.hour == 9
    assert dt >= datetime.now(DENVER) - timedelta(hours=1)


def test_pm_conversion_and_noon_midnight():
    extract = _load()
    t = extract("How about Friday Dec 5: 12:00 PM?")[0]
    assert t[11:16] == "12:00"        # 12 PM -> 12:00
    t2 = extract("How about Friday Dec 5: 12:30 AM?")[0]
    assert t2[11:16] == "00:30"       # 12:30 AM -> 00:30


def test_empty_and_no_times():
    extract = _load()
    assert extract("") == []
    assert extract("Thanks, talk soon.") == []


def test_caps_at_six():
    extract = _load()
    body = "A few options:\n" + "\n".join(f"Monday Jun {d}: 9:00 AM" for d in range(1, 12))
    assert len(extract(body)) <= 6


CAM_BODY = """Hi Cam,
The first lunch is on Wednesday, September 23, 12 to 2 pm, so I would like to announce it and settle the rest of the plan. Could you help with a few things?
1. Confirm the room reservation went through for both dates.
Thank you again,
Kit

From: Cam Underhill
Sent: Monday, August 31, 2026 3:49 PM
To: Calloway, Kit
Subject: RE: A monthly lunch series
Wednesday Sep 16 at 2:00 PM would be my pick.
On Mon, Aug 31, 2026 at 3:44 PM Kit wrote:
Wednesday Sep 23 at 2:00 PM works for me."""


def test_announcement_with_quoted_history_is_not_a_proposal():
    """A draft that names when an event happens, above a quoted thread full
    of header timestamps and old proposals, proposes nothing."""
    extract = _load()
    assert extract(CAM_BODY) == []


def test_quoted_history_is_never_scanned():
    extract = _load()
    body = ("How about Thursday Oct 8 at 3 PM?\n\n"
            "From: Someone\nSent: Monday, August 31, 2026 3:49 PM\n"
            "Friday Oct 9 at 10 AM would work for me.")
    times = extract(body)
    assert len(times) == 1 and times[0][11:16] == "15:00", times


def test_range_yields_its_start():
    extract = _load()
    assert extract("Could you do Thursday Oct 8, 12 to 2 pm?")[0][11:16] == "12:00"
    assert extract("Could you do Thursday Oct 8, 11 to 1 pm?")[0][11:16] == "11:00"
    assert extract("Could you do Thursday Oct 8, 1:00-1:30 PM?")[0][11:16] == "13:00"


def test_past_times_are_references_not_proposals():
    extract = _load()
    mid_september = datetime(2026, 9, 15, 9, 0, tzinfo=DENVER)
    assert extract("Monday Aug 31 at 3:49 PM works for me.", mid_september) == []


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
