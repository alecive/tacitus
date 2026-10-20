"""Deterministic diction guard for EA output.

A pure-text scrub applied to every outbound Slack message, independent
of model adherence. Email drafts have their own enforcement layers
(per-prompt STYLE_RULES and the rewrite hook on created drafts); Slack
reply text has only this.

Scope is deliberately narrow:
  - em-dash (—) and double-hyphen surrounded by spaces ( -- ) become
    a comma joint, per the substitution rule (commas/colons/semicolons).
  - en-dash (–) is NOT touched: legitimate range typography
    ("4–6 PM", "Jun 19–30") that the briefings themselves use.
  - text inside backtick code spans / fenced blocks is left alone
    (commands and ids must not be rewritten).
"""
from __future__ import annotations

import re

_EM_DASH_JOINT = re.compile(r"\s*—\s*")
_DOUBLE_HYPHEN_JOINT = re.compile(r"(?<=\S) -- (?=\S)")
_CODE_SPLIT = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)


def strip_em_dashes(text: str) -> tuple[str, int]:
    """Return (cleaned_text, replacements_made).

    Em-dashes used as a joint become ", "; leading/trailing ones are
    dropped. Code spans pass through untouched."""
    if not text or ("—" not in text and " -- " not in text):
        return text, 0
    count = 0
    parts = _CODE_SPLIT.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 1:  # code span, untouched
            continue
        n_em = part.count("—")
        n_hy = len(_DOUBLE_HYPHEN_JOINT.findall(part))
        if not n_em and not n_hy:
            continue
        part = _EM_DASH_JOINT.sub(", ", part)
        part = _DOUBLE_HYPHEN_JOINT.sub(", ", part)
        # an em-dash at line start or end leaves a dangling ", "
        part = re.sub(r"(?m)^, ", "", part)
        part = re.sub(r"(?m), $", "", part)
        parts[i] = part
        count += n_em + n_hy
    return "".join(parts), count
