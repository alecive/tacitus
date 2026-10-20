"""Code reads behind a room reply's status claims (the reply check's first try).

The reply check (ea-chat-bot.py) flags a sentence that states a ledger
row's status, or whether a thread was answered, when the turn made no read
behind it. This module tries to settle each flagged sentence with one read
in code: the ledger row (ledger.get_entry) or the thread
(graph_actions.py get-conversation). A claim holds when the read agrees.
Anything this module cannot settle, and every contradiction, goes to
Claude's second pass as before; a negative claim ("there is no X") always
does, because it needs a search, not a read.

    settle(check, reply, tool_text, get_entry=..., read_thread=..., ...)
      -> {"settled": bool, "results": [...], "reads": {"ledger": n, "mail": n, "web": 0}}

A result is {"kind": "ledger"|"mail", "claim": sentence, "ref": id,
"outcome": "held"|"contradicted"|"unresolved", "why": text}.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Callable

# Status words, split by what they assert about the row.
_DONE_RE = re.compile(
    r"\balready (?:submitted|done|handled|filed|registered|paid)\b"
    r"|\b(?:is|was|are|were|remains) closed\b|\bclosed (?:on|as|it)\b|\bmarked (?:done|closed)\b",
    re.I)
_OPEN_RE = re.compile(
    r"\bnot yet (?:submitted|in|filed|done|paid|sent)\b|\bstill (?:open|pending|due|outstanding)\b"
    r"|\b(?:is|was|remains) (?:still )?open\b",
    re.I)
_DUE_RE = re.compile(
    r"\b(?:is|was|are|were|remains) (?:due|overdue)\b|\bdeadline (?:is|was)\b|\bdue (?:on |by )?[a-z]+\.? ?\d"
    r"|\bdue (?:today|tomorrow)\b",
    re.I)
_OVERDUE_RE = re.compile(r"\boverdue\b", re.I)
_WEEKDAY_RE = re.compile(r"\b(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?\b", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH_DAY_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?\b(?:,? (\d{4})\b)?",
    re.I)
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# A month/day date: month 1-12, day 1-31, not inside a longer slash run
# ("1/2/3/4"), and not a fraction or ratio ("2/3 of the budget", "a 3/4
# majority", "rated 4/5").
_SLASH_RE = re.compile(
    r"(?<![\d/.])(?<!ratio )(?<!split )(?<!score )(?<!rated )(?<!odds )(?<!ratio of )"
    r"(1[0-2]|0?[1-9])/(3[01]|[12]\d|0?[1-9])(?:/(\d{4}|\d{2}))?(?![\d/])"
    r"(?!\s+(?:of|majority|split|ratio|vote|votes|share|chance|stars|points|"
    r"complete|done|finished|full|time|speed)\b)",
    re.I)
_TODAY_RE = re.compile(r"\btoday\b", re.I)
_TOMORROW_RE = re.compile(r"\btomorrow\b", re.I)

# Reply claims, split by who the sentence says owes the next message.
_USER_OWES_RE = re.compile(r"\byou (?:still )?owe\b", re.I)
# The user as the subject of a send or reply verb that is about a thread:
# "you already replied to Dana", "you sent him the reply". "You" opens its
# clause, so "the file you sent" and "the question you answered" are prose,
# and "sent" counts only with a reply as its object.
_USER_REPLIED_RE = re.compile(
    r"(?:^|[.;:!?,(]\s*|\b(?:and|but|so|yes|then|because|since)\s+)"
    r"you(?:'ve|\s+have)?(?:\s+already)?\s+"
    r"(?:(?:replied|responded|answered|wrote\s+back|written\s+back)"
    r"(?=\s*(?:$|[.,;!?)]|\s(?:to|on|in|him|her|them|already|yesterday|today|last)\b|\s(?-i:[A-Z])))"
    r"|sent\s+(?:(?:him|her|them|(?-i:[A-Z][\w'-]+))\s+)?(?:(?:a|an|the|your)\s+)?"
    r"(?:reply|response|answer|follow-?up)\b)",
    re.I)
_THEY_OWE_RE = re.compile(
    r"\bhas(?:n'?t| not) (?:replied|responded|answered|written back)\b"
    r"|\bno (?:reply|answer|response) (?:yet|so far|to|from|in)\b"
    r"|\bstill (?:waiting|unanswered|silent)\b"
    r"|\bawaiting (?:a |his |her |their )?(?:reply|answer|response)\b|\bwent unanswered\b",
    re.I)

# A ledger id is lowercase words joined by hyphens or underscores; only
# tokens the ledger knows count.
_ID_TOKEN_RE = re.compile(r"(?<![\w.-])([a-z0-9][a-z0-9_]*(?:[-_][a-z0-9_]+){1,12})(?![\w-])", re.I)
_CONV_ARG_RE = re.compile(r"--conversation-id[ =]['\"]?([A-Za-z0-9+/=_-]{16,})")
_DONE_STATUSES = {"completed", "closed", "done"}


def ledger_kind(sentence: str) -> str:
    """'done', 'open', 'due' or '' for a ledger-status sentence."""
    if _DONE_RE.search(sentence):
        return "done"
    if _OPEN_RE.search(sentence):
        return "open"
    if _DUE_RE.search(sentence):
        return "due"
    return ""


def mail_kind(sentence: str) -> str:
    """Who the sentence says owes the next message: 'user' (the user owes a
    reply), 'they' (the other side has not answered the user), 'user-sent'
    (the user already replied), or '' when it says neither."""
    if _USER_OWES_RE.search(sentence):
        return "user"
    if _USER_REPLIED_RE.search(sentence):
        return "user-sent"
    if _THEY_OWE_RE.search(sentence):
        return "they"
    return ""


def _full_date(year: str, month: int, day: int, today: date) -> date | None:
    """The calendar date a sentence names. A date with no year is the
    occurrence nearest `today`; an impossible date is None."""
    if year:
        y = int(year)
        y = y + 2000 if y < 100 else y
        try:
            return date(y, month, day)
        except ValueError:
            return None
    found: list[date] = []
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            found.append(date(y, month, day))
        except ValueError:
            continue
    return min(found, key=lambda d: abs((d - today).days)) if found else None


def _dates_in(sentence: str, today: date) -> list[date]:
    """Every concrete date the sentence names, with the year resolved;
    'today' and 'tomorrow' resolve against `today`."""
    out = [_full_date(y, int(mo), int(d), today) for y, mo, d in _ISO_RE.findall(sentence)]
    out += [_full_date(y, _MONTHS[m.lower()[:3]], int(d), today)
            for m, d, y in _MONTH_DAY_RE.findall(sentence)]
    out += [_full_date(y, int(mo), int(d), today) for mo, d, y in _SLASH_RE.findall(sentence)
            if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31]
    if _TODAY_RE.search(sentence):
        out.append(today)
    if _TOMORROW_RE.search(sentence):
        out.append(today + timedelta(days=1))
    return [d for d in out if d is not None]


def _deadline(row: dict) -> date | None:
    raw = str(row.get("deadline") or "").strip()[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def check_ledger(sentence: str, row: dict, today: date) -> tuple[str, str]:
    """(outcome, why) for one ledger-status sentence against its row."""
    kind = ledger_kind(sentence)
    status = str(row.get("status") or "").lower()
    done = status in _DONE_STATUSES
    if kind == "done":
        return ("held", f"row is {status}") if done else ("contradicted", f"row is {status}")
    if kind == "open":
        return ("contradicted", f"row is {status}") if done else ("held", f"row is {status}")
    if kind == "due":
        dl = _deadline(row)
        if dl is None:
            return "unresolved", "row has no deadline"
        if done:
            return "contradicted", f"row is {status}"
        if _OVERDUE_RE.search(sentence):
            return ("held", f"deadline {dl} passed") if dl < today else ("contradicted", f"deadline {dl}")
        named = _dates_in(sentence, today)
        if named:
            return (("held", f"deadline {dl}") if dl in named
                    else ("contradicted", f"deadline {dl}"))
        if _WEEKDAY_RE.search(sentence):
            return "unresolved", "a weekday with no date"
        return "unresolved", "no date named"
    return "unresolved", "no status word this module reads"


def check_thread(sentence: str, messages: list[dict], user_email: str) -> tuple[str, str]:
    """(outcome, why) for one reply claim against the thread's messages
    (oldest first, as get-conversation prints them)."""
    kind = mail_kind(sentence)
    if not kind:
        return "unresolved", "no reply claim this module reads"
    sent = [m for m in messages or [] if not m.get("is_draft")]
    if not sent:
        return "unresolved", "no messages read"
    last = sent[-1]
    who = str((last.get("from") or {}).get("address") or "").lower()
    own = bool(last.get("own")) or (bool(user_email) and who == user_email.lower())
    if kind == "user":
        return ("held", "the latest message is inbound") if not own else (
            "contradicted", "the user sent the latest message")
    return ("held", "the user sent the latest message") if own else (
        "contradicted", "the latest message is inbound")


def ledger_ids(text: str, known: Callable[[list[str]], set[str]], cap: int = 60) -> list[str]:
    """Ledger ids in `text`, in order, as `known` (a batch existence check)
    confirms them."""
    toks: list[str] = []
    for t in _ID_TOKEN_RE.findall(text or ""):
        if t not in toks:
            toks.append(t)
        if len(toks) >= cap:
            break
    if not toks:
        return []
    have = known(toks)
    return [t for t in toks if t in have]


def ledger_known(ids: list[str], db_path=None) -> set[str]:
    """The ids among `ids` that are ledger rows (one read-only query)."""
    try:
        import ledger  # noqa: PLC0415
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import ledger  # noqa: PLC0415
    if not ids:
        return set()
    conn = ledger.guarded_connect(db_path or ledger.DB_PATH, read_only=True)
    try:
        marks = ",".join("?" * len(ids))
        return {r[0] for r in conn.execute(f"SELECT id FROM ledger WHERE id IN ({marks})", ids)}
    finally:
        conn.close()


def ledger_row(entry_id: str, db_path=None) -> dict | None:
    try:
        import ledger  # noqa: PLC0415
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import ledger  # noqa: PLC0415
    return ledger.get_entry(entry_id, db_path)


def settle(check: dict, reply: str, tool_text: str, *,
           known: Callable[[list[str]], set[str]],
           get_entry: Callable[[str], dict | None],
           read_thread: Callable[[str], list[dict] | None],
           user_email: str = "", today: date | None = None) -> dict:
    """Try to settle every flagged ledger and mail claim with a code read."""
    today = today or date.today()
    results: list[dict] = []
    reads = {"ledger": 0, "mail": 0, "web": 0}
    if check.get("negative"):
        return {"settled": False, "results": results, "reads": reads,
                "why": "negative claims need a search"}
    candidates = ledger_ids(reply + "\n" + (tool_text or ""), known)
    rows: dict[str, dict | None] = {}

    def _row(i: str) -> dict | None:
        if i not in rows:
            rows[i] = get_entry(i)
            reads["ledger"] += 1
        return rows[i]

    def _ref_for(sentence: str) -> str:
        """The one ledger id the sentence names itself; an id elsewhere in
        the reply or the turn's commands never stands in for it."""
        own_ids = [i for i in candidates if i in sentence]
        return own_ids[0] if len(own_ids) == 1 else ""

    for s in check.get("ledger") or []:
        ref = _ref_for(s)
        row = _row(ref) if ref else None
        if not row:
            results.append({"kind": "ledger", "claim": s, "ref": ref,
                            "outcome": "unresolved", "why": "no single ledger row named"})
            continue
        outcome, why = check_ledger(s, row, today)
        results.append({"kind": "ledger", "claim": s, "ref": ref, "outcome": outcome, "why": why})

    threads: dict[str, list[dict] | None] = {}
    mail_claims = check.get("mail") or []
    convs_from_tools = _CONV_ARG_RE.findall(tool_text or "")
    for s in mail_claims:
        ref = _ref_for(s)
        conv = str((_row(ref) or {}).get("conversation_id") or "") if ref else ""
        if not conv:
            # Only a conversation id written in the sentence itself.
            own = {c for c in convs_from_tools if c and c in s}
            conv = next(iter(own)) if len(own) == 1 else ""
        if not conv:
            results.append({"kind": "mail", "claim": s, "ref": "",
                            "outcome": "unresolved", "why": "no single thread named"})
            continue
        if conv not in threads:
            threads[conv] = read_thread(conv)
            reads["mail"] += 1
        msgs = threads[conv]
        if msgs is None:
            results.append({"kind": "mail", "claim": s, "ref": conv,
                            "outcome": "unresolved", "why": "thread read failed"})
            continue
        outcome, why = check_thread(s, msgs, user_email)
        results.append({"kind": "mail", "claim": s, "ref": conv, "outcome": outcome, "why": why})

    settled = bool(results) and all(r["outcome"] == "held" for r in results)
    return {"settled": settled, "results": results, "reads": reads}
