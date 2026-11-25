"""A card a scheduled run stages goes into the thread of the post that run
makes, not to the channel's top level: chat_notify records the post, the
bot's event sweeper waits while a run's marker stands and then threads the
run's cards under that post (2026-11-25: the morning briefing's trash cards
each came as their own conversation in the web chat)."""
import ast
import json
import os
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))

import chat_notify  # noqa: E402


def _bot_fns(private: Path) -> dict:
    src = (ROOT / "ea-chat-bot.py").read_text()
    names = {"_scheduled_run_active", "_scheduled_post_for"}
    body = [n for n in ast.parse(src).body
            if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "SCHEDULED_RUN_MAX_S"
                                                  for t in n.targets))]
    ns = {"json": json, "time": time, "CHANNEL_ID": "C_MAIN",
          "_chat": types.SimpleNamespace(private_dir=lambda: private)}
    exec(compile(ast.Module(body=body, type_ignores=[]), "ea-chat-bot.py", "exec"), ns)
    return ns


def test_the_post_is_recorded_for_the_sweeper(tmp_path, monkeypatch):
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path))
    chat_notify._note_ea_post(types.SimpleNamespace(channel="C_MAIN", id="111.222"))
    rec = json.loads((tmp_path / chat_notify.LAST_EA_POST).read_text())
    assert (rec["channel"], rec["ts"]) == ("C_MAIN", "111.222") and abs(rec["at"] - time.time()) < 5


def test_a_card_staged_before_the_post_goes_into_its_thread(tmp_path):
    fns = _bot_fns(tmp_path)
    now = time.time()
    (tmp_path / "last-ea-post.json").write_text(json.dumps({"channel": "C_MAIN", "ts": "111.222", "at": now}))
    assert fns["_scheduled_post_for"](now - 300) == {"channel": "C_MAIN", "thread_ts": "111.222"}
    assert fns["_scheduled_post_for"](now + 60) is None        # staged after the post: not that run's
    assert fns["_scheduled_post_for"](now - 7200) is None      # far older than the post
    assert fns["_scheduled_post_for"](0) is None


def test_a_running_lane_holds_the_sweep_and_a_stale_marker_does_not(tmp_path):
    fns = _bot_fns(tmp_path)
    assert fns["_scheduled_run_active"]() is False
    mark = tmp_path / "scheduled-run.morning-briefing.123"
    mark.write_text("")
    assert fns["_scheduled_run_active"]() is True
    old = time.time() - 2 * fns["SCHEDULED_RUN_MAX_S"]
    os.utime(mark, (old, old))
    assert fns["_scheduled_run_active"]() is False


def test_run_agent_leaves_its_marker_and_removes_it_on_exit():
    src = (ROOT / "run-agent.sh").read_text()
    assert 'RUN_MARK="${EA_PRIVATE_DIR:-$HOME/.ea-private}/scheduled-run.${AGENT}.$$"' in src
    assert "trap 'rm -f \"$RUN_MARK\"' EXIT" in src
    bot = (ROOT / "ea-chat-bot.py").read_text()
    assert "if _scheduled_run_active():" in bot and "_scheduled_post_for(" in bot
