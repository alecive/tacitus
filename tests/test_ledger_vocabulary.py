"""The ledger's vocabulary: one word per meaning.

What must hold: a main-EA row's type is one of ledger.TYPES, other
spellings fold into it and anything else is refused with the list; a
plugin row keeps its own type; a status is open, follow_up, snoozed or
completed ('closed' folds to completed, 'waiting' makes the row an open
waiting row); a --set key that spells a column lands in the column, hyphen
or not; date_identified is stored in one format; list filters take the old
words; the detectors write canonical types; `normalize` folds existing rows
the same way, keeps every note, writes nothing without --apply, and a
second run changes nothing.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "preprocessor" / "ledger.py"
sys.path.insert(0, str(ROOT / "preprocessor"))

import ledger as ledger_mod  # noqa: E402


def _cli(ea: Path, *args: str, ok: bool = True) -> dict:
    r = subprocess.run([sys.executable, str(LEDGER), *args],
                       env=dict(os.environ, EA_DIR=str(ea)),
                       capture_output=True, text=True, timeout=30)
    if ok:
        assert r.returncode == 0, (r.stdout, r.stderr)
    return json.loads(r.stdout)


@pytest.fixture
def ea(tmp_path):
    d = tmp_path / "ea"
    (d / "state").mkdir(parents=True)
    return d


def _entry(out: dict) -> dict:
    return out.get("entry", out)


@pytest.mark.parametrize("given,stored", [
    ("awaiting", "waiting"), ("waiting_on", "waiting"), ("follow-up", "follow_up"),
    ("followup", "follow_up"), ("scheduling_request", "scheduling"),
    ("priority_alert", "response_needed"), ("action_item", "task"),
    ("travel_reimbursement", "reimbursement"), ("Task", "task"),
])
def test_add_folds_a_spelling_into_its_type(ea, given, stored):
    out = _cli(ea, "add", "--id", "r1", "--type", given, "--description", "d")
    assert _entry(out)["type"] == stored


def test_add_refuses_an_unknown_type_and_names_the_allowed_ones(ea):
    out = _cli(ea, "add", "--id", "r1", "--type", "vibes", "--description", "d", ok=False)
    assert "unknown type" in out["error"] and "response_needed" in out["error"]
    assert "error" in _cli(ea, "get", "--id", "r1", ok=False)


def test_a_plugin_row_keeps_its_own_type(ea):
    out = _cli(ea, "add", "--id", "ff-1", "--type", "ff_docket_tier1", "--description", "d",
               "--set", "source=plugin:ff-watcher")
    assert _entry(out)["type"] == "ff_docket_tier1"


def test_statuses_fold_and_an_unknown_one_is_refused(ea):
    _cli(ea, "add", "--id", "r1", "--type", "task", "--description", "d")
    # "closed" folds to completed; a model's close by update is a proposal.
    proposed = _cli(ea, "update", "--id", "r1", "--status", "closed")
    assert proposed["close"] == "proposed" and _entry(proposed)["status"] == "open"
    done = _entry(_cli(ea, "close", "--id", "r1", "--user-said", "done", "--skip-archive"))
    assert done["status"] == "completed" and done.get("closed_at")
    out = _cli(ea, "update", "--id", "r1", "--status", "sorta", ok=False)
    assert "unknown status" in out["error"]
    _cli(ea, "add", "--id", "r2", "--type", "action_item", "--description", "d")
    waiting = _entry(_cli(ea, "update", "--id", "r2", "--status", "waiting"))
    assert (waiting["status"], waiting["type"]) == ("open", "waiting")


def test_a_hyphenated_set_key_lands_in_its_column(ea):
    """The property plugin's follow-up date and another plugin's
    notes went into metadata as last-action / follow-up-after and were
    never read."""
    _cli(ea, "add", "--id", "r1", "--type", "task", "--description", "d")
    row = _entry(_cli(ea, "update", "--id", "r1", "--set",
                      "last-action=Tam confirmed", "follow-up-after=2026-10-08",
                      "type=awaiting"))
    assert row["last_action"] == "Tam confirmed"
    assert row["follow_up_after"] == "2026-10-08"
    assert row["type"] == "waiting"
    assert "last-action" not in row and "follow-up-after" not in row


def test_date_identified_is_stored_in_one_format(ea):
    row = _entry(_cli(ea, "add", "--id", "r1", "--type", "task", "--description", "d",
                      "--date-identified", "2026-09-22T16:50:03.357758+00:00"))
    assert row["date_identified"] == "2026-09-22T16:50:03Z"
    assert ledger_mod.iso_utc("2026-08-11") == "2026-08-11T00:00:00Z"
    assert ledger_mod.iso_utc("2026-10-06T12:16:36-06:00") == "2026-10-06T18:16:36Z"
    assert ledger_mod.iso_utc("not a date") == "not a date"


def test_list_filters_take_the_old_words(ea):
    _cli(ea, "add", "--id", "r1", "--type", "reimbursement", "--description", "d")
    _cli(ea, "add", "--id", "r2", "--type", "task", "--description", "d")
    _cli(ea, "close", "--id", "r2", "--note", "done", "--user-said", "done", "--skip-archive")
    by_type = _cli(ea, "list", "--type", "travel_reimbursement,watch")
    assert [e["id"] for e in by_type["entries"]] == ["r1"]
    by_status = _cli(ea, "list", "--status", "closed")
    assert [e["id"] for e in by_status["entries"]] == ["r2"]


def test_types_lists_the_vocabulary(ea):
    out = _cli(ea, "types")
    assert set(out["types"]) == set(ledger_mod.TYPES)
    assert out["statuses"] == list(ledger_mod.STATUSES)


def test_add_entry_refuses_an_unknown_main_type(tmp_path):
    db = tmp_path / "l.db"
    conn = sqlite3.connect(db)
    conn.executescript(ledger_mod.SCHEMA)
    for m in ledger_mod.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="unknown type"):
        ledger_mod.add_entry("x", "priority_thing", "d", db_path=db)
    assert ledger_mod.add_entry("y", "priority_alert", "d", db_path=db)
    assert ledger_mod.get_entry("y", db_path=db)["type"] == "response_needed"


def test_the_detectors_write_canonical_types():
    src = (ROOT / "preprocessor" / "preprocess.py").read_text()
    for old in ("'scheduling_request'", '"scheduling_request"', '"priority_alert"'):
        assert old not in src, old
    assert '_ledger_mod.canonical_type(thread["category"])' in src
    assert "datetime.now(timezone.utc).isoformat(),\n            item.get" not in src


# ── normalize ───────────────────────────────────────────────────────────

def _legacy_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(ledger_mod.SCHEMA)
    for m in ledger_mod.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    rows = [
        # id, type, status, notes, last_action, follow_up_after, metadata, date, source
        ("a", "awaiting", "open", "", "", None, "{}", "2026-08-31T18:20:02.340992+00:00", None),
        ("b", "action_item", "waiting", "", "", None, "{}", "2026-08-11", None),
        ("c", "priority_alert", "closed", "", "", None, "{}", "2026-06-01T10:00:00Z", None),
        ("d", "followup", "open", "orig notes", "", "2026-09-08",
         json.dumps({"last-action": "plugin note", "follow-up-after": "2026-10-08"}),
         "2026-09-01T00:00:00Z", "plugin:property"),
        ("e", "task", "open", "n", "column note", None,
         json.dumps({"last-action": "metadata note", "conversation_id": "None"}),
         "2026-09-01T00:00:00Z", None),
        ("f", "ff_docket_tier1", "open", "", "", None, "{}", "2026-09-01T00:00:00Z",
         "plugin:ff-watcher"),
    ]
    for (i, t, st, notes, la, fua, meta, date, src) in rows:
        conn.execute(
            """INSERT INTO ledger (id, type, status, description, notes, last_action,
                   follow_up_after, metadata, date_identified, source, created_at, updated_at)
               VALUES (?, ?, ?, 'd', ?, ?, ?, ?, ?, ?, '2026-09-01T00:00:00Z',
                       '2026-09-01T00:00:00Z')""",
            (i, t, st, notes, la, fua, meta, date, src))
    conn.commit()
    conn.close()
    return path


def _rows(path: Path) -> dict:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    out = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM ledger")}
    conn.close()
    return out


def test_normalize_reports_without_writing_then_applies_once(ea, tmp_path):
    db = _legacy_db(tmp_path / "legacy.db")
    before = _rows(db)
    dry = _cli(ea, "normalize", "--db", str(db))
    assert dry["applied"] is False and dry["changed"] == 5 and dry["unknown_types"] == []
    assert _rows(db) == before

    applied = _cli(ea, "normalize", "--db", str(db), "--apply")
    assert applied["changed"] == 5
    rows = _rows(db)
    assert rows["a"]["type"] == "waiting"
    assert rows["a"]["date_identified"] == "2026-08-31T18:20:02Z"
    assert (rows["b"]["type"], rows["b"]["status"]) == ("waiting", "open")
    assert rows["b"]["date_identified"] == "2026-08-11T00:00:00Z"
    assert (rows["c"]["type"], rows["c"]["status"]) == ("response_needed", "completed")
    # the plugin row: alias folded, empty column filled, later date wins
    assert rows["d"]["type"] == "follow_up"
    assert rows["d"]["last_action"] == "plugin note"
    assert rows["d"]["follow_up_after"] == "2026-10-08"
    assert json.loads(rows["d"]["metadata"]) == {}
    # a full column keeps its text; the metadata note is kept in notes
    assert rows["e"]["last_action"] == "column note"
    assert "metadata note" in rows["e"]["notes"] and rows["e"]["notes"].startswith("n")
    assert json.loads(rows["e"]["metadata"]) == {}
    assert rows["f"]["type"] == "ff_docket_tier1"

    again = _cli(ea, "normalize", "--db", str(db), "--apply")
    assert again["changed"] == 0, again


def test_every_alias_lands_on_a_type():
    assert set(ledger_mod.TYPE_ALIASES.values()) <= set(ledger_mod.TYPES)
    assert set(ledger_mod.STATUS_ALIASES.values()) <= set(ledger_mod.STATUSES)
    assert not set(ledger_mod.TYPE_ALIASES) & set(ledger_mod.TYPES)
