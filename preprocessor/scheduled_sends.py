#!/usr/bin/env python3
"""
Manage scheduled email sends. Drafts stay in Outlook until the send time.

Usage:
  python scheduled_sends.py add --draft-id <id> --send-after "2026-04-21T09:00" --description "..." [--ledger-id "..."]
  python scheduled_sends.py check        # cron calls this; sends anything due
  python scheduled_sends.py list         # show pending scheduled sends
  python scheduled_sends.py cancel --draft-id <id>
  python scheduled_sends.py due-today    # items sending today (for morning briefing)
"""

import argparse
import fcntl
import hashlib
import hmac
try:
    import atomic_io as _atomic
    import owner_trust as _owner_trust
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import atomic_io as _atomic
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import resident_io as _rio
import json
import os
import stat
import sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ledger as ledger_mod

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
SENDS_FILE = EA_DIR / "state" / "scheduled-sends.json"

# ── Approval credential ──────────────────────────────────────────────
# SENDS_FILE sits in an ea-bot-writable directory and `check` runs as the owner with
# the Graph token, so the file itself proves nothing. The credential is an
# HMAC-SHA256 over (draft_id, send_after, fingerprint), keyed by a file only the
# owner can read. The fingerprint is the content fingerprint of the draft the
# owner reviewed on the Schedule card (draft_fingerprint.py), and the entry
# stores it beside the token. An entry that ea-bot writes, edits, re-times,
# copies onto a different draft, or re-points at other content fails
# verification, and a draft whose content changed after the click no longer
# has the signed fingerprint, so `check` drops it. Verification is fail CLOSED:
# no key file, an unreadable key file, or a short key means nothing sends.
#
# Version 2 of the payload added the fingerprint. A version 1 token binds no
# content, so `check` drops such an entry with an alert and sends nothing; the
# owner schedules it again on a new card.
#
# The key sits in ~/.ea/auth, under a directory ea-bot owns, so ea-bot can
# rename the auth directory away and put its own in its place. An owner
# process therefore reads the key only through owner_trust: the directory and
# the file must be the owner's and writable by no other user, checked on the
# open descriptors. A key that fails the checks is treated as missing.
HMAC_KEY_FILE = EA_DIR / "auth" / "scheduled-send-hmac.key"
TOKEN_PREFIX = "pre-approved:v2:"
LEGACY_TOKEN_PREFIX = "pre-approved:v1:"
MIN_KEY_BYTES = 32


class ApprovalKeyError(RuntimeError):
    """The owner-only signing key is missing, unreadable, or too short."""


def _plain_key_read() -> bool:
    """True when the key is read by plain path: in the resident's own
    process, or on a scratch EA tree that is not the owner's install tree
    (a test). An owner process on the install tree always takes the checked
    read, whatever owner_trust.is_owner_process() says, so a missing or
    unexpected private directory cannot turn the checks off. An unknown
    answer counts as the checked read."""
    try:
        import pwd
        if os.geteuid() == pwd.getpwnam(_rio.RESIDENT_USER).pw_uid:
            return True
    except KeyError:
        pass
    except Exception:
        return False
    try:
        return (os.path.realpath(_owner_trust.ea_dir())
                != os.path.realpath(_owner_trust.install_ea_dir()))
    except Exception:
        return False


def _load_key() -> bytes:
    """Read the owner-only signing key. Raises ApprovalKeyError, never returns
    a weak key; callers treat any failure as 'do not send'. Unless
    _plain_key_read() says otherwise, the auth directory and the key file
    must pass the owner checks (owner_trust.read_owner_file), with no
    group-write allowance."""
    try:
        if not _plain_key_read():
            raw = _owner_trust.read_owner_file(HMAC_KEY_FILE)
        else:
            raw = HMAC_KEY_FILE.read_bytes()
    except _owner_trust.OwnerTrustError as e:
        raise ApprovalKeyError(f"refused {HMAC_KEY_FILE}: {e}") from None
    except OSError as e:
        raise ApprovalKeyError(
            f"cannot read {HMAC_KEY_FILE}: {e}. Provision it owned by the owner, mode 0600 "
            f"(head -c 48 /dev/urandom | base64 > {HMAC_KEY_FILE})") from None
    key = raw.strip()
    if len(key) < MIN_KEY_BYTES:
        raise ApprovalKeyError(
            f"{HMAC_KEY_FILE} is shorter than {MIN_KEY_BYTES} bytes")
    return key


def _payload(draft_id: str, send_after: str, fingerprint: str) -> bytes:
    """Signed message. NUL-separated so no field can impersonate another."""
    return "\x00".join(("ea-scheduled-send", "v2", draft_id, send_after,
                        fingerprint)).encode()


def make_send_token(draft_id: str, send_after: str, fingerprint: str) -> str:
    """Mint the approval credential for one draft at one time with one
    content fingerprint.

    Callable only where the key is readable (owner side, after the Slack button).
    Raises ApprovalKeyError if the key is missing, ValueError with no
    fingerprint."""
    if not fingerprint:
        raise ValueError("a scheduled send is signed over a content fingerprint")
    mac = hmac.new(_load_key(), _payload(draft_id, send_after, fingerprint),
                   hashlib.sha256).hexdigest()
    return f"{TOKEN_PREFIX}{mac}:{send_after}"


def is_legacy_token(token) -> bool:
    """True for a version 1 credential, which binds no content."""
    return isinstance(token, str) and token.startswith(LEGACY_TOKEN_PREFIX)


def token_shape_ok(token: str) -> bool:
    """True if the token is even the right SHAPE. Cheap, keyless, and decides
    'forged / legacy, drop it' apart from 'signed, but the key is unreadable
    right now, hold it'."""
    return isinstance(token, str) and token.startswith(TOKEN_PREFIX) \
        and len(token.split(":", 3)) == 4 and bool(token.split(":", 3)[2])


def verify_send_token(token: str, draft_id: str,
                      send_after: str | None = None,
                      fingerprint: str | None = None) -> bool:
    """True only for a credential this system signed for THIS draft with
    THIS content fingerprint.

    `send_after` binds the queue entry to the signed time: an entry whose
    send_after was edited (to fire early) no longer verifies. `fingerprint`
    is required: with none, or another one, nothing verifies. Legacy bare
    "pre-approved:..." markers and version 1 tokens fail here, by design."""
    # Every field comes from a file an untrusted user can write: nothing here
    # may raise, and anything unexpected is a refusal.
    if not isinstance(draft_id, str) or not token_shape_ok(token):
        return False
    if not isinstance(fingerprint, str) or not fingerprint:
        return False
    _, _, mac, signed_after = token.split(":", 3)  # pre-approved:v2:<mac>:<after>
    if send_after is not None and send_after != signed_after:
        return False
    try:
        key = _load_key()
    except ApprovalKeyError:
        return False
    expected = hmac.new(key, _payload(draft_id, signed_after, fingerprint),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, expected)


def _load_sends() -> list:
    """The queue, a list of entry dicts. Every command writes the list back
    into resident space, so it is read with the resident's access
    (resident_io refuses a link or a hard link at the name), and only dict
    entries with a string draft_id are kept. A file that is there but
    unreadable, not JSON or not a list raises, so the queue is never
    silently rewritten from nothing."""
    if _in_resident_space(SENDS_FILE):
        text = _rio.read_text(SENDS_FILE)
        if text is None and os.path.lexists(SENDS_FILE):
            raise OSError(f"{SENDS_FILE} is there but could not be read")
    elif SENDS_FILE.exists():
        text = SENDS_FILE.read_text()
    else:
        text = None
    if text is None:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"{SENDS_FILE} does not hold a JSON list")
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("draft_id"), str)]


def _save_sends(sends: list):
    _atomic.write_json(SENDS_FILE, sends)


# ── Queue lock ───────────────────────────────────────────────────────
# Every command here is a read-modify-write of one JSON file, and two of
# them run from different processes: `cancel` (bot side, on a click) and
# `check` (the */5 owner cron). Unlocked, the cron's _save_sends(remaining)
# rewrites the queue from a list it read BEFORE the cancel landed, so a
# cancelled send comes back, or a send queued mid-pass disappears. One
# exclusive flock per command, held across the whole read-modify-write.
def _sends_lock_path() -> Path:
    """Derived from SENDS_FILE so it follows a rebound queue path (tests)."""
    return SENDS_FILE.with_suffix(".lock")


def _in_resident_space(path: Path) -> bool:
    """True when this is an owner process and `path` lies in resident space
    (the EA tree as resolved, or the resident's home), which it reaches only
    with the resident's access. A non-owner process, or a path of the
    owner's own (a scratch directory), is reached directly."""
    if not _rio.is_owner():
        return False
    parent = os.path.realpath(Path(path).parent)
    roots = [os.path.realpath(_owner_trust.ea_dir())]
    home = os.path.expanduser(f"~{_rio.RESIDENT_USER}")
    if not home.startswith("~"):
        roots.append(os.path.realpath(home))
    return any(parent == r or parent.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def _open_sends_lock(lock: Path) -> int:
    """A descriptor to flock for the queue lock. The lock is shared with the
    resident (`cancel`) and never truncated, and a link at its name is never
    followed. In resident space an owner process creates it with the
    resident's access (resident_io.ensure_file) and opens it read-only
    (flock needs no write access); any other process creates it directly.
    Only a single-linked regular file is accepted."""
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if _in_resident_space(lock):
        if not _rio.ensure_file(lock):
            raise OSError(f"cannot create the lock file {lock}")
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | cloexec)
    else:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | cloexec,
                     0o664)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError(f"{lock} is not a plain single-linked file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _with_sends_lock(fn):
    """Run fn() holding the queue lock."""
    fd = _open_sends_lock(_sends_lock_path())
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fn()
    finally:
        os.close(fd)   # releases the lock


# What `check` (an owner cron) takes from the ea-bot-writable ~/.ea/.env: the
# Slack channel for alerts and the non-secret Graph settings, never a secret
# and never a path or a variable that says where to look next (EA_DIR,
# EA_PRIVATE_DIR, HOME, PATH). GRAPH_AUTHORITY is taken only on the Microsoft
# sign-in host, so no value in that file can send the refresh token elsewhere.
DOTENV_KEYS = ("EA_SLACK_CHANNEL_ID", "GRAPH_AUTH_METHOD", "GRAPH_CLIENT_ID",
               "GRAPH_SCOPES", "GRAPH_AUTHORITY")
GRAPH_AUTHORITY_PREFIX = "https://login.microsoftonline.com/"
SECRET_NAMES = ("SLACK_BOT_TOKEN", "GRAPH_CLIENT_SECRET")


def _load_env():
    """Fill in what `check` needs when it is not already in the environment:
    the secrets by the owner_trust rule (never from .env), and the
    DOTENV_KEYS settings from .env, read as data."""
    for key, val in _owner_trust.owner_secrets(
            SECRET_NAMES, legacy=EA_DIR / _owner_trust.LEGACY_SECRETS_FILE).items():
        os.environ.setdefault(key, val)
    for key, val in _owner_trust.read_env_values(EA_DIR / ".env", DOTENV_KEYS).items():
        if key == "GRAPH_AUTHORITY" and not val.startswith(GRAPH_AUTHORITY_PREFIX):
            continue
        os.environ.setdefault(key, val)


def _post_slack(text: str):
    """Post a message to the EA channel with the bot's credentials."""
    import chat_notify
    chat_notify.post_as_bot(os.environ.get("EA_SLACK_CHANNEL_ID", ""), text)


def _update_ledger(ledger_id: str, description: str):
    """Close the row the scheduled send names, now that it went out. The
    closure policy decides (a row waiting on someone else stays open)."""
    if not ledger_id:
        return
    if not ledger_mod.entry_exists(ledger_id):
        return
    import closure
    db = ledger_mod._get_db()
    try:
        closure.settle(db, ledger_id, signal="linked_scheduled_send", actor="scheduled_sends",
                       note=f"Scheduled send delivered: {description}")
        db.commit()
    finally:
        db.close()


def cmd_add(args):
    # Refuse to queue anything that is not signed for this exact draft and
    # time. `check` re-verifies (the file can be written behind our back), but
    # refusing here turns a bad call into an immediate error instead of a
    # silent entry that gets dropped hours later.
    token = getattr(args, "confirm_token", "") or ""
    fingerprint = getattr(args, "fingerprint", "") or ""
    if not verify_send_token(token, args.draft_id, args.send_after,
                             fingerprint=fingerprint):
        print(json.dumps({"error": "refused: --confirm-token is not a valid "
                                   "approval signature for this draft, time "
                                   "and content. "
                                   "Schedule via `graph_actions.py schedule-send` "
                                   "after a Slack approval."}))
        return

    def _queue() -> dict:
        sends = _load_sends()
        # Check for duplicate
        for s in sends:
            if s["draft_id"] == args.draft_id:
                return {"error": f"Draft {args.draft_id} already scheduled"}
        sends.append({
            "draft_id": args.draft_id,
            "send_after": args.send_after,
            "description": args.description,
            "ledger_id": args.ledger_id or "",
            "confirm_token": getattr(args, "confirm_token", "") or "",
            "fingerprint": fingerprint,
            "created": datetime.now().isoformat(),
        })
        _save_sends(sends)
        return {"status": "scheduled", "send_after": args.send_after,
                "description": args.description}

    print(json.dumps(_with_sends_lock(_queue)))


# A queued draft that Graph refuses (deleted in Outlook, mailbox locked)
# is dropped after this many attempts, so a send that can never succeed
# stops warning Slack every five minutes.
MAX_SEND_ATTEMPTS = 5


def cmd_check(args):
    """Send any drafts that are due. Called by cron every 5 minutes.

    The whole pass runs under the queue lock: a `cancel` that lands while
    this is deciding what to send would otherwise be undone by the
    _save_sends at the end, and a send the user just cancelled would go out
    on the next tick.
    """
    _load_env()
    print(json.dumps(_with_sends_lock(lambda: _check_pass())))


def _check_pass() -> dict:
    sends = _load_sends()
    now = datetime.now()
    remaining = []
    sent_count = 0
    refused_count = 0

    for entry in sends:
        description = entry.get("description", "(no description)")
        draft_id = entry.get("draft_id") or ""
        send_after = entry.get("send_after") or ""
        try:
            send_time = datetime.fromisoformat(send_after)
        except (TypeError, ValueError):
            # An unparseable time can never come due. Drop this entry only;
            # aborting the pass would strand every legitimate send.
            refused_count += 1
            _post_slack(f":red_circle: Scheduled send dropped, unreadable "
                        f"send_after {send_after!r}: {description}")
            continue
        if send_time.tzinfo is not None:
            # `now` is naive local, so an offset-aware send_after must be
            # converted before comparison. The signed STRING is untouched,
            # so the HMAC over (draft_id, send_after, fingerprint) still verifies.
            send_time = send_time.astimezone().replace(tzinfo=None)
        if send_time <= now:
            # The queue file is writable by the prompt-injectable user, so the
            # signature is the authority, not the entry. Verify before the
            # Graph token is ever touched.
            token = entry.get("confirm_token") or ""
            if token_shape_ok(token):
                try:
                    _load_key()
                except ApprovalKeyError as e:
                    # Operational, not an attack: hold the entry and alarm.
                    _post_slack(f":warning: Scheduled send held, approval key "
                                f"unavailable ({e}). Nothing was sent.")
                    remaining.append(entry)
                    continue
            if is_legacy_token(token):
                refused_count += 1
                _post_slack(
                    f":red_circle: Scheduled send dropped: {description}. It was "
                    f"approved before approvals were bound to the draft content, "
                    f"so nothing was sent. Schedule it again to review the "
                    f"current draft.")
                continue
            fingerprint = entry.get("fingerprint") or ""
            if not verify_send_token(token, draft_id, send_after,
                                     fingerprint=fingerprint):
                refused_count += 1
                _post_slack(
                    f":red_circle: Scheduled send REFUSED and dropped: "
                    f"{description}. No valid approval signature for draft "
                    f"{draft_id[:24]}. Nobody approved this send.")
                continue
            # Try to send
            try:
                import graph_client as graph
                # The draft is read again: the signature covers the content
                # the owner reviewed, and update-draft, attach-file and
                # remove-attachment can change a queued draft. send_draft
                # checks once more right before the send.
                current = graph.draft_fingerprint(draft_id)
                result = (graph.send_draft(draft_id, confirm_token=token,
                                           fingerprint=fingerprint)
                          if current == fingerprint else {"status": "changed"})
                if result.get("status") == "changed":
                    refused_count += 1
                    _post_slack(
                        f":red_circle: Scheduled send DROPPED: {description}. The "
                        f"draft changed after you approved it, so nothing was "
                        f"sent. The draft is still in Outlook; schedule it again "
                        f"to review the new content.")
                    continue
                if result.get("status") == "blocked":
                    _post_slack(f":warning: Scheduled send token rejected: {description}: {result.get('error', 'unknown')}")
                    remaining.append(entry)
                    continue
                if result.get("status") != "sent":
                    # A held draft lock or any other failure: nothing went
                    # out, so the entry stays queued for the next run.
                    _post_slack(f":warning: Scheduled send not sent yet: {description}: "
                                f"{result.get('error') or result.get('status') or 'unknown'}. "
                                f"It stays queued and is tried again.")
                    remaining.append(entry)
                    continue
                sent_count += 1
                # Update ledger
                _update_ledger(entry.get("ledger_id", ""), description)
                # Silent - next morning briefing will report it
            except Exception as e:
                attempts = int(entry.get("attempts") or 0) + 1
                entry["attempts"] = attempts
                if attempts >= MAX_SEND_ATTEMPTS:
                    refused_count += 1
                    _post_slack(
                        f":red_circle: Scheduled send GIVEN UP after "
                        f"{attempts} attempts: {description} - {e}. The draft "
                        f"is still in Outlook; nothing was sent.")
                    continue
                _post_slack(f":warning: Scheduled send failed "
                            f"(attempt {attempts}/{MAX_SEND_ATTEMPTS}): "
                            f"{description} - {e}")
                remaining.append(entry)  # Keep it for retry
        else:
            remaining.append(entry)

    _save_sends(remaining)
    return {"checked": len(sends), "sent": sent_count,
            "refused": refused_count, "remaining": len(remaining)}


def cmd_list(args):
    sends = _load_sends()
    print(json.dumps(sends, indent=2))


def cmd_cancel(args):
    def _drop() -> dict:
        sends = _load_sends()
        remaining = [s for s in sends if s["draft_id"] != args.draft_id]
        if len(remaining) == len(sends):
            return {"error": f"No scheduled send found for draft {args.draft_id}"}
        _save_sends(remaining)
        return {"status": "cancelled", "draft_id": args.draft_id}

    print(json.dumps(_with_sends_lock(_drop)))


def cmd_due_today(args):
    """Return items scheduled to send today (for morning briefing preview)."""
    print(json.dumps(get_due_today(), indent=2))


# ── Module API (for import by preprocess.py) ─────────────────────────

def get_due_today() -> list:
    """Return scheduled sends due today. For use by preprocessor."""
    today = date.today()
    due = []
    for entry in _load_sends():
        try:
            send_date = datetime.fromisoformat(entry.get("send_after") or "").date()
        except (TypeError, ValueError):
            continue   # a malformed entry must not break the briefing
        if send_date == today:
            due.append(entry)
    return due


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage scheduled email sends", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add", allow_abbrev=False)
    p.add_argument("--draft-id", required=True)
    p.add_argument("--send-after", required=True, help="ISO datetime, e.g. 2026-04-21T09:00")
    p.add_argument("--description", required=True)
    p.add_argument("--ledger-id", default=None)
    p.add_argument("--confirm-token", default=None, help="Approval token from user confirmation")
    p.add_argument("--fingerprint", default="",
                   help="Content fingerprint of the reviewed draft (signed in the token)")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("check", allow_abbrev=False)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("list", allow_abbrev=False)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("cancel", allow_abbrev=False)
    p.add_argument("--draft-id", required=True)
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("due-today", allow_abbrev=False)
    p.set_defaults(func=cmd_due_today)

    args = parser.parse_args()
    args.func(args)
