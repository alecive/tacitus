#!/usr/bin/env python3
"""
Mailing-list digest triage: the code half of the `list-digest` agent.

Two shapes of mail arrive as "many items in one email":

  * DIGESTS (robotics-worldwide): plain text, 10-20 posts between dash
    rules. Almost none matter; the few that do (a dataset release) sit
    between job ads and workshop deadline extensions.
  * NEWSLETTERS (a university research office's funding bulletin): HTML,
    every item a hyperlink behind a click-tracking redirect, sections of
    funding calls with due dates.

Division of labour:
  * this module (runs as the owner inside run-agent.sh's preprocess step,
    zero Claude tokens): find the mails in the inbox, split each into
    items, decode Outlook safelinks, resolve tracking redirects to the
    real page, drop job posts deterministically, and write
    list-digest-latest.json;
  * skills/list-digest.md (Claude): judge or rank the items against
    user-profile.json plus the user's rules from preferences.py (scopes
    `newsletters`, `newsletters:<list>`, and the list's own `scopes`),
    which ride on each entry as `rules`; fetch the linked pages where the
    list asks for it; write the Slack snippets, or the NOTHING_RELEVANT
    sentinel;
  * run-agent.sh: posts the text, THEN runs `list_digest.py archive`, so a
    failed run (Claude down, validation failed, Slack post failed) leaves
    the mail in the inbox and the next scheduled pass retries it. Nothing
    is archived unseen.

Config: ~/.ea/config/list-digests.json (template in config/).

Usage:
    python3 list_digest.py collect [--output FILE]
        (what `preprocess.py --mode list-digest` runs)
    python3 list_digest.py parse --file body.txt|body.html [--list-name NAME]
        (offline: split a saved body, for debugging a format change)
    python3 list_digest.py archive --input list-digest-latest.json \
        --disposition posted|silent
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import preferences as _prefs  # the personalization store (rules per scope)
import atomic_io as _atomic  # noqa: E402
import resident_io as _rio  # noqa: E402

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
CONFIG_FILE = EA_DIR / "config" / "list-digests.json"
PROFILE_FILE = EA_DIR / "config" / "user-profile.json"
STATE_DIR = EA_DIR / "state"
HISTORY_FILE = STATE_DIR / "list-digest-history.jsonl"

DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_MAX_ITEM_CHARS = 2200
# What an unparsed mail is allowed to hand Claude raw: enough for a whole
# normal digest, bounded so a runaway message cannot blow the prompt.
RAW_FALLBACK_CHARS = 14000
MAX_LINKS_PER_ITEM = 8
REDIRECT_HOPS = 5
REDIRECT_TIMEOUT_S = 8

# Per-list keys the skill needs to see on every entry, with defaults.
LIST_PASSTHROUGH = {
    "mode": "filter",          # filter: post the few relevant items
                               # prioritize: rank what is worth his time
    "follow_links": False,     # skill fetches each candidate's page
    "emoji": ":newspaper:",    # header shortcode of the Slack post
    "notes": "",               # free text about this list for the skill
}

# Tags that mark a job post on every list seen so far ([Jobs], [jobs],
# [Job], [Positions]). The config's drop_tags extends this per list.
DEFAULT_DROP_TAGS = {"jobs", "job", "position", "positions", "hiring",
                     "career", "careers"}

# Backstop for job posts without a tag. Conservative on purpose: the tag
# catches the bulk, and a false positive here hides an item forever.
# "postdoc" alone is not enough (a postdoc appreciation luncheon is an
# event, not an ad); it needs a position word next to it.
JOB_SUBJECT_RE = re.compile(
    r"(post-?doc(?:toral)?\s+(?:position|opening|researcher|"
    r"research\s+(?:assistant|associate|fellow)|opportunit\w*|vacanc\w*|job)"
    r"|ph\.?d\.?\s+(?:position|opening|student\w*|scholarship|studentship|opportunit\w*)"
    r"|faculty\s+position|tenure[- ]track|professorship"
    r"|(?:research|engineer\w*|scientist|fellow\w*)\s+positions?\b"
    r"|positions?\s+(?:at|in|available|open)\b"
    r"|job\s+(?:opening|posting|opportunit\w*|offer)"
    r"|vacanc\w*|we\s*(?:'|a)re\s+hiring|\bhiring\b|internship)",
    re.I,
)

# --- digest body structure -------------------------------------------------
# Sympa/KIT digests: "Table of contents:" then "* N - subject - author", a
# 70-dash rule, then items headed " * N" and separated by 30-dash rules.
# Mailman digests: "Today's Topics:", items headed "Message: N", same rules.
_SEP_RE = re.compile(r"^\s*-{20,}\s*$")
_ITEM_HEAD_RE = re.compile(r"^\s*(?:\*\s*(\d+)|Message:\s*(\d+))\s*$")
_HEADER_RE = re.compile(
    r"^(Date|From|Subject|To|Cc|Reply-To|Message-ID|Message-Id|In-Reply-To|"
    r"References|Content-Type|MIME-Version):\s?(.*)$", re.I)
_TOC_LINE_RE = re.compile(r"^\s*\*\s*(\d+)\s*-\s+\S")
_TAG_RE = re.compile(r"\[([^\[\]]{1,40})\]")
_FROM_RE = re.compile(r'^\s*"?(.*?)"?\s*<([^<>\s]+@[^<>\s]+)>\s*$')
_BANNER_RE = re.compile(r"^\s*\[External email - use caution\]\s*$", re.I | re.M)
_SAFELINK_RE = re.compile(
    r"https?://[A-Za-z0-9.-]*safelinks\.protection\.outlook\.com/\?[^\s<>\"'\)\]]+")
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")

# --- newsletter (HTML) structure -------------------------------------------
# Every real item is a hyperlink. The block around it (an <li>, else the
# <p> or <td>) carries the due date and flags; a nested list starting with
# "Note" is the contact line. Anchors that are navigation, tracking pixels
# or the newsletter's own name are not items.
STOP_LINK_TEXT = {
    "view on website", "view in browser", "view online", "unsubscribe",
    "manage your preferences", "click here to manage your preferences or unsubscribe",
    "contact us", "new", "read more", "learn more", "here", "website",
    "privacy policy", "forward to a friend",
}


def _institution_phrases() -> set[str]:
    """The institution's own footer text ("www.<domain>"; the profile's
    institution block), never an item."""
    try:
        try:
            import institution
        except ImportError:
            from preprocessor import institution
        return set(institution.boilerplate_phrases())
    except Exception:
        return set()
_DUE_RE = re.compile(r"\b(?:due|deadline|apply by|register by|by)\s*:?\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?)", re.I)
# A date that opens the item line. One research-office bulletin writes "09/09 : Event" and
# "Due 10/12: Seed Grant"; a college research-support newsletter writes
# "October 30, 2026 | Young Investigator Program", "Sept. 15, 2026 |
# Sloan", "Anytime | NSF: ..." and "Rolling Application Deadline |".
_LEAD_DATE_RE = re.compile(
    r"^\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?(?:\s*(?:&|and|,|–|-|to)\s*\d{1,2}(?:/\d{1,2})?)*)\s*[:|]")
_LEAD_MONTH_RE = re.compile(r"^\s*([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?\s*[:|]")
_LEAD_WORD_RE = re.compile(
    r"^\s*(anytime|open|rolling(?:\s+application)?(?:\s+deadline)?|deadlines?\s+var(?:y|ies))\s*[:|]",
    re.I)
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_ROLLING_RE = re.compile(r"rolling\s+(?:application\s+)?(?:deadline|basis|submission)", re.I)
_VARIES_RE = re.compile(r"\bdeadlines?\s+var(?:y|ies)\b", re.I)
_ANYTIME_RE = re.compile(r"\b(?:accepted\s+)?anytime\b", re.I)
_STAGE_RE = re.compile(r"\((LOI|Pre-Proposal|Pre-Application|Nomination|Concept Paper|White Paper)\)", re.I)
_NOTE_RE = re.compile(r"^\s*note\b", re.I)
_CONTACT_LINE_RE = re.compile(r"^\s*(if interested|to view more|email |for more|to learn more)", re.I)


# ---------------------------------------------------------------------------
# Config / profile
# ---------------------------------------------------------------------------
def load_config(path: Path = CONFIG_FILE) -> dict:
    """list-digests.json with defaults; `_doc`/`_example` keys stripped."""
    cfg: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            cfg = {k: v for k, v in raw.items() if not k.startswith("_")}
        except (json.JSONDecodeError, OSError) as e:
            print(f"list_digest: could not read {path}: {e}", file=sys.stderr)
    cfg.setdefault("lookback_days", DEFAULT_LOOKBACK_DAYS)
    cfg.setdefault("max_item_chars", DEFAULT_MAX_ITEM_CHARS)
    cfg.setdefault("lists", [])
    # The user's judgment lives in ~/.ea/config/preferences.json (see
    # preferences.py): rules scoped `newsletters`, `newsletters:<list>` and
    # whatever the list's own `scopes` add. A legacy `relevance` block here
    # is still converted per run by legacy_relevance_rules, with a note on
    # stderr to migrate it.
    if cfg.get("relevance"):
        print("list_digest: list-digests.json still has a `relevance` block; it is "
              "read this run, but its rules belong in preferences.json "
              "(preferences.py add ...)", file=sys.stderr)
    else:
        cfg["relevance"] = None
    for lst in cfg["lists"]:
        lst.setdefault("scopes", [])           # extra preference scopes, e.g. ["funding"]
        lst.setdefault("senders", [])
        lst.setdefault("list_address", "")
        lst.setdefault("subject_contains", "")
        lst.setdefault("drop_tags", [])
        lst.setdefault("format", "auto")       # auto | digest | newsletter
        lst.setdefault("resolve_redirects", False)
        lst.setdefault("skip_link_text", [])
        lst.setdefault("skip_sections", [])
        for k, v in LIST_PASSTHROUGH.items():
            lst.setdefault(k, v)
    return cfg


def load_profile(path: Path = PROFILE_FILE) -> dict:
    """The fields of user-profile.json the relevance judgment needs."""
    try:
        p = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    keys = ("name", "role_description", "department", "university", "bio_blurb")
    return {k: p[k] for k in keys if p.get(k)}


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------
def unwrap_url(url: str) -> str:
    """Peel Outlook safelinks (and a Gmail/Google redirect nested inside one)
    back to the address the poster wrote. parse_qs already percent-decodes
    once, which is exactly the encoding the wrapper added."""
    for _ in range(4):
        p = urlparse(url)
        host = p.netloc.lower()
        qs = parse_qs(p.query)
        if host.endswith("safelinks.protection.outlook.com") and qs.get("url"):
            url = qs["url"][0]
            continue
        if host in ("www.google.com", "google.com") and p.path == "/url" and qs.get("q"):
            url = qs["q"][0]
            continue
        break
    return url


def clean_text(text: str) -> str:
    """Normalise newlines, drop the tenant's external-mail banner, decode
    safelinks in place, collapse blank-line runs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _BANNER_RE.sub("", text)
    text = _SAFELINK_RE.sub(lambda m: unwrap_url(m.group(0)), text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_text(html: str) -> str:
    """Enough HTML-to-text for a digest that arrives as text/html: block
    tags become newlines, the rest is stripped."""
    import graph_client as graph
    html = re.sub(r"(?i)<\s*br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</\s*(p|div|li|tr|h[1-6]|blockquote|pre)\s*>", "\n", html)
    return graph._strip_html(html)


def extract_links(text: str, cap: int = MAX_LINKS_PER_ITEM) -> list[str]:
    out: list[str] = []
    for u in _URL_RE.findall(text):
        u = u.rstrip(".,;:!?*")
        if u.lower().startswith("mailto:") or u in out:
            continue
        out.append(u)
        if len(out) >= cap:
            break
    return out


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # back up to a line break so the cut does not split a URL
    nl = cut.rfind("\n")
    if nl > limit * 0.6:
        cut = cut[:nl]
    return cut.rstrip() + "\n[... truncated ...]"


def _norm(s: str) -> str:
    return " ".join((s or "").split())


# ---------------------------------------------------------------------------
# Digest parsing (plain text)
# ---------------------------------------------------------------------------
def parse_from(value: str) -> tuple[str, str]:
    """'Name <addr>' -> (name, addr); a bare address -> ('', addr)."""
    value = (value or "").strip()
    m = _FROM_RE.match(value)
    if m:
        return m.group(1).strip().strip('"'), m.group(2).strip()
    if "@" in value and " " not in value:
        return "", value
    return value, ""


def split_subject(subject: str, list_name: str = "") -> tuple[list[str], str]:
    """'[robotics-worldwide]  [Software] [Dataset] Release of X' ->
    (['software', 'dataset'], 'Release of X'). The list's own tag is not
    a category and is dropped."""
    subject = _norm(subject)
    tags = []
    for t in _TAG_RE.findall(subject):
        t_norm = _norm(t).lower()
        if list_name and list_name.lower() in t_norm:
            continue
        if t_norm and t_norm not in tags:
            tags.append(t_norm)
    title = _TAG_RE.sub("", subject)
    title = _norm(title).strip(" -:")
    return tags, title


def _tag_words(tag: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", tag.lower()) if w}


def job_reason(tags: list[str], title: str, drop_tags: list[str]) -> str | None:
    """Why an item is a job post, or None. Tag match first (the list's own
    convention), subject regex as backstop."""
    drop = DEFAULT_DROP_TAGS | {t.lower() for t in drop_tags}
    for t in tags:
        if t in drop or (_tag_words(t) & drop):
            return f"tag:{t}"
    if JOB_SUBJECT_RE.search(title or ""):
        return "subject:job"
    return None


def _parse_headers(lines: list[str], start: int) -> tuple[dict, int]:
    """RFC-822-ish headers from lines[start:] up to the first blank line.
    Continuation lines (leading whitespace) fold into the previous header.
    Returns (headers, index of first body line)."""
    headers: dict[str, str] = {}
    last = None
    i = start
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
            break
        m = _HEADER_RE.match(ln)
        if m:
            last = m.group(1).lower()
            headers[last] = m.group(2).strip()
        elif last and ln[:1] in (" ", "\t"):
            headers[last] = (headers[last] + " " + ln.strip()).strip()
        else:
            break  # body started without a blank line
        i += 1
    return headers, i


def count_toc(text: str) -> int:
    """How many items the digest's own table of contents announces."""
    n = 0
    for ln in text.split("\n"):
        if _SEP_RE.match(ln):
            break
        if _TOC_LINE_RE.match(ln):
            n += 1
    return n


def split_digest(text: str, list_name: str = "") -> list[dict]:
    """Split a cleaned digest body into items. Chunks between dash rules
    that do not open with an item head (' * N' / 'Message: N') are the
    preamble, table of contents or footer and are skipped."""
    lines = text.split("\n")
    chunks: list[list[str]] = []
    cur: list[str] = []
    for ln in lines:
        if _SEP_RE.match(ln):
            chunks.append(cur)
            cur = []
        else:
            cur.append(ln)
    chunks.append(cur)

    items: list[dict] = []
    for chunk in chunks:
        i = 0
        while i < len(chunk) and not chunk[i].strip():
            i += 1
        if i >= len(chunk):
            continue
        m = _ITEM_HEAD_RE.match(chunk[i])
        if not m:
            continue
        n = int(m.group(1) or m.group(2))
        headers, j = _parse_headers(chunk, i + 1)
        body = "\n".join(chunk[j:]).strip()
        subject = headers.get("subject", "")
        tags, title = split_subject(subject, list_name)
        from_name, from_email = parse_from(headers.get("from", ""))
        items.append({
            "n": n,
            "subject": _norm(subject),
            "title": title,
            "tags": tags,
            "from_name": from_name,
            "from_email": from_email,
            "date": headers.get("date", ""),
            "body": body,
            "links": extract_links(body),
        })
    return items


# ---------------------------------------------------------------------------
# Newsletter parsing (HTML)
# ---------------------------------------------------------------------------
def _own_text(tag) -> str:
    """Text of `tag` minus its nested lists: the item line without the
    Note that hangs under it."""
    clone = copy.copy(tag)
    for sub in clone.find_all(["ul", "ol"]):
        sub.decompose()
    return _norm(clone.get_text(" ", strip=True))


def _lead_month_date(text: str) -> str:
    """'Sept. 15, 2026 |' -> '9/15/2026'; '' when the line does not open
    with a month-name date."""
    m = _LEAD_MONTH_RE.match(text or "")
    if not m:
        return ""
    mon = _MONTHS.get(m.group(1)[:3].lower())
    if not mon:
        return ""
    day = int(m.group(2))
    if not 1 <= day <= 31:
        return ""
    return f"{mon}/{day}" + (f"/{m.group(3)}" if m.group(3) else "")


def _has_lead_date(text: str) -> bool:
    """Does the item line open with a date or a deadline word? Such a
    line is an item even when its anchor is one word or repeats the
    section heading."""
    return bool(_LEAD_DATE_RE.match(text) or _lead_month_date(text)
                or _LEAD_WORD_RE.match(text))


def _deadline_of(block_text: str) -> tuple[str, str]:
    """(deadline, stage) read off an item line. 'Due 09/09', a leading
    '09/09 :' or 'Sept. 15, 2026 |', 'register by 9/16', 'rolling',
    'anytime' or 'varies'."""
    deadline = ""
    m = _LEAD_DATE_RE.match(block_text)
    if m:
        deadline = _norm(m.group(1))
    elif _lead_month_date(block_text):
        deadline = _lead_month_date(block_text)
    else:
        d = _DUE_RE.search(block_text)
        low = block_text.lower()
        if d:
            deadline = _norm(d.group(1))
        elif _ROLLING_RE.search(block_text):
            deadline = "rolling"
        elif _VARIES_RE.search(block_text):
            deadline = "varies"
        elif _LEAD_WORD_RE.match(block_text) or _ANYTIME_RE.match(low):
            deadline = "anytime"
    sm = _STAGE_RE.search(block_text)
    return deadline, (sm.group(1) if sm else "")


_BLOCK_TAGS = ["li", "p", "h1", "h2", "h3", "h4", "div", "td"]
_BLOCK_CAP = 600  # chars: a block longer than this holds several items


def _is_item_href(href: str) -> bool:
    return not (href or "").strip().lower().startswith(
        ("mailto:", "#", "javascript:", "tel:"))


def _segment_text(a, block, cap: int = _BLOCK_CAP) -> str:
    """The text that belongs to anchor `a` inside a block holding several
    items: the short lead on its own line (a date, 'Due 10/12:'), the
    anchor text, and what follows up to the next item link or heading.

    Newsletters built in a marketing-cloud editor (a college research
    support office) put a whole section in one table cell or wrap five
    items in one <h3>; the cell's own text is then every item at once."""
    from bs4 import Comment, NavigableString, Tag
    prefix: list[str] = []
    parts: list[str] = []
    started = False
    for node in block.descendants:
        if node is a:
            started = True
            continue
        if isinstance(node, Tag):
            if not started:
                if node.name in ("br", "li", "p", "div", "tr", "h1", "h2", "h3", "h4"):
                    prefix = []
            elif node.name == "a" and _is_item_href(node.get("href", "")):
                break
            elif node.name in ("h1", "h2", "h3", "h4"):
                break
            continue
        if isinstance(node, Comment) or not isinstance(node, NavigableString):
            continue
        if node.parent is not None and node.parent.name in ("script", "style"):
            continue
        s = _norm(str(node))
        if not s:
            continue
        (parts if started else prefix).append(s)
    line = _norm(" ".join(prefix))
    lead = line
    if lead and not (_has_lead_date(lead) or _DUE_RE.search(lead) or len(lead) <= 40):
        lead = ""  # the tail of the previous item, not this one's date
    text = _norm(" ".join(([lead] if lead else []) + parts))
    return text[:cap], line


def _item_block(a) -> tuple:
    """(block, item text, line lead) for an item anchor. A list item or
    paragraph of sane size IS the item line (a bulletin); anything bigger, or a
    cell or heading, is segmented around the anchor (a newsletter). The line
    lead is whatever preceded the anchor on its line, so a link inside a
    'Note:' sentence can still be told from an item."""
    block = a.find_parent(_BLOCK_TAGS)
    if block is None:
        return None, "", ""
    if block.name in ("li", "p"):
        own = _own_text(block)
        if len(own) <= _BLOCK_CAP:
            return block, own, own
    text, line = _segment_text(a, block)
    return block, text, (line or text)


def _section_of(a) -> str:
    """The heading above an anchor, skipping headings the anchor sits
    INSIDE: a newsletter that makes each item title an <h3> link would
    otherwise be its own section and get dropped as footer navigation."""
    h = a.find_previous(["h1", "h2", "h3", "h4"])
    while h is not None:
        if h not in a.parents:
            return _norm(h.get_text(" ", strip=True))
        h = h.find_previous(["h1", "h2", "h3", "h4"])
    return ""


def parse_newsletter(html: str, lst: dict) -> list[dict]:
    """Items out of an HTML newsletter: one per content hyperlink, with
    the section heading above it, the due date and NEW flag on its line,
    and the Note under it. Navigation, tracking pixels, the sender's own
    name and contact links inside notes are not items."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "head", "title"]):
        t.decompose()
    root = soup.body or soup
    skip = (STOP_LINK_TEXT | _institution_phrases()
            | {_norm(s).lower() for s in lst.get("skip_link_text", [])})
    name = _norm(lst.get("name", "")).lower()
    skip_sections = {_norm(s).lower() for s in lst.get("skip_sections", [])}

    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    pending_new = False
    for a in root.find_all("a", href=True):
        text = _norm(a.get_text(" ", strip=True))
        href = a["href"].strip()
        low = text.lower()
        if low == "new":
            pending_new = True
            continue
        if not text or len(text) < 3 or low in skip or low == name:
            continue
        if not _is_item_href(href):
            continue
        block, block_own, line_lead = _item_block(a)
        if block is None:
            continue
        if _NOTE_RE.match(line_lead) or _CONTACT_LINE_RE.match(line_lead):
            continue  # the contact link inside a Note line
        section = _section_of(a)
        if section.lower() in skip_sections:
            continue
        # footer navigation: an anchor that repeats (part of) its own
        # section heading ("Funding" under "Funding Opportunities"), or a
        # one- or two-word anchor that IS its whole block with no date
        if section and low in section.lower() and not _has_lead_date(block_own):
            continue
        if len(text.split()) <= 2 and block_own.lower() == low and not _has_lead_date(block_own):
            continue
        key = (href, low)
        if key in seen:
            continue
        seen.add(key)
        note = ""
        for sub in block.find_all(["ul", "ol"]):
            t = _norm(sub.get_text(" ", strip=True))
            if _NOTE_RE.match(t):
                note = t
                break
        deadline, stage = _deadline_of(block_own)
        items.append({
            "n": len(items) + 1,
            "subject": text,
            "title": text,
            "tags": [],
            "section": section,
            "deadline": deadline,
            "stage": stage,
            "is_new": pending_new,
            "from_name": "",
            "from_email": "",
            "date": "",
            "body": block_own + (("\n" + note) if note else ""),
            "links": [href],
            "tracking_url": href,
        })
        pending_new = False
    return items


def resolve_redirect(url: str, hops: int = REDIRECT_HOPS,
                     timeout: int = REDIRECT_TIMEOUT_S) -> str:
    """The page behind a click-tracking link. Walks one hop at a time with
    HEAD requests, validating every hop against fetch_url's address policy
    and pinning DNS the same way, so a newsletter link cannot steer the
    preprocessor at an internal service. Returns the input on any failure;
    the caller keeps the tracking URL, which still works."""
    import requests
    import fetch_url
    import outbound_log
    current = url
    good = url  # the last hop that passed the policy and answered
    try:
        for hop in range(hops):
            try:
                info = fetch_url.validate_url(current)
            except fetch_url.BlockedURL as e:
                outbound_log.note("blocked", "list_digest", url=current, reason=str(e),
                                  method="HEAD", hop=hop)
                raise
            with fetch_url.pinned_dns(info["host"], info["addrinfo"]):
                resp = requests.head(current, headers={"User-Agent": fetch_url.USER_AGENT},
                                     timeout=timeout, allow_redirects=False)
            # Every hop goes on the reviewed outbound log (outbound_log).
            outbound_log.note("fetch", "list_digest", url=current, host=info["host"],
                              ip=info["ips"][0], method="HEAD", status=resp.status_code,
                              hop=hop)
            good = current
            loc = resp.headers.get("Location", "")
            if resp.status_code in fetch_url.REDIRECT_STATUSES and loc:
                current = urljoin(current, loc)
                continue
            return current
    except Exception as e:  # blocked, DNS, timeout: keep the last good hop
        print(f"list_digest: redirect resolve stopped for {url[:60]}...: {e}",
              file=sys.stderr)
    return good


def resolve_item_links(items: list[dict], resolver=resolve_redirect) -> None:
    """In place: items[*].links[0] becomes the real page; the tracking URL
    stays in `tracking_url`. Sequential on purpose: pinned_dns patches the
    process-wide resolver and is not thread-safe."""
    cache: dict[str, str] = {}
    for it in items:
        src = it["links"][0] if it.get("links") else ""
        if not src:
            continue
        if src not in cache:
            cache[src] = resolver(src)
        final = cache[src]
        it["links"] = [final] + [u for u in it["links"][1:] if u != final]
        it["url_host"] = urlparse(final).netloc.lower()


def triage_items(items: list[dict], drop_tags: list[str],
                 max_chars: int) -> tuple[list[dict], list[dict]]:
    """(kept, dropped). Dropped rows keep only what a tally needs."""
    kept, dropped = [], []
    for it in items:
        reason = job_reason(it["tags"], it["title"], drop_tags)
        if reason:
            dropped.append({"n": it["n"], "title": it["title"],
                            "tags": it["tags"], "reason": reason})
            continue
        row = dict(it)
        row["chars"] = len(it["body"])
        row["body"] = truncate(it["body"], max_chars)
        kept.append(row)
    return kept, dropped


# ---------------------------------------------------------------------------
# Inbox discovery
# ---------------------------------------------------------------------------
def _addr(rec: dict) -> str:
    return ((rec or {}).get("emailAddress") or {}).get("address", "").lower()


_LISTS_CACHE: tuple | None = None


def configured_lists(path: Path = CONFIG_FILE) -> list[dict]:
    """The `lists` of list-digests.json, re-read when the file changes.
    One process (a preprocess run) asks per inbox message."""
    global _LISTS_CACHE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0
    if _LISTS_CACHE is None or _LISTS_CACHE[0] != (str(path), mtime):
        _LISTS_CACHE = ((str(path), mtime), load_config(path)["lists"])
    return _LISTS_CACHE[1]


def is_list_mail(msg: dict, lists: list[dict] | None = None) -> bool:
    """Does this Graph message belong to a configured list? The triage
    detectors in preprocess.py ask this so a digest or newsletter never
    becomes a needs-response row, a dropped thread, a deadline or a
    priority alert: the list-digest agent posts and archives it."""
    return match_list(msg, configured_lists() if lists is None else lists) is not None


def match_list(msg: dict, lists: list[dict]) -> dict | None:
    """The configured list a Graph message belongs to, or None. A message
    matches when its sender is one of the list's `senders` OR the list
    address is among its recipients (covers both digest mode, sent by the
    -request robot, and individual-post mode, sent by the poster). An
    optional `subject_contains` narrows either path."""
    sender = _addr(msg.get("from"))
    recips = {_addr(r) for r in (msg.get("toRecipients") or [])}
    recips |= {_addr(r) for r in (msg.get("ccRecipients") or [])}
    subject = (msg.get("subject") or "").lower()
    for lst in lists:
        senders = {s.lower() for s in lst.get("senders", [])}
        la = (lst.get("list_address") or "").lower()
        hit = (sender and sender in senders) or (la and la in recips)
        if not hit:
            continue
        sc = (lst.get("subject_contains") or "").lower()
        if sc and sc not in subject:
            continue
        return lst
    return None


def _body_parts(full: dict) -> tuple[str, bool]:
    body = full.get("body") or {}
    content = body.get("content") or ""
    return content, (body.get("contentType") or "").lower() == "html"


def _message_text(full: dict) -> str:
    content, is_html = _body_parts(full)
    if is_html:
        content = html_to_text(content)
    return clean_text(content)


def build_digest_entry(msg: dict, lst: dict, full: dict, max_chars: int,
                       resolver=resolve_redirect, legacy_relevance: dict | None = None,
                       prefs_data: dict | None = None) -> dict:
    """One inbox message -> one entry for the JSON. A plain-text digest
    splits into items; an HTML newsletter yields one item per content
    link; a message that does neither (individual-post mode, or a format
    this parser does not know) becomes a single item, with the raw text
    attached when even that looks wrong."""
    list_name = lst.get("name", "")
    fmt = lst.get("format", "auto")
    content, is_html = _body_parts(full)
    text = _message_text(full)
    warnings: list[str] = []
    items: list[dict] = []
    kind = "digest"

    if fmt in ("auto", "digest"):
        items = split_digest(text, list_name)
    toc = count_toc(text)
    if items:
        if toc and toc != len(items):
            warnings.append(f"table of contents lists {toc} items, parser found {len(items)}")
    elif is_html and fmt in ("auto", "newsletter"):
        try:
            items = parse_newsletter(content, lst)
        except Exception as e:  # a parser bug must not sink the run
            warnings.append(f"newsletter parse failed: {e}")
        if items:
            kind = "newsletter"
            if lst.get("resolve_redirects"):
                resolve_item_links(items, resolver)
        elif fmt == "newsletter":
            warnings.append("newsletter format requested but no content links found")

    if not items:
        kind = "post"
        tags, title = split_subject(msg.get("subject") or "", list_name)
        from_name = ((msg.get("from") or {}).get("emailAddress") or {}).get("name", "")
        items = [{
            "n": 1,
            "subject": _norm(msg.get("subject") or ""),
            "title": title,
            "tags": tags,
            "from_name": from_name or "",
            "from_email": _addr(msg.get("from")),
            "date": msg.get("receivedDateTime", ""),
            "body": text,
            "links": extract_links(text),
        }]
        if toc:
            # It announced a table of contents but nothing split: a format
            # change. Hand Claude the raw text so the run still judges it.
            kind = "unparsed"
            warnings.append(f"digest format not recognised (toc={toc}); raw_text attached")

    kept, dropped = triage_items(items, lst.get("drop_tags", []), max_chars)
    entry = {
        "list": list_name,
        "kind": kind,
        "message_id": msg.get("id"),
        "conversation_id": msg.get("conversationId"),
        "subject": msg.get("subject"),
        "from": _addr(msg.get("from")),
        "from_name": ((msg.get("from") or {}).get("emailAddress") or {}).get("name", ""),
        "received": msg.get("receivedDateTime"),
        "items_total": len(items),
        "items": kept,
        "dropped": dropped,
        "parse_warnings": warnings,
    }
    for k, default in LIST_PASSTHROUGH.items():
        entry[k] = lst.get(k, default)
    # The user's judgment for this list, in their words (preferences.py).
    entry["rules"] = rules_for_list(lst, legacy_relevance, prefs_data)
    if kind == "unparsed":
        entry["raw_text"] = truncate(text, RAW_FALLBACK_CHARS)
    return entry


def rule_scopes(lst: dict) -> list[str]:
    """The preference scopes whose rules apply to one list."""
    name = (lst.get("name") or "").strip().lower()
    scopes = ["newsletters"]
    if name:
        scopes.append(f"newsletters:{name}")
    for s in lst.get("scopes", []) or []:
        s = str(s).strip().lower()
        if s and s not in scopes:
            scopes.append(s)
    return scopes


def legacy_relevance_rules(rel: dict, list_name: str) -> dict:
    """A legacy list-digests.json `relevance` block, in the shape
    preferences.select() returns, so an unmigrated config keeps working.
    `instructions` rows carry `list` (`*` or a list name); the others are
    plain strings."""
    out = {"scopes": [], "instructions": [], "prefer": [], "avoid": [], "facts": [],
           "calibration": []}
    if not isinstance(rel, dict):
        return out
    for ins in rel.get("instructions", []) or []:
        if not isinstance(ins, dict):
            continue
        target = (ins.get("list") or "*").lower()
        if target in ("*", list_name.lower()):
            out["instructions"].append({"id": "legacy", "text": ins.get("text", ""),
                                        "quote": ins.get("text", ""), "date": ins.get("date", ""),
                                        "scopes": [f"newsletters:{target}" if target != "*" else "newsletters"]})
    for t in rel.get("always_relevant", []) or []:
        out["prefer"].append({"id": "legacy", "text": str(t), "date": "", "scopes": ["newsletters"]})
    for t in rel.get("never_relevant", []) or []:
        out["avoid"].append({"id": "legacy", "text": str(t), "date": "", "scopes": ["newsletters"]})
    for key, label in (("interests", "Research interests"), ("eligibility", "Eligibility")):
        if rel.get(key):
            out["facts"].append({"id": "legacy", "text": f"{label}: {rel[key]}", "date": "",
                                 "scopes": ["newsletters"]})
    for c in rel.get("calibration", []) or []:
        out["calibration"].append({"date": "", "scope": "newsletters", "note": str(c)})
    return out


def merge_rules(a: dict, b: dict) -> dict:
    out = {k: list(a.get(k, [])) for k in ("instructions", "prefer", "avoid", "facts", "calibration")}
    out["scopes"] = list(a.get("scopes", []))
    for k in ("instructions", "prefer", "avoid", "facts", "calibration"):
        out[k] += list(b.get(k, []))
    return out


def rules_for_list(lst: dict, legacy_relevance: dict | None = None,
                   prefs_data: dict | None = None) -> dict:
    """The user's rules for one list: the preference store's selection for
    the list's scopes, plus whatever a legacy `relevance` block adds."""
    scopes = rule_scopes(lst)
    rules = _prefs.select(scopes, prefs_data)
    if legacy_relevance:
        rules = merge_rules(rules, legacy_relevance_rules(legacy_relevance, lst.get("name", "")))
        rules["scopes"] = scopes
    return rules


def collect(cfg: dict | None = None, inbox: list | None = None,
            fetch_full=None, resolver=resolve_redirect,
            prefs_data: dict | None = None) -> dict:
    """The preprocess mode. `inbox`, `fetch_full` and `resolver` are
    injectable so the tests never touch Graph or the network."""
    cfg = cfg or load_config()
    lists = cfg["lists"]
    out = {
        "mode": "list-digest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": cfg["lookback_days"],
        "lists_configured": [l.get("name") for l in lists],
        "digests": [],
        "profile": load_profile(),
        "errors": [],
    }
    if not lists:
        out["errors"].append("no lists configured in list-digests.json")
        return out

    if inbox is None or fetch_full is None:
        import graph_client as graph
        if inbox is None:
            inbox = graph.get_messages("inbox", days_back=cfg["lookback_days"])
        if fetch_full is None:
            fetch_full = graph.get_message_full

    matched = [(m, lst) for m in inbox if (lst := match_list(m, lists))]
    matched.sort(key=lambda p: p[0].get("receivedDateTime", ""))
    for msg, lst in matched:
        try:
            full = fetch_full(msg["id"])
        except Exception as e:  # one bad fetch must not sink the run
            out["errors"].append(f"{msg.get('subject')!r}: fetch failed: {e}")
            continue
        out["digests"].append(
            build_digest_entry(msg, lst, full, cfg["max_item_chars"], resolver,
                               legacy_relevance=cfg.get("relevance"), prefs_data=prefs_data))
    return out


# ---------------------------------------------------------------------------
# Archive (called by run-agent.sh AFTER the Slack post)
# ---------------------------------------------------------------------------
def _append_history(rec: dict) -> None:
    """One history line. `archive` runs as the owner (run-agent.sh) and the
    history file is resident space, so the append is made with the
    resident's access (resident_io.append_text, which refuses a link at the
    name and creates the file and directory as the resident)."""
    _rio.append_text(HISTORY_FILE, json.dumps(rec) + "\n")


def archive(data: dict, disposition: str, mover=None) -> dict:
    """Move every digest in `data` to Archive and log one history line per
    digest. Returns a summary; `failed` > 0 means at least one stayed."""
    if mover is None:
        import graph_client as graph

        def mover(mid: str) -> dict:
            return graph.graph_post(f"/me/messages/{mid}/move",
                                    {"destinationId": "archive"})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows, failed = [], 0
    for d in data.get("digests", []):
        mid = d.get("message_id")
        ok, err = True, None
        try:
            mover(mid)
        except Exception as e:
            ok, err = False, str(e)[:300]
            failed += 1
        rec = {
            "ts": now,
            "list": d.get("list"),
            "kind": d.get("kind"),
            "subject": d.get("subject"),
            "message_id": mid,
            "items_total": d.get("items_total"),
            "items_kept": len(d.get("items", [])),
            "items_dropped": len(d.get("dropped", [])),
            "disposition": disposition,
            "archived": ok,
            "error": err,
        }
        _append_history(rec)
        rows.append(rec)
    return {"archived": len(rows) - failed, "failed": failed,
            "disposition": disposition, "digests": rows}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_collect(args) -> int:
    result = collect()
    text = json.dumps(result, indent=2)
    if args.output:
        _atomic.write_text(args.output, text, mode=0o664)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


def _cmd_parse(args) -> int:
    cfg = load_config()
    lst = next((l for l in cfg["lists"] if l.get("name") == args.list_name), None)
    if lst is None:
        lst = dict(LIST_PASSTHROUGH, name=args.list_name or "", drop_tags=[],
                   format="auto", resolve_redirects=False, skip_link_text=[],
                   skip_sections=[], scopes=[])
    raw = Path(args.file).read_text(errors="replace")
    is_html = args.file.lower().endswith((".html", ".htm")) or "<html" in raw[:2000].lower()
    full = {"body": {"contentType": "html" if is_html else "text", "content": raw}}
    msg = {"id": "offline", "subject": args.file, "from": {"emailAddress": {"address": ""}}}
    entry = build_digest_entry(msg, lst, full, cfg["max_item_chars"],
                               resolver=(resolve_redirect if args.resolve else (lambda u: u)),
                               legacy_relevance=cfg.get("relevance"))
    print(json.dumps(entry, indent=2))
    return 0


def _cmd_archive(args) -> int:
    data = json.loads(Path(args.input).read_text())
    summary = archive(data, args.disposition)
    print(json.dumps(summary))
    return 1 if summary["failed"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Mailing-list digest triage (code half)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("collect", help="find digests in the inbox, write the agent's JSON")
    p.add_argument("--output")
    p = sub.add_parser("parse", help="split a saved body (offline)")
    p.add_argument("--file", required=True)
    p.add_argument("--list-name", default="")
    p.add_argument("--resolve", action="store_true",
                   help="resolve tracking redirects (network)")
    p = sub.add_parser("archive", help="archive every digest in a collect JSON")
    p.add_argument("--input", required=True)
    p.add_argument("--disposition", choices=("posted", "silent"), required=True)
    args = ap.parse_args(argv)
    return {"collect": _cmd_collect, "parse": _cmd_parse,
            "archive": _cmd_archive}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
