#!/usr/bin/env python3
"""A staged event must survive its own agenda.

Slack caps a button `value` at 2000 characters and rejects the whole
chat_postMessage above it. An event payload carried inside that value
raises at post time, AFTER the staging file has been claimed, for any
meeting with a real agenda in `body`, which CLAUDE.md's pending-event.json
schema is explicit about. Through the sweeper the exception lands in a
log.debug the live INFO level suppresses, so the event silently never
happens and an array fan-out loses every event after it.

The payload lives server-side and the button carries only its key.
Buttons already in flight with an inline payload keep working.
"""

import json
import os
import sys
import tempfile
import threading
import time
import traceback
import types
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import atomic_io as _atomic  # noqa: E402

import chat as _chat  # noqa: E402
from chat.memory_frontend import MemoryFrontend  # noqa: E402
from chat.slack_frontend import render_card  # noqa: E402

BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()

AGENDA = ("Agenda item that runs long. " * 120)          # ~3.2 KB


def _exec_block(start_marker: str, end_marker: str, ns: dict) -> dict:
    start = BOT_SRC.index(start_marker)
    end = BOT_SRC.index(end_marker, start)
    exec(compile(BOT_SRC[start:end], "ea-chat-bot.py[block]", "exec"), ns)
    return ns


def _fn_src(name: str) -> str:
    """One top-level function's source. Stops at the next def OR the next
    @app.action decorator, so a handler does not drag the next one's
    decorator line in with it."""
    start = BOT_SRC.index(f"\ndef {name}(")
    ends = [BOT_SRC.index("\ndef ", start + 1)]
    nxt = BOT_SRC.find("\n" + "@app.action(", start + 1)
    if nxt > 0:
        ends.append(nxt)
    return BOT_SRC[start:min(ends)]


def _ns(tmp: Path):
    """The posters and handlers against a MemoryFrontend. The second value
    returned is the frontend: every card lands in its .cards."""
    updates = []
    frontend = MemoryFrontend()
    ns = {
        "json": json, "os": os, "time": time, "uuid": uuid, "_atomic": _atomic,
        "threading": threading, "Path": Path, "EA_DIR": tmp,
        # the bot keeps these stores in its private dir; here, the scratch dir
        "_private_state": lambda name: tmp / "state" / name,
        "log": types.SimpleNamespace(info=lambda *a, **k: None,
                                     debug=lambda *a, **k: None,
                                     warning=lambda *a, **k: None,
                                     error=lambda *a, **k: None),
        "_chat": _chat,
        "FRONTEND": frontend,
        "_CARDS": _chat.CardRegistry(tmp / "private" / "chat-cards.json"),
        "_resolve_dm_channel": lambda: "DM1",
        # The shared delivery helper's side effects (tally, thread pointer)
        # are not under test here; the posted cards are.
        "_note_run_post": lambda kind: None,
        "_post_dm_breadcrumb": lambda *a, **k: None,
        "_resolve_online_location": lambda loc, online: loc,
        # The invitee check reads live calendars; not under test here.
        "_event_attendee_warning": lambda *a, **k: "",
        # The owner-side event read and the join block (test_event_card_gate
        # covers them); here, a fixed read and the body as staged.
        "_invite_body_as_sent": lambda body, online: body,
        "_read_calendar_event": lambda event_id, google="": {
            "subject": "Group Meeting", "when": "S to E", "organizer": "you",
            "is_organizer": True, "attendees": [], "location": "", "series": "",
            "cancelled": False, "fingerprint": "f" * 64},
        "_event_span_text": lambda s, e, *a: f"{s} to {e}",
        # The staged times as written (test_event_card_gate covers the
        # conversion to the user's zone).
        "_event_time_local": lambda v: v,
        "_event_time_text": lambda v, *a: v,
        "_repeat_summary": lambda rep: "weekly",
        "_verify_button_user": lambda body: True,
        "_reject_if_stale_confirmation": lambda body, kind: False,
        "_replace_buttons_with_status":
            lambda ch, ts, txt: updates.append({"channel": ch, "ts": ts,
                                                "text": txt}),
    }
    _exec_block("# ── Confirmation cards", "# ── End confirmation cards", ns)
    _exec_block("# ── Staged button payloads",
                "def _post_event_confirmation", ns)
    for fn in ("_clicked_card", "_card_value", "_owner_card_channel", "_deliver_confirmation",
               "_post_event_confirmation",
               "_post_event_update_confirmation", "_post_event_delete_confirmation"):
        exec(compile(_fn_src(fn), "ea-chat-bot.py[fn]", "exec"), ns)
    return ns, frontend, updates


def _buttons(frontend):
    """(action_id -> value string) for the last posted card, as Slack
    renders it."""
    _, blocks = render_card(frontend.cards[-1]["card"])
    actions = [b for b in blocks if b["type"] == "actions"][0]
    return {e["action_id"]: e["value"] for e in actions["elements"]}


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

def test_a_big_payload_round_trips_through_a_small_button_value():
    with tempfile.TemporaryDirectory() as td:
        ns, _, _ = _ns(Path(td))
        payload = {"subject": "Team kickoff", "body": AGENDA,
                   "attendees": "a@b.c", "start": "S", "end": "E"}
        value = ns["_staged_button_value"]({"thread_ts": "T1",
                                            "ea_channel": "C1"}, payload)
        assert len(value) < ns["_SLACK_BUTTON_VALUE_MAX"], len(value)
        assert AGENDA not in value
        back = ns["_staged_button_payload"](json.loads(value))
        assert back == payload


def test_a_key_is_consumed_once():
    with tempfile.TemporaryDirectory() as td:
        ns, _, _ = _ns(Path(td))
        value = json.loads(ns["_staged_button_value"]({}, {"subject": "one"}))
        assert ns["_staged_button_payload"](value)["subject"] == "one"
        assert ns["_staged_button_payload"](value) is None, (
            "a consumed key must not create the event twice")


def test_a_peek_does_not_consume():
    with tempfile.TemporaryDirectory() as td:
        ns, _, _ = _ns(Path(td))
        value = json.loads(ns["_staged_button_value"]({}, {"subject": "one"}))
        assert ns["_staged_button_payload"](value, consume=False)
        assert ns["_staged_button_payload"](value) is not None


def test_an_inline_payload_from_a_button_in_flight_still_works():
    """Backward compatibility for one deploy window."""
    with tempfile.TemporaryDirectory() as td:
        ns, _, _ = _ns(Path(td))
        legacy = {"thread_ts": "T1", "ea_channel": "C1",
                  "event": {"subject": "old shape"}}
        assert ns["_staged_button_payload"](legacy) == {"subject": "old shape"}


def test_a_missing_key_reads_as_gone_not_as_empty():
    with tempfile.TemporaryDirectory() as td:
        ns, _, _ = _ns(Path(td))
        assert ns["_staged_button_payload"]({"payload_ref": "deadbeef"}) is None


def test_stale_entries_are_swept_on_write():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ns, _, _ = _ns(tmp)
        old = json.loads(ns["_staged_button_value"]({}, {"subject": "old"}))
        store = tmp / "state" / "button-payloads.json"
        data = json.loads(store.read_text())
        data[old["payload_ref"]]["created_at"] = time.time() - 30 * 24 * 3600
        store.write_text(json.dumps(data))
        ns["_staged_button_value"]({}, {"subject": "new"})      # triggers a save
        assert old["payload_ref"] not in json.loads(store.read_text())


def test_the_store_is_dropped_on_cancel():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ns, _, _ = _ns(tmp)
        value = json.loads(ns["_staged_button_value"]({}, {"subject": "x"}))
        ns["_drop_button_payload"](value["payload_ref"])
        assert json.loads((tmp / "state" / "button-payloads.json").read_text()) == {}


def test_an_unwritable_store_still_posts_a_valid_button():
    """Fallback: inline, minus the body, rather than a rejected message."""
    with tempfile.TemporaryDirectory() as td:
        ns, _, _ = _ns(Path(td))
        ns["_stash_button_payload"] = lambda payload: ""
        value = ns["_staged_button_value"](
            {"thread_ts": "T1"}, {"subject": "Team", "body": AGENDA})
        assert len(value) < ns["_SLACK_BUTTON_VALUE_MAX"], len(value)
        assert json.loads(value)["event"]["subject"] == "Team"


# ---------------------------------------------------------------------------
# the posters
# ---------------------------------------------------------------------------

def test_create_confirmation_posts_under_the_slack_cap():
    with tempfile.TemporaryDirectory() as td:
        ns, posted, _ = _ns(Path(td))
        ns["_post_event_confirmation"](
            "C1", "T1", subject="Team kickoff", start="S", end="E",
            attendees="ash.iverson87@example.com", location="Zoom", body=AGENDA)
        values = _buttons(posted)
        for action_id, value in values.items():
            assert len(value) <= ns["_SLACK_BUTTON_VALUE_MAX"], action_id
        event = ns["_staged_button_payload"](
            json.loads(values["approve_event"]), consume=False)
        assert event["body"] == AGENDA, "the agenda must survive server-side"


def test_update_and_delete_confirmations_use_the_store_too():
    with tempfile.TemporaryDirectory() as td:
        ns, posted, _ = _ns(Path(td))
        ns["_post_event_update_confirmation"](
            "C1", "T1", event_id="EV1", subject="Group Meeting",
            new_start="S", new_end="E", attendees="a@b.c" * 200)
        vals = _buttons(posted)
        assert len(vals["approve_event_update"]) <= ns["_SLACK_BUTTON_VALUE_MAX"]
        ev = ns["_staged_button_payload"](
            json.loads(vals["approve_event_update"]), consume=False)
        assert ev["event_id"] == "EV1"

        ns["_post_event_delete_confirmation"](
            "C1", "T1", event_id="EV2", subject="Group Meeting",
            start="S", end="E")
        vals = _buttons(posted)
        ev = ns["_staged_button_payload"](
            json.loads(vals["approve_event_delete"]), consume=False)
        assert ev["event_id"] == "EV2"


def test_the_cancel_button_carries_the_same_key():
    with tempfile.TemporaryDirectory() as td:
        ns, posted, _ = _ns(Path(td))
        ns["_post_event_confirmation"]("C1", "T1", subject="Team", start="S",
                                       end="E", attendees="", body=AGENDA)
        vals = _buttons(posted)
        approve = json.loads(vals["approve_event"])
        cancel = json.loads(vals["cancel_event"])
        assert cancel["payload_ref"] == approve["payload_ref"], (
            "cancel must be able to release the stored payload")


# ---------------------------------------------------------------------------
# the click
# ---------------------------------------------------------------------------

def _handler_ns(tmp: Path):
    ns, posted, updates = _ns(tmp)
    created = []
    ns["_execute_event_create"] = lambda *a, **k: created.append((a, k))
    ns["threading"] = types.SimpleNamespace(
        Thread=lambda target=None, daemon=None: types.SimpleNamespace(
            start=target))
    exec(compile(_fn_src("handle_approve_event"), "ea-chat-bot.py[fn]",
                 "exec"), ns)
    exec(compile(_fn_src("handle_cancel_event"), "ea-chat-bot.py[fn]",
                 "exec"), ns)
    return ns, posted, updates, created


def _click(value: str, action_id: str = "approve_event") -> "_chat.ActionEvent":
    """A click on the card in DM1 as the frontend dispatches it: the
    button value parsed from its JSON string."""
    return _chat.ActionEvent(action_id, "U1", _chat.MessageRef("DM1", "111.1", "111.1"),
                             json.loads(value))


def test_a_click_recovers_the_full_payload():
    with tempfile.TemporaryDirectory() as td:
        ns, posted, updates, created = _handler_ns(Path(td))
        ns["_post_event_confirmation"]("C1", "T1", subject="Team", start="S",
                                       end="E", attendees="x@y.z", body=AGENDA)
        value = _buttons(posted)["approve_event"]
        ns["handle_approve_event"](_click(value))
        assert created, "the Create button must reach _execute_event_create"
        event = created[0][0][3]
        assert event["body"] == AGENDA
        assert event["subject"] == "Team"


def test_an_expired_key_says_so_instead_of_creating_a_half_event():
    with tempfile.TemporaryDirectory() as td:
        ns, posted, updates, created = _handler_ns(Path(td))
        ns["handle_approve_event"](
            _click(json.dumps({"thread_ts": "T1", "payload_ref": "gone"})))
        assert created == []
        assert updates and "no longer have the details" in updates[-1]["text"]


def test_cancel_releases_the_stored_payload():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ns, posted, updates, _ = _handler_ns(tmp)
        ns["_post_event_confirmation"]("C1", "T1", subject="Team", start="S",
                                       end="E", attendees="", body=AGENDA)
        cancel = _buttons(posted)["cancel_event"]
        ns["handle_cancel_event"](_click(cancel, "cancel_event"))
        store = json.loads((tmp / "state" / "button-payloads.json").read_text())
        assert store == {}, "a cancelled event must not leave its payload behind"



def test_hold_flag_rides_from_the_staged_file_to_create_event():
    """`"hold": true` on a staged event reaches the Create block (a Hold
    line) and the create-event command (--hold), and is dropped when the
    event carries attendees, since a hold never invites anyone."""
    src = BOT_SRC
    assert "hold=_is_hold(act)," in src and "hold=_is_hold(ev)," in src
    post = _fn_src("_post_event_confirmation")
    assert "hold: bool = False" in post and '"hold": bool(hold)' in post
    assert "bool(hold), btn_value" in post
    assert "*Hold:* tentative, tagged EA Hold" in _fn_src("_event_card")
    run = _fn_src("_execute_event_create")
    assert 'if event_data.get("hold"):\n                cmd.append("--hold")' in run
    ns = {}
    exec(compile(_fn_src("_is_hold"), "ea-chat-bot.py[fn]", "exec"), ns)
    assert ns["_is_hold"]({"hold": True, "attendees": ""}) is True
    assert ns["_is_hold"]({"hold": True, "attendees": "harper.fairbanks70@example.edu"}) is False
    assert ns["_is_hold"]({"attendees": ""}) is False

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            passed += 1
        except Exception as e:
            traceback.print_exc()
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
