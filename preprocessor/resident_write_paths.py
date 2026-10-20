#!/usr/bin/env python3
"""The places the EA's file tools (Write, Edit) may change, as permission rules.

A bare `Write` or `Edit` grant lets the model change any file ea-bot owns,
and ea-bot owns files that steer later runs: its Claude settings (hooks,
permissions), its CLAUDE.md and auto-memory, the config, and the agent-zone
plugin scripts the crons run. The chat lane (ea-chat-bot.py) and the cron
lane (run-agent.sh) grant the file tools only on the paths below, the ones
CLAUDE.md, docs/ea/*.md and the skills tell the EA to write:

  <ea>/state/**        reply bodies, writer briefs, staged cards and events
                       (pending-*.json), reports, scratch copies
  <ea>/docs/**         project notes, drafts for review, Overleaf clones
  <ea>/letters/**      letters and their PDFs (compose-letter)
  <ea>/research/**     the shared research folders
  <ea>/receipts/**     receipts and trip.json (reimbursement)
  <ea>/exports/**      context exports, poll screenshots
  <ea>/plugins/*/state/**   a plugin's own state, never its code
  <ea>/config/autonomy-tiers.json   a tier promotion (CLAUDE.md rule 12)
  <home>/presentations/**   decks and the public site clones
  <home>/doc-review/**      the doc-review clone
  <home>/proposals/**, <home>/letters-of-recommendation/**   Overleaf clones
  /tmp/**              scratch files and attachments the mediator accepts

Every other config file has its own verb (preferences.py, onboarding.py,
project_store.py), so the file tools never need it. Claude Code matches only
`Edit(path)` rules for its file permission checks, and an Edit rule covers
Write too; `//` starts an absolute path.

Inside those paths, DENIED_RULES keep the file tools off every `.git`
directory and every `.gitattributes` file: a clone's own config, hooks and
attributes can name programs git runs or change what a diff shows, and the
git tools (safe_git.py) refuse or ignore them. The rules sit in the shared
deny list (scripts/disallowed-tools.txt), so they hold in every lane, the
resident pane included.

The resident pane runs with --dangerously-skip-permissions, where these
rules do not apply; the deny list and the persistence digest
(scripts/persistence-digest.py) cover it.

CLI (run-agent.sh):  resident_write_paths.py --home <ea-bot home> --ea-dir <EA dir>
prints the rules comma separated.
"""
from __future__ import annotations

import argparse
import sys

# (base, relative path, recursive). base "ea" is the EA tree, "home" ea-bot's
# home, "root" the filesystem root.
WRITE_PATHS: tuple[tuple[str, str, bool], ...] = (
    ("ea", "state", True),
    ("ea", "docs", True),
    ("ea", "letters", True),
    ("ea", "research", True),
    ("ea", "receipts", True),
    ("ea", "exports", True),
    ("ea", "plugins/*/state", True),
    ("ea", "config/autonomy-tiers.json", False),
    ("home", "presentations", True),
    ("home", "doc-review", True),
    ("home", "proposals", True),
    ("home", "letters-of-recommendation", True),
    ("root", "tmp", True),
)


# Edit (and so Write) denials inside the write paths, in every lane.
DENIED_RULES: tuple[str, ...] = (
    "Edit(//**/.git/**)",
    "Edit(//**/.git)",
    "Edit(//**/.gitattributes)",
)

# Edit (and so Write) denials on the files a later Claude session loads,
# beyond ~/.claude's settings, skills, agents, commands and plugins: the
# user config, auto-memory, rules, output styles, a project's settings or
# MCP file anywhere, the memory files in the docs and research folders
# (inside the write paths) and the lane directories
# (slack_sessions.LANE_ROOT), whose sessions load a CLAUDE.md found there
# or above. They sit in the shared deny list, so they hold in the bypass
# lanes too, where the deny list is the only tool layer.
STEERING_RULES: tuple[str, ...] = (
    "Edit(~/.claude.json)",
    "Edit(~/.claude/projects/*/memory/**)",
    "Edit(~/.claude/rules/**)",
    "Edit(~/.claude/output-styles/**)",
    "Edit(//**/.claude/settings*.json)",
    "Edit(//**/.mcp.json)",
    "Edit(~/CLAUDE.local.md)",
    "Edit(~/.ea/CLAUDE.local.md)",
    "Edit(~/.ea/docs/CLAUDE.md)",
    "Edit(~/.ea/docs/CLAUDE.local.md)",
    "Edit(~/.ea/docs/**/CLAUDE.md)",
    "Edit(~/.ea/docs/**/CLAUDE.local.md)",
    "Edit(~/.ea/research/CLAUDE.md)",
    "Edit(~/.ea/research/CLAUDE.local.md)",
    "Edit(~/.ea/research/**/CLAUDE.md)",
    "Edit(~/.ea/research/**/CLAUDE.local.md)",
    "Edit(~/.local/CLAUDE.md)",
    "Edit(~/.local/state/CLAUDE.md)",
    "Edit(~/.local/state/ea-lanes/**)",
)


def paths(home: str, ea_dir: str) -> list[str]:
    """The absolute path patterns, `/**` on a directory."""
    bases = {"ea": ea_dir.rstrip("/"), "home": home.rstrip("/"), "root": ""}
    out = []
    for base, rel, recursive in WRITE_PATHS:
        p = f"{bases[base]}/{rel}"
        out.append(p + "/**" if recursive else p)
    return out


def grants(home: str, ea_dir: str) -> list[str]:
    """One Edit rule per path; an absolute path is written with `//`."""
    if not home.startswith("/") or not ea_dir.startswith("/"):
        raise ValueError("home and ea_dir must be absolute paths")
    return [f"Edit(/{p})" for p in paths(home, ea_dir)]


def grants_csv(home: str, ea_dir: str) -> str:
    return ",".join(grants(home, ea_dir))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the EA's Write/Edit permission rules",
                                 allow_abbrev=False)
    ap.add_argument("--home", required=True, help="ea-bot's home directory")
    ap.add_argument("--ea-dir", required=True, help="the EA tree (resolved)")
    a = ap.parse_args(argv)
    try:
        print(grants_csv(a.home, a.ea_dir))
    except ValueError as e:
        print(f"resident_write_paths: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
