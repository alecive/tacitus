"""The preference-rules digest reports each change once and seeds silently.

An added rule shapes what the EA does, and the resident can add one from a
Slack message, so the digest is the user's visibility on rule changes he did
not make. First run: silent seed. Then: adds and retirements, once each.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Sandbox before import: the module derives its store and cursor paths from
# EA_DIR, and a test run must never write the live store.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-prefs-digest-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))
import preferences as P  # noqa: E402


def _store():
    return {"version": 1, "rules": [], "calibration": []}


def test_first_run_seeds_and_reports_nothing():
    data = _store()
    P.add_rule(data, kind="avoid", scopes=["funding"], text="Skip standing BAAs.")
    report, seen = P.digest(data, None)          # no cursor yet
    assert report == {"added": [], "retired": []}
    assert P.render_digest(report) == ""
    assert set(seen["rules"]) == {"skip-standing-baas"}


def test_a_new_rule_is_reported_once():
    data = _store()
    _, seen = P.digest(data, None)               # seed empty
    P.add_rule(data, kind="avoid", scopes=["briefing"], text="Never show me X.",
               source="slack")
    report, seen2 = P.digest(data, seen)
    assert [r["text"] for r in report["added"]] == ["Never show me X."]
    assert "*Added (1)*\n- *avoid* [briefing] Never show me X. _via chat_" \
        in P.render_digest(report)
    # second run: nothing new
    report2, _ = P.digest(data, seen2)
    assert report2 == {"added": [], "retired": []}


def test_a_retirement_is_reported_once():
    data = _store()
    P.add_rule(data, kind="prefer", scopes=["*"], text="Do Z.", rule_id="do-z")
    _, seen = P.digest(data, None)               # seed with the active rule
    P.retire_rule(data, "do-z", note="changed my mind")
    report, seen2 = P.digest(data, seen)
    assert [r["text"] for r in report["retired"]] == ["Do Z."]
    assert "*Retired (1)*\n- [*] Do Z." in P.render_digest(report)
    assert P.digest(data, seen2)[0] == {"added": [], "retired": []}


def test_added_and_retired_between_runs_shows_as_retired_not_added():
    data = _store()
    _, seen = P.digest(data, None)
    P.add_rule(data, kind="fact", scopes=["funding"], text="Transient.", rule_id="t")
    P.retire_rule(data, "t")
    report, _ = P.digest(data, seen)
    assert report["added"] == [] and [r["text"] for r in report["retired"]] == ["Transient."]


def test_load_and_save_follow_a_repointed_store(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere.json"
    P.save({"version": 1, "rules": [{"id": "keep", "text": "Keep."}], "calibration": []},
           elsewhere)
    store = tmp_path / "preferences.json"
    monkeypatch.setattr(P, "PREFS_FILE", store)
    data = P.load()
    P.add_rule(data, kind="avoid", scopes=["*"], text="R.")
    P.save(data)
    assert [r["text"] for r in P.load(store)["rules"]] == ["R."]
    assert [r["id"] for r in P.load(elsewhere)["rules"]] == ["keep"]


def test_cli_digest_advances_the_cursor(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(P, "PREFS_FILE", tmp_path / "preferences.json")
    monkeypatch.setattr(P, "DIGEST_CURSOR", tmp_path / "cursor.json")
    P.save(_store(), tmp_path / "preferences.json")
    assert P.main(["digest"]) == 0                     # seed
    assert capsys.readouterr().out.strip() == ""
    data = P.load()
    P.add_rule(data, kind="avoid", scopes=["briefing"], text="Hide Y.")
    P.save(data)
    assert P.main(["digest"]) == 0                     # reports Hide Y once
    assert "Hide Y." in capsys.readouterr().out
    assert P.main(["digest"]) == 0                     # cursor advanced -> silent
    assert capsys.readouterr().out.strip() == ""
    # --dry-run never advances the cursor
    data = P.load(); P.add_rule(data, kind="avoid", scopes=["*"], text="Q."); P.save(data)
    assert P.main(["digest", "--dry-run"]) == 0 and "Q." in capsys.readouterr().out
    assert P.main(["digest", "--dry-run"]) == 0 and "Q." in capsys.readouterr().out


def test_a_flood_of_rules_stays_readable():
    data = _store()
    _, seen = P.digest(data, None)
    for _ in range(20):
        P.add_rule(data, kind="avoid", scopes=["*"], text="Q.")
    for i in range(15):
        P.add_rule(data, kind="fact", scopes=["funding"], text=f"Fact {i}.")
    report, _ = P.digest(data, seen)
    text = P.render_digest(report)
    assert "*Added (35)*" in text
    assert "- *avoid* [*] Q. _via edit_ (x20)" in text
    bullets = [ln for ln in text.splitlines() if ln.startswith("- ")]
    assert len(bullets) == P.DIGEST_MAX_LINES + 1
    assert bullets[-1].startswith("- 4 more.")
