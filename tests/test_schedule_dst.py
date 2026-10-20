"""Other people's free/busy across a daylight-saving change. getSchedule's
availability view is real time, one character per 15 minutes; the solver's
grid is wall-clock time. A busy block on the day after the change must
land at its own clock time, not an hour off. No network."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import scheduling_engine as se  # noqa: E402

DEN = ZoneInfo("America/Denver")


def _view(start_local, end_local, busy_local, tentative_local=()):
    """The view Graph returns: one character per real 15 minutes from the
    start; '2' inside a busy (start, end) local range, '1' inside a
    tentative one."""
    out, t = [], start_local.astimezone(timezone.utc)
    end = end_local.astimezone(timezone.utc)
    while t < end:
        local = t.astimezone(DEN)
        ch = "0"
        if any(a <= local < b for a, b in tentative_local):
            ch = "1"
        if any(a <= local < b for a, b in busy_local):
            ch = "2"
        out.append(ch)
        t += timedelta(minutes=15)
    return "".join(out)


def _slots(origin, day, end_dt, busy, tentative=()):
    view = _view(origin, end_dt, busy, tentative)
    with patch.object(se.graph, "get_schedule_detail",
                      return_value={"cy.zabriskie3@example.edu": {"view": view, "error": ""}}):
        return se._get_internal_busy_slots(["cy.zabriskie3@example.edu"], origin, end_dt, 3 * 96)["cy.zabriskie3@example.edu"], view


def test_fall_back_keeps_a_busy_block_at_its_clock_time():
    origin = datetime(2026, 10, 31, 0, 0, tzinfo=DEN)
    end_dt = datetime(2026, 11, 2, 23, 59, 59, tzinfo=DEN)
    busy = [(datetime(2026, 11, 2, 9, 30, tzinfo=DEN), datetime(2026, 11, 2, 10, 0, tzinfo=DEN))]
    tent = [(datetime(2026, 11, 2, 9, 0, tzinfo=DEN), datetime(2026, 11, 2, 9, 30, tzinfo=DEN))]
    got, view = _slots(origin, 2, end_dt, busy, tent)
    assert len(view) == 3 * 96 + 4                       # the repeated hour on Nov 1
    assert got["busy"] == {2 * 96 + 38, 2 * 96 + 39}      # 9:30 and 9:45 on Nov 2
    assert got["tentative"] == {2 * 96 + 36, 2 * 96 + 37}
    assert "unchecked" not in got


def test_spring_forward_keeps_a_busy_block_at_its_clock_time():
    origin = datetime(2027, 3, 13, 0, 0, tzinfo=DEN)
    end_dt = datetime(2027, 3, 15, 23, 59, 59, tzinfo=DEN)
    busy = [(datetime(2027, 3, 15, 9, 30, tzinfo=DEN), datetime(2027, 3, 15, 10, 0, tzinfo=DEN))]
    got, view = _slots(origin, 2, end_dt, busy)
    assert len(view) == 3 * 96 - 4                       # the skipped hour on Mar 14
    assert got["busy"] == {2 * 96 + 38, 2 * 96 + 39}


def test_an_unread_calendar_is_marked_unchecked():
    origin = datetime(2026, 10, 6, 0, 0, tzinfo=DEN)
    with patch.object(se.graph, "get_schedule_detail",
                      return_value={"hollis.isenberg21@example.org": {"view": "", "error": "ErrorMailboxNotFound"}}):
        got = se._get_internal_busy_slots(["hollis.isenberg21@example.org"], origin, origin + timedelta(days=1), 96)
    assert got["hollis.isenberg21@example.org"] == {"busy": set(), "tentative": set(),
                                        "unchecked": "ErrorMailboxNotFound"}


def test_check_attendees_runs_are_at_clock_time_across_the_change():
    start = datetime(2026, 10, 31, 9, 0, tzinfo=DEN)
    end = datetime(2026, 11, 2, 10, 0, tzinfo=DEN)
    busy = [(datetime(2026, 11, 2, 9, 30, tzinfo=DEN), end)]
    runs = se._view_intervals(_view(start, end, busy), start, 15)
    assert runs == [{"state": "busy", "start": "2026-11-02T09:30:00-07:00",
                     "end": "2026-11-02T10:00:00-07:00"}]
