#!/usr/bin/env python3
"""The scheduled-send credential must be a real signature, not a magic prefix.

`~/.ea/state/scheduled-sends.json` is writable by the prompt-injectable user
(ea-bot) and `scheduled_sends.py check` runs as crh with the Graph token. A
literal marker string in that file would let anyone who can write it send
mail as the user with no Slack button.

These tests pin the credential: an HMAC over (draft_id, send_after,
fingerprint) keyed by a file only crh can read, verified in `cmd_check` AND in
`graph_client._validate_library_token`, with every failure mode closed. The
fingerprint is the content of the draft the owner reviewed: a queued draft
whose content changed is dropped, and a version 1 token (no content bound)
is dropped too.

Nothing here touches live state: EA_DIR is repointed at a sandbox before the
modules that read it are imported, and every test rebinds the module paths.
"""
import io
import json
import os
import stat
import sys
import tempfile
import traceback
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

_SANDBOX = tempfile.mkdtemp(prefix="ea-sched-send-tests-")
os.environ["EA_DIR"] = _SANDBOX
# The send path's draft locks live in the owner-private directory.
os.environ["EA_PRIVATE_DIR"] = os.path.join(_SANDBOX, "private")
# _post_slack is a no-op without these; keep it that way even if the runner has
# them exported.
os.environ.pop("SLACK_BOT_TOKEN", None)
os.environ.pop("EA_SLACK_CHANNEL_ID", None)

import scheduled_sends as S  # noqa: E402

DRAFT = "AAMkADraftIdExample="
WHEN = "2020-01-01T00:00"          # in the past: "due" for cmd_check
FP = "sha256:" + "ab" * 32          # the reviewed content
OTHER_FP = "sha256:" + "cd" * 32    # the same draft after an edit


@contextmanager
def _sandbox(with_key: bool = True):
    """Point the module at a private EA_DIR and capture Slack posts."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "state").mkdir()
        (tmp / "auth").mkdir()
        old = (S.SENDS_FILE, S.HMAC_KEY_FILE, S._post_slack)
        S.SENDS_FILE = tmp / "state" / "scheduled-sends.json"
        S.HMAC_KEY_FILE = tmp / "auth" / "scheduled-send-hmac.key"
        posts: list[str] = []
        S._post_slack = posts.append
        if with_key:
            S.HMAC_KEY_FILE.write_bytes(b"0123456789abcdef0123456789abcdef0123456789")
            S.HMAC_KEY_FILE.chmod(0o600)
        try:
            yield tmp, posts
        finally:
            S.SENDS_FILE, S.HMAC_KEY_FILE, S._post_slack = old


class _FakeGraph:
    """Stands in for graph_client so a test can never reach the network, and a
    forged entry that slips through is loud instead of silent."""

    def __init__(self, result=None, current=FP):
        self.calls = []
        self.reads = []
        self.result = result or {"status": "sent"}
        self.current = current       # the draft's content fingerprint now

    def draft_fingerprint(self, message_id):
        self.reads.append(message_id)
        return self.current

    def send_draft(self, message_id, confirm_token=None, fingerprint=None):
        self.calls.append((message_id, confirm_token))
        if fingerprint != self.current:
            return {"status": "changed"}
        return self.result


@contextmanager
def _fake_graph_client(result=None, current=FP):
    fake = _FakeGraph(result, current)
    had = "graph_client" in sys.modules
    prev = sys.modules.get("graph_client")
    sys.modules["graph_client"] = fake
    try:
        yield fake
    finally:
        if had:
            sys.modules["graph_client"] = prev
        else:
            del sys.modules["graph_client"]


def _run_check() -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        S.cmd_check(None)
    return json.loads(buf.getvalue())


def _entry(token, draft_id=DRAFT, send_after=WHEN, **extra):
    e = {"draft_id": draft_id, "send_after": send_after,
         "description": "follow up with Eli", "ledger_id": "",
         "confirm_token": token, "fingerprint": FP,
         "created": "2026-08-22T09:00:00"}
    e.update(extra)
    return e


# ── the credential itself ────────────────────────────────────────────

def test_token_round_trip():
    with _sandbox():
        tok = S.make_send_token(DRAFT, WHEN, FP)
        assert tok.startswith(S.TOKEN_PREFIX)
        assert S.verify_send_token(tok, DRAFT, WHEN, fingerprint=FP)
        assert S.verify_send_token(tok, DRAFT, fingerprint=FP)          # send_after optional


def test_token_is_bound_to_the_draft_and_the_time():
    with _sandbox():
        tok = S.make_send_token(DRAFT, WHEN, FP)
        # replayed onto another draft
        assert not S.verify_send_token(tok, "AAMkOtherDraft=", WHEN, fingerprint=FP)
        # the queue entry re-timed to fire early
        assert not S.verify_send_token(tok, DRAFT, "2019-01-01T00:00", fingerprint=FP)
        # the signed time inside the token edited
        parts = tok.split(":", 3)
        forged = ":".join(parts[:3] + ["2019-01-01T00:00"])
        assert not S.verify_send_token(forged, DRAFT, fingerprint=FP)
        # one flipped hex digit in the MAC
        mac = list(parts[2])
        mac[0] = "0" if mac[0] != "0" else "1"
        assert not S.verify_send_token(
            ":".join(parts[:2] + ["".join(mac), parts[3]]), DRAFT, WHEN, fingerprint=FP)


def test_token_is_bound_to_the_content():
    with _sandbox():
        tok = S.make_send_token(DRAFT, WHEN, FP)
        # the entry re-pointed at other content
        assert not S.verify_send_token(tok, DRAFT, WHEN, fingerprint=OTHER_FP)
        # no fingerprint at all: fail closed
        for missing in (None, ""):
            assert not S.verify_send_token(tok, DRAFT, WHEN, fingerprint=missing)
        try:
            S.make_send_token(DRAFT, WHEN, "")
            assert False, "minting without a fingerprint must fail"
        except ValueError:
            pass


def test_legacy_bare_marker_is_not_a_credential():
    with _sandbox():
        for token in ("pre-approved:anything", "pre-approved:", "pre-approved",
                      "pre-approved:v1:", "pre-approved:v1:deadbeef",
                      f"pre-approved:v1:{'a' * 64}:{WHEN}", "", None):
            assert not S.verify_send_token(token, DRAFT, WHEN, fingerprint=FP), token


def test_missing_key_fails_closed():
    with _sandbox(with_key=False):
        try:
            S.make_send_token(DRAFT, WHEN, FP)
            assert False, "minting must fail without the key"
        except S.ApprovalKeyError:
            pass
        assert not S.verify_send_token(f"pre-approved:v1:{'a' * 64}:{WHEN}", DRAFT, fingerprint=FP)


def test_unreadable_or_short_key_fails_closed():
    with _sandbox():
        good = S.make_send_token(DRAFT, WHEN, FP)
        S.HMAC_KEY_FILE.chmod(0o000)
        try:
            if os.geteuid() != 0:          # root reads it regardless
                assert not S.verify_send_token(good, DRAFT, WHEN, fingerprint=FP)
        finally:
            S.HMAC_KEY_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
        S.HMAC_KEY_FILE.write_bytes(b"short")
        assert not S.verify_send_token(good, DRAFT, WHEN, fingerprint=FP)


# ── cmd_check: the gate the cron actually runs ───────────────────────

def test_check_refuses_a_forged_legacy_entry():
    """The exact exploit from the finding: ea-bot writes the queue file with a
    bare marker and a past time. Nothing may be sent."""
    with _sandbox() as (_tmp, posts):
        S._save_sends([_entry("pre-approved:anything")])
        with _fake_graph_client() as fake:
            out = _run_check()
        assert fake.calls == [], "a forged entry reached graph_client"
        assert out["sent"] == 0 and out["refused"] == 1 and out["remaining"] == 0
        assert S._load_sends() == []            # dropped, not left to retry
        assert any("REFUSED" in p for p in posts), posts


def test_check_refuses_a_forged_entry_even_before_the_key_exists():
    """The deploy window: no key file yet. A legacy/forged entry is still
    dropped on its shape, not held and re-alarmed every five minutes."""
    with _sandbox(with_key=False) as (_tmp, posts):
        S._save_sends([_entry("pre-approved:anything")])
        with _fake_graph_client() as fake:
            out = _run_check()
        assert fake.calls == [] and out["refused"] == 1 and out["remaining"] == 0
        assert any("REFUSED" in p for p in posts), posts


def test_check_refuses_an_entry_retimed_after_signing():
    with _sandbox() as (_tmp, posts):
        tok = S.make_send_token(DRAFT, "2030-01-01T09:00", FP)
        S._save_sends([_entry(tok, send_after=WHEN)])   # moved into the past
        with _fake_graph_client() as fake:
            out = _run_check()
        assert fake.calls == [] and out["refused"] == 1
        assert any("REFUSED" in p for p in posts), posts


def test_check_refuses_a_token_minted_for_another_draft():
    with _sandbox():
        tok = S.make_send_token("AAMkSomeOtherDraft=", WHEN, FP)
        S._save_sends([_entry(tok)])
        with _fake_graph_client() as fake:
            out = _run_check()
        assert fake.calls == [] and out["refused"] == 1


def test_check_sends_a_properly_signed_entry():
    with _sandbox():
        tok = S.make_send_token(DRAFT, WHEN, FP)
        S._save_sends([_entry(tok)])
        with _fake_graph_client() as fake:
            out = _run_check()
        assert fake.calls == [(DRAFT, tok)]
        assert fake.reads == [DRAFT], "the cron re-reads the draft before sending"
        assert out["sent"] == 1 and out["refused"] == 0 and out["remaining"] == 0


def test_check_drops_a_draft_whose_content_changed_after_approval():
    """update-draft, attach-file and remove-attachment are SAFE verbs: a
    queued draft can change after the click. The signed fingerprint no
    longer matches the draft, so nothing is sent, the entry is dropped and
    the owner is told."""
    with _sandbox() as (_tmp, posts):
        tok = S.make_send_token(DRAFT, WHEN, FP)
        S._save_sends([_entry(tok)])
        with _fake_graph_client(current=OTHER_FP) as fake:
            out = _run_check()
        assert fake.calls == [], "a changed draft reached send_draft"
        assert out["sent"] == 0 and out["refused"] == 1 and out["remaining"] == 0
        assert S._load_sends() == []
        assert any("changed after you approved it" in p for p in posts), posts


def test_check_drops_a_draft_that_changes_between_the_read_and_the_send():
    """send_draft checks the content once more right before the send and
    answers "changed"; the cron drops that entry the same way."""
    with _sandbox() as (_tmp, posts):
        tok = S.make_send_token(DRAFT, WHEN, FP)
        S._save_sends([_entry(tok)])
        with _fake_graph_client(result={"status": "changed"}) as fake:
            out = _run_check()
        assert fake.calls == [(DRAFT, tok)]
        assert out["sent"] == 0 and out["refused"] == 1 and out["remaining"] == 0
        assert any("changed after you approved it" in p for p in posts), posts


def test_check_refuses_an_entry_repointed_at_other_content():
    """ea-bot edits the stored fingerprint to match its edited draft. The
    signature covers the fingerprint, so the entry no longer verifies."""
    with _sandbox() as (_tmp, posts):
        tok = S.make_send_token(DRAFT, WHEN, FP)
        S._save_sends([_entry(tok, fingerprint=OTHER_FP)])
        with _fake_graph_client(current=OTHER_FP) as fake:
            out = _run_check()
        assert fake.calls == [] and out["refused"] == 1
        assert any("REFUSED" in p for p in posts), posts


def test_check_drops_a_version_1_entry():
    """A version 1 token signed only (draft_id, send_after) and binds no
    content. Decision: fail closed. The entry is dropped with an alert that
    says to schedule it again; nothing is sent, even with a valid v1 MAC."""
    import hashlib
    import hmac as _hmac
    with _sandbox() as (_tmp, posts):
        key = S._load_key()
        v1 = "\x00".join(("ea-scheduled-send", "v1", DRAFT, WHEN)).encode()
        mac = _hmac.new(key, v1, hashlib.sha256).hexdigest()
        tok = f"{S.LEGACY_TOKEN_PREFIX}{mac}:{WHEN}"
        S._save_sends([_entry(tok)])
        with _fake_graph_client() as fake:
            out = _run_check()
        assert fake.calls == [] and fake.reads == []
        assert out["sent"] == 0 and out["refused"] == 1 and out["remaining"] == 0
        assert any("Schedule it again" in p for p in posts), posts
        assert not S.verify_send_token(tok, DRAFT, WHEN, fingerprint=FP)


def test_check_leaves_future_entries_alone():
    with _sandbox():
        tok = S.make_send_token(DRAFT, "2099-01-01T09:00", FP)
        S._save_sends([_entry(tok, send_after="2099-01-01T09:00")])
        with _fake_graph_client() as fake:
            out = _run_check()
        assert fake.calls == [] and out["remaining"] == 1


def test_check_holds_everything_when_the_key_is_missing():
    """Operational failure, not an attack: hold and alarm, never send."""
    with _sandbox(with_key=True) as (_tmp, posts):
        tok = S.make_send_token(DRAFT, WHEN, FP)
        S._save_sends([_entry(tok)])
        S.HMAC_KEY_FILE.unlink()
        with _fake_graph_client() as fake:
            out = _run_check()
        assert fake.calls == []
        assert out["sent"] == 0 and out["remaining"] == 1   # kept for retry
        assert any("key unavailable" in p for p in posts), posts


def test_check_survives_a_malformed_send_after():
    """One unparseable entry must not abort the pass and strand the rest."""
    with _sandbox():
        tok = S.make_send_token(DRAFT, WHEN, FP)
        S._save_sends([_entry("pre-approved:x", draft_id="bad", send_after="soon"),
                       _entry(tok)])
        with _fake_graph_client() as fake:
            out = _run_check()
        assert fake.calls == [(DRAFT, tok)]
        assert out["sent"] == 1 and out["refused"] == 1


def test_add_refuses_an_unsigned_entry():
    import argparse
    with _sandbox():
        args = argparse.Namespace(draft_id=DRAFT, send_after=WHEN,
                                  description="x", ledger_id=None,
                                  confirm_token="pre-approved:anything",
                                  fingerprint=FP)
        buf = io.StringIO()
        with redirect_stdout(buf):
            S.cmd_add(args)
        assert "refused" in json.loads(buf.getvalue())["error"]
        assert S._load_sends() == []
        args.confirm_token = S.make_send_token(DRAFT, WHEN, FP)
        args.fingerprint = OTHER_FP          # signed for other content
        buf = io.StringIO()
        with redirect_stdout(buf):
            S.cmd_add(args)
        assert "refused" in json.loads(buf.getvalue())["error"]
        args.fingerprint = FP
        buf = io.StringIO()
        with redirect_stdout(buf):
            S.cmd_add(args)
        assert json.loads(buf.getvalue())["status"] == "scheduled"
        assert [e["fingerprint"] for e in S._load_sends()] == [FP]


# ── the library gate (graph_client) ──────────────────────────────────

def _graph_client():
    import graph_client
    return graph_client


def test_library_gate_refuses_the_bare_prefix():
    gc = _graph_client()
    with _sandbox():
        for token in ("pre-approved:", "pre-approved:anything",
                      "pre-approved:" + "x" * 64):
            assert not gc._validate_library_token(token, "send", DRAFT), token
            assert not gc._validate_library_token(token, "event", "Subject"), token


def test_library_gate_verifies_the_scheduled_send_hmac():
    gc = _graph_client()
    with _sandbox():
        tok = S.make_send_token(DRAFT, WHEN, FP)
        assert gc._validate_library_token(tok, "send", DRAFT, FP)
        assert gc._validate_library_token(tok, "send", DRAFT, FP)   # not one-time
        assert not gc._validate_library_token(tok, "send", "AAMkOther=", FP)
        # bound to the content too, and a send with no fingerprint fails
        assert not gc._validate_library_token(tok, "send", DRAFT, OTHER_FP)
        assert not gc._validate_library_token(tok, "send", DRAFT)
        # a send credential is not a calendar credential
        assert not gc._validate_library_token(tok, "event", DRAFT)
        assert not gc._validate_library_token(tok, "delete_event", DRAFT)
        # ...including the reschedule action, which consults the gate on
        # every change and not only an attendee change
        assert not gc._validate_library_token(tok, "update_event", DRAFT)


def test_library_gate_fails_closed_without_the_key():
    gc = _graph_client()
    with _sandbox() as (_tmp, _posts):
        tok = S.make_send_token(DRAFT, WHEN, FP)
        S.HMAC_KEY_FILE.unlink()
        assert not gc._validate_library_token(tok, "send", DRAFT, FP)


def test_inprocess_approval_is_one_time_and_bound():
    gc = _graph_client()
    tok = gc.issue_inprocess_approval("send", DRAFT, FP)
    assert tok not in ("", None) and not tok.startswith("pre-approved:")
    assert not gc._validate_library_token(tok, "send", "AAMkOther=", FP)  # wrong id
    tok2 = gc.issue_inprocess_approval("event", "Faculty meeting")
    assert not gc._validate_library_token(tok2, "send", "Faculty meeting", FP)
    tok4 = gc.issue_inprocess_approval("send", DRAFT, FP)
    assert not gc._validate_library_token(tok4, "send", DRAFT, OTHER_FP)  # other content
    tok5 = gc.issue_inprocess_approval("send", DRAFT)
    assert not gc._validate_library_token(tok5, "send", DRAFT)        # no content bound
    tok3 = gc.issue_inprocess_approval("send", DRAFT, FP)
    assert gc._validate_library_token(tok3, "send", DRAFT, FP)
    assert not gc._validate_library_token(tok3, "send", DRAFT, FP)    # consumed
    # a calendar approval needs no fingerprint
    tok6 = gc.issue_inprocess_approval("event", "Faculty meeting")
    assert gc._validate_library_token(tok6, "event", "Faculty meeting")


def test_button_send_path_still_passes_the_library_gate():
    """The live path: the Slack Send button -> graph_actions.cmd_send_draft
    -> graph_client.send_draft. The CLI mints a process-local approval
    rather than a forgeable marker string, and the send still goes
    through."""
    from unittest.mock import patch
    import argparse
    import graph_actions as ga
    gc = _graph_client()
    mid = "AAMkDraftToSend="
    with _sandbox() as (tmp, _posts):
        ga.PENDING_CONFIRMS_FILE = tmp / "pending-confirmations.json"
        ga.PENDING_CONFIRMS_LOCK = tmp / "pending-confirmations.lock"
        token = ga._create_confirmation("send", mid, "test", fingerprint=FP)
        posted = []
        buf = io.StringIO()
        with patch.object(gc, "preflight_check",
                          return_value={"ok": True, "warnings": [],
                                        "errors": [], "draft_summary": {}}), \
             patch.object(gc, "draft_fingerprint", return_value=FP), \
             patch.object(gc, "graph_post",
                          side_effect=lambda ep, body: posted.append(ep) or {}), \
             redirect_stdout(buf):
            ga.cmd_send_draft(argparse.Namespace(message_id=mid,
                                                 confirm_token=token))
        out = json.loads(buf.getvalue())
        assert out.get("status") == "sent", out
        assert posted == [f"/me/messages/{mid}/send"]
        # and the same CLI token cannot be replayed
        buf = io.StringIO()
        with redirect_stdout(buf):
            ga.cmd_send_draft(argparse.Namespace(message_id=mid,
                                                 confirm_token=token))
        assert "BLOCKED" in buf.getvalue()


def test_button_send_refuses_a_draft_changed_after_the_click():
    """The token carries the reviewed fingerprint; send_draft reads the
    draft again right before the send and posts nothing when it changed."""
    from unittest.mock import patch
    import argparse
    import graph_actions as ga
    gc = _graph_client()
    mid = "AAMkDraftEdited="
    with _sandbox() as (tmp, _posts):
        ga.PENDING_CONFIRMS_FILE = tmp / "pending-confirmations.json"
        ga.PENDING_CONFIRMS_LOCK = tmp / "pending-confirmations.lock"
        posted = []
        ok = {"ok": True, "warnings": [], "errors": [], "draft_summary": {}}
        token = ga._create_confirmation("send", mid, "test", fingerprint=FP)
        buf = io.StringIO()
        with patch.object(gc, "preflight_check", return_value=ok), \
             patch.object(gc, "draft_fingerprint", return_value=OTHER_FP), \
             patch.object(gc, "graph_post",
                          side_effect=lambda ep, body: posted.append(ep) or {}), \
             redirect_stdout(buf):
            ga.cmd_send_draft(argparse.Namespace(message_id=mid, confirm_token=token))
        assert json.loads(buf.getvalue())["status"] == "changed"
        assert posted == []
        # a send token minted with no fingerprint never sends
        bare = ga._create_confirmation("send", mid, "test")
        buf = io.StringIO()
        with patch.object(gc, "graph_post",
                          side_effect=lambda ep, body: posted.append(ep) or {}), \
             redirect_stdout(buf):
            ga.cmd_send_draft(argparse.Namespace(message_id=mid, confirm_token=bare))
        assert "BLOCKED" in buf.getvalue() and posted == []
        # nor does a direct library call with no fingerprint
        assert gc.send_draft(mid, confirm_token=gc.issue_inprocess_approval(
            "send", mid))["status"] == "blocked"


def test_send_draft_is_blocked_by_a_forged_token():
    """End of the exploit chain: even if a forged entry reached graph_client,
    send_draft must refuse it before any Graph call."""
    gc = _graph_client()
    with _sandbox():
        result = gc.send_draft(DRAFT, confirm_token="pre-approved:anything")
        assert result["status"] == "blocked"


# ── schedule-send mints what check accepts ───────────────────────────

def test_schedule_send_mints_a_token_check_accepts():
    from unittest.mock import patch
    import argparse
    import graph_actions as ga
    with _sandbox() as (tmp, _posts):
        ga.PENDING_CONFIRMS_FILE = tmp / "pending-confirmations.json"
        ga.PENDING_CONFIRMS_LOCK = tmp / "pending-confirmations.lock"
        token = ga._create_confirmation("send", DRAFT, "test", fingerprint=FP)
        args = argparse.Namespace(draft_id=DRAFT, send_after="2030-04-21T09:00",
                                  description="Eli follow-up",
                                  ledger_id=None, confirm_token=token)
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return type("P", (), {"stdout": "{}", "stderr": ""})()

        buf = io.StringIO()
        with patch("subprocess.run", side_effect=fake_run), \
             patch.object(ga.graph, "draft_fingerprint", return_value=FP), \
             redirect_stdout(buf):
            ga.cmd_schedule_send(args)
        flag = [c for c in seen["cmd"] if c.startswith("--confirm-token=")]
        assert flag, seen["cmd"]
        assert f"--fingerprint={FP}" in seen["cmd"]
        minted = flag[0].split("=", 1)[1]
        assert S.verify_send_token(minted, DRAFT, "2030-04-21T09:00", fingerprint=FP)
        # ...and it is useless for any other draft, time or content
        assert not S.verify_send_token(minted, "AAMkOther=", "2030-04-21T09:00", fingerprint=FP)
        assert not S.verify_send_token(minted, DRAFT, "2020-01-01T00:00", fingerprint=FP)
        assert not S.verify_send_token(minted, DRAFT, "2030-04-21T09:00",
                                       fingerprint=OTHER_FP)


def test_schedule_send_refuses_a_draft_changed_since_the_card():
    from unittest.mock import patch
    import argparse
    import graph_actions as ga
    with _sandbox() as (tmp, _posts):
        ga.PENDING_CONFIRMS_FILE = tmp / "pending-confirmations.json"
        ga.PENDING_CONFIRMS_LOCK = tmp / "pending-confirmations.lock"
        token = ga._create_confirmation("send", DRAFT, "test", fingerprint=FP)
        args = argparse.Namespace(draft_id=DRAFT, send_after="2030-04-21T09:00",
                                  description="x", ledger_id=None, confirm_token=token)
        buf = io.StringIO()
        with patch("subprocess.run") as run, \
             patch.object(ga.graph, "draft_fingerprint", return_value=OTHER_FP), \
             redirect_stdout(buf):
            ga.cmd_schedule_send(args)
        assert not run.called, "a changed draft was queued"
        assert json.loads(buf.getvalue())["status"] == "changed"


def test_schedule_send_refuses_without_the_key_and_keeps_the_token():
    from unittest.mock import patch
    import argparse
    import graph_actions as ga
    with _sandbox(with_key=False) as (tmp, _posts):
        ga.PENDING_CONFIRMS_FILE = tmp / "pending-confirmations.json"
        ga.PENDING_CONFIRMS_LOCK = tmp / "pending-confirmations.lock"
        token = ga._create_confirmation("send", DRAFT, "test")
        args = argparse.Namespace(draft_id=DRAFT, send_after="2030-04-21T09:00",
                                  description="x", ledger_id=None,
                                  confirm_token=token)
        buf = io.StringIO()
        with patch("subprocess.run") as run, redirect_stdout(buf):
            ga.cmd_schedule_send(args)
        assert not run.called
        assert json.loads(buf.getvalue())["status"] == "blocked"
        # the user's one-time approval was not burned by our own misconfiguration
        assert token in ga._load_pending()


def test_check_keeps_an_entry_the_send_did_not_send():
    """send_draft answers "error" when another edit holds the draft lock (or
    any other failure that sent nothing). The entry stays queued, the ledger
    row stays open, and the owner is told it is tried again."""
    with _sandbox() as (_tmp, posts):
        tok = S.make_send_token(DRAFT, WHEN, FP)
        S._save_sends([_entry(tok)])
        with _fake_graph_client(result={"status": "error", "error": "draft is busy"}) as fake:
            out = _run_check()
        assert fake.calls == [(DRAFT, tok)]
        assert out["sent"] == 0 and out["remaining"] == 1
        assert len(S._load_sends()) == 1
        assert any("not sent yet" in p for p in posts), posts


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
