#!/usr/bin/env python3
"""One owner-side lock per draft, so no draft edit lands between a send's
content check and the send itself.

A send reads the draft, checks its content fingerprint against the one the
owner approved, and then sends it in a separate request. update-draft,
attach-file, remove-attachment, delete-draft and create-reply are SAFE verbs
the mail mediator runs for the resident at any time. Without a lock, an edit
could land after the check and before the send, and the mail that left
would not be the mail the owner approved.

Both sides take the same lock files:

  - the send path (graph_client.send_draft, gmail_client.send_draft) holds
    the draft's lock around the check and the send;
  - the mail mediator holds it around each draft-changing verb
    (mail_mediation.draft_lock_scope).

The files live in the owner-private directory (owner_trust.private_dir(),
mode 0700), which the resident cannot reach, so the resident can neither
take a lock to stall a send nor remove one. Every holder first takes the
mailbox lock: shared for an operation on named drafts, exclusive for a verb
that cannot name its draft before it runs (create-reply finds the EA's draft
in the thread itself). The order is always mailbox, then drafts in sorted
order, so two holders never wait on each other.

    with hold(["<draft id>"]):            # one draft
    with hold(whole_mailbox=True):        # every draft
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import time
from pathlib import Path

WAIT_S = 20.0
POLL_S = 0.05
MAILBOX = "mailbox.lock"


class Busy(RuntimeError):
    """The lock was not free within the wait."""


def lock_dir() -> Path:
    """The lock directory, created 0700 in the owner-private directory."""
    try:
        import owner_trust
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import owner_trust
    d = owner_trust.private_dir() / "draft-locks"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _name(draft_id: str) -> str:
    return "draft-" + hashlib.sha256(str(draft_id).encode("utf-8")).hexdigest()[:40] + ".lock"


def _take(path: Path, mode: int, deadline: float) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(path), flags, 0o600)
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise Busy(f"{path.name} is held")
            time.sleep(POLL_S)
        except BaseException:
            os.close(fd)
            raise


@contextlib.contextmanager
def hold(draft_ids=(), *, whole_mailbox: bool = False, wait_s: float | None = None):
    """Hold the lock of each draft in `draft_ids` (exclusive) under the
    mailbox lock (shared), or the mailbox lock alone, exclusive, with
    `whole_mailbox` or with no draft named. Raises Busy when a lock is not
    free within `wait_s` seconds (WAIT_S when not given)."""
    ids = sorted({str(i) for i in draft_ids or () if str(i or "").strip()})
    whole = whole_mailbox or not ids
    d = lock_dir()
    deadline = time.monotonic() + max(0.0, WAIT_S if wait_s is None else wait_s)
    fds: list[int] = []
    try:
        fds.append(_take(d / MAILBOX, fcntl.LOCK_EX if whole else fcntl.LOCK_SH, deadline))
        if not whole:
            for i in ids:
                fds.append(_take(d / _name(i), fcntl.LOCK_EX, deadline))
        yield
    finally:
        for fd in reversed(fds):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
