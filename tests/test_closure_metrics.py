#!/usr/bin/env python3
"""Tests for preprocessor/closure_metrics.py (closure-loop observability).

Fixtures are sanitized copies of a production false-positive cluster
(one panel email closing unrelated entries on banner-vocabulary overlap)
plus representative missed-closure records. Pins:
  - hit-rate arithmetic and provenance classification
  - signal-prefix parsing incl. inbound_ack:<label>
  - false-positive cluster flagging (the NSF shape MUST be caught)
  - backtest recall (would-catch) and precision (would-suppress) counts
"""

import json
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import closure_metrics as cm  # noqa: E402


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _setup(tmp: Path):
    (tmp / "logs").mkdir(parents=True, exist_ok=True)
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    cm.EA_DIR = tmp
    cm.SCRUBBER_LOG = tmp / "logs" / "closure-scrubber.log"
    cm.MISSED_LOG = tmp / "logs" / "missed-closures.log"
    cm.LEDGER_DB = tmp / "state" / "obligations-ledger.db"


def _write_scrubber_log(tmp: Path):
    """Sanitized NSF cluster: same sender+minute closes 4 entries on
    banner-token overlap, plus 1 legit arxiv ack closure."""
    ts_cluster = _iso(2)[:16] + ":34Z"  # same minute for all 4
    rows = []
    for i in range(4):
        rows.append({
            "id": f"response-nsf-{i}=",
            "reason": ("confirmation_email (from=noor.ambrose88@example.com, "
                       "subject='P000001 FRR Unsolicited Panel - HD0001', "
                       "overlap=['caution', 'email', 'external'])"),
            "description": f"Needs response: unrelated thing {i}",
            "conversation_id": f"conv{i}", "gmail_thread_id": "",
            "ts": ts_cluster, "dry_run": False,
        })
    rows.append({
        "id": "deadline-arxiv-submit",
        "reason": ("inbound_ack:submission-ack from=jules.larchmont52@example.org "
                   "(subject='Your submission has been received', "
                   "overlap=['manuscript', 'belief'])"),
        "description": "Submit manuscript to arXiv",
        "conversation_id": "convX", "gmail_thread_id": "",
        "ts": _iso(1), "dry_run": False,
    })
    # A dry-run row that must be ignored everywhere
    rows.append({
        "id": "dry-run-row", "reason": "confirmation_email (from=x@y, "
        "subject='s', overlap=['real', 'tokens'])",
        "ts": _iso(1), "dry_run": True,
    })
    with open(cm.SCRUBBER_LOG, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _write_calendar_log(tmp: Path):
    """Sanitized calendar cluster: one hexapod meeting closes 3 unrelated
    entries on generic overlap, one entry is closed twice (so a human
    reopened it in between), and one genuine person-name match closes
    once and sticks."""
    rows = []
    ts1 = _iso(3)[:16] + ":40Z"
    for i, ov in enumerate((['research'], ['research', 'undergrad'],
                            ['2026'])):
        rows.append({
            "id": f"cal-generic-{i}",
            "reason": ("calendar_event_match (subject='Pascoe Wexley / Kit "
                       f"- undergrad research (hexapod)', overlap={ov})"),
            "description": f"unrelated obligation {i}",
            "ts": ts1, "dry_run": False,
        })
    rows.append({
        "id": "cal-note-event",
        "reason": ("calendar_event_match (subject='RF Team Weekly [note]', "
                   "overlap=['yelland', 'tavi'])"),
        "description": "Pathway gala RSVP", "ts": ts1, "dry_run": False,
    })
    for ts in (ts1, _iso(1)[:16] + ":52Z"):
        rows.append({
            "id": "cal-refought",
            "reason": ("calendar_event_match (subject='Kit lecture', "
                       "overlap=['lecture'])"),
            "description": "FRR/NRI invited talk", "ts": ts, "dry_run": False,
        })
    rows.append({
        "id": "cal-genuine",
        "reason": ("calendar_event_match (subject='NSF Frontiers', "
                   "overlap=['mabry', 'expeditions', 'jarrold'])"),
        "description": "Ask Mabry Jarrold for times", "ts": _iso(2),
        "dry_run": False,
    })
    with open(cm.SCRUBBER_LOG, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _write_calendar_ledger(tmp: Path):
    """cal-genuine stuck; cal-note-event was reopened by the user."""
    con = sqlite3.connect(str(cm.LEDGER_DB))
    for rid, status in (("cal-generic-0", "completed"),
                        ("cal-generic-1", "completed"),
                        ("cal-generic-2", "completed"),
                        ("cal-note-event", "open"),
                        ("cal-refought", "open"),
                        ("cal-genuine", "completed")):
        con.execute(
            "INSERT INTO ledger (id, type, status, description, last_action,"
            " closed_at, source, created_at, updated_at) VALUES "
            "(?, 'scheduling', ?, 'd', 'Auto-closed by closure_scrubber: "
            "calendar_event_match (...)', ?, NULL, ?, ?)",
            (rid, status, _iso(1), _iso(10), _iso(1)))
    con.commit()
    con.close()


def _write_missed_log(tmp: Path):
    recs = [
        {
            "ts": _iso(3), "ledger_id": "workshop-paper-submit",
            "user_message": "ARSS was submitted.", "source": "slack",
            "entry_summary": {"type": "deadline"},
            "scrubber_signals_present": [],
            "candidate_evidence": [
                "inbox " + _iso(4) + ": subject='Submission received: "
                "ARSS 2026' from=noreply@openreview.net",
            ],
            "suggested_signal": "Consider adding this sender domain to "
                                "CONFIRMATION_SENDER_SUBSTRINGS",
        },
        {
            "ts": _iso(5), "ledger_id": "no-evidence-item",
            "user_message": "handled X", "source": "slack",
            "entry_summary": {"type": "response_needed"},
            "scrubber_signals_present": [],
            "candidate_evidence": [],
            "suggested_signal": "",
        },
    ]
    with open(cm.MISSED_LOG, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def _write_ledger(tmp: Path):
    con = sqlite3.connect(str(cm.LEDGER_DB))
    con.execute("""CREATE TABLE ledger (
        id TEXT PRIMARY KEY, type TEXT, status TEXT, description TEXT,
        from_email TEXT, from_name TEXT, source_message_id TEXT,
        date_identified TEXT, deadline TEXT, follow_up_after TEXT,
        snooze_until TEXT, last_action TEXT, notes TEXT, priority TEXT,
        metadata TEXT, created_at TEXT, updated_at TEXT, closed_at TEXT,
        conversation_id TEXT, email_archived INTEGER, source TEXT,
        gmail_thread_id TEXT)""")
    rows = [
        # scrubber closure
        ("a1", "completed",
         "Auto-closed by closure_scrubber: confirmation_email (...)",
         _iso(2), None),
        # briefing LLM sanity-close
        ("a2", "completed", "auto-closed by briefing: CC only", _iso(2), None),
        # inbox-archive (ledger_sync)
        ("a3", "closed",
         "2026-06-08: Auto-closed: email archived from inbox by user. "
         "Previous: x", _iso(1), None),
        # manual user-stated
        ("m1", "completed", "User confirmed done in Slack", _iso(1), None),
        ("m2", "closed", "closed per Kit", _iso(3), None),
        # plugin entry — must be EXCLUDED
        ("p1", "completed", "audit only", _iso(1), "plugin:ff-watcher"),
        # old closure outside window
        ("old1", "completed", "User confirmed done", _iso(40), None),
    ]
    for rid, status, la, closed_at, source in rows:
        con.execute(
            "INSERT INTO ledger (id, type, status, description, last_action,"
            " closed_at, source, created_at, updated_at) VALUES "
            "(?, 'response_needed', ?, 'd', ?, ?, ?, ?, ?)",
            (rid, status, la, closed_at, source, _iso(10), closed_at))
    con.commit()
    con.close()


# ---------------------------------------------------------------------------

def test_signal_parsing():
    assert cm.signal_of("confirmation_email (from=x)") == "confirmation_email"
    assert cm.signal_of("inbound_ack:submission-ack from=y") == \
        "inbound_ack:submission-ack"
    assert cm.signal_of("user_replied_outlook: sent later") == \
        "user_replied_outlook"
    assert cm.signal_of("???") == "unknown"


def test_confirmation_reason_parsing():
    info = cm.parse_confirmation_reason(
        "confirmation_email (from=noor.ambrose88@example.com, "
        "subject='P000001 FRR', overlap=['caution', 'email'])")
    assert info["from"] == "noor.ambrose88@example.com"
    assert info["subject"] == "P000001 FRR"
    assert info["overlap"] == ["caution", "email"]


def test_provenance_classification():
    assert cm.classify_last_action(
        "Auto-closed by closure_scrubber: x") == "scrubber"
    assert cm.classify_last_action(
        "auto-closed by briefing: y") == "briefing_llm"
    assert cm.classify_last_action(
        "2026-06-08: Auto-closed: email archived from inbox by user."
    ) == "inbox_archive"
    assert cm.classify_last_action("did it myself") == "manual"


def test_report_hit_rate_and_provenance():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        _write_scrubber_log(tmp)
        _write_missed_log(tmp)
        _write_ledger(tmp)
        rep = cm.build_report(days=7)
        # window: a1 scrubber, a2 briefing, a3 inbox, m1+m2 manual;
        # p1 plugin excluded; old1 outside window
        assert rep["autonomous_closures"] == 3, rep
        assert rep["manual_closures"] == 2, rep
        assert rep["hit_rate"] == 0.6, rep
        assert rep["provenance"]["scrubber"] == 1
        assert rep["provenance"]["inbox_archive"] == 1
        assert rep["user_stated_closures_logged"] == 2
        # dry-run row ignored in signal distribution
        dist = rep["scrubber_signal_distribution"]
        assert dist["confirmation_email"] == 4
        assert dist["inbound_ack:submission-ack"] == 1


def test_nsf_cluster_flagged():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        _write_scrubber_log(tmp)
        _write_missed_log(tmp)
        _write_ledger(tmp)
        rep = cm.build_report(days=7)
        fps = rep["false_positive_candidates"]
        kinds = [f["kind"] for f in fps]
        assert "one-email-closed-many" in kinds, fps
        cluster = next(f for f in fps if f["kind"] == "one-email-closed-many")
        assert cluster["count"] == 4
        assert cluster["sender"] == "noor.ambrose88@example.com"
        # all 4 also flagged generic-only
        assert kinds.count("generic-only-overlap") == 4
        # the legit arxiv closure must NOT be flagged
        flagged_ids = {f.get("entry_id") for f in fps}
        assert "deadline-arxiv-submit" not in flagged_ids


def test_backtest_tightening_suppresses_cluster_only():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        _write_scrubber_log(tmp)
        _write_missed_log(tmp)
        res = cm.backtest(days=7, require_nongeneric_overlap=True)
        assert res["would_suppress_count"] == 4, res
        ids = {s["entry_id"] for s in res["would_suppress"]}
        assert "deadline-arxiv-submit" not in ids


def test_backtest_min_overlap_tokens():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        _write_scrubber_log(tmp)
        _write_missed_log(tmp)
        res = cm.backtest(days=7, min_overlap_tokens=1)
        # cluster rows have 0 non-generic tokens -> suppressed
        assert res["would_suppress_count"] == 4


def test_backtest_loosening_catches_missed():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        _write_scrubber_log(tmp)
        _write_missed_log(tmp)
        res = cm.backtest(days=7, add_confirmation_sender="openreview.net")
        assert res["would_catch_count"] == 1, res
        assert res["would_catch"][0]["ledger_id"] == "workshop-paper-submit"
        # non-matching sender catches nothing
        res2 = cm.backtest(days=7, add_confirmation_sender="nothere.org")
        assert res2["would_catch_count"] == 0


def test_backtest_expresses_the_calendar_signal():
    """A tightening branch must exist for the calendar signal too, not
    confirmation_email only. The reference rule suppresses every fought
    closure and keeps the genuine person-name one."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        _write_scrubber_log(tmp)
        _write_calendar_log(tmp)
        _write_ledger(tmp)
        _write_calendar_ledger(tmp)
        res = cm.backtest(days=7, min_overlap_tokens=2,
                          require_distinctive_overlap=True,
                          drop_note_events=True, respect_reopen_veto=True,
                          signals=("calendar_event_match",))
        sup = {s["entry_id"] for s in res["would_suppress"]}
        assert "cal-genuine" not in sup, res["would_suppress"]
        for wrong in ("cal-generic-0", "cal-generic-1", "cal-generic-2",
                      "cal-note-event", "cal-refought"):
            assert wrong in sup, (wrong, res["would_suppress"])
        # The [note] event is suppressed for BEING a note, not by count.
        note = next(s for s in res["would_suppress"]
                    if s["entry_id"] == "cal-note-event")
        assert "non-binding" in note["why"], note
        # Second close of the refought entry cites the veto.
        vetoed = [s for s in res["would_suppress"]
                  if s["entry_id"] == "cal-refought"]
        assert any("veto" in s["why"] for s in vetoed), vetoed
        # Confirmation rows are untouched when the signal is narrowed.
        assert all(s["signal"] == "calendar_event_match"
                   for s in res["would_suppress"])
        assert res["outcome_scores"]["stuck_suppressed"] < res[
            "outcome_scores"]["stuck_total"]


def test_backtest_default_covers_both_overlap_signals():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        _write_scrubber_log(tmp)
        _write_calendar_log(tmp)
        res = cm.backtest(days=7, min_overlap_tokens=2)
        sigs = {s["signal"] for s in res["would_suppress"]}
        assert sigs == {"confirmation_email", "calendar_event_match"}, sigs


def test_repeat_close_is_flagged_as_a_false_positive():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        _write_calendar_log(tmp)
        _write_ledger(tmp)
        fps = cm.build_report(days=7)["false_positive_candidates"]
        refought = [f for f in fps if f["kind"] == "reclosed-after-reopen"]
        assert [f["entry_id"] for f in refought] == ["cal-refought"], fps
        assert refought[0]["count"] == 2


def test_render_md_contains_key_lines():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        _write_scrubber_log(tmp)
        _write_missed_log(tmp)
        _write_ledger(tmp)
        md = cm.render_md(cm.build_report(days=7))
        assert "Hit rate: 60%" in md
        assert "Suspected false positives" in md
        assert "noor.ambrose88@example.com closed 4 entries" in md


def test_empty_logs_no_crash():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        rep = cm.build_report(days=7)
        assert rep["hit_rate"] is None
        assert rep["false_positive_candidates"] == []
        md = cm.render_md(rep)
        assert "n/a" in md


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
