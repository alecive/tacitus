"""Draft-events journal: structured action reporting for draft mutations.

graph_actions.py appends one JSONL record per draft mutation to
~/.ea/state/draft-events.jsonl, and the bot reads the records that
appeared during a Claude run:

    {"ts": "...", "action": "created|updated|deleted", "draft_id": "AAMk...",
     "verb": "create-reply", "pid": 12345, "run_id": "...",
     "system": "gmail", "email": "user@gmail.com",      # Gmail records only
     "ledger_ids": ["row-id"]}                          # when the draft names rows

The bot snapshots the journal offset before invoking Claude and reads
from that offset afterward. Outlook records come from graph_actions.py,
Gmail records from gmail_client.py (create/update/delete-draft). A Gmail
draft made through the claude.ai MCP never passes through either CLI, so
it is not journalled; the bot captures its id from the MCP tool result
as it streams (see gmail_ids_in) and lists the account's new drafts as a
last resort.

Run attribution
---------------
A byte offset alone is not an owner: the journal is global, so two
concurrent dispatches each read "everything after my offset" and the
earlier one would claim the later one's draft, putting a live Send button
in the wrong Slack thread. Two mechanisms prevent that, in this order:

  * `run_id` — `record()` stamps `$EA_RUN_ID`, which the bot sets on the
    Claude subprocess it spawns. A record carrying another run's id is
    never ours, wherever it sits in the journal.
  * the claim cursor — under mail mediation the record is written by the
    owner-side mediator's child process, whose env carries no EA_RUN_ID, so
    production records are unlabelled. An unlabelled record is genuinely
    ambiguous; `claim_since()` hands it to the FIRST run that finishes
    after it was written and advances a shared cursor, so it reaches
    exactly one thread instead of every overlapping dispatch.

The journal is append-only and self-pruning (rewritten when it exceeds
MAX_BYTES, keeping the newest half). Concurrent appends are safe at the
OS level (single short line, O_APPEND).

The journal, its cursor and its lock file live in the resident's space and
both users write them, so every access goes through resident_io: an owner
process (the bot, the mediator's child) appends, rewrites and reads them
with the resident's access, and a link at one of those names reaches only
what the resident could reach already.
"""
from __future__ import annotations

import json
import os
import re
try:
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import resident_io as _rio
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:                      # pragma: no cover - non-POSIX
    fcntl = None

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
JOURNAL = EA_DIR / "state" / "draft-events.jsonl"
MAX_BYTES = 256 * 1024

# Env var the bot stamps on a Claude subprocess so its draft mutations are
# attributable to that dispatch. Absent = unlabelled (see module docstring).
RUN_ID_ENV = "EA_RUN_ID"


def current_run_id() -> str:
    """This process's run identity, or "" when it was not stamped."""
    return os.environ.get(RUN_ID_ENV, "") or ""


# A Gmail draft id: "r" plus a long run of digits (the API's draft ids).
GMAIL_DRAFT_ID_RE = re.compile(r"^r-?\d{6,}$")
# The same id as it appears in tool output: the MCP's {"id": "r..."}, the
# older MCP's {"draftId": "..."}, and gmail_client.py's {"draft_id": "r..."}.
_GMAIL_ID_IN_TEXT_RE = re.compile(r'"(?:draftId|draft_id|id)":\s*"(r-?\d{6,})"')


def record(action: str, draft_id: str, verb: str = "", *,
           system: str = "", email: str = "", ledger_ids=()) -> None:
    """Append one event. Best-effort: never raises (a journal failure
    must not break the draft operation that just succeeded).

    `system` names the mail system when it is not Outlook ("gmail"), and
    `email` the account, so the bot can render and later send the draft
    through the right client. `ledger_ids` are the obligation rows the
    draft delivers (`--ledger-id`); sending it closes exactly those."""
    try:
        ev = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "action": action,
            "draft_id": draft_id,
            "verb": verb,
            "pid": os.getpid(),
            "run_id": current_run_id(),
        }
        if system:
            ev["system"] = system
        if email:
            ev["email"] = email
        ids = parse_ledger_ids(ledger_ids)
        if ids:
            ev["ledger_ids"] = ids
        line = json.dumps(ev)
        if not _rio.append_text(JOURNAL, line + "\n"):
            return
        if JOURNAL.stat().st_size > MAX_BYTES:
            _prune()
    except Exception:
        pass


def _newest_half(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(lines[len(lines) // 2:]) + "\n"


def _prune() -> None:
    try:
        # In place under the file's lock, so the inode (and any reader's
        # open descriptor) stays the same.
        if not _rio.update_text(JOURNAL, _newest_half):
            return
        # Offsets just shifted, so a cursor into the old layout is
        # meaningless. Re-point it at EOF rather than 0: everything left in
        # the file is history, and re-opening it for claiming would post a
        # live Send button for every stale draft in the surviving half.
        with _CursorLock():
            _write_cursor(offset())
    except Exception:
        pass


def is_gmail(ev: dict) -> bool:
    """True for a Gmail draft record: stamped `system: gmail`, or an id in
    the Gmail shape (a record written before the stamp existed)."""
    if (ev.get("system") or "") == "gmail":
        return True
    return bool(GMAIL_DRAFT_ID_RE.match(ev.get("draft_id") or ""))


def gmail_account(ev: dict) -> str:
    return ev.get("email") or ""


def system_of(events, draft_id: str) -> tuple[str, str]:
    """("gmail", account) or ("outlook", "") for a draft id, from the
    records that mention it."""
    for ev in events or []:
        if (ev.get("draft_id") or "") == draft_id and is_gmail(ev):
            return "gmail", gmail_account(ev)
    if GMAIL_DRAFT_ID_RE.match(draft_id or ""):
        return "gmail", ""
    return "outlook", ""


# A ledger row id as ledger.py writes them: slugs, detector ids with a
# conversation-id tail ("response-<12 base64 chars>"), dated suffixes.
_LEDGER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:=+-]{0,199}$")


def parse_ledger_ids(values) -> list[str]:
    """The ledger ids in `--ledger-id` values (repeatable, each one may be
    comma-separated), deduplicated in order; anything not shaped like a
    row id is dropped."""
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for v in values or ():
        for part in str(v).split(","):
            part = part.strip()
            if part and _LEDGER_ID_RE.match(part) and part not in out:
                out.append(part)
    return out


def ledger_ids_of(draft_id: str) -> list[str]:
    """Every ledger row the draft's create and update records name, in
    order. The journal keeps weeks of records; a Send card lives 48 h."""
    out: list[str] = []
    if not draft_id:
        return out
    for _, ev in read_records(0):
        if (ev.get("draft_id") or "") != draft_id:
            continue
        for lid in parse_ledger_ids(ev.get("ledger_ids") or ()):
            if lid not in out:
                out.append(lid)
    return out


def gmail_ids_in(text: str) -> list[str]:
    """Every Gmail draft id quoted in tool output or reply text, in order,
    deduplicated. Outlook ids (AAMk...) never match."""
    out: list[str] = []
    for m in _GMAIL_ID_IN_TEXT_RE.finditer(text or ""):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def offset() -> int:
    """Current journal size — snapshot BEFORE invoking Claude."""
    try:
        return JOURNAL.stat().st_size
    except OSError:
        return 0


def _plain_id(value) -> bool:
    try:
        import mail_mediation as _mm
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import mail_mediation as _mm
    return _mm.is_plain_id(value)


def read_records(start: int) -> list[tuple[int, dict]]:
    """(byte offset, event) for every record appended after `start`.

    The offset is the record's own start position, which is what the
    claim cursor compares against. The journal is resident state, so a
    record whose draft_id is not a plain mail id
    (mail_mediation.is_plain_id) is dropped here: no reader (the bot's Send
    card, its draft fetch, the ledger link) ever acts on one."""
    # Bytes, so the offsets handed back are exact byte positions.
    data = _rio.read_bytes(JOURNAL)
    if data is None:
        return []
    if len(data) < start:
        start = 0
    records: list[tuple[int, dict]] = []
    pos = start
    for raw in data[start:].splitlines(keepends=True):
        here = pos
        pos += len(raw)
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        if "draft_id" in ev and not _plain_id(ev.get("draft_id")):
            continue
        records.append((here, ev))
    return records


def known_draft_ids(actions: tuple = ("created", "updated")) -> set:
    """Every draft id this system recorded a mutation for.

    Provenance, cheaply: the journal holds only drafts graph_actions itself
    created or updated, so an id absent from it (and from draft_capture)
    was written by a human in Outlook. create-reply's dedupe uses this to
    refuse to overwrite the user's own hand-written draft.

    Partial by construction -- the journal self-prunes at MAX_BYTES -- so a
    miss means "cannot vouch for it", which is the fail-safe direction:
    the caller writes a new draft instead of overwriting one.
    """
    out = set()
    for _, e in read_records(0):
        if e.get("action") in actions and e.get("draft_id"):
            out.add(e["draft_id"])
    return out


def read_since(start: int) -> list[dict]:
    """Events appended after byte offset `start`.

    If the file shrank below `start` (pruned mid-run), reads from 0:
    over-reporting is handled by the caller's created-deleted set math
    and is preferable to silently missing a created draft."""
    return [ev for _, ev in read_records(start)]


# Claim cursor: at-most-once delivery of unlabelled records.

def _cursor_path() -> Path:
    return JOURNAL.with_name(JOURNAL.name + ".cursor")


def _lock_path() -> Path:
    return JOURNAL.with_name(JOURNAL.name + ".lock")


def _read_cursor() -> int:
    try:
        return int(json.loads(_rio.read_text(_cursor_path()) or "{}").get("offset", 0))
    except Exception:
        return 0


def _write_cursor(value: int) -> None:
    try:
        _rio.replace_text(_cursor_path(), json.dumps({"offset": int(value)}), 0o664)
    except Exception:
        pass


def _open_lock(path: Path) -> int:
    """A read-only descriptor on the lock file, never through a link at its
    name. flock needs no write access, so both users lock it the same way;
    a missing file is made with the resident's access first."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(path, flags)
    except FileNotFoundError:
        if not _rio.ensure_file(path):
            raise
        return os.open(path, flags)


class _CursorLock:
    """flock around the read-modify-write of the claim cursor. Degrades to
    a no-op where fcntl is unavailable or the lock cannot be opened; the
    cursor is an optimisation, not a correctness gate for the labelled
    path."""

    def __enter__(self):
        self._fd = None
        if fcntl is None:
            return self
        try:
            self._fd = _open_lock(_lock_path())
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except Exception:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
            self._fd = None
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
        return False


def claim_since(start: int, run_id: str = "", *,
                accept_unlabelled: bool = True) -> list[dict]:
    """Events appended after `start` that THIS run owns.

    Ownership, in order:
      1. a record stamped with `run_id` is ours wherever it sits;
      2. an unlabelled record is ours only if no earlier caller already
         claimed it — the shared cursor moves past everything we look at,
         so two overlapping dispatches split the unlabelled records rather
         than both claiming all of them;
      3. a record stamped with a different run id is never ours.

    `run_id=""` still claims unlabelled records but yields nothing
    labelled. A run whose own records always carry its id (a per-thread
    `claude -p` run: the mediator forwards EA_RUN_ID) passes
    `accept_unlabelled=False`: it takes only its own records and leaves
    the cursor where it is, so an unlabelled draft (the resident's, a
    shell's) is never posted in that run's thread.
    """
    if not accept_unlabelled:
        if not run_id:
            return []
        return [ev for _pos, ev in read_records(start)
                if (ev.get("run_id") or "").strip() == run_id]
    with _CursorLock():
        size = offset()
        claimed = _read_cursor()
        if claimed > size:
            # The journal was pruned or reset under us and the cursor now
            # points past EOF. Treat what survives as history (fail closed:
            # a missing button is recoverable, a flood of Send buttons for
            # long-settled drafts is not).
            claimed = size
        mine: list[dict] = []
        for pos, ev in read_records(start):
            ev_run = (ev.get("run_id") or "").strip()
            if ev_run:
                if run_id and ev_run == run_id:
                    mine.append(ev)
                continue
            if pos >= claimed:
                mine.append(ev)
        if size > claimed:
            _write_cursor(size)
    return mine


def filter_events(events: list[dict], run_id: str | None = None,
                  accept_unlabeled: bool = True) -> list[dict]:
    """Drop records belonging to another run.

    run_id=None disables filtering entirely."""
    if run_id is None:
        return list(events)
    out = []
    for ev in events:
        ev_run = (ev.get("run_id") or "").strip()
        if not ev_run:
            if accept_unlabeled:
                out.append(ev)
            continue
        if ev_run == run_id:
            out.append(ev)
    return out


def effective_drafts(events: list[dict], run_id: str | None = None,
                     accept_unlabeled: bool = True) -> dict:
    """Net result of a run's events.

    created: draft ids created and NOT subsequently deleted (these get
             :lock: Send confirmations)
    updated: ids updated but not created/deleted this run (the bot checks
             whether that draft still has a live confirmation block and
             reposts one if the buttons are gone)
    deleted: ids deleted this run

    `run_id` filters to one dispatch's records; see filter_events.
    """
    created: set = set()
    updated: set = set()
    deleted: set = set()
    for ev in filter_events(events, run_id, accept_unlabeled):
        did = ev.get("draft_id") or ""
        if not did:
            continue
        action = ev.get("action")
        if action == "created":
            created.add(did)
            deleted.discard(did)
        elif action == "deleted":
            deleted.add(did)
            created.discard(did)
        elif action == "updated":
            updated.add(did)
    return {
        "created": created,
        "updated": updated - created - deleted,
        "deleted": deleted,
    }
