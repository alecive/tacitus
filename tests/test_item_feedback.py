#!/usr/bin/env python3
"""Item feedback (preprocessor/item_feedback.py) and its dashboard route.

Each disposition against a scratch ledger: what the ledger row becomes,
what reaches the missed-closure log and the preferences store, and what
the feedback log records. The dashboard's apply_op must route
{"op": "disposition"} here, and the page must offer the four choices
without a new HTML sink.

Usage:
    python3 tests/test_item_feedback.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import traceback
from datetime import date, timedelta
from pathlib import Path

EA_DIR = Path(tempfile.mkdtemp(prefix="ea-test-feedback-"))
os.environ["EA_DIR"] = str(EA_DIR)
for sub in ("config", "state", "logs"):
    (EA_DIR / sub).mkdir(parents=True, exist_ok=True)
(EA_DIR / "config" / "preferences.json").write_text(json.dumps(
    {"version": 1, "rules": [], "calibration": []}))

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import item_feedback as fb  # noqa: E402
import ledger as ledger_mod  # noqa: E402

DB = EA_DIR / "state" / "obligations-ledger.db"


def _fresh():
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(DB) + suffix).unlink(missing_ok=True)
    conn = sqlite3.connect(str(DB))
    conn.executescript(ledger_mod.SCHEMA)
    for m in getattr(ledger_mod, "MIGRATIONS", []):
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    now = "2026-09-01T00:00:00Z"
    rows = [
        ("row-done", "task", "Send Gus the search plan", "", ""),
        ("row-mine", "response_needed", "Needs response: FW: campus AI lunch", "", ""),
        ("row-snooze", "follow_up", "Chase Eli after his trip", "", ""),
        ("row-open", "deadline", "Field workshop participant list to Dana", "", ""),
    ]
    for lid, typ, desc, mid, conv in rows:
        conn.execute("INSERT INTO ledger (id, type, status, description, source_message_id, "
                     "conversation_id, date_identified, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (lid, typ, "open", desc, mid, conv, now, now, now))
    conn.commit()
    conn.close()
    ledger_mod.DB_PATH = DB
    for p in (EA_DIR / "logs").glob("*"):
        p.unlink()


def _row(lid):
    return ledger_mod.get_entry(lid, db_path=DB)


def _feedback_log():
    p = EA_DIR / "logs" / "item-feedback.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_done_closes_and_logs_a_missed_closure():
    _fresh()
    import log_missed_closure as lmc
    lmc.LOG_PATH = EA_DIR / "logs" / "missed-closures.log"
    r = fb.apply_disposition("row-done", "done", "sent it Friday", fetch_for_log=False)
    assert r["ok"] and r["missed_closure_logged"], r
    row = _row("row-done")
    assert row["status"] == "completed"
    assert "done, per user (dashboard): sent it Friday" in row["last_action"]
    rec = json.loads(lmc.LOG_PATH.read_text().splitlines()[-1])
    assert rec["ledger_id"] == "row-done" and rec["source"] == "dashboard"
    assert rec["user_message"] == "sent it Friday"
    log = _feedback_log()
    assert log[-1]["kind"] == "done" and log[-1]["ledger_id"] == "row-done"


def test_not_mine_closes_without_archiving_and_calibrates():
    _fresh()
    r = fb.apply_disposition("row-mine", "not_mine", "FYI only, I was cc'd")
    assert r["ok"] and r["calibration_recorded"], r
    row = _row("row-mine")
    assert row["status"] == "completed" and "not my obligation" in row["last_action"]
    assert row["email_archived"] in (0, None, False)
    prefs = json.loads((EA_DIR / "config" / "preferences.json").read_text())
    cal = prefs["calibration"][-1]
    assert cal["scope"] == "briefing" and "campus AI lunch" in cal["note"] and "cc'd" in cal["note"]
    assert _feedback_log()[-1]["kind"] == "not_mine"


def test_snooze_parks_the_row_until_a_future_date():
    _fresh()
    until = (date.today() + timedelta(days=5)).isoformat()
    r = fb.apply_disposition("row-snooze", "snooze", "after his trip", until, snooze_mail=False)
    assert r["ok"] and r["until"] == until, r
    row = _row("row-snooze")
    assert row["status"] == "snoozed" and row["snooze_until"] == until
    assert "snoozed until" in row["last_action"] and "after his trip" in row["last_action"]
    assert "mail_snoozed" not in r  # no source email on the row
    # dates that are not dates, or not in the future, are refused
    assert not fb.apply_disposition("row-open", "snooze", "", "soon")["ok"]
    assert not fb.apply_disposition("row-open", "snooze", "", date.today().isoformat())["ok"]
    assert _row("row-open")["status"] == "open"


def test_still_open_stamps_the_row_for_the_audit():
    _fresh()
    r = fb.apply_disposition("row-open", "still_open", "waiting on Dana")
    assert r["ok"] and r["confirmed_open_at"], r
    row = _row("row-open")
    assert row["status"] == "open"
    assert row["confirmed_open_at"] == r["confirmed_open_at"]  # metadata, merged by _row_to_dict
    assert "confirmed still open" in row["last_action"]
    # and the pre-send audit leaves it alone
    import open_claims_audit as oca
    assert oca.extract_claims("Field workshop participant list to Dana", [row], []) == []


def test_bad_inputs_are_refused_cleanly():
    _fresh()
    assert fb.apply_disposition("", "done")["error"] == "ledger_id required"
    assert "kind must be" in fb.apply_disposition("row-open", "delete")["error"]
    assert "no ledger item" in fb.apply_disposition("nope", "done")["error"]
    assert _feedback_log() == []


def test_dashboard_routes_the_disposition_op():
    _fresh()
    import project_dashboard as pd
    import project_store as ps
    ps.DB_PATH = DB
    r = pd.apply_op({"op": "disposition", "ledger_id": "row-open", "kind": "still_open",
                     "note": "yes"})
    assert r["ok"] and r["kind"] == "still_open", r
    assert _row("row-open")["confirmed_open_at"]
    assert not pd.apply_op({"op": "disposition", "ledger_id": "row-open", "kind": "x"})["ok"]


def test_page_offers_the_four_dispositions_without_a_new_html_sink():
    html = (REPO / "preprocessor" / "dashboard.html").read_text()
    js = html[html.index("<script"):html.index("</script>")]
    assert 'op:"disposition"' in js
    for kind in ("done", "not_mine", "snooze", "still_open"):
        assert f'"{kind}"' in js, kind
    # the controls are built with el()/textContent: the drawer template
    # stays the page's only innerHTML sink (test_dashboard_xss pins it)
    assert js.count("innerHTML") == 1
    assert "function feedbackEl" in js


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
