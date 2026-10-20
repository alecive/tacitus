"""Optional attendees: the Graph payload on create and on reschedule (an
attendee already optional stays optional, rooms stay booked), the CLI flag,
and the Create and Reschedule cards. No network."""
import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import graph_client as gc  # noqa: E402
import graph_actions as ga  # noqa: E402

START, END = _dt.datetime(2026, 10, 14, 10), _dt.datetime(2026, 10, 14, 11)


def _types(body):
    return {a["emailAddress"]["address"]: a["type"] for a in body["attendees"]}


def test_attendee_list_dedupes_and_optional_wins():
    out = gc.attendee_list(["kit.bancroft59@example.edu", "harper.juniper70@example.edu", "kit.bancroft59@example.edu"], ["harper.juniper70@example.edu", "yael.marchetti63@example.edu"])
    assert [(a["emailAddress"]["address"], a["type"]) for a in out] == [
        ("kit.bancroft59@example.edu", "required"), ("harper.juniper70@example.edu", "optional"), ("yael.marchetti63@example.edu", "optional")]
    assert gc.attendee_list(None, None) == []


def test_create_event_marks_optional_attendees_and_a_hold_refuses_them():
    seen = {}
    with patch.object(gc, "graph_post", lambda path, body: seen.update(body=body) or {"id": "E1"}), \
         patch.object(gc, "_get_user_timezone", return_value="America/Denver"):
        gc.create_event("Sync", START, END, attendees=["morgan.vasquez41@example.edu"],
                        optional_attendees=["finley.kestrel1@example.edu"],
                        confirm_token=gc.issue_inprocess_approval("event", "Sync"))
    assert _types(seen["body"]) == {"morgan.vasquez41@example.edu": "required", "finley.kestrel1@example.edu": "optional"}
    r = gc.create_event("HOLD: x", START, END, optional_attendees=["eli.valentijn76@example.edu"], hold=True,
                        confirm_token=gc.issue_inprocess_approval("event", "HOLD: x"))
    assert r["status"] == "blocked"


def test_reschedule_keeps_optional_people_optional_and_rooms_booked():
    current = {"attendees": [
        {"emailAddress": {"address": "morgan.vasquez41@example.edu"}, "type": "required"},
        {"emailAddress": {"address": "finley.kestrel1@example.edu"}, "type": "optional"},
        {"emailAddress": {"address": "wynn.tremaine45@example.edu"}, "type": "resource"}]}
    sent = {}
    with patch.object(gc, "get_event", lambda eid: current), \
         patch.object(gc, "graph_patch", lambda path, body: sent.update(body=body) or {"id": "E1"}), \
         patch.object(gc, "_get_user_timezone", return_value="America/Denver"):
        gc.update_event("E1", attendees=["morgan.vasquez41@example.edu", "finley.kestrel1@example.edu", "emerson.yamamura4@example.edu"],
                        confirm_token=gc.issue_inprocess_approval("update_event", "E1"))
        assert _types(sent["body"]) == {"morgan.vasquez41@example.edu": "required", "finley.kestrel1@example.edu": "optional",
                                        "emerson.yamamura4@example.edu": "required", "wynn.tremaine45@example.edu": "resource"}
        # optional_attendees alone changes types on the current list
        gc.update_event("E1", optional_attendees=["morgan.vasquez41@example.edu"],
                        confirm_token=gc.issue_inprocess_approval("update_event", "E1"))
        assert _types(sent["body"])["morgan.vasquez41@example.edu"] == "optional"
        assert _types(sent["body"])["wynn.tremaine45@example.edu"] == "resource"
        # a pure retime leaves the attendees alone
        gc.update_event("E1", start=START, end=END,
                        confirm_token=gc.issue_inprocess_approval("update_event", "E1"))
        assert "attendees" not in sent["body"]


def test_the_cli_passes_the_flag(capsys):
    seen = {}
    args = argparse.Namespace(
        subject="Sync", start=START.isoformat(), end=END.isoformat(), attendees="morgan.vasquez41@example.edu",
        optional_attendees="finley.kestrel1@example.edu, ", body="", location="", online=False,
        confirm_token="tok", hold=False, repeat=None, repeat_days=None, repeat_interval=None,
        repeat_until=None, repeat_count=None)
    with patch.object(ga, "_validate_and_consume_token", lambda *a: {"ok": True}), \
         patch.object(ga, "_recurrence_from_args", lambda *a: None), \
         patch.object(ga.graph, "create_event", lambda **kw: seen.update(kw) or {"id": "E1"}):
        ga.cmd_create_event(args)
    assert seen["optional_attendees"] == ["finley.kestrel1@example.edu"]
    assert json.loads(capsys.readouterr().out)["status"] == "event_created"
    src = (ROOT / "preprocessor" / "graph_actions.py").read_text()
    assert src.count('"--optional-attendees"') == 2               # create-event and update-event


def _bot_fn(name):
    src = (ROOT / "ea-chat-bot.py").read_text()
    start = src.index(f"\ndef {name}(")
    ends = [i for i in (src.find("\ndef ", start + 1), src.find("\n@", start + 1)) if i > 0]
    return src[start:min(ends)]


def test_the_bot_carries_and_shows_optional_attendees():
    import types
    ns = {}
    exec(_bot_fn("_csv"), ns)
    exec(_bot_fn("_is_hold"), ns)
    assert ns["_is_hold"]({"hold": True, "attendees": ""}) is True
    assert ns["_is_hold"]({"hold": True, "attendees": "", "optional_attendees": ["eli.valentijn76@example.edu"]}) is False

    import dataclasses

    import chat
    src = (ROOT / "ea-chat-bot.py").read_text()
    start = src.index("# ── Confirmation cards")
    ns.update(json=json, _chat=chat, dataclasses=dataclasses, FRONTEND=types.SimpleNamespace())
    exec(src[start:src.index("# ── End confirmation cards", start)], ns)
    value = json.dumps({"payload_ref": "k"})
    card = ns["_event_card"]("C", "T", "Sync", "s", "e", "morgan.vasquez41@example.edu", "", "",
                             False, value, optional_attendees="finley.kestrel1@example.edu")
    assert [(f.label, f.value) for f in card.fields][2:4] == [
        ("Attendees", "morgan.vasquez41@example.edu"), ("Optional", "finley.kestrel1@example.edu")]
    assert "*Optional:* finley.kestrel1@example.edu" in card.fallback_text
    # A reschedule that makes someone optional shows them as optional in
    # the list after the change, from the owner-side read of the event.
    read = {"subject": "Sync", "when": "s to e", "is_organizer": True, "fingerprint": "f",
            "attendees": [{"name": "", "address": "morgan.vasquez41@example.edu",
                           "type": "required"}]}
    upd = ns["_event_update_card"]("C", "T", read, {
        "subject": "Sync", "when": "s2 to e2", "attendees": "morgan.vasquez41@example.edu",
        "optional_attendees": "finley.kestrel1@example.edu"}, value)
    text = chat.card_text.strip_inert("\n".join(upd.lines))
    assert "finley.kestrel1@example.edu (optional)" in text
    assert "*Added (1):* finley.kestrel1@example.edu (optional)" in text
    for fn in ("_execute_event_create", "_execute_event_update"):
        assert '"--optional-attendees"' in _bot_fn(fn), fn
