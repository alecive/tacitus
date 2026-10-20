"""The remote briefing's staleness evals (tests/evals/remote_briefing_eval.py):
the fixtures reach remote_briefing in the preprocessor's shape, the code-held
cases pass on a stand-in model, and each check catches the post it exists
for. The real-model run is nightly (tests/evals/run_full_if_changed.sh)."""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("EA_DIR", tempfile.mkdtemp(prefix="ea-test-rbe-"))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "evals"))
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import remote_briefing_eval as rbe  # noqa: E402


def test_the_fixtures_reach_remote_briefing_in_the_preprocessors_shape():
    _fixture, b, now = rbe.load("completed-with-deadline")
    [mail] = b["new_needs_response"]
    assert mail["from"] == "edie.stavros43@example.com" and mail["from_name"] == "Tam Marchetti"
    assert mail["body_preview"] and mail["conversation_id"]
    assert [r["id"] for r in b["recently_closed"]] == ["tam-marchetti-certificate-curriculum-review-20260327"]
    assert "today_top" in b and now.tzinfo is not None
    _f, calm, _n = rbe.load("empty-calm-day")
    assert [o["status"] for o in calm["open_obligations"]] == ["snoozed"]


def test_the_code_held_cases_pass_on_a_stand_in_model(capsys):
    assert rbe.main(["--stub"]) == 0
    out = capsys.readouterr().out
    assert "PASS  completed-with-deadline" in out and "PASS  empty-calm-day" in out
    assert "already-replied-thread" not in out      # the model's call, not code's


def test_each_check_catches_the_post_it_exists_for():
    head = ":sunny: *Morning Briefing, Thu Oct 15*\n\n:dart: *Get done today* (in this order)\n"
    foot = "\nOpen obligations: 1 | _written by the remote model_"
    overdue = head + "1. *Send the certificate curriculum* (Tam Marchetti): OVERDUE since Apr 3\n" + foot
    assert not all(c["ok"] for c in rbe._completed_with_deadline(overdue))
    jess = (head + "Nothing is pressing today.\n\n:question: *Needs you* (1)\n"
            "- *Re: Lab Meeting Agenda* (Jo Carrow): reply to her\n" + foot)
    checks = {c["check"]: c["ok"] for c in rbe._already_replied(jess)}
    assert checks == {"Jo's FYI is not an action": False, "Harper's new question is raised": False}
    noisy = head + "1. *Acme DLI* : URGENT\n" + foot
    assert not all(c["ok"] for c in rbe._calm_day(noisy))
    assert all(c["ok"] for c in rbe.frame(head + "Nothing is pressing today.\n" + foot))
