"""sched_evolve: the prompt evolution's code is the grader.

Pure tests with a fake judge, a fake model and a fake resident runner;
the store is a temporary directory."""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import sched_evolve as se  # noqa: E402
import sched_pipeline as sp  # noqa: E402
import sched_render as sr  # noqa: E402

USER = {"name": "Kit Calloway", "first": "Kit", "email": "wren.fairbanks84@example.edu", "zone": "America/Denver"}
INTENT = {"mode": "propose", "sender_name": "Sol Ashcombe", "sender_email": "sol.ashcombe@example.org",
          "sender_timezone": "America/New_York", "duration_minutes": 30, "window_start": "2026-11-02",
          "window_end": "2026-11-06", "preferred_days": ["2026-11-06"], "excluded_days": [], "offered_times": [],
          "other_attendees": [], "note_to_user": ""}
THREAD = [{"id": "m1", "conversation_id": "c1", "from": {"address": "sol.ashcombe@example.org"},
           "sent": "2026-10-14T21:07:00Z", "text": "Any day in the first week of November works, though Friday would be best."}]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(se._rl, "store_dir", lambda: tmp_path)
    return tmp_path


def test_request_text_and_conversation_from_reads():
    user = ("# Scheduling Assistant\n...tools...\nThe user posted this NEW message:\n\n> Follow up with Sol\n> with times that work.\n")
    assert se.request_text(user) == "Follow up with Sol\nwith times that work."
    reads = [{"verb": "search-mail", "cmd": "python3 x.py search-mail --query Sol"},
             {"verb": "get-conversation", "cmd": 'python3 x.py get-conversation --conversation-id "AAQkAQQQQQQQQQQQQQAQQQQQQQQQQQQQQQQQ" --max-chars 1200'}]
    assert se.conversation_from_reads(reads) == "AAQkAQQQQQQQQQQQQQAQQQQQQQQQQQQQQQQQ"
    assert se.conversation_from_reads(reads[:1]) == ""


def test_score_intent_weights_fields_and_ignores_scheduling_fields_of_an_other_label():
    score, misses = se.score_intent(dict(INTENT), dict(INTENT))
    assert score == 1.0 and misses == {}
    got = dict(INTENT, mode="validate", sender_timezone="US/Eastern", window_end="2026-11-05")
    score, misses = se.score_intent(got, INTENT)
    assert set(misses) == {"mode", "window_end"}          # US/Eastern counts as New York
    assert score == pytest.approx((13 - 4) / 13)
    other = dict(INTENT, mode="other")
    score, misses = se.score_intent(dict(INTENT, mode="other", window_start="x"), other)
    assert score == 1.0


def test_shift_window_moves_a_past_window_forward_by_whole_weeks():
    args = {"earliest_date": "2026-09-14", "latest_date": "2026-09-18",
            "attendees": [{"email": "a@b.c", "availability": [{"day": "2026-09-15", "start": "00:00", "end": "24:00"}]}]}
    out = se._shift_window(args)
    start = date.fromisoformat(out["earliest_date"])
    assert start > date.today() and start.weekday() == 0 and (start - date(2026, 9, 14)).days % 7 == 0
    assert out["attendees"][0]["availability"][0]["day"] == (start + timedelta(days=1)).isoformat()
    future = {"earliest_date": "2099-01-04", "latest_date": "2099-01-08", "attendees": []}
    assert se._shift_window(future) == future


def test_evaluate_scores_each_turn_with_a_fake_model(store):
    se._write_rows(se.CORPUS, [{"session": "s1", "ts": "2026-10-14T15:30:00Z", "request": "Follow up with Sol", "messages": THREAD},
                               {"session": "s2", "ts": "2026-10-14T15:30:00Z", "request": "no thread", "messages": []}])
    se._write_rows(se.LABELS, [{"session": "s1", "ts": "2026-10-14T15:30:00Z", "intent": INTENT},
                               {"session": "s2", "ts": "2026-10-14T15:30:00Z", "intent": INTENT}])
    calls = []

    def llm(messages, *, purpose, schema, **kw):
        calls.append(schema)
        if schema is sp.INTENT_SCHEMA:
            return dict(INTENT, window_end="2026-11-05"), {"latency_ms": 3}
        return {"picks": ["o1", "o2"], "preferred_day_available": True, "opening": "", "closing": "", "note_to_user": ""}, {}

    def runner(cmd):
        opts = [{"start": "2026-11-06T09:30:00-07:00", "end": "2026-11-06T10:00:00-07:00"},
                {"start": "2026-11-02T14:15:00-07:00", "end": "2026-11-02T14:45:00-07:00"}]
        sr.label_options(opts, [])
        return 0, json.dumps({"status": "ok", "options": opts}), ""

    rep = se.evaluate({"intent": sp.INTENT_PROMPT, "pick": sp.PICK_PROMPT, "locate": sp.LOCATE_PROMPT, "name": "t"},
                      parallel=1, user=USER, llm=llm, runner=runner)
    assert rep["n"] == 1 and rep["misses"] == {"window_end": 1} and rep["perfect"] == 0
    assert rep["score"] == pytest.approx(12 / 13, abs=1e-3)
    assert rep["pick_turns"] == 1 and rep["pick_clean"] == 1 and rep["rows"][0]["picked"] == ["o1", "o2"]
    assert "intent agreement" in se.summary_text(rep)


def test_propose_prompts_keeps_only_complete_rewrites():
    good = sp.INTENT_PROMPT.replace("Fields:", "Fields (read the thread twice):")
    judge = lambda prompt, schema: {"prompts": [good, "too short", sp.INTENT_PROMPT.replace("{weeks}", "")]}
    rep = {"score": 0.8, "n": 10, "rows": []}
    out = se.propose_prompts(sp.INTENT_PROMPT, rep, 3, judge=judge)
    assert out == [good]


def test_evolve_keeps_the_best_child_and_writes_the_prompts_file(store):
    se._write_rows(se.CORPUS, [{"session": "s1", "ts": "2026-10-14T15:30:00Z", "request": "Follow up with Sol", "messages": THREAD}])
    se._write_rows(se.LABELS, [{"session": "s1", "ts": "2026-10-14T15:30:00Z", "intent": INTENT}])
    better = sp.INTENT_PROMPT + "\nRead the dates twice."

    def llm(messages, *, purpose, schema, **kw):
        system = messages[0]["content"]
        # the rewritten prompt gets the window right; the default does not
        ok = "Read the dates twice." in system
        return dict(INTENT, window_end="2026-11-06" if ok else "2026-11-05"), {"latency_ms": 1}

    judge = lambda prompt, schema: {"prompts": [better, sp.INTENT_PROMPT + "\nNo change."]}
    best = se.evolve(1, 2, parallel=1, user=USER, llm=llm, judge=judge, with_pick=False,
                     start={"intent": sp.INTENT_PROMPT, "pick": sp.PICK_PROMPT, "locate": sp.LOCATE_PROMPT, "name": "default"})
    assert best["name"] == "gen1-1" and best["score"] == 1.0
    written = json.loads((store / se.PROMPTS).read_text())
    assert written["intent"] == better and written["name"] == "gen1-1"
    assert sp.load_prompts(store)["intent"] == better
    hist = se._rows(se.HISTORY)
    assert [h["gen"] for h in hist] == [0, 1, 1] and hist[1]["score"] == 1.0


def test_shadow_records_each_variant_with_claudes_times(store, monkeypatch):
    monkeypatch.setattr(sp, "user_profile", lambda reader=None: USER)
    (store / "tuning").mkdir()
    (store / sp.VARIANTS_FILE).write_text(json.dumps([{"name": "a"}, {"name": "b", "pick": sp.PICK_PROMPT + " B."}]))
    assert [v["name"] for v in sp.load_variants(store)] == ["a", "b"]

    def llm(messages, *, purpose, schema, **kw):
        if schema is sp.INTENT_SCHEMA:
            return dict(INTENT), {}
        return {"picks": ["o1"], "preferred_day_available": True, "opening": "Friday works.", "closing": "", "note_to_user": ""}, {}

    def runner(cmd):
        argv = cmd[cmd.index("python3") + 2:]
        if argv[0] == "search-mail":
            return 0, json.dumps([{"id": "m1", "conversation_id": "c1", "subject": "PhD", "from": {"address": "sol.ashcombe@example.org"},
                                   "received": "2026-10-14T21:07:00Z"}]), ""
        if argv[0] == "get-conversation":
            return 0, json.dumps({"messages": THREAD + [{"id": "m9", "conversation_id": "c1", "sent": "2026-10-15T00:00:00Z",
                                                          "from": {"address": "sol.ashcombe@example.org"}, "text": "later"}]}), ""
        opts = [{"start": "2026-11-06T09:30:00-07:00", "end": "2026-11-06T10:00:00-07:00"}]
        sr.label_options(opts, [{"email": "sol.ashcombe@example.org", "timezone": "America/New_York"}])
        return 0, json.dumps({"status": "ok", "options": opts}), ""

    job = {"run_id": "r1", "ts": "2026-10-14T21:30:00Z", "thread_ts": "t", "lane": "sched-channel",
           "user_msg": "Follow up with Sol with times that work that week.",
           "claude": {"reply": "Draft made: Fri Nov 6, 9:30-10:00 AM MT and Mon Nov 2, 2:15-2:45 PM MT", "drafts": []}}
    recs = sp.shadow(job, variants=sp.load_variants(store), llm=llm, runner=runner)
    assert [r["prompts"] for r in recs] == ["a", "b"]
    assert all(r["status"] == "ok" and r["write"] is None and r["as_of"] == "2026-10-14T21:30:00Z" for r in recs)
    assert recs[0]["target"]["message_id"] == "m1"           # the later message was dropped
    assert recs[0]["compare"]["overlap"] == 0.5 and len(recs[0]["compare"]["claude"]) == 2
    stored = [json.loads(l) for l in (store / sp.TURNS).read_text().splitlines()]
    assert len(stored) == 2 and stored[1]["shadow_of"]["run_id"] == "r1"
    assert "times shared with Claude 0.50" in sp.report(days=365)
