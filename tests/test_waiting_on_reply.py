"""A row whose thread ends on the user's own question waits on the other
side: off the ranked queue for ledger.WAITING_DAYS, back afterwards as
unanswered. The working-group slides row went on the do-list "before your 2:30"
while the user waited to hear how long his slot was."""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import ledger  # noqa: E402
from preprocess import build_today_top  # noqa: E402


def _row(since: str, **kw) -> dict:
    return {"id": "kept-slides", "type": "task", "status": "open",
            "description": "Slides for the Policy Working Group", "days_stale": 0,
            "thread_brief": {"waiting_on_reply": {"since": since, "asked": "Say how long the slot is."}},
            **kw}


def test_the_wait_holds_for_waiting_days_then_reads_unanswered():
    row = _row("2026-10-16T02:53:58Z")
    assert ledger.waiting_on_reply(row, date(2026, 10, 16))["asked"] == "Say how long the slot is."
    assert ledger.unanswered_since(row, date(2026, 10, 16)) == ""
    later = date(2026, 10, 15) + timedelta(days=ledger.WAITING_DAYS + 1)
    assert ledger.waiting_on_reply(row, later) == {}
    assert ledger.unanswered_since(row, later) == "2026-10-15"
    assert ledger.waiting_on_reply({"id": "x"}) == {} and ledger.unanswered_since({"id": "x"}) == ""


def test_the_ranked_queue_skips_a_waiting_row_and_flags_an_unanswered_one():
    now = datetime.now(timezone.utc)
    fresh = _row((now - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ"), deadline=now.date().isoformat())
    top, rest = build_today_top([], [], [], [], [fresh], [])
    assert not top and not rest
    old = _row((now - timedelta(days=ledger.WAITING_DAYS + 2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               deadline=now.date().isoformat())
    top, _ = build_today_top([], [], [], [], [old], [])
    assert [t["ref"] for t in top] == ["kept-slides"]
    assert "no answer to your question since" in top[0]["reason"]
