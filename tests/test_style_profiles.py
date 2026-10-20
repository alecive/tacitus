#!/usr/bin/env python3
"""Tests for preprocessor/style_profiles.py (persistent per-contact style).

Profiles and few-shot snippets accumulate in SQLite, rather than being
recomputed from a 14-day sent window each briefing.
Covers: feature extraction, watermark idempotency, running averages,
snippet rotation (latest 3), quoted-history stripping, and get_profile.
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Sandbox before import: owner_trust counts a process whose EA_DIR is the
# live tree as the owner, so a run of this file alone must not see it.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-style-profiles-")
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import style_profiles as sp  # noqa: E402


def _msg(addr, sent_at, body, name="Alice Smith"):
    return {
        "sentDateTime": sent_at,
        "toRecipients": [{"emailAddress": {"address": addr, "name": name}}],
        "bodyPreview": body,
    }


BODY = ("Hi Alice,\n\nSounds good, I'll see you Thursday at 2. "
        "Don't worry about the slides.\n\nThanks,\nKit")


def test_profile_accumulates():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages(
            [_msg("alice@x.edu", "2026-06-01T10:00:00Z", BODY)], db_path=db)
        r = sp.update_from_messages(
            [_msg("alice@x.edu", "2026-06-02T10:00:00Z",
                  "Hi Alice,\n\nWorks for me.\n\nThanks,\nKit")],
            db_path=db)
        assert r["processed"] == 1
        p = sp.get_profile("alice@x.edu", db_path=db)
        assert p["samples"] == 2
        assert p["greeting"] == "Hi {name},"
        assert p["closing"].lower().startswith("thanks")
        assert p["uses_contractions"] is True
        assert len(p["recent_examples"]) == 2


def test_watermark_idempotent():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        batch = [_msg("kit.bancroft59@example.edu", "2026-06-01T10:00:00Z", BODY)]
        r1 = sp.update_from_messages(batch, db_path=db)
        r2 = sp.update_from_messages(batch, db_path=db)  # same batch again
        assert r1["processed"] == 1
        assert r2["processed"] == 0 and r2["skipped"] == 1
        assert sp.get_profile("kit.bancroft59@example.edu", db_path=db)["samples"] == 1


def test_snippet_rotation_keeps_latest_three():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        msgs = [_msg("kit.bancroft59@example.edu", f"2026-06-0{i}T10:00:00Z",
                     f"Hi Alice,\n\nNote number {i} here with enough text.")
                for i in range(1, 6)]
        sp.update_from_messages(msgs, db_path=db)
        p = sp.get_profile("kit.bancroft59@example.edu", db_path=db)
        assert p["samples"] == 5
        assert len(p["recent_examples"]) == 3
        assert "number 5" in p["recent_examples"][0]["snippet"]
        assert "number 3" in p["recent_examples"][-1]["snippet"]


def test_quoted_history_stripped():
    body = ("Hi Bob,\n\nYes, approved.\n\nKit\n"
            "________________________________\n"
            "From: Bob Jones <bob@y.org>\nSent: Monday\n"
            "A very long quoted thread that is not Kit's writing at all "
            "and would poison the length statistics badly " * 5)
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages(
            [_msg("bob@y.org", "2026-06-01T10:00:00Z", body, name="Bob")],
            db_path=db)
        p = sp.get_profile("bob@y.org", db_path=db)
        assert p["avg_words"] < 15, f"quoted thread leaked: {p['avg_words']}"
        assert "quoted thread" not in p["recent_examples"][0]["snippet"]


def _multi(addrs, sent_at, body):
    return {
        "sentDateTime": sent_at,
        "toRecipients": [{"emailAddress": {"address": a, "name": a}}
                         for a in addrs],
        "body": {"contentType": "text", "content": body},
    }


def test_bare_sign_off_reported_over_stale_closing():
    """One 'Best' must not pin a contact who signs off bare 9 times.

    The legacy `closing` column is last-write-wins and cannot be cleared
    by a later bare email, which had every draft to Gus closing
    'Best' against a 93%-bare record.
    """
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        msgs = [_msg("zion.corvino32@example.edu", "2026-06-01T10:00:00Z",
                     "Hi Gus,\n\nThat works for me.\n\nBest,\nKit")]
        for i in range(2, 11):
            msgs.append(_msg("zion.corvino32@example.edu", f"2026-06-{i:02d}T10:00:00Z",
                             "Hi Gus,\n\nAgreed on the timeline.\n\nKit"))
        sp.update_from_messages(msgs, db_path=db)
        p = sp.get_profile("zion.corvino32@example.edu", db_path=db)
        assert p["closing"] == "Best"          # legacy field, unchanged
        assert p["bare_close_rate"] == 0.9
        assert p["sign_off"].startswith("bare")


def test_median_not_dragged_by_one_long_email():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        msgs = [_msg("harper.juniper70@example.edu", f"2026-06-{i:02d}T10:00:00Z",
                     "Hi Bob,\n\n" + " ".join(["word"] * 30) + "\n\nKit")
                for i in range(1, 10)]
        msgs.append(_msg("harper.juniper70@example.edu", "2026-06-20T10:00:00Z",
                         "Hi Bob,\n\n" + " ".join(["word"] * 1500) + "\n\nKit"))
        sp.update_from_messages(msgs, db_path=db)
        p = sp.get_profile("harper.juniper70@example.edu", db_path=db)
        assert p["median_words"] < 60          # the outlier does not move it
        assert p["avg_words"] > 150            # the legacy mean does
        assert p["max_words"] > 1400


def test_every_addressee_of_a_small_thread_is_credited():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages(
            [_multi(["kit.bancroft59@example.edu", "harper.juniper70@example.edu", "yael.marchetti63@example.edu"],
                    "2026-06-01T10:00:00Z",
                    "Hi all,\n\nLet's meet Thursday to settle it.\n\nKit")],
            db_path=db)
        for a in ("kit.bancroft59@example.edu", "harper.juniper70@example.edu", "yael.marchetti63@example.edu"):
            assert sp.get_profile(a, db_path=db)["messages"] == 1


def test_broadcast_is_not_a_per_contact_model():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        r = sp.update_from_messages(
            [_multi([f"p{i}@x.edu" for i in range(8)],
                    "2026-06-01T10:00:00Z",
                    "Hi all,\n\nThe retreat is moved to Friday.\n\nKit")],
            db_path=db)
        assert r["processed"] == 0
        assert sp.get_profile("dana.westbrook88@example.edu", db_path=db) is None


def test_zoom_invite_boilerplate_is_not_his_prose():
    """Outlook appends it to every hold; counting it as style makes a
    contact he meets with weekly look like someone he never writes to."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages([_msg(
            "dana.galloway76@example.edu", "2026-06-01T10:00:00Z",
            "Hi Casey,\n\nBack Thursday if you want to reschedule.\n\nKit"
            "\n\nKit Calloway is inviting you to a scheduled Zoom "
            "meeting.\nJoin Zoom Meeting\nhttps://example.zoom.us/j/123\n"
            "Meeting ID: 123 456 7890\nOne tap mobile\n")], db_path=db)
        snippet = sp.get_profile("dana.galloway76@example.edu", db_path=db)["recent_examples"][0]
        assert "Zoom" not in snippet["snippet"]
        assert "reschedule" in snippet["snippet"]


def test_html_body_keeps_its_line_structure():
    """Flattening block tags to spaces puts the whole email on one line,
    and the closing lives on the last line."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages([{
            "sentDateTime": "2026-06-01T10:00:00Z",
            "toRecipients": [{"emailAddress": {"address": "jules.isenberg6@example.edu",
                                               "name": "H"}}],
            "body": {"contentType": "html",
                     "content": "<div>Hi Harper,</div><div>&nbsp;</div>"
                                "<div>Ship it when the eval clears.</div>"
                                "<div>Best,</div><div>Kit</div>"},
        }], db_path=db)
        p = sp.get_profile("jules.isenberg6@example.edu", db_path=db)
        assert p["greeting"] == "Hi {name},"
        assert p["closing"] == "Best"
        assert "&nbsp;" not in p["recent_examples"][0]["snippet"]


def test_full_body_sync_upgrades_a_preview_without_double_counting():
    """The briefing records a 255-char preview; the cron sync later sees
    the real body. The watermark would lock the second pass out, and
    re-running it must not count the message twice."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        preview = "Hi Alice,\n\nHere is the plan for Thursday, we should"
        sp.update_from_messages(
            [_msg("alice@x.edu", "2026-06-01T10:00:00Z", preview)],
            db_path=db)
        assert sp.get_profile("alice@x.edu", db_path=db)["messages"] == 1

        full = {
            "sentDateTime": "2026-06-01T10:00:00Z",
            "toRecipients": [{"emailAddress": {"address": "alice@x.edu",
                                               "name": "Alice"}}],
            "body": {"contentType": "text",
                     "content": preview + " start with the eval harness "
                                          "and hold the rest.\n\nKit"},
        }
        sp.update_from_messages([full], db_path=db, reprocess=True)
        p = sp.get_profile("alice@x.edu", db_path=db)
        assert p["messages"] == 1                     # not counted twice
        assert p["samples"] == 1
        assert "eval harness" in p["recent_examples"][0]["snippet"]


def test_unknown_contact_returns_none():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages([], db_path=db)
        assert sp.get_profile("dov.fairbanks76@example.org", db_path=db) is None


def test_short_or_recipientless_skipped():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        r = sp.update_from_messages([
            {"sentDateTime": "2026-06-01T10:00:00Z", "toRecipients": [],
             "bodyPreview": BODY},
            _msg("kit.bancroft59@example.edu", "2026-06-01T11:00:00Z", "ok"),
        ], db_path=db)
        assert r["processed"] == 0 and r["skipped"] == 2


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
