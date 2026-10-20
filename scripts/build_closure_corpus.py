#!/usr/bin/env python3
"""Build the labeled closure regression corpus from live production history.

A scrubber change measured on false positives alone cannot tell a
precision fix from a recall regression: a build that closed NOTHING would
score perfectly. This script turns the production audit trail into a
corpus that scores both directions; `preprocessor/closure_harness.py`
runs any scrubber build against it, and `tests/test_closure_recall.py`
holds the floor.

READ-ONLY on production. It reads live EA state and the mailbox and writes
exactly one file, the owner-private real corpus
(closure_harness.default_real_corpus_path(): closure_corpus.real.json in
EA_PRIVATE_DIR, default ~/.ea-private), mode 0600. It never writes to
~/.ea, and the output is never committed: the corpus in the repository,
tests/fixtures/closure_corpus.json, is invented data written by
tests/fixtures/closure_corpus_synthetic.py. The recall test scores the
real corpus too whenever closure_harness.real_corpus_path() finds it.

Sources, all ground truth from what actually happened:
  ~/.ea/logs/closure-scrubber.log           every auto-close the scrubber made
  ~/.ea/state/closure-scrubber-vetoes.json  auto-closes a human reversed
  ~/.ea/logs/missed-closures.log            closures the user had to state
  ~/.ea/state/obligations-ledger.db         entry text + REOPENED narration
  Graph (read-only)                         the sent/inbox/calendar artifacts

Labels:
  must_not_close  a human reversed this close, or an audit judged it wrong
  should_close    the obligation really was finished and evidence existed
  (a third bucket, `unactionable`, is recorded but NOT scored: the user
   closed it and no email or calendar artifact exists that any signal
   could have seen. Counting those as recall misses would blame the
   scrubber for closures made in a hallway.)

Cohorts split the recall story, because the two halves need different fixes:
  email_linked  the entry carries a conversation_id, so a signal can reach it
  id_orphan     the entry carries neither conversation_id nor
                source_message_id, so NO email signal can ever fire on it.
                Each id_orphan case gets an `id_repaired` twin carrying the
                ids the entry should have captured. The gap between the two
                is the id-capture defect, measured rather than asserted.

Usage (needs read access to ~/.ea and a Graph token, i.e. run as the owner).
Rebuilding is TWO steps: the second one annotates each case with whether
any scrubber build has ever closed it, and the test wall reads that flag.

    python3 scripts/build_closure_corpus.py --scrub ~/.ea-private/closure-corpus-scrub.json
    python3 preprocessor/closure_harness.py audit --real --write \
        --rev a96d962 --rev 9120f65 --rev bb95ac1 --rev 9ca265c \
        --rev 9c50a5b --rev 7e0601f --rev b1fe734

    python3 scripts/build_closure_corpus.py --offline --snapshot-dir /tmp/snap \
        --scrub ~/.ea-private/closure-corpus-scrub.json

The sources hold real names and addresses, so even the private corpus is
written through a scrub: `--scrub <map.json>` applies a
map of exact-string replacements (real name or address to stand-in; the
map stays outside the repo, tests/fixtures/closure_corpus_scrub.example.json
shows the shape) to every string in the document, then turns any
remaining address at the owner's domains into <local>@example.edu and
any other non-example address into <local>@example.com. The script
refuses to write a corpus in which the owner's name, address or domains
from the live profile still appear.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import closure_harness as ch  # noqa: E402  (where the real corpus lives)
import closure_metrics as cm  # noqa: E402  (signal_of, reused not reimplemented)

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
SCRUBBER_LOG = EA_DIR / "logs" / "closure-scrubber.log"
MISSED_LOG = EA_DIR / "logs" / "missed-closures.log"
VETO_STATE = EA_DIR / "state" / "closure-scrubber-vetoes.json"
LEDGER_DB = EA_DIR / "state" / "obligations-ledger.db"

# Entry ids the test wall once wrote into the PRODUCTION audit log. They
# are not ledger rows and must never become corpus cases.
FIXTURE_IDS = {"e-outlook-reply", "e-gmail-reply", "e-aicb", "e-surv"}

# Scrubber audit chatter. Stripped from entry text so a false-positive
# case tests the MATCHING logic and not the VetoStore, which reads exactly
# this text. The historical false closes happened while the entry was
# still clean, so the cleaned text is the faithful one. Sentence-level,
# because the scrubber and the briefing both write whole sentences into
# `notes`.
_CHATTER_WORDS = ("closure_scrubber", "scrubber", "reopen", "false-clos",
                  "false closure", "auto-clos", "calendar_event_match",
                  "briefing 2026")


def strip_chatter(text: str) -> str:
    keep = [s for s in re.split(r"(?<=[.!?])\s+", text or "")
            if not any(w in s.lower() for w in _CHATTER_WORDS)]
    return " ".join(keep).strip()

# Fields of a ledger row any closure signal can read. Everything else is
# dropped, so the fixture carries no more of the user's data than the test
# needs.
ENTRY_FIELDS = ("id", "type", "description", "from_email", "from_name",
                "conversation_id", "source_message_id", "gmail_thread_id",
                "date_identified", "deadline", "notes", "last_action",
                "priority")

NOTES_CAP = 400        # _entry_match_tokens reads notes[:400]
BALLAST_NOTES_CAP = 200
PREVIEW_CAP = 400      # bodyPreview is only ever tokenised, never displayed

# The institution's external-sender banner, verbatim from the ledger notes of the
# entries a confirmation cluster closed. Its vocabulary IS the defect: it
# sits in both the entry notes and the inbound preview, so two unrelated
# texts share ['caution', 'email', 'external'].
BANNER = ("[External email - use caution] CAUTION: This email originated "
          "from outside of the organization. Do not click links or open "
          "attachments unless you recognize the sender and know the "
          "content is safe.")

# Banner + triage vocabulary. A confirmation_email closure whose whole
# overlap sits in here is a banner match, which the scrubber's banner
# stripping exists to kill. Used to label, not to score.
_BANNER_VOCAB = {"caution", "external", "email", "sender", "originated",
                 "outside", "link", "links", "click", "clicking",
                 "attachments", "unless", "recognize", "content", "safe",
                 "note", "message", "response", "reply"}

# An inbound_ack closure is ground-truth evidence that the obligation was
# DISCHARGED only when its label says the counterparty received, recorded
# or will act on the thing. The rest ("out of office", "all set", a bare
# "thanks") say something about the conversation and nothing about the
# obligation, and the scrubber deliberately changed its answer on several
# of them. Scoring a scrubber on a case whose right answer is contested
# would make the harness lie, so those go in `contested`: recorded, not
# scored.
COMPLETION_ACK_LABELS = {"submission-ack", "receipt-ack", "review-ack",
                         "portal-receipt-ack", "registration-ack",
                         "submission-success-ack", "confirmation-ack"}


# ── sanitisation ───────────────────────────────────────────────────────

class Pseudonymiser:
    """Opaque Outlook ids become stable short handles.

    Every signal tests ids for equality only, so a handle preserves the
    behaviour under test while keeping 152-char mailbox-scoped identifiers
    out of the repo."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def conv(self, cid: str) -> str:
        return self._get(cid, "conv")

    def msg(self, mid: str) -> str:
        return self._get(mid, "msg")

    def _get(self, raw: str, kind: str) -> str:
        if not raw:
            return ""
        if raw not in self._map:
            self._map[raw] = f"{kind}-{len(self._map) + 1:03d}"
        return self._map[raw]


def _addr(obj: dict) -> dict:
    ea = (obj or {}).get("emailAddress") or {}
    return {"name": ea.get("name") or "", "address": ea.get("address") or ""}


def sanitise_message(m: dict, pseudo: Pseudonymiser) -> dict:
    """Keep only the fields a closure signal reads."""
    out = {
        "conversationId": pseudo.conv(m.get("conversationId") or ""),
        "subject": m.get("subject") or "",
        "bodyPreview": (m.get("bodyPreview") or "")[:PREVIEW_CAP],
        "from": {"emailAddress": _addr(m.get("from"))},
    }
    for k in ("receivedDateTime", "sentDateTime"):
        if m.get(k):
            out[k] = m[k]
    return out


def sanitise_event(ev: dict) -> dict:
    """Keep subject, start, organizer and attendee identity: the token bag
    `_prepare_events` builds, and nothing else."""
    return {
        "subject": ev.get("subject") or "",
        "start": {"dateTime": ((ev.get("start") or {}).get("dateTime") or ""),
                  "timeZone": "UTC"},
        "isAllDay": bool(ev.get("isAllDay")),
        "showAs": ev.get("showAs") or "",
        "organizer": {"emailAddress": _addr(ev.get("organizer"))},
        "attendees": [{"emailAddress": _addr(a)}
                      for a in (ev.get("attendees") or [])],
    }


def sanitise_entry(row: dict, pseudo: Pseudonymiser, *, clean: bool,
                   notes_cap: int = NOTES_CAP) -> dict:
    out: dict = {}
    for f in ENTRY_FIELDS:
        out[f] = row.get(f) or ""
    out["status"] = "open"
    out["conversation_id"] = pseudo.conv(out["conversation_id"])
    out["source_message_id"] = pseudo.msg(out["source_message_id"])
    out["notes"] = (out["notes"] or "")[:notes_cap]
    out["last_action"] = (out["last_action"] or "")[:NOTES_CAP]
    if clean:
        out["notes"] = strip_chatter(out["notes"])
        # last_action feeds ONLY `_history_has_reopen_veto`; it
        # contributes no match tokens. Blanking it is what a fresh entry
        # looks like, and it keeps the veto path out of the token tests.
        out["last_action"] = ""
    return out


# ── source readers ─────────────────────────────────────────────────────

def read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def load_ledger(db: Path) -> dict[str, dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = {r["id"]: dict(r) for r in con.execute("SELECT * FROM ledger")}
    con.close()
    return rows


def open_entries(db: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM ledger WHERE status IN ('open', 'follow_up') "
        "AND (source IS NULL OR source NOT LIKE 'plugin:%')")]
    con.close()
    return rows


def parse_reason(reason: str) -> dict:
    """subject / from / overlap / sent out of any scrubber reason string."""
    out = {"subject": "", "from": "", "overlap": [], "sent": ""}
    m = re.search(r"subject=('[^']*'|\"[^\"]*\")", reason or "")
    if m:
        out["subject"] = m.group(1).strip("'\"")
    m = re.search(r"from=([^,)\s]+)", reason or "")
    if m:
        out["from"] = m.group(1)
    m = re.search(r"overlap=(\[[^\]]*\])", reason or "")
    if m:
        try:
            out["overlap"] = [str(t) for t in ast.literal_eval(m.group(1))]
        except Exception:
            pass
    m = re.search(r"sent=([0-9T:\-]+Z)", reason or "")
    if m:
        out["sent"] = m.group(1)
    return out


def load_snapshot(snap: Path, name: str) -> list:
    p = snap / name
    return json.loads(p.read_text()) if p.exists() else []


def fetch_snapshot(snap: Path) -> None:
    """One read-only Graph pass. Skipped when the snapshot already exists."""
    snap.mkdir(parents=True, exist_ok=True)
    if (snap / "sent.json").exists():
        return
    import graph_client as g
    (snap / "sent.json").write_text(json.dumps(g.get_sent(days_back=100)))
    (snap / "inbox.json").write_text(json.dumps(g.get_entire_inbox()))
    start = datetime.now(timezone.utc) - timedelta(days=100)
    end = datetime.now(timezone.utc) + timedelta(days=50)
    (snap / "calendar.json").write_text(
        json.dumps(g.get_calendar_view(start=start, end=end)))


# ── world reconstruction ───────────────────────────────────────────────

def _forward_start(ts: str) -> str:
    """A start time one day after `ts`. A reconstructed event has to be
    forward-looking relative to the entry or the signal skips it."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return "2026-12-31T00:00:00.0000000"
    return (dt + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.0000000")


def find_event(events: list, subject: str, not_before: str = "") -> dict | None:
    """Recover the real event the log named.

    The reason string carries subject[:60], so an exact hit is checked
    first and a 60-char prefix second. Prefix matching alone is unsafe:
    'RF Team Weekly' is a prefix of 'RF Team Weekly [note]', and the
    `[note]` suffix is exactly what the calendar signal keys on, so a
    sloppy match would build the case around a different event with
    different rules.
    Occurrences starting before the entry existed are rejected because the
    signal rejects them too, and among the rest the largest attendee
    roster wins: the recorded overlap usually came off that roster."""
    if not subject:
        return None
    key = subject.strip().lower()
    exact, prefix = [], []
    for ev in events:
        s = (ev.get("subject") or "").strip().lower()
        if not s:
            continue
        if s == key:
            exact.append(ev)
        elif len(key) >= 55 and s.startswith(key):
            prefix.append(ev)
    for pool in (exact, prefix):
        usable = [e for e in pool
                  if not not_before
                  or ((e.get("start") or {}).get("dateTime") or "") >= not_before]
        if usable:
            return max(usable, key=lambda e: len(e.get("attendees") or []))
    return None


def synth_event(subject: str, overlap: list[str], start: str) -> dict:
    """An event carrying exactly the tokens the log says overlapped.

    Used only when the real event has aged out of the calendar window.
    Tokens absent from the subject go on an attendee, which is where they
    came from: `_prepare_events` tokenises subject + organizer + attendees
    into one bag."""
    subj_low = subject.lower()
    extra = [t for t in overlap if t not in subj_low]
    attendees = []
    if extra:
        attendees.append({"emailAddress": {"name": " ".join(extra),
                                           "address": ""}})
    return {
        "subject": subject,
        "start": {"dateTime": start, "timeZone": "UTC"},
        "isAllDay": False,
        "showAs": "busy",
        "organizer": {"emailAddress": {"name": "", "address": ""}},
        "attendees": attendees,
    }


def topup_notes(entry: dict, overlap: list[str]) -> list[str]:
    """Make the entry carry the tokens the log recorded as overlapping.

    A false-positive case is vacuous if the entry no longer contains the
    tokens that produced the match, and several do not: the scrubber and
    the briefing OVERWRITE `notes` with their own audit text when they
    close and reopen an item, so the person tokens that drove the original
    match are gone. Re-adding the recorded tokens restores the condition
    under test. Returns what was added; the case records it so the
    reconstruction is visible rather than implied."""
    have = set(re.findall(r"[a-z0-9]+", " ".join([
        entry.get("description") or "", entry.get("notes") or "",
        entry.get("from_name") or "", entry.get("from_email") or "",
    ]).lower()))
    missing = [t for t in overlap if t.lower() not in have]
    if missing:
        entry["notes"] = ((entry.get("notes") or "") + " " +
                          " ".join(missing)).strip()
    return missing


def linkage(row: dict) -> str:
    if row.get("conversation_id") or row.get("gmail_thread_id"):
        return "email_linked"
    if row.get("source_message_id"):
        return "message_id_only"
    return "id_orphan"


def empty_world() -> dict:
    return {"sent": [], "inbox": [], "events": [], "closed_conversations": {}}


# ── scrub ──────────────────────────────────────────────────────────────
# The committed fixture carries stand-ins. The map of real strings to
# stand-ins lives outside the repo; the generic address rule and the
# residue check are the backstop for anything the map misses.

_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
EXAMPLE_DOMAINS = ("example.com", "example.edu", "example.org", "example.net")


def load_scrub_map(path: Path) -> dict[str, str]:
    """{"real string": "stand-in"}; a key starting with `_` is a comment."""
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError("scrub map: expected a JSON object")
    out = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if not isinstance(v, str) or not k:
            raise ValueError(f"scrub map: {k!r} must map to a string")
        out[k] = v
    return out


def _domain_of(domain: str, roots) -> bool:
    d = domain.lower()
    return any(d == r or d.endswith("." + r) for r in roots)


def scrub_address(addr: str, owner_domains) -> str:
    """An address at the owner's domains becomes <local>@example.edu; any
    other non-example address becomes <local>@example.com."""
    local, _, domain = addr.rpartition("@")
    if _domain_of(domain, EXAMPLE_DOMAINS):
        return addr
    if _domain_of(domain, owner_domains):
        return f"{local}@example.edu"
    return f"{local}@example.com"


def scrub_text(text: str, replacements: dict[str, str], owner_domains) -> str:
    """Exact replacements, longest key first, then the address rule."""
    for old in sorted(replacements, key=len, reverse=True):
        text = text.replace(old, replacements[old])
    return _ADDRESS_RE.sub(lambda m: scrub_address(m.group(0), owner_domains), text)


def scrub(obj, replacements: dict[str, str], owner_domains):
    """`scrub_text` over every string in a JSON document, dict keys
    included (sent mail is keyed by the account address)."""
    if isinstance(obj, str):
        return scrub_text(obj, replacements, owner_domains)
    if isinstance(obj, list):
        return [scrub(x, replacements, owner_domains) for x in obj]
    if isinstance(obj, dict):
        return {scrub_text(k, replacements, owner_domains) if isinstance(k, str) else k:
                scrub(v, replacements, owner_domains) for k, v in obj.items()}
    return obj


def owner_domains(profile: dict) -> list[str]:
    """The domains the profile calls the owner's: the mail address's,
    the institution's and its related campuses'."""
    inst = profile.get("institution") or {}
    doms = [(profile.get("email") or "").rpartition("@")[2]]
    doms += list(inst.get("domains") or []) + list(inst.get("related_domains") or [])
    return sorted({d.lower().strip() for d in doms if d and "." in d})


def owner_markers(profile: dict) -> list[tuple[str, re.Pattern]]:
    """(label, pattern) pairs that identify the owner: the full name, each
    name token as a word, the address local parts as substrings, the
    institution's name and every owner domain."""
    out: list[tuple[str, re.Pattern]] = []
    name = (profile.get("name") or "").strip()
    if name:
        out.append((name, re.compile(re.escape(name), re.I)))
        for tok in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", name):
            out.append((tok, re.compile(r"(?<![A-Za-z])" + re.escape(tok) + r"(?![A-Za-z])", re.I)))
    addrs = [profile.get("email") or ""] + list(profile.get("gmail_accounts") or [])
    for a in addrs:
        local = a.rpartition("@")[0].strip()
        if len(local) >= 4:
            out.append((local, re.compile(re.escape(local), re.I)))
    inst_name = ((profile.get("institution") or {}).get("name") or "").strip()
    if inst_name:
        out.append((inst_name, re.compile(re.escape(inst_name), re.I)))
    for d in owner_domains(profile):
        out.append((d, re.compile(re.escape(d), re.I)))
    return out


def residue(obj, markers, path: str = "$") -> list[str]:
    """Paths of the strings in `obj` that still carry an owner marker."""
    hits: list[str] = []
    if isinstance(obj, str):
        for label, pat in markers:
            if pat.search(obj):
                hits.append(f"{path}: {label!r}")
                break
    elif isinstance(obj, list):
        for i, x in enumerate(obj):
            hits += residue(x, markers, f"{path}[{i}]")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                hits += residue(k, markers, f"{path}.{k} (key)")
            hits += residue(v, markers, f"{path}.{k}")
    return hits


# ── hand-confirmed cases ───────────────────────────────────────────────
# Verified against the live mailbox at build time. They need a judgement
# the logs do not carry, so the owner writes them by hand, and they hold
# real ledger text, so they live in the private directory beside the
# corpus, never in this file. The JSON object has three lists:
#   cases        hand-confirmed misses: {ledger_id, label, cohort,
#                rationale, inbox, sent, as_of, handle?, entry}; each gets
#                an id_orphan case and its id_repaired twin
#   gmail        complete Gmail cases (entry and world given whole)
#   calendar_fp  calendar false closes whose ledger row is gone:
#                {case_id, entry, event_subject, overlap, as_of, rationale}
HAND_CASES_NAME = "closure_corpus_hand_cases.json"


def default_hand_cases_path() -> Path:
    return ch.default_real_corpus_path().with_name(HAND_CASES_NAME)


def load_hand_cases(path: Path | None) -> dict:
    """{"cases": [...], "gmail": [...], "calendar_fp": [...]}; empty lists
    when the file is absent."""
    out = {"cases": [], "gmail": [], "calendar_fp": []}
    p = path or default_hand_cases_path()
    try:
        raw = json.loads(Path(p).read_text())
    except FileNotFoundError:
        return out
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: expected a JSON object")
    for key in out:
        val = raw.get(key) or []
        if not isinstance(val, list):
            raise ValueError(f"{p}: {key} must be a list")
        out[key] = val
    return out


HAND_CASES: list = []
HAND_GMAIL: list = []
HAND_FP: list = []

# ── build ──────────────────────────────────────────────────────────────

def _mk_msg(d: dict, cid: str, kind: str) -> dict:
    m = {
        "conversationId": cid,
        "subject": d["subject"],
        "bodyPreview": d.get("preview", "")[:PREVIEW_CAP],
        "from": {"emailAddress": {"name": "",
                                  "address": d.get("from", "")}},
    }
    if kind == "sent":
        m["sentDateTime"] = d["sent_at"]
    else:
        m["receivedDateTime"] = d["received"]
    return m


def build(args) -> dict:
    global HAND_CASES, HAND_GMAIL, HAND_FP
    hand = load_hand_cases(Path(args.hand_cases) if args.hand_cases else None)
    HAND_CASES, HAND_GMAIL, HAND_FP = hand["cases"], hand["gmail"], hand["calendar_fp"]
    snap = Path(args.snapshot_dir)
    if not args.offline:
        fetch_snapshot(snap)
    sent = load_snapshot(snap, "sent.json")
    inbox = load_snapshot(snap, "inbox.json")
    events = load_snapshot(snap, "calendar.json")

    db = Path(args.ledger_db)
    ledger = load_ledger(db)
    closures = [r for r in read_jsonl(SCRUBBER_LOG)
                if r.get("id") not in FIXTURE_IDS and not r.get("dry_run")]
    vetoed = set((json.loads(VETO_STATE.read_text()).get("vetoes") or {})
                 if VETO_STATE.exists() else {})
    missed = read_jsonl(MISSED_LOG)

    pseudo = Pseudonymiser()
    cases: list[dict] = []
    unactionable: list[dict] = []
    contested: list[dict] = []

    sent_by_conv: dict[str, list] = {}
    for m in sent:
        sent_by_conv.setdefault(m.get("conversationId") or "", []).append(m)
    inbox_by_conv: dict[str, list] = {}
    for m in inbox:
        inbox_by_conv.setdefault(m.get("conversationId") or "", []).append(m)

    # ---- 1. false positives: closes a human reversed ------------------
    seen_fp: set[str] = set()
    for rec in closures:
        eid = rec["id"]
        signal = cm.signal_of(rec.get("reason", "")).split(":")[0]
        row = ledger.get(eid)
        if row is None or eid in seen_fp:
            continue
        info = parse_reason(rec["reason"])
        la = row.get("last_action") or ""
        human_reversed = eid in vetoed or bool(re.search(r"reopen", la, re.I))
        banner_only = (signal == "confirmation_email" and info["overlap"]
                       and all(t.lower() in _BANNER_VOCAB
                               for t in info["overlap"]))
        if not (human_reversed or banner_only):
            continue
        seen_fp.add(eid)

        fidelity = "live"
        world = empty_world()
        if signal == "calendar_event_match":
            ev = find_event(events, info["subject"],
                            not_before=row.get("date_identified") or "")
            if ev is None:
                ev = synth_event(info["subject"], info["overlap"],
                                 _forward_start(rec["ts"]))
                fidelity = "reconstructed"
            world["events"] = [sanitise_event(ev)]
        elif signal == "confirmation_email":
            world["inbox"] = [{
                "conversationId": "",
                "subject": info["subject"],
                "bodyPreview": BANNER + " " + info["subject"],
                "from": {"emailAddress": {"name": "",
                                          "address": info["from"]}},
                "receivedDateTime": rec["ts"],
            }]
            fidelity = "reconstructed"
        else:
            continue   # only signals 4 and 5 have recorded false closes

        entry = sanitise_entry(row, pseudo, clean=True)
        added = topup_notes(entry, info["overlap"])
        why = ("vetoed in closure-scrubber-vetoes.json" if eid in vetoed
               else "ledger last_action records a human reopen"
               if human_reversed else
               "overlap is entirely external-sender-banner vocabulary "
               "(bb95ac1 / 9c50a5b judged every such closure wrong)")
        cases.append({
            "case_id": f"fp-{signal}-{eid}",
            "label": "must_not_close",
            "cohort": f"{signal}_false_positive",
            "linkage": linkage(row),
            "signal_under_test": signal,
            "provenance": f"closure-scrubber.log {rec['ts']}; {why}",
            "world_fidelity": fidelity,
            "notes_topup": added,
            "rationale": (f"{signal} closed this on {rec['ts'][:10]} with "
                          f"overlap={info['overlap']} against "
                          f"{info['subject'][:60]!r}."),
            "as_of": rec["ts"],
            "entry": entry,
            "world": world,
        })

    for hand in HAND_FP:
        entry = dict(hand["entry"])
        ev = find_event(events, hand["event_subject"],
                        not_before=entry.get("date_identified") or "")
        fidelity = "live"
        if ev is None:
            ev = synth_event(hand["event_subject"], hand["overlap"],
                             _forward_start(hand["as_of"]))
            fidelity = "reconstructed"
        added = topup_notes(entry, hand["overlap"])
        world = empty_world()
        world["events"] = [sanitise_event(ev)]
        cases.append({
            "case_id": hand["case_id"],
            "label": "must_not_close",
            "cohort": "calendar_event_match_false_positive",
            "linkage": linkage(entry),
            "signal_under_test": "calendar_event_match",
            "provenance": f"closure-scrubber.log {hand['as_of']}; "
                          "named in the 2026-08-28 brief; ledger row gone",
            "world_fidelity": fidelity,
            "notes_topup": added,
            "rationale": hand["rationale"],
            "as_of": hand["as_of"],
            "entry": entry,
            "world": world,
        })

    for hand in HAND_GMAIL:
        case = dict(hand)
        world = empty_world()
        world.update(hand["world"])
        case["world"] = world
        case["linkage"] = linkage(case["entry"])
        case["notes_topup"] = []
        cases.append(case)

    # ---- 2. true positives: closes that stood -------------------------
    seen_tp: set[str] = set()
    for rec in closures:
        eid = rec["id"]
        signal = cm.signal_of(rec.get("reason", "")).split(":")[0]
        if signal not in ("user_replied_outlook", "inbound_ack"):
            continue
        row = ledger.get(eid)
        if row is None or eid in seen_tp or eid in seen_fp:
            continue
        if row.get("status") not in ("completed", "closed"):
            continue
        if re.search(r"reopen", row.get("last_action") or "", re.I):
            continue
        ack_label = cm.signal_of(rec.get("reason", "")).partition(":")[2]
        if signal == "inbound_ack" and ack_label not in COMPLETION_ACK_LABELS:
            seen_tp.add(eid)
            contested.append({
                "ledger_id": eid, "ts": rec["ts"], "signal": signal,
                "ack_label": ack_label,
                "description": (row.get("description") or "")[:100],
                "why": ("the recorded ack label says something about the "
                        "conversation, not about the obligation; 7e0601f "
                        "changed the answer here on purpose"),
            })
            continue
        seen_tp.add(eid)
        info = parse_reason(rec["reason"])
        entry = sanitise_entry(row, pseudo, clean=True)
        cid = entry["conversation_id"]
        world = empty_world()
        if signal == "user_replied_outlook":
            # The reason string carries the exact sentDateTime, and signal
            # 1 reads nothing else, so this world is exact.
            world["sent"] = [{
                "conversationId": cid,
                "subject": (row.get("description") or "")[:120],
                "sentDateTime": info["sent"] or rec["ts"],
                "bodyPreview": "",
                "from": {"emailAddress": {"name": "", "address": ""}},
            }]
            fidelity = "exact"
        else:
            # Signal 3 reads the latest inbound's body preview. The entry's
            # own stored notes ARE that preview for `response-*` rows.
            world["inbox"] = [{
                "conversationId": cid,
                "subject": (row.get("description") or "")[:120],
                "bodyPreview": (row.get("notes") or "")[:PREVIEW_CAP],
                "from": {"emailAddress": {"name": "",
                                          "address": info["from"]}},
                "receivedDateTime": rec["ts"],
            }]
            fidelity = "reconstructed"
        cases.append({
            "case_id": f"tp-{signal}-{eid}",
            "label": "should_close",
            "cohort": f"{signal}_true_positive",
            "linkage": linkage(row),
            "signal_under_test": signal,
            "provenance": f"closure-scrubber.log {rec['ts']}; never reopened",
            "world_fidelity": fidelity,
            "notes_topup": [],
            "rationale": ("the scrubber closed this and the user let it "
                          "stand. A change that stops closing it is a "
                          "recall regression."),
            "as_of": rec["ts"],
            "entry": entry,
            "world": world,
        })

    # ---- 3. user-stated closures, split by whether evidence exists ----
    seen_miss: set[str] = set()
    for rec in missed:
        lid = rec.get("ledger_id") or ""
        row = ledger.get(lid)
        if row is None or lid in seen_miss or lid in seen_fp or lid in seen_tp:
            continue
        seen_miss.add(lid)
        as_of = rec.get("ts") or ""
        cid_raw = row.get("conversation_id") or ""
        born = row.get("date_identified") or ""
        ev_sent = [m for m in sent_by_conv.get(cid_raw, [])
                   if cid_raw and born <= (m.get("sentDateTime") or "") <= as_of]
        ev_inbox = [m for m in inbox_by_conv.get(cid_raw, [])
                    if cid_raw and (m.get("receivedDateTime") or "") <= as_of]
        if not ev_sent and not ev_inbox:
            unactionable.append({
                "ledger_id": lid,
                "user_message": (rec.get("user_message") or "")[:120],
                "ts": as_of,
                "linkage": linkage(row),
                "why": ("no sent or inbound artifact exists in this "
                        "conversation between the entry's creation and the "
                        "moment the user said it was done"),
            })
            continue
        cases.append({
            "case_id": f"miss-{lid}",
            "label": "should_close",
            "cohort": "user_stated_miss",
            "linkage": linkage(row),
            "signal_under_test": None,
            "provenance": f"missed-closures.log {as_of}",
            "world_fidelity": "live",
            "notes_topup": [],
            "rationale": ("the user had to say this was done "
                          f"({(rec.get('user_message') or '')[:70]!r}) while "
                          "the evidence sat in the mailbox."),
            "as_of": as_of,
            "entry": sanitise_entry(row, pseudo, clean=True),
            "world": {
                "sent": [sanitise_message(m, pseudo) for m in ev_sent],
                "inbox": [sanitise_message(m, pseudo) for m in ev_inbox],
                "events": [],
                "closed_conversations": {},
            },
        })

    # ---- 4. hand-confirmed misses, with their id_repaired twins -------
    for hand in HAND_CASES:
        # The live row when the ledger still has it, else the case's own
        # stand-in entry, so the case is built on any install.
        row = ledger.get(hand["ledger_id"]) or hand.get("entry")
        if row is None:
            continue
        handle = hand.get("handle") or hand["ledger_id"][:8]
        cid = f"conv-hand-{handle}"
        world = {
            "sent": [_mk_msg(d, cid, "sent") for d in hand.get("sent", [])],
            "inbox": [_mk_msg(d, cid, "inbox") for d in hand.get("inbox", [])],
            "events": [],
            "closed_conversations": {},
        }
        base = sanitise_entry(row, pseudo, clean=True)
        cases.append({
            "case_id": f"orphan-{hand['ledger_id']}",
            "label": hand["label"],
            "cohort": hand["cohort"],
            "linkage": "id_orphan",
            "signal_under_test": None,
            "provenance": ("verified against the live mailbox "
                           "2026-08-28; named in the 2026-08-28 brief"),
            "world_fidelity": "live",
            "notes_topup": [],
            "rationale": hand["rationale"],
            "as_of": hand["as_of"],
            "entry": base,
            "world": world,
        })
        # The twin: same entry, same evidence, ids captured at creation.
        # The ONLY difference between the pair is id capture, so the gap
        # between their scores is the id-capture defect and nothing else.
        twin = dict(base)
        twin["id"] = base["id"] + "--id-repaired"
        twin["conversation_id"] = cid
        twin["source_message_id"] = f"msg-hand-{handle}"
        cases.append({
            "case_id": f"repaired-{hand['ledger_id']}",
            "label": hand["label"],
            "cohort": hand["cohort"],
            "linkage": "id_repaired",
            "signal_under_test": None,
            "provenance": ("counterfactual twin of "
                           f"orphan-{hand['ledger_id']}: identical entry and "
                           "evidence, with the ids ledger.py add never "
                           "captured"),
            "world_fidelity": "live",
            "notes_topup": [],
            "rationale": ("if this closes and its orphan twin does not, the "
                          "defect is id capture at creation, not the "
                          "signal."),
            "as_of": hand["as_of"],
            "entry": twin,
            "world": world,
        })

    # ---- 5. document-frequency ballast --------------------------------
    # The distinctiveness test measures a token's document frequency
    # over the OPEN LEDGER passed to scrub_closures, and skips the test
    # entirely below _RARE_MIN_CORPUS entries. Scoring one case at a time
    # against a corpus of one would therefore exercise a code path
    # production never takes. The ballast is the real open ledger, so the
    # harness reproduces production's token statistics.
    ballast = [sanitise_entry(r, pseudo, clean=True,
                              notes_cap=BALLAST_NOTES_CAP)
               for r in open_entries(db)]

    return {"cases": cases, "unactionable": unactionable,
            "contested": contested, "ballast": ballast}


def _live_profile() -> dict:
    try:
        return json.loads((EA_DIR / "config" / "user-profile.json").read_text())
    except (OSError, ValueError):
        return {}


def _builder_profile(prof: dict | None = None) -> dict:
    """The identity fields of the live profile the corpus is built
    against; preprocessor/closure_harness.py scores with them. The scrub
    maps them to the stand-ins like every other string."""
    prof = _live_profile() if prof is None else prof
    return {"name": prof.get("name", ""), "email": prof.get("email", ""),
            "gmail_accounts": prof.get("gmail_accounts", []) or []}


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the closure corpus")
    ap.add_argument("--out", default=str(ch.default_real_corpus_path()),
                    help="where to write the real corpus (owner-private; "
                         "default %(default)s)")
    ap.add_argument("--hand-cases", default="",
                    help="the owner-private hand-case file (default: "
                         f"{HAND_CASES_NAME} beside the real corpus)")
    ap.add_argument("--snapshot-dir", default="/tmp/ea-closure-snapshot")
    ap.add_argument("--ledger-db", default=str(LEDGER_DB))
    ap.add_argument("--offline", action="store_true",
                    help="use an existing snapshot dir, make no Graph calls")
    ap.add_argument("--scrub", metavar="MAP.json",
                    help="a JSON object of exact-string replacements applied "
                         "to every string before writing; addresses outside "
                         "the map become example.edu (owner domains) or "
                         "example.com")
    args = ap.parse_args()

    profile = _live_profile()
    built = build(args)
    doc = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "built_by": "scripts/build_closure_corpus.py",
        "profile": _builder_profile(profile),
        "caveats": [
            "Worlds are reconstructed from the production audit trail, not "
            "from replayed mailboxes. A user_replied_outlook world carries "
            "the exact sentDateTime the log recorded and signal 1 reads "
            "nothing else, so it is exact; calendar and confirmation worlds "
            "reproduce the token condition the log recorded, which is what "
            "the signal actually tested.",
            "`notes_topup` lists tokens re-added to an entry because the "
            "scrubber's and the briefing's own audit text overwrote the "
            "notes that produced the historical match.",
            "This corpus scores closure_scrubber's six signals. It does NOT "
            "score ledger_sync (inbox-as-completion), which makes about 65% "
            "of all closures the system has ever made, nor the briefing "
            "skill's step-6a LLM closeability pass.",
            "`unactionable` entries are recorded and NOT scored: the user "
            "closed them and no email or calendar artifact exists that any "
            "signal could have seen.",
            "`contested` entries are recorded and NOT scored: the scrubber "
            "closed them on an ack label that says something about the "
            "conversation rather than about the obligation ('out of "
            "office', 'all set'), and 7e0601f changed the answer on "
            "purpose. Scoring them would punish a deliberate decision.",
            "What this fixture deliberately KEEPS, because the signals "
            "read it: colleague names and addresses (the calendar-match "
            "defect is a person-token defect), subjects, timestamps, and "
            "ledger `notes` truncated to the same 400 characters "
            "`_entry_match_tokens` reads. What it drops: Outlook "
            "conversation and message ids (pseudonymised to handles), "
            "recipients, full message bodies, attachments, and every "
            "ledger column no signal touches.",
            "`discriminating` is added afterwards by `closure_harness.py "
            "audit --write`: false when no scored scrubber build closes "
            "the case, so holding it proves nothing.",
        ],
        "ballast": built["ballast"],
        "cases": built["cases"],
        "unactionable": built["unactionable"],
        "contested": built["contested"],
    }
    if args.scrub:
        doc = scrub(doc, load_scrub_map(Path(args.scrub)), owner_domains(profile))
    left = residue(doc, owner_markers(profile))
    if left:
        print(f"refusing to write {args.out}: {len(left)} string(s) still "
              "name the owner" + ("" if args.scrub else " (pass --scrub)"),
              file=sys.stderr)
        for line in left[:40]:
            print(f"  {line}", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(doc, indent=1, sort_keys=False))
    os.chmod(out, 0o600)
    labels: dict[str, int] = {}
    for c in doc["cases"]:
        labels[c["label"]] = labels.get(c["label"], 0) + 1
    print(f"wrote {args.out}")
    print(f"  cases: {len(doc['cases'])} {labels}")
    print(f"  unactionable: {len(doc['unactionable'])}")
    print(f"  contested: {len(doc['contested'])}")
    print(f"  ballast: {len(doc['ballast'])} open ledger entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
