#!/usr/bin/env python3
"""The owner-side gates of the suites that spend model quota as the owner.

The nightly full-mode evals (run_full_if_changed.sh) and the weekly
release tests (tests/release/run_weekly.sh) start `claude -p` as the
owner. Whether they run, and which commit the nightly run last measured,
is the owner's to say, so both live in the owner-only private dir, never
in ~/.ea, which the resident writes:

    <private_dir>/evals.json
        {"eval_full_enabled": true, "release_tests_enabled": true,
         "eval_postcommit_enabled": true}
        A key left out, or the file absent, means enabled.
        eval_postcommit_enabled is the post-commit hook's test wall and
        prompt evals; EA_POSTCOMMIT_TESTS in the hook's environment
        (0/false/off or 1/true/on) overrides it.
    <private_dir>/evals/last-full-commit
        The commit id the nightly run last claimed.

Both are read and written through owner_trust (the directory and the
file must be the owner's, not links, writable by no one else). A file
that fails those checks, or does not parse, reads as disabled, except
for the post-commit switch: running the test wall is the safe direction,
so for it such a file reads as enabled.

    eval_gates.py enabled full|release|postcommit   prints True or False
    eval_gates.py last-commit              prints the commit, or nothing
    eval_gates.py set-last-commit <id>
    eval_gates.py clear-last-commit
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "preprocessor"))
import owner_trust  # noqa: E402

SETTINGS_FILE = "evals.json"
STATE_DIR = "evals"
LAST_COMMIT_FILE = "last-full-commit"
KEYS = {"full": "eval_full_enabled", "release": "release_tests_enabled",
        "postcommit": "eval_postcommit_enabled"}
# The suites whose switch reads as on when the file cannot be trusted:
# turning them off is what an attacker would want, not running them.
_ON_WHEN_UNREADABLE = {"postcommit"}
POSTCOMMIT_ENV = "EA_POSTCOMMIT_TESTS"
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")


def settings_path() -> Path:
    return owner_trust.private_dir() / SETTINGS_FILE


def last_commit_path() -> Path:
    return owner_trust.private_dir() / STATE_DIR / LAST_COMMIT_FILE


def enabled(which: str) -> bool:
    """Whether the suite `which` ("full", "release" or "postcommit") may
    run. The post-commit switch takes EA_POSTCOMMIT_TESTS first."""
    key = KEYS[which]
    if which == "postcommit":
        env = os.environ.get(POSTCOMMIT_ENV, "").strip().lower()
        if env in ("0", "false", "off", "no"):
            return False
        if env in ("1", "true", "on", "yes"):
            return True
    bad = which in _ON_WHEN_UNREADABLE
    try:
        raw = owner_trust.read_owner_file(settings_path())
    except FileNotFoundError:
        return True
    except (OSError, owner_trust.OwnerTrustError) as e:
        print(f"eval_gates: {settings_path()} unreadable, {key} reads as {bad}: {e}",
              file=sys.stderr)
        return bad
    try:
        data = json.loads(raw)
    except ValueError as e:
        print(f"eval_gates: {settings_path()} is not JSON, {key} reads as {bad}: {e}",
              file=sys.stderr)
        return bad
    if not isinstance(data, dict):
        return bad
    return data.get(key, True) is not False


def last_commit() -> str:
    """The recorded commit id, or "" when there is none or it is not a
    bare commit id."""
    try:
        raw = owner_trust.read_owner_file(last_commit_path())
    except (OSError, owner_trust.OwnerTrustError):
        return ""
    value = raw.decode("utf-8", "replace").strip()
    return value if _COMMIT.match(value) else ""


def set_last_commit(commit: str) -> None:
    if not _COMMIT.match(commit):
        raise ValueError(f"not a commit id: {commit!r}")
    d = last_commit_path().parent
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner_trust.write_owner_file(last_commit_path(), commit + "\n")


def clear_last_commit() -> None:
    p = last_commit_path()
    try:
        with owner_trust.open_owner_dir(p.parent) as dfd:
            os.unlink(p.name, dir_fd=dfd)
    except FileNotFoundError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    en = sub.add_parser("enabled")
    en.add_argument("which", choices=sorted(KEYS))
    sub.add_parser("last-commit")
    sc = sub.add_parser("set-last-commit")
    sc.add_argument("commit")
    sub.add_parser("clear-last-commit")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "enabled":
            print(enabled(a.which))
        elif a.cmd == "last-commit":
            print(last_commit())
        elif a.cmd == "set-last-commit":
            set_last_commit(a.commit)
        else:
            clear_last_commit()
    except (OSError, ValueError, owner_trust.OwnerTrustError) as e:
        print(f"eval_gates: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
