#!/usr/bin/env python3
"""The conversation index (preprocessor/session_index.py): what every
other session on the host is doing, built from history.jsonl, the
transcript tails and the two registries. Fixtures are synthetic; the
module never reads a whole transcript.

Usage:
    python3 tests/test_session_index.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="ea-test-sidx-"))
EA_DIR = ROOT / "ea"
CLAUDE_HOME = ROOT / "claude"
for d in (EA_DIR / "state", EA_DIR / "config", CLAUDE_HOME / "projects" / "-home-ea-bot-ea-system"):
    d.mkdir(parents=True, exist_ok=True)
os.environ["EA_DIR"] = str(EA_DIR)
os.environ["EA_CLAUDE_HOME"] = str(CLAUDE_HOME)
os.environ["EA_CLAUDE_PROJECT_DIR"] = "-home-ea-bot-ea-system"

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import session_index as si  # noqa: E402

SESS = CLAUDE_HOME / "projects" / "-home-ea-bot-ea-system"
FABLE = "d488ed1f-f075-4ddd-bbf2-eb382ad3eb87"
OPUS = "1d430d9f-8687-4531-a931-d23b62b4902d"
THREAD = "aaaaaaaa-1111-2222-3333-444444444444"
ME = "eeeeeeee-1111-2222-3333-444444444444"
NOW = datetime.now(timezone.utc)


def _rec(kind, text, ts, model="", tool_result=False):
    if tool_result:
        content = [{"type": "tool_result", "content": text}]
    else:
        content = [{"type": "text", "text": text}]
    msg = {"role": kind, "content": content}
    if model:
        msg["model"] = model
    return json.dumps({"type": kind, "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                       "message": msg, "sessionId": "x"})


def _transcript(sid, turns, minutes_ago, model=""):
    p = SESS / f"{sid}.jsonl"
    t = NOW - timedelta(minutes=minutes_ago)
    lines = []
    for i, (kind, text) in enumerate(turns):
        lines.append(_rec(kind, text, t + timedelta(seconds=i), model=model))
    p.write_text("\n".join(lines) + "\n")
    os.utime(p, (t.timestamp(), t.timestamp()))


def _history(rows):
    (CLAUDE_HOME / "history.jsonl").write_text("\n".join(
        json.dumps({"display": d, "sessionId": sid, "project": "/home/ea-bot/ea-system",
                    "timestamp": int((NOW - timedelta(minutes=m)).timestamp() * 1000)})
        for sid, d, m in rows) + "\n")


def _seed():
    for p in SESS.glob("*.jsonl"):
        p.unlink()
    _transcript(FABLE, [("user", "is it feasible to climb the ladder?"),
                        ("assistant", "Feasible, with two corrections to the premise. " * 3),
                        ("user", "[tool result noise]"),
                        ("user", "then send me your response above to the slack thread verbatim"),
                        ("assistant", "Posted to the thread.")], 12, model="claude-fable-5-1")
    _transcript(OPUS, [("user", "Try again."), ("assistant", "All six went out yesterday evening.")], 180,
                model="claude-opus-5")
    _transcript(THREAD, [("user", "[From the private #ea-x channel, scoped to project 'x'.]\n\nDraft a reply to Sol"),
                         ("assistant", "Draft created to Sol re: abstract.")], 60 * 30)
    _transcript(ME, [("user", "hello"), ("assistant", "hi")], 1)
    _transcript("99999999-1111-2222-3333-444444444444", [("assistant", "old")], 60 * 24 * 20)
    _history([(FABLE, "then send me your response above to the slack thread verbatim", 12),
              (OPUS, "Try again.", 180), (OPUS, "/model opus-4-8", 200),
              (THREAD, "[Pasted text #1 +173 lines]", 60 * 30),
              (ME, "hello", 1)])
    # registries: the thread has a title, the fable session has a room
    import slack_sessions as ss
    ss.REGISTRY = EA_DIR / "state" / "slack-sessions.json"
    ss.REGISTRY.write_text(json.dumps({"1788000000.1": {
        "session_id": THREAD, "created": NOW.isoformat(), "last_used": NOW.isoformat(),
        "meta": {"title": "Draft a reply to Sol", "channel": "C0MAIN"}}}))
    import ea_channels as ch
    ch.REGISTRY = EA_DIR / "config" / "ea-channels.json"
    ch.REGISTRY.unlink(missing_ok=True)
    ch.register("C0ROOM0001", name="tmp-fable-field", kind="session", session_id=FABLE,
                model="claude-fable-5-1")


def test_scan_finds_every_recent_session_with_where_and_words():
    _seed()
    rows = si.scan(days=7, sessions_dir=SESS, history_path=CLAUDE_HOME / "history.jsonl",
                   exclude={ME})
    by = {r["session_id"]: r for r in rows}
    assert [r["session_id"] for r in rows] == [FABLE, OPUS, THREAD], [r["session_id"] for r in rows]
    assert ME not in by and "99999999-1111-2222-3333-444444444444" not in by
    f = by[FABLE]
    assert f["kind"] == "room" and f["where"] == "#tmp-fable-field (session room)"
    assert f["model"] == "claude-fable-5-1" and f["prompts"] == 1
    assert f["last_prompt"].startswith("then send me your response above")
    assert f["last_reply"] == "Posted to the thread."
    o = by[OPUS]
    assert o["kind"] == "shell" and o["where"] == "shell session" and o["prompts"] == 2
    assert o["last_prompt"] == "Try again." and o["model"] == "claude-opus-5"
    t = by[THREAD]
    assert t["kind"] == "thread" and t["where"] == "Slack thread 'Draft a reply to Sol'"
    # the routing prefix is stripped from the transcript's last user text
    assert t["last_prompt"] == "Draft a reply to Sol"


def test_brief_is_bounded_and_names_rooms_first():
    _seed()
    rows = si.scan(days=7, sessions_dir=SESS, history_path=CLAUDE_HOME / "history.jsonl", exclude={ME})
    text = si.brief(rows, max_chars=2200)
    assert text.startswith("CONVERSATION INDEX (other sessions on this host")
    import re
    assert re.search(r"#tmp-fable-field \(session room\), claude-fable-5-1 \| 1[0-9]m ago \| 1 prompts \| `d488ed1f`", text), text
    assert "asked: then send me your response above" in text
    assert "last:  Posted to the thread." in text
    assert "Slack thread 'Draft a reply to Sol'" in text
    assert len(text) <= 2200
    short = si.brief(rows, max_chars=300)
    assert "(cut)" in short and len(short) <= 320
    assert si.brief([], 2200) == ""


def test_tail_read_survives_a_torn_first_line_and_big_files():
    p = SESS / "big.jsonl"
    filler = json.dumps({"type": "assistant", "timestamp": "2026-09-05T10:00:00.000Z",
                         "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * 500}]}})
    p.write_text("\n".join([filler] * 400 + [
        _rec("user", "the last question", NOW), _rec("assistant", "the last answer", NOW)]) + "\n")
    out = si.tail_transcript(p, tail_bytes=8 * 1024)
    assert out["last_user"] == "the last question" and out["last_assistant"] == "the last answer"


def test_cli_json_and_text_run_from_a_lane():
    _seed()
    env = dict(os.environ, EA_DIR=str(EA_DIR), EA_CLAUDE_HOME=str(CLAUDE_HOME))
    r = subprocess.run([sys.executable, str(REPO / "preprocessor" / "session_index.py"),
                        "--json", "--exclude", ME], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert [x["session_id"] for x in rows] == [FABLE, OPUS, THREAD]
    r = subprocess.run([sys.executable, str(REPO / "preprocessor" / "session_index.py"),
                        "--exclude", ME, "--max-chars", "900"], capture_output=True, text=True, env=env)
    assert r.stdout.startswith("CONVERSATION INDEX") and len(r.stdout) <= 920


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
