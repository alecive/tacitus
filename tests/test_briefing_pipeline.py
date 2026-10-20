#!/usr/bin/env python3
"""Tests for the briefing pipeline stages that keep the briefing short.

Covers sent-reply cross-referencing (split_replied_items), Slack mention
reconciliation (annotate_slack_mentions), and the deterministic priority
queue (build_today_top).

Usage:
    python3 tests/test_briefing_pipeline.py
"""

import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# No test may touch production state. preprocess.py does STATE_DIR.mkdir()
# at import, so EA_DIR must be a sandbox BEFORE the import below; unset, it
# resolves to the live ~/.ea.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-briefing-")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from preprocessor.preprocess import (
    _norm_subject,
    _parse_deadline_date,
    annotate_slack_mentions,
    build_sent_reply_index,
    build_today_top,
    split_replied_items,
    _parse_ledger_deadline,
)


def _sent(to, subject, ts, cid=None):
    return {
        "conversationId": cid,
        "subject": subject,
        "sentDateTime": ts,
        "toRecipients": [{"emailAddress": {"address": to}}],
    }


def _item(frm, subject, received, cid="conv-x", **kw):
    d = {"from": frm, "from_name": kw.pop("from_name", ""),
         "subject": subject, "received": received,
         "conversation_id": cid, "message_id": kw.pop("message_id", "m1")}
    d.update(kw)
    return d


def test_norm_subject_strips_stacked_prefixes():
    assert _norm_subject("RE: Fwd: RE:  Budget  plan ") == "budget plan"
    assert _norm_subject(None) == ""


def test_same_conversation_reply_suppresses():
    idx = build_sent_reply_index(
        [_sent("kit.bancroft59@example.edu", "RE: Budget", "2026-08-10T20:00:00Z", cid="c1")])
    still, handled = split_replied_items(
        [_item("kit.bancroft59@example.edu", "Budget", "2026-08-10T15:00:00Z", cid="c1")],
        idx, "needs_response")
    assert not still and len(handled) == 1
    assert handled[0]["match"] == "same-conversation"


def test_newer_inbound_stays_open():
    # They answered back after the user's reply: item must stay open.
    idx = build_sent_reply_index(
        [_sent("kit.bancroft59@example.edu", "RE: Budget", "2026-08-10T20:00:00Z", cid="c1")])
    still, handled = split_replied_items(
        [_item("kit.bancroft59@example.edu", "Budget", "2026-08-10T21:00:00Z", cid="c1")],
        idx, "needs_response")
    assert len(still) == 1 and not handled


def test_mail_to_the_sender_in_another_thread_marks_the_item_not_news():
    """Kit wrote to Zion on the proposal draft after Zion's
    biosketch mail: not a reply (the item stays open), but the briefing
    must not present the ask as untouched, so the item carries when and
    on what he wrote."""
    idx = build_sent_reply_index(
        [_sent("kit.bancroft59@example.edu", "RE: Proposal draft", "2026-08-10T20:00:00Z", cid="c-other")])
    assert idx["recip"]["kit.bancroft59@example.edu"]["at"] == "2026-08-10T20:00:00Z"
    still, handled = split_replied_items(
        [_item("kit.bancroft59@example.edu", "Biosketch", "2026-08-10T15:00:00Z", cid="c1"),
         _item("harper.juniper70@example.edu", "Other ask", "2026-08-10T15:00:00Z", cid="c2")],
        idx, "needs_response")
    assert len(still) == 2 and not handled
    assert still[0]["wrote_to_since"] == {"at": "2026-08-10T20:00:00Z", "subject": "RE: Proposal draft"}
    assert "wrote_to_since" not in still[1]
    # Older mail to the sender than their message says nothing.
    still, _ = split_replied_items(
        [_item("kit.bancroft59@example.edu", "Biosketch", "2026-08-11T15:00:00Z", cid="c1")], idx, "needs_response")
    assert "wrote_to_since" not in still[0]


def test_cross_thread_reply_matches_recipient_and_subject():
    # Reply started a fresh thread: different conversation id, same
    # person and (prefix-stripped) subject.
    idx = build_sent_reply_index(
        [_sent("kit.bancroft59@example.edu", "RE: Budget plan", "2026-08-10T20:00:00Z", cid="c9")])
    still, handled = split_replied_items(
        [_item("kit.bancroft59@example.edu", "Budget plan", "2026-08-10T15:00:00Z", cid="c1")],
        idx, "needs_response")
    assert not still and handled[0]["match"] == "recipient+subject"


def test_different_subject_not_matched():
    idx = build_sent_reply_index(
        [_sent("kit.bancroft59@example.edu", "Totally different", "2026-08-10T20:00:00Z", cid="c9")])
    still, handled = split_replied_items(
        [_item("kit.bancroft59@example.edu", "Budget plan", "2026-08-10T15:00:00Z", cid="c1")],
        idx, "needs_response")
    assert len(still) == 1 and not handled


def test_slack_mention_by_name_and_subject():
    items = [
        _item("kit.bancroft59@example.edu", "Quad chart for Sponsor review", "2026-08-10T15:00:00Z",
              from_name="Edie Lindqvist"),
        _item("harper.juniper70@example.edu", "Unrelated thing", "2026-08-10T15:00:00Z",
              from_name="Zz"),
    ]
    msgs = [{"text": "I sent the quad chart to sponsor already", "sender": "user",
             "ts": "1"}]
    n = annotate_slack_mentions(items, msgs)
    assert n == 1
    assert items[0]["mentioned_in_slack"]["sender"] == "user"
    assert "mentioned_in_slack" not in items[1]


def test_slack_single_weak_token_does_not_match():
    items = [_item("kit.bancroft59@example.edu", "Budget planning committee", "2026-08-10T15:00:00Z",
                   from_name="Al")]
    # Only ONE significant subject token appears — should not tag.
    n = annotate_slack_mentions(
        items, [{"text": "the budget looks fine", "sender": "user"}])
    assert n == 0


def test_parse_deadline_dates():
    now = datetime.now(timezone.utc)
    d = _parse_deadline_date("August 15, 2026")
    assert d is not None and (d.month, d.day, d.year) == (8, 15, 2026)
    assert _parse_deadline_date("8/15/26").year == 2026
    assert _parse_deadline_date("next Friday") is None
    # Bare month/day far in the past rolls to next year.
    past = now - timedelta(days=200)
    rolled = _parse_deadline_date(f"{past:%B} {past.day}")
    assert rolled is not None and rolled > now - timedelta(days=95)


def test_today_top_ranks_overdue_deadline_first():
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    deadlines = [{"message_id": "d1", "subject": "Camera ready",
                  "from": "blake.abernathy84@example.org", "from_name": "Pubs",
                  "extracted_date": f"{yesterday:%B} {yesterday.day}, {yesterday.year}",
                  "deadline_text": "due"}]
    needs = [_item("oakley.prescott15@example.edu", "Quick question",
                   "2026-08-10T15:00:00Z", message_id="n1",
                   user_is_direct_recipient=True, days_in_inbox=1,
                   response_signals=2)]
    top, rest = build_today_top(needs, [], deadlines, [], [],
                                ["oakley.prescott15@example.edu"])
    assert top and top[0]["ref"] == "d1"
    assert "OVERDUE" in top[0]["reason"]


def test_today_top_demotes_cc_only_and_caps():
    needs = [_item(f"p{i}@x.edu", f"Thing {i}", "2026-08-10T15:00:00Z",
                   message_id=f"n{i}", user_is_direct_recipient=True,
                   days_in_inbox=10, response_signals=5)
             for i in range(8)]
    cc = _item("ash.nakashima59@example.edu", "FYI thread", "2026-08-10T15:00:00Z",
               message_id="cc1", user_is_cc_only=True, days_in_inbox=10,
               response_signals=5)
    top, rest = build_today_top(needs + [cc], [], [], [], [], [])
    assert len(top) <= 5
    assert all(t["ref"] != "cc1" for t in top)
    assert any(r["title"] == "FYI thread" for r in rest)


def test_today_top_skips_snoozed_obligations():
    obl = [{"id": "ob1", "type": "deadline", "status": "snoozed",
            "description": "snoozed thing", "days_stale": 30},
           {"id": "ob2", "type": "deadline", "status": "open",
            "description": "live deadline", "days_stale": 10}]
    top, rest = build_today_top([], [], [], [], obl, [])
    refs = {t["ref"] for t in top} | {r["title"][:80] for r in rest}
    assert "ob1" not in refs and "snoozed thing" not in refs


def main():
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





def _obl(oid, **kw):
    d = {"id": oid, "type": "task", "status": "open",
         "description": kw.pop("description", oid), "days_stale": 0}
    d.update(kw)
    return d



def _ymd(offset_days):
    return f"{datetime.now(timezone.utc) + timedelta(days=offset_days):%Y-%m-%d}"


def test_parse_ledger_deadline_forms():
    assert _parse_ledger_deadline("2026-08-28").day == 28
    assert _parse_ledger_deadline("2026-08-28T09:00:00Z").month == 8
    assert _parse_ledger_deadline("2026-8-3").day == 3
    # Hand-written entries fall through to the free-text parser.
    assert _parse_ledger_deadline("August 15, 2026").day == 15
    for bad in ("", "   ", "TBD", "end of the month", "2026-02-30",
                "0000-00-00", None, 20260828, {"when": "soon"}):
        assert _parse_ledger_deadline(bad) is None, bad



def test_today_top_obligation_due_today_outranks_far_deadline():
    obl = [_obl("industry", type="project", deadline=_ymd(0),
                priority="high", days_stale=14,
                description="Industry proposal"),
           _obl("talk", type="deadline", deadline=_ymd(90), days_stale=4,
                description="Invited talk")]
    top, rest = build_today_top([], [], [], [], obl, [])
    assert top and top[0]["ref"] == "industry"
    assert "due TODAY" in top[0]["reason"]
    assert "high priority" in top[0]["reason"]
    # A deadline 90 days out is not today's work, whatever the type says.
    assert all(t["ref"] != "talk" for t in top)
    assert any(r["title"] == "Invited talk" for r in rest)



def test_today_top_obligation_overdue_ranks_high():
    obl = [_obl("late", deadline=_ymd(-2), description="late thing"),
           _obl("soon", deadline=_ymd(5), description="next week")]
    top, _rest = build_today_top([], [], [], [], obl, [])
    assert top and top[0]["ref"] == "late"
    assert "OVERDUE" in top[0]["reason"] and "2d late" in top[0]["reason"]
    assert top[0]["score"] > 60



def test_today_top_obligation_priority_orders_the_urgent_band():
    obl = [_obl("plain", deadline=_ymd(1), days_stale=3),
           _obl("hot", deadline=_ymd(1), days_stale=3, priority="high"),
           _obl("cold", deadline=_ymd(1), days_stale=3, priority="low")]
    top, _rest = build_today_top([], [], [], [], obl, [])
    order = [t["ref"] for t in top]
    assert order[0] == "hot"
    assert order.index("plain") < order.index("cold")



def test_today_top_deadline_type_without_a_date_keeps_credit():
    # Real rows exist with type 'deadline' and an empty deadline column
    # (journal-4-reviews-due-20260826). The type is then the only signal.
    obl = [_obl("nodate", type="deadline", days_stale=2,
                description="4 journal reviews")]
    top, _rest = build_today_top([], [], [], [], obl, [])
    assert top and top[0]["ref"] == "nodate"
    assert "no date" in top[0]["reason"]



def test_today_top_malformed_obligation_deadline_never_raises():
    bad = ["", "   ", "TBD", "end of the month", "2026-02-30", None,
           20260828, {"when": "soon"}]
    obl = [_obl(f"b{i}", deadline=v, days_stale=1) for i, v in enumerate(bad)]
    top, rest = build_today_top([], [], [], [], obl, [])
    assert len(top) + len(rest) == len(bad)
    # base 5 + one day of staleness, no deadline credit, no crash.
    assert all(r["score"] == 7 for r in rest)


def test_today_top_skips_parked_obligations():
    # A future follow_up_after or snooze_until means "not before this
    # day": the row stays out of the queue and the digest even when its
    # deadline would rank it (the on-sale date stored as a deadline).
    obl = [_obl("tickets", deadline=_ymd(5), follow_up_after=_ymd(5), days_stale=3,
                description="Buy tickets (on sale in 5 days)"),
           _obl("snoozed", deadline=_ymd(-3), snooze_until=_ymd(10), days_stale=9,
                description="snoozed dispute"),
           _obl("live", deadline=_ymd(5), follow_up_after=_ymd(-1), days_stale=3,
                description="park date passed")]
    top, rest = build_today_top([], [], [], [], obl, [])
    refs = {t["ref"] for t in top}
    titles = {r["title"] for r in rest}
    assert "tickets" not in refs and "Buy tickets (on sale in 5 days)" not in titles
    assert "snoozed" not in refs and "snoozed dispute" not in titles
    assert "live" in refs


if __name__ == "__main__":
    main()
