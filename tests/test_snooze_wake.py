#!/usr/bin/env python3
"""wake-snoozed must never lose a snooze it could not move.

`wake_snoozed_emails` rewrites the file from `remaining`, so a failed
entry has to land there. Recorded only in `woken` with an error label, a
429, a 503 or a stale message id deletes the record permanently while
the mail sits in Archive: the obligation is gone, the email never comes
back, and a snooze with no ledger_id leaves no other trace.

Also pinned: one malformed snooze_until does not abort the whole pass,
a stale id is re-resolved through its conversation the way create-reply
does, and a permanently dead entry escalates instead of failing quietly
forever.
"""

import json
import os
import sys
import tempfile
import traceback
import types
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

PAST = (date.today() - timedelta(days=1)).isoformat()
FUTURE = (date.today() + timedelta(days=7)).isoformat()


class FakeGraph:
    """move() fails for ids in `dead`; conversations re-resolve to live ids."""

    def __init__(self, dead=(), conversations=None):
        self.dead = set(dead)
        self.conversations = dict(conversations or {})
        self.moved: list = []

    def graph_patch(self, endpoint, body):
        return {}

    def graph_post(self, endpoint, body):
        mid = endpoint.split("/me/messages/", 1)[1].rsplit("/move", 1)[0]
        if mid in self.dead:
            raise RuntimeError(f"Graph 404 for {mid}")
        self.moved.append((mid, body.get("destinationId")))
        return {"id": mid + "-moved"}

    def _resolve_latest_in_conversation(self, conv_id):
        return self.conversations.get(conv_id)


def _ga(tmp: Path, graph):
    import graph_actions as ga
    os.environ["EA_DIR"] = str(tmp)
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    ga.graph = graph
    return ga


def _snoozes(tmp: Path):
    return json.loads((tmp / "state" / "snoozed-messages.json").read_text())


def _write(tmp: Path, entries):
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    (tmp / "state" / "snoozed-messages.json").write_text(json.dumps(entries))


def _entry(**kw):
    base = {"message_id": "M-DUE", "original_message_id": "M-ORIG",
            "conversation_id": "", "snooze_until": PAST,
            "description": "Conference travel decision", "ledger_id": ""}
    base.update(kw)
    return base


def test_a_due_snooze_is_woken_and_dropped():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write(tmp, [_entry()])
        g = FakeGraph()
        out = _ga(tmp, g).wake_snoozed_emails()
        assert out["woken"] == 1 and out["remaining"] == 0, out
        assert g.moved == [("M-DUE", "inbox")]
        assert _snoozes(tmp) == []


def test_a_future_snooze_is_left_alone():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write(tmp, [_entry(snooze_until=FUTURE)])
        out = _ga(tmp, FakeGraph()).wake_snoozed_emails()
        assert out == {"woken": 0, "descriptions": [], "remaining": 1}
        assert len(_snoozes(tmp)) == 1


def test_a_failed_move_keeps_the_record_for_the_next_pass():
    """A failed move keeps the entry, rather than deleting it while the
    mail stays in Archive."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write(tmp, [_entry()])
        out = _ga(tmp, FakeGraph(dead=["M-DUE"])).wake_snoozed_emails()
        assert out["woken"] == 0
        assert out["remaining"] == 1, out
        kept = _snoozes(tmp)
        assert len(kept) == 1, "a failed wake must never delete the snooze"
        assert kept[0]["wake_attempts"] == 1
        assert "404" in kept[0]["wake_error"]
        assert out["failed"] and "move failed 1/" in out["failed"][0]


def test_attempts_accumulate_and_then_escalate():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write(tmp, [_entry()])
        ga = _ga(tmp, FakeGraph(dead=["M-DUE"]))
        for _ in range(ga.MAX_WAKE_ATTEMPTS - 1):
            out = ga.wake_snoozed_emails()
            assert "escalated" not in out, out
        out = ga.wake_snoozed_emails()
        assert out.get("escalated"), out
        assert _snoozes(tmp)[0]["wake_attempts"] == ga.MAX_WAKE_ATTEMPTS
        assert len(_snoozes(tmp)) == 1, (
            "escalation is loud, not lossy: the obligation stays")


def test_a_stale_id_is_re_resolved_through_the_conversation():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write(tmp, [_entry(message_id="M-STALE", conversation_id="CONV-9")])
        g = FakeGraph(dead=["M-STALE"], conversations={"CONV-9": "M-LIVE"})
        out = _ga(tmp, g).wake_snoozed_emails()
        assert out["woken"] == 1, out
        assert g.moved == [("M-LIVE", "inbox")]
        assert _snoozes(tmp) == []


def test_an_unresolvable_conversation_still_keeps_the_record():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write(tmp, [_entry(message_id="M-STALE", conversation_id="CONV-9")])
        g = FakeGraph(dead=["M-STALE"], conversations={})
        out = _ga(tmp, g).wake_snoozed_emails()
        assert out["woken"] == 0
        assert len(_snoozes(tmp)) == 1


def test_an_id_that_is_not_a_plain_mail_id_never_reaches_graph():
    """The record is in resident space and its ids go into Graph paths run
    with the owner's token. An id with a slash, a dot segment, a query or a
    percent escape is escalated at once and kept; nothing is sent, and the
    other due entries still wake."""
    bad = [
        _entry(message_id="M-DUE/../../me/sendMail", description="path"),
        _entry(message_id="M-DUE?$select=body", description="query"),
        _entry(message_id="M%2FDUE", description="escape"),
        _entry(message_id={"id": "x"}, description="not a string"),
        _entry(message_id="M-DEAD", conversation_id="C') or (1 eq 1", description="conv"),
    ]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write(tmp, bad + [_entry(message_id="M-OK", description="good")])
        seen = []
        g = FakeGraph(dead=["M-DEAD"])
        real_patch = g.graph_patch
        g.graph_patch = lambda ep, body: (seen.append(ep), real_patch(ep, body))[1]
        g._resolve_latest_in_conversation = lambda conv: seen.append(conv) or "M-LIVE"
        out = _ga(tmp, g).wake_snoozed_emails()
        assert g.moved == [("M-OK", "inbox")], g.moved
        assert all("M-OK" in ep for ep in seen), seen
        assert len(out["escalated"]) == len(bad), out
        assert all("not a plain mail id" in e for e in out["escalated"])
        assert sorted(s["description"] for s in _snoozes(tmp)) == sorted(
            e["description"] for e in bad)


def test_a_live_id_that_is_not_plain_is_not_moved_either():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write(tmp, [_entry(message_id="M-STALE", conversation_id="CONV-9")])
        g = FakeGraph(dead=["M-STALE"], conversations={"CONV-9": "M/../LIVE"})
        out = _ga(tmp, g).wake_snoozed_emails()
        assert out["woken"] == 0 and g.moved == []
        assert out["escalated"] and len(_snoozes(tmp)) == 1


def test_one_malformed_date_does_not_abort_the_whole_pass():
    """date.fromisoformat sat outside the try: one bad value stranded every
    other due snooze."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write(tmp, [_entry(snooze_until="soon", description="broken"),
                     _entry(message_id="M-OK", description="good")])
        g = FakeGraph()
        out = _ga(tmp, g).wake_snoozed_emails()
        assert out["woken"] == 1, out
        assert g.moved == [("M-OK", "inbox")]
        assert out["escalated"] and "unreadable snooze_until" in out["escalated"][0]
        assert [s["description"] for s in _snoozes(tmp)] == ["broken"]


def test_a_recovered_entry_clears_its_failure_bookkeeping():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write(tmp, [_entry()])
        ga = _ga(tmp, FakeGraph(dead=["M-DUE"]))
        ga.wake_snoozed_emails()
        assert _snoozes(tmp)[0]["wake_attempts"] == 1
        ga.graph = FakeGraph()
        out = ga.wake_snoozed_emails()
        assert out["woken"] == 1 and _snoozes(tmp) == []


def test_snooze_records_the_conversation_id():
    """Without it a stale id has nothing to re-resolve through."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        g = FakeGraph()
        g.get_message_full = lambda mid: {"conversationId": "CONV-77"}
        ga = _ga(tmp, g)
        args = types.SimpleNamespace(message_id="M1", until=FUTURE,
                                     description="hotel deadline",
                                     ledger_id="")
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            ga.cmd_snooze_message(args)
        rec = _snoozes(tmp)[0]
        assert rec["conversation_id"] == "CONV-77"
        assert rec["original_message_id"] == "M1"


def test_concurrent_snooze_and_wake_do_not_lose_an_entry():
    """Both sides take the same file lock, so the read-modify-writes
    serialise instead of clobbering each other."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write(tmp, [_entry(message_id="M-DUE", description="due")])
        g = FakeGraph()
        g.get_message_full = lambda mid: {"conversationId": "CONV-A"}
        ga = _ga(tmp, g)
        args = types.SimpleNamespace(message_id="M-NEW", until=FUTURE,
                                     description="new snooze", ledger_id="")
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            ga.cmd_snooze_message(args)
        ga.wake_snoozed_emails()
        left = [s["description"] for s in _snoozes(tmp)]
        assert left == ["new snooze"], left


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
