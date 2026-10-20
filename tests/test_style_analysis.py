#!/usr/bin/env python3
"""Tests for preprocessor/style_analysis.py (monthly voice measurement).

The module measures the whole sent corpus in Python, snapshots it, and
reports only movement worth a human's attention. The judgment of what a
drift means goes to the resident pane; these tests cover the measurement
and the gate in front of it.
"""

import json
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import style_analysis as sa  # noqa: E402


def _msg(to, body, sent_at="2026-06-01T10:00:00Z", html=False):
    return {
        "sentDateTime": sent_at,
        "toRecipients": [{"emailAddress": {"address": a, "name": a}}
                         for a in to],
        "body": {"contentType": "html" if html else "text",
                 "content": body},
    }


BODY = ("Hi Alice,\n\nThat works, let's hold Thursday at two and I will "
        "bring the eval numbers with me.\n\nKit")


def test_broadcast_and_draft_and_stub_excluded():
    rows = sa.build_rows([
        _msg([f"p{i}@x.edu" for i in range(9)], BODY),      # broadcast
        dict(_msg(["alice@x.edu"], BODY), isDraft=True),         # draft
        _msg(["bob@x.edu"], "ok"),                             # too short
        _msg(["carol@x.edu"], BODY),                             # keeper
    ])
    assert len(rows) == 1
    assert rows[0]["to"] == ["carol@x.edu"]


def test_quoted_history_and_invite_boilerplate_excluded():
    """Shares style_profiles' extraction, so the corpus measures his
    prose and not Outlook's."""
    rows = sa.build_rows([_msg(["alice@x.edu"], BODY + (
        "\n\nKit Calloway is inviting you to a scheduled Zoom "
        "meeting.\nJoin Zoom Meeting\nhttps://x/j/1\n\n"
        "From: Alice <alice@x.edu>\nquoted thread that is not his writing"))])
    assert len(rows) == 1
    assert rows[0]["words"] < 30          # only his own paragraph


def test_override_beats_domain_and_domain_is_the_fallback():
    over = {"boss@example.edu": "leadership/peers"}
    assert sa.segment(["boss@example.edu"], over,
                      "example.edu") == "leadership/peers"
    assert sa.segment(["someone@example.edu"], over,
                      "example.edu") == "internal"
    assert sa.segment(["someone@army.mil"], over, "example.edu") == "government"
    assert sa.segment(["someone@mit.edu"], over,
                      "example.edu") == "external academic"
    assert sa.segment(["someone@acme.com"], over, "example.edu") == "industry"
    assert sa.segment(["someone@gmail.com"], over, "example.edu") == "personal"


def test_first_run_reports_no_drift():
    report = sa.analyze(sa.build_rows(
        [_msg(["alice@x.edu"], BODY, f"2026-06-{i:02d}T10:00:00Z")
         for i in range(1, 20)]), "example.edu")
    assert sa.drift(report, {}) == []


def test_drift_needs_both_a_real_move_and_enough_messages():
    prev = {"overall": {"messages": 40, "median_words": 40,
                        "hedges_per_1k": 3.0},
            "segments": {"thin": {"messages": 4, "median_words": 40},
                         "solid": {"messages": 30, "median_words": 40}}}
    cur = {"overall": {"messages": 40, "median_words": 41,
                       "hedges_per_1k": 6.0},
           "segments": {"thin": {"messages": 5, "median_words": 90},
                        "solid": {"messages": 30, "median_words": 90}}}
    found = sa.drift(cur, prev)
    fields = {(d["scope"], d["field"]) for d in found}
    # a 2.5% move is wobble; a doubled hedge rate is not
    assert ("overall", "median_words") not in fields
    assert ("overall", "hedges_per_1k") in fields
    # too few messages to mean anything, however big the swing
    assert ("thin", "median_words") not in fields
    assert ("solid", "median_words") in fields


def test_report_carries_the_numbers_a_draft_is_calibrated_against():
    rows = sa.build_rows(
        [_msg(["alice@x.edu"], BODY, f"2026-06-{i:02d}T10:00:00Z")
         for i in range(1, 20)])
    r = sa.analyze(rows, "example.edu")
    o = r["overall"]
    assert o["messages"] == 19
    assert o["bare_sign_off_pct"] == 100        # BODY ends on the name
    assert o["median_words"] > 0
    assert r["greetings"]["Hi {name},"] == 19
    assert sum(b["n"] for b in r["length_bands"].values()) == 19


def test_html_mail_is_measured_with_its_line_structure():
    rows = sa.build_rows([_msg(["alice@x.edu"],
                               "<div>Hi Alice,</div><div>Ship it once the "
                               "eval clears and tell me.</div>"
                               "<div>Best,</div><div>Kit</div>",
                               html=True)])
    assert rows[0]["greeting"] == "Hi {name},"
    assert rows[0]["closing"] == "Best"


def test_em_dash_forms_are_counted_apart():
    """CLAUDE.md bans both, but they behave differently: the true
    character arrives pasted in, the double hyphen he actually types.
    Merging them overstates how absent the rule's target is."""
    rows = sa.build_rows([
        _msg(["alice@x.edu"], "Hi Alice,\n\nThanks for reaching out -- happy "
                          "to hop on a call and hear more about it.\n\nKit"),
        _msg(["bob@x.edu"], "Hi Bob,\n\nThe **TTO**—which operates as Tech "
                          "Ventures here—owns that decision entirely.\n\nKit"),
    ])
    d = sa.analyze(rows, "example.edu")["em_dashes"]
    assert d["double_hyphen"]["occurrences"] == 1
    assert d["double_hyphen"]["typed_by_hand"] == 1
    assert d["true_em_dash"]["occurrences"] == 2
    # markdown in the body means it was pasted, so it is not his hand
    assert d["true_em_dash"]["in_pasted_markdown"] == 1
    assert d["true_em_dash"]["typed_by_hand"] == 0


def test_signature_delimiter_is_not_a_dash_he_typed():
    rows = sa.build_rows([_msg(["alice@x.edu"],
                               "Hi Alice,\n\nSounds good, see you then and "
                               "thanks for organizing it.\n\nKit\n\n--\n"
                               "Kit Calloway\nAssociate Professor")])
    d = sa.analyze(rows, "example.edu")["em_dashes"]
    assert d["double_hyphen"]["occurrences"] == 0


def test_render_names_the_drift_it_found():
    report = {"window": {"from": "2026-05-01", "to": "2026-08-01"},
              "overall": {"messages": 10, "words": 400, "median_words": 36,
                          "p90_words": 180, "bare_sign_off_pct": 75,
                          "hedges_per_1k": 3.2},
              "segments": {}}
    out = sa.render(report, [{"scope": "overall", "field": "median_words",
                              "was": 36, "now": 90, "change_pct": 150}])
    assert "median_words" in out and "+150%" in out
    assert "No drift" not in out
    assert "No drift" in sa.render(report, [])


def test_snapshot_round_trips_under_a_month_key():
    orig_dir, orig_latest = sa.REPORT_DIR, sa.LATEST
    try:
        with tempfile.TemporaryDirectory() as td:
            sa.REPORT_DIR = Path(td) / "style-analysis"
            sa.LATEST = sa.REPORT_DIR / "latest.json"
            report = {"window": {"from": "2026-05-02", "to": "2026-08-05"},
                      "overall": {"messages": 3}}
            path = sa.save(report)
            assert path.name == "2026-08.json"
            assert sa.load_previous()["overall"]["messages"] == 3
    finally:
        sa.REPORT_DIR, sa.LATEST = orig_dir, orig_latest


def test_missing_audience_config_falls_back_to_domains():
    orig = sa.AUDIENCE_CONFIG
    try:
        sa.AUDIENCE_CONFIG = Path("/nonexistent/style-audiences.json")
        assert sa.load_audiences() == {}
        assert sa.segment(["someone@example.edu"], {},
                          "example.edu") == "internal"
    finally:
        sa.AUDIENCE_CONFIG = orig


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
