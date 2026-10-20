"""An update to a closed ledger row says the row is closed.

What must hold: `ledger.py update` on a closed or completed row, without a
status that reopens it, returns a warning naming how the row was closed,
and keeps the closing note in metadata when a new last_action replaces
it. A session that keeps writing notes to a row the sync closed must learn
that the ledger no longer counts the work as owed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "preprocessor" / "ledger.py"


def _ledger(ea_dir: Path, *args: str) -> dict:
    env = dict(os.environ, EA_DIR=str(ea_dir))
    r = subprocess.run([sys.executable, str(LEDGER), *args], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_update_on_a_closed_row_warns_and_keeps_the_closing_note(tmp_path):
    ea = tmp_path / "ea"
    (ea / "state").mkdir(parents=True)
    _ledger(ea, "add", "--id", "t1", "--type", "task", "--description", "Send the outline")
    _ledger(ea, "close", "--id", "t1", "--user-said", "done", "--skip-archive",
            "--note", "Auto-closed: email archived from inbox by user.")
    out = _ledger(ea, "update", "--id", "t1", "--last-action", "outline uploaded; waiting")
    assert "is completed" in out["warning"] and "Auto-closed" in out["warning"]
    assert out["entry"]["status"] == "completed"
    assert out["entry"]["closed_note"] == "Auto-closed: email archived from inbox by user."
    reopened = _ledger(ea, "update", "--id", "t1", "--status", "open")
    assert "warning" not in reopened and reopened["entry"]["status"] == "open"


def test_update_on_an_open_row_has_no_warning(tmp_path):
    ea = tmp_path / "ea"
    (ea / "state").mkdir(parents=True)
    _ledger(ea, "add", "--id", "t2", "--type", "task", "--description", "x")
    assert "warning" not in _ledger(ea, "update", "--id", "t2", "--last-action", "y")


def test_reopening_an_archive_closed_row_marks_the_archive_as_not_evidence(tmp_path):
    ea = tmp_path / "ea"
    (ea / "state").mkdir(parents=True)
    _ledger(ea, "add", "--id", "t3", "--type", "task", "--description", "Budget to Nico")
    _ledger(ea, "close", "--id", "t3", "--user-said", "done", "--skip-archive",
            "--note", "2026-09-11: Auto-closed: email archived from inbox by user. Previous: ")
    out = _ledger(ea, "update", "--id", "t3", "--status", "open", "--last-action", "reopened")
    assert out["entry"]["archive_not_evidence"].endswith("reopened after an archive closed it")
    _ledger(ea, "add", "--id", "t4", "--type", "task", "--description", "x")
    _ledger(ea, "close", "--id", "t4", "--user-said", "done", "--skip-archive",
            "--note", "done by hand")
    assert "archive_not_evidence" not in _ledger(ea, "update", "--id", "t4", "--status", "open")["entry"]


def test_a_reopen_stamps_reopened_at_and_an_open_update_does_not(tmp_path):
    """The scrubber counts only evidence after reopened_at, so every reopen
    of a closed row records when it happened, whatever closed the row."""
    ea = tmp_path / "ea"
    (ea / "state").mkdir(parents=True)
    _ledger(ea, "add", "--id", "w1", "--type", "task", "--description", "White paper")
    assert "reopened_at" not in _ledger(ea, "update", "--id", "w1", "--notes", "n")["entry"]
    _ledger(ea, "close", "--id", "w1", "--user-said", "sent it", "--skip-archive",
            "--note", "User sent from Outlook directly")
    out = _ledger(ea, "update", "--id", "w1", "--status", "open", "--last-action", "reopened")
    assert out["entry"]["reopened_at"] >= out["entry"]["created_at"], out["entry"]


def test_a_close_sweeps_detector_siblings_and_leaves_other_rows_on_the_thread(tmp_path):
    """Closing the white paper co-closed the pilot CONOPS wait on the
    same thread. Detector rows are duplicates of one ask and sweep together;
    a task or a wait the EA logged from the thread is its own obligation."""
    ea = tmp_path / "ea"
    (ea / "state").mkdir(parents=True)
    for rid, kind in (("field-trial-sponsor-whitepaper", "task"),
                      ("field-trial-pilot-conops", "waiting"),
                      ("response-sponsor", "response_needed"),
                      ("priority-sponsor", "priority_alert")):
        _ledger(ea, "add", "--id", rid, "--type", kind, "--description", rid,
                "--conversation-id", "convC")
    out = _ledger(ea, "close", "--id", "field-trial-sponsor-whitepaper", "--user-said", "sent it",
                  "--note", "User sent from Outlook directly", "--skip-archive")
    assert sorted(out["co_closed_siblings"]) == ["priority-sponsor", "response-sponsor"], out
    conops = _ledger(ea, "get", "--id", "field-trial-pilot-conops")
    assert conops.get("entry", conops)["status"] == "open", conops
