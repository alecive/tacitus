"""The Slack card renderer against the goldens in tests/fixtures/slack_cards.

Each `*_card` function is the bot's own ConfirmationCard construction,
exec'd from its `# ── Confirmation cards` block: it takes the builder's
inputs, after the builder's non-rendering work (draft fetch, preflight,
payload stash, location and repeat resolution), and returns the neutral
card. The test builds every card from the golden's recorded
inputs and asserts `render_card(card)` equals the golden's text and blocks
as parsed JSON, and that `post_card` sends the golden's kwargs.

The bot helpers that stay in the core (`_format_send_after`,
`_repeat_summary`, `_resolve_online_location`, `_staged_button_value`)
are exec'd from the bot's source with the same stubs capture.py used.
"""
from __future__ import annotations

import copy
import json
import sys
import threading
import time
import types
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

from chat.cards import ConfirmationCard  # noqa: E402
from chat.slack_frontend import SlackFrontend, render_card  # noqa: E402
from chat.types import MessageRef  # noqa: E402

GOLDEN = ROOT / "tests" / "fixtures" / "slack_cards"
BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()

ZOOM = "https://example.zoom.us/j/1234567890"
JOIN_BLOCK = f"Join Zoom meeting\n{ZOOM}"
PAYLOAD_REF = "0123456789abcdef0123456789abcdef"
CONFIRM_TS = "1700000001.000200"
DM = "D_OWNER"


def golden(name: str) -> dict:
    return json.loads((GOLDEN / name).read_text())


def goldens(prefix: str) -> list[str]:
    return sorted(p.name for p in GOLDEN.glob(f"{prefix}__*.json"))


# ---------------------------------------------------------------------------
# Core helpers exec'd from the bot, as capture.py does
# ---------------------------------------------------------------------------

def _fn_src(name: str) -> str:
    marker = f"\ndef {name}("
    start = BOT_SRC.index(marker)
    ends = [BOT_SRC.index("\ndef ", start + 1)]
    for deco in ("@app.action(", "@app.view(", "@app.event("):
        nxt = BOT_SRC.find("\n" + deco, start + 1)
        if nxt > 0:
            ends.append(nxt)
    return BOT_SRC[start:min(ends)]


def _block_src(start_marker: str, end_marker: str) -> str:
    start = BOT_SRC.index(start_marker)
    return BOT_SRC[start:BOT_SRC.index(end_marker, start)]


def core_ns(stash_ref: str = PAYLOAD_REF) -> dict:
    quiet = lambda *a, **k: None  # noqa: E731
    ns = {"json": json, "time": time, "threading": threading, "datetime": datetime,
          "Path": Path, "EA_DIR": Path("/nonexistent-ea-dir"),
          "_private_state": lambda name: Path("/nonexistent-ea-dir") / "state" / name,
          "log": types.SimpleNamespace(info=quiet, debug=quiet, warning=quiet, error=quiet),
          "_zoom_link": lambda: ZOOM}
    exec(compile(_block_src("_ONLINE_PLACEHOLDERS = ", "\n\n\n"), "bot[const]", "exec"), ns)
    exec(compile(_block_src("# \u2500\u2500 Staged button payloads",
                            "\ndef _post_event_confirmation("), "bot[staged]", "exec"), ns)
    ns["_stash_button_payload"] = lambda payload: stash_ref
    for name in ("_format_send_after", "_repeat_summary", "_resolve_online_location"):
        exec(compile(_fn_src(name), f"bot[{name}]", "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# The card construction per kind, exec'd from the bot's card block
# ---------------------------------------------------------------------------

def card_ns() -> dict:
    """The bot's `# ── Confirmation cards` block: one pure function per
    card kind, the builder's computed inputs in, a ConfirmationCard out."""
    import dataclasses

    import chat
    from chat.slack_frontend import draft_card_fits
    # A Send or Schedule card asks the frontend whether the whole draft fits
    # in one card; here that is the Slack rule.
    ns = {"json": json, "_chat": chat, "dataclasses": dataclasses,
          "FRONTEND": types.SimpleNamespace(card_fits=draft_card_fits)}
    exec(compile(_block_src("# \u2500\u2500 Confirmation cards",
                            "# \u2500\u2500 End confirmation cards"), "bot[cards]", "exec"), ns)
    return ns


_CARDS_NS = card_ns()
send_card = _CARDS_NS["_send_card"]
schedule_card = _CARDS_NS["_schedule_card"]
bulk_card = _CARDS_NS["_bulk_card"]
event_card = _CARDS_NS["_event_card"]
event_update_card = _CARDS_NS["_event_update_card"]
event_delete_card = _CARDS_NS["_event_delete_card"]
room_picker_card = _CARDS_NS["_room_picker_card"]
ROOM_PICKER_FOOTER = _CARDS_NS["_ROOM_PICKER_FOOTER"]


# ---------------------------------------------------------------------------
# Golden cases: inputs to card
# ---------------------------------------------------------------------------

def _args(g: dict) -> dict:
    return g["inputs"]["args"]


def _stubs(g: dict) -> dict:
    return g["inputs"].get("stubs") or {}


def card_for(name: str, g: dict) -> ConfirmationCard:
    a, s = _args(g), _stubs(g)
    builder = name.split("__")[0]
    if builder == "build_send_confirmation":
        system = a.get("email_system", "outlook")
        warning = s.get("sched_warning", "") if system == "outlook" else ""
        return send_card(a["channel"], a["thread_ts"], a["draft_id"], s["draft"], warning,
                         system, a.get("gmail_email", ""))
    if builder == "post_schedule_send_confirmation":
        when = core_ns()["_format_send_after"](a["send_after"])
        return schedule_card(a["channel"], a["thread_ts"], a["draft_id"], s["draft"],
                             a["send_after"], when, a["description"], a["ledger_id"],
                             s.get("sched_warning", ""))
    if builder == "post_bulk_card":
        confirms = s.get("confirms") or {}
        live = [d for d in s["bulk"]["ids"] if (confirms.get(d) or {}).get("status") == "live"]
        return bulk_card("b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0"[:12], len(live))
    ns = core_ns(s.get("stash_ref", PAYLOAD_REF))
    base = {"thread_ts": a["thread_ts"], "ea_channel": a["channel"]}
    if builder == "post_event_confirmation":
        location = ns["_resolve_online_location"](a.get("location", ""), a.get("online", False))
        repeat = a.get("repeat") or {}
        event_data = {"subject": a["subject"], "start": a["start"], "end": a["end"],
                      "attendees": a["attendees"], "location": location,
                      "body": a.get("body", ""), "online": a.get("online", False),
                      "hold": bool(a.get("hold", False)), **repeat}
        # The body as the invite carries it: the bot adds the join block of
        # an online invite (_invite_body_as_sent); here, a fixed one.
        body = a.get("body", "")
        if a.get("online"):
            body = f"{body}\n\n{JOIN_BLOCK}\n" if body else JOIN_BLOCK
        return event_card(a["channel"], a["thread_ts"], a["subject"], a["start"], a["end"],
                          a["attendees"], location,
                          ns["_repeat_summary"](repeat) if repeat else "",
                          bool(a.get("hold", False)),
                          ns["_staged_button_value"](base, event_data), body=body)
    if builder == "post_event_update_confirmation":
        # `stubs.event` is the bot's own read of the event
        # (_read_calendar_event); `stubs.change_when` is the staged new time
        # as _event_span_text writes it.
        event_data = {"event_id": a["event_id"], "subject": a["subject"],
                      "start": a.get("new_start", ""), "end": a.get("new_end", ""),
                      "attendees": a.get("attendees", ""), "location": a.get("location", "")}
        if a.get("original_start"):
            event_data["original_start"] = a["original_start"]
        event_data["event_fingerprint"] = s["event"]["fingerprint"]
        change = {"subject": a["subject"], "when": s.get("change_when", ""),
                  "attendees": a.get("attendees", ""), "optional_attendees": "",
                  "location": a.get("location", "")}
        return event_update_card(a["channel"], a["thread_ts"], s["event"], change,
                                 ns["_staged_button_value"](base, event_data))
    if builder == "post_event_delete_confirmation":
        event_data = {"event_id": a["event_id"], "subject": a["subject"],
                      "start": a.get("start", ""), "end": a.get("end", ""),
                      "event_fingerprint": s["event"]["fingerprint"]}
        return event_delete_card(a["channel"], a["thread_ts"], s["event"],
                                 ns["_staged_button_value"](base, event_data),
                                 staged_subject=a["subject"])
    if builder == "room_picker":
        return room_picker_card(a["channel"], a["thread_ts"], a["query"], a["candidates"],
                                s.get("active") or [])
    raise AssertionError(f"no card construction for {name}")


CARD_GOLDENS = [n for prefix in ("build_send_confirmation", "post_schedule_send_confirmation",
                                 "post_bulk_card", "post_event_confirmation",
                                 "post_event_update_confirmation",
                                 "post_event_delete_confirmation", "room_picker")
                for n in goldens(prefix)]


def test_every_golden_is_covered():
    covered = set(CARD_GOLDENS) | set(goldens("replace_buttons_with_status")) \
        | set(goldens("post_dm_breadcrumb"))
    assert covered == {p.name for p in GOLDEN.glob("*.json")}
    assert len(CARD_GOLDENS) == 23


class FakeClient:
    """Records Web API calls; chat_postMessage returns the golden card ts."""

    def __init__(self, permalink_error: bool = False):
        self.calls: list[dict] = []
        self.permalink_error = permalink_error

    def chat_postMessage(self, **kw):
        self.calls.append({"method": "chat_postMessage", "kwargs": copy.deepcopy(kw)})
        return {"ok": True, "ts": CONFIRM_TS}

    def chat_update(self, **kw):
        self.calls.append({"method": "chat_update", "kwargs": copy.deepcopy(kw)})
        return {"ok": True}

    def chat_getPermalink(self, **kw):
        self.calls.append({"method": "chat_getPermalink", "kwargs": copy.deepcopy(kw)})
        if self.permalink_error:
            raise RuntimeError("permalink lookup refused")
        return {"ok": True,
                "permalink": "https://example.slack.com/archives/D_OWNER/p1700000001000200"}


def frontend(client: FakeClient) -> SlackFrontend:
    return SlackFrontend("xoxb-test", channel_name="ea", channel_id="C_MAIN",
                         owner_id="U_OWNER", for_cli=True, client=client)


@pytest.mark.parametrize("name", CARD_GOLDENS)
def test_render_matches_golden(name):
    g = golden(name)
    card = card_for(name, g)
    text, blocks = render_card(card)
    assert json.loads(json.dumps(blocks)) == g["blocks"]
    assert text == g["text"]


@pytest.mark.parametrize("name", [n for n in CARD_GOLDENS if golden(n)["kwargs"]])
def test_post_card_kwargs_match_golden(name):
    """The card post: to the DM with no thread_ts, or into the EA thread
    when there is no DM."""
    g = golden(name)
    card = card_for(name, g)
    kw = g["kwargs"]
    client = FakeClient()
    ref = frontend(client).post_card(kw["channel"], card, thread_ts=kw.get("thread_ts"))
    assert client.calls[0] == {"method": "chat_postMessage", "kwargs": kw}
    assert ref.id == CONFIRM_TS and ref.posted_at == float(CONFIRM_TS)


def test_update_card_sends_blocks():
    g = golden("build_send_confirmation__basic.json")
    card = card_for("build_send_confirmation__basic.json", g)
    client = FakeClient()
    assert frontend(client).update_card(MessageRef(DM, CONFIRM_TS, CONFIRM_TS), card)
    assert client.calls == [{"method": "chat_update", "kwargs": {
        "channel": DM, "ts": CONFIRM_TS, "text": g["text"], "blocks": g["blocks"]}}]


@pytest.mark.parametrize("name", goldens("replace_buttons_with_status"))
def test_replace_card_with_status_matches_golden(name):
    g = golden(name)
    a = _args(g)
    client = FakeClient()
    ok = frontend(client).replace_card_with_status(
        MessageRef(a["channel"], a["message_ts"], a["message_ts"]), a["status_text"])
    assert ok
    assert client.calls == g["calls"]


def breadcrumb(fe: SlackFrontend, ea_channel: str, thread_ts: str | None, dm_channel: str,
               dm_ts: str | None, what: str) -> None:
    """_post_dm_breadcrumb on the frontend: a permalink when there is a card
    ts, then one unfurl-off line in the thread."""
    if not thread_ts or not dm_channel or not ea_channel or ea_channel == dm_channel:
        return
    link = ""
    if dm_ts:
        try:
            link = fe.permalink(dm_channel, dm_ts)
        except Exception:
            link = ""
    text = f":lock: {what} is in your DM" + (f": <{link}|open it>" if link else ".")
    fe.post(ea_channel, text, thread_ts=thread_ts, unfurl=False)


@pytest.mark.parametrize("name", goldens("post_dm_breadcrumb"))
def test_breadcrumb_matches_golden(name):
    g = golden(name)
    a = _args(g)
    client = FakeClient(permalink_error=bool(_stubs(g).get("permalink_error")))
    breadcrumb(frontend(client), a["ea_channel"], a["thread_ts"], a["dm_channel"],
               a["dm_ts"], a["what"])
    assert client.calls == g["calls"]


def test_card_post_then_breadcrumb_matches_event_golden():
    """The whole delivery of one event card, as _deliver_confirmation does it."""
    name = "post_event_confirmation__basic.json"
    g = golden(name)
    card = card_for(name, g)
    client = FakeClient()
    fe = frontend(client)
    ref = fe.post_card(DM, card)
    breadcrumb(fe, "C_MAIN", card.thread_ts, DM, ref.id,
               "The Create button for this event")
    assert client.calls == g["calls"]


def test_room_picker_cuts():
    long_name = "N" * 90
    card = room_picker_card("C_MAIN", "1700000000.000100", "q",
                            [{"id": "p1", "name": long_name}],
                            [{"id": "x" * 90, "name": long_name}] +
                            [{"id": f"p{i}", "name": f"P{i}"} for i in range(150)])
    _, blocks = render_card(card)
    elements = blocks[1]["elements"]
    assert elements[0]["text"]["text"] == ("Open: " + long_name)[:75]
    select = elements[1]
    assert select["type"] == "static_select" and len(select["options"]) == 100
    assert select["options"][0]["text"]["text"] == long_name[:75]
    assert select["options"][0]["value"] == "x" * 75
    assert elements[2]["action_id"] == "room_create_project"


def test_unknown_kind_raises():
    from chat import ChatError
    with pytest.raises(ChatError):
        render_card(ConfirmationCard(kind="nope", title="x"))
