"""Where the owner's own processes write their logs.

The EA's log directory (`~/.ea/logs`, ea-bot-owned and group-writable) is
shared: ea-bot writes its own logs there, and the owner's cron jobs and
services write theirs. An owner process appending to a path in that shared
directory can be redirected by an ea-bot symlink planted at the log name, so
a log that only the owner writes belongs somewhere ea-bot cannot reach.

`owner_log_dir()` is that place: `~/.ea-private/logs` under the owner's
private home (mode 700, ea-bot cannot traverse it). It is only for logs
written solely by owner processes (the bot's crons, the mediator, sentinel).
A log any ea-bot process writes must stay in the shared `~/.ea/logs`, or the
ea-bot writer would be unable to reach it.

The owner's home comes from `Path.home()`, so this resolves per user and
carries to a fresh install without a hard-coded account.
"""
from __future__ import annotations

from pathlib import Path


def owner_log_dir() -> Path:
    """The owner-private log directory, created if missing (0700)."""
    d = Path.home() / ".ea-private" / "logs"
    try:
        (Path.home() / ".ea-private").mkdir(mode=0o700, exist_ok=True)
        d.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        pass
    return d


def owner_log(name: str) -> Path:
    """Path to a named owner log in the owner-private directory."""
    return owner_log_dir() / name
