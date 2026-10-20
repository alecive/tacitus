#!/usr/bin/env python3
"""Tests for the EA-Managed protection reconciler.

Hermetic: drives the pure planning layer on fixtures, and apply_actions on
a recording fake service. No network, no live calendars.

Protection is declarative, so the solver's movability heuristic cannot
offer a zero-attendee lecture away as if it were a 1:1. A recurring
zero-attendee source gets a visible p0 [auto] mirror on EA-Managed,
anchored to the source series id, and a user deletion of that mirror is
a logged, permanent release rather than a glitch to undo.
"""

import json
import os
import sys
import tempfile
from pathlib import Path


# No test may touch production state. google_calendar_client (pulled in by
# calendar_reconciler) does AUTH_DIR.mkdir() at import, so EA_DIR must be a
# sandbox BEFORE the import below; unset, it resolves to the live ~/.ea.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-calrecon-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))

import calendar_reconciler as cr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _instance(summary="Kit lecture", series="ser-lecture", att=0,
              start="2026-08-20T14:00:00-06:00", end="2026-08-20T16:00:00-06:00",
              **over):
    item = {
        "summary": summary,
        "recurringEventId": series,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "status": "confirmed",
    }
    if att:
        item["attendees"] = [{"email": f"a{i}@x.edu"} for i in range(att)]
    item.update(over)
    return item


def _master(summary="Kit lecture", series_id="ser-lecture",
            start="2026-08-20T14:00:00-06:00", end="2026-08-20T16:00:00-06:00",
            rrule="RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261204T065959Z"):
    return {
        "id": series_id,
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "recurrence": [rrule],
    }


def _mirror(key="jo.ambrose4@example.com/ser-lecture", status="confirmed",
            summary="p0 [auto] Kit lecture",
            start="2026-08-20T14:00:00-06:00", end="2026-08-20T16:00:00-06:00",
            rrule="RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261204T065959Z"):
    return {
        "id": "mir-1",
        "summary": summary,
        "description": cr.build_description(key, cr.RULE_RECURRING_HOLD),
        "status": status,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "recurrence": [rrule],
    }


LECTURE_INFO_KEY = "jo.ambrose4@example.com/ser-lecture"


def _protected(instances=None):
    return {LECTURE_INFO_KEY: {
        "source_key": LECTURE_INFO_KEY,
        "summary": "Kit lecture",
        "start_local": "2026-08-20T14:00:00",
        "end_local": "2026-08-20T16:00:00",
        "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261204T065959Z"],
        "instances": instances or [("2026-08-20T14:00:00", "2026-08-20T16:00:00"),
                                   ("2026-08-25T14:00:00", "2026-08-25T16:00:00")],
    }}


def _view_item(subject="Kit lecture", master="AAMk-outlook-master",
               start="2026-08-20T20:00:00", end="2026-08-20T22:00:00",
               att=0, **over):
    """A graph_actions calendar-view dict. Times naive UTC, per Graph."""
    item = {"subject": subject, "series_master_id": master, "start": start,
            "end": end, "attendees": att, "all_day": False,
            "show_as": "busy", "event_type": "occurrence"}
    item.update(over)
    return item


def _outlook_master(subject="Kit lecture"):
    """Raw Graph series master, as get-event returns it."""
    return {
        "subject": subject,
        "start": {"dateTime": "2026-08-20T20:00:00.0000000",
                  "timeZone": "UTC"},
        "end": {"dateTime": "2026-08-20T22:00:00.0000000", "timeZone": "UTC"},
        "recurrence": {
            "pattern": {"type": "weekly", "interval": 1,
                        "daysOfWeek": ["tuesday", "thursday"]},
            "range": {"type": "endDate", "startDate": "2026-08-20",
                      "endDate": "2026-12-03",
                      "recurrenceTimeZone": "America/Denver"},
        },
    }


# ---------------------------------------------------------------------------
# Rule R1
# ---------------------------------------------------------------------------

def test_recurring_zero_attendee_lecture_qualifies():
    assert cr.qualifies_as_protected(_instance()) is True


def test_every_meeting_shape_qualifies_at_item_level():
    """Attendee count picks the mirror level and never gates existence.
    One-offs pass the item test too; whether they are MIRRORED is the
    source's one_offs setting. The live run sets it on every blocking
    calendar; a source without it skips them."""
    assert cr.qualifies_as_protected(_instance(att=1))
    assert cr.qualifies_as_protected(_instance(att=9))
    one_off = _instance()
    del one_off["recurringEventId"]
    assert cr.qualifies_as_protected(one_off)
    scanned = cr.scan_google(lambda e, c: [dict(one_off, id="x1")],
                             lambda e, c, s: _master(),
                             [{"email": "jo.ambrose4@example.com"}])
    assert scanned == {}, "default sources must not mirror one-offs"


def test_token_level_is_the_movability_declaration():
    assert cr._level_and_rule(0) == ("p0", cr.RULE_RECURRING_HOLD)
    assert cr._level_and_rule(1) == ("p1", cr.RULE_RECURRING_1ON1)
    assert cr._level_and_rule(9) == ("p0", cr.RULE_RECURRING_GROUP)


def test_rules_allday_free_and_cancelled_do_not_qualify():
    # a label is the user's level for the event, read by source_label
    assert cr.qualifies_as_protected(_instance(summary="cleaner [note]"))
    assert cr.qualifies_as_protected(_instance(summary="p1 soft pref"))
    assert not cr.qualifies_as_protected(_instance(summary="wh working hours"))
    assert not cr.qualifies_as_protected(_instance(summary="wr from home"))
    assert not cr.qualifies_as_protected(
        _instance(transparency="transparent")), "shown as Free blocks nothing"
    assert not cr.qualifies_as_protected(_instance(status="cancelled"))
    allday = _instance()
    allday["start"] = {"date": "2026-08-20"}
    assert not cr.qualifies_as_protected(allday)


def test_scan_google_collects_series_and_instances():
    accounts = [{"email": "jo.ambrose4@example.com"}]
    items = [_instance(),
             _instance(start="2026-08-25T14:00:00-06:00",
                       end="2026-08-25T16:00:00-06:00")]
    protected = cr.scan_google(lambda e, c: items,
                               lambda e, c, s: _master(), accounts)
    assert list(protected) == [LECTURE_INFO_KEY]
    assert len(protected[LECTURE_INFO_KEY]["instances"]) == 2


def test_exchange_anchors_the_mirror_for_cross_calendar_duplicates():
    """The same lecture on Exchange and Google is one commitment, and
    Exchange, scanned first, owns the key. Day order in BYDAY must not
    break the signature match (Google writes TH,TU; the translator
    writes TU,TH)."""
    outlook = cr.scan_outlook([_view_item()], lambda sid: _outlook_master())
    google = cr.scan_google(lambda e, c: [_instance()],
                            lambda e, c, s: _master(),
                            [{"email": "jo.ambrose4@example.com"}])
    merged = cr.dedupe_series([outlook, google])
    assert list(merged) == ["outlook/AAMk-outlook-master"]
    assert merged["outlook/AAMk-outlook-master"]["start_local"] == \
        "2026-08-20T14:00:00", "naive-UTC Graph times must land in Denver"


# ---------------------------------------------------------------------------
# Outlook rule + recurrence translation
# ---------------------------------------------------------------------------

def test_qualifies_outlook():
    assert cr.qualifies_outlook(_view_item()) is True
    assert cr.qualifies_outlook(_view_item(att=9)), \
        "group meetings are reflected too, at level p0"
    assert cr.qualifies_outlook(_view_item(series_master_id="")), \
        "one-off meetings are mirrored too"
    assert not cr.qualifies_outlook(_view_item(my_response="declined"))
    assert not cr.qualifies_outlook(_view_item(show_as="free"))
    assert not cr.qualifies_outlook(_view_item(all_day=True))
    assert cr.qualifies_outlook(_view_item(subject="p1 soft pref"))
    assert cr.qualifies_outlook(_view_item(subject="cleaner [note]"))
    assert not cr.qualifies_outlook(_view_item(subject="wh hours"))


def test_outlook_weekly_end_date_translates_to_google_style_rrule():
    rrule = cr.outlook_recurrence_to_rrule(_outlook_master()["recurrence"])
    assert rrule == ("RRULE:FREQ=WEEKLY;BYDAY=TH,TU;"
                     "UNTIL=20261204T065959Z"), (
        "Dec 3 end-of-day in Denver (MST) is 06:59:59Z on Dec 4, matching "
        "how Google writes its own UNTILs")


def test_outlook_daily_numbered_and_unsupported_patterns():
    daily = cr.outlook_recurrence_to_rrule({
        "pattern": {"type": "daily", "interval": 2},
        "range": {"type": "numbered", "numberOfOccurrences": 10}})
    assert daily == "RRULE:FREQ=DAILY;INTERVAL=2;COUNT=10"
    monthly = cr.outlook_recurrence_to_rrule({
        "pattern": {"type": "absoluteMonthly", "dayOfMonth": 3},
        "range": {"type": "noEnd"}})
    assert monthly == "", "unsupported patterns skip, never half-protect"


# ---------------------------------------------------------------------------
# Mirror inventory
# ---------------------------------------------------------------------------

def test_load_mirrors_splits_active_and_tombstoned():
    active, tomb = cr.load_mirrors([
        _mirror(),
        _mirror(key="jo.ambrose4@example.com/ser-old", status="cancelled"),
    ])
    assert set(active) == {LECTURE_INFO_KEY}
    assert tomb == {"jo.ambrose4@example.com/ser-old"}


def test_load_mirrors_ignores_exception_instances():
    """An exception instance inherits the master's summary and keyed
    description and appears in the singleEvents=False listing. It must
    not clobber the master's active entry, or a series retime lands on an
    instance id, and a CANCELLED one must not tombstone the whole
    series."""
    master = _mirror()
    live_exc = dict(_mirror(), id="master-id_20260819T150000Z",
                    recurringEventId="master-id", recurrence=None)
    dead_exc = dict(_mirror(), id="master-id_20260826T150000Z",
                    recurringEventId="master-id", recurrence=None,
                    status="cancelled")
    active, tombstoned = cr.load_mirrors([master, live_exc, dead_exc])
    assert active[LECTURE_INFO_KEY]["id"] == master["id"]
    assert tombstoned == set()


def test_load_mirrors_ignores_hand_placed_tokens():
    """A p0 without [auto] is the user's own hold and must be invisible
    to the reconciler in every direction."""
    active, tomb = cr.load_mirrors([
        {"id": "x", "summary": "p0 Casey teacher visit", "status": "confirmed",
         "description": "", "start": {}, "end": {}},
    ])
    assert active == {} and tomb == set()


def test_description_round_trip():
    d = cr.build_description(LECTURE_INFO_KEY, cr.RULE_RECURRING_HOLD)
    assert cr.parse_source_key(d) == LECTURE_INFO_KEY
    assert cr.RULE_RECURRING_HOLD in d


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_plan_creates_missing_mirror():
    actions = cr.plan_actions(_protected(), {}, set(), set())
    assert [a["op"] for a in actions] == ["create"]


def test_plan_in_sync_is_a_noop():
    active, _ = cr.load_mirrors([_mirror()])
    assert cr.plan_actions(_protected(), active, set(), set()) == []


def test_plan_retimes_when_source_moved():
    active, _ = cr.load_mirrors([_mirror(start="2026-08-20T13:00:00-06:00",
                                         end="2026-08-20T15:00:00-06:00")])
    actions = cr.plan_actions(_protected(), active, set(), set())
    assert [a["op"] for a in actions] == ["retime"]


def test_plan_removes_mirror_when_source_gone():
    active, _ = cr.load_mirrors([_mirror()])
    actions = cr.plan_actions({}, active, set(), set())
    assert [a["op"] for a in actions] == ["remove"]


def test_user_deletion_is_released_once_and_kept_as_a_note():
    """Tombstoned mirror + still-present source = a fresh user decision:
    log the release once, and keep the event on the calendar as a [note]
    that blocks nothing. Deleting the note too ends it."""
    fresh = cr.plan_actions(_protected(), {}, {LECTURE_INFO_KEY}, set())
    assert [a["op"] for a in fresh] == ["release", "create"]
    assert fresh[1]["info"]["level"] == "note"
    assert fresh[1]["info"]["rule"] == cr.RULE_RELEASED
    later = cr.plan_actions(_protected(), {}, {LECTURE_INFO_KEY},
                            {LECTURE_INFO_KEY})
    assert [a["op"] for a in later] == ["create"], "logged once, noted once"
    note = _mirror(summary="[note] [auto] Kit lecture")
    kept = cr.plan_actions(_protected(), {LECTURE_INFO_KEY: note},
                           {LECTURE_INFO_KEY}, {LECTURE_INFO_KEY})
    assert kept == [], "the note mirror stands; its level is never touched"
    gone = cr.plan_actions(_protected(), {}, {LECTURE_INFO_KEY},
                           {LECTURE_INFO_KEY}, final_keys={LECTURE_INFO_KEY})
    assert gone == []


# ---------------------------------------------------------------------------
# Release memory
# ---------------------------------------------------------------------------

def test_log_release_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "DECISIONS_FILE", tmp_path / "decisions.json")
    cr.log_release(LECTURE_INFO_KEY, "Kit lecture")
    assert cr.released_keys_from_log() == {LECTURE_INFO_KEY}
    entries = json.loads((tmp_path / "decisions.json").read_text())
    assert entries[0]["action"] == "released"
    assert entries[0]["event"] == "Kit lecture"


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

class _FakeEvents:
    def __init__(self, log):
        self.log = log

    def insert(self, calendarId, body):
        self.log.append(("insert", calendarId, body))
        return self

    def patch(self, calendarId, eventId, body):
        self.log.append(("patch", calendarId, eventId, body))
        return self

    def delete(self, calendarId, eventId):
        self.log.append(("delete", calendarId, eventId))
        return self

    def execute(self):
        return {"id": "new-mirror-id"}


class _FakeService:
    def __init__(self, log):
        self._events = _FakeEvents(log)

    def events(self):
        return self._events


def test_every_write_targets_the_ea_managed_calendar_by_id(tmp_path,
                                                           monkeypatch):
    """Scope invariant, and the reason the reconciler's deletes need no
    confirmation button: it writes ONLY to the EA-Managed calendar, and
    only to event ids that came out of load_mirrors or a mirror's own
    instance list. Nothing here is chosen by matching a subject string,
    and nothing here can reach Outlook or a personal calendar, where a
    delete mails cancellations to attendees."""
    monkeypatch.setattr(cr, "DECISIONS_FILE", tmp_path / "decisions.json")
    log = []
    active, _ = cr.load_mirrors([_mirror()])
    plan = cr.plan_actions(_protected(), {}, set(), set())          # create
    plan += cr.plan_actions({}, active, set(), set())               # remove
    plan += cr.plan_instance_actions(
        _protected(instances=[("2026-08-20T14:00:00", "2026-08-20T16:00:00")]),
        active, _MIRROR_INSTANCES, set())                           # cancel
    cr.apply_actions(plan, _FakeService(log), {"calendar_id": "ea-cal-id"})
    assert {"insert", "patch", "delete"} & {e[0] for e in log}
    known_ids = {"mir-1", "inst-thu", "inst-tue"}
    for entry in log:
        assert entry[1] == "ea-cal-id", f"write escaped EA-Managed: {entry}"
        if entry[0] in ("patch", "delete"):
            assert entry[2] in known_ids, \
                f"target id did not come from a mirror inventory: {entry}"


def test_the_live_run_targets_only_the_owner_private_calendar(tmp_path, monkeypatch):
    """The cron writes with the owner's token, so the calendar it writes
    comes from the owner-private calendars.json, never from the resident's
    scheduling-preferences.json. Without the file it writes nothing and
    builds no service at all."""
    import mail_mediation as M
    import owner_trust
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv(M.OWNER_SIDE_ENV, "1")
    monkeypatch.setattr(owner_trust, "private_dir", lambda: priv)
    monkeypatch.setattr(cr, "DECISIONS_FILE", tmp_path / "decisions.json")
    monkeypatch.setattr(cr.gcal, "_ea_managed_source", lambda: {
        "email": "resident.pick@example.com", "calendar_id": "the-users-own@example.com"})
    built = []

    def outlook(*argv):
        raise AssertionError("the run went on without a target")
    monkeypatch.setattr(cr, "_graph_actions", outlook)
    monkeypatch.setattr(cr.gcal, "_build_service",
                        lambda email, *a, **k: built.append(email) or None)
    r = cr._live_run(apply=True, horizon_days=7)
    assert "calendars.json" in r["error"] and built == []

    (priv / "calendars.json").write_text(json.dumps({
        "ea_managed_calendar_id": "owner-ea@group.calendar.google.com",
        "ea_managed_account": "owner.account@example.com"}))
    os.chmod(priv / "calendars.json", 0o600)
    listed = []
    monkeypatch.setattr(cr, "_graph_actions", lambda *argv: {"events": []})
    monkeypatch.setattr(cr, "_blocking_accounts", lambda: [])
    monkeypatch.setattr(cr, "_user_domain", lambda: "@example.edu")
    monkeypatch.setattr(cr.gcal, "_build_service",
                        lambda email, *a, **k: built.append(email) or object())
    monkeypatch.setattr(cr, "list_masters", lambda svc, cal: listed.append(cal) or [])
    r = cr._live_run(apply=True, horizon_days=7)
    assert "error" not in r, r
    assert listed == ["owner-ea@group.calendar.google.com"]
    assert built == ["owner.account@example.com"]


def test_the_reconciler_only_ever_READS_outlook():
    """It reaches Exchange through graph_actions subprocesses. Every verb
    it can name must be read-only: a mirror is reconciled by writing to
    EA-Managed, never by mutating the source commitment."""
    import re
    src = Path(cr.__file__).read_text()
    verbs = set(re.findall(r'_graph_actions\(\s*"([a-z-]+)"', src))
    assert verbs, "sanity: the Outlook path is a graph_actions subprocess"
    assert verbs <= {"calendar-view", "get-event"}, verbs


def test_apply_create_carries_marker_and_anchor(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "DECISIONS_FILE", tmp_path / "decisions.json")
    log = []
    actions = cr.plan_actions(_protected(), {}, set(), set())
    results = cr.apply_actions(actions, _FakeService(log),
                               {"calendar_id": "ea-cal-id"})
    assert results[0]["status"] == "ok"
    op, cal, body = log[0]
    assert op == "insert" and cal == "ea-cal-id"
    assert body["summary"].startswith("p0 ") and cr.AUTO_MARKER in body["summary"]
    assert cr.parse_source_key(body["description"]) == LECTURE_INFO_KEY
    assert body["recurrence"], "mirror must be one recurring series"
    assert body["start"]["timeZone"] == "America/Denver"


def test_removal_stamps_provenance_before_deleting(tmp_path, monkeypatch):
    """Google hands a deleted master back through showDeleted WITH its
    description, so the description is where provenance has to go. Stamp
    first, delete second."""
    monkeypatch.setattr(cr, "DECISIONS_FILE", tmp_path / "decisions.json")
    log = []
    active, _ = cr.load_mirrors([_mirror()])
    actions = cr.plan_actions({}, active, set(), set())
    results = cr.apply_actions(actions, _FakeService(log),
                               {"calendar_id": "ea-cal-id"})
    assert results[0]["status"] == "ok"
    assert [entry[0] for entry in log] == ["patch", "delete"]
    _op, _cal, event_id, body = log[0]
    assert event_id == "mir-1"
    assert cr.is_reconciler_removal(body["description"])
    # The source anchor survives the stamp; the tombstone is still
    # attributable to its series.
    assert cr.parse_source_key(body["description"]) == LECTURE_INFO_KEY


def test_a_failed_stamp_cancels_the_delete(tmp_path, monkeypatch):
    """An un-removed mirror costs a stale hold for 30 minutes. An
    unstamped tombstone costs the protection permanently, in Kit's
    name. So the stamp gates the delete."""
    monkeypatch.setattr(cr, "DECISIONS_FILE", tmp_path / "decisions.json")
    log = []

    class _NoPatch(_FakeService):
        def events(self):
            events = super().events()
            events.patch = _raise
            return events

    def _raise(**kw):
        raise RuntimeError("500 backendError")

    active, _ = cr.load_mirrors([_mirror()])
    results = cr.apply_actions(cr.plan_actions({}, active, set(), set()),
                               _NoPatch(log), {"calendar_id": "ea-cal-id"})
    assert results[0]["status"] == "error"
    assert not [entry for entry in log if entry[0] == "delete"]


def test_a_failed_delete_takes_the_stamp_back_off(tmp_path, monkeypatch):
    """A LIVE mirror carrying a removal stamp is worse than no stamp: the
    user's own deletion of it would later read as ours and their release
    would be undone. So a delete that fails after the stamp landed puts
    the description back."""
    monkeypatch.setattr(cr, "DECISIONS_FILE", tmp_path / "decisions.json")
    log = []

    class _NoDelete(_FakeService):
        def events(self):
            events = super().events()
            events.delete = _raise
            return events

    def _raise(**kw):
        raise RuntimeError("503 backendError")

    active, _ = cr.load_mirrors([_mirror()])
    results = cr.apply_actions(cr.plan_actions({}, active, set(), set()),
                               _NoDelete(log), {"calendar_id": "ea-cal-id"})
    assert results[0]["status"] == "error"
    assert [e[0] for e in log] == ["patch", "patch"]
    assert not cr.is_reconciler_removal(log[-1][3]["description"])
    assert cr.parse_source_key(log[-1][3]["description"]) == LECTURE_INFO_KEY


def test_a_reconciler_tombstone_is_not_read_as_a_user_deletion():
    """The booby trap: the reconciler removes a mirror (horizon roll, or
    the transient failure above), the series comes back, and the
    tombstone is read as a fresh decision by Kit -- logged permanently,
    never re-protected, and reported to him as 'released by you'."""
    stamped = _mirror(status="cancelled")
    stamped["description"] = cr.mark_description_removed(
        stamped["description"])
    active, tombstoned = cr.load_mirrors([stamped])
    assert active == {}
    assert tombstoned == set(), "our own removal is not the user's decision"
    assert cr.self_removed_keys([stamped]) == {LECTURE_INFO_KEY}
    # ...so a returning series is simply re-protected.
    assert [a["op"] for a in
            cr.plan_actions(_protected(), active, tombstoned, set())] \
        == ["create"]


def test_a_hand_deleted_mirror_still_releases_permanently():
    """The other half of the same discrimination: an UNSTAMPED tombstone
    is still the user's decision: released, and kept only as a note."""
    hand_deleted = _mirror(status="cancelled")
    active, tombstoned = cr.load_mirrors([hand_deleted])
    assert tombstoned == {LECTURE_INFO_KEY}
    assert cr.self_removed_keys([hand_deleted]) == set()
    assert [a["op"] for a in
            cr.plan_actions(_protected(), active, tombstoned, set())] \
        == ["release", "create"]


def test_apply_release_writes_the_decision_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "DECISIONS_FILE", tmp_path / "decisions.json")
    results = cr.apply_actions(
        [{"op": "release", "key": LECTURE_INFO_KEY, "summary": "Kit lecture"}],
        _FakeService([]), {"calendar_id": "ea-cal-id"})
    assert results[0]["status"] == "ok"
    assert cr.released_keys_from_log() == {LECTURE_INFO_KEY}


# ---------------------------------------------------------------------------
# Per-instance reconciliation
# ---------------------------------------------------------------------------

_MIRROR_INSTANCES = {LECTURE_INFO_KEY: [
    ("2026-08-20T14:00:00", "2026-08-20T16:00:00", "inst-thu"),
    ("2026-08-25T14:00:00", "2026-08-25T16:00:00", "inst-tue"),
]}


def _active():
    active, _ = cr.load_mirrors([_mirror()])
    return active


def test_in_sync_instances_are_a_noop():
    actions = cr.plan_instance_actions(_protected(), _active(),
                                       _MIRROR_INSTANCES, set())
    assert actions == []


def test_cancelled_source_instance_releases_the_mirror_occurrence():
    """The Thanksgiving case: the source instance vanishes from the
    expanded view, so the matching mirror occurrence must be cancelled
    instead of freezing a holiday afternoon."""
    protected = _protected(instances=[("2026-08-20T14:00:00",
                                       "2026-08-20T16:00:00")])
    actions = cr.plan_instance_actions(protected, _active(),
                                       _MIRROR_INSTANCES, set())
    assert [a["op"] for a in actions] == ["cancel_instance"]
    assert actions[0]["instance_id"] == "inst-tue"


def test_moved_source_instance_retimes_the_mirror_occurrence():
    protected = _protected(instances=[
        ("2026-08-20T14:00:00", "2026-08-20T16:00:00"),
        ("2026-08-25T15:00:00", "2026-08-25T17:00:00"),  # moved 1h later
    ])
    actions = cr.plan_instance_actions(protected, _active(),
                                       _MIRROR_INSTANCES, set())
    assert [a["op"] for a in actions] == ["retime_instance"]
    assert actions[0]["instance_id"] == "inst-tue"
    assert actions[0]["start_local"] == "2026-08-25T15:00:00"


def test_restored_source_instance_is_reported_not_written():
    """Google cannot cleanly resurrect a cancelled series instance, so an
    un-cancelled source occurrence surfaces as a gap for a human look."""
    protected = _protected(instances=[
        ("2026-08-20T14:00:00", "2026-08-20T16:00:00"),
        ("2026-08-25T14:00:00", "2026-08-25T16:00:00"),
        ("2026-08-27T14:00:00", "2026-08-27T16:00:00"),
    ])
    actions = cr.plan_instance_actions(protected, _active(),
                                       _MIRROR_INSTANCES, set())
    assert [a["op"] for a in actions] == ["instance_gap"]


def test_rescheduled_instance_moves_the_mirror_not_cancel_plus_gap():
    """One occurrence moved to an off-pattern day pairs the vacated slot
    with the new one, instead of releasing the old and nagging about the
    new every half hour."""
    protected = _protected(instances=[
        ("2026-08-20T14:00:00", "2026-08-20T16:00:00"),
        ("2026-08-26T09:00:00", "2026-08-26T11:00:00"),  # Tue -> Wed
    ])
    actions = cr.plan_instance_actions(protected, _active(),
                                       _MIRROR_INSTANCES, set())
    assert [a["op"] for a in actions] == ["move_instance"]
    move = actions[0]
    assert move["instance_id"] == "inst-tue"
    assert move["start_local"] == "2026-08-26T09:00:00"
    assert move["revive"] is False


def test_cross_run_reschedule_revives_the_cancelled_mirror_instance():
    """When the cancel half already executed on a previous run, the gap
    pairs with the cancelled instance and revives it at the new time."""
    protected = _protected(instances=[
        ("2026-08-20T14:00:00", "2026-08-20T16:00:00"),
        ("2026-08-26T09:00:00", "2026-08-26T11:00:00"),
    ])
    live = {LECTURE_INFO_KEY: [
        ("2026-08-20T14:00:00", "2026-08-20T16:00:00", "inst-thu")]}
    cancelled = {LECTURE_INFO_KEY: [("2026-08-25", "inst-tue")]}
    actions = cr.plan_instance_actions(protected, _active(), live, set(),
                                       cancelled_instances=cancelled)
    assert [a["op"] for a in actions] == ["move_instance"]
    assert actions[0]["instance_id"] == "inst-tue"
    assert actions[0]["revive"] is True


def test_user_released_occurrence_is_not_revived():
    """A cancelled mirror instance on a day the source still occupies is
    the user's own release; their deletion stands and the gap stays a
    reported gap."""
    protected = _protected()
    live = {LECTURE_INFO_KEY: [
        ("2026-08-20T14:00:00", "2026-08-20T16:00:00", "inst-thu")]}
    cancelled = {LECTURE_INFO_KEY: [("2026-08-25", "inst-tue")]}
    actions = cr.plan_instance_actions(protected, _active(), live, set(),
                                       cancelled_instances=cancelled)
    assert [a["op"] for a in actions] == ["instance_gap"]


def test_no_mirror_is_created_while_a_source_is_unreliable():
    """A broken fetch defers creates the way it already freezes removals.
    With one source down, the Google copies of its anchored series lose
    their anchor and look new, and the source's return would then remove
    the mirrors as 'source gone'."""
    protected = _protected()
    key = next(iter(protected))
    google_key = "xan.marchetti45@example.com/series-lecture-copy"
    protected[google_key] = dict(protected[key], summary="Kit lecture")
    # every source readable: the missing mirror is created
    actions = cr.plan_actions(protected, {}, set(), set())
    assert sorted(a["op"] for a in actions) == ["create", "create"]
    # Outlook unreliable: nothing is created, for ANY source
    actions = cr.plan_actions(protected, {}, set(), set(),
                              unreliable_prefixes={"outlook"})
    assert actions == []
    # and a release is still recorded (it is the user's own deletion)
    actions = cr.plan_actions(protected, {}, {google_key}, set(),
                              unreliable_prefixes={"outlook"})
    assert [a["op"] for a in actions] == ["release"]


def test_horizon_window_ends_both_sources_at_the_same_instant_across_dst():
    """Both sources end the horizon at one instant, computed by one
    helper, at the end of the horizon day rather than on a sliding edge.
    A local bound and a UTC bound drift an hour apart across a DST
    change."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    mt = ZoneInfo("America/Denver")
    now = datetime(2026, 9, 3, 14, 7, tzinfo=mt)              # MDT
    start, end, span = cr.horizon_window(now, 60)
    assert start == "2026-09-03T14:07" and end == "2026-11-02T23:59"
    assert span[0] == now.astimezone(timezone.utc)
    assert span[1].isoformat() == "2026-11-03T06:59:00+00:00"  # 23:59 MST
    # parsed the way calendar-view parses a naive bound: the same instant
    parsed = datetime.fromisoformat(end).replace(tzinfo=mt).astimezone(timezone.utc)
    assert parsed == span[1]
    # inside one DST regime the same holds
    now = datetime(2026, 7, 1, 9, 0, tzinfo=mt)
    _, end, span = cr.horizon_window(now, 60)
    assert end == "2026-08-30T23:59"
    assert span[1].isoformat() == "2026-08-31T05:59:00+00:00"


def test_a_gap_never_pairs_with_a_vacated_slot_a_season_away():
    """A partner more than MAX_MOVE_DAYS away is not the same occurrence
    rescheduled: report the gap and release the slot. Pairing across a
    season moves a mirror instance onto a day that already holds a
    natural one, and cancels it the run after."""
    protected = _protected(instances=[
        ("2026-11-02T13:15:00", "2026-11-02T14:15:00")])
    cancelled = {LECTURE_INFO_KEY: [("2026-09-07", "inst-labor-day")]}
    actions = cr.plan_instance_actions(protected, _active(),
                                       {LECTURE_INFO_KEY: []}, set(),
                                       cancelled_instances=cancelled)
    assert [a["op"] for a in actions] == ["instance_gap"]
    # a same-run vacated slot that far away is not paired either
    live = {LECTURE_INFO_KEY: [
        ("2026-09-07T13:15:00", "2026-09-07T14:15:00", "inst-labor-day")]}
    actions = cr.plan_instance_actions(protected, _active(), live, set())
    assert sorted(a["op"] for a in actions) == ["cancel_instance", "instance_gap"]
    # within the limit, the pairing still happens
    protected = _protected(instances=[
        ("2026-09-14T13:15:00", "2026-09-14T14:15:00")])
    actions = cr.plan_instance_actions(protected, _active(), live, set())
    assert [a["op"] for a in actions] == ["move_instance"]
    assert actions[0]["instance_id"] == "inst-labor-day"


def test_gap_warnings_post_once(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "GAP_WARNED_FILE", tmp_path / "gaps.json")
    gap = {"op": "instance_gap", "key": "k", "summary": "X",
           "when": "2026-08-19T10:00:00", "status": "reported"}
    other = {"op": "create", "key": "k2", "info": {"summary": "Y"},
             "status": "ok"}
    first = cr.suppress_known_gaps([gap, other])
    assert gap in first and other in first
    second = cr.suppress_known_gaps([gap, other])
    assert gap not in second and other in second
    # Gap heals -> state clears -> a re-opened gap warns afresh.
    cr.suppress_known_gaps([other])
    third = cr.suppress_known_gaps([gap])
    assert gap in third


def test_series_level_action_defers_instance_reconciliation():
    protected = _protected(instances=[("2026-08-20T14:00:00",
                                       "2026-08-20T16:00:00")])
    actions = cr.plan_instance_actions(protected, _active(),
                                       _MIRROR_INSTANCES,
                                       {LECTURE_INFO_KEY})
    assert actions == []


# ---------------------------------------------------------------------------
# Fetch-failure safety
# ---------------------------------------------------------------------------

def test_failed_source_fetch_freezes_removals():
    """An Outlook outage must not read as 'every commitment vanished'."""
    active, _ = cr.load_mirrors([_mirror(key="outlook/AAMk-master",
                                         summary="p0 [auto] Kit lecture")])
    frozen = cr.plan_actions({}, active, set(), set(),
                             unreliable_prefixes={"outlook"})
    assert frozen == [], "mirrors of an unreachable source must be left alone"
    normal = cr.plan_actions({}, active, set(), set())
    assert [a["op"] for a in normal] == ["remove"]


def test_transient_master_fetch_failure_freezes_only_that_series():
    """A 429 (or a subprocess timeout) on ONE series master must be
    reported as a failed fetch. Swallowed, it reads identically to 'this
    series no longer exists', and the next step removes the mirror as an
    orphan and tombstones it permanently."""
    def _boom(series_id):
        raise cr.MasterFetchError("HTTP 429 Too Many Requests")

    failed = set()
    protected = cr.scan_outlook([_view_item()], _boom, failed=failed)
    assert protected == {}
    assert failed == {"outlook/AAMk-outlook-master"}

    active, _ = cr.load_mirrors([_mirror(key="outlook/AAMk-outlook-master")])
    frozen = cr.plan_actions({}, active, set(), set(), frozen_keys=failed)
    assert frozen == [], "an unreadable master must never unprotect its slot"
    # A series that really is gone still gets removed: it has no
    # occurrences in the view, so its master is never fetched.
    assert [a["op"] for a in cr.plan_actions({}, active, set(), set())] \
        == ["remove"]


def test_a_master_that_is_genuinely_gone_is_not_frozen():
    """Only a RAISED fetch failure freezes. A get_master that answers
    None (no such master) is real information and still removes."""
    failed = set()
    assert cr.scan_outlook([_view_item()], lambda sid: None,
                           failed=failed) == {}
    assert failed == set()


def test_google_master_fetch_failure_freezes_that_series():
    def _boom(email, cal, series_id):
        raise cr.MasterFetchError("SSLError")

    failed = set()
    protected = cr.scan_google(lambda e, c: [_instance()], _boom,
                               [{"email": "jo.ambrose4@example.com"}],
                               failed=failed)
    assert protected == {}
    assert failed == {LECTURE_INFO_KEY}
    active, _ = cr.load_mirrors([_mirror()])
    assert cr.plan_actions({}, active, set(), set(),
                           frozen_keys=failed) == []


def test_failed_source_freezes_instance_actions_too():
    protected = {"outlook/AAMk-master": {**_protected()[LECTURE_INFO_KEY],
                                         "source_key": "outlook/AAMk-master",
                                         "instances": []}}
    active, _ = cr.load_mirrors([_mirror(key="outlook/AAMk-master")])
    instances = {"outlook/AAMk-master":
                 _MIRROR_INSTANCES[LECTURE_INFO_KEY]}
    actions = cr.plan_instance_actions(protected, active, instances, set(),
                                       unreliable_prefixes={"outlook"})
    assert actions == []


# ---------------------------------------------------------------------------
# Google normalizes stored RRULEs; comparison must be semantic
# ---------------------------------------------------------------------------

def test_rrule_signature_ignores_param_order_wkst_and_day_order():
    """Google reorders a stored RRULE's parameters, so raw string
    comparison would plan a retime on every run, forever."""
    written = ["RRULE:FREQ=WEEKLY;BYDAY=TH,TU;UNTIL=20261204T065959Z"]
    stored = ["RRULE:FREQ=WEEKLY;UNTIL=20261204T065959Z;BYDAY=TU,TH;WKST=SU"]
    assert cr._rrule_signature(written) == cr._rrule_signature(stored)
    other = ["RRULE:FREQ=WEEKLY;BYDAY=TH;UNTIL=20261204T065959Z"]
    assert cr._rrule_signature(written) != cr._rrule_signature(other)


def test_plan_does_not_retime_on_normalized_recurrence():
    active, _ = cr.load_mirrors([_mirror(
        rrule="RRULE:FREQ=WEEKLY;UNTIL=20261204T065959Z;BYDAY=TU,TH;WKST=SU")])
    assert cr.plan_actions(_protected(), active, set(), set()) == []


def test_notify_survives_and_posts(monkeypatch):
    """notify must import slack_notifier standalone (running as a script,
    'preprocessor' is not on sys.path) and post one message per run."""
    posts = []

    class _Fake:
        @staticmethod
        def post_to_slack(key, text, **kw):
            posts.append((key, text))
            return True

        @staticmethod
        def calendar_channel_key():
            return "calendar"

    monkeypatch.setitem(sys.modules, "slack_notifier", _Fake())
    # a routine create posts nothing
    cr.notify([{"op": "create", "key": "outlook/x", "status": "ok",
                "info": {"summary": "Kit lecture"}}])
    assert posts == []
    # a failed create, or a protection-reducing change, posts once, to
    # the calendar target, and lists only what needs a look
    cr.notify([{"op": "create", "key": "outlook/x", "status": "ok",
                "info": {"summary": "Kit lecture"}},
               {"op": "create", "key": "outlook/y", "status": "error",
                "error": "403", "info": {"summary": "Oakley / Kit"}},
               {"op": "remove", "key": "outlook/z", "status": "ok",
                "info": {"summary": "Survey team call"}}])
    assert len(posts) == 1 and posts[0][0] == "calendar"
    text = posts[0][1]
    assert text.startswith(":warning: *Calendar protection changed*")
    assert "Oakley / Kit" in text and "FAILED: 403" in text
    assert "Survey team call" in text and "Kit lecture" not in text


# ---------------------------------------------------------------------------
# Full reflection: levels on creation, user promotions preserved
# ---------------------------------------------------------------------------

def test_one_on_one_mirror_is_created_as_p1(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "DECISIONS_FILE", tmp_path / "d.json")
    protected = _protected()
    protected[LECTURE_INFO_KEY]["level"] = "p1"
    protected[LECTURE_INFO_KEY]["rule"] = cr.RULE_RECURRING_1ON1
    protected[LECTURE_INFO_KEY]["summary"] = "Cam / Kit"
    log = []
    actions = cr.plan_actions(protected, {}, set(), set())
    cr.apply_actions(actions, _FakeService(log), {"calendar_id": "ea"})
    _, _, body = log[0]
    assert body["summary"] == "p1 [auto] Cam / Kit"
    assert cr.RULE_RECURRING_1ON1 in body["description"]


def test_p1_mirror_is_recognized_and_releasable():
    active, tomb = cr.load_mirrors([
        _mirror(summary="p1 [auto] Cam / Kit"),
        _mirror(key="outlook/gone", summary="p1 [auto] Old 1:1",
                status="cancelled"),
    ])
    assert set(active) == {LECTURE_INFO_KEY}
    assert tomb == {"outlook/gone"}


def test_user_promotion_to_p0_is_never_reverted():
    """The user edits one word (p1 -> p0) to make a 1:1 immovable. The
    reconciler manages times, so an in-sync series with a promoted level
    must plan nothing."""
    protected = _protected()
    protected[LECTURE_INFO_KEY]["level"] = "p1"
    active, _ = cr.load_mirrors([_mirror(summary="p0 [auto] Gus / Kit")])
    assert cr.plan_actions(protected, active, set(), set()) == []


# ---------------------------------------------------------------------------
# Extra sources: subscribed feeds and soft-reflected calendars
# ---------------------------------------------------------------------------

def test_forced_level_source_reflects_everything_as_that_level():
    """A subscribed department feed or the family calendar lands at the
    configured level regardless of attendee counts."""
    accounts = [{"email": "jo.ambrose4@example.com",
                 "calendar_id": "fern.corvino51@example.com",
                 "force_level": "p1"}]
    protected = cr.scan_google(lambda e, c: [_instance(att=9)],
                               lambda e, c, s: _master(), accounts)
    info = protected["fern.corvino51@example.com/ser-lecture"]
    assert info["level"] == "p1"
    assert info["rule"] == cr.RULE_SUBSCRIBED


def test_one_offs_mirrored_only_when_enabled():
    one_off = _instance()
    del one_off["recurringEventId"]
    one_off["id"] = "single-1"
    base = {"email": "jo.ambrose4@example.com",
            "calendar_id": "fern.corvino51@example.com",
            "force_level": "p1"}
    without = cr.scan_google(lambda e, c: [one_off],
                             lambda e, c, s: _master(), [dict(base)])
    assert without == {}
    withit = cr.scan_google(lambda e, c: [one_off],
                            lambda e, c, s: _master(),
                            [dict(base, one_offs=True)])
    info = withit["fern.corvino51@example.com/single-1"]
    assert info["level"] == "p1" and info["recurrence"] == []


def test_disjoint_one_offs_stay_distinct():
    """Two seminars at the same clock time on different days share no
    occurrence and are different commitments."""
    a = {"source_key": "x", "summary": "Seminar", "recurrence": [],
         "start_local": "2026-09-01T16:00:00", "end_local": "2026-09-01T17:00:00"}
    b = dict(a, start_local="2026-09-08T16:00:00",
             end_local="2026-09-08T17:00:00")
    merged = cr.dedupe_series([{"k1": a}, {"k2": b}])
    assert len(merged) == 2


def test_drifted_cross_calendar_copy_still_dedupes():
    """A copy of a series on a second calendar can carry one cancelled
    instance its twin lacks. Exact set equality split those into a false
    duplicate mirror; any shared occurrence means same commitment."""
    outlook = {"source_key": "outlook/rf", "summary": "RF Team Weekly",
               "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TH"],
               "start_local": "2026-08-06T10:00:00",
               "end_local": "2026-08-06T11:00:00",
               "instances": [("2026-08-06T10:00:00", "2026-08-06T11:00:00"),
                             ("2026-08-20T10:00:00", "2026-08-20T11:00:00")]}
    fw_copy = {"source_key": "gmail/fw-rf", "summary": "FW: RF Team Weekly",
               "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TH"],
               "start_local": "2026-08-06T10:00:00",
               "end_local": "2026-08-06T11:00:00",
               "instances": [("2026-08-06T10:00:00", "2026-08-06T11:00:00"),
                             ("2026-08-13T10:00:00", "2026-08-13T11:00:00"),
                             ("2026-08-20T10:00:00", "2026-08-20T11:00:00")]}
    merged = cr.dedupe_series([{"outlook/rf": outlook},
                               {"gmail/fw-rf": fw_copy}])
    assert list(merged) == ["outlook/rf"], (
        "the drifted copy must fold into the Exchange-anchored series")


def test_one_off_mirrors_skip_instance_planning():
    """A single-event mirror has no occurrence set; the instance planner
    must not report a gap for it every run."""
    protected = {"feed/single-1": {
        "source_key": "feed/single-1", "summary": "Seminar",
        "level": "p1", "rule": cr.RULE_SUBSCRIBED, "recurrence": [],
        "start_local": "2026-09-01T16:00:00",
        "end_local": "2026-09-01T17:00:00",
        "instances": [("2026-09-01T16:00:00", "2026-09-01T17:00:00")],
    }}
    active, _ = cr.load_mirrors([_mirror(key="feed/single-1",
                                         summary="p1 [auto] Seminar",
                                         rrule=None)])
    active["feed/single-1"]["recurrence"] = None
    actions = cr.plan_instance_actions(protected, active, {}, set())
    assert actions == []


def test_sequential_split_series_both_survive_dedupe():
    """Outlook publishes a this-and-future edit as a degenerate
    one-instance series at the same weekday and clock time as its
    successor. Same shape, different occurrences, different commitments:
    the Aug 24 department meeting must not dedupe away against the
    Aug 31 series."""
    fall = {"source_key": "feed/fall", "summary": "CS Dept Meeting",
            "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261130T192000Z"],
            "start_local": "2026-08-31T12:20:00",
            "end_local": "2026-08-31T13:20:00",
            "instances": [("2026-08-31T12:20:00", "2026-08-31T13:20:00"),
                          ("2026-09-14T12:20:00", "2026-09-14T13:20:00")]}
    degenerate = {"source_key": "feed/aug24", "summary": "CS Dept Meeting",
                  "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20260824T182000Z"],
                  "start_local": "2026-08-24T12:20:00",
                  "end_local": "2026-08-24T13:20:00",
                  "instances": [("2026-08-24T12:20:00", "2026-08-24T13:20:00")]}
    merged = cr.dedupe_series([{"feed/fall": fall, "feed/aug24": degenerate}])
    assert len(merged) == 2, "sequential series share a slot, never a date"
    # And a true cross-calendar copy (identical occurrences) still dedupes.
    copy = dict(fall, source_key="othercal/fall2")
    merged = cr.dedupe_series([{"feed/fall": fall}, {"othercal/fall2": copy}])
    assert len(merged) == 1


# ---------------------------------------------------------------------------
# One-off meetings: every commitment has an entry
# ---------------------------------------------------------------------------

def _one_off(subject="Proposal planning", eid="AAMk-one-off", others=1,
             ext=0, **over):
    fields = {"id": eid, "start": "2026-10-08T21:30:00",
              "end": "2026-10-08T22:00:00", "att": others, "others": others,
              "others_external": ext, "my_response": "organizer", **over}
    return _view_item(subject=subject, master="", **fields)


def test_outlook_one_off_gets_its_own_mirror():
    protected = cr.scan_outlook([_one_off()], lambda sid: None)
    info = protected["outlook/AAMk-one-off"]
    assert info["recurrence"] == [] and info["instances"] == [
        ("2026-10-08T15:30:00", "2026-10-08T16:00:00")]
    assert (info["level"], info["rule"]) == ("p1", cr.RULE_ONE_OFF_1ON1)
    [create] = cr.plan_actions(protected, {}, set(), set())
    assert create["op"] == "create"


def test_mirror_levels_follow_the_meeting():
    lvl = lambda **kw: cr.scan_outlook([_one_off(**kw)], lambda sid: None)[
        "outlook/AAMk-one-off"]["level"]
    assert lvl(others=0) == "p0", "a visit or a personal block"
    assert lvl(others=1) == "p1", "a 1:1 inside the university can move"
    assert lvl(others=1, ext=1) == "p0", "an outside 1:1 takes weeks to re-land"
    assert lvl(others=6) == "p0", "a defense or committee"
    assert lvl(subject="Jules / Kit", others=2, ext=2) == "p0", \
        "two addresses for one outside person is still a 1:1"
    assert lvl(subject="Reese / Kit debrief", others=2) == "p1"
    assert lvl(subject="Ira / Zev / Kit lunch", others=2) == "p0"
    assert lvl(show_as="tentative", others=1) == "p2", "an invite not yet accepted"
    assert lvl(my_response="notResponded", others=9) == "p2"


def test_raw_attendee_count_is_the_fallback():
    item = _one_off()
    del item["others"], item["others_external"]
    item["attendees"] = 1
    assert cr.scan_outlook([item], lambda sid: None)[
        "outlook/AAMk-one-off"]["level"] == "p1"


def test_series_with_no_rrule_form_is_mirrored_per_occurrence():
    monthly = _outlook_master("Jules / Kit")
    monthly["recurrence"]["pattern"] = {"type": "relativeMonthly", "interval": 1,
                                        "daysOfWeek": ["tuesday"], "index": "second"}
    items = [_view_item(subject="Jules / Kit", master="AAMk-monthly", id="occ-1",
                        start="2026-10-13T19:00:00", end="2026-10-13T19:30:00",
                        others=1, my_response="organizer"),
             _view_item(subject="Jules / Kit", master="AAMk-monthly", id="occ-2",
                        start="2026-11-10T19:00:00", end="2026-11-10T19:30:00",
                        others=1, my_response="organizer")]
    reads = []
    protected = cr.scan_outlook(items, lambda sid: reads.append(sid) or monthly)
    assert sorted(protected) == ["outlook/occ-1", "outlook/occ-2"]
    assert all(i["recurrence"] == [] for i in protected.values())
    assert reads == ["AAMk-monthly"], "the master is read once"


def test_google_one_offs_and_responses():
    me = {"email": "jo.ambrose4@example.com", "self": True, "responseStatus": "accepted"}
    uli = {"email": "cam.iverson36@example.com"}
    item = _instance(summary="Kit <> Uli", attendees=[me, uli], id="g1")
    del item["recurringEventId"]
    acct = [{"email": "jo.ambrose4@example.com", "one_offs": True}]
    scan = lambda it: cr.scan_google(lambda e, c: [it], lambda e, c, s: None,
                                     acct, domain="@example.edu")
    info = scan(item)["jo.ambrose4@example.com/g1"]
    assert (info["level"], info["rule"]) == ("p0", cr.RULE_EXTERNAL_1ON1)
    pending = dict(item, attendees=[dict(me, responseStatus="needsAction"), uli])
    assert scan(pending)["jo.ambrose4@example.com/g1"]["level"] == "p2"
    solo = dict(item, attendees=[], summary="telehealth")
    assert scan(solo)["jo.ambrose4@example.com/g1"]["level"] == "p0"


def _one_off_mirror(end, key="outlook/AAMk-one-off"):
    return {"id": "mir-one", "summary": "p1 [auto] Proposal planning",
            "description": cr.build_description(key, cr.RULE_ONE_OFF_1ON1),
            "status": "confirmed",
            "start": {"dateTime": "2026-10-08T15:30:00-06:00"},
            "end": {"dateTime": end}}


def test_a_past_one_off_mirror_stays():
    active = {"outlook/AAMk-one-off": _one_off_mirror("2026-10-08T16:00:00-06:00")}
    assert cr.plan_actions({}, active, set(), set(),
                           now_local="2026-10-09T09:00:00") == []


def test_a_cancelled_upcoming_one_off_is_removed_quietly(monkeypatch):
    active = {"outlook/AAMk-one-off": _one_off_mirror("2026-10-08T16:00:00-06:00")}
    [remove] = cr.plan_actions({}, active, set(), set(),
                               now_local="2026-10-07T17:00:00")
    assert remove["op"] == "remove" and remove["one_off"] is True
    posts = []

    class _Fake:
        post_to_slack = staticmethod(lambda key, text, **kw: posts.append(text))
        calendar_channel_key = staticmethod(lambda: "calendar")
    monkeypatch.setitem(sys.modules, "slack_notifier", _Fake())
    cr.notify([{**remove, "status": "ok"},
               {"op": "retime", "key": "outlook/AAMk-one-off", "one_off": True,
                "status": "ok", "info": {"summary": "Proposal planning"}}])
    assert posts == [], "a meeting moved or cancelled is routine"
    cr.notify([{**remove, "status": "error", "error": "500"}])
    assert len(posts) == 1, "a failure still posts"


def test_a_hand_placed_token_covers_a_one_off():
    hand = [{"summary": "p0 Vendor visit", "status": "confirmed",
             "start": {"dateTime": "2026-10-20T09:00:00-06:00"},
             "end": {"dateTime": "2026-10-20T16:30:00-06:00"}},
            {"summary": "p2 HOLD: Uli intro", "status": "confirmed",
             "start": {"dateTime": "2026-10-14T14:15:00-06:00"},
             "end": {"dateTime": "2026-10-14T15:30:00-06:00"}},
            {"summary": "p0 [auto] something", "status": "confirmed",
             "start": {"dateTime": "2026-10-14T14:00:00-06:00"},
             "end": {"dateTime": "2026-10-14T16:00:00-06:00"}}]
    spans = cr.hand_token_spans(hand)
    assert [lvl for _, _, lvl in spans] == ["p0", "p2"], "[auto] is never a hand token"
    visit = {"start_local": "2026-10-20T09:00:00", "end_local": "2026-10-20T16:30:00",
             "level": "p0", "recurrence": []}
    uli = {"start_local": "2026-10-14T14:30:00", "end_local": "2026-10-14T15:00:00",
             "level": "p1", "recurrence": []}
    covered = cr.covered_one_offs({"o/visit": visit, "o/uli": uli}, spans)
    assert covered == {"o/visit"}, "a p2 hold does not stand in for a real 1:1"
    assert cr.plan_actions({"o/visit": dict(visit, summary="Vendor visit")}, {},
                           set(), set(), covered=covered) == []


def test_list_masters_reads_every_page():
    pages = [{"items": [{"id": "a"}], "nextPageToken": "t2"},
             {"items": [{"id": "b"}], "nextPageToken": "t3"},
             {"items": [{"id": "c"}]}]
    seen = []

    class _Req:
        def __init__(self, kw):
            self.kw = kw

        def execute(self):
            seen.append(self.kw.get("pageToken"))
            return pages[len(seen) - 1]

    class _Svc:
        def events(self):
            return self

        def list(self, **kw):
            return _Req(kw)
    assert [m["id"] for m in cr.list_masters(_Svc(), "ea-cal")] == ["a", "b", "c"]
    assert seen == [None, "t2", "t3"]


# ---------------------------------------------------------------------------
# Labels on the source, the note level, released events
# ---------------------------------------------------------------------------

def test_source_label():
    assert cr.source_label("p0 Kit watching Parker") == ("p0", "Kit watching Parker")
    assert cr.source_label("nm IROS travel") == ("p0", "IROS travel")
    assert cr.source_label("[note] Xan!") == ("note", "Xan!")
    assert cr.source_label("cleaner [note]") == ("note", "cleaner")
    assert cr.source_label("p0") == ("", "p0"), "a bare word is a title"
    assert cr.source_label("Casey swim class") == ("", "Casey swim class")


def test_a_labelled_family_event_mirrors_at_its_label():
    """The family calendar mirrors as notes (force_level), but a level the
    user wrote on the event wins."""
    acct = [{"email": "drew.prescott19@example.com", "force_level": "note", "one_offs": True}]
    items = [dict(_instance(summary="p0 Kit watching Parker"), id="t1"),
             dict(_instance(summary="Hale's Birthday Party",
                            start="2026-10-18T14:30:00-06:00",
                            end="2026-10-18T16:30:00-06:00"), id="b1")]
    for it in items:
        del it["recurringEventId"]
    scan = cr.scan_google(lambda e, c: items, lambda e, c, s: None, acct)
    parker, party = scan["drew.prescott19@example.com/t1"], scan["drew.prescott19@example.com/b1"]
    assert (parker["level"], parker["summary"]) == ("p0", "Kit watching Parker")
    assert parker["labeled"] and parker["rule"] == cr.RULE_LABELLED
    assert (party["level"], party["rule"]) == ("note", cr.RULE_SUBSCRIBED)


def test_a_label_added_later_relevels_the_mirror():
    labelled = _protected()
    labelled[LECTURE_INFO_KEY].update(level="p0", labeled=True)
    note = _mirror(summary="[note] [auto] Kit lecture")
    [relevel] = cr.plan_actions(labelled, {LECTURE_INFO_KEY: note}, set(), set())
    assert relevel["op"] == "relevel" and relevel["info"]["level"] == "p0"
    log = []
    cr.apply_actions([relevel], _FakeService(log), {"calendar_id": "ea-cal-id"})
    assert log[-1][0] == "patch" and log[-1][3]["summary"] == "p0 [auto] Kit lecture"
    # without a label, the mirror's word is the user's
    assert cr.plan_actions(_protected(), {LECTURE_INFO_KEY: note}, set(), set()) == []


def test_an_accepted_invite_relevels_the_p2_the_reconciler_set_for_it():
    """A sponsor call mirrored while the invite was unanswered got p2 by
    rule; once the user accepts, the mirror takes the answered level.
    A p2 the user wrote stands: the rule line tells the two apart."""
    accepted = _protected()
    accepted[LECTURE_INFO_KEY].update(level="p0", rule=cr.RULE_RECURRING_GROUP)
    stale = _mirror(summary="p2 [auto] Kit lecture")
    stale["description"] = cr.build_description(LECTURE_INFO_KEY, cr.RULE_UNANSWERED)
    [relevel] = cr.plan_actions(accepted, {LECTURE_INFO_KEY: stale}, set(), set())
    assert relevel["op"] == "relevel" and relevel["info"]["level"] == "p0"
    assert cr.parse_rule(relevel["description"]) == cr.RULE_RECURRING_GROUP
    assert cr.parse_source_key(relevel["description"]) == LECTURE_INFO_KEY
    log = []
    cr.apply_actions([relevel], _FakeService(log), {"calendar_id": "ea-cal-id"})
    body = log[-1][3]
    assert body["summary"] == "p0 [auto] Kit lecture" and "rule: " + cr.RULE_RECURRING_GROUP in body["description"]
    # the user's own p2 (no unanswered rule on the mirror) is never touched
    users = _mirror(summary="p2 [auto] Kit lecture")
    assert cr.plan_actions(accepted, {LECTURE_INFO_KEY: users}, set(), set()) == []
    # still unanswered: the p2 stands
    pending = _protected()
    pending[LECTURE_INFO_KEY].update(level="p2", rule=cr.RULE_UNANSWERED)
    assert cr.plan_actions(pending, {LECTURE_INFO_KEY: stale}, set(), set()) == []


def test_note_mirrors_are_mirrors():
    note = _mirror(summary="[note] [auto] RF Team Weekly")
    assert cr.is_auto_mirror(note) and cr.mirror_level(note["summary"]) == "note"
    assert cr.mirror_title("note", "RF Team Weekly") == "[note] [auto] RF Team Weekly"
    deleted_note = dict(note, status="cancelled")
    assert cr.note_tombstoned_keys([deleted_note]) == {LECTURE_INFO_KEY}
    assert cr.note_tombstoned_keys([_mirror(status="cancelled")]) == set()
    log = []
    created = cr.apply_actions(
        [{"op": "create", "key": LECTURE_INFO_KEY,
          "info": dict(_protected()[LECTURE_INFO_KEY], level="note",
                       rule=cr.RULE_RELEASED)}],
        _FakeService(log), {"calendar_id": "ea-cal-id"})
    assert created[0]["status"] == "ok"
    body = log[0][2]
    assert body["summary"] == "[note] [auto] Kit lecture"
    assert "rule: released-to-note" in body["description"]


def test_a_word_note_is_not_a_hand_token():
    spans = cr.hand_token_spans([{"summary": "note to self", "status": "confirmed",
                                  "start": {"dateTime": "2026-10-14T09:00:00-06:00"},
                                  "end": {"dateTime": "2026-10-14T10:00:00-06:00"}}])
    assert spans == []
