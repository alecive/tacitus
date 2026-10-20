#!/usr/bin/env python3
"""Tests for preprocessor/draft_capture.py (draft-vs-sent style diffs).

The module holds the last EA-authored body for each draft, pairs it
against what the user actually sent, and purges on the monthly harvest.
The diff is the only signal showing what the user *removes* from a
draft, and nothing else survives the send.
"""

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))
# Owner-side file access goes through resident_io, which runs a resident
# child in an owner process; a sandboxed EA_DIR makes this process a
# resident one, so the calls act directly on the test's temp paths.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-draftcap-")

import draft_capture as dc  # noqa: E402

DRAFTED = ("Hi Gus,\n\nThe problem is staffing this term. Running these "
           "labs means four to six new sections that no "
           "other group can cover. Taking it on spends the one "
           "advantage we have.\n\nKit")
SENT = ("Hi Gus,\n\nThe problem as I see it is staffing this term. Running "
        "these labs means four to six new sections that no "
        "other group can cover, not even the TA pool.\n\nKit")


def _store(td):
    dc.STORE = Path(td) / "draft-bodies"
    return dc.STORE


def _sent_msg(conv, text, when="2026-12-05T20:00:00Z", subject="RE: AI minor"):
    return {"conversationId": conv, "sentDateTime": when, "subject": subject,
            "body": {"contentType": "text", "content": text}}


def test_capture_and_pair_recovers_the_edit():
    with tempfile.TemporaryDirectory() as td:
        _store(td)
        dc.capture("D1", DRAFTED, verb="create-reply",
                   conversation_id="C1", subject="RE: AI minor",
                   to=["zion.corvino32@example.edu"])
        pairs = dc.pair_with_sent([_sent_msg("C1", SENT)])
        assert len(pairs) == 1
        p = pairs[0]
        assert "as I see it" not in p["drafted"]
        assert "as I see it" in p["sent"]          # hedge he added
        assert "spends the one advantage" in p["drafted"]
        assert "spends the one advantage" not in p["sent"]   # cut
        assert p["to"] == ["zion.corvino32@example.edu"]


def test_update_overwrites_so_the_baseline_is_the_last_ea_body():
    with tempfile.TemporaryDirectory() as td:
        _store(td)
        dc.capture("D1", "Hi Bob,\n\n" + " ".join(["first"] * 30),
                   conversation_id="C1")
        dc.capture("D1", "Hi Bob,\n\n" + " ".join(["second"] * 30),
                   verb="update-draft")
        recs = dc.load_all()
        assert len(recs) == 1
        assert "second" in recs[0]["body"] and "first" not in recs[0]["body"]
        assert recs[0]["revisions"] == 2
        assert recs[0]["conversation_id"] == "C1"   # carried across update


def test_a_deleted_draft_leaves_nothing_to_diff():
    with tempfile.TemporaryDirectory() as td:
        _store(td)
        dc.capture("D1", DRAFTED, conversation_id="C1")
        dc.drop("D1")
        assert dc.load_all() == []


def test_purge_drops_past_the_window_and_keeps_the_rest():
    with tempfile.TemporaryDirectory() as td:
        store = _store(td)
        dc.capture("D1", DRAFTED, conversation_id="C1")
        store.mkdir(parents=True, exist_ok=True)
        stale = store / "stale.json"
        stale.write_text(json.dumps({
            "draft_id": "OLD", "body": DRAFTED,
            "captured_at": "2020-01-01T00:00:00Z"}), encoding="utf-8")
        assert dc.purge(days=31) == 1
        left = dc.load_all()
        assert len(left) == 1 and left[0]["draft_id"] == "D1"


def test_a_send_before_the_draft_is_not_its_pair():
    """A thread usually holds several messages; only one came from the
    draft, and it is the one sent after it."""
    with tempfile.TemporaryDirectory() as td:
        _store(td)
        dc.capture("D1", DRAFTED, conversation_id="C1")
        earlier = _sent_msg("C1", SENT, when="2020-01-01T00:00:00Z")
        assert dc.pair_with_sent([earlier]) == []


def test_falls_back_to_subject_when_a_new_draft_has_no_conversation():
    with tempfile.TemporaryDirectory() as td:
        _store(td)
        dc.capture("D1", DRAFTED, subject="Lab sections", conversation_id="")
        pairs = dc.pair_with_sent(
            [_sent_msg("", SENT, subject="Re: Lab sections")])
        assert len(pairs) == 1


def test_quoted_history_is_not_counted_as_his_edit():
    with tempfile.TemporaryDirectory() as td:
        _store(td)
        dc.capture("D1", DRAFTED, conversation_id="C1")
        pairs = dc.pair_with_sent([_sent_msg(
            "C1", SENT + "\n\nFrom: Gus\nlong quoted thread " * 40)])
        assert pairs[0]["sent_words"] < 60


def test_stub_drafts_are_not_worth_a_diff():
    with tempfile.TemporaryDirectory() as td:
        _store(td)
        dc.capture("D1", "Sounds good.")
        assert dc.load_all() == []


def test_capture_never_raises_on_a_bad_store():
    dc.STORE = Path("/proc/nonwritable/draft-bodies")
    dc.capture("D1", DRAFTED, conversation_id="C1")   # must not raise
    assert dc.load_all() == []


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
