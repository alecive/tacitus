#!/usr/bin/env python3
"""Tests for the pre-briefing closure scrubber.

Two layers:

  - Layer A (acknowledgment classifier): unit tests for
    `acknowledgment_patterns.classify()`. Pinned positive and negative
    cases including the canonical edge "thanks for your reply, but I
    have a follow-up question" that must NOT be classified as an
    acknowledgment.

  - Layer B (scrubber end-to-end): drives `scrub_closures()` against
    a synthetic in-memory ledger DB with hand-built Outlook inbox /
    sent and Gmail-sent state. Asserts which entries get closed and
    with which reason.

The class of failure both layers exist for: a morning briefing that
surfaces items already resolved, because the user replied via Outlook,
replied via Gmail, or the recipient sent "thanks for submitting".
"""

import json
import sqlite3
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import acknowledgment_patterns as ack  # noqa: E402
import closure_scrubber as cs  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import log_missed_closure as lmc  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Log isolation
# ────────────────────────────────────────────────────────────────────
# Four of these tests run the scrubber in LIVE mode, which appends to
# closure_scrubber.LOG_PATH -- the file closure_metrics treats as ground
# truth. Isolation belongs to the tests rather than to the __main__
# runner below, so it holds under any runner and no synthetic id reaches
# the production audit log.

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _isolate_logs(tmp_path, monkeypatch):
        monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
        monkeypatch.setattr(cs, "LOG_PATH", tmp_path / "closure-scrubber.log")
        monkeypatch.setattr(lmc, "LOG_PATH", tmp_path / "missed-closures.log")
except ImportError:  # pragma: no cover - script mode; _call_test isolates
    pass


# ────────────────────────────────────────────────────────────────────
# Layer A: acknowledgment_patterns.classify()
# ────────────────────────────────────────────────────────────────────


def _fired(result: dict) -> int:
    """Rows a signal fired on: closed on decisive evidence or proposed
    (closure.py decides which). Detection is what these tests pin; the
    policy has its own tests in test_closure_policy.py."""
    return int(result.get("closed", 0)) + int(result.get("proposed", 0))

def test_ack_thanks_for_submitting():
    is_ack, label = ack.classify("Thanks for submitting your proposal — we'll be in touch.")
    assert is_ack and label == "submission-ack", (is_ack, label)


def test_ack_we_will_be_in_touch():
    is_ack, _ = ack.classify("Got it. We will be in touch.")
    assert is_ack


def test_ack_received_your_application():
    is_ack, label = ack.classify("Received your application. We will review and respond.")
    assert is_ack, (is_ack, label)


def test_ack_portal_submission_recorded():
    is_ack, label = ack.classify("Your submission has been received.")
    assert is_ack and label == "portal-receipt-ack"


def test_ack_terse_thanks():
    is_ack, label = ack.classify("Thanks!")
    assert is_ack and label == "terse-ack"


def test_ack_got_it():
    is_ack, _ = ack.classify("Got it.")
    assert is_ack


def test_ack_no_further_action_needed():
    is_ack, _ = ack.classify("All set, thanks!")
    assert is_ack


def test_ack_ooo():
    is_ack, label = ack.classify("I'm out of office until June 3.")
    assert is_ack and label == "ooo"


def test_negative_guard_but_followup_question():
    # The canonical user-flagged edge case. Must NOT close.
    is_ack, _ = ack.classify("Thanks for your reply, but I have a follow-up question.")
    assert not is_ack


def test_negative_guard_trailing_question_mark():
    is_ack, _ = ack.classify("Got it — can you also send the budget?")
    assert not is_ack


def test_negative_guard_can_you_also():
    is_ack, _ = ack.classify("Thanks for submitting. Can you also share the slides?")
    assert not is_ack


def test_negative_no_ack_present():
    is_ack, _ = ack.classify("Hi Kit, when is the meeting?")
    assert not is_ack


def test_empty_input():
    is_ack, label = ack.classify("")
    assert not is_ack and label is None
    is_ack, label = ack.classify(None)
    assert not is_ack and label is None


def test_negative_forward_buried_ask():
    # An OOO-shaped opener followed by a forwarded ask should NOT close.
    text = ("I am currently out of office until June 3. "
            "Forwarded below — please send the budget when you can.")
    is_ack, _ = ack.classify(text)
    assert not is_ack


# ────────────────────────────────────────────────────────────────────
# Layer A′: the false-close corpus
# ────────────────────────────────────────────────────────────────────
# Bodies drawn from recorded production evidence. None may classify as
# an acknowledgment: a polite opener or a delivered confirmation
# followed by a NEW ask leaves the thread open.

B4B04_FALSE_CLOSE_CORPUS = [
    # confirmation-ack: the word appears, but the confirmation is the
    # thing being ASKED FOR, or an ask follows the delivered one.
    "We need your confirmation before we can proceed with the contract.",
    ("Attached is the confirmation number for the room. I still need your "
     "signed W-9 before payment can be issued."),
    ("Confirmation of receipt is not the same as approval; the panel still "
     "requires your rebuttal."),
    # ack-short: polite opener, then a whole new message body.
    ("Ok. The committee meeting moved to Thursday and we need your slides "
     "by Wednesday noon."),
    ("Sounds good. I'll need the letter on department letterhead, signed, "
     "before we can submit."),
    ("Noted. Three action items for you: review the budget, sign the MOU, "
     "and confirm the travel dates."),
    "Will do. In the meantime please review the attached draft and mark it up.",
    "Got it, I'll expect the revised budget from you by Monday.",
    # receipt-ack + a deadline and an ask.
    ("I have your request for a letter of recommendation. The deadline is "
     "Sept 1 and I need your CV."),
    # ooo + an imperative.
    ("I am out of office until June 3. My colleague Dana will not be "
     "covering; hold everything for me."),
    # the corpus control: a plain question, never an acknowledgment.
    "Hi Kit, when is the meeting?",
]

# Acknowledgments that MUST keep closing. Every case pinned by the tests
# above and by the module self-test, plus "Purchase Confirmation", a live
# subject shape behind a production closure.
B4B04_TRUE_POSITIVE_CORPUS = [
    "Thanks for submitting your proposal — we'll be in touch.",
    "Got it. We will be in touch.",
    "Received your application. We will review and respond.",
    "Your submission has been received.",
    "Thanks!",
    "Got it.",
    "All set, thanks!",
    "I'm out of office until June 3.",
    "Confirmation of your registration is attached.",
    "Thank you for registering. Your registration is confirmed.",
    "Submitted successfully.",
    "Purchase Confirmation",
    "Noted, thanks Kit.",
    "Your proposal has been successfully recorded.",
]


def test_b4b04_false_close_corpus_no_longer_closes():
    still_closing = [(t, ack.classify(t)[1])
                     for t in B4B04_FALSE_CLOSE_CORPUS if ack.classify(t)[0]]
    assert not still_closing, still_closing


def test_b4b04_true_positives_still_close():
    broken = [t for t in B4B04_TRUE_POSITIVE_CORPUS if not ack.classify(t)[0]]
    assert not broken, broken


def test_confirmation_ack_group_is_not_optional():
    # The capture group after "confirmation" is required, not optional.
    assert not ack.classify("Please send your confirmation when you can.")[0]
    assert ack.classify("Confirmation number 8813 for your booking.")[0]
    assert ack.classify("Confirmation: your room is held.")[0]


def test_ack_short_tail_is_bounded():
    tail = "x" * (ack.ACK_SHORT_MAX_TAIL_CHARS + 5)
    assert not ack.classify(f"Noted. {tail}")[0]
    assert ack.classify("Noted, thanks Kit.")[0]


def test_question_mark_anywhere_vetoes():
    # A question mark anywhere vetoes, not only a trailing one.
    assert not ack.classify(
        "Got it. Can you confirm the date? The room is booked.")[0]


def test_support_boilerplate_question_does_not_veto():
    assert ack.classify(
        "Thank you for your submission. Questions? Contact the help desk.")[0]


def test_deadline_phrase_vetoes():
    assert not ack.classify("Noted. Draft is due by Friday.")[0]
    assert not ack.classify("Got it. The deadline is Sept 1.")[0]


# ────────────────────────────────────────────────────────────────────
# Layer B: scrub_closures() with synthetic state
# ────────────────────────────────────────────────────────────────────

def _new_db(tmp_path: Path) -> Path:
    """Build a fresh empty ledger DB at the given path."""
    db_path = tmp_path / "ledger.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.executescript(ledger_mod.SCHEMA)
    for m in ledger_mod.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    return db_path


def _add(db_path, entry_id, conv_id="", gmail_thread_id=None,
         date_identified="2026-05-25T10:00:00Z", description="test"):
    conn = sqlite3.connect(str(db_path), timeout=10)
    now = ledger_mod._now()
    conn.execute(
        """
        INSERT INTO ledger (id, type, status, description, from_email, from_name,
                           source_message_id, conversation_id, gmail_thread_id,
                           date_identified, last_action, notes, priority,
                           source, metadata, created_at, updated_at)
        VALUES (?, 'response_needed', 'open', ?, '', '', '', ?, ?, ?, '', '', NULL,
                NULL, '{}', ?, ?)
        """,
        (entry_id, description, conv_id, gmail_thread_id, date_identified, now, now),
    )
    conn.commit()
    conn.close()


def _proposal(db_path, entry_id) -> dict:
    """The close proposal an open row carries ({} when none)."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    row = conn.execute("SELECT metadata FROM ledger WHERE id = ?", (entry_id,)).fetchone()
    conn.close()
    return (json.loads((row or [None])[0] or "{}") or {}).get("close_proposal") or {}


def _proposed(db_path, entry_id, signal: str) -> bool:
    """Open, with a proposal from `signal`: what a non-decisive signal does."""
    return (_status(db_path, entry_id)[0] == "open"
            and _proposal(db_path, entry_id).get("signal", "").startswith(signal))


def _status(db_path, entry_id):
    conn = sqlite3.connect(str(db_path), timeout=10)
    row = conn.execute("SELECT status, last_action FROM ledger WHERE id = ?",
                       (entry_id,)).fetchone()
    conn.close()
    return row


def test_outlook_reply_closes(tmp_path):
    db_path = _new_db(tmp_path)
    _add(db_path, "e-outlook-reply", conv_id="convA",
         date_identified="2026-05-25T10:00:00Z",
         description="Email from collaborator — needs response")
    open_entries = ledger_mod.get_open_entries(db_path=db_path)
    sent = [{"conversationId": "convA", "sentDateTime": "2026-05-26T15:30:00Z"}]
    inbox: list = []
    result = cs.scrub_closures(
        open_entries=open_entries, outlook_sent=sent, outlook_inbox=inbox,
        db_path=db_path,
    )
    assert _fired(result) == 1, result
    assert any("user_replied_outlook" in c["reason"] for c in result["closures"])
    status, last_action = _status(db_path, "e-outlook-reply")
    assert status == "completed"
    assert "user_replied_outlook" in last_action


def _add_from(db_path, entry_id, *, conv_id, from_email,
              date_identified="2026-09-22T16:50:03Z", description="test"):
    """A response_needed row that knows who it came from."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    now = ledger_mod._now()
    conn.execute(
        """INSERT INTO ledger (id, type, status, description, from_email,
               from_name, source_message_id, conversation_id, gmail_thread_id,
               date_identified, last_action, notes, priority, source, metadata,
               created_at, updated_at)
           VALUES (?, 'response_needed', 'open', ?, ?, '', 'MSG', ?, NULL, ?,
                   '', '', NULL, NULL, '{}', ?, ?)""",
        (entry_id, description, from_email, conv_id, date_identified, now, now))
    conn.commit()
    conn.close()


def _sent_in_conv(conv_id, to, sent_at, *, cc=()):
    return {
        "conversationId": conv_id,
        "subject": "Re: Access to XRL Ground Autonomy Stack",
        "sentDateTime": sent_at,
        "from": {"emailAddress": {"address": "wren.fairbanks84@example.edu"}},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
    }


def test_outlook_forward_to_third_parties_does_not_close(tmp_path):
    """Seren Ivers's access instructions were forwarded to four students
    within the hour. The sent message sits in her conversation, so the
    time index alone reads it as a reply to her and closes the row; she
    never heard back and the access steps went undone. A send that names
    recipients and leaves the correspondent off every one of them is a
    forward, and the row stays open, with the hold on the near-miss log."""
    db_path = _new_db(tmp_path)
    _add_from(db_path, "e-forward", conv_id="conv-frl",
              from_email="oakley.lindqvist50@example.com")
    sent = [_sent_in_conv("conv-frl",
                          ["dov.pemberton79@example.edu", "wren.abernathy53@example.edu",
                           "gus.umberfield51@example.edu", "noor.oyelaran83@example.edu"],
                          "2026-09-22T17:14:25Z")]
    result = cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path),
        outlook_sent=sent, outlook_inbox=[], db_path=db_path)
    assert _fired(result) == 0, result
    assert _status(db_path, "e-forward")[0] == "open"
    held = [n for n in result.get("suppressed", [])
            if n["id"] == "e-forward" and n["signal"] == "user_replied_outlook"]
    assert held and "forward only" in held[0]["suppressed_because"], result


def test_outlook_reply_that_includes_the_correspondent_closes(tmp_path):
    """The same conversation, but the user's send lists the correspondent
    (on CC here): that is a reply and closes the row. A forward followed
    by a real reply closes too."""
    db_path = _new_db(tmp_path)
    _add_from(db_path, "e-reply-cc", conv_id="conv-frl",
              from_email="oakley.lindqvist50@example.com")
    sent = [
        _sent_in_conv("conv-frl", ["dov.pemberton79@example.edu"],
                      "2026-09-22T17:14:25Z"),
        _sent_in_conv("conv-frl", ["dov.pemberton79@example.edu"],
                      "2026-09-23T09:00:00Z", cc=["oakley.lindqvist50@example.com"]),
    ]
    result = cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path),
        outlook_sent=sent, outlook_inbox=[], db_path=db_path)
    assert _fired(result) == 1, result
    assert "user_replied_outlook" in result["closures"][0]["reason"]


def test_outlook_send_without_recipient_data_still_closes(tmp_path):
    """A sent record that carries no recipients at all (the shape older
    logs and the corpus worlds have) keeps closing on the time index."""
    db_path = _new_db(tmp_path)
    _add_from(db_path, "e-no-rcpt", conv_id="conv-frl",
              from_email="oakley.lindqvist50@example.com")
    sent = [{"conversationId": "conv-frl", "sentDateTime": "2026-09-22T17:14:25Z"}]
    result = cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path),
        outlook_sent=sent, outlook_inbox=[], db_path=db_path)
    assert _fired(result) == 1, result


def test_gmail_reply_closes(tmp_path, monkeypatch):
    db_path = _new_db(tmp_path)
    _add(db_path, "e-gmail-reply",
         gmail_thread_id="g-thread-1",
         date_identified="2026-05-25T10:00:00Z")
    open_entries = ledger_mod.get_open_entries(db_path=db_path)
    # Mock the Gmail index builder and thread reader so no real API fires.
    # The thread carries an inbound message older than the user's sent
    # mail — the shape the reply verification requires.
    monkeypatch.setattr(cs, "_build_gmail_sent_index",
                        lambda account, days_back=60: {"g-thread-1":
                                                       "2026-05-26T18:00:00Z"})
    import gmail_client
    monkeypatch.setattr(gmail_client, "get_thread",
                        lambda email, tid: {"thread_id": tid, "messages": [
                            {"from": "Someone <someone@example.com>",
                             "date": "Mon, 25 May 2026 08:00:00 -0600"},
                            {"from": "Me <cam.yarborough33@example.com>",
                             "date": "Tue, 26 May 2026 12:00:00 -0600"},
                        ]})
    result = cs.scrub_closures(
        open_entries=open_entries, outlook_sent=[], outlook_inbox=[],
        gmail_accounts=["cam.yarborough33@example.com"], db_path=db_path,
    )
    assert _fired(result) == 1, result
    assert any("user_replied_gmail" in c["reason"] for c in result["closures"])


def test_index_gmail_sent_prefilters_api_direct_outreach():
    """A sent message whose id equals its thread_id started the thread
    (API-direct sends keep the composer's id). The whole thread stays out
    of the reply index — a later nudge on it included. This prefilter is
    free but NOT sufficient: draft-composed outreach is sent under a
    different id (see the thread-verification tests below)."""
    idx = cs._index_gmail_sent([
        {"id": "g-start-1", "thread_id": "g-start-1",
         "date": "Mon, 31 Aug 2026 14:56:31 -0600"},
        {"id": "g-nudge-2", "thread_id": "g-start-1",
         "date": "Wed, 02 Sep 2026 09:00:00 -0600"},
        {"id": "g-reply-9", "thread_id": "g-thread-2",
         "date": "Fri, 21 Aug 2026 09:12:00 -0600"},
    ])
    assert "g-start-1" not in idx, idx
    assert "g-thread-2" in idx


def test_gmail_draft_composed_outreach_does_not_close(tmp_path):
    """When the user STARTED the thread from a draft, Gmail pins the
    thread id to the auto-saved draft, so the sent copy's id differs from
    the thread id and the prefilter cannot see it. The thread holds only
    the user's message: nothing was answered, so nothing closes."""
    db_path = _new_db(tmp_path)
    _add(db_path, "e-outreach", gmail_thread_id="g-thread-draft",
         date_identified="2026-08-31T20:48:13Z")
    open_entries = ledger_mod.get_open_entries(db_path=db_path)
    result = cs.scrub_closures(
        open_entries=open_entries, outlook_sent=[], outlook_inbox=[],
        gmail_sent={"kit.kowalczyk1@example.com": [
            {"id": "g-sent-copy", "thread_id": "g-thread-draft",
             "date": "Mon, 31 Aug 2026 14:56:31 -0600"},
        ]},
        gmail_threads={"kit.kowalczyk1@example.com": {"g-thread-draft": [
            {"from": "Me <kit.kowalczyk1@example.com>",
             "date": "Mon, 31 Aug 2026 14:56:31 -0600"},
        ]}},
        db_path=db_path,
    )
    assert _fired(result) == 0, result
    assert _status(db_path, "e-outreach")[0] == "open"


def test_gmail_unverifiable_thread_does_not_close(tmp_path):
    """A candidate thread the fetcher cannot read must hold, not close.
    Closing on the sent index alone is a close with no verification."""
    db_path = _new_db(tmp_path)
    _add(db_path, "e-unverifiable", gmail_thread_id="g-thread-x",
         date_identified="2026-08-20T10:00:00Z")
    open_entries = ledger_mod.get_open_entries(db_path=db_path)
    result = cs.scrub_closures(
        open_entries=open_entries, outlook_sent=[], outlook_inbox=[],
        gmail_sent={"kit.kowalczyk1@example.com": [
            {"id": "g-reply-9", "thread_id": "g-thread-x",
             "date": "Fri, 21 Aug 2026 09:12:00 -0600"},
        ]},
        gmail_threads={"kit.kowalczyk1@example.com": {}},   # thread not readable
        db_path=db_path,
    )
    assert _fired(result) == 0, result


def test_gmail_inbound_only_after_users_sent_does_not_close():
    """An inbound message that POSTDATES the user's latest sent mail is
    not what the user replied to. It is the answer coming back, so it is
    not a reply by the user."""
    entry = {"gmail_thread_id": "g-t", "date_identified": "2026-08-20T10:00:00Z"}
    indices = {"kit.kowalczyk1@example.com": {"g-t": "2026-08-21T15:12:00Z"}}
    threads = {"g-t": [
        {"from": "Me <kit.kowalczyk1@example.com>",
         "date": "Fri, 21 Aug 2026 09:12:00 -0600"},
        {"from": "Engineer <finley.valentijn50@example.com>",
         "date": "Sat, 22 Aug 2026 10:00:00 -0600"},
    ]}
    fired, _ = cs._user_replied_gmail(
        entry, indices, thread_fetch=lambda a, t: threads.get(t))
    assert not fired


def test_gmail_injected_reply_still_closes(tmp_path):
    """The injectable gmail_sent/gmail_threads path must close a genuine
    reply (thread started by someone else, inbound predating the user's
    sent mail) exactly like the API path does."""
    db_path = _new_db(tmp_path)
    _add(db_path, "e-reply", gmail_thread_id="g-thread-2",
         date_identified="2026-08-20T10:00:00Z")
    open_entries = ledger_mod.get_open_entries(db_path=db_path)
    result = cs.scrub_closures(
        open_entries=open_entries, outlook_sent=[], outlook_inbox=[],
        gmail_sent={"kit.kowalczyk1@example.com": [
            {"id": "g-reply-9", "thread_id": "g-thread-2",
             "date": "Fri, 21 Aug 2026 09:12:00 -0600"},
        ]},
        gmail_threads={"kit.kowalczyk1@example.com": {"g-thread-2": [
            {"from": "Someone <someone@example.com>",
             "date": "Thu, 20 Aug 2026 08:30:00 -0600"},
            {"from": "Me <kit.kowalczyk1@example.com>",
             "date": "Fri, 21 Aug 2026 09:12:00 -0600"},
        ]}},
        db_path=db_path,
    )
    assert _fired(result) == 1, result
    assert "user_replied_gmail" in result["closures"][0]["reason"]


def test_user_replied_close_reopened_is_vetoed_not_reclosed(tmp_path):
    """A user_replied close is recorded in the veto store, so a human
    reopen creates a standing veto for that signal. Without it the same
    sent mail re-closes the entry on every subsequent run."""
    db_path = _new_db(tmp_path)
    veto_path = tmp_path / "vetoes.json"
    _add(db_path, "e-reclose", gmail_thread_id="g-thread-3",
         date_identified="2026-08-20T10:00:00Z")
    world = dict(
        outlook_sent=[], outlook_inbox=[],
        gmail_sent={"kit.kowalczyk1@example.com": [
            {"id": "g-reply-1", "thread_id": "g-thread-3",
             "date": "Fri, 21 Aug 2026 09:12:00 -0600"}]},
        gmail_threads={"kit.kowalczyk1@example.com": {"g-thread-3": [
            {"from": "Someone <someone@example.com>",
             "date": "Thu, 20 Aug 2026 08:30:00 -0600"},
            {"from": "Me <kit.kowalczyk1@example.com>",
             "date": "Fri, 21 Aug 2026 09:12:00 -0600"},
        ]}},
        db_path=db_path, veto_store_path=veto_path,
    )
    r1 = cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path), **world)
    assert _fired(r1) == 1, r1
    assert _status(db_path, "e-reclose")[0] == "completed"

    # The human reopens it.
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("UPDATE ledger SET status = 'follow_up' WHERE id = ?",
                 ("e-reclose",))
    conn.commit()
    conn.close()

    r2 = cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path), **world)
    assert _fired(r2) == 0, (
        "the reopened entry was re-closed on the same evidence", r2)
    assert any(v["id"] == "e-reclose" and v["signal"] == "user_replied_gmail"
               for v in r2["new_vetoes"]), r2["new_vetoes"]
    assert _status(db_path, "e-reclose")[0] == "follow_up"


def test_a_reopen_outranks_evidence_older_than_it(tmp_path):
    """The PSA white paper: a send button closed it on the action-items
    reply, the EA reopened it, and user_replied_outlook closed it again on
    that same reply, because a close the scrubber did not make leaves no
    veto record. `ledger.py update --status open` stamps reopened_at, and
    only evidence after it counts. A later send still closes it."""
    import os
    import subprocess
    ea = tmp_path / "ea"
    (ea / "state").mkdir(parents=True)
    db_path = ea / "state" / "obligations-ledger.db"
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.executescript(ledger_mod.SCHEMA)
    for m in ledger_mod.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    _add_full(db_path, "field-trial-meridian-psa-whitepaper", conv_id="convC",
              entry_type="task", date_identified="2026-10-06T18:16:36Z",
              status="completed", closed_at="2026-10-06T22:17:56Z")
    r = subprocess.run(
        [sys.executable, str(HERE.parent / "preprocessor" / "ledger.py"), "update",
         "--id", "field-trial-meridian-psa-whitepaper", "--status", "open",
         "--last-action", "reopened: the reply was not the white paper"],
        env=dict(os.environ, EA_DIR=str(ea)), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    entry = ledger_mod.get_entry("field-trial-meridian-psa-whitepaper", db_path=db_path)
    assert entry["reopened_at"] > "2026-10-06T22:17:33Z", entry

    action_items = [{"conversationId": "convC", "sentDateTime": "2026-10-06T22:17:33Z"}]
    r1 = cs.scrub_closures(open_entries=ledger_mod.get_open_entries(db_path=db_path),
                           outlook_sent=action_items, outlook_inbox=[],
                           db_path=db_path, veto_store_path=tmp_path / "v.json")
    assert _fired(r1) == 0, r1

    later = action_items + [{"conversationId": "convC",
                             "sentDateTime": "2099-01-01T00:00:00Z"}]
    r2 = cs.scrub_closures(open_entries=ledger_mod.get_open_entries(db_path=db_path),
                           outlook_sent=later, outlook_inbox=[],
                           db_path=db_path, veto_store_path=tmp_path / "v.json")
    assert _fired(r2) == 1 and "user_replied_outlook" in r2["closures"][0]["reason"], r2


def test_the_users_own_send_never_closes_a_waiting_row(tmp_path):
    """Meridian owes the CONOPS. Kit's reply in the thread (the
    action-items mail) is not their delivery, by Outlook or by Gmail; the
    response row on the same thread does close."""
    db_path = _new_db(tmp_path)
    for rid, kind in (("field-trial-meridian-pilot-conops", "waiting"),
                      ("response-psa", "response_needed")):
        _add_full(db_path, rid, conv_id="convC", entry_type=kind,
                  date_identified="2026-10-06T18:16:36Z")
    _add_full(db_path, "yarrow-outreach", conv_id="convG", entry_type="awaiting",
              date_identified="2026-08-20T10:00:00Z")
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("UPDATE ledger SET gmail_thread_id = 'g-wait' WHERE id = 'yarrow-outreach'")
    conn.commit()
    conn.close()
    result = cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path),
        outlook_sent=[{"conversationId": "convC", "sentDateTime": "2026-10-06T22:17:33Z"}],
        outlook_inbox=[],
        gmail_sent={"kit.kowalczyk1@example.com": [
            {"id": "g-1", "thread_id": "g-wait", "date": "Fri, 21 Aug 2026 09:12:00 -0600"}]},
        gmail_threads={"kit.kowalczyk1@example.com": {"g-wait": [
            {"from": "Yarrow <yarrow@example.com>", "date": "Thu, 20 Aug 2026 08:30:00 -0600"},
            {"from": "Me <kit.kowalczyk1@example.com>", "date": "Fri, 21 Aug 2026 09:12:00 -0600"}]}},
        db_path=db_path, veto_store_path=tmp_path / "v.json")
    assert [c["id"] for c in result["closures"]] == ["response-psa"], result
    assert _status(db_path, "field-trial-meridian-pilot-conops")[0] == "open"
    assert _status(db_path, "yarrow-outreach")[0] == "open"


def test_banner_vocabulary_does_not_close(tmp_path):
    """The 'CAUTION: external sender' banner appears in both an entry's
    notes (stored body previews) and an inbound preview, yielding overlaps
    like ['caution','email','external']. Banner vocabulary lives in
    _GENERIC_NOISE_TOKENS, so such an overlap closes nothing."""
    db_path = _new_db(tmp_path)
    _add(db_path, "e-banner-noise", conv_id="convZ",
         date_identified="2026-05-25T10:00:00Z",
         description="Needs response: Share Your Research: Blog Opportunity")
    # Plant the banner text in notes, the way briefing-created entries
    # store body previews.
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute(
        "UPDATE ledger SET notes = ? WHERE id = ?",
        ("CAUTION: This email originated from outside of the organization. "
         "Do not click links or open attachments unless you recognize the "
         "sender and know the content is safe.", "e-banner-noise"))
    conn.commit()
    conn.close()
    open_entries = ledger_mod.get_open_entries(db_path=db_path)
    inbox = [{
        "from": {"emailAddress": {"address": "noor.ambrose88@example.com"}},
        "receivedDateTime": "2026-06-05T01:00:00Z",
        "subject": "Registration confirmation for NSF panel P900022",
        "bodyPreview": "CAUTION: This email originated from outside of the "
                       "organization. Do not click links or open attachments "
                       "unless you recognize the sender.",
    }]
    result = cs.scrub_closures(
        open_entries=open_entries, outlook_sent=[], outlook_inbox=inbox,
        db_path=db_path,
    )
    assert _fired(result) == 0, (
        f"banner-vocabulary overlap must not close entries: {result}")
    status, _ = _status(db_path, "e-banner-noise")
    assert status == "open"


def test_inbound_ack_closes_orbx_style(tmp_path):
    """RIO-style 'thanks for submitting' closes the entry even with no
    user reply (the ORBX portal-submission case)."""
    db_path = _new_db(tmp_path)
    _add(db_path, "e-orbx", conv_id="convORBX",
         date_identified="2026-05-25T10:00:00Z",
         description="Submit ORBX proposal")
    open_entries = ledger_mod.get_open_entries(db_path=db_path)
    inbox = [
        {
            "conversationId": "convORBX",
            "receivedDateTime": "2026-05-26T16:00:00Z",
            "subject": "Thanks for submitting!",
            "bodyPreview": "Thanks for submitting your AI Discovery proposal — we'll be in touch.",
            "from": {"emailAddress": {"address": "wren.isenberg76@example.edu"}},
        },
    ]
    result = cs.scrub_closures(
        open_entries=open_entries, outlook_sent=[], outlook_inbox=inbox,
        db_path=db_path,
    )
    assert _fired(result) == 1, result
    assert any("inbound_ack" in c["reason"] for c in result["closures"])


def test_a_refused_reply_does_not_hide_a_later_signal(tmp_path):
    """The user kept the row open after their own reply. That reply still
    fires first on every run; the check moves past it to the next signal,
    and the row, kept open once, gets a proposal, never a close."""
    db_path = _new_db(tmp_path)
    _add(db_path, "e-kept", conv_id="convKept",
         date_identified="2026-05-25T10:00:00Z", description="Submit ORBX proposal")
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("UPDATE ledger SET metadata = ? WHERE id = 'e-kept'", (json.dumps(
        {"close_vetoes": {"user_replied_outlook": "2026-05-27T00:00:00Z",
                          "*": "2026-05-27T00:00:00Z"}}),))
    conn.commit()
    conn.close()
    sent = [{"conversationId": "convKept", "sentDateTime": "2026-05-26T15:30:00Z"}]
    inbox = [{
        "conversationId": "convKept",
        "receivedDateTime": "2026-05-28T16:00:00Z",
        "subject": "Thanks for submitting!",
        "bodyPreview": "Thanks for submitting your AI Discovery proposal — we'll be in touch.",
        "from": {"emailAddress": {"address": "wren.isenberg76@example.edu"}},
    }]
    result = cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path),
        outlook_sent=sent, outlook_inbox=inbox, db_path=db_path)
    assert [c["action"] for c in result["closures"]] == ["proposed"], result
    assert result["closures"][0]["reason"].startswith("inbound_ack")
    assert _proposed(db_path, "e-kept", "inbound_ack")


def test_negative_guard_prevents_false_close(tmp_path):
    db_path = _new_db(tmp_path)
    _add(db_path, "e-followup", conv_id="convFollow",
         date_identified="2026-05-25T10:00:00Z",
         description="Open thread — user must answer")
    open_entries = ledger_mod.get_open_entries(db_path=db_path)
    inbox = [
        {
            "conversationId": "convFollow",
            "receivedDateTime": "2026-05-26T16:00:00Z",
            "subject": "Quick follow-up",
            "bodyPreview": "Thanks for your reply, but I have a follow-up question.",
            "from": {"emailAddress": {"address": "someone@example.com"}},
        },
    ]
    result = cs.scrub_closures(
        open_entries=open_entries, outlook_sent=[], outlook_inbox=inbox,
        db_path=db_path,
    )
    assert _fired(result) == 0, result
    status, _ = _status(db_path, "e-followup")
    assert status == "open"


def test_b4b04_polite_opener_plus_new_ask_does_not_close(tmp_path):
    """End-to-end version of the false-close corpus: every entry stays
    open when driven through the real closer."""
    db_path = _new_db(tmp_path)
    inbox = []
    for i, body in enumerate(B4B04_FALSE_CLOSE_CORPUS):
        eid, conv = f"e-b4b04-{i}", f"convB4b04-{i}"
        _add(db_path, eid, conv_id=conv,
             date_identified="2026-05-25T10:00:00Z",
             description="Open thread — user must act")
        inbox.append({
            "conversationId": conv,
            "receivedDateTime": "2026-05-26T16:00:00Z",
            "subject": "Re: budget",
            "bodyPreview": body,
            "from": {"emailAddress": {"address": "ari.calloway10@example.edu"}},
        })
    open_entries = ledger_mod.get_open_entries(db_path=db_path)
    result = cs.scrub_closures(
        open_entries=open_entries, outlook_sent=[], outlook_inbox=inbox,
        db_path=db_path,
    )
    assert _fired(result) == 0, result["closures"]
    for i in range(len(B4B04_FALSE_CLOSE_CORPUS)):
        assert _status(db_path, f"e-b4b04-{i}")[0] == "open"


def test_entry_older_than_reply_only_closes_if_reply_is_newer(tmp_path):
    """A user reply BEFORE the entry's date_identified shouldn't close —
    that reply belongs to an earlier turn."""
    db_path = _new_db(tmp_path)
    _add(db_path, "e-old-reply", conv_id="convOld",
         date_identified="2026-05-26T10:00:00Z")  # entry is newer
    open_entries = ledger_mod.get_open_entries(db_path=db_path)
    sent = [{"conversationId": "convOld",
             "sentDateTime": "2026-05-25T10:00:00Z"}]  # reply is older
    result = cs.scrub_closures(
        open_entries=open_entries, outlook_sent=sent, outlook_inbox=[],
        db_path=db_path,
    )
    assert _fired(result) == 0, result


def test_dry_run_does_not_mutate(tmp_path):
    db_path = _new_db(tmp_path)
    _add(db_path, "e-dry", conv_id="convDry")
    open_entries = ledger_mod.get_open_entries(db_path=db_path)
    sent = [{"conversationId": "convDry", "sentDateTime": "2026-05-26T15:30:00Z"}]
    result = cs.scrub_closures(
        open_entries=open_entries, outlook_sent=sent, outlook_inbox=[],
        dry_run=True, db_path=db_path,
    )
    assert _fired(result) == 1, result
    assert result["dry_run"] is True
    status, _ = _status(db_path, "e-dry")
    assert status == "open", "dry_run must not mutate the DB"


# ────────────────────────────────────────────────────────────────────
# Standalone runner (compatible with the post-commit hook pattern)
# ────────────────────────────────────────────────────────────────────

class _FakeMonkeypatch:
    """Minimal monkeypatch shim so tests can run without pytest."""

    def __init__(self):
        self._undos: list = []

    def setattr(self, target, name, value):
        if isinstance(target, str):
            raise NotImplementedError("string-target setattr not supported")
        prev = getattr(target, name)
        self._undos.append((target, name, prev))
        setattr(target, name, value)

    def undo(self):
        for target, name, prev in reversed(self._undos):
            setattr(target, name, prev)
        self._undos.clear()


def _call_test(fn, tmp_path: Path) -> None:
    """Call a test with whichever fixtures it asks for (tmp_path / monkeypatch).

    Also redirects the scrubber's audit log into tmp_path for the duration
    of the test so test runs don't pollute ~/.ea/logs/closure-scrubber.log
    with synthetic fixture IDs (e-orbx, e-outlook-reply, ...).
    """
    import inspect
    params = list(inspect.signature(fn).parameters)
    args = []
    mp = None
    for p in params:
        if p == "tmp_path":
            args.append(tmp_path)
        elif p == "monkeypatch":
            mp = _FakeMonkeypatch()
            args.append(mp)
        else:
            raise RuntimeError(f"Unknown fixture: {p}")
    # Always isolate the audit log path, even for tests that didn't ask
    # for monkeypatch. EA_DIR goes with it: the scrubber also writes a
    # veto state file under it, and a script-mode run with the real
    # EA_DIR would seed production state with fixture ids.
    import os as _os
    _local_mp = mp or _FakeMonkeypatch()
    _local_mp.setattr(cs, "LOG_PATH", tmp_path / "closure-scrubber.log")
    _local_mp.setattr(lmc, "LOG_PATH", tmp_path / "missed-closures.log")
    _prev_ea = _os.environ.get("EA_DIR")
    _os.environ["EA_DIR"] = str(tmp_path / "ea")
    try:
        fn(*args)
    finally:
        if _prev_ea is None:
            _os.environ.pop("EA_DIR", None)
        else:
            _os.environ["EA_DIR"] = _prev_ea
        _local_mp.undo()
        if mp is not None and mp is not _local_mp:
            mp.undo()


# ── Signal 6: closed-sibling sweep ─────────────────────────────────────

def _add_full(db_path, entry_id, *, conv_id, status="open",
              entry_type="response_needed", source_message_id="MSG",
              date_identified="2026-05-25T10:00:00Z", closed_at=None):
    conn = sqlite3.connect(str(db_path), timeout=10)
    now = ledger_mod._now()
    conn.execute(
        """INSERT INTO ledger (id, type, status, description, from_email,
               from_name, source_message_id, conversation_id, gmail_thread_id,
               date_identified, last_action, notes, priority, source, metadata,
               created_at, updated_at, closed_at)
           VALUES (?, ?, ?, 'test', '', '', ?, ?, NULL, ?, '', '', NULL, NULL,
                   '{}', ?, ?, ?)""",
        (entry_id, entry_type, status, source_message_id, conv_id,
         date_identified, now, now, closed_at))
    conn.commit()
    conn.close()


def test_closed_sibling_sweeps_survivor(tmp_path):
    """A detector sibling closed AFTER the survivor existed closes the survivor."""
    db_path = _new_db(tmp_path)
    _add_full(db_path, "response-surv", conv_id="convS",
              date_identified="2026-05-25T10:00:00Z")
    _add_full(db_path, "priority-done", conv_id="convS", status="closed",
              closed_at="2026-05-26T09:00:00Z")
    result = cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path),
        outlook_sent=[], outlook_inbox=[], db_path=db_path)
    assert _fired(result) == 1, result
    assert _proposed(db_path, "response-surv", "closed_sibling"), _status(db_path, "response-surv")


def test_closed_sibling_leaves_the_eas_own_rows_on_the_thread_open(tmp_path):
    """A task and a wait the EA logged from one thread are their own
    obligations: the journal manuscript row shared its kickoff email's
    thread and was swept when that email's row closed. A
    source_message_id does not mark a duplicate; the EA links its rows
    to their thread too."""
    db_path = _new_db(tmp_path)
    _add_full(db_path, "task-journal-manuscript-first-pass", conv_id="convK",
              entry_type="task", date_identified="2026-08-11T17:16:09Z")
    _add_full(db_path, "field-trial-meridian-pilot-conops", conv_id="convK",
              entry_type="waiting", date_identified="2026-08-11T17:16:09Z")
    _add_full(db_path, "response-kickoff", conv_id="convK", status="closed",
              closed_at="2026-08-11T20:45:06Z")
    result = cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path),
        outlook_sent=[], outlook_inbox=[], db_path=db_path)
    assert _fired(result) == 0, result


def test_closed_sibling_skips_ea_workflow_entries(tmp_path):
    """draft_pending / no-source entries are NOT swept (Mesalands FP)."""
    db_path = _new_db(tmp_path)
    _add_full(db_path, "e-draft", conv_id="convD", entry_type="draft_pending",
              date_identified="2026-05-25T10:00:00Z")
    _add_full(db_path, "e-nosrc", conv_id="convD", source_message_id="",
              date_identified="2026-05-25T10:00:00Z")
    _add_full(db_path, "e-doneD", conv_id="convD", status="closed",
              closed_at="2026-05-26T09:00:00Z")
    result = cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path),
        outlook_sent=[], outlook_inbox=[], db_path=db_path)
    assert _fired(result) == 0, result


def test_closed_sibling_direction_guard(tmp_path):
    """A survivor born AFTER the sibling closure (new inbound) is left open."""
    db_path = _new_db(tmp_path)
    _add_full(db_path, "e-newask", conv_id="convN",
              date_identified="2026-05-27T10:00:00Z")   # born after closure
    _add_full(db_path, "e-doneN", conv_id="convN", status="closed",
              closed_at="2026-05-26T09:00:00Z")
    result = cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path),
        outlook_sent=[], outlook_inbox=[], db_path=db_path)
    assert _fired(result) == 0, result



def test_closed_sibling_honours_a_human_still_open(tmp_path):
    """closed_sibling proposes (closure.py). The user's "Still open" on
    the card vetoes it for that row, and the next run proposes nothing."""
    db_path = _new_db(tmp_path)
    vetoes = tmp_path / "vetoes.json"
    _add_full(db_path, "response-work", conv_id="convW",
              date_identified="2026-08-11T17:00:00Z")
    _add_full(db_path, "priority-kick", conv_id="convW", status="closed",
              closed_at="2026-08-11T20:45:06Z")
    r = cs.scrub_closures(open_entries=ledger_mod.get_open_entries(db_path=db_path),
                          outlook_sent=[], outlook_inbox=[], db_path=db_path,
                          veto_store_path=vetoes)
    assert _fired(r) == 1 and _proposed(db_path, "response-work", "closed_sibling")
    # the human says it is still open
    import closure
    conn = ledger_mod.guarded_connect(db_path)
    closure.reject(conn, "response-work")
    conn.commit()
    conn.close()
    r = cs.scrub_closures(open_entries=ledger_mod.get_open_entries(db_path=db_path),
                          outlook_sent=[], outlook_inbox=[], db_path=db_path,
                          veto_store_path=vetoes)
    assert _fired(r) == 0 and _status(db_path, "response-work")[0] == "open", r
    assert not _proposal(db_path, "response-work")

# ── Signal 4: cross-thread confirmation email ──────────────────────────
#
# Two ways one message closes many entries: a domain-wide sender
# substring that also matches a human program officer, and an overlap
# test satisfied by ONE shared word. Suppressing the banner tokens alone
# leaves overlaps like ['registration'] and ['note'] live.

def _confirmation_inbox(addr="zion.dalby12@example.com",
                        subject="P900022 Example Unsolicited Panel - XX0010",
                        preview="Registration is complete."):
    return [{
        "from": {"emailAddress": {"address": addr}},
        "receivedDateTime": "2026-06-05T01:00:00Z",
        "subject": subject,
        "bodyPreview": preview,
    }]


def test_program_officer_at_nsf_is_not_a_confirmation_sender():
    """zion.dalby12@example.com is a human program officer. Their mail is action,
    not completion, and must not match the sender allowlist at all."""
    entry = {"id": "e1", "description": "P900022 FRR Unsolicited Panel review",
             "date_identified": "2026-06-01T10:00:00Z"}
    fired, reason = cs._confirmation_email_satisfies(
        entry, _confirmation_inbox())
    assert not fired, reason


def test_nsf_meeting_reminder_is_not_a_confirmation_sender():
    """sol.ambrose87@example.com is a reminder address. A reminder is the
    opposite of a completion, so it is not a confirmation sender."""
    entry = {"id": "e2",
             "description": "Needs response: [DUE] NSF Project Report for Award",
             "date_identified": "2026-05-20T10:00:00Z"}
    fired, reason = cs._confirmation_email_satisfies(
        entry, _confirmation_inbox(addr="sol.ambrose87@example.com"))
    assert not fired, reason


def test_single_token_overlap_does_not_close():
    """One shared content word never closes. 'osprey' is a real,
    non-generic token and is still not enough on its own."""
    entry = {"id": "e3", "description": "Submit Osprey budget narrative",
             "date_identified": "2026-06-01T10:00:00Z"}
    inbox = _confirmation_inbox(
        addr="noreply@nsf.gov",
        subject="Registration confirmed: Osprey workshop",
        preview="Your registration is complete.")
    fired, reason = cs._confirmation_email_satisfies(entry, inbox)
    assert not fired, reason


def test_two_token_overlap_still_closes():
    """The tightening must not make the signal unfireable. Two distinct
    non-generic tokens from a genuine noreply confirmation still close."""
    entry = {"id": "e4",
             "description": "Submit Osprey map-consistency manuscript",
             "date_identified": "2026-06-01T10:00:00Z"}
    inbox = _confirmation_inbox(
        addr="noreply@easychair.org",
        subject="Submission received: Osprey map-consistency",
        preview="Thank you for your submission.")
    fired, reason = cs._confirmation_email_satisfies(entry, inbox)
    assert fired, reason
    assert "confirmation_email" in reason


def test_residual_noise_tokens_are_suppressed():
    """`note` and `registration` were the two overlap tokens the first
    remediation missed; both closed a real obligation."""
    for tok in ("note", "registration", "register"):
        assert tok in cs._GENERIC_NOISE_TOKENS, tok


def test_fairford_shape_no_longer_closes(tmp_path):
    """End-to-end replay of `fairford-revisit-oct2026`: parked for Fall
    2027 recruitment, killed 4 months early on overlap=['note']."""
    db_path = _new_db(tmp_path)
    _add(db_path, "fairford-revisit", conv_id="convQ",
         date_identified="2026-05-25T10:00:00Z",
         description="Revisit Fairford PhD note in October")
    result = cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path),
        outlook_sent=[], outlook_inbox=_confirmation_inbox(
            subject="Panel note", preview="Registration complete."),
        db_path=db_path)
    assert _fired(result) == 0, result


# ── Missed-closure autopsies ───────────────────────────────────────────
#
# Under mail mediation the helper cannot import graph_client. Running the
# signals against EMPTY inbox/sent/calendar lists would record, in
# writing, that no closure signal was available, which is a false autopsy
# on the only feedback channel the scrubber has. A signal that could not
# be evaluated is reported as unevaluated.

def _seed_entry(tmp_path, monkeypatch, entry_id="e-miss"):
    db_path = _new_db(tmp_path)
    _add(db_path, entry_id, conv_id="convM",
         date_identified="2026-05-25T10:00:00Z",
         description="Submit ARSS map-consistency manuscript")
    monkeypatch.setattr(ledger_mod, "DB_PATH", db_path)
    monkeypatch.setattr(lmc, "LOG_PATH", tmp_path / "missed-closures.log")
    return db_path


def test_autopsy_marks_signals_unevaluated_when_graph_is_unreachable(
        tmp_path, monkeypatch):
    _seed_entry(tmp_path, monkeypatch)
    monkeypatch.setattr(lmc, "_fetch_evidence", lambda **kw: (
        [], [], [], {"route": "graph_actions CLI (graph_client unavailable: "
                              "PermissionError)",
                     "inbox": "unavailable: PermissionError",
                     "sent": "unavailable: PermissionError",
                     "calendar": "unavailable: PermissionError",
                     "unevaluable_signals": {
                         "user_replied_outlook": "sent unavailable",
                         "latest_inbound_is_ack": "inbox unavailable",
                         "confirmation_email": "inbox unavailable",
                         "calendar_event_match": "calendar unavailable"},
                     "complete": False}))
    rec = lmc.log_missed_closure(ledger_id="e-miss",
                                 user_message="ARSS was submitted.")
    by_name = {s["signal"]: s for s in rec["scrubber_signals_present"]}
    for name in ("user_replied_outlook", "latest_inbound_is_ack",
                 "confirmation_email", "calendar_event_match"):
        assert by_name[name]["would_fire"] is None, by_name[name]
        assert by_name[name]["evaluated"] is False, by_name[name]
    assert rec["evidence_complete"] is False
    assert rec["candidate_evidence_status"].startswith("not searched")


def test_autopsy_suggestion_is_inconclusive_not_no_signal(
        tmp_path, monkeypatch):
    """The exact false conclusion: 'No detectable signal in data' was
    written 20/20 times on records that never looked."""
    _seed_entry(tmp_path, monkeypatch)
    monkeypatch.setattr(lmc, "_fetch_evidence", lambda **kw: (
        [], [], [], {"route": "graph_actions CLI (graph_client unavailable: "
                              "PermissionError)",
                     "inbox": "unavailable: PermissionError",
                     "sent": "unavailable: PermissionError",
                     "calendar": "unavailable: PermissionError",
                     "unevaluable_signals": {
                         "confirmation_email": "inbox unavailable"},
                     "complete": False}))
    rec = lmc.log_missed_closure(ledger_id="e-miss",
                                 user_message="ARSS was submitted.")
    assert rec["suggested_signal"].startswith("INCONCLUSIVE"), (
        rec["suggested_signal"])
    assert "No detectable signal in data" not in rec["suggested_signal"]


def test_autopsy_still_concludes_when_the_search_really_ran(
        tmp_path, monkeypatch):
    """The other direction: a complete fetch that finds nothing keeps the
    original verdict, so the honest branch has not swallowed the useful
    one."""
    _seed_entry(tmp_path, monkeypatch)
    monkeypatch.setattr(lmc, "_fetch_evidence", lambda **kw: (
        [], [], [], {"route": "graph_client", "inbox": "ok (0 messages)",
                     "sent": "ok (0 messages)", "calendar": "ok (0 events)",
                     "unevaluable_signals": {}, "complete": True}))
    rec = lmc.log_missed_closure(ledger_id="e-miss",
                                 user_message="ARSS was submitted.")
    assert rec["evidence_complete"] is True
    assert all(s["evaluated"] for s in rec["scrubber_signals_present"])
    assert rec["suggested_signal"].startswith("No detectable signal")
    assert rec["candidate_evidence_status"].startswith("searched:")


def test_autopsy_no_fetch_does_not_claim_a_search(tmp_path, monkeypatch):
    _seed_entry(tmp_path, monkeypatch)
    rec = lmc.log_missed_closure(ledger_id="e-miss", user_message="done",
                                 no_fetch=True)
    assert rec["evidence_complete"] is False
    assert "not searched" in rec["candidate_evidence_status"]
    assert rec["suggested_signal"].startswith("INCONCLUSIVE")


def test_cli_message_normalization_keeps_the_confirmation_signal_usable():
    """The mediated fallback route yields a flattened shape. It must carry
    enough for the confirmation signal, plus the conversationId the two
    same-conversation signals join on."""
    rows = [{"id": "1", "subject": "Submission received",
             "from": "noreply@easychair.org", "received": "2026-06-05T01:00:00Z",
             "conversation_id": "AAQkCONV",
             "preview": "Thank you for your submission."},
            {"_note": "Older messages remain in inbox."}]
    out = lmc._normalize_cli_messages(rows)
    assert len(out) == 1
    assert out[0]["from"]["emailAddress"]["address"] == "noreply@easychair.org"
    assert out[0]["bodyPreview"] == "Thank you for your submission."
    assert out[0]["conversationId"] == "AAQkCONV"
    assert lmc._CLI_UNEVALUABLE == {}


def test_cli_message_normalization_carries_recipients():
    """sender_reply_match joins Sent mail on the recipient address, and
    under mail mediation list-mail is ea-bot's only route to Sent. The
    row carries to/cc so the signal is evaluable there; a graph_actions
    that omits them leaves the signal saying no."""
    out = lmc._normalize_cli_messages([{
        "id": "1", "subject": "RE: Pathway BD Invitation for Sept 3",
        "from": "wren.fairbanks84@example.edu",
        "received": "2026-08-24T15:51:07Z", "preview": "",
        "to": ["ash.calloway87@example.edu"], "cc": []}])
    assert out[0]["toRecipients"][0]["emailAddress"]["address"] \
        == "ash.calloway87@example.edu"
    legacy = lmc._normalize_cli_messages([{"id": "2", "subject": "x",
                                           "from": "yael.quillon56@example.edu",
                                           "received": "", "preview": ""}])
    assert legacy[0]["toRecipients"] == []


def test_cli_normalization_tolerates_a_row_with_no_conversation_id():
    """Older callers, and any row Graph returned without one, still parse."""
    out = lmc._normalize_cli_messages([{"id": "1", "subject": "x",
                                        "from": "yael.quillon56@example.edu", "received": "",
                                        "preview": ""}])
    assert out[0]["conversationId"] == ""


def test_evidence_falls_back_to_the_mediated_cli(monkeypatch):
    """The disposition's primary fix: when graph_client cannot be imported,
    evidence comes from graph_actions.py (a SAFE mediated verb), not from
    nowhere. The MSAL banner graph_client prints to stdout must not defeat
    the JSON parse."""
    import builtins
    import subprocess

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "graph_client":
            raise PermissionError("token cache is crh-owned")
        return real_import(name, *a, **kw)

    class _Result:
        returncode = 0
        stderr = ""

        def __init__(self, out):
            self.stdout = out

    def fake_run(cmd, **kw):
        banner = "Using MSAL token for avery.foxworth36@example.edu.\n"
        if "calendar-view" in cmd:
            return _Result(banner + '{"count": 1, "events": [{"id": "E1", '
                                    '"subject": "Defense", "start": '
                                    '"2026-09-01T10:00:00", "organizer": "Ana"}]}')
        return _Result(banner + '[{"id": "1", "subject": "Submission received", '
                                '"from": "noreply@easychair.org", "received": '
                                '"2026-06-05T01:00:00Z", "preview": "Thank you."}]')

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(subprocess, "run", fake_run)

    inbox, sent, events, status = lmc._fetch_evidence()
    assert status["route"].startswith("graph_actions CLI"), status
    assert len(inbox) == 1 and len(sent) == 1 and len(events) == 1
    assert status["inbox"].startswith("ok via CLI"), status
    # list-mail carries conversationId, so the CLI route evaluates every
    # signal the graph_client route does.
    assert status["unevaluable_signals"] == {}
    assert status["complete"] is True


# ── Signal 5: calendar_event_match ─────────────────────────────────────
# The signal has no replay corpus for closure_metrics.backtest; these
# tests are the backtest, run against a reconstructed corpus.

def _use_profile(tmp_path: Path, email="avery.foxworth36@example.edu"):
    """Point EA_DIR at a temp profile and clear the identity-token cache.

    Returns a restore callable. `_user_tokens()` memoises per process, so
    a stale cache would leak the real profile (or another test's) in.
    """
    import os as _os
    ea = tmp_path / "ea"
    (ea / "config").mkdir(parents=True, exist_ok=True)
    (ea / "config" / "user-profile.json").write_text(
        f'{{"name": "Kit Calloway", "email": "{email}"}}')
    prev_dir = _os.environ.get("EA_DIR")
    prev_tokens = cs._USER_TOKENS
    _os.environ["EA_DIR"] = str(ea)
    cs._USER_TOKENS = None

    def _restore():
        if prev_dir is None:
            _os.environ.pop("EA_DIR", None)
        else:
            _os.environ["EA_DIR"] = prev_dir
        cs._USER_TOKENS = prev_tokens

    return _restore


def _cal_event(subject, start, attendees=(), organizer=("", "")):
    return {
        "subject": subject,
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": start, "timeZone": "UTC"},
        "organizer": {"emailAddress": {"name": organizer[0],
                                       "address": organizer[1]}},
        "attendees": [{"emailAddress": {"name": n, "address": a}}
                      for n, a in attendees],
    }


def test_user_identity_tokens_cover_digit_local_part_and_domain(tmp_path):
    """The identity filter used a letters-only tokeniser while the content
    side is alphanumeric, so 'avery.foxworth36@example.edu' produced the identity
    token 'foxworth' against the content token 'foxworth36', and the user's own
    address was never filtered. Domain labels are excluded too."""
    restore = _use_profile(tmp_path)
    try:
        toks = cs._user_tokens()
        assert "foxworth36" in toks, toks
        assert "example" in toks, toks
        assert "calloway" in toks, toks
        # And the content extractor agrees, which is the whole point.
        content = cs._extract_content_tokens("cc avery.foxworth36@example.edu please")
        assert "foxworth36" not in content, content
        assert "example" not in content, content
    finally:
        restore()


def test_calendar_event_match_closes_scheduling_entry(tmp_path):
    """A forward-looking event whose SUBJECT names the entry's person
    closes a scheduling-TYPED entry.

    The entry type matters: a person's name on both sides is the only
    evidence here, and that concession is granted to scheduling-typed
    rows only (see _person_only_evidence)."""
    restore = _use_profile(tmp_path)
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-cal-hit",
                 "Schedule dissertation defense with Oswin Gresham",
                 entry_type="scheduling_request",
                 date_identified="2026-08-10T10:00:00Z")
        cal = [_cal_event(
            "Gresham dissertation defense", "2026-08-27T16:00:00Z",
            [("Oswin Gresham", "finley.bellweather3@example.edu")])]
        result = _run_cal(db_path, cal, tmp_path)
        assert _fired(result) == 1, result
        assert any("calendar_event_match" in c["reason"]
                   for c in result["closures"]), result
        assert _proposed(db_path, "e-cal-hit", "calendar_event_match")
    finally:
        restore()


def test_calendar_institutional_overlap_does_not_close(tmp_path):
    """An entry whose notes quote the user's own address shares only
    'foxworth36'/'example' with an unrelated lab-tour event. Identity
    tokens are not evidence, so that overlap closes nothing."""
    restore = _use_profile(tmp_path)
    try:
        db_path = _new_db(tmp_path)
        _add(db_path, "e-cal-noise", date_identified="2026-08-10T10:00:00Z",
             description="Schedule a call about the NSF site visit")
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute("UPDATE ledger SET notes = ? WHERE id = ?",
                     ("Body preview: please cc avery.foxworth36@example.edu on "
                      "the reply.", "e-cal-noise"))
        conn.commit()
        conn.close()
        open_entries = ledger_mod.get_open_entries(db_path=db_path)
        cal = [_cal_event(
            "Robotics lab tour", "2026-08-28T17:00:00Z",
            [("Eda Birchford", "blake.yarborough73@example.edu")],
            organizer=("Eda Birchford", "blake.yarborough73@example.edu"))]
        result = cs.scrub_closures(
            open_entries=open_entries, outlook_sent=[], outlook_inbox=[],
            calendar_events=cal, db_path=db_path,
        )
        assert _fired(result) == 0, (
            f"institutional-domain overlap must not close: {result}")
        status, _ = _status(db_path, "e-cal-noise")
        assert status == "open"
    finally:
        restore()


# ── Signal 5, tightened ────────────────────────────────────────────────
# A ONE-token overlap is not evidence: one meeting would otherwise close
# every entry sharing a word like 'research' with it, and would re-close
# entries the user had reopened. These pin the five properties that
# prevent it.

def _add_cal(db_path, entry_id, description, *, from_email="", from_name="",
             notes="", last_action="", entry_type="response_needed",
             date_identified="2026-08-10T10:00:00Z", status="open"):
    conn = sqlite3.connect(str(db_path), timeout=10)
    now = ledger_mod._now()
    conn.execute(
        """INSERT INTO ledger (id, type, status, description, from_email,
               from_name, source_message_id, conversation_id,
               gmail_thread_id, date_identified, last_action, notes,
               priority, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, '', '', NULL, ?, ?, ?, NULL, NULL,
                   '{}', ?, ?)""",
        (entry_id, entry_type, status, description, from_email, from_name,
         date_identified, last_action, notes, now, now))
    conn.commit()
    conn.close()


def _set_deadline(db_path, entry_id, deadline):
    """Put a hard deadline on a row. `_deliverable_entry` treats the field
    itself as deliverable evidence, independent of the type vocabulary."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("UPDATE ledger SET deadline = ? WHERE id = ?",
                 (deadline, entry_id))
    conn.commit()
    conn.close()


def _run_cal(db_path, cal, tmp_path, **kw):
    return cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path),
        outlook_sent=[], outlook_inbox=[], calendar_events=cal,
        db_path=db_path, **kw)


def _suppressed(tmp_path) -> list:
    path = cs._suppressed_log_path()
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_calendar_generic_tokens_do_not_close(tmp_path):
    """The live shape: 'Pascoe Wexley / Kit - undergrad research (hexapod)'
    closed four unrelated obligations on overlap=['research']."""
    restore = _use_profile(tmp_path)
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-state-grant",
                 "Write the CU-side SIFO sections: research plan, budget",
                 notes="Meeting notes from the kickoff call")
        _add_cal(db_path, "e-leif",
                 "Meet Leif Rackham re: Fall 2026 undergrad research")
        cal = [_cal_event(
            "Pascoe Wexley / Kit - undergrad research (hexapod)",
            "2026-08-28T16:00:00Z",
            [("Pascoe Wexley", "tam.ambrose45@example.edu")])]
        result = _run_cal(db_path, cal, tmp_path)
        assert _fired(result) == 0, result
        why = {s["id"]: s["suppressed_because"] for s in result["suppressed"]}
        # Two different guards, one per entry: the proposal is not
        # scheduling-shaped at all, and the meeting's overlap is furniture.
        assert "not scheduling-shaped" in why["e-state-grant"], why
        assert "0 usable overlap token" in why["e-leif"], why
    finally:
        restore()


def test_calendar_year_and_domain_tokens_are_not_usable(tmp_path):
    """'2026' (a bare year) and 'gmail' (a mail-domain label) closed
    chat-connector, regional-autonomy-workshop and the vendor rebid."""
    restore = _use_profile(tmp_path)
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-year",
                 "Schedule a call about the 2026 rebid",
                 notes="Reply goes to jo.ambrose4@example.com")
        cal = [_cal_event("GXC Meeting (Fall 2026)", "2026-08-28T16:00:00Z",
                          [("Ann Lee", "rio.dunmore79@example.com")])]
        result = _run_cal(db_path, cal, tmp_path)
        assert _fired(result) == 0, result
    finally:
        restore()


def _crowd(db_path, n=10):
    """A corpus where one collaborator's name is everywhere."""
    for i in range(n):
        _add_cal(db_path, f"e-crowd-{i}",
                 f"Sync with Nolwenn Pettigrew about item {i}")


def test_calendar_requires_a_distinctive_token(tmp_path):
    """A name in many entries identifies none of them. 'nolwenn' is in 7
    open entries, so a 'Nolwenn / Kit' 1:1 satisfied all 7."""
    restore = _use_profile(tmp_path)
    try:
        db_path = _new_db(tmp_path)
        _crowd(db_path)
        _add_cal(db_path, "e-rare",
                 "Ask Mabry Jarrold for times to meet on Frontiers scoping")
        cal = [_cal_event("Nolwenn / Kit", "2026-08-28T16:00:00Z",
                          [("Nolwenn Pettigrew",
                            "lane.nakashima17@example.edu")]),
               _cal_event("NSF Frontiers", "2026-08-29T16:00:00Z",
                          [("Mabry Jarrold", "gus.holloway34@example.edu")])]
        result = _run_cal(db_path, cal, tmp_path)
        ids = [c["id"] for c in result["closures"]]
        assert ids == ["e-rare"], result
        assert any("no distinctive token" in s["suppressed_because"]
                   for s in result["suppressed"]), result["suppressed"]
    finally:
        restore()


def test_calendar_person_binding_beats_frequency(tmp_path):
    """Person binding still beats document frequency: 'nolwenn' is common
    across the ledger, and being the entry's own correspondent on the
    event keeps it DISTINCTIVE where the rarity test alone would not.

    Distinctive is no longer the last gate, so the close needs the
    scheduling TYPE as well; the crowd of response_needed rows carrying
    the same name is stopped one gate earlier, with a different reason.
    """
    restore = _use_profile(tmp_path)
    try:
        db_path = _new_db(tmp_path)
        _crowd(db_path)
        _add_cal(db_path, "e-bound",
                 "Schedule the AI search sync with Nolwenn Pettigrew",
                 entry_type="scheduling_request",
                 from_email="lane.nakashima17@example.edu",
                 from_name="Nolwenn Pettigrew")
        cal = [_cal_event("Nolwenn / Kit", "2026-08-28T16:00:00Z",
                          [("Nolwenn Pettigrew",
                            "lane.nakashima17@example.edu")])]
        result = _run_cal(db_path, cal, tmp_path)
        assert [c["id"] for c in result["closures"]] == ["e-bound"], result
        why = {s["id"]: s["suppressed_because"] for s in result["suppressed"]}
        assert "no distinctive token" in why["e-crowd-0"], why
    finally:
        restore()


def test_calendar_person_name_alone_never_closes_a_crowd_meeting(tmp_path):
    """The two false closures that survived b1fe734, both person names.

    'PLEASE HOLD for TPEC Presentation Meeting' closed
    response-syn-io01 on ['bastian', 'kingsley'] and 'Faculty
    Search Chair Initial Workshop' closed priority-syn-aaq01 on
    ['nolwenn', 'pettigrew']: one person spelled twice clears a
    two-token floor, and both people were merely attendees of somebody
    else's meeting.
    """
    restore = _use_profile(tmp_path)
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-tpec",
                 "Needs response: Fw: Call for Foundations of UG Inquiry "
                 "(FOUND) Projects for Spring 2027",
                 from_email="ira.thornbury32@example.edu",
                 from_name="Bastian Kingsley",
                 date_identified="2026-08-28T00:30:00Z")
        _add_cal(db_path, "e-agenda", "Priority email: Re: Dept. Meeting Agenda",
                 entry_type="priority_alert",
                 from_email="lane.nakashima17@example.edu",
                 from_name="Nolwenn Pettigrew",
                 date_identified="2026-08-27T18:00:00Z")
        # The Pathway gala RSVP, closed three times on ['yelland', 'tavi']
        # against 'RF Team Weekly [note]'. The [note] guard already stops
        # that literal event, so the copy here is a real meeting: the
        # person-name rule has to stop it on its own.
        _add_cal(db_path, "e-pathway-names",
                 "Pathway Bridge to the Fellowship Gala Thu Sep 3: Ned Tarrant "
                 "needs an RSVP, Tavi Yelland is presenting a poster",
                 entry_type="scheduling",
                 from_email="ash.calloway87@example.edu",
                 date_identified="2026-08-24T14:53:08Z")
        cal = [
            _cal_event("RF Team Weekly", "2026-08-31T20:00:00Z",
                       [("Tavi Yelland", "wren.abernathy53@example.edu")]),
            _cal_event("PLEASE HOLD for TPEC Presentation Meeting for CS "
                       "RVW cases", "2026-08-31T16:00:00Z",
                       [("Bastian Kingsley", "ira.thornbury32@example.edu"),
                        ("Marlowe Oldfield", "tam.iverson2@example.edu")]),
            _cal_event("Faculty Search Chair Initial Workshop - hosted by "
                       "College Faculty Affairs", "2026-08-31T18:00:00Z",
                       [("Nolwenn Pettigrew", "lane.nakashima17@example.edu")]),
        ]
        result = _run_cal(db_path, cal, tmp_path)
        assert _fired(result) == 0, result
        assert _status(db_path, "e-tpec")[0] == "open"
        assert _status(db_path, "e-agenda")[0] == "open"
        assert _status(db_path, "e-pathway-names")[0] == "open"
        why = {s["id"]: s["suppressed_because"] for s in result["suppressed"]}
        assert "person-name overlap only" in why["e-agenda"], why
        assert "person-name overlap only" in why["e-pathway-names"], why
        # The TPEC entry never reaches the overlap test: "Call for
        # Foundations of UG Inquiry Projects" is a solicitation, and
        # the bare `call\b` keyword read it as a phone call.
        assert "e-tpec" not in why, why
        assert not cs._is_scheduling_entry(
            {"type": "response_needed",
             "description": "Fw: Call for Foundations of UG Inquiry"})
        assert cs._is_scheduling_entry(
            {"type": "response_needed", "description": "quick call Thursday?"})
    finally:
        restore()


def test_calendar_never_closes_a_deliverable(tmp_path):
    """A meeting about a thing is not the thing. A deliverable with a
    live deadline must survive a calendar event that merely names the
    person it is owed to."""
    restore = _use_profile(tmp_path)
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-slides",
                 "AI degree update: send Nolwenn 2-3 recap slides and advance "
                 "reading materials for the 08/31 CS faculty meeting",
                 entry_type="deadline",
                 from_email="quinn.merriweather73@example.edu",
                 from_name="Nolwenn Pettigrew")
        _add_cal(db_path, "e-narrative",
                 "Write the CU-side SIFO narrative: exec summary, milestones, "
                 "stakeholder collaboration; meeting with Gideon to confirm",
                 entry_type="action_item")
        _set_deadline(db_path, "e-narrative", "2026-08-26")
        cal = [
            _cal_event("Nolwenn / Kit", "2026-08-28T16:00:00Z",
                       [("Nolwenn Pettigrew", "lane.nakashima17@example.edu")]),
            _cal_event("SIFO stakeholder collaboration narrative sync",
                       "2026-08-28T18:00:00Z",
                       [("Gideon Ramsden", "noor.ravensworth68@example.edu")]),
        ]
        result = _run_cal(db_path, cal, tmp_path)
        assert _fired(result) == 0, result
        why = {s["id"]: s["suppressed_because"] for s in result["suppressed"]}
        assert "deliverable" in why["e-slides"], why
        assert "deliverable" in why["e-narrative"], why
    finally:
        restore()


def test_calendar_note_and_solver_events_never_close(tmp_path):
    """'RF Team Weekly [note]' closed the Pathway gala RSVP three times. A
    [note] event records when something happens without claiming the
    user's time; solver token events are the EA's own preferences."""
    restore = _use_profile(tmp_path)
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-gala",
                 "Schedule the Pathway gala RSVP with Tavi Yelland",
                 from_email="wren.abernathy53@example.edu")
        cal = [_cal_event("RF Team Weekly [note]", "2026-08-28T16:00:00Z",
                          [("Tavi Yelland", "wren.abernathy53@example.edu")]),
               _cal_event("p0 Tavi Yelland prep", "2026-08-29T16:00:00Z",
                          [("Tavi Yelland", "wren.abernathy53@example.edu")])]
        result = _run_cal(db_path, cal, tmp_path)
        assert _fired(result) == 0, result
        assert any("non-binding" in s["suppressed_because"]
                   or "[note]" in s["suppressed_because"]
                   for s in result["suppressed"]), result["suppressed"]
    finally:
        restore()


def test_calendar_notes_do_not_make_an_entry_schedulable(tmp_path):
    """jrnl-4-reviews-due closed on overlap=['hold'] because the word
    'Meeting' appeared in a quoted email in its notes. The scheduling
    gate reads the description now."""
    restore = _use_profile(tmp_path)
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-jrnl",
                 "4 JRNL manuscript reviews now due at once: 0501, 0602",
                 notes=("PLEASE HOLD for the TPEC Presentation Meeting "
                        "with Gideon Nevill"))
        cal = [_cal_event("TPEC Presentation Meeting with Gideon Nevill",
                          "2026-08-28T16:00:00Z",
                          [("Gideon Nevill", "oakley.yarborough88@example.edu")])]
        result = _run_cal(db_path, cal, tmp_path)
        assert _fired(result) == 0, result
        assert any("not scheduling-shaped" in s["suppressed_because"]
                   for s in result["suppressed"]), result["suppressed"]
    finally:
        restore()


def test_calendar_still_open_veto_survives_a_fresh_run(tmp_path):
    """The war, ended. A calendar match proposes; the user's "Still open"
    is a standing veto for that (entry, signal), on ANY calendar match,
    stored on the row itself."""
    restore = _use_profile(tmp_path)
    try:
        db_path = _new_db(tmp_path)
        veto_path = tmp_path / "vetoes.json"
        _add_cal(db_path, "e-veto",
                 "Ask Mabry Jarrold for times to meet on Frontiers scoping")
        cal = [_cal_event("NSF Frontiers", "2026-08-29T16:00:00Z",
                          [("Mabry Jarrold", "gus.holloway34@example.edu")])]
        first = _run_cal(db_path, cal, tmp_path, veto_store_path=veto_path)
        assert _fired(first) == 1 and _proposed(db_path, "e-veto", "calendar_event_match"), first

        # The user keeps it open on the card.
        import closure
        conn = ledger_mod.guarded_connect(db_path)
        closure.reject(conn, "e-veto")
        conn.commit()
        conn.close()

        second = _run_cal(db_path, cal, tmp_path, veto_store_path=veto_path)
        assert _fired(second) == 0, second
        assert _status(db_path, "e-veto")[0] == "open" and not _proposal(db_path, "e-veto")

        # A third run reads the veto from the row again: still vetoed.
        third = _run_cal(db_path, cal, tmp_path, veto_store_path=veto_path)
        assert _fired(third) == 0, third

        # A different event does not get a second chance either.
        other = [_cal_event("Frontiers scoping with Mabry",
                            "2026-09-02T16:00:00Z",
                            [("Mabry Jarrold", "gus.holloway34@example.edu")])]
        fourth = _run_cal(db_path, other, tmp_path, veto_store_path=veto_path)
        assert _fired(fourth) == 0, fourth
    finally:
        restore()


def test_calendar_veto_bootstraps_from_ledger_history(tmp_path):
    """Vetoes predating the state file are read off the entry's own
    last_action on the first run after deploy."""
    restore = _use_profile(tmp_path)
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-hist",
                 "Ask Mabry Jarrold for times to meet on Frontiers scoping",
                 last_action=("Auto-closed by closure_scrubber: "
                              "calendar_event_match (subject='NSF "
                              "Frontiers', overlap=['mabry']). Previous: "
                              "2026-08-26 15:00 MDT: REOPENED (again)."))
        cal = [_cal_event("NSF Frontiers", "2026-08-29T16:00:00Z",
                          [("Mabry Jarrold", "gus.holloway34@example.edu")])]
        result = _run_cal(db_path, cal, tmp_path,
                          veto_store_path=tmp_path / "fresh-vetoes.json")
        assert _fired(result) == 0, result
        assert result["new_vetoes"][0]["source"] == "ledger history", result
    finally:
        restore()


def test_calendar_suppressions_are_logged_separately(tmp_path):
    """Near-misses are audit data, but they belong in their own log:
    closure_metrics reads closure-scrubber.log as what really closed."""
    restore = _use_profile(tmp_path)
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-logged",
                 "Meet Leif Rackham re: Fall 2026 undergrad research")
        cal = [_cal_event(
            "Pascoe Wexley / Kit - undergrad research (hexapod)",
            "2026-08-28T16:00:00Z",
            [("Pascoe Wexley", "tam.ambrose45@example.edu")])]
        result = _run_cal(db_path, cal, tmp_path)
        assert _fired(result) == 0, result
        rows = _suppressed(tmp_path)
        assert rows and rows[0]["id"] == "e-logged", rows
        assert rows[0]["signal"] == "calendar_event_match"
        assert rows[0]["raw_overlap"], rows
        assert rows[0]["suppressed_because"]
        assert not cs.LOG_PATH.exists(), "nothing closed; audit log stays empty"
    finally:
        restore()


# ── Signals 7 and 8: entries with no conversation id ───────────────────
# Every earlier signal joins on conversation_id or gmail_thread_id. A row
# carrying neither has no live signal at all once the calendar signal is
# veto-fenced, so these two reach it by other evidence.


def _sent(subject, to, sent_at, *, frm="wren.fairbanks84@example.edu",
          cc=()):
    return {
        "subject": subject,
        "sentDateTime": sent_at,
        "receivedDateTime": sent_at,
        "from": {"emailAddress": {"name": "Kit Calloway", "address": frm}},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
    }


def _inbound(subject, frm, received, preview="", name=""):
    return {
        "subject": subject,
        "receivedDateTime": received,
        "bodyPreview": preview,
        "from": {"emailAddress": {"name": name, "address": frm}},
        "toRecipients": [{"emailAddress": {
            "address": "wren.fairbanks84@example.edu"}}],
    }


def _link_project(db_path, project_id, ledger_ids, members):
    """Create the project-store tables the scrubber reads and file rows
    into them. `_new_db` only applies the ledger schema, and
    `build_project_index` must survive their absence, so the tables are
    created here rather than in the fixture."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS project_ledger ("
        " project_id TEXT NOT NULL, ledger_id TEXT NOT NULL,"
        " PRIMARY KEY (project_id, ledger_id));"
        "CREATE TABLE IF NOT EXISTS project_people ("
        " project_id TEXT NOT NULL, email TEXT NOT NULL,"
        " role_in_project TEXT, note TEXT,"
        " PRIMARY KEY (project_id, email));")
    for lid in ledger_ids:
        conn.execute("INSERT OR IGNORE INTO project_ledger VALUES (?,?)",
                     (project_id, lid))
    for addr in members:
        conn.execute("INSERT OR IGNORE INTO project_people VALUES (?,?,?,?)",
                     (project_id, addr, "member", ""))
    conn.commit()
    conn.close()


def _run(db_path, tmp_path, **kw):
    return cs.scrub_closures(
        open_entries=ledger_mod.get_open_entries(db_path=db_path),
        db_path=db_path, **kw)


def test_sender_reply_match_closes_the_pathway_rsvp(tmp_path):
    """A hand-created row carries no conversation id, so
    `_user_replied_outlook` returns False on its first line. The reply is
    found by sender match instead.

    The reply arrives via `outlook_archive`: archiving a conversation
    moves the user's own sent copy OUT of Sent Items."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-pathway",
                 "Pathway Bridge to the Fellowship Gala, Thu Sep 3 6-8pm, "
                 "Riverside Building cafe. Ned Tarrant needs an RSVP by "
                 "Tue Aug 25.",
                 entry_type="scheduling",
                 from_email="ash.calloway87@example.edu",
                 date_identified="2026-08-24T14:53:08Z")
        _set_deadline(db_path, "e-pathway", "2026-08-25")
        archive = [
            _sent("RE: Pathway BD Invitation for Sept 3",
                  ["ash.calloway87@example.edu"], "2026-08-24T15:51:07Z"),
            # Ned's thank-you afterwards must not keep the row alive.
            _inbound("RE: Pathway BD Invitation for Sept 3",
                     "ash.calloway87@example.edu", "2026-08-24T15:58:13Z",
                     preview="Kit: Thanks for letting me know and for "
                             "advising Tavi. Ned"),
        ]
        result = _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=[],
                      outlook_archive=archive)
        assert _fired(result) == 1, result
        assert result["closures"][0]["reason"].startswith(
            "sender_reply_match"), result
        assert "pathway" in result["closures"][0]["reason"], result
        assert _proposed(db_path, "e-pathway", "sender_reply_match")
    finally:
        restore()


def test_sender_reply_match_ignores_a_meeting_acceptance(tmp_path):
    """'Accepted: Lucan-Kit Tag Up - SPI Orbital Defense Studies' is
    written by Outlook when the user clicks a button, not by the user.
    It would have closed twl-spi-visit-sep1-lucan-ibbotson, whose ask is
    an RSVP to a Tidewater Research Lab visit plus a choice of slot."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-spi",
                 "Lucan Ibbotson (the SPI): RSVP for the Tidewater Research Lab "
                 "visit to SPI Tue Sep 1, and pick an SPI Orbital Defense "
                 "Studies strategy slot",
                 from_email="vic.ellery24@example.edu",
                 date_identified="2026-08-24T02:07:49Z")
        sent = [_sent("Accepted: Lucan-Kit Tag Up - SPI Orbital Defense "
                      "Studies", ["vic.ellery24@example.edu"],
                      "2026-08-24T18:44:53Z")]
        result = _run(db_path, tmp_path, outlook_sent=sent, outlook_inbox=[])
        assert _fired(result) == 0, result
        assert _status(db_path, "e-spi")[0] == "open"
    finally:
        restore()


def test_sender_reply_match_refuses_deliverable_types(tmp_path):
    """Writing to a collaborator does not deliver a deliverable.
    `state-grant-poc-application` (type deadline, due 8/27) would have closed on
    Kit's 8/26 'RE: SIFO POC: Scope, IRF kickoff, and next steps' to
    Ned, a day before the application went in. Only correspondence-shaped
    types are eligible (`_REPLY_SHAPED_TYPES`)."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-state-grant-dl",
                 "SIFO Frontier Industries POC application (Brightlane "
                 "microrobot) due Aug 27",
                 entry_type="deadline",
                 from_email="sol.carrow62@example.com",
                 date_identified="2026-08-11T17:17:54Z")
        _set_deadline(db_path, "e-state-grant-dl", "2026-08-27")
        sent = [_sent("RE: SIFO POC: Scope, IRF kickoff, and next steps",
                      ["sol.carrow62@example.com"], "2026-08-26T17:29:26Z")]
        result = _run(db_path, tmp_path, outlook_sent=sent, outlook_inbox=[])
        assert _fired(result) == 0, result
        assert _status(db_path, "e-state-grant-dl")[0] == "open"
    finally:
        restore()


def test_sender_reply_match_needs_a_non_person_token(tmp_path):
    """The address join already said who. Saying it again in the subject
    is not a second fact, so a reply whose only overlap is the
    correspondent's own name does not close."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-name-only",
                 "Priority email: Re: Dept. Meeting Agenda",
                 entry_type="priority_alert",
                 from_email="lane.nakashima17@example.edu",
                 from_name="Nolwenn Pettigrew",
                 date_identified="2026-08-27T18:00:00Z")
        sent = [_sent("Re: Nolwenn Pettigrew",
                      ["lane.nakashima17@example.edu"], "2026-08-27T19:00:00Z")]
        result = _run(db_path, tmp_path, outlook_sent=sent, outlook_inbox=[])
        assert _fired(result) == 0, result
        why = {s["id"]: s["suppressed_because"] for s in result["suppressed"]}
        assert "non-person" in why["e-name-only"], why
    finally:
        restore()


def test_sender_reply_match_needs_the_reply_to_postdate_the_entry(tmp_path):
    """Mail the user sent BEFORE the row existed did not answer it."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-early",
                 "Pathway Bridge to the Fellowship Gala RSVP owed to Ned Tarrant",
                 entry_type="scheduling",
                 from_email="ash.calloway87@example.edu",
                 date_identified="2026-08-24T14:53:08Z")
        sent = [_sent("RE: Pathway BD Invitation for Sept 3",
                      ["ash.calloway87@example.edu"], "2026-08-21T09:00:00Z")]
        result = _run(db_path, tmp_path, outlook_sent=sent, outlook_inbox=[])
        assert _fired(result) == 0, result
    finally:
        restore()


def test_project_member_completion_closes_the_sifo_application(tmp_path):
    """A project member writing 'submitted!' closes the single open row
    on that project when its deadline is the same day."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-state-grant",
                 "SIFO Frontier Industries POC application (Brightlane "
                 "microrobot) due Aug 27; internal IRF in eRA must go in ASAP",
                 entry_type="deadline",
                 from_email="sol.carrow62@example.com",
                 date_identified="2026-08-11T17:17:54Z")
        _set_deadline(db_path, "e-state-grant", "2026-08-27")
        _link_project(db_path, "state-grant-poc", ["e-state-grant"],
                      ["gus.tremaine71@example.edu",
                       "sol.carrow62@example.com"])
        inbox = [_inbound("proposal copy - RE: submitted! ",
                          "gus.tremaine71@example.edu",
                          "2026-08-28T01:45:49Z",
                          preview="Hi Kit, And here is the compiled "
                                  "proposal copy as a PDF. Best of luck! "
                                  "-Saoirse",
                          name="Saoirse Kingsford")]
        result = _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=inbox)
        assert _fired(result) == 1, result
        assert result["closures"][0]["reason"].startswith(
            "project_member_completion"), result
        assert _proposed(db_path, "e-state-grant", "project_member_completion")
    finally:
        restore()


def test_project_member_completion_wants_the_subject_not_the_body(tmp_path):
    """Body-only completion talk is a passing reference to some OTHER
    thing finishing. All three live examples were: 'the prequal has been
    approved' on a thread about the full application, 'I just submitted
    the prequalification questionnaire', and 'We are all set!' about a
    bagel order. Each would have closed a live obligation."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-state-grant-body",
                 "SIFO Frontier Industries POC application due Aug 27",
                 entry_type="deadline",
                 date_identified="2026-08-11T17:17:54Z")
        _link_project(db_path, "state-grant-poc", ["e-state-grant-body"],
                      ["gus.tremaine71@example.edu"])
        inbox = [_inbound(
            "FW: AI Proof of Concept Prequalification Application Approved",
            "gus.tremaine71@example.edu", "2026-08-20T17:38:00Z",
            preview="Hi Kit, The prequal has been approved! A blank copy "
                    "of the application is attached for reference.")]
        result = _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=inbox)
        assert _fired(result) == 0, result
        assert _status(db_path, "e-state-grant-body")[0] == "open"
    finally:
        restore()


def test_project_member_completion_refuses_a_crowded_project(tmp_path):
    """`mrl-workshop` carries eight open rows and two members, so one
    'we are submitted' from either member would close all eight. When a
    project has more than one open obligation, 'it is done' is
    ambiguous and the signal declines."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        ids = []
        for n in ("date-room", "participants", "survey-owner"):
            eid = f"e-mrl-{n}"
            ids.append(eid)
            _add_cal(db_path, eid, f"[ALW-WS] {n} for the the ALW workshop",
                     entry_type="task",
                     date_identified="2026-08-01T10:00:00Z")
        _link_project(db_path, "mrl-workshop", ids,
                      ["zion.fairbanks6@example.edu"])
        inbox = [_inbound("Re: the ALW AI workshop -- the agenda is complete",
                          "zion.fairbanks6@example.edu", "2026-08-20T14:13:00Z",
                          preview="Everything is complete on our side.")]
        result = _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=inbox)
        assert _fired(result) == 0, result
        for eid in ids:
            assert _status(db_path, eid)[0] == "open"
    finally:
        restore()


def test_project_member_completion_ignores_a_stranger(tmp_path):
    """Curated membership IS the join. Someone not on the project saying
    'submitted!' says nothing about this obligation."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-stranger",
                 "SIFO Frontier Industries POC application due Aug 27",
                 entry_type="deadline",
                 date_identified="2026-08-11T17:17:54Z")
        _link_project(db_path, "state-grant-poc", ["e-stranger"],
                      ["gus.tremaine71@example.edu"])
        inbox = [_inbound("DoE Horizon submitted!",
                          "ira.thornbury32@example.edu",
                          "2026-08-26T10:00:00Z",
                          preview="We are submitted!")]
        result = _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=inbox)
        assert _fired(result) == 0, result
    finally:
        restore()


def test_project_index_survives_a_database_with_no_project_tables(tmp_path):
    """An older database has no project store. That must read as 'this
    signal does not apply', never as an exception inside the
    pre-briefing pass."""
    db_path = _new_db(tmp_path)
    _add(db_path, "e-noproj")
    assert cs.build_project_index(
        ledger_mod.get_open_entries(db_path=db_path), db_path=db_path) == {}
    assert cs.build_project_index([], db_path=db_path) == {}


def test_split_archive_separates_the_users_own_sent_copies(tmp_path):
    """Archiving a conversation moves the user's OWN sent copy out of
    Sent Items, so a folder-scoped `get_sent` cannot see a reply in an
    archived thread, and an archived thread is the likeliest to be
    finished work."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        mine = _sent("RE: Pathway BD Invitation for Sept 3",
                     ["ash.calloway87@example.edu"], "2026-08-24T15:51:07Z")
        theirs = _inbound("Pathway BD Invitation for Sept 3",
                          "ash.calloway87@example.edu", "2026-08-20T15:17:03Z")
        authored, received = cs._split_archive([mine, theirs])
        assert authored == [mine], authored
        assert received == [theirs], received
        assert cs._split_archive([]) == ([], [])
        assert cs._split_archive(None) == ([], [])
    finally:
        restore()


def test_new_signals_never_raise_on_malformed_input(tmp_path):
    """A missing From block, a null subject, an entry with no fields:
    the scrubber runs inside the briefing build, so a bad row must
    produce 'no' rather than an exception."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        junk_msgs = [
            {},
            {"from": None, "subject": None, "receivedDateTime": None},
            {"from": {"emailAddress": {}}, "toRecipients": None,
             "ccRecipients": [{}], "bodyPreview": None},
            {"from": {"emailAddress": {"address": "yael.quillon56@example.edu"}},
             "subject": "submitted!", "receivedDateTime": ""},
        ]
        junk_entries = [
            {},
            {"id": "x"},
            {"id": "y", "type": None, "description": None, "from_email": None,
             "conversation_id": None, "notes": None, "deadline": None},
        ]
        for e in junk_entries:
            assert cs._user_replied_to_correspondent(e, junk_msgs) == (False, "")
            assert cs._project_member_completion(
                e, junk_msgs, project_index={}) == (False, "")
            assert cs._calendar_event_satisfies(e, [], vetoed=False) == (False, "")
            assert cs._deliverable_entry(e) in (True, False)
        # And end to end: junk in every input slot, nothing raised.
        db_path = _new_db(tmp_path)
        _add(db_path, "e-junk")
        result = _run(db_path, tmp_path, outlook_sent=junk_msgs,
                      outlook_inbox=junk_msgs, outlook_archive=junk_msgs,
                      calendar_events=[{}, {"start": None, "subject": None}])
        assert _fired(result) == 0, result
    finally:
        restore()


def test_archive_does_not_change_the_inbox_only_signals(tmp_path):
    """`outlook_archive` is additive and feeds signals 7 and 8 only. The
    ack and confirmation signals keep reading the inbox alone, so an
    archived ack must not close anything through them."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add(db_path, "e-arch-ack", conv_id="convArch",
             date_identified="2026-08-01T10:00:00Z",
             description="Submit the ORBX proposal")
        archived_ack = {
            "conversationId": "convArch",
            "receivedDateTime": "2026-08-02T16:00:00Z",
            "subject": "Thanks for submitting!",
            "bodyPreview": "Thanks for submitting your proposal, we'll be "
                           "in touch.",
            "from": {"emailAddress": {"address": "wren.isenberg76@example.edu"}},
        }
        result = _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=[],
                      outlook_archive=[archived_ack])
        assert _fired(result) == 0, result
        # The same message in the inbox does close it, which is what
        # makes the assertion above about routing rather than content.
        result2 = cs.scrub_closures(
            open_entries=ledger_mod.get_open_entries(db_path=db_path),
            outlook_sent=[], outlook_inbox=[archived_ack], db_path=db_path)
        assert _fired(result2) == 1, result2
    finally:
        restore()


# ────────────────────────────────────────────────────────────────────
# Id-less rows the mailbox has already answered. One route per row shape.
# ────────────────────────────────────────────────────────────────────

def _sent_named(subject, recipients, sent_at,
                frm="wren.fairbanks84@example.edu"):
    """A sent message whose To line carries display names, the way
    Outlook writes them (signal 7's by-name path joins on those)."""
    return {
        "subject": subject,
        "sentDateTime": sent_at,
        "receivedDateTime": sent_at,
        "from": {"emailAddress": {"name": "Kit Calloway", "address": frm}},
        "toRecipients": [{"emailAddress": {"name": n, "address": a}}
                         for n, a in recipients],
        "ccRecipients": [],
    }


def _subdir(tmp_path, name):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _add_closed(db_path, entry_id, description, *, closed_at,
                last_action="2026-09-01: Auto-closed: email archived from inbox by user.",
                notes="", conv_id="", created_at=None):
    """A completed row, the way ledger_sync leaves one after the user
    archives its email."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute(
        """INSERT INTO ledger (id, type, status, description, from_email,
               from_name, source_message_id, conversation_id, gmail_thread_id,
               date_identified, last_action, notes, priority, source,
               metadata, created_at, updated_at, closed_at)
           VALUES (?, 'response_needed', 'completed', ?, '', '', '', ?, NULL,
                   ?, ?, ?, NULL, NULL, '{}', ?, ?, ?)""",
        (entry_id, description, conv_id, created_at or closed_at,
         last_action, notes, created_at or closed_at, closed_at, closed_at))
    conn.commit()
    conn.close()


# ── Signal 9: meeting_response_sent ─────────────────────────────────

_SURE_DESC = ("RSVP to the 'BRISK Mission Campaign' invite on Mon Sep 8, "
              "16:00 UTC / 10:00 MT -- Outlook shows my_response=notResponded")


def test_meeting_response_closes_the_sure_ai_rsvp(tmp_path):
    """A row created from a calendar-view query carries no email and no
    conversation id, so a sent meeting response is the only signal that
    can close it."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-sure", _SURE_DESC, entry_type="action_item",
                 date_identified="2026-08-31T15:13:52Z")
        _set_deadline(db_path, "e-sure", "2026-09-08")
        sent = [_sent("Declined: BRISK Mission Campaign",
                      ["finley.stavros43@example.com"], "2026-09-01T16:04:12Z")]
        result = _run(db_path, tmp_path, outlook_sent=sent, outlook_inbox=[])
        assert _fired(result) == 1, result
        reason = result["closures"][0]["reason"]
        assert reason.startswith("meeting_response_sent"), reason
        assert "Declined: BRISK Mission Campaign" in reason
        assert _proposed(db_path, "e-sure", "meeting_response_sent")
    finally:
        restore()


def test_meeting_response_needs_the_invite_title_verbatim(tmp_path):
    """Token overlap is not the invite. The SPI row asks for an RSVP to a
    Tidewater Research Lab visit; accepting a 'Tag Up' shares three tokens
    with it and answers nothing. The row about an invite quotes it."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-spi",
                 "Lucan Ibbotson (the SPI): RSVP for the Tidewater Research Lab "
                 "visit to SPI Tue Sep 1, and pick an SPI Orbital Defense "
                 "Studies strategy slot",
                 entry_type="action_item",
                 date_identified="2026-08-24T14:00:00Z")
        sent = [_sent("Accepted: Lucan-Kit Tag Up - SPI Orbital Defense Studies",
                      ["vic.ellery24@example.edu"], "2026-08-24T18:44:53Z")]
        result = _run(db_path, tmp_path, outlook_sent=sent, outlook_inbox=[])
        assert _fired(result) == 0, result
        assert any(s["signal"] == "meeting_response_sent"
                   for s in result["suppressed"]), result["suppressed"]
    finally:
        restore()


def test_meeting_response_ignores_a_response_older_than_the_row(tmp_path):
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-sure", _SURE_DESC, entry_type="action_item",
                 date_identified="2026-08-31T15:13:52Z")
        sent = [_sent("Declined: BRISK Mission Campaign",
                      ["finley.stavros43@example.com"], "2026-08-30T16:04:12Z")]
        result = _run(db_path, tmp_path, outlook_sent=sent, outlook_inbox=[])
        assert _fired(result) == 0, result
    finally:
        restore()


def test_meeting_response_only_reaches_rsvp_shaped_blind_rows(tmp_path):
    """A deliverable that merely names the meeting is not discharged by
    declining it, and a row WITH a conversation id is signal 1's."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-slides",
                 "Prepare the intro slides for the BRISK Mission Campaign "
                 "call on Sep 8", entry_type="task",
                 date_identified="2026-08-31T15:13:52Z")
        _add(db_path, "e-with-conv", conv_id="conv-sure",
             date_identified="2026-08-31T15:13:52Z", description=_SURE_DESC)
        sent = [_sent("Declined: BRISK Mission Campaign",
                      ["finley.stavros43@example.com"], "2026-09-01T16:04:12Z")]
        result = _run(db_path, tmp_path, outlook_sent=sent, outlook_inbox=[])
        assert _fired(result) == 0, result
    finally:
        restore()


# ── Signal 10: identifier_siblings_closed ────────────────────────────

_JRNL_DESC = "4 JRNL manuscript reviews now due at once: 0501, 0602, 0703, 0804"


def _jrnl_siblings(db_path, ids=("0501", "0602", "0703", "0804"),
                   closed_at="2026-09-01T13:00:05Z", **kw):
    for n in ids:
        _add_closed(db_path, f"response-{n}",
                    f"Needs response: Reviewer Assignments for Manuscript ID "
                    f"JRNL-2026-{n} are now due", closed_at=closed_at, **kw)


def test_identifier_siblings_close_the_jrnl_umbrella(tmp_path):
    """The live case. jrnl-4-reviews-due-20260826 (hand-made, type
    deadline, no ids) was 'all overdue at once' in the 09-02 briefing;
    the user had archived the last of the four per-manuscript reminders
    on 09-01 and ledger_sync closed their rows."""
    db_path = _new_db(tmp_path)
    _add_cal(db_path, "e-jrnl4", _JRNL_DESC, entry_type="deadline",
             date_identified="2026-08-26T10:04:28Z")
    _jrnl_siblings(db_path)
    result = _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=[])
    assert _fired(result) == 1, result
    reason = result["closures"][0]["reason"]
    assert reason.startswith("identifier_siblings_closed"), reason
    for key in ("jrnl:0501", "jrnl:0602", "jrnl:0703", "jrnl:0804"):
        assert key in reason, reason
    assert _proposed(db_path, "e-jrnl4", "identifier_siblings_closed")


def test_identifier_siblings_hold_while_one_id_is_still_open(tmp_path):
    db_path = _new_db(tmp_path)
    _add_cal(db_path, "e-jrnl4", _JRNL_DESC, entry_type="deadline",
             date_identified="2026-08-26T10:04:28Z")
    _jrnl_siblings(db_path, ids=("0501", "0602", "0703"))
    result = _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=[])
    assert _fired(result) == 0, result
    miss = [s for s in result["suppressed"]
            if s["signal"] == "identifier_siblings_closed"]
    assert miss and "0804" in miss[0]["why"], result["suppressed"]


def test_identifier_siblings_need_closures_after_the_row_and_by_a_human(tmp_path):
    """A sibling closed BEFORE the umbrella existed is what the umbrella
    was written against; one closed by a scrubber token match must not
    cascade."""
    db_path = _new_db(tmp_path)
    _add_cal(db_path, "e-jrnl4", _JRNL_DESC, entry_type="deadline",
             date_identified="2026-08-26T10:04:28Z")
    _jrnl_siblings(db_path, ids=("0501", "0602", "0703"))
    _add_closed(db_path, "response-0804",
                "Needs response: Reviewer Assignments for Manuscript ID "
                "JRNL-2026-0804 are now due", closed_at="2026-08-25T13:30:03Z")
    assert _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=[])["closed"] == 0

    db_path = _new_db(_subdir(tmp_path, "b"))
    _add_cal(db_path, "e-jrnl4", _JRNL_DESC, entry_type="deadline",
             date_identified="2026-08-26T10:04:28Z")
    _jrnl_siblings(db_path, ids=("0501", "0602", "0703"))
    _add_closed(db_path, "response-0804",
                "Needs response: Reviewer Assignments for Manuscript ID "
                "JRNL-2026-0804 are now due", closed_at="2026-09-01T13:00:05Z",
                last_action="Auto-closed by closure_scrubber: calendar_event_match "
                            "(subject='PLEASE HOLD', overlap=['hold'])")
    assert _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=[])["closed"] == 0


def test_identifier_siblings_ignore_notes_and_standing_rows(tmp_path):
    """The identifier join reads descriptions only, and never closes a
    standing process row: a shared identifier in NOTES is not evidence
    that the row's own work is done."""
    db_path = _new_db(tmp_path)
    _add_cal(db_path, "e-topups",
             "AE reviewer pipeline: monitor invites + top up per coverage "
             "policy (2 confirmed->2 out, 1->3, 0->5)",
             entry_type="task", date_identified="2026-07-23T16:47:11Z",
             notes="2026-08-17: bounce NDR for 'Invitation reminder - JRNL-2026-0105'")
    _add_closed(db_path, "response-unavail", "Needs response: Reviewer Unavailable",
                closed_at="2026-08-24T17:00:08Z",
                notes="One or more reviewers invited to review JRNL-2026-0105 have declined.")
    assert _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=[])["closed"] == 0

    # even with the id in both descriptions, a standing row never closes
    db_path = _new_db(_subdir(tmp_path, "b"))
    _add_cal(db_path, "e-topups",
             "Monitor reviewer invites for JRNL-2026-0105 and top up per policy",
             entry_type="task", date_identified="2026-07-23T16:47:11Z")
    _add_closed(db_path, "response-0105",
                "Needs response: Reviewer Unavailable JRNL-2026-0105",
                closed_at="2026-08-24T17:00:08Z")
    assert _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=[])["closed"] == 0


def test_identifier_siblings_read_prefixed_ids_and_proposal_numbers(tmp_path):
    db_path = _new_db(tmp_path)
    _add_cal(db_path, "e-doe",
             "Decide the two DOE ASCR mail-in reviews, proposals 0000900016 "
             "and 0000904025, by Sep 6", entry_type="deadline",
             date_identified="2026-08-23T10:00:00Z")
    _add_closed(db_path, "response-016",
                "Needs response: Request to Review DOE (ASCR) Proposal 0000900016",
                closed_at="2026-08-31T13:00:05Z")
    _add_closed(db_path, "response-025",
                "Needs response: Request to Review DOE (ASCR) Proposal 0000904025",
                closed_at="2026-08-31T13:00:05Z")
    _add_cal(db_path, "e-fr",
             "Repairbot (XR-26-0222, IEEE T-XR): check review status",
             entry_type="follow_up", date_identified="2026-08-16T19:46:25Z")
    _add_closed(db_path, "response-fr",
                "Needs response: Decision on your manuscript XR-26-0222",
                closed_at="2026-08-30T13:00:05Z")
    result = _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=[])
    assert _fired(result) == 2, result
    assert {c["id"] for c in result["closures"]} == {"e-doe", "e-fr"}
    # bare numbers with no namespace word the closed rows share stay open
    db_path = _new_db(_subdir(tmp_path, "b"))
    _add_cal(db_path, "e-bare", "Order parts 4457 and 9981 for the bench",
             entry_type="task", date_identified="2026-08-23T10:00:00Z")
    _add_closed(db_path, "response-x", "Needs response: PO-2026-4457 approved",
                closed_at="2026-08-31T13:00:05Z")
    assert _run(db_path, tmp_path, outlook_sent=[], outlook_inbox=[])["closed"] == 0


# ── Signal 7 by name ────────────────────────────────────────────────

_NORTHFORD_DESC = ("Broad Area AI search committee: explicitly confirm Elio "
                 "Northford's participation (never directly asked; assumed via "
                 "Nolwenn) and correct the 'Eilo' misspelling in the plan "
                 "sent 8/20")
_WELCOME_TO = [("Faye Drummond", "avery.corvino33@example.edu"),
               ("Rafe Dimmock", "uli.iverson72@example.edu"),
               ("Joss Whitcombe", "pax.juniper4@example.edu"),
               ("Elio Northford", "gus.yarborough37@example.edu")]


def test_sender_reply_match_finds_the_correspondent_by_name(tmp_path):
    """The live case. ai-search-northford-confirm (08-24, no from_email)
    asked to confirm Elio Northford's seat; the 08-28 'Welcome to the
    AY26-27 AI faculty search committee' mail went To him by name, and
    the row was 'overdue since Aug 28' in the 09-02 briefing."""
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-northford", _NORTHFORD_DESC, entry_type="follow_up",
                 date_identified="2026-08-24T02:07:20Z")
        _set_deadline(db_path, "e-northford", "2026-08-28")
        sent = [_sent_named("Welcome to the AY26-27 AI faculty search committee",
                            _WELCOME_TO, "2026-08-28T17:24:32Z")]
        result = _run(db_path, tmp_path, outlook_sent=sent, outlook_inbox=[])
        assert _fired(result) == 1, result
        reason = result["closures"][0]["reason"]
        assert reason.startswith("sender_reply_match"), reason
        assert "named='Elio Northford'" in reason, reason
        assert "to=gus.yarborough37@example.edu" in reason, reason
    finally:
        restore()


def test_sender_reply_match_by_name_needs_the_recipient_and_the_topic(tmp_path):
    restore = _use_profile(tmp_path, email="wren.fairbanks84@example.edu")
    try:
        # the named person is not on the mail
        db_path = _new_db(tmp_path)
        _add_cal(db_path, "e-northford", _NORTHFORD_DESC, entry_type="follow_up",
                 date_identified="2026-08-24T02:07:20Z")
        sent = [_sent_named("Welcome to the AY26-27 AI faculty search committee",
                            _WELCOME_TO[:3], "2026-08-28T17:24:32Z")]
        assert _fired(_run(db_path, tmp_path, outlook_sent=sent,
                    outlook_inbox=[])) == 0
        # the named person is on the mail, but the mail is about lunch
        db_path = _new_db(_subdir(tmp_path, "b"))
        _add_cal(db_path, "e-northford", _NORTHFORD_DESC, entry_type="follow_up",
                 date_identified="2026-08-24T02:07:20Z")
        sent = [_sent_named("Lunch next week?", _WELCOME_TO[3:],
                            "2026-08-28T17:24:32Z")]
        assert _fired(_run(db_path, tmp_path, outlook_sent=sent,
                    outlook_inbox=[])) == 0
        # 'Last, First' display names resolve too
        db_path = _new_db(_subdir(tmp_path, "c"))
        _add_cal(db_path, "e-northford", _NORTHFORD_DESC, entry_type="follow_up",
                 date_identified="2026-08-24T02:07:20Z")
        sent = [_sent_named("Welcome to the AY26-27 AI faculty search committee",
                            [("Northford, Elio", "gus.yarborough37@example.edu")],
                            "2026-08-28T17:24:32Z")]
        assert _fired(_run(db_path, tmp_path, outlook_sent=sent,
                    outlook_inbox=[])) == 1
    finally:
        restore()


def test_named_people_skips_the_user_and_non_names():
    names = cs._named_people(_NORTHFORD_DESC)
    assert "elio northford" in names
    assert not any("kit" in n for n in names)
    assert "broad area" in names  # loose on purpose: the To line decides
    assert cs._recipient_named(
        _sent_named("x", [("Broad Area", "")], "2026-01-01T00:00:00Z"),
        names) == ("", "")


if __name__ == "__main__":
    import shutil
    import tempfile

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    base = Path(tempfile.mkdtemp(prefix="closure-scrubber-tests-"))
    try:
        passed = failed = 0
        for t in tests:
            sub = base / t.__name__
            sub.mkdir(parents=True, exist_ok=True)
            try:
                _call_test(t, sub)
                print(f"  ✅ {t.__name__}")
                passed += 1
            except Exception as e:
                print(f"  ❌ {t.__name__}: {e}")
                traceback.print_exc()
                failed += 1
        print(f"\n  {passed} passed, {failed} failed")
        sys.exit(1 if failed else 0)
    finally:
        shutil.rmtree(base, ignore_errors=True)
