"""Synthetic-event factories for hermetic scheduling-engine tests.

Used by tests/test_scheduling_cli.py to drive `cmd_propose_meeting` /
`cmd_validate_draft` end-to-end without hitting Outlook or Google.
Patch `scheduling_engine.graph.get_calendar_view` and
`scheduling_engine.gcal.get_all_events` with `lambda *a, **k: <list>`
where the list is built by these helpers.
"""

from datetime import datetime, timedelta, timezone


def _mt(year, month, day, hour, minute=0):
    """Helper: build a tz-aware Mountain-Time datetime (UTC-6, no DST math)."""
    return datetime(year, month, day, hour, minute,
                    tzinfo=timezone(timedelta(hours=-6)))


def outlook_event(subject, start, end, is_all_day=False, show_as="busy",
                  attendees=None, organizer=None, source_account="chris@org"):
    """Build one event in the shape `graph.get_calendar_view` returns.

    Args:
        subject: title string
        start, end: tz-aware datetimes (use `_mt`)
        is_all_day: bool
        show_as: "busy" | "tentative" | "oof" | "free" | "workingElsewhere"
        attendees: list of email strings (optional)
        organizer: email string (optional)
    """
    e = {
        "subject": subject,
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Denver"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Denver"},
        "isAllDay": is_all_day,
        "showAs": show_as,
        "attendees": [{"emailAddress": {"address": a}} for a in (attendees or [])],
        "organizer": {"emailAddress": {"address": organizer or source_account}},
    }
    return e


def google_event(subject, start, end, is_all_day=False,
                 source_calendar="jo.ambrose4@example.com"):
    """Build one event in the shape `gcal.get_all_events` returns.

    NOTE: `_build_busy_intervals` in scheduling_engine.py uses
    `source_calendar` to look up `blocks_availability` from
    scheduling-preferences.json. To exercise the
    `blocks_availability: false` path, pass the family/non-blocking
    account's email here."""
    return {
        "subject": subject,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "isAllDay": is_all_day,
        "source_calendar": source_calendar,
        "source_type": "google",
    }


def synthetic_outlook_events(events_data):
    """Build a list of Outlook events from a list of (subject, start, end[, kwargs])
    tuples. Convenience wrapper for tests that need to mock
    `_fetch_all_events`."""
    out = []
    for spec in events_data:
        if len(spec) == 3:
            subj, start, end = spec
            out.append(outlook_event(subj, start, end))
        elif len(spec) == 4:
            subj, start, end, kwargs = spec
            out.append(outlook_event(subj, start, end, **kwargs))
        else:
            raise ValueError(f"unexpected event spec shape: {spec}")
    return out


def synthetic_google_events(events_data):
    """Build a list of Google events from (subject, start, end[, kwargs]) tuples."""
    out = []
    for spec in events_data:
        if len(spec) == 3:
            subj, start, end = spec
            out.append(google_event(subj, start, end))
        elif len(spec) == 4:
            subj, start, end, kwargs = spec
            out.append(google_event(subj, start, end, **kwargs))
        else:
            raise ValueError(f"unexpected event spec shape: {spec}")
    return out
