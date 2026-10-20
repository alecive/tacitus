"""The canned calendar (EA_CANNED_CALENDAR): the fixture loader, its date
and weekly expansion, and the three reads the engine routes through it
(the calendars, the EA-Managed rules, attendee free/busy). Hermetic: no
Graph, Google or network call; a scratch EA_DIR with no auth directory.

    python3 -m pytest tests/test_scheduling_engine_canned.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

_SCRATCH = Path(tempfile.mkdtemp(prefix="ea-test-canned-"))
(_SCRATCH / "config").mkdir()
(_SCRATCH / "config" / "user-profile.json").write_text(
    json.dumps({"name": "Kit Example", "email": "owner@example.edu", "timezone": "America/Denver"}))
(_SCRATCH / "config" / "scheduling-preferences.json").write_text(json.dumps({"timezone": "America/Denver"}))
os.environ["EA_DIR"] = str(_SCRATCH)
os.environ.pop("EA_CANNED_CALENDAR", None)

import scheduling_engine as se  # noqa: E402
import sched_agent as sa  # noqa: E402

FIXTURE = REPO / "tests" / "release" / "fixtures" / "calendar-canned.json"
TODAY = date(2026, 10, 14)   # a Wednesday


@pytest.fixture
def canned(monkeypatch):
    monkeypatch.setenv("EA_CANNED_CALENDAR", str(FIXTURE))
    se._CANNED_CACHE.clear()
    yield FIXTURE
    se._CANNED_CACHE.clear()


# ── the loader ─────────────────────────────────────────────────────────

def test_dates_expand_like_the_release_runner():
    out = se._canned_dates({"a": "{{+7d}}T10:00:00", "b": ["{{-1d}}", "{{+2d:%A}}"], "n": 3}, TODAY)
    assert out == {"a": "2026-10-21T10:00:00", "b": ["2026-10-13", "Friday"], "n": 3}


def test_loader_expands_weekly_rules_and_stamps_wall_clock_times():
    cal = se.load_canned_calendar(FIXTURE, TODAY)
    assert cal["timezone"] == "America/Denver"
    wh = [e for e in cal["ea_managed"] if e["subject"] == "wh working hours"]
    # one instance per weekday over the horizon, none on a weekend
    days = {date.fromisoformat(e["start"]["dateTime"][:10]) for e in wh}
    assert days and all(d.weekday() < 5 for d in days)
    assert TODAY in days and TODAY + timedelta(days=14) in days and TODAY + timedelta(days=3) not in days
    one = next(e for e in wh if e["start"]["dateTime"].startswith(TODAY.isoformat()))
    assert one["start"]["dateTime"] == "2026-10-14T09:00:00-06:00"
    assert one["end"]["dateTime"] == "2026-10-14T17:00:00-06:00"
    assert one["source_calendar"] == "ea-managed" and one["recurring_event_id"] == "wh-working-hours"
    # a weekly Outlook series lands in the Outlook list with its series id
    group = [e for e in cal["outlook"] if e["subject"] == "Group meeting"]
    assert group and all(date.fromisoformat(e["start"]["dateTime"][:10]).weekday() == 0 for e in group)
    assert group[0]["seriesMasterId"] == "series-group-meeting"
    # a naive time is stamped in the fixture zone; an explicit zone is kept
    lecture = next(e for e in cal["outlook"] if e["id"] == "canned-o-lecture")
    assert lecture["start"]["dateTime"] == "2026-10-15T13:00:00-06:00"
    retreat = next(e for e in cal["outlook"] if e["id"] == "canned-o-retreat")
    assert retreat["isAllDay"] and retreat["start"]["dateTime"].startswith("2026-10-23T00:00:00+00:00")
    assert se._all_day_span(retreat) == (date(2026, 10, 23), date(2026, 10, 23))
    # Google events carry the google shape, EA-Managed ones the merged tag
    dentist = next(e for e in cal["google"] if e["id"] == "canned-g-dentist")
    assert dentist["source_type"] == "google" and dentist["source_calendar"] == "personal@gmail.com"
    hold = next(e for e in cal["ea_managed"] if e["id"] == "canned-ea-hold-preread")
    assert hold["ea_managed_hold"] and hold["source_calendar"] == "ea-managed"
    # attendee blocks are stamped too, keyed by lower-cased address
    bo = cal["attendees"]["indra.isenberg18@example.edu"]
    assert bo[0] == {"start": "2026-10-15T10:00:00-06:00", "end": "2026-10-15T11:00:00-06:00", "state": "busy"}
    assert cal["attendees"]["wren.fairbanks84@example.edu"] == []


def test_loader_refuses_a_non_object(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[]")
    with pytest.raises(ValueError):
        se.load_canned_calendar(p, TODAY)


def test_no_env_means_live_reads(monkeypatch):
    monkeypatch.delenv("EA_CANNED_CALENDAR", raising=False)
    se._CANNED_CACHE.clear()
    assert se.canned_calendar_path() is None and se._canned() is None
    assert se._schedule_source() == "Graph getSchedule"


# ── the reads ──────────────────────────────────────────────────────────

def test_fetch_all_events_reads_the_fixture_in_the_window(canned):
    outlook, google = se._fetch_all_events(14)
    now = datetime.now(timezone.utc)
    assert outlook and google
    for e in outlook + google:
        s = se._parse_dt(e["start"]["dateTime"])
        assert s <= now + timedelta(days=14, hours=1)
    assert {e["source_calendar"] for e in google} >= {"ea-managed", "personal@gmail.com"}
    assert all(e.get("ea_managed_hold") for e in google if e["source_calendar"] == "ea-managed")
    assert se.calendar_fetch_errors() == []
    # the busy view: the rules are constraints, the [note] is nothing, the
    # lecture and the holds are busy time
    busy = {e["subject"] for _s, _t, e in se._build_busy_intervals(outlook + google)}
    assert "CSCI 4100 lecture" in busy and "EA Hold: call with the vendor" in busy
    assert "Hold: committee pre-read" in busy
    assert "wh working hours" not in busy and "[note] house cleaner" not in busy


def test_ea_managed_rules_come_from_the_fixture(canned):
    today = datetime.now(se.USER_TZ).date()
    tc = se._load_token_constraints(today, today + timedelta(days=13))
    assert tc["has_wh"]
    tokens = {i["token"] for i in tc["instances"]}
    assert tokens == {"wh", "nm", "p1"}
    wh_days = {i["date"] for i in tc["instances"] if i["token"] == "wh"}
    assert all(d.weekday() < 5 for d in wh_days) and len(wh_days) == 10
    lunch = next(i for i in tc["instances"] if i["token"] == "nm")
    assert (lunch["start_min"], lunch["end_min"]) == (12 * 60, 13 * 60)


def test_schedule_detail_comes_from_attendee_blocks(canned):
    today = datetime.now(se.USER_TZ).date()
    day = today + timedelta(days=1)
    start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=se.USER_TZ)
    end = start + timedelta(hours=2)
    out = se._schedule_detail(["indra.isenberg18@example.edu", "Nobody@example.edu",
                               "wren.fairbanks84@example.edu"], start, end, 15)
    # 09:30-11:30 in quarter hours: two free, four busy, two free
    bo = out["indra.isenberg18@example.edu"]
    assert bo["view"] == "00222200" and not bo["error"]
    assert out["Nobody@example.edu"] == {"view": "", "error": "not in the canned calendar"}
    assert out["wren.fairbanks84@example.edu"]["view"] == "00000000"
    assert se._attendee_status(bo["view"]) == "partial"
    assert se._schedule_source() == "canned calendar"


def test_cache_follows_the_file(canned, tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"timezone": "UTC", "outlook": [{"id": "a", "subject": "A",
                             "start": {"dateTime": "{{+1d}}T10:00:00"}, "end": {"dateTime": "{{+1d}}T11:00:00"}}]}))
    monkeypatch.setenv("EA_CANNED_CALENDAR", str(p))
    se._CANNED_CACHE.clear()
    assert [e["id"] for e in se._canned()["outlook"]] == ["a"]
    p.write_text(json.dumps({"timezone": "UTC", "outlook": []}))
    os.utime(p, ns=(2, 2))
    assert se._canned()["outlook"] == []


# ── the CLI on a scratch EA_DIR with no auth directory ─────────────────

def _cli(*argv):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GRAPH_")}
    env.update(EA_DIR=str(_SCRATCH), EA_CANNED_CALENDAR=str(FIXTURE))
    p = subprocess.run([sys.executable, str(REPO / "preprocessor" / "scheduling_engine.py"), *argv],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr[-600:]
    return json.loads(p.stdout)


def test_the_three_verbs_run_without_a_token():
    # the Google client makes the auth directory at import; it holds no token
    assert not list((_SCRATCH / "auth").glob("*")) if (_SCRATCH / "auth").exists() else True
    out = _cli("propose-meeting", "--attendees",
               json.dumps([{"email": "indra.isenberg18@example.edu", "name": "Bo", "type": "external"}]),
               "--duration", "30", "--days-ahead", "10")
    assert out["status"] == "ok" and len(out["options"]) >= 2
    assert out["calendar_fetch_errors"] == []
    for o in out["options"]:
        local = se._parse_dt(o["start"]).astimezone(se.USER_TZ)
        assert local.weekday() < 5 and 9 <= local.hour < 17
    assert "Department retreat" in {a["subject"] for a in out["all_day_events"]}
    today = datetime.now(se.USER_TZ).date()
    day2 = (today + timedelta(days=2)).isoformat()
    v = _cli("validate-draft", "--times", json.dumps([f"{day2}T14:30:00"]), "--duration", "30")
    assert v["valid"] is False
    assert any(i.get("type") == "conflict" and "1:1 with Sage" in str(i) for i in v["issues"])
    day1 = (today + timedelta(days=1)).isoformat()
    c = _cli("check-attendees", "--attendees", "indra.isenberg18@example.edu,nobody@example.edu",
             "--start", f"{day1}T10:00:00", "--end", f"{day1}T11:00:00")
    assert c["attendees"]["indra.isenberg18@example.edu"]["status"] == "busy"
    assert c["attendees"]["indra.isenberg18@example.edu"]["source"] == "canned calendar"
    assert c["attendees"]["nobody@example.edu"]["status"] == "unknown"


# ── the agent runs the tool where it is called ─────────────────────────

def test_resident_cmd_runs_the_tool_directly_in_canned_mode(monkeypatch):
    monkeypatch.setenv("EA_CANNED_CALENDAR", str(FIXTURE))
    cmd = sa._resident_cmd("scheduling_engine", ["propose-meeting", "--duration", "30"], "r1")
    assert cmd[0] == "env" and "sudo" not in cmd
    assert "EA_RUN_ID=shadow-r1" in cmd and f"EA_CANNED_CALENDAR={FIXTURE}" in cmd
    assert cmd[cmd.index(sys.executable) + 1] == str(REPO / "preprocessor" / "scheduling_engine.py")
    assert cmd[-3:] == ["propose-meeting", "--duration", "30"]
    live = sa._resident_cmd("scheduling_engine", ["log-decision"], "r2", live=True)
    assert "EA_RUN_ID=r2" in live
    monkeypatch.delenv("EA_CANNED_CALENDAR")
    cmd = sa._resident_cmd("scheduling_engine", ["propose-meeting"], "r1")
    assert cmd[:4] == ["sudo", "-n", "-u", "ea-bot"]
