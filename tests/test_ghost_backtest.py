"""ghost_backtest: exchanges from a fake collector, sealed against a stub
model, briefed from the store, judged and replayed by fakes, and the
report. Nothing reaches the network, the transcripts or the real
private dir."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
import mail_privacy as mp  # noqa: E402
import ghost_week as gw  # noqa: E402
import ghost_backtest as gb  # noqa: E402

GOOD = {"sensitivity": "normal", "confidential_kind": "none", "needs_reply": True,
        "ask": "Send the slides.", "deadline": "2026-10-15", "deadline_text": "by Wednesday",
        "scheduling": "none", "offered_times": [], "people": [{"name": "Pat", "role": "colleague"}],
        "tone": [], "state": "needs-reply", "domain": "research", "summary": "Pat wants the slides by Wednesday."}
MID = "AAMk" + "x" * 40 + "1"


@pytest.fixture
def priv(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(mp, "_user_addresses", lambda: {"emerson.vasquez27@example.edu"})
    monkeypatch.setattr(mp, "_profile_line", lambda: "The user is a professor.")
    monkeypatch.setattr(mp, "_default_graph", lambda: object())
    monkeypatch.setattr(mp, "_inline_records", lambda msgs, **k: None)
    mp._INDEX_CACHE.update(mtime=None, index=None)
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "enforce",
           "scheduling": "off", "shadow_until": "2099-01-01",
           "keep_from_anthropic": {"domains": ["mil"], "addresses": [], "admissions": True}}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    rl.append(mp.EXTRACTIONS, {"ts": "2026-10-14T15:00:00Z", "key": "conv1|2026-10-14T14:00:00Z", "id": MID,
                               "conversation_id": "conv1", "received": "2026-10-14T14:00:00Z",
                               "from": "morgan.vasquez41@example.edu", "subject": "Slides", "status": "ok", "result": dict(GOOD)})
    mp._INDEX_CACHE.update(mtime=None, index=None)
    return d


def _exchange(ts="2026-10-14T16:00:00Z", piped=False):
    conv = {"conversation_id": "conv1", "subject": "Slides", "message_count": 1,
            "participants": ["morgan.vasquez41@example.edu"],
            "messages": [{"id": MID, "subject": "Slides", "from": {"address": "morgan.vasquez41@example.edu"},
                          "to": ["emerson.vasquez27@example.edu"], "cc": [], "sent": "2026-10-14T14:00:00Z", "is_draft": False,
                          "has_attachments": False,
                          "text": "Could you send the slides by Wednesday? Template: https://x.example/t"}]}
    out = "sent from pat\n" if piped else "Reading mail...\n" + json.dumps(conv, indent=1) + "\n"
    return {"session": "s1", "ts": ts, "user": "The user posted this NEW message:\n\n> reply to Pat with the template\n",
            "reads": [{"verb": "get-conversation", "cmd": "graph_actions.py get-conversation --conversation-id conv1",
                       "output": out},
                      {"verb": "search-mail", "cmd": "graph_actions.py search-mail --query slides",
                       "output": json.dumps([{"id": MID, "subject": "Slides", "conversation_id": "conv1",
                                              "body_preview": "Could you"}])}],
            "reply": "Draft created to Pat.", "drafts": ["graph_actions.py create-reply --message-id x"],
            "writes": [{"path": "/home/ea-bot/.ea/state/reply-body.txt",
                        "content": "Hi Pat, the slides come Tuesday; template https://x.example/t"}], "tools": 4}


def test_collect_parses_the_extractors_lines_and_user_message_is_quoted(priv):
    ex = gb.collect(7, runner=lambda argv: json.dumps(_exchange()) + "\nnot json\n" + json.dumps({"reads": []}) + "\n")
    assert len(ex) == 1 and ex[0]["reads"][0]["verb"] == "get-conversation"
    assert gb._user_message(ex[0]["user"]) == "reply to Pat with the template"
    assert gb._parse_output("junk") is None and gb._parse_output(_exchange()["reads"][0]["output"])["message_count"] == 1


def test_extractor_runs_as_a_script_on_a_fake_transcript(tmp_path, monkeypatch):
    """The extractor itself, run with python -I on a made-up transcript dir."""
    import subprocess
    d = tmp_path / "projects"
    d.mkdir()
    rows = [
        {"type": "user", "timestamp": "2026-10-14T16:00:00Z",
         "message": {"content": "The user posted this NEW message:\n\n> reply to Pat\n"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "python3 /home/ea-bot/ea-system/preprocessor/graph_actions.py get-conversation --conversation-id c1"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                  "content": [{"type": "text", "text": '{"conversation_id": "c1"}'}]}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "Write",
             "input": {"file_path": "/home/ea-bot/.ea/state/reply-body.txt", "content": "Hi Pat"}},
            {"type": "tool_use", "id": "t3", "name": "Bash",
             "input": {"command": "python3 graph_actions.py create-reply --message-id m --body x"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"},
                                                 {"type": "tool_result", "tool_use_id": "t3", "content": "{}"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Draft created to Pat."}]}},
        {"type": "user", "timestamp": "2026-10-14T17:00:00Z", "message": {"content": "thanks"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Welcome."}]}},
    ]
    (d / "abc.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    script = gb.EXTRACTOR.replace('"/home/ea-bot/.claude/projects/-home-ea-bot-ea-system"', json.dumps(str(d)))
    p = subprocess.run([sys.executable, "-I", "-", "7", "1000", "1000", "1000", "1000"], input=script,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    lines = [json.loads(l) for l in p.stdout.splitlines()]
    assert len(lines) == 1                                   # the "thanks" turn made no read
    ex = lines[0]
    assert ex["session"] == "abc" and ex["reads"][0]["output"] == '{"conversation_id": "c1"}'
    assert ex["writes"][0]["content"] == "Hi Pat" and ex["drafts"] and ex["reply"] == "Draft created to Pat."
    assert ex["tools"] == 3


def test_prepare_seals_parsable_reads_and_briefs_the_thread(priv, monkeypatch):
    monkeypatch.setattr(gb, "_ledger_rows", lambda convs: {})
    prep = gb.prepare(_exchange())
    assert [r["parsed"] for r in prep["reads"]] == [True, True]
    r = prep["reads"][0]
    assert r["sealed"]["sealed"] is True and "https://x.example/t" not in json.dumps(r["sealed"])
    assert "https://x.example/t" in r["raw"] and r["seal_error"] == ""
    assert prep["conversations"] == ["conv1"] and prep["briefs"]["conv1"]["ask"] == "Send the slides."
    assert prep["unread_before"] == 0 and prep["redone"] == 0
    seen = []
    monkeypatch.setattr(gb, "_redo", lambda rows, **k: seen.append(len(rows)) or len(rows))
    assert gb.prepare(_exchange(), redo=True)["redone"] == 1 and seen == [1]
    prep2 = gb.prepare(_exchange(piped=True))
    assert prep2["reads"][0] == {"verb": "get-conversation", "parsed": False}


def test_run_judges_replays_and_writes_the_report(priv, monkeypatch):
    answers = [
        {"facts_used": [{"fact": "template URL", "in_record": False, "in_brief": False, "category": "url"},
                        {"fact": "due Wednesday", "in_record": True, "in_brief": True, "category": "date"}],
         "record_errors": [], "verdict_sealed": "degraded", "verdict_brief": "degraded",
         "enhancement_record": "Carry URLs.", "enhancement_brief": "Carry the links each message names."},
        {"verdict": "minor", "missing_in_b": ["the template link"], "wrong_in_b": [], "note": "B omitted the link."},
        {"facts_used": [], "record_errors": [{"field": "deadline", "record": "", "thread": "Wednesday"}],
         "verdict_sealed": "blocked", "verdict_brief": "blocked",
         "enhancement_record": "Carry URLs.", "enhancement_brief": "Keep a row for every thread the user replies in."},
    ]

    def judge(prompt, schema, which):
        assert which == "claude"
        return dict(answers.pop(0)), {"cost_usd": 0.03}
    claude = lambda prompt, model: {"text": "Hi Pat, Tuesday evening.\nDRAFT:\nHi Pat, the slides come Tuesday.",
                                    "cost_usd": 0.1, "rc": 0}
    monkeypatch.setattr(gb, "_ledger_rows", lambda convs: {"conv1": "open"})
    exs = [_exchange(ts="2026-10-13T10:00:00Z"), _exchange(ts="2026-10-14T16:00:00Z"), _exchange(piped=True)]
    rep = gb.run(days=7, max_n=5, replay=1, collector=lambda argv: "".join(json.dumps(e) + "\n" for e in exs),
                 judge_runner=judge, claude=claude, stamp="t1")
    assert rep["collected"] == 3 and rep["selected"] == 2 and rep["judged"] == 2
    assert rep["verdict_sealed"] == {"blocked": 1, "degraded": 1} and rep["blocked_share_sealed"] == 0.5
    assert rep["with_brief"] == 2 and rep["with_ledger_row"] == 2
    assert rep["verdict_brief_when_row_exists"] == {"blocked": 1, "degraded": 1}
    assert rep["missing_brief_by_category"] == {"url": 1} and rep["errors_by_field"] == {"deadline": 1}
    assert rep["replayed"] == 1 and rep["compare"] == {"minor": 1} and rep["judge_cost_usd"] == 0.19
    md = Path(rep["path"]).read_text()
    assert "Changes the ledger needs" in md and "Carry the links each message names." in md
    assert "Replay A/B" in md and "B omitted the link." in md
    rows = [json.loads(l) for l in (Path(rep["path"]).parent / "exchanges.jsonl").read_text().splitlines()]
    assert rows[0]["ts"] == "2026-10-14T16:00:00Z" and rows[0]["replay_ok"] and rows[1].get("replay_ok") is None
    assert "raw" not in json.dumps(rows[0]["reads"]) and "sealed" not in json.dumps(rows[0]["reads"])
