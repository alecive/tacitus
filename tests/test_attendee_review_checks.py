"""The review-time checks of other people's calendars: the meeting length a
draft states, the per-recipient notes from check-attendees on the Send
card's preflight, and the warning line on the Create card. The bot's
functions run from its source with stubs; no network."""
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "ea-chat-bot.py").read_text()

# The internal domains come from the profile (preprocessor/institution.py).
_EA = Path(tempfile.mkdtemp(prefix="ea-test-attendee-review-"))
(_EA / "config").mkdir()
(_EA / "config" / "user-profile.json").write_text(json.dumps({"email": "owner@example.edu"}))
os.environ["EA_DIR"] = str(_EA)


def _fn(name):
    start = SRC.index(f"\ndef {name}(")
    ends = [i for i in (SRC.find("\ndef ", start + 1), SRC.find("\n@", start + 1)) if i > 0]
    return SRC[start:min(ends)]


def _ns(**extra):
    start = SRC.index("_MONTHS = {m: i for i, m in enumerate(")
    end = SRC.index("def _proposed_duration_minutes(")
    ns = {"re": re, "json": json, "datetime": datetime, "timedelta": timedelta,
          "SCRIPT_DIR": ROOT, "_USER_EMAIL": "owner@example.edu",
          "log": types.SimpleNamespace(warning=lambda *a, **k: None), **extra}
    exec(compile(SRC[start:end], "bot[block]", "exec"), ns)
    for name in ("_proposed_duration_minutes", "_attendee_check_lines", "_event_attendee_warning"):
        exec(compile(_fn(name), f"bot[{name}]", "exec"), ns)
    return ns


def test_the_stated_meeting_length():
    d = _ns()["_proposed_duration_minutes"]
    assert d("How about Mon Nov 2, 9-10 AM?") == 60
    assert d("Does Tue Oct 21 1:00-1:30 PM work?") == 30
    assert d("Thu Oct 22 11 to 1 pm works") == 120
    assert d("Does Thursday at 3 PM work?") == 30
    assert d("Mon Nov 2 9-10 AM works.\n\nFrom: Pat\nSent: x\nTo: y\n2-5 PM") == 60


def _check_output(statuses):
    return {"status": "ok", "attendees": statuses}


def test_recipient_notes_name_who_is_not_free(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = _check_output({
            "alice@example.edu": {
                "status": "partial",
                "not_free": [{"state": "tentative", "start": "2026-11-02T09:00:00-07:00",
                              "end": "2026-11-02T09:30:00-07:00"},
                             {"state": "busy", "start": "2026-11-02T09:30:00-07:00",
                              "end": "2026-11-02T10:00:00-07:00"}]},
            "bob@example.edu": {"status": "free", "not_free": []},
            "carol@example.edu": {"status": "unknown", "not_free": []}})
        return types.SimpleNamespace(stdout="Using token\n" + json.dumps(out), stderr="", returncode=0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    ns = _ns()
    got = ns["_attendee_check_lines"](
        ["2026-11-02T09:00:00-07:00"], 60,
        "alice@example.edu, bob@example.edu,carol@example.edu, hollis.isenberg21@example.org, "
        "owner@example.edu")
    assert got == [("2026-11-02T09:00:00-07:00", [
        "*alice@example.edu* tentative 9:00-9:30 AM, busy 9:30-10:00 AM",
        "carol@example.edu calendar not readable"])]
    cmd = calls[0]
    assert cmd[2] == "check-attendees"
    assert cmd[cmd.index("--attendees") + 1] == "alice@example.edu,bob@example.edu,carol@example.edu"
    assert cmd[cmd.index("--end") + 1] == "2026-11-02T10:00:00-07:00"
    assert ns["_attendee_check_lines"](["2026-11-02T09:00:00-07:00"], 60, "hollis.isenberg21@example.org") == []


def test_the_create_card_names_busy_invitees(monkeypatch):
    seen = {}

    def lines(times, minutes, people):
        seen.update(times=times, minutes=minutes, people=people)
        return [(times[0], ["*alice@example.edu* busy 9:30-10:00 AM"])]
    ns = _ns()
    ns["_attendee_check_lines"] = lines
    w = ns["_event_attendee_warning"]("2026-11-02T09:00:00", "2026-11-02T10:00:00",
                                      "alice@example.edu", ["dave@example.edu"])
    assert w == ":warning: *Invitees not free then:* *alice@example.edu* busy 9:30-10:00 AM"
    assert seen["minutes"] == 60 and seen["times"] == ["2026-11-02T09:00:00-07:00"]
    assert seen["people"] == "alice@example.edu,dave@example.edu"
    ns["_attendee_check_lines"] = lambda *a: []
    assert ns["_event_attendee_warning"]("2026-11-02T09:00:00", "2026-11-02T10:00:00", "", "") == ""
    card = _fn("_event_card")
    assert "lines = [warning] if warning else []" in card and "fallback.append(warning)" in card
    assert "_event_attendee_warning(\n                           start, end, attendees" in _fn("_post_event_confirmation")
    pre = _fn("_scheduling_preflight")
    assert '"--duration", str(duration)' in pre and "_attendee_check_lines(times, duration, recipients)" in pre


def test_a_conflicting_event_name_cannot_forge_a_card_line(monkeypatch):
    """The name of the event a time conflicts with comes from the calendar
    (an invite anyone can send): an inert span on the preflight line."""
    sys.path.insert(0, str(ROOT / "preprocessor"))
    from preprocessor import chat
    out = {"issues": [{"proposed_time": "2026-11-02T09:00:00-07:00", "type": "conflict",
                       "conflict_with": "Lunch\n*To:* boss@example.org <!channel>"},
                      {"proposed_time": "not a time\n*CC:* x", "type": "preference",
                       "detail": "outside hours\n*BCC:* y"}]}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(
        stdout=json.dumps(out), stderr="", returncode=0))
    ns = _ns(_chat=chat)
    exec(compile(_fn("_scheduling_preflight"), "bot[_scheduling_preflight]", "exec"), ns)
    ns["_extract_proposed_times"] = lambda body: ["2026-11-02T09:00:00-07:00"]
    ns["_attendee_check_lines"] = lambda *a: []
    warn = ns["_scheduling_preflight"]("Mon Nov 2 at 9 AM?", "")
    assert "conflicts with" in warn
    for forged in ("\n*To:*", "\n*CC:*", "\n*BCC:*"):
        assert forged not in warn, forged
    assert chat.card_text.INERT_OPEN + "Lunch *To:* boss@example.org <!channel>" in warn
