#!/usr/bin/env python3
"""preprocessor/claude_cli.py: the one builder for every `claude -p`
command line, its wiring into the lanes, and the doctor's opt-in probe.

The builder keeps runs on the user's subscription login: it adds the
CLI's bare-mode opt-out when the CLI lists one, then the configured
flags. It never adds --bare or an API key, and it gives a command that
already carries --bare (the remote arm) no login flags. Every command it
builds, bare or not, ends in the lane permission mode (dontAsk).
"""
import importlib.util
import json
import re
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import claude_cli  # noqa: E402


@pytest.fixture()
def cli(monkeypatch):
    """claude_cli with a fresh memo, the probe on, and no owner file."""
    monkeypatch.setattr(claude_cli, "_memo", {})
    monkeypatch.setenv("EA_CLAUDE_PROBE", "on")
    monkeypatch.setattr(claude_cli._owner_trust, "is_owner_process", lambda: False)
    monkeypatch.setenv("EA_DIR", tempfile.mkdtemp(prefix="ea-claude-cli-"))
    return claude_cli


def _help(monkeypatch, cli, text):
    calls = []

    def fake(claude_bin, as_ea_bot):
        calls.append((claude_bin, as_ea_bot))
        return text
    monkeypatch.setattr(cli, "_help_text", fake)
    return calls


MODE = ["--permission-mode", "dontAsk"]


def test_a_bare_command_gets_the_mode_and_no_login_flags(cli, monkeypatch):
    calls = _help(monkeypatch, cli, "  --no-bare  keep the login")
    args = ["-p", "x", "--tools", "Bash", "--bare"]
    assert cli.apply(args) == args + MODE
    assert calls == [], "a bare command is not probed"


def test_no_flags_when_the_cli_has_no_opt_out(cli, monkeypatch):
    _help(monkeypatch, cli, "Usage: claude [options]\n  --bare  Minimal mode")
    assert cli.apply(["-p", "hi"]) == ["-p", "hi"] + MODE


def test_the_opt_out_goes_in_front_when_the_cli_lists_it(cli, monkeypatch):
    calls = _help(monkeypatch, cli, "Options:\n  --no-bare   Read the subscription login\n")
    assert cli.apply(["-p", "hi"], claude_bin="/x/claude", as_ea_bot=True) == \
        ["--no-bare", "-p", "hi"] + MODE
    cli.apply(["-p", "again"], claude_bin="/x/claude", as_ea_bot=True)
    assert calls == [("/x/claude", True)], "the answer is kept per binary"


def test_the_probe_switch_turns_the_probe_off(cli, monkeypatch):
    calls = _help(monkeypatch, cli, "  --no-bare")
    monkeypatch.setenv("EA_CLAUDE_PROBE", "off")
    assert cli.login_flags("/x/claude") == []
    assert calls == []


def test_never_bare_even_from_config(cli, monkeypatch):
    _help(monkeypatch, cli, "")
    monkeypatch.setattr(cli, "_is_resident_process", lambda: True)
    cfg = Path(os.environ["EA_DIR"]) / "config"
    cfg.mkdir(parents=True)
    (cfg / "ea-config.json").write_text(json.dumps(
        {"claude": {"login_flags": ["--bare", "--no-bare", "--no-bare", " "]}}))
    assert cli.login_flags() == ["--no-bare"]


def test_the_resident_takes_only_allowlisted_flags_from_ea_config(cli, monkeypatch):
    """ea-config.json is the resident's to write, so it can add only the
    login opt-out, and only to a process running as the resident user."""
    _help(monkeypatch, cli, "")
    cfg = Path(os.environ["EA_DIR"]) / "config"
    cfg.mkdir(parents=True)
    (cfg / "ea-config.json").write_text(json.dumps({"claude": {"login_flags": [
        "--no-bare", "--dangerously-skip-permissions", "--settings", "/tmp/x.json"]}}))
    monkeypatch.setattr(cli, "_is_resident_process", lambda: True)
    assert cli.configured_flags() == ["--no-bare"]
    monkeypatch.setattr(cli, "_is_resident_process", lambda: False)
    assert cli.configured_flags() == []
    assert cli.RESIDENT_ALLOWED_FLAGS == frozenset({"--no-bare"})


def test_the_owner_reads_its_private_file_not_ea_config(cli, monkeypatch):
    _help(monkeypatch, cli, "  --no-bare")
    priv = Path(tempfile.mkdtemp(prefix="ea-claude-priv-"))
    priv.chmod(0o700)
    f = priv / "claude.json"
    f.write_text(json.dumps({"login_flags": ["--from-owner"]}))
    f.chmod(0o600)
    cfg = Path(os.environ["EA_DIR"]) / "config"
    cfg.mkdir(parents=True)
    (cfg / "ea-config.json").write_text(json.dumps({"claude": {"login_flags": ["--from-resident"]}}))
    monkeypatch.setattr(cli._owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    assert cli.login_flags("/x/claude") == ["--no-bare", "--from-owner"]
    cache = json.loads((priv / cli.CACHE_FILE).read_text())
    assert any(v.get("opt_out") is True for v in cache.values())


def test_a_failed_probe_is_not_cached(cli, monkeypatch):
    priv = Path(tempfile.mkdtemp(prefix="ea-claude-priv-"))
    priv.chmod(0o700)
    monkeypatch.setattr(cli._owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    _help(monkeypatch, cli, "")
    assert cli.login_flags("/x/claude") == []
    assert not (priv / cli.CACHE_FILE).exists()


def test_the_cli_prints_nul_separated_flags(cli, monkeypatch, capsys):
    _help(monkeypatch, cli, "  --no-bare")
    cfg = Path(os.environ["EA_DIR"]) / "config"
    cfg.mkdir(parents=True)
    (cfg / "ea-config.json").write_text(json.dumps({"claude": {"login_flags": ["--a b"]}}))
    monkeypatch.setattr(cli, "_is_resident_process", lambda: True)
    assert cli.main(["flags", "--claude-bin", "/x/claude", "--as-ea-bot"]) == 0
    assert capsys.readouterr().out == "--no-bare\0"


def test_discovered_memory_follows_bare_and_the_directory(tmp_path):
    assert claude_cli.discovered_memory([], tmp_path) == frozenset()
    (tmp_path / "CLAUDE.md").write_text("# Core\n")
    assert claude_cli.discovered_memory(["-p", "x"], tmp_path) == {"CLAUDE.md"}
    (tmp_path / "CLAUDE.local.md").write_text("# Install facts\n")
    assert claude_cli.discovered_memory(["-p", "x"], str(tmp_path)) == {"CLAUDE.md", "CLAUDE.local.md"}
    assert claude_cli.discovered_memory(["--tools", "Bash", "--bare"], tmp_path) == frozenset()
    assert claude_cli.discovered_memory([], tmp_path / "missing") == frozenset()


def test_the_cli_prints_nothing_without_flags(cli, monkeypatch, capsys):
    _help(monkeypatch, cli, "")
    assert cli.main(["flags"]) == 0
    assert capsys.readouterr().out == ""


# ── wiring ────────────────────────────────────────────────────────────

def test_run_agent_passes_the_flags_to_every_claude_run():
    src = (ROOT / "run-agent.sh").read_text()
    assert "claude_cli.py\" flags" in src and "--as-ea-bot" in src
    assert "while IFS= read -r -d '' _flag; do" in src
    assert not re.search(r"^\s*(mapfile|readarray)\b", src, re.M)
    assert src.count('--verbose \\"\\${@:4}\\""') == 3
    assert src.count('${CLAUDE_FLAGS[@]+"${CLAUDE_FLAGS[@]}"}') == 3


def test_run_plugin_passes_the_flags():
    src = (ROOT / "run-plugin.sh").read_text()
    assert "claude_cli.py\" flags" in src
    assert not re.search(r"^\s*(mapfile|readarray)\b", src, re.M)
    assert "--verbose \"${@:5}\"'" in src
    assert src.count('${CLAUDE_FLAGS[@]+"${CLAUDE_FLAGS[@]}"}') == 1


def test_the_owner_tools_build_through_the_builder():
    for name in ("mail_privacy.py", "mail_tuning.py", "mail_tagger.py"):
        src = (ROOT / "preprocessor" / name).read_text()
        assert "claude_cli.apply(" in src, name


def test_the_shell_flag_loop_handles_empty_and_spaced_flags():
    """The read loop and the guarded expansion, run for real: no flags
    adds no argument, and a flag with a space stays one argument."""
    script = r'''
set -euo pipefail
emit() { if [ "$1" = yes ]; then printf -- '--no-bare\0--a b\0'; fi; }
for mode in no yes; do
  CLAUDE_FLAGS=()
  while IFS= read -r -d '' _flag; do CLAUDE_FLAGS+=("$_flag"); done < <(emit "$mode" || true)
  bash -c 'echo "$#:${*:4}"' -- m a d ${CLAUDE_FLAGS[@]+"${CLAUDE_FLAGS[@]}"}
done
'''
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == ["3:", "5:--no-bare --a b"]


def _bot_fn(name: str) -> str:
    src = (ROOT / "ea-chat-bot.py").read_text()
    start = src.index(f"\ndef {name}(")
    end = src.index("\ndef ", start + 1)
    return src[start:end]


def test_context_export_asks_the_remote_model_first():
    fn = _bot_fn("_handle_context_export")
    remote = fn.index('purpose="chat-context-export"')
    claude = fn.index("_sudo_claude_cmd(manifest_claude_args)")
    assert remote < claude
    assert "if remote_briefing:" in fn and "_resident_io.replace_text(briefing_path, remote_briefing" in fn
    # It runs in ea-bot's home, where no CLAUDE.md is discovered, so the
    # helper puts the core and the overlay in its prompt.
    assert "claude_md = _build_claude_md(set(), export_flags, EA_BOT_HOME)" in fn
    assert 'manifest_claude_args = ["-p", prompt] + export_flags' in fn
    assert "cwd=EA_BOT_HOME" in fn


def test_the_chat_jobs_have_their_own_breaker(monkeypatch):
    import remote_llm
    import retry_policy
    names = []
    monkeypatch.setattr(retry_policy, "CircuitBreaker",
                        lambda name, **kw: names.append(name) or object())
    for purpose in ("chat-thread-title", "chat-dash-rewrite", "chat-context-export",
                    "mail-extract", "mail-tune-x", "tool-gateway"):
        remote_llm._breaker(purpose)
    assert names == ["remote-llm-chat"] * 3 + ["remote-llm", "remote-llm-tune", "remote-llm-gateway"]


# ── the doctor probe ──────────────────────────────────────────────────

def _doctor():
    spec = importlib.util.spec_from_file_location("ea_doctor_probe", ROOT / "scripts" / "ea-doctor.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.RESULTS.clear()
    return m


def _probe(monkeypatch, stdout, stderr="", rc=0):
    m = _doctor()
    monkeypatch.setattr(m, "_as_ea_bot", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(m.pwd, "getpwnam", lambda n: types.SimpleNamespace(pw_dir="/home/ea-bot"))
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setenv("EA_CLAUDE_PROBE", "off")
    m.check_claude_probe()
    return m.RESULTS[-1], seen


def test_the_probe_passes_on_ok(monkeypatch):
    res, seen = _probe(monkeypatch, json.dumps({"result": "OK", "is_error": False}))
    assert res["status"] == "PASS"
    cmd = seen[0]
    assert cmd[:4] == ["sudo", "-n", "-u", "ea-bot"]
    assert "--no-session-persistence" in cmd and "haiku" in cmd
    assert "--bare" not in cmd


def test_the_probe_names_the_bare_default_on_a_login_failure(monkeypatch):
    res, _ = _probe(monkeypatch, "", "Invalid API key. Please run /login", rc=1)
    assert res["status"] == "FAIL"
    assert "no longer reads the subscription login" in res["detail"]
    assert "login_flags" in res["fix"]


def test_the_probe_reports_other_failures_plainly(monkeypatch):
    res, _ = _probe(monkeypatch, "", "something else broke", rc=2)
    assert res["status"] == "FAIL" and "rc=2" in res["detail"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
