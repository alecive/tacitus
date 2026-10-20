#!/usr/bin/env python3
"""id_handles: short handles for the long Outlook ids within one run.

A Graph message id is 150 characters of base64; a model copying one
into its next call drops or doubles a character, the call fails and
the turn is lost. Within a run (EA_RUN_ID) every id a read verb prints
gets a short handle beside it (`"ref": "m3"` by the message id,
`"conversation_ref": "c2"` by the conversation id, `"ref": "e4"` by an
event id), kept in a table under the resident's state, and every verb
that takes an id accepts the handle in its place. The ea-bot shim does
both (`resolve_argv` before the request is forwarded, `annotate_output`
on the answer), so the mediator and the owner's tools never see a
handle. Without EA_RUN_ID nothing changes.

The table is resident state, so a handle stands for whatever ea-bot wrote
there. Resolution therefore happens only in the resident's own process: one
whose effective user is the resident user (RESIDENT_USER), as the ea-bot
shim runs, before the mediator checks the request. Every other process
resolves nothing, whatever its environment says: an owner process that runs
a tool directly with a run id (the remote briefing, the bot's own verbs, a
plugin child), and a child the mediator starts (EA_MEDIATED_VERB set). A
handle resolves only to a plain id.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

DIR = "id-handles"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_HANDLE = re.compile(r"^(m|c|e)(\d{1,4})$")
_GRAPH_ID = re.compile(r"^[A-Za-z0-9_\-+/=]{40,}$")
# What a handle may resolve to: the characters the mediator accepts in an id
# (mail_mediation._ID_RE), so a resolved value can never add a path segment.
_PLAIN_ID = re.compile(r"[A-Za-z0-9_=+-]{1,512}")
MEDIATED_VERB_ENV = "EA_MEDIATED_VERB"
RESIDENT_USER = "ea-bot"
ID_FLAGS = ("--message-id", "--conversation-id", "--event-id", "--ignore-event", "--draft-id",
            "--series-id", "--attachment-id")
KINDS = {"id": "m", "message_id": "m", "conversation_id": "c", "event_id": "e", "series_master_id": "e",
         "draft_id": "m"}


def _ea_dir() -> Path:
    return Path(os.environ.get("EA_DIR", Path.home() / ".ea"))


def _is_resident() -> bool:
    """True only when this process's effective user is the resident user.
    An unknown answer (no such user, no pwd module) is False."""
    try:
        import pwd
        return os.geteuid() == pwd.getpwnam(RESIDENT_USER).pw_uid
    except Exception:
        return False


def run_id() -> str:
    return os.environ.get("EA_RUN_ID", "").strip()


def table_path(rid: str | None = None) -> Path | None:
    rid = rid if rid is not None else run_id()
    if not rid:
        return None
    return _ea_dir() / "state" / DIR / (_SAFE.sub("_", rid)[:80] + ".json")


def _rio():
    try:
        import resident_io
    except ModuleNotFoundError:
        from preprocessor import resident_io
    return resident_io


def load(rid: str | None = None) -> dict:
    p = table_path(rid)
    if p is None:
        return {}
    try:
        data = json.loads(_rio().read_text(p) or "")
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(table: dict, rid: str | None = None) -> None:
    """The table is resident state, written with the resident's access
    (resident_io) whichever user runs the tool. Tables are small and
    few; the state directory's own housekeeping clears old ones."""
    p = table_path(rid)
    if p is None:
        return
    try:
        rio = _rio()
        rio.make_dir(p.parent)
        rio.replace_text(p, json.dumps(table), mode=0o664)
    except Exception:
        pass


def handle_for(table: dict, kind: str, value: str) -> str:
    """The handle of `value` in `table` (made when new): {"m3": id, ...}
    plus a reverse index under "_by_id"."""
    by_id = table.setdefault("_by_id", {})
    key = kind + ":" + value
    if key in by_id:
        return by_id[key]
    n = 1 + sum(1 for k in table if k.startswith(kind) and k[1:].isdigit())
    h = f"{kind}{n}"
    table[h] = value
    by_id[key] = h
    return h


def _is_event_row(d: dict) -> bool:
    return "start" in d and "end" in d and ("subject" in d or "summary" in d)


def annotate(data, table: dict):
    """Walk JSON and put a handle beside every id. Returns the same
    structure."""
    if isinstance(data, list):
        for x in data:
            annotate(x, table)
    elif isinstance(data, dict):
        for k in list(data.keys()):
            v = data[k]
            if isinstance(v, str) and k in KINDS and _GRAPH_ID.match(v):
                kind = "e" if (k == "id" and _is_event_row(data)) else KINDS[k]
                ref_key = "ref" if k in ("id", "message_id", "event_id", "draft_id") else k.replace("_id", "_ref")
                if ref_key not in data:
                    data[ref_key] = handle_for(table, kind, v)
            elif isinstance(v, (dict, list)):
                annotate(v, table)
    return data


def annotate_output(text: str) -> str:
    """The stdout of a read verb with handles added, when the run has an
    id and the output is JSON; otherwise the text as it was."""
    if not run_id() or not text or text.lstrip()[:1] not in "[{":
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    table = load()
    before = json.dumps(table, sort_keys=True)
    annotate(data, table)
    if json.dumps(table, sort_keys=True) != before:
        save(table)
    return json.dumps(data, indent=2) + ("\n" if text.endswith("\n") else "")


def resolve(value: str, table: dict | None = None) -> str:
    """The id a handle stands for, or the value itself. A table entry that
    is not a plain id is ignored, and the handle is left for the verb to
    refuse."""
    m = _HANDLE.match(str(value).strip())
    if not m:
        return value
    table = table if table is not None else load()
    found = table.get(m.group(0))
    if isinstance(found, str) and _PLAIN_ID.fullmatch(found):
        return found
    return value


def resolve_argv(argv: list[str]) -> list[str]:
    """argv with every handle after an id flag (or in `--flag=value`)
    replaced by its id. Unknown handles are left for the verb to refuse.
    Only the resident's own process resolves (_is_resident): an owner
    process never takes an id from the resident's table, and in a child the
    mediator started argv is what the mediator checked."""
    if not run_id() or os.environ.get(MEDIATED_VERB_ENV) or not _is_resident():
        return list(argv)
    table = load()
    if not table:
        return list(argv)
    out = list(argv)
    for i, a in enumerate(out):
        if a in ID_FLAGS and i + 1 < len(out):
            out[i + 1] = resolve(out[i + 1], table)
        elif "=" in a and a.split("=", 1)[0] in ID_FLAGS:
            flag, val = a.split("=", 1)
            out[i] = f"{flag}={resolve(val, table)}"
    return out
