"""Does a reply promise a button the bot never posted?

The model's reply and the bot's :lock: block are produced by different
processes. When the reply says "click Send on the :lock: block" and the
run's detection found no draft, the user is told to click something that
does not exist. The bot compares the two after every run: a reply that
names a button while the run posted no confirmation gets a :warning: line
in the thread, so a silent miss becomes a visible one.

`promised(text)` is the reading side, pure and unit-tested. The tally of
what a run actually posted lives in the bot (a per-run list every
confirmation poster appends to).
"""
from __future__ import annotations

import re

# Phrases that name a confirmation button or block. Case-insensitive.
_PROMISE_RE = re.compile(
    r"(?ix)"
    r"(?::lock:)"                                          # the block's own marker
    r"|\bclick\s+(?:the\s+)?(?:send|create|schedule|reschedule|delete|approve)\b"
    r"|\b(?:send|create|schedule|reschedule|delete)\s*(?:/\s*cancel\s*)?button"
    r"|\bsend\s*/\s*cancel\b"
    r"|\bconfirmation\s+(?:block|card|code)\b"          # the card and code frontends
    r"|\breact\s+with\b"
    r"|\bcode\s+is\s+in\s+your\s+dm\b"
    r"|\bbutton\s+(?:is\s+)?(?:above|below|in\s+(?:this\s+thread|your\s+dm))\b"
)

# A reply that says the button is somewhere else on purpose is not a
# promise about this turn (the bot's own pointer, or a report of an
# earlier block the user already has).
_DEFERRED_RE = re.compile(
    r"(?ix)\b(?:already|earlier|previous(?:ly)?|existing|still\s+(?:has|have|live))\b")


def promised(text: str) -> bool:
    """True when the reply tells the user a button or :lock: block exists."""
    if not text:
        return False
    for m in _PROMISE_RE.finditer(text):
        start = max(0, m.start() - 60)
        window = text[start:m.end() + 40]
        if _DEFERRED_RE.search(window):
            continue
        return True
    return False


WARNING = (":warning: My reply names a button, but no :lock: block or card was posted "
           "for this turn: no new draft or staged event was detected. Check your "
           "DM for a block that may belong to this thread, or tell me to create "
           "it again.")
