"""Keep EA draft bodies long enough to learn from what the user changed.

The style signal is the diff between what the EA drafted and what the user
sent. This holds the last EA-authored body for each draft; the monthly
style harvest pairs each one against the sent message from the same
conversation, hands the resident the before/after, and purges anything
past the retention window, so the store never becomes a shadow copy of
the mail.

Retention covers the harvest cadence with a few days' overlap, so
nothing is dropped between runs and nothing older than one harvest is
kept.

Best-effort throughout. A capture failure must never break the draft
operation that just succeeded.

The store is in the resident's space and both users write it (the resident,
and the owner's graph_actions under mail mediation or from the bot). Writes
and reads go through resident_io, so an owner process touches the store
only with the resident's access. Listing the whole store from an owner
process takes one resident child (_DUMP_CHILD) instead of one per file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import resident_io as _rio

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
STORE = EA_DIR / "state" / "draft-bodies"
RETENTION_DAYS = 31
# Below this a "draft" is a stub and the diff teaches nothing.
MIN_WORDS = 15

_TAG_RE = re.compile(r"<[^>]+>")


def _key(draft_id: str) -> str:
    """Filename for a draft id.

    Graph ids are long and contain / and +, so they cannot be filenames.
    """
    return hashlib.sha256(draft_id.encode("utf-8")).hexdigest()[:24]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def capture(draft_id: str, body: str, *, verb: str = "",
            conversation_id: str = "", subject: str = "",
            to: list | None = None) -> None:
    """Record the body the EA just wrote for this draft.

    Overwrites on update: what the user edits from is the last thing the EA
    wrote, so an earlier revision is not the baseline for the diff.
    """
    try:
        if not draft_id or not body:
            return
        text = _TAG_RE.sub(" ", body) if "<" in body else body
        if len(text.split()) < MIN_WORDS:
            return
        path = STORE / f"{_key(draft_id)}.json"
        prior = {}
        try:
            prior = json.loads(_rio.read_text(path) or "{}")
        except Exception:
            prior = {}
        if not isinstance(prior, dict):
            prior = {}
        _rio.replace_text(path, json.dumps({
            "draft_id": draft_id,
            "conversation_id": conversation_id or prior.get(
                "conversation_id", ""),
            "subject": subject or prior.get("subject", ""),
            "to": to or prior.get("to") or [],
            "verb": verb or prior.get("verb", ""),
            "first_captured_at": prior.get("first_captured_at") or _now(),
            "captured_at": _now(),
            "revisions": (prior.get("revisions") or 0) + 1,
            "body": body,
        }, indent=2), 0o664)
    except Exception:
        pass


def ids_for_conversation(conversation_id: str) -> set:
    """Draft ids the EA captured for one conversation.

    Half of the create-reply provenance check: the dedupe may only ever
    overwrite a draft the EA itself wrote. Captures are skipped for stub
    bodies (MIN_WORDS), so this is a partial view by design -- pair it
    with draft_events.known_draft_ids().
    """
    if not conversation_id:
        return set()
    out = set()
    for rec in load_all():
        if rec.get("conversation_id") == conversation_id and rec.get("draft_id"):
            out.add(rec["draft_id"])
    return out


# The program one resident child runs to list the store for an owner process.
# It imports only the standard library and depends on no file the resident
# can change. argv: <store>. Every *.json directly in the store is opened
# without following a link at its name and taken only when it is a
# single-linked regular file; the reply is `DRAFT-CAPTURE-OK <n>\n` and n
# bytes of JSON, a list of [name, text].
_DUMP_OK = b"DRAFT-CAPTURE-OK "
_DUMP_CHILD = r'''
import json, os, stat, sys
OK = b"DRAFT-CAPTURE-OK "
MAX = 4194304
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
store = sys.argv[1]
out = []
try:
    names = sorted(os.listdir(store))
except FileNotFoundError:
    names = []
for name in names:
    if not name.endswith(".json"):
        continue
    try:
        fd = os.open(os.path.join(store, name), FLAGS)
    except OSError:
        continue
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX:
            continue
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
    finally:
        os.close(fd)
    out.append([name, b"".join(chunks).decode("utf-8", "replace")])
payload = json.dumps(out).encode()
sys.stdout.buffer.write(OK + str(len(payload)).encode() + b"\n" + payload)
sys.stdout.buffer.flush()
'''
DUMP_TIMEOUT_S = 60


def _dump_via_resident() -> list:
    """[(name, text)] for every capture, read by one resident child; [] on
    any outcome but a clean framed reply (fail closed)."""
    argv = [_rio._resident_python(), "-I", "-c", _DUMP_CHILD,
            _rio._resolved(os.path.abspath(STORE))]
    p = None
    try:
        p = _rio._spawn(argv)
        out, _err = p.communicate(b"", timeout=DUMP_TIMEOUT_S)
    except Exception as e:
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass
        print(f"draft_capture: resident listing failed: {e}", file=sys.stderr)
        return []
    head, sep, payload = (out or b"").partition(b"\n")
    digits = head[len(_DUMP_OK):] if head.startswith(_DUMP_OK) else b""
    if p.returncode != 0 or not sep or not digits.isdigit() or int(digits) != len(payload):
        print("draft_capture: resident listing gave no valid reply", file=sys.stderr)
        return []
    try:
        rows = json.loads(payload)
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    return [(r[0], r[1]) for r in rows
            if isinstance(r, list) and len(r) == 2 and isinstance(r[0], str)
            and isinstance(r[1], str) and "/" not in r[0] and r[0] not in (".", "..")]


def _raw_captures() -> list:
    """[(path, text)] for every capture in the store."""
    if _rio._via_child():
        return [(STORE / n, t) for n, t in _dump_via_resident()]
    out = []
    try:
        paths = sorted(STORE.glob("*.json"))
    except Exception:
        return []
    for path in paths:
        text = _rio.read_text(path)
        if text is not None:
            out.append((path, text))
    return out


def _load_with_paths() -> list:
    out = []
    try:
        for path, text in _raw_captures():
            try:
                rec = json.loads(text)
            except Exception:
                continue
            if isinstance(rec, dict):
                out.append((path, rec))
    except Exception:
        return []
    return sorted(out, key=lambda p: p[1].get("captured_at", ""))


def load_all() -> list:
    return [rec for _, rec in _load_with_paths()]


def drop(draft_id: str) -> None:
    try:
        (STORE / f"{_key(draft_id)}.json").unlink(missing_ok=True)
    except Exception:
        pass


def purge(days: int = RETENTION_DAYS) -> int:
    """Delete captures past the retention window. Returns how many went.

    Unlinks the file it actually read rather than recomputing the name
    from the draft id, so a record whose filename ever drifts from its
    key is still purged instead of counted and left behind.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    removed = 0
    for path, rec in _load_with_paths():
        if (rec.get("captured_at") or "") < cutoff:
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except Exception:
                continue
    return removed


def _norm(text: str) -> list:
    """Words, case-folded, for comparing meaning rather than whitespace."""
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def pair_with_sent(sent: list, captures: list | None = None) -> list:
    """Match each captured draft to the message it became.

    Keyed on conversationId, which survives the send; the draft's own id
    does not. Falls back to an exact subject match for a fresh draft that
    had no conversation yet.
    """
    import style_profiles as sp

    captures = load_all() if captures is None else captures
    if not captures:
        return []

    by_conv, by_subject = {}, {}
    for m in sent or []:
        if m.get("isDraft"):
            continue
        conv = m.get("conversationId") or ""
        when = m.get("sentDateTime") or ""
        body = m.get("body")
        content = body.get("content") if isinstance(body, dict) else ""
        entry = {"sent_at": when,
                 "text": sp._own_text(sp._html_to_text(content or "")),
                 "subject": m.get("subject") or ""}
        if not entry["text"]:
            continue
        # keep the earliest send after a capture, so a later message in
        # the same thread is not mistaken for the edited draft
        if conv:
            by_conv.setdefault(conv, []).append(entry)
        subj = re.sub(r"(?i)^\s*(re|fw|fwd):\s*", "", entry["subject"]).strip()
        if subj:
            by_subject.setdefault(subj.lower(), []).append(entry)

    pairs = []
    for rec in captures:
        cands = by_conv.get(rec.get("conversation_id") or "", [])
        if not cands:
            subj = re.sub(r"(?i)^\s*(re|fw|fwd):\s*",
                          "", rec.get("subject") or "").strip().lower()
            cands = by_subject.get(subj, [])
        after = sorted((c for c in cands
                        if c["sent_at"] >= (rec.get("captured_at") or "")),
                       key=lambda c: c["sent_at"])
        if not after:
            continue
        drafted = sp._own_text(sp._html_to_text(rec.get("body") or ""))
        final = after[0]["text"]
        if len(final.split()) < MIN_WORDS:
            continue
        dw, fw = _norm(drafted), _norm(final)
        if not dw:
            continue
        kept = len(set(dw) & set(fw))
        pairs.append({
            "subject": rec.get("subject") or after[0]["subject"],
            "to": rec.get("to") or [],
            "sent_at": after[0]["sent_at"],
            "revisions": rec.get("revisions", 1),
            "draft_words": len(dw),
            "sent_words": len(fw),
            "length_change_pct": round(100 * (len(fw) - len(dw)) / len(dw)),
            # rough overlap, enough to rank which pairs are worth reading
            "overlap_pct": round(100 * kept / len(set(dw))),
            "drafted": drafted,
            "sent": final,
        })
    return sorted(pairs, key=lambda p: p["overlap_pct"])
