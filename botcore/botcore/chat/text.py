"""Text helpers shared by the frontends: chunking, mrkdwn conversion and
topic names.

The LLM writes Slack mrkdwn on every platform; a frontend that renders
another markup converts on the way out. mrkdwn_to_zulip is that
conversion for Zulip Markdown.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from . import card_text as _ct

from .zulip_emoji_names import SLACK_TO_ZULIP

# Slack shortcode -> Zulip name for every Slack name Zulip does not know.
# The generated table (scripts/zulip_emoji_table.py) covers Slack's whole
# vocabulary, since the LLM picks any of it; the entries after it are the
# names that table cannot join. scripts/zulip_emoji_check.py checks the
# repo's own shortcodes against the live server.
EMOJI_ALIASES = {
    **SLACK_TO_ZULIP,
    # Zulip has one clock face, so Slack's other hour faces show it.
    **{f"clock{h}{m}": "clock" for h in range(1, 13) for m in ("", "30")},
    # Not a Slack name, but the LLM writes it after the other large circles.
    "large_red_circle": "red_circle",
    # Zulip knows x as the boxed cross, which reads as a ticked box; Slack's
    # x is the failure mark. The table leaves Zulip's own names alone, so
    # this one is set here.
    "x": "cross_mark",
}


def smart_chunk(text: str, limit: int = 3000) -> list[str]:
    """Split text into chunks at paragraph boundaries, respecting the limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find the last double-newline within the limit
        cut = text.rfind("\n\n", 0, limit)
        if cut == -1:
            # Fall back to single newline
            cut = text.rfind("\n", 0, limit)
        if cut == -1:
            # Fall back to space
            cut = text.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return chunks


_CODE_SPLIT_RE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_ANGLE_RE = re.compile(r"<([^<>\s][^<>]*)>")
_URL_RE = re.compile(r"^([a-z][a-z0-9+.-]*:[^|\s]+)(?:\|(.*))?$", re.IGNORECASE)
_CHANNEL_RE = re.compile(r"^#([A-Za-z0-9:_-]+)(?:\|(.*))?$")
_USER_RE = re.compile(r"^@([A-Za-z0-9]+)(?:\|.*)?$")
_BOLD_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_ITALIC_RE = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_STRIKE_RE = re.compile(r"(?<![\w~])~(?=\S)([^~\n]+?)(?<=\S)~(?![\w~])")
_EMOJI_RE = re.compile(r":([a-z0-9_+-]+):")
_HOLD_RE = re.compile(r"\x00(\d+)\x00")


def _unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _convert_plain(text: str, mention: Callable[[str], str] | None,
                   card: bool = False) -> str:
    """Convert one stretch of text that holds no code. In card text a user
    mention stays text and a labelled link shows its target."""
    held: list[str] = []

    def hold(out: str) -> str:
        held.append(out)
        return f"\x00{len(held) - 1}\x00"

    def angle(m: re.Match) -> str:
        inner = m.group(1)
        if cm := _CHANNEL_RE.match(inner):
            cid, name = cm.group(1), cm.group(2)
            if mention is not None:
                return hold(mention(cid))
            return hold(f"#{name or cid}")
        if um := _USER_RE.match(inner):
            return hold(f"@{um.group(1)}" if card else f"@**{um.group(1)}**")
        if lm := _URL_RE.match(inner):
            url, label = lm.group(1), lm.group(2)
            url = _unescape(url)
            if card:
                return hold(f"{_unescape(label)} ({url})" if label else url)
            return hold(f"[{_unescape(label)}]({url})" if label else url)
        return m.group(0)

    text = _ANGLE_RE.sub(angle, text)
    text = _BOLD_RE.sub(r"**\1**", text)
    text = _ITALIC_RE.sub(r"*\1*", text)
    text = _STRIKE_RE.sub(r"~~\1~~", text)
    text = _EMOJI_RE.sub(lambda m: f":{EMOJI_ALIASES.get(m.group(1), m.group(1))}:", text)
    text = _unescape(text)
    return _HOLD_RE.sub(lambda m: held[int(m.group(1))] if int(m.group(1)) < len(held)
                        else "", text)


# An inert span (card_text.inert) the template wrapped in backticks: the
# span is a code span already.
_TICKED_INERT_RE = re.compile(
    f"`({_ct.INERT_OPEN}[^{_ct.INERT_OPEN}{_ct.INERT_CLOSE}]*{_ct.INERT_CLOSE})`")
_INERT_HOLD_RE = re.compile(r"\x01(\d+)\x01")


def mrkdwn_to_zulip(text: str, mention: Callable[[str], str] | None = None,
                    card: bool = False) -> str:
    """Slack mrkdwn to Zulip Markdown; code spans and fences stay as written.

    `<#C123>` becomes mention("C123") when a mention function is given,
    else `#C123` (or `#name` for the `<#C123|name>` form). An inert span
    (card_text.inert) becomes a code span whatever it holds.

    `card` is for the text of a confirmation card: invisible characters are
    marked, Zulip's own link, mention, math and widget syntax is broken so
    it shows as typed, a Slack user mention stays text, a labelled link
    shows its target, and no line opens a fence, a rule or a heading.
    """
    # The converter's own placeholders (\x00n\x00 and \x01n\x01) cannot
    # come from the text: a forged one would pick another span or none.
    # Card text marks them (clean); other text drops them.
    text = text or ""
    if card:
        text = _ct.clean(text, keep_inert=True)
    else:
        text = text.replace("\x00", "").replace("\x01", "")
    held: list[str] = []
    if _ct.INERT_OPEN in text:
        text = _TICKED_INERT_RE.sub(r"\1", text)

        def hold(m: re.Match) -> str:
            held.append(_ct.zulip_code_span(m.group(1)))
            return f"\x01{len(held) - 1}\x01"
        text = _ct.INERT_RE.sub(hold, text)
        text = text.replace(_ct.INERT_OPEN, "").replace(_ct.INERT_CLOSE, "")
    if card:
        text = _ct.zulip_defang_inline(text)
    parts = _CODE_SPLIT_RE.split(text)
    out = "".join(p if i % 2 else _convert_plain(p, mention, card)
                  for i, p in enumerate(parts))
    if card:
        out = _ct.zulip_defang_lines(out)
    if held:
        out = _INERT_HOLD_RE.sub(
            lambda m: held[int(m.group(1))] if int(m.group(1)) < len(held) else "", out)
    return out


_TOPIC_LINK_RE = re.compile(r"<[^<>|\s]+\|([^<>]*)>|<([^<>|\s]+)>")
# Zulip Markdown in an owner's post: [label](url) and @**Name|id** mentions.
_TOPIC_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)\s]*\)")
_TOPIC_MENTION_RE = re.compile(r"@_?\*\*([^*|]+)(?:\|\d+)?\*\*")
_TOPIC_LEAD_RE = re.compile(r"^(?:[>#]+\s*)+")
_TOPIC_MARK_RE = re.compile(r"[*~`]|(?<!\w)_|_(?!\w)")


def topic_from_text(text: str, limit: int = 60) -> str:
    """A topic name from a message, in Slack mrkdwn or Zulip Markdown: its
    first non-empty line, links reduced to their labels, marks and
    shortcodes stripped, cut at a word boundary within limit."""
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    line = _TOPIC_LEAD_RE.sub("", line)
    line = _TOPIC_MD_LINK_RE.sub(lambda m: m.group(1), line)
    line = _TOPIC_MENTION_RE.sub(lambda m: m.group(1), line)
    line = _TOPIC_LINK_RE.sub(lambda m: m.group(1) or m.group(2), line)
    line = _EMOJI_RE.sub("", line)
    line = _TOPIC_MARK_RE.sub("", line)
    line = _unescape(" ".join(line.split()))
    if len(line) > limit:
        cut = line.rfind(" ", 0, limit + 1)
        line = line[:cut if cut > 0 else limit].rstrip().rstrip(",;:-–—").rstrip()
    return line or "chat"


def unique_topic(name: str, taken, limit: int = 60) -> str:
    """name, or name with the lowest " (n)" suffix not in taken, cut to fit
    limit. Zulip compares topic names without case, so this does too."""
    used = {str(t).lower() for t in taken}
    if name.lower() not in used:
        return name
    n = 2
    while True:
        suffix = f" ({n})"
        candidate = name[:max(1, limit - len(suffix))].rstrip() + suffix
        if candidate.lower() not in used:
            return candidate
        n += 1


# The first line of every message carried over from Slack history into
# Zulip: the author, the original time and this mark. Readers of recent
# chat (the briefing's chat_read) skip marked messages, since their Zulip
# timestamps are the day of the port, not the day they were written.
PORTED_MARK = "from Slack"
_PORTED_RE = re.compile(r"\*\*[^*\n]+\*\* · \d{4}-\d{2}-\d{2} \d{2}:\d{2} · from Slack(?:\n|$)")


def ported_header(who: str, when: str) -> str:
    """The header line of a ported message; when is "YYYY-MM-DD HH:MM"."""
    return f"**{who}** · {when} · {PORTED_MARK}"


def is_ported(text: str) -> bool:
    """True for a message that opens with a ported_header line."""
    return bool(_PORTED_RE.match(text or ""))
