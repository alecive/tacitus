#!/usr/bin/env python3
"""Owner-side plugin verbs, as the resident calls them (docs/plugins.md).

An owner-zone plugin may declare verbs that need the owner's tokens, such
as a read of a chat service. The resident runs:

    python3 plugin_verbs.py <plugin> <verb> [args...]

or the alias the verb declares on a core tool (`graph_actions.py
get-teams-dms ...` when a plugin owns that verb). Either way the request
goes through the mail mediator as tool `plugin:<plugin>`. The mediator
runs it only when the owner's lock has the plugin enabled, its
capabilities accepted, its files unchanged, and the verb declared SAFE. A
GATED verb never runs from the resident; it runs from a card after a click.

An owner process that calls this (no mediation) runs the verb in place,
under the same lock and integrity checks.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_mediation as M  # noqa: E402
import plugin_registry as R  # noqa: E402


def _fail(msg: str, code: int = 2):
    print(json.dumps({"status": "error", "message": msg}), file=sys.stderr)
    sys.exit(code)


def run(plugin: str, argv: list) -> int:
    """Forward `plugin argv` through the mediator (never returns there), or
    run it as the owner. Returns the exit status otherwise."""
    tool = M.PLUGIN_TOOL_PREFIX + plugin
    M.forward_or_none(tool, list(argv))
    # Not mediated: this is the owner, or a host without a mediator.
    if not R._is_owner():
        _fail(f"plugin verb {plugin} {argv[0] if argv else ''}: plugin verbs run only "
              f"through the mail mediator or as the owner")
    try:
        return R.run_owner_verb(plugin, list(argv))
    except (LookupError, PermissionError) as e:
        _fail(str(e))
    return 2


def forward_alias(tool: str, argv: list) -> None:
    """Call before a core tool parses its arguments. When argv names a verb
    a plugin declares as an alias on `tool`, run the plugin verb and exit;
    otherwise return. A core verb always wins over an alias."""
    if not argv:
        return
    verb = argv[0]
    if verb in M.SAFE.get(tool, set()) or verb in M.GATED.get(tool, set()):
        return
    try:
        target = R.verb_aliases(tool).get(verb)
    except Exception:
        target = None
    if not target:
        return
    plugin, pverb = target
    sys.exit(run(plugin, [pverb] + list(argv[1:])))


def call_alias(tool: str, verb: str, args=(), timeout: int = 180):
    """(returncode, stdout, stderr) of the plugin verb an owner plugin
    declares as the alias `tool verb`, or None when no enabled plugin
    declares it. For code that needs the output, such as the preprocessor
    reading Teams DMs. An owner process runs the verb here, after the lock
    and integrity checks; any other process runs it the way the resident
    does, through this script and the mediator. Raises LookupError or
    PermissionError when the checks refuse it, or subprocess.TimeoutExpired."""
    try:
        target = R.verb_aliases(tool).get(verb)
    except Exception:
        target = None
    if not target:
        return None
    plugin, pverb = target
    argv = [pverb] + [str(a) for a in args]
    if R._is_owner() and not M.should_mediate():
        p = R.owner_verb_process(plugin, argv)
    else:
        p = subprocess.run([sys.executable, str(Path(__file__).resolve()), plugin] + argv,
                           capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or not R.PM.NAME_RE.match(argv[0]) or not R.PM.VERB_RE.match(argv[1]):
        _fail("usage: plugin_verbs.py <plugin> <verb> [args...]")
    return run(argv[0], argv[1:])


if __name__ == "__main__":
    sys.exit(main())
