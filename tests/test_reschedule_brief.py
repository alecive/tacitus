#!/usr/bin/env python3
"""The reschedule brief's decisions are made in code, the same every time.

A "can we move our meeting?" email gets one solver call that returns the
meeting(s) with the sender, their history with the user, options ranked
by that history, and a recommendation: stage the Reschedule button for
the obvious slot, or offer options. These tests pin the pure parts on
fake calendar data; nothing touches Graph.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("EA_DIR", tempfile.mkdtemp(prefix="ea-test-brief-"))
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import scheduling_engine as se  # noqa: E402

OAKLEY = "wren.abernathy53@example.edu"
NOW = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)


def _ev(start_local: str, minutes: int = 45, attendee: str = OAKLEY, subject: "str" = "Oakley / Kit",
        typ: str = "occurrence", eid: str | None = None) -> dict:
    st = datetime.fromisoformat(start_local).replace(tzinfo=se.USER_TZ)
    en = st + timedelta(minutes=minutes)
    return {"id": eid or f"E-{start_local}", "subject": subject, "type": typ,
            "start": {"dateTime": st.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000")},
            "end": {"dateTime": en.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000")},
            "attendees": [{"emailAddress": {"address": attendee.title()}}],
            "organizer": {"emailAddress": {"address": "wren.fairbanks84@example.edu"}}}


def _history():
    # eight Tuesdays at 11:15, one Thursday at 14:00, all in the past
    evs = [_ev(f"2026-0{m}-{d:02d}T11:15:00") for m, d in
           ((7, 7), (7, 14), (7, 21), (7, 28), (8, 4), (8, 11), (8, 18), (8, 25))]
    evs.append(_ev("2026-08-27T14:00:00"))
    evs.append(_ev("2026-08-26T09:00:00", attendee="someone@else.org", subject="Other"))
    return evs


def test_events_with_attendee_matches_case_insensitively_and_skips_cancelled():
    evs = _history() + [dict(_ev("2026-09-22T11:15:00"), isCancelled=True)]
    mine = se.events_with_attendee(evs, OAKLEY.upper())
    assert len(mine) == 9 and all("Oakley" in e["subject"] for e in mine)
    assert se.events_with_attendee(evs, "") == []


def test_history_profile_reads_the_usual_slot():
    p = se.history_profile(_history(), OAKLEY, NOW)
    assert p["count"] == 9
    assert p["usual_weekday"] == "Tue" and p["usual_start"] == "11:15"
    assert p["usual_duration_min"] == 45
    assert list(p["weekdays"])[0] == "Tue" and p["weekdays"]["Thu"] == 1
    assert p["last_met"].startswith("2026-08-27")
    empty = se.history_profile(_history(), "blake.ravensworth37@example.org", NOW)
    assert empty["count"] == 0 and empty["usual_weekday"] == ""


def test_history_fit_rewards_their_weekday_and_hour():
    p = se.history_profile(_history(), OAKLEY, NOW)
    same = se.history_fit("2026-09-29T11:15:00", p)          # Tue 11:15
    near = se.history_fit("2026-09-29T12:15:00", p)          # Tue, an hour off
    thu = se.history_fit("2026-10-01T11:15:00", p)           # Thu (seen once) same hour
    fri = se.history_fit("2026-10-02T16:00:00", p)           # never met on Fri, far hour
    assert same == 1.0 and 0.75 <= near < 1.0 and 0.7 <= thu <= 0.8 and fri == 0.0
    assert se.history_fit("2026-09-29T11:15:00", {"count": 0}) == 0.5     # neutral


def test_rank_options_by_history_keeps_solver_cost_visible():
    p = se.history_profile(_history(), OAKLEY, NOW)
    opts = [{"rank": 1, "start": "2026-10-02T16:00:00-06:00", "end": "2026-10-02T16:45:00-06:00", "cost": 1.0},
            {"rank": 2, "start": "2026-09-29T11:15:00-06:00", "end": "2026-09-29T12:00:00-06:00", "cost": 2.0}]
    ranked = se.rank_options_by_history(opts, p)
    assert [o["rank"] for o in ranked] == [2, 1]
    assert ranked[0]["history_fit"] == 1.0 and ranked[0]["cost"] == 2.0


def _meeting():
    return {"id": "EVT1", "subject": "Oakley / Kit", "start": "2026-09-22T11:15-06:00",
            "end": "2026-09-22T12:00-06:00", "duration_min": 45}


def test_recommend_stages_a_clean_proposed_time():
    p = se.history_profile(_history(), OAKLEY, NOW)
    validated = [{"time": "2026-09-23T15:00:00", "valid": False, "issues": [{"type": "conflict"}]},
                 {"time": "2026-09-29T11:15:00", "valid": True, "issues": []}]
    rec = se.recommend_reschedule([_meeting()], [], validated, p, 45)
    assert rec["action"] == "stage_reschedule" and rec["event_id"] == "EVT1"
    assert rec["start"].startswith("2026-09-29T11:15") and rec["end"].startswith("2026-09-29T12:00")
    assert "proposed" in rec["basis"]


def test_recommend_stages_the_usual_slot_when_the_solver_offers_it():
    p = se.history_profile(_history(), OAKLEY, NOW)
    opts = se.rank_options_by_history(
        [{"rank": 1, "start": "2026-09-29T11:15:00-06:00", "end": "2026-09-29T12:00:00-06:00", "cost": 1.0, "conditional": None},
         {"rank": 2, "start": "2026-09-30T14:00:00-06:00", "end": "2026-09-30T14:45:00-06:00", "cost": 1.0, "conditional": None}], p)
    rec = se.recommend_reschedule([_meeting()], opts, [], p, 45)
    assert rec["action"] == "stage_reschedule" and rec["start"].startswith("2026-09-29T11:15")
    assert "usual slot" in rec["basis"]


def test_recommend_offers_options_when_nothing_is_obvious():
    p = se.history_profile(_history(), OAKLEY, NOW)
    # only off-pattern options
    opts = se.rank_options_by_history(
        [{"rank": 1, "start": "2026-10-02T16:00:00-06:00", "end": "2026-10-02T16:45:00-06:00", "cost": 1.0, "conditional": None},
         {"rank": 2, "start": "2026-10-01T16:00:00-06:00", "end": "2026-10-01T16:45:00-06:00", "cost": 1.2, "conditional": None}], p)
    rec = se.recommend_reschedule([_meeting()], opts, [], p, 45)
    assert rec["action"] == "offer_options" and len(rec["options"]) == 2
    # a conditional option never stages by itself, even at perfect fit
    cond = se.rank_options_by_history(
        [{"rank": 1, "start": "2026-09-29T11:15:00-06:00", "end": "2026-09-29T12:00:00-06:00", "cost": 1.0,
          "conditional": "if the 1:1 with X moves"}], p)
    assert se.recommend_reschedule([_meeting()], cond, [], p, 45)["action"] == "offer_options"
    # a proposed time that fails validation is reported, not staged
    bad = [{"time": "2026-09-23T15:00:00", "valid": False, "issues": [{"type": "conflict"}]}]
    rec = se.recommend_reschedule([_meeting()], opts, bad, p, 45)
    assert rec["action"] == "offer_options" and rec["proposed_blocked"][0]["time"] == "2026-09-23T15:00:00"


def test_recommend_never_stages_without_exactly_one_meeting():
    p = se.history_profile(_history(), OAKLEY, NOW)
    good = se.rank_options_by_history(
        [{"rank": 1, "start": "2026-09-29T11:15:00-06:00", "end": "2026-09-29T12:00:00-06:00", "cost": 1.0, "conditional": None}], p)
    assert se.recommend_reschedule([], good, [], p, 45)["action"] == "offer_options"
    two = [_meeting(), dict(_meeting(), id="EVT2", subject="Oakley / Sam / Kit")]
    rec = se.recommend_reschedule(two, good, [], p, 45)
    assert rec["action"] == "offer_options" and rec["meetings"] == ["EVT1", "EVT2"]


def test_reschedule_brief_is_a_registered_mediated_verb():
    import mail_mediation as M
    src = Path(se.__file__).read_text()
    verbs = set(re.findall(r"""sub\.add_parser\(\s*["']([a-z-]+)["']""", src))
    assert "reschedule-brief" in verbs
    assert M.is_safe("scheduling_engine", "reschedule-brief")
    assert not M.is_gated("scheduling_engine", "reschedule-brief")
    assert not (M.SAFE["scheduling_engine"] - verbs)
    # the brief reads; the move still goes through the button
    fn = src[src.index("def cmd_reschedule_brief("):]
    assert "pending-event-update.json" in fn and "update-event" not in fn.split("how_to_stage")[0]


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL  {name}: {e!r}")
    print(f"{'OVERALL: [FAIL]' if failed else 'OVERALL: [PASS]'} ({failed} failed)")
    sys.exit(1 if failed else 0)
