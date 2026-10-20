"""The EA's own mailbox moves, so an archive the EA made is never read as
the user's answer to a ledger row.

graph_client records every message move it makes: an archive, a snooze, a
close's thread archive, the admissions filter, the list digest. The user's
own moves happen in Outlook and never pass through it. ledger_sync reads
this record before it counts a row's mail leaving the inbox as the user's
archive, so a move the EA made defers instead of closing.

The record lives in the owner-private directory when an owner process
writes it (every Graph caller is one under mail mediation), so the
resident can neither forge nor erase it; any other process (a test on a
scratch tree) writes beside the EA tree's state. Best effort throughout: a
failed write never fails the move.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    import owner_trust as _owner_trust
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import owner_trust as _owner_trust

JOURNAL_NAME = "ea-mail-moves.jsonl"
MAX_BYTES = 2_000_000
_INBOX_NAMES = ("inbox",)


def journal_path() -> Path:
    if _owner_trust.is_owner_process():
        return _owner_trust.private_dir() / JOURNAL_NAME
    return Path(os.environ.get("EA_DIR", str(Path.home() / ".ea"))) / "state" / JOURNAL_NAME


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(message_id: str, new_id: str = "", destination: str = "",
           conversation_id: str = "") -> None:
    """Append one move. The id changes on every Outlook move, so both the
    id before and the id after are kept."""
    try:
        path = journal_path()
        line = json.dumps({"ts": _now(), "from_id": message_id or "",
                           "to_id": new_id or "", "dest": destination or "",
                           "conv": conversation_id or ""}) + "\n"
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
        if path.stat().st_size > MAX_BYTES:
            lines = path.read_text().splitlines(keepends=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text("".join(lines[len(lines) // 2:]))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
    except Exception:
        pass


def _records():
    try:
        text = journal_path().read_text()
    except OSError:
        return
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            yield rec


def moved_out_by_ea(ids, since: str = "", after: str = "") -> dict | None:
    """The EA move that last took any of `ids` (an id before or after a
    move) out of the inbox, at or after `since` and strictly after
    `after`, or None. A later EA move back into the inbox (a snooze waking)
    cancels it."""
    want = {i for i in ids if i}
    if not want:
        return None
    hit = None
    for rec in _records():
        if not (rec.get("from_id") in want or rec.get("to_id") in want):
            continue
        want.add(rec.get("to_id") or "")
        ts = rec.get("ts", "")
        if ts < (since or "") or (after and ts <= after):
            continue
        hit = None if (rec.get("dest") or "").lower() in _INBOX_NAMES else rec
    return hit
