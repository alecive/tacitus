#!/usr/bin/env python3
"""The chat and cron lanes grant Write/Edit only on the paths the EA writes.

A bare `Write` or `Edit` grant reaches every file ea-bot owns, including its
Claude settings (hooks), its CLAUDE.md and memory, the config and the plugin
scripts the crons run. preprocessor/resident_write_paths.py is the one list;
these tests pin that both lanes use it, that it covers the paths the guides
tell the EA to write, and that it leaves out the files that change later
behaviour.

Usage:  python3 tests/test_resident_write_paths.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import resident_write_paths as RW  # noqa: E402

HOME = "/home/ea-bot"
EA = "/home/ea-bot/.ea"


def _covered(path: str) -> bool:
    """Whether one of the rules covers `path` (the gitignore-style subset
    the module uses: `*` is one segment, a trailing `/**` everything below)."""
    for pat in RW.paths(HOME, EA):
        if pat.endswith("/**"):
            rx = re.escape(pat[:-3]).replace(r"\*", "[^/]+") + "/.+"
        else:
            rx = re.escape(pat).replace(r"\*", "[^/]+")
        if re.fullmatch(rx, path):
            return True
    return False


def test_rules_are_absolute_edit_rules():
    rules = RW.grants(HOME, EA)
    assert rules
    for r in rules:
        assert r.startswith("Edit(//"), r


def test_relative_paths_refused():
    try:
        RW.grants("home/ea-bot", EA)
    except ValueError:
        return
    raise AssertionError("a relative home was accepted")


def test_workflow_paths_covered():
    for p in [
        f"{EA}/state/reply-body.txt",
        f"{EA}/state/pending-event.json",
        f"{EA}/state/reports/x.md",
        f"{EA}/docs/sabbatical/notes.md",
        f"{EA}/docs/proj/overleaf/main.tex",
        f"{EA}/letters/smith/smith.tex",
        f"{EA}/research/topic/notes.md",
        f"{EA}/receipts/trip/trip.json",
        f"{EA}/exports/topic-2026/context-export.md",
        f"{EA}/plugins/demo/state/state.json",
        f"{EA}/config/autonomy-tiers.json",
        f"{HOME}/presentations/decks/talk/slides.md",
        f"{HOME}/presentations/sites/csci5942/index.md",
        f"{HOME}/doc-review/drafts/x.md",
        f"{HOME}/proposals/ttpe/main.tex",
        f"{HOME}/letters-of-recommendation/x/main.tex",
        "/tmp/x.pdf",
    ]:
        assert _covered(p), p


def test_behaviour_stores_not_covered():
    for p in [
        f"{HOME}/.claude/settings.json",
        f"{HOME}/.claude/CLAUDE.md",
        f"{HOME}/CLAUDE.md",
        f"{HOME}/.claude/projects/-home-ea-bot-ea-system/memory/MEMORY.md",
        f"{HOME}/.bashrc",
        f"{EA}/CLAUDE.md",
        f"{EA}/.env",
        f"{EA}/config/ea-config.json",
        f"{EA}/config/user-profile.json",
        f"{EA}/config/vip_senders.txt",
        f"{EA}/config/preferences.json",
        f"{EA}/plugins/demo/chat_skill.md",
        f"{EA}/plugins/demo/collect.py",
        f"{EA}/plugins/demo/plugin.json",
        f"{HOME}/ea-system/preprocessor/ledger.py",
    ]:
        assert not _covered(p), p


def test_cli_prints_the_same_rules():
    r = subprocess.run([sys.executable, str(REPO / "preprocessor" / "resident_write_paths.py"),
                        "--home", HOME, "--ea-dir", EA], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == RW.grants_csv(HOME, EA)


def _grant_tokens(text: str) -> set[str]:
    return {t.strip() for t in re.split(r'[,"]', text) if t.strip()}


def test_bot_grants_no_bare_write_or_edit():
    src = (REPO / "ea-chat-bot.py").read_text()
    m = re.search(r"_BASH_ONLY_TOOLS = \((.*?)\n\)", src, re.S)
    assert m
    toks = _grant_tokens(m.group(1))
    assert "Write" not in toks and "Edit" not in toks
    assert "_resident_write_grants()" in src
    assert '"Read,Write,Edit"' not in src


def test_run_agent_grants_no_bare_write_or_edit():
    src = (REPO / "run-agent.sh").read_text()
    lines = [ln for ln in src.splitlines()
             if "ALLOWED_TOOLS" in ln and not ln.lstrip().startswith("#")]
    for ln in lines:
        assert not re.search(r"[,\"]\s*(Write|Edit)\s*[,\"]", ln), ln
    assert "resident_write_paths.py" in src


def test_deny_list_names_the_session_stores():
    """The shared deny list holds in the resident pane, where the allow
    list is moot: the file tools never reach the Claude settings, the
    session-wide instructions or a login hook."""
    lines = {ln.strip() for ln in (REPO / "scripts" / "disallowed-tools.txt").read_text().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")}
    for rule in ["Edit(~/.claude/settings.json)", "Edit(~/.claude/settings.local.json)",
                 "Edit(~/.claude/CLAUDE.md)", "Edit(~/.claude/skills/**)",
                 "Edit(~/.claude/agents/**)", "Edit(~/.claude/plugins/**)",
                 "Edit(~/CLAUDE.md)", "Edit(~/.ea/CLAUDE.md)", "Edit(~/.config/systemd/**)",
                 "Edit(~/.bashrc)"]:
        assert rule in lines, rule
    # No deny may cover a path the lanes are meant to write.
    for rule in lines:
        if rule.startswith("Edit("):
            target = rule[5:-1].replace("~", HOME, 1)
            for p in RW.paths(HOME, EA):
                assert not p.startswith(target.rstrip("*").rstrip("/") + "/"), (rule, p)


def test_git_files_inside_the_write_paths_are_denied():
    """A clone under a write path keeps its .git and its .gitattributes
    files out of the file tools' reach, in every lane."""
    lines = {ln.strip() for ln in (REPO / "scripts" / "disallowed-tools.txt").read_text().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")}
    for rule in RW.DENIED_RULES:
        assert rule in lines, rule
    assert "Edit(//**/.git/**)" in RW.DENIED_RULES
    assert "Edit(//**/.gitattributes)" in RW.DENIED_RULES
    # The clones those rules protect are inside the write paths.
    assert _covered(f"{HOME}/presentations/sites/csci5942/.git/config")
    assert _covered(f"{EA}/docs/proj/overleaf/.gitattributes")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    print(f"{fails} failed")
    sys.exit(1 if fails else 0)
