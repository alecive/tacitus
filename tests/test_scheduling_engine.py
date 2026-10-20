#!/usr/bin/env python3
"""Regression tests for preprocessor/scheduling_engine.py solver behavior.

These cover two solver properties: the no-buffer penalty must not be
applied to back-to-back slots, and the exclusion stride must not drop
valid quarter-hour starts.

Hermetic — does NOT hit Outlook, Google, or the network. Drives
`_solve_one_pass` directly with synthetic `slot_info` and `hard_blocked`.
"""

import contextlib
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))


# No test may touch production state: sandbox EA_DIR before the
# preprocessor import below binds module paths.
import os
import tempfile
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-schedeng-")
# The user's own domain makes an attendee internal (preprocessor/institution.py).
os.makedirs(os.path.join(os.environ["EA_DIR"], "config"), exist_ok=True)
with open(os.path.join(os.environ["EA_DIR"], "config", "user-profile.json"), "w") as _f:
    _f.write('{"email": "owner@example.edu"}')

import scheduling_engine as se


@contextlib.contextmanager
def _own_domain(domain="@example.edu"):
    """An attendee at the user's own mail domain is internal. The domain
    comes from the profile and is cached per process, so pin it here."""
    had = hasattr(se, "_INTERNAL_DOMAINS_CACHE")
    prev = getattr(se, "_INTERNAL_DOMAINS_CACHE", None)
    se._INTERNAL_DOMAINS_CACHE = (domain,)
    try:
        yield
    finally:
        if had:
            se._INTERNAL_DOMAINS_CACHE = prev
        else:
            del se._INTERNAL_DOMAINS_CACHE


# Anchor every test at the same fictional Tuesday 09:00 local origin so
# slot-index math is predictable without depending on the system clock.
_ORIGIN = datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc)


def _slot_info(hours_span: int = 6, duration_minutes: int = 30) -> dict:
    """Build a slot_info dict for an N-hour window starting at _ORIGIN."""
    return {
        "origin": _ORIGIN,
        "total_slots": hours_span * 4,           # 15-min grid
        "duration_slots": duration_minutes // 15,
    }


def _block_window(start_min_after_origin: int, end_min_after_origin: int) -> set:
    """Return slot indices covering [start, end) at 15-min granularity."""
    return set(range(start_min_after_origin // 15, end_min_after_origin // 15))


def _picked_slots(results: list) -> list:
    return [r["slot_index"] for r in results]


def _cost_keys(results: list) -> set:
    keys = set()
    for r in results:
        keys.update(r.get("cost_breakdown", {}).keys())
    return keys


# --------------------------------------------------------------------------
# Patch 1: no_buffer penalty is gone
# --------------------------------------------------------------------------

def test_no_buffer_weight_not_in_defaults():
    """The no_buffer entry was removed from _DEFAULT_WEIGHTS — adjacent
    meetings are allowed, so there's no penalty to configure."""
    assert "no_buffer" not in se._DEFAULT_WEIGHTS, (
        f"no_buffer should not appear in _DEFAULT_WEIGHTS, got "
        f"{sorted(se._DEFAULT_WEIGHTS.keys())}"
    )


def test_back_to_back_slot_appears_in_options():
    """With organizer events 9:30-10:30 and 14:00-15:00 and a 30-min ask,
    the 9:00 start (ends exactly when the 9:30 meeting starts) and the
    10:30 start (begins exactly when it ends) must both appear in the
    proposal set. A "no_buffer" penalty on them pushes both below
    later-but-worse slots like 10:45 and 11:15.
    """
    slot_info = _slot_info(hours_span=6, duration_minutes=30)
    # 9:30-10:30 -> slots [2, 6); 14:00-15:00 -> slots [20, 24)
    hard_blocked = _block_window(30, 90) | _block_window(300, 360)

    # With no soft costs at all, the solver should still surface both
    # back-to-back-adjacent starts as candidates.
    results = se._solve_one_pass(
        slot_info=slot_info,
        hard_blocked=hard_blocked,
        soft_costs={},
        excluded=set(),
        num_options=6,
    )
    picked = _picked_slots(results)

    # Slot 0 == 09:00, slot 6 == 10:30. Both must be options.
    assert 0 in picked, (
        f"expected 09:00 (slot 0) in options, got {picked}"
    )
    assert 6 in picked, (
        f"expected 10:30 (slot 6) in options, got {picked}"
    )

    # And `no_buffer` must not surface in any breakdown.
    assert "no_buffer" not in _cost_keys(results), (
        f"no_buffer leaked into cost_breakdown: {_cost_keys(results)}"
    )


# --------------------------------------------------------------------------
# Patch 2: exclusion zone is the picked slot only, not ±15 min
# --------------------------------------------------------------------------

def test_consecutive_quarter_hour_starts_both_proposed():
    """With a wide-open morning and a 30-min ask, consecutive
    quarter-hour starts (10:30 and 10:45) must both be proposable in a
    single num_options=6 pass. If picking 10:45 excluded 10:30 and 11:00
    from later iterations, a 6-slot ask against 6+ valid starts would
    return 2 options."""
    slot_info = _slot_info(hours_span=4, duration_minutes=30)
    # Force a tie among 09:00, 09:15, 09:30, 09:45, 10:00, 10:15, 10:30
    # by giving every start the same cost. The solver picks one each
    # iteration; we just need it to surface ≥4 distinct quarter-hour
    # starts within 6 attempts.
    soft_costs = {s: {"flat": 1.0} for s in range(slot_info["total_slots"])}

    results = se._solve_one_pass(
        slot_info=slot_info,
        hard_blocked=set(),
        soft_costs=soft_costs,
        excluded=set(),
        num_options=6,
    )
    picked = sorted(_picked_slots(results))

    assert len(picked) == 6, (
        f"expected 6 options from an unconstrained 4-hour grid, got "
        f"{len(picked)}: {picked}"
    )
    # Quarter-hour granularity: with single-slot exclusion the picks
    # should be 6 distinct indices, not 6 indices spaced 30 min apart.
    distinct_gaps = sorted({picked[i + 1] - picked[i] for i in range(5)})
    assert 1 in distinct_gaps, (
        f"pre-fix, picks were spaced ≥2 slots apart everywhere; with "
        f"single-slot exclusion at least one consecutive-quarter-hour "
        f"pair (gap=1) is expected. Gaps: {distinct_gaps}"
    )


def test_exclusion_does_not_skip_adjacent_to_picked():
    """Rank slot 6 cheapest and slot 7 second-cheapest; the solver must
    pick 6 then 7. A duration-overlap exclusion that reads picking 6 as
    "covers slots 6 AND 7" skips 7."""
    slot_info = _slot_info(hours_span=4, duration_minutes=30)
    soft_costs = {s: {"flat": 5.0} for s in range(slot_info["total_slots"])}
    soft_costs[6] = {"flat": 0.0}  # cheapest
    soft_costs[7] = {"flat": 1.0}  # second-cheapest, but ADJACENT to 6

    results = se._solve_one_pass(
        slot_info=slot_info,
        hard_blocked=set(),
        soft_costs=soft_costs,
        excluded=set(),
        num_options=2,
    )
    picked = _picked_slots(results)
    assert picked[0] == 6, f"expected first pick to be slot 6, got {picked}"
    # A duration-coverage exclusion would force a gap of ≥2 and pick ≥8.
    assert picked[1] == 7, (
        f"expected second pick to be slot 7 (10:45, adjacent), got "
        f"{picked} — quarter-hour-adjacent starts are being suppressed."
    )


# --------------------------------------------------------------------------
# All-day event classification — _classify_all_day_event
# --------------------------------------------------------------------------

def test_classify_all_day_conference_returns_hard_block():
    """Conference/travel/vacation/OOF events block the day hard."""
    for subj in [
        "ICRA 2026 Conference",
        "Travel to Boston",
        "Vacation",
        "OOO — kids home from school",
        "Flight to SFO",
        "Out of office",
        "Department retreat",
    ]:
        assert se._classify_all_day_event(subj) == "hard_block", (
            f"{subj!r} should hard-block, got "
            f"{se._classify_all_day_event(subj)}"
        )


def test_classify_all_day_hold_returns_soft_penalty():
    """'Hold' / 'tentative' / 'placeholder' / 'block' = soft penalty."""
    for subj in [
        "Hold for grant review",
        "Tentative: Dean meeting",
        "Placeholder — possible workshop",
        "Block for paper writing",
    ]:
        assert se._classify_all_day_event(subj) == "soft_penalty", (
            f"{subj!r} should soft-penalize, got "
            f"{se._classify_all_day_event(subj)}"
        )


def test_classify_all_day_birthday_returns_ignore():
    """Reminders, birthdays, anniversaries are informational only."""
    for subj in [
        "Lane's birthday",
        "Anniversary — wedding",
        "Final exam deadline",
        "Reminder: pay rent",
        "Exam week",
        "Task: review draft",
    ]:
        assert se._classify_all_day_event(subj) == "ignore", (
            f"{subj!r} should be ignored, got "
            f"{se._classify_all_day_event(subj)}"
        )


def test_classify_all_day_random_returns_unknown():
    """Anything that doesn't match any pattern returns 'unknown'.

    The bot's instructions say to surface unknown all-day events to
    the user for judgment, not silently classify."""
    for subj in [
        "Xan",                              # nanny name; common all-day
        "Project XYZ kickoff",
        "Annual planning week",
        "Sprint 12",
        "",                                     # empty subject defensive case
    ]:
        assert se._classify_all_day_event(subj) == "unknown", (
            f"{subj!r} should be unknown, got "
            f"{se._classify_all_day_event(subj)}"
        )


# --------------------------------------------------------------------------
# Solver weights table integrity
# --------------------------------------------------------------------------

def test_default_weights_has_expected_shape():
    """The _DEFAULT_WEIGHTS dict is the rate card the solver scores
    against. Pin the exact set of keys so adding a new weight is a
    conscious decision, and removing one (like the historic
    `no_buffer`) is intentional."""
    expected = {
        "lunch_overlap",
        "preferred_slot_miss",
        "preferred_window_miss",
        "clustering",
        "later_in_week",
        "after_preferred",
        "earlier_in_day",
        "all_day_hold_tentative",
        "all_day_unknown",
        # Token-event soft penalties (EA-Managed calendar): p1 strong, p2 mild.
        "p1_soft",
        "p2_pref",
    }
    actual = set(se._DEFAULT_WEIGHTS.keys())
    assert actual == expected, (
        f"_DEFAULT_WEIGHTS keys changed: missing {expected - actual}, "
        f"unexpected {actual - expected}"
    )


def test_lunch_overlap_is_largest_default():
    """Lunch is the strongest soft signal — even VIPs prefer not to
    overlap unless the user explicitly overrides. Pinning this keeps
    a future tweak from quietly flipping the relative ordering."""
    w = se._DEFAULT_WEIGHTS
    for other in ("preferred_slot_miss", "preferred_window_miss",
                  "clustering", "later_in_week", "after_preferred",
                  "all_day_hold_tentative", "all_day_unknown"):
        assert w["lunch_overlap"] >= w[other], (
            f"lunch_overlap ({w['lunch_overlap']}) should be >= "
            f"{other} ({w[other]}) by default"
        )


# --------------------------------------------------------------------------
# Slot-index ↔ datetime roundtrip
# --------------------------------------------------------------------------

def test_time_slot_roundtrip_at_15_min_grid():
    """_time_to_slot_index and _slot_index_to_time must round-trip at
    every 15-minute increment within the day."""
    origin = datetime(2026, 5, 26, 0, 0, tzinfo=timezone.utc)
    for minutes in range(0, 24 * 60, 15):
        t = origin + timedelta(minutes=minutes)
        idx = se._time_to_slot_index(t, origin)
        back = se._slot_index_to_time(idx, origin)
        assert back == t, (
            f"roundtrip failed at +{minutes}min: {t} → {idx} → {back}"
        )


def test_slot_index_zero_is_origin():
    """Slot 0 = the origin exactly. Off-by-one bugs in slot math have
    been a source of past failures (the Tam case was rooted here)."""
    origin = datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc)
    assert se._slot_index_to_time(0, origin) == origin
    assert se._time_to_slot_index(origin, origin) == 0


# --------------------------------------------------------------------------
# _solve_one_pass — additional contract checks
# --------------------------------------------------------------------------

def test_solve_one_pass_respects_num_options_cap():
    """Asking for N options when more candidates exist returns
    exactly N (not all candidates, not fewer than N)."""
    slot_info = _slot_info(hours_span=8, duration_minutes=30)
    # 32 slots, all feasible, all equal cost → up to 31 starts available
    soft_costs = {s: {"flat": 1.0} for s in range(slot_info["total_slots"])}
    for ask in (1, 3, 6, 10):
        results = se._solve_one_pass(
            slot_info=slot_info, hard_blocked=set(),
            soft_costs=soft_costs, excluded=set(), num_options=ask,
        )
        assert len(results) == ask, (
            f"num_options={ask} should produce {ask} results, got "
            f"{len(results)}"
        )


def test_solve_one_pass_returns_no_options_when_all_blocked():
    """If every slot is hard-blocked, the solver returns an empty
    list (not a crash, not bogus options)."""
    slot_info = _slot_info(hours_span=2, duration_minutes=30)
    hard_blocked = set(range(slot_info["total_slots"]))
    results = se._solve_one_pass(
        slot_info=slot_info, hard_blocked=hard_blocked,
        soft_costs={}, excluded=set(), num_options=5,
    )
    assert results == [], (
        f"expected [] when everything is hard-blocked, got {results}"
    )


def test_solve_one_pass_excluded_param_skips_those_starts():
    """Caller can pre-exclude specific start indices (e.g. previously
    proposed slots in a relaxation pass). They must not appear."""
    slot_info = _slot_info(hours_span=4, duration_minutes=30)
    soft_costs = {s: {"flat": 1.0} for s in range(slot_info["total_slots"])}
    pre_excluded = {0, 1, 2, 3, 4, 5}
    results = se._solve_one_pass(
        slot_info=slot_info, hard_blocked=set(),
        soft_costs=soft_costs, excluded=pre_excluded, num_options=6,
    )
    picked = _picked_slots(results)
    for s in pre_excluded:
        assert s not in picked, (
            f"excluded slot {s} appeared in picks {picked}"
        )


def test_solve_one_pass_lower_cost_picked_first():
    """The solver MUST pick the lowest-cost candidate first.
    Foundational property; without it, ranking is meaningless."""
    slot_info = _slot_info(hours_span=4, duration_minutes=30)
    # Slot 3 strictly cheapest, slot 7 second-cheapest, everything else equal-high.
    soft_costs = {s: {"flat": 10.0} for s in range(slot_info["total_slots"])}
    soft_costs[3] = {"flat": 0.0}
    soft_costs[7] = {"flat": 1.0}
    results = se._solve_one_pass(
        slot_info=slot_info, hard_blocked=set(),
        soft_costs=soft_costs, excluded=set(), num_options=2,
    )
    picked = _picked_slots(results)
    assert picked[0] == 3, f"cheapest should be picked first, got {picked}"
    assert picked[1] == 7, f"second-cheapest should be picked next, got {picked}"


def test_solve_one_pass_cost_breakdown_present_in_each_option():
    """Output schema: every option must have a `cost_breakdown` dict.
    The Slack-visible trace footer + the dump file both rely on this
    field being present."""
    slot_info = _slot_info(hours_span=2, duration_minutes=30)
    soft_costs = {0: {"lunch_overlap": 3.0, "clustering": 0.5}}
    results = se._solve_one_pass(
        slot_info=slot_info, hard_blocked=set(),
        soft_costs=soft_costs, excluded=set(), num_options=1,
    )
    assert results, "expected ≥1 result"
    assert "cost_breakdown" in results[0]
    assert isinstance(results[0]["cost_breakdown"], dict)


# --------------------------------------------------------------------------
# Standalone runner (mirrors tests/test_graph_client.py footer style).
# --------------------------------------------------------------------------

def test_is_note_event_matches_anywhere_case_insensitively():
    """[note] is non-positional so an existing title can be tagged in place."""
    for subj in [
        "[note] house cleaner",
        "house cleaner [note]",
        "Xan [NOTE]",
        "[Note] Bri travelling",
    ]:
        assert se._is_note_event(subj), f"{subj!r} should be a note event"


def test_is_note_event_rejects_untagged_and_empty():
    for subj in ["Group Meeting", "note taking app review", "", None]:
        assert not se._is_note_event(subj), f"{subj!r} should not be a note event"


def test_note_event_does_not_block_a_slot():
    """A timed [note] event is dropped from busy intervals entirely.

    Both events sit on a calendar with blocks_availability=True, so the
    only reason the note is absent is the [note] marker itself.
    """
    events = [
        {"subject": "[note] house cleaner",
         "start": {"dateTime": "2026-11-10T08:30:00-07:00"},
         "end": {"dateTime": "2026-11-10T12:00:00-07:00"},
         "source_calendar": "jo.ambrose4@example.com"},
        {"subject": "Group Meeting",
         "start": {"dateTime": "2026-11-10T13:00:00-07:00"},
         "end": {"dateTime": "2026-11-10T14:00:00-07:00"},
         "source_calendar": "jo.ambrose4@example.com"},
    ]
    busy = se._build_busy_intervals(events)
    subjects = [e.get("subject") for _, _, e in busy]
    assert "[note] house cleaner" not in subjects
    assert "Group Meeting" in subjects


def test_all_day_note_event_not_surfaced_for_judgment():
    """An all-day [note] needs no block/ignore decision from Claude."""
    events = [
        {"subject": "[note] Bri travelling", "isAllDay": True,
         "start": {"date": "2026-11-10"}, "end": {"date": "2026-11-11"},
         "source_calendar": "jo.ambrose4@example.com"},
        {"subject": "ICRA 2026 Conference", "isAllDay": True,
         "start": {"date": "2026-11-10"}, "end": {"date": "2026-11-11"},
         "source_calendar": "jo.ambrose4@example.com"},
    ]
    from datetime import date as _date
    found = se._get_all_day_events_on_date(_date(2026, 11, 10), events)
    subjects = [e["subject"] for e in found]
    assert "[note] Bri travelling" not in subjects
    assert "ICRA 2026 Conference" in subjects


# --- Movable-block classification -----------------------------------------
# Movability cannot be `len(attendees) <= 1`: a zero-attendee event is a
# personal block, not a 1:1. A lecture has no attendees, because you do
# not invite eighty students, and the relaxation pass must not offer that
# slot away as reschedulable.

def test_external_one_on_one_is_not_offerable():
    """A 1:1 with an external party has no p0 mirror, because one-offs
    are not mirrored, so attendee count alone would make it movable. An
    external counterpart makes it immovable."""
    ev = {"subject": "Kit / Yuki - AI + humanities",
          "attendees": ["bri.kowalczyk10@example.org"],
          "source_calendar": "outlook"}
    assert se._is_movable_block(ev) is False


def test_internal_one_on_one_stays_offerable():
    ev = {"subject": "Taylor / Kit",
          "attendees": ["taylor@example.edu"],
          "source_calendar": "outlook"}
    with _own_domain():
        assert se._is_movable_block(ev) is True


def test_lecture_with_no_attendees_is_not_movable():
    ev = {"subject": "Kit lecture", "attendees": [],
          "source_calendar": "jo.ambrose4@example.com"}
    assert se._is_movable_block(ev) is False, (
        "a zero-attendee teaching block must not be offered away")


def test_real_one_on_one_stays_movable():
    ev = {"subject": "Gus / Kit",
          "attendees": [{"emailAddress": {"address": "gus@example.edu"}}],
          "source_calendar": "Outlook"}
    with _own_domain():
        assert se._is_movable_block(ev) is True


def test_group_meeting_is_not_movable():
    ev = {"subject": "Group Meeting",
          "attendees": [{"emailAddress": {"address": f"s{i}@example.edu"}}
                        for i in range(9)],
          "source_calendar": "Outlook"}
    assert se._is_movable_block(ev) is False


def test_ea_managed_hold_stays_movable_despite_no_attendees():
    """Holds are attendee-less by design and meant to be reclaimable."""
    ev = {"subject": "HOLD: faculty retreat", "attendees": [],
          "source_calendar": "ea-managed"}
    assert se._is_movable_block(ev) is True


def test_missing_and_null_attendees_are_treated_as_none():
    for ev in ({"subject": "Travel", "source_calendar": "Outlook"},
               {"subject": "Travel", "attendees": None,
                "source_calendar": "Outlook"}):
        assert se._is_movable_block(ev) is False, (
            "an absent or null attendee list is a personal block, not a 1:1")


def test_a_typed_naive_time_means_local_not_utc():
    """A typed naive time is local. Read as UTC, a typed "09:00" lands at
    about 3am in Denver, where validate-draft would call a booked slot
    both valid and outside working hours."""
    parsed = se._parse_user_dt("2026-08-10T09:00:00")
    local = parsed.astimezone(se.USER_TZ)
    assert (local.hour, local.minute) == (9, 0), (
        f"typed 09:00 became {local:%H:%M} local")


def test_an_explicit_offset_is_honoured_as_written():
    """propose-meeting emits offset-aware strings; pasting one back must
    not shift it."""
    local = se._parse_user_dt("2026-08-10T13:30:00-06:00").astimezone(se.USER_TZ)
    assert (local.hour, local.minute) == (13, 30)
    z = se._parse_user_dt("2026-08-10T15:00:00Z").astimezone(timezone.utc)
    assert (z.hour, z.minute) == (15, 0)


def test_api_times_still_read_as_utc():
    """Graph and Google hand back naive UTC, so _parse_dt must keep its
    own convention. Changing it globally would shift every event."""
    assert se._parse_dt("2026-08-10T15:00:00").hour == 15
    assert se._parse_dt("2026-08-10T15:00:00").tzinfo == timezone.utc


def test_the_two_parsers_disagree_by_exactly_the_utc_offset():
    typed = se._parse_user_dt("2026-08-10T09:00:00")
    as_api = se._parse_dt("2026-08-10T09:00:00")
    delta_h = (typed - as_api).total_seconds() / 3600
    assert delta_h == 6, (
        f"Denver is UTC-6 in August; got {delta_h}h. If this fails the "
        "parsers have converged and validate-draft is misreading again.")


# --- All-day event shapes --------------------------------------------------
# No producer emits `event["start"]["date"]`, so an all-day path keyed on
# it reaches neither the availability math nor the output. These fixtures
# are the shapes production actually hands the engine; do not "simplify"
# them back to the date-only form.

def _graph_all_day(subject, first_day, last_day, show_as="free", **extra):
    """Graph's real all-day shape: `isAllDay` plus dateTimeTimeZone objects,
    where the END is midnight of the day AFTER the last covered day."""
    exclusive_end = date.fromisoformat(last_day) + timedelta(days=1)
    e = {
        "subject": subject,
        "start": {"dateTime": f"{first_day}T00:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"{exclusive_end.isoformat()}T00:00:00.0000000",
                "timeZone": "UTC"},
        "isAllDay": True,
        "showAs": show_as,
        "attendees": [],
        "source_calendar": "outlook",
    }
    e.update(extra)
    return e


def _google_all_day(subject, first_day, last_day,
                    source_calendar="jo.ambrose4@example.com", **extra):
    """google_calendar_client.get_events' normalized all-day shape:
    `is_all_day` (snake_case) with the EXCLUSIVE end date carrying a
    23:59:59 tail bolted on by the client."""
    exclusive_end = date.fromisoformat(last_day) + timedelta(days=1)
    e = {
        "subject": subject,
        "start": {"dateTime": f"{first_day}T00:00:00"},
        "end": {"dateTime": f"{exclusive_end.isoformat()}T23:59:59"},
        "is_all_day": True,
        "source_calendar": source_calendar,
        "source_type": "google",
    }
    e.update(extra)
    return e


def test_all_day_detected_in_both_producer_shapes():
    """camelCase (Graph), snake_case (Google) and the raw date-only API
    item must all read as all-day."""
    assert se._is_all_day_event(_graph_all_day("ICRA", "2026-10-05", "2026-10-09"))
    assert se._is_all_day_event(_google_all_day("ICRA", "2026-10-05", "2026-10-09"))
    assert se._is_all_day_event({"start": {"date": "2026-10-05"},
                                 "end": {"date": "2026-10-06"}})
    assert not se._is_all_day_event(
        {"start": {"dateTime": "2026-10-05T09:00:00"},
         "end": {"dateTime": "2026-10-05T10:00:00"}})


def test_all_day_span_honours_the_exclusive_end_date():
    """Both producers report the end EXCLUSIVELY. A single-day event must
    cover ONE day, not two -- otherwise every all-day event bleeds into the
    following morning."""
    for ev in (_graph_all_day("Vacation", "2026-10-05", "2026-10-05"),
               _google_all_day("Vacation", "2026-10-05", "2026-10-05"),
               {"start": {"date": "2026-10-05"}, "end": {"date": "2026-10-06"},
                "isAllDay": True}):
        assert se._all_day_span(ev) == (date(2026, 10, 5), date(2026, 10, 5)), (
            f"single-day all-day event span wrong for {ev}")


def test_all_day_span_covers_every_day_of_a_multi_day_event():
    """Mon-Fri conference = five days, in both shapes."""
    for ev in (_graph_all_day("ICRA 2026 Conference", "2026-10-05", "2026-10-09"),
               _google_all_day("ICRA 2026 Conference", "2026-10-05", "2026-10-09")):
        assert se._all_day_span(ev) == (date(2026, 10, 5), date(2026, 10, 9)), (
            f"multi-day span wrong for {ev}")


def test_all_day_span_reads_graph_utc_shifted_boundaries():
    """Graph converts to UTC when no Prefer header is sent, so a Denver
    all-day event can come back as 06:00Z on both ends. Reading the DATE
    PART keeps that on the right days; converting to local time would not
    (it would move Google's naive midnights back a day)."""
    ev = {"subject": "Vacation", "isAllDay": True,
          "start": {"dateTime": "2026-10-05T06:00:00.0000000", "timeZone": "UTC"},
          "end": {"dateTime": "2026-10-10T06:00:00.0000000", "timeZone": "UTC"}}
    assert se._all_day_span(ev) == (date(2026, 10, 5), date(2026, 10, 9))


def test_all_day_span_tolerates_an_inclusive_end():
    """A hand-built event whose end is not past the start is one day, not a
    negative span."""
    ev = {"subject": "Retreat", "isAllDay": True,
          "start": {"dateTime": "2026-10-05T00:00:00"},
          "end": {"dateTime": "2026-10-05T23:59:59"}}
    assert se._all_day_span(ev) == (date(2026, 10, 5), date(2026, 10, 5))


def test_google_all_day_event_is_not_a_busy_interval():
    """An all-day event is classified, never counted as busy time.

    The camelCase-only `isAllDay` check missed Google's `is_all_day`
    entirely; what kept those events out of the busy set was the unrelated
    ">= 24 hours" filter catching the client's 23:59:59 tail. Detection now
    does it on purpose, so tightening that filter cannot resurrect a
    day-long busy block."""
    events = [_google_all_day("Lane's birthday", "2026-11-10", "2026-11-10"),
              {"subject": "Group Meeting",
               "start": {"dateTime": "2026-11-10T13:00:00-07:00"},
               "end": {"dateTime": "2026-11-10T14:00:00-07:00"},
               "source_calendar": "jo.ambrose4@example.com"}]
    subjects = [e.get("subject") for _, _, e in se._build_busy_intervals(events)]
    assert "Lane's birthday" not in subjects, (
        "an all-day event must be classified, not treated as busy time")
    assert "Group Meeting" in subjects


def test_out_of_office_all_day_hard_blocks_whatever_the_title_says():
    """skills/scheduling-rules.md: showAs oof always blocks."""
    assert se._classify_all_day_event("Xan", "oof") == "hard_block"
    assert se._classify_all_day_event("Xan") == "unknown"


def test_show_as_free_does_not_downgrade_a_conference():
    """Outlook marks all-day events `free` by DEFAULT, so showAs may only
    escalate. A Free conference week still blocks."""
    assert se._classify_all_day_event("ICRA 2026 Conference", "free") == "hard_block"


def test_all_day_events_surface_on_every_day_they_cover():
    """`_get_all_day_events_on_date` is what validate-draft judges from. A
    Mon-Fri conference must answer for Wednesday too."""
    events = [_graph_all_day("ICRA 2026 Conference", "2026-10-05", "2026-10-09")]
    for day in (5, 7, 9):
        found = se._get_all_day_events_on_date(date(2026, 10, day), events)
        assert [f["subject"] for f in found] == ["ICRA 2026 Conference"], (
            f"conference missing on 2026-10-{day:02d}: {found}")
        assert found[0]["classification"] == "hard_block"
        assert found[0]["effect"] == "blocked"
        assert found[0]["start_date"] == "2026-10-05"
        assert found[0]["end_date"] == "2026-10-09", (
            "end_date must be the INCLUSIVE last day")
    assert se._get_all_day_events_on_date(date(2026, 10, 10), events) == [], (
        "the exclusive end date is NOT covered by the event")


def test_ea_managed_token_events_are_not_offered_for_all_day_judgment():
    """A tokened EA-Managed event is a scheduling CONSTRAINT, already
    applied from the token path. Judging it again would double-count it as
    an unknown all-day event."""
    events = [_google_all_day("nm No-meeting Friday", "2026-11-13", "2026-11-13",
                              source_calendar="ea-managed")]
    assert se._get_all_day_events_on_date(date(2026, 11, 13), events) == []


# --- Multi-day all-day TOKEN events ----------------------------------------

def _token_constraints_from(events, earliest, latest):
    """Run _load_token_constraints against a synthetic EA-Managed read."""
    original = se._fetch_ea_managed_events
    se._fetch_ea_managed_events = lambda a, b: events
    try:
        return se._load_token_constraints(earliest, latest)
    finally:
        se._fetch_ea_managed_events = original


def test_multi_day_all_day_token_covers_every_day():
    """A token event dragged Mon-Fri on the EA-Managed calendar covers
    all five days. One instance on the Monday leaves the other four open
    and reports a one-day block in `calendar_preferences.tokens`."""
    events = [_google_all_day("nm IROS travel", "2026-10-05", "2026-10-09",
                              source_calendar="ea-managed"),
              _google_all_day("wh Working hours", "2026-10-05", "2026-10-09",
                              source_calendar="ea-managed")]
    tc = _token_constraints_from(events, date(2026, 10, 5), date(2026, 10, 9))
    nm_dates = sorted(i["date"] for i in tc["instances"] if i["token"] == "nm")
    assert nm_dates == [date(2026, 10, d) for d in range(5, 10)], (
        f"nm token covered {nm_dates}, expected all five days")
    for inst in tc["instances"]:
        assert (inst["start_min"], inst["end_min"]) == (0, 1440)
        assert inst["all_day"] is True
    assert tc["has_wh"] is True

    summary = se.summarize_token_constraints(tc)
    assert summary["nm"][0]["dates"] == [f"2026-10-0{d}" for d in range(5, 10)], (
        f"token summary must name every blocked date: {summary['nm']}")
    assert summary["nm"][0]["window"] == "all day"


def test_multi_day_token_stops_on_the_exclusive_end_date():
    """The day after the last covered day belongs to nobody."""
    events = [_google_all_day("nm IROS travel", "2026-10-05", "2026-10-07",
                              source_calendar="ea-managed")]
    tc = _token_constraints_from(events, date(2026, 10, 5), date(2026, 10, 9))
    nm_dates = sorted(i["date"] for i in tc["instances"] if i["token"] == "nm")
    assert nm_dates == [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)], (
        f"expected Oct 5-7 only, got {nm_dates}")


def test_multi_day_token_is_clamped_to_the_requested_horizon():
    """A token event overlapping the window start still applies, but the
    instance list stays inside the horizon it was asked for."""
    events = [_graph_all_day("nm Research leave", "2026-09-28", "2026-10-20",
                             source_calendar="ea-managed")]
    tc = _token_constraints_from(events, date(2026, 10, 5), date(2026, 10, 7))
    nm_dates = sorted(i["date"] for i in tc["instances"] if i["token"] == "nm")
    assert nm_dates == [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)], (
        f"horizon clamp failed: {nm_dates}")


def test_timed_token_events_still_land_on_one_day():
    """A timed token event stays on its own day; only an all-day one
    spans days."""
    events = [{"subject": "nm Lunch", "is_all_day": False,
               "start": {"dateTime": "2026-10-05T18:00:00+00:00"},
               "end": {"dateTime": "2026-10-05T19:00:00+00:00"},
               "source_calendar": "ea-managed"}]
    tc = _token_constraints_from(events, date(2026, 10, 5), date(2026, 10, 9))
    assert len(tc["instances"]) == 1
    inst = tc["instances"][0]
    assert inst["date"] == date(2026, 10, 5)
    assert (inst["start_min"], inst["end_min"]) == (12 * 60, 13 * 60)



# --- the [auto] mirror decides what a real event costs ------------------------

def _mirror(level_title, key, start, end):
    return {"subject": level_title, "mirror_of": key,
            "start": {"dateTime": start}, "end": {"dateTime": end},
            "source_calendar": "ea-managed", "source_type": "google"}


def _outlook(subject, eid, start_utc, end_utc, series="", attendees=3):
    return {"subject": subject, "id": eid, "seriesMasterId": series or None,
            "start": {"dateTime": start_utc, "timeZone": "UTC"},
            "end": {"dateTime": end_utc, "timeZone": "UTC"},
            "attendees": [{"emailAddress": {"address": f"a{i}@example.edu"}}
                          for i in range(attendees)]}


def test_mirror_level_of_titles():
    assert se._mirror_level_of("p0 [auto] Vendor visit") == "p0"
    assert se._mirror_level_of("p1 [auto] Kit / Bo 1:1") == "p1"
    assert se._mirror_level_of("[note] [auto] RF Team Weekly") == "note"
    assert se._mirror_level_of("p0 childcare dropoff") == "", "a hand token"


def test_a_mirror_decides_whether_its_event_blocks():
    events = [
        _outlook("RF Team Weekly", "occ-rf", "2026-10-15T19:00:00", "2026-10-15T20:00:00",
                 series="ser-rf"),
        _outlook("AI working group", "one-wg", "2026-10-16T16:00:00", "2026-10-16T17:00:00"),
        _outlook("Faculty meeting", "one-fm", "2026-10-16T18:00:00", "2026-10-16T19:00:00"),
        _outlook("New meeting", "one-new", "2026-10-16T20:00:00", "2026-10-16T21:00:00"),
        _mirror("[note] [auto] RF Team Weekly", "outlook/ser-rf",
                "2026-10-15T13:00:00-06:00", "2026-10-15T14:00:00-06:00"),
        _mirror("p2 [auto] AI working group", "outlook/one-wg",
                "2026-10-16T10:00:00-06:00", "2026-10-16T11:00:00-06:00"),
        _mirror("p1 [auto] Faculty meeting", "outlook/one-fm",
                "2026-10-16T12:00:00-06:00", "2026-10-16T13:00:00-06:00"),
    ]
    busy = {e["subject"]: e for _, _, e in se._build_busy_intervals(events)}
    assert "RF Team Weekly" not in busy, "a note is free time"
    assert "AI working group" not in busy, "p2 is a penalty, not a block"
    assert busy["Faculty meeting"]["mirror_level"] == "p1"
    assert se._is_movable_block(busy["Faculty meeting"]), \
        "the user made this group meeting movable"
    assert "mirror_level" not in busy["New meeting"], "no mirror yet"
    assert not se._is_movable_block(busy["New meeting"]), "heuristic: a group"
    kept = {e["subject"]: e for _, _, e in se._build_busy_intervals(events, keep_soft=True)}
    assert kept["RF Team Weekly"]["mirror_level"] == "note"
    assert kept["AI working group"]["mirror_level"] == "p2"


def test_p0_mirror_overrides_the_1on1_heuristic():
    one = _outlook("Kit / Bo 1:1", "one-bo", "2026-10-12T20:15:00",
                   "2026-10-12T20:45:00", attendees=1)
    with _own_domain():
        assert se._is_movable_block(one), "an internal 1:1 with no mirror can move"
        pinned = _mirror("p0 [auto] Kit / Bo 1:1", "outlook/one-bo",
                         "2026-10-12T14:15:00-06:00", "2026-10-12T14:45:00-06:00")
        [(_, _, e)] = se._build_busy_intervals([one, pinned])
        assert e["mirror_level"] == "p0" and not se._is_movable_block(e)


def test_google_events_find_their_mirror():
    g = {"subject": "Hiring Committee", "id": "inst_1", "recurring_event_id": "ser-hire",
         "calendar_id": "jo.ambrose4@example.com", "source_calendar": "jo.ambrose4@example.com",
         "source_type": "google",
         "start": {"dateTime": "2026-10-20T10:00:00-06:00"},
         "end": {"dateTime": "2026-10-20T11:00:00-06:00"}}
    copy = dict(g, subject="Computer Science Department Meeting", id="feed_1",
                recurring_event_id="feed-ser",
                start={"dateTime": "2026-10-19T12:20:00-06:00"},
                end={"dateTime": "2026-10-19T13:10:00-06:00"})
    hold = dict(copy, subject="hold", id="h1", source_calendar="ea-managed",
                recurring_event_id="")
    events = [g, copy, hold,
              _mirror("[note] [auto] Hiring Committee", "jo.ambrose4@example.com/ser-hire",
                      "2026-10-20T10:00:00-06:00", "2026-10-20T11:00:00-06:00"),
              # the Exchange series the feed copy was folded into
              _mirror("[note] [auto] CS Department Meeting", "outlook/ser-dept",
                      "2026-10-19T12:20:00-06:00", "2026-10-19T13:10:00-06:00")]
    busy = [e["subject"] for _, _, e in se._build_busy_intervals(events)]
    assert busy == ["hold"], busy


def test_check_attendees_lists_soft_events_without_calling_them_busy():
    e = _outlook("AI working group", "one-wg", "2026-10-16T16:00:00", "2026-10-16T17:00:00")
    m = _mirror("p2 [auto] AI working group", "outlook/one-wg",
                "2026-10-16T10:00:00-06:00", "2026-10-16T11:00:00-06:00")
    [(s, t, ev)] = se._build_busy_intervals([e, m], keep_soft=True)
    assert se._event_summary(ev, s, t)["level"] == "p2"


# --------------------------------------------------------------------------
# Distinct windows for a short list; enumeration keeps every start
# --------------------------------------------------------------------------
def test_distinct_pass_returns_separate_windows():
    """Five equal-cost starts on one morning: with `distinct` a pick
    excludes the starts its meeting would overlap, so a 30-minute
    meeting's options are 30 minutes apart; without it they are the
    quarter-hour starts of one window."""
    slot_info = _slot_info(hours_span=4, duration_minutes=30)
    soft_costs = {s: {"flat": 1.0} for s in range(slot_info["total_slots"])}
    picked = _picked_slots(se._solve_one_pass(
        slot_info=slot_info, hard_blocked=set(), soft_costs=soft_costs,
        excluded=set(), num_options=5, distinct=True))
    assert len(picked) == 5
    assert all(b - a >= 2 for a, b in zip(sorted(picked), sorted(picked)[1:])), picked
    dense = _picked_slots(se._solve_one_pass(
        slot_info=slot_info, hard_blocked=set(), soft_costs=soft_costs,
        excluded=set(), num_options=5))
    assert any(b - a == 1 for a, b in zip(sorted(dense), sorted(dense)[1:])), dense
    assert se.ENUMERATE_FROM >= 100


def test_timezone_availability_is_working_hours_on_the_users_clock():
    """A London attendee in October (UTC+1 against MDT) is free 02:00 to
    10:00 Mountain; a Tokyo attendee's day splits across the user's
    previous evening and the small hours."""
    if str(se.USER_TZ) != "America/Denver":
        return
    w = se._timezone_availability("Europe/London", date(2026, 10, 13), date(2026, 10, 14))
    assert w == [{"day": "2026-10-13", "start": "02:00", "end": "10:00"},
                 {"day": "2026-10-14", "start": "02:00", "end": "10:00"}]
    w = se._timezone_availability("Asia/Tokyo", date(2026, 10, 13), date(2026, 10, 13))
    assert w == [{"day": "2026-10-13", "start": "00:00", "end": "02:00"},
                 {"day": "2026-10-13", "start": "18:00", "end": "24:00"}]
    w = se._timezone_availability("Europe/London", date(2026, 10, 13), date(2026, 10, 13), hours=["10:00", "12:00"])
    assert w == [{"day": "2026-10-13", "start": "03:00", "end": "05:00"}]
    try:
        se._timezone_availability("London", date(2026, 10, 13), date(2026, 10, 13))
        assert False, "unknown zone accepted"
    except ValueError as e:
        assert "Europe/London" in str(e)

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


# --- [note] marker: informational events never block ------------------------

