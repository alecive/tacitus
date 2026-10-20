"""Owner-only logs resolve to the owner-private dir, out of ea-bot's reach."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import log_paths  # noqa: E402


def test_owner_log_dir_is_under_the_private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    d = log_paths.owner_log_dir()
    assert d == tmp_path / ".ea-private" / "logs" and d.is_dir()
    # created 0700 so ea-bot cannot traverse it
    assert (d.stat().st_mode & 0o077) == 0
    assert log_paths.owner_log("x.log") == d / "x.log"


def test_owner_writers_use_the_private_dir():
    # the three owner-only writers must resolve their logs through owner_log,
    # not the shared EA_DIR/logs an ea-bot symlink could redirect
    for mod, name in (("sentinel.py", "ledger-sync.log"),
                      ("sentinel.py", "sentinel.log"),
                      ("mail_mediator.py", "mail-mediator.log")):
        src = (ROOT / "preprocessor" / mod).read_text()
        assert f'owner_log("{name}")' in src, (mod, name)
    # phase 2: sentinel scans the owner dir (run-agent writes the per-run
    # agent logs there now), and the wrappers + cost logger write there too
    sen = (ROOT / "preprocessor" / "sentinel.py").read_text()
    assert "LOG_DIR = owner_log_dir()" in sen
    ra = (ROOT / "run-agent.sh").read_text()
    assert 'LOG_DIR="$HOME/.ea-private/logs"' in ra
    rp = (ROOT / "run-plugin.sh").read_text()
    assert 'LOG_DIR="$HOME/.ea-private/logs/plugins/$PLUGIN_NAME"' in rp
    cost = (ROOT / "preprocessor" / "log_claude_cost.py").read_text()
    assert 'owner_log("claude-cost.log")' in cost
    # the ea-bot child's log stays in the shared dir (ea-bot must reach it)
    mc = (ROOT / "preprocessor" / "log_missed_closure.py").read_text()
    assert "owner_log" not in mc

    # the bot's own logs (slack-bot.log, reply-check.jsonl, scheduler dumps)
    bot = (ROOT / "ea-chat-bot.py").read_text()
    assert "LOG_DIR = _owner_log_dir()" in bot
    assert '_REPLY_CHECK_LOG = _owner_log("reply-check.jsonl")' in bot
    assert 'dump_dir = _owner_log_dir() / "scheduler"' in bot
    rr = (ROOT / "preprocessor" / "reply_check_report.py").read_text()
    assert '_owner_log("slack-bot.log")' in rr and '_owner_log("reply-check.jsonl")' in rr



if __name__ == "__main__":
    test_owner_writers_use_the_private_dir()
    print("OVERALL: [PASS]")
