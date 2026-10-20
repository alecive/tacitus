"""Closure-automation metrics + rule backtester (offline reader).

Part of the "user-stated closure is a bug" loop: the closure_scrubber
auto-closes ledger items from data signals, and every manual user-stated
closure is logged to missed-closures.log as a defect. This module turns
those logs into numbers and lets proposed rule changes be BACKTESTED
against recorded history before adoption.

Deliberately separate from closure_scrubber.py: the scrubber runs in the
hot pre-briefing path; this is an offline reader for the weekly audit.
Reads closure-scrubber.log, missed-closures.log, and the ledger DB
(read-only). Writes nothing.

Closure provenance taxonomy (classified from ledger `last_action`):
  scrubber        "Auto-closed by closure_scrubber: <reason>"
  briefing_llm    "auto-closed by briefing: ..."   (skill sanity-close)
  inbox_archive   "...Auto-closed: email archived from inbox by user..."
  manual          everything else (plugin-sourced entries excluded)

Hit rate = autonomous / (autonomous + manual), where autonomous =
scrubber + briefing_llm + inbox_archive. Manual closures are the bug
class; missed-closures.log records the explicitly-user-stated subset
with full signal autopsies.

Backtest caveat (stated, not hidden): backtests replay RECORDED evidence
summaries from the logs, not refetched mailboxes. Results are
directional — good enough to rank rule ideas, not a guarantee.

Usage:
    python3 preprocessor/closure_metrics.py report [--days 7] [--json|--md]
    python3 preprocessor/closure_metrics.py backtest [--days 90]
        [--min-overlap-tokens N] [--require-nongeneric-overlap]
        [--require-distinctive-overlap] [--drop-note-events]
        [--respect-reopen-veto] [--signal calendar_event_match]
        [--add-confirmation-sender SUBSTR] [--add-ack-pattern REGEX]

The tightening rules cover every overlap-bearing signal, so the calendar
signal replays the same way the confirmation signal does:

    backtest --days 7 --min-overlap-tokens 2 --require-distinctive-overlap \\
             --drop-note-events --respect-reopen-veto \\
             --signal calendar_event_match
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
SCRUBBER_LOG = EA_DIR / "logs" / "closure-scrubber.log"
MISSED_LOG = EA_DIR / "logs" / "missed-closures.log"
LEDGER_DB = EA_DIR / "state" / "obligations-ledger.db"


def _open_ledger_ro(db) -> sqlite3.Connection:
    """A read-only connection to the ledger. An owner process (the check-in
    cron) opens it through ledger.guarded_connect, which refuses a link at
    the database or its sidecars; the resident opens it directly."""
    import ledger as _ledger
    return _ledger.guarded_connect(db, read_only=True)

# Tokens that should never count as meaningful overlap on their own.
# Superset of the scrubber's _GENERIC_NOISE_TOKENS plus the
# "CAUTION: external sender" banner vocabulary, which one email can share
# with many unrelated entries.
_METRICS_GENERIC_TOKENS: set[str] = {
    "caution", "external", "email", "sender", "link", "links", "note",
    "attachments", "unless", "click", "message", "response", "reply",
}
try:
    from closure_scrubber import _GENERIC_NOISE_TOKENS
    _METRICS_GENERIC_TOKENS |= set(_GENERIC_NOISE_TOKENS)
except Exception:
    pass

# Calendar furniture is generic for the calendar signal only, so it is
# kept separate from the shared set above.
_CALENDAR_GENERIC_TOKENS: set[str] = set()
try:
    from closure_scrubber import (_CALENDAR_NOISE_TOKENS,
                                  _INSTITUTIONAL_LABELS)
    _CALENDAR_GENERIC_TOKENS = set(_CALENDAR_NOISE_TOKENS) | set(
        _INSTITUTIONAL_LABELS)
except Exception:
    pass

_SIGNAL_PREFIXES = (
    "user_replied_outlook", "user_replied_gmail", "inbound_ack",
    "confirmation_email", "calendar_event_match",
    "closed_sibling", "sender_reply_match", "project_member_completion",
    "meeting_response_sent", "identifier_siblings_closed",
)

# Signals whose reason string carries a token overlap, so the tightening
# rules below can be replayed against them.
_OVERLAP_SIGNALS = ("confirmation_email", "calendar_event_match")


# ---------------------------------------------------------------------------
# Log / ledger readers
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def signal_of(reason: str) -> str:
    """Signal label from a scrubber reason string.

    'inbound_ack:<label> ...' keeps its sublabel; others use the prefix."""
    reason = (reason or "").strip()
    for p in _SIGNAL_PREFIXES:
        if reason.startswith(p):
            if p == "inbound_ack":
                m = re.match(r"inbound_ack:([\w-]+)", reason)
                return m.group(0) if m else p
            return p
    return "unknown"


def parse_confirmation_reason(reason: str) -> dict:
    """Parse 'confirmation_email (from=X, subject='Y', overlap=[...])'."""
    out = {"from": "", "subject": "", "overlap": []}
    m = re.search(r"from=([^,)]+)", reason or "")
    if m:
        out["from"] = m.group(1).strip()
    m = re.search(r"subject='([^']*)'", reason or "")
    if m:
        out["subject"] = m.group(1)
    m = re.search(r"overlap=(\[[^\]]*\])", reason or "")
    if m:
        try:
            out["overlap"] = ast.literal_eval(m.group(1))
        except Exception:
            pass
    return out


def generic_tokens_for(signal: str) -> set[str]:
    """Which vocabulary counts as meaningless for this signal."""
    if signal == "calendar_event_match":
        return _METRICS_GENERIC_TOKENS | _CALENDAR_GENERIC_TOKENS
    return _METRICS_GENERIC_TOKENS


def _is_wordless(token: str) -> bool:
    return not any(c.isalpha() for c in token)


def ledger_token_df(db_path: Path | None = None) -> tuple[dict, int]:
    """Document frequency of match-tokens over the whole ledger.

    Offline stand-in for what the scrubber computes from the live open
    entries. Used by --require-distinctive-overlap, which asks whether a
    matched token is rare enough to identify one obligation."""
    db = db_path or LEDGER_DB
    try:
        import closure_scrubber as cs
    except Exception:
        return ({}, 0)
    try:
        con = _open_ledger_ro(db)
    except Exception:
        return ({}, 0)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT description, from_name, from_email, notes FROM ledger"
        ).fetchall()
    except Exception:
        return ({}, 0)
    finally:
        con.close()
    entries = [dict(r) for r in rows]
    return (cs.build_token_df(entries), len(entries))


def closure_outcomes(days: int, db_path: Path | None = None) -> dict:
    """Ground truth per (entry_id, signal) from the log + the ledger.

    A closure the user fought is a false positive, and the log says so
    without anyone labelling it: the same signal closing the same entry
    twice means a human reopened it in between. An entry that is open
    today despite an auto-close says the same thing."""
    db = db_path or LEDGER_DB
    status: dict[str, str] = {}
    try:
        con = _open_ledger_ro(db)
        for row in con.execute("SELECT id, status FROM ledger"):
            status[row[0]] = row[1]
        con.close()
    except Exception:
        pass
    out: dict[tuple, dict] = {}
    for r in scrubber_closures(days):
        sig = signal_of(r.get("reason", ""))
        key = (r.get("id"), sig)
        rec = out.setdefault(key, {"id": r.get("id"), "signal": sig,
                                   "closes": 0, "reopened": False})
        rec["closes"] += 1
    for key, rec in out.items():
        st = status.get(rec["id"], "")
        rec["status"] = st
        rec["reopened"] = (rec["closes"] > 1
                           or (st not in ("", "completed", "closed")))
    return out


def scrubber_closures(days: int) -> list[dict]:
    cutoff = _cutoff_iso(days)
    return [r for r in _read_jsonl(SCRUBBER_LOG)
            if not r.get("dry_run") and (r.get("ts") or "") >= cutoff]


def missed_closures(days: int) -> list[dict]:
    cutoff = _cutoff_iso(days)
    return [r for r in _read_jsonl(MISSED_LOG)
            if (r.get("ts") or "") >= cutoff]


def classify_last_action(last_action: str) -> str:
    la = (last_action or "")
    if la.startswith("Auto-closed by closure_scrubber:"):
        return "scrubber"
    if la.lower().startswith("auto-closed by briefing:"):
        return "briefing_llm"
    if "Auto-closed: email archived from inbox by user" in la:
        return "inbox_archive"
    return "manual"


def ledger_closures(days: int, db_path: Path | None = None) -> list[dict]:
    """Closed/completed non-plugin entries in window, with provenance."""
    db = db_path or LEDGER_DB
    cutoff = _cutoff_iso(days)
    try:
        con = _open_ledger_ro(db)
    except Exception:
        return []
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, status, last_action, closed_at, source, description "
        "FROM ledger WHERE status IN ('closed', 'completed') "
        "AND closed_at >= ? "
        "AND (source IS NULL OR source NOT LIKE 'plugin:%')",
        (cutoff,),
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["provenance"] = classify_last_action(d.get("last_action") or "")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def false_positive_candidates(rows: list[dict]) -> list[dict]:
    """Suspicious auto-closure clusters in scrubber-log rows.

    Three heuristics:
      1. One artifact (an email, or a calendar event) closing >=3
         entries in the same run.
      2. Overlap that is entirely generic (banner vocabulary, scheduling
         noise, calendar furniture, bare years).
      3. The same signal closing the same entry more than once. Only a
         human reopen can put an entry back, so a repeat close is a
         recorded fight — the strongest false-positive evidence there
         is, and it needs no ledger read."""
    flagged: list[dict] = []

    # Cluster: same (minute-rounded ts, source artifact) closing many entries
    clusters: dict[tuple, list[dict]] = {}
    for r in rows:
        sig = signal_of(r.get("reason", ""))
        if sig not in _OVERLAP_SIGNALS:
            continue
        info = parse_confirmation_reason(r["reason"])
        source = info["from"] or info["subject"]
        key = ((r.get("ts") or "")[:16], sig, source)
        clusters.setdefault(key, []).append(r)
    for (ts_min, sig, source), members in clusters.items():
        if len(members) >= 3:
            flagged.append({
                "kind": "one-email-closed-many",
                "signal": sig, "sender": source, "ts": ts_min,
                "count": len(members),
                "entry_ids": [m["id"] for m in members],
            })

    # Generic-only overlap
    for r in rows:
        sig = signal_of(r.get("reason", ""))
        if sig not in _OVERLAP_SIGNALS:
            continue
        overlap = parse_confirmation_reason(r["reason"])["overlap"]
        generic = generic_tokens_for(sig)
        if overlap and all(t in generic or _is_wordless(t) for t in overlap):
            flagged.append({
                "kind": "generic-only-overlap", "signal": sig,
                "entry_id": r["id"], "ts": r.get("ts"),
                "overlap": overlap,
            })

    # Repeat close of the same (entry, signal): a fought closure
    repeats: dict[tuple, list[dict]] = {}
    for r in rows:
        sig = signal_of(r.get("reason", ""))
        repeats.setdefault((r.get("id"), sig), []).append(r)
    for (entry_id, sig), members in sorted(repeats.items()):
        if len(members) >= 2:
            flagged.append({
                "kind": "reclosed-after-reopen", "signal": sig,
                "entry_id": entry_id, "count": len(members),
                "ts": (members[-1].get("ts") or ""),
            })
    return flagged


def build_report(days: int, db_path: Path | None = None) -> dict:
    scrub = scrubber_closures(days)
    missed = missed_closures(days)
    ledger_rows = ledger_closures(days, db_path=db_path)

    by_prov = Counter(r["provenance"] for r in ledger_rows)
    autonomous = (by_prov["scrubber"] + by_prov["briefing_llm"]
                  + by_prov["inbox_archive"])
    manual = by_prov["manual"]
    total = autonomous + manual
    hit_rate = (autonomous / total) if total else None

    signal_dist = Counter(signal_of(r.get("reason", "")) for r in scrub)

    suggestions = Counter(
        r.get("suggested_signal") for r in missed if r.get("suggested_signal")
    )

    return {
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "hit_rate": round(hit_rate, 3) if hit_rate is not None else None,
        "autonomous_closures": autonomous,
        "manual_closures": manual,
        "provenance": dict(by_prov),
        "scrubber_signal_distribution": dict(signal_dist),
        "user_stated_closures_logged": len(missed),
        "false_positive_candidates": false_positive_candidates(scrub),
        "suggested_signals": [
            {"suggestion": s, "count": c} for s, c in suggestions.most_common()
        ],
    }


def render_md(rep: dict) -> str:
    lines = [f"*Closure automation — last {rep['window_days']}d*"]
    hr = rep["hit_rate"]
    lines.append(
        f"• Hit rate: {f'{hr:.0%}' if hr is not None else 'n/a'} "
        f"({rep['autonomous_closures']} autonomous / "
        f"{rep['manual_closures']} manual)")
    prov = rep["provenance"]
    lines.append("• Provenance: " + ", ".join(
        f"{k} {v}" for k, v in sorted(prov.items(), key=lambda kv: -kv[1]))
        if prov else "• Provenance: none")
    sd = rep["scrubber_signal_distribution"]
    if sd:
        lines.append("• Scrubber signals: " + ", ".join(
            f"{k} {v}" for k, v in sorted(sd.items(), key=lambda kv: -kv[1])))
    if rep["user_stated_closures_logged"]:
        lines.append(f"• User-stated closures logged (bug class): "
                     f"{rep['user_stated_closures_logged']}")
    fps = rep["false_positive_candidates"]
    if fps:
        lines.append(f"• Suspected false positives: {len(fps)}")
        for fp in fps[:5]:
            if fp["kind"] == "one-email-closed-many":
                lines.append(f"    - {fp['sender']} closed {fp['count']} "
                             f"entries at {fp['ts']}")
            elif fp["kind"] == "reclosed-after-reopen":
                lines.append(f"    - {fp['entry_id']}: {fp['signal']} closed "
                             f"it {fp['count']}x after reopen")
            else:
                lines.append(f"    - {fp['entry_id']}: generic-only overlap "
                             f"{fp['overlap']}")
    else:
        lines.append("• Suspected false positives: none")
    for s in rep["suggested_signals"][:3]:
        lines.append(f"• Suggested: {s['suggestion'][:120]} (x{s['count']})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

_EVIDENCE_INBOX_RE = re.compile(
    r"^inbox\s+(?P<ts>\S+):\s+subject=(?P<subj>'[^']*'|\"[^\"]*\")"
    r"\s+from=(?P<from>\S+)")


def backtest(days: int, *, min_overlap_tokens: int = 0,
             require_nongeneric_overlap: bool = False,
             add_confirmation_sender: str = "",
             add_ack_pattern: str = "",
             signals: tuple | None = None,
             require_distinctive_overlap: bool = False,
             drop_note_events: bool = False,
             respect_reopen_veto: bool = False,
             db_path: Path | None = None) -> dict:
    """Replay recorded history through a parameterized rule overlay.

    LOOSENING rules (recall gain — which past MISSED closures would the
    new rule have caught?):
      --add-confirmation-sender: a missed closure whose candidate
        evidence includes an inbound from a matching sender would now
        be eligible for the confirmation_email signal.
      --add-ack-pattern: evidence inbound subjects matching the regex
        would now classify as acks.

    TIGHTENING rules (precision impact — which past AUTO-closures would
    the stricter rule have suppressed?). They apply to every
    overlap-bearing signal (confirmation_email AND calendar_event_match)
    unless `signals` narrows it:
      --min-overlap-tokens N: closures with fewer than N usable overlap
        tokens get suppressed. "Usable" drops generic vocabulary and
        bare numbers; for the calendar signal it also drops calendar
        furniture and mail-domain labels.
      --require-nongeneric-overlap: closures whose overlap is entirely
        generic get suppressed.
      --require-distinctive-overlap: closures with no RARE token get
        suppressed (frequency measured over the ledger; a word that
        appears in many entries identifies none of them).
      --drop-note-events: calendar closures whose event is a `[note]`
        record or a wh/nm/p0/p1/p2/wr solver event get suppressed.
      --respect-reopen-veto: the 2nd and later closes of the same
        (entry, signal) get suppressed. A repeat close is proof a human
        reopened the entry in between, which is a standing veto.

    Every suppression is scored against ground truth: a closure the user
    fought (closed twice, or open today) was a false positive; one that
    stuck is presumed correct. `outcome_scores` reports both.
    """
    would_catch: list[dict] = []
    would_suppress: list[dict] = []
    signals = tuple(signals) if signals else _OVERLAP_SIGNALS

    if add_confirmation_sender or add_ack_pattern:
        ack_re = re.compile(add_ack_pattern, re.I) if add_ack_pattern else None
        for rec in missed_closures(days):
            for ev in rec.get("candidate_evidence") or []:
                m = _EVIDENCE_INBOX_RE.match(ev)
                if not m:
                    continue
                addr = m.group("from").lower()
                subj = m.group("subj").strip("'\"")
                hit = False
                if (add_confirmation_sender
                        and add_confirmation_sender.lower() in addr):
                    hit = True
                if ack_re and ack_re.search(subj):
                    hit = True
                if hit:
                    would_catch.append({
                        "ledger_id": rec.get("ledger_id"),
                        "user_message": rec.get("user_message"),
                        "evidence": ev[:160],
                    })
                    break

    tightening = (min_overlap_tokens or require_nongeneric_overlap
                  or require_distinctive_overlap or drop_note_events
                  or respect_reopen_veto)
    outcomes = closure_outcomes(days, db_path=db_path) if tightening else {}
    scores = {"fought_total": 0, "fought_suppressed": 0,
              "stuck_total": 0, "stuck_suppressed": 0}
    if tightening:
        token_df, corpus = ({}, 0)
        if require_distinctive_overlap:
            token_df, corpus = ledger_token_df(db_path)
        seen: dict[tuple, int] = {}
        rows = sorted(scrubber_closures(days), key=lambda r: r.get("ts") or "")
        for r in rows:
            sig = signal_of(r.get("reason", ""))
            if sig not in signals:
                continue
            info = parse_confirmation_reason(r["reason"])
            overlap = info["overlap"]
            key = (r.get("id"), sig)
            nth = seen.get(key, 0)
            seen[key] = nth + 1
            generic = generic_tokens_for(sig)
            usable = [t for t in overlap
                      if t not in generic and not _is_wordless(t)]
            why = ""
            if respect_reopen_veto and nth >= 1:
                why = ("standing veto: this signal already closed the "
                       "entry once and a human reopened it")
            elif drop_note_events and _is_note_subject(info["subject"]):
                why = f"non-binding event: {info['subject'][:60]!r}"
            elif min_overlap_tokens and len(usable) < min_overlap_tokens:
                why = (f"only {len(usable)} usable overlap "
                       f"token(s) (< {min_overlap_tokens})")
            elif require_nongeneric_overlap and overlap and not usable:
                why = f"overlap entirely generic: {overlap}"
            elif require_distinctive_overlap and usable and not _any_rare(
                    usable, token_df, corpus):
                why = (f"no distinctive token; all of {usable} are common "
                       f"in the ledger")
            fought = (outcomes.get(key) or {}).get("reopened", False)
            scores["fought_total" if fought else "stuck_total"] += 1
            if why:
                scores["fought_suppressed" if fought
                       else "stuck_suppressed"] += 1
                would_suppress.append({
                    "entry_id": r["id"], "ts": r.get("ts"), "signal": sig,
                    "description": (r.get("description") or "")[:80],
                    "overlap": overlap, "why": why,
                    "outcome": "fought" if fought else "stuck",
                })

    return {
        "window_days": days,
        "rule": {
            "signals": list(signals),
            "min_overlap_tokens": min_overlap_tokens,
            "require_nongeneric_overlap": require_nongeneric_overlap,
            "require_distinctive_overlap": require_distinctive_overlap,
            "drop_note_events": drop_note_events,
            "respect_reopen_veto": respect_reopen_veto,
            "add_confirmation_sender": add_confirmation_sender,
            "add_ack_pattern": add_ack_pattern,
        },
        "would_catch": would_catch,
        "would_catch_count": len(would_catch),
        "would_suppress": would_suppress,
        "would_suppress_count": len(would_suppress),
        "outcome_scores": scores,
        "caveat": ("backtest replays recorded log evidence, not refetched "
                   "mailboxes — directional, not exact"),
    }


def _is_note_subject(subject: str) -> bool:
    try:
        import closure_scrubber as cs
        return cs._event_is_non_binding(subject or "")
    except Exception:
        return "[note]" in (subject or "").lower()


def _any_rare(tokens: list, token_df: dict, corpus: int) -> bool:
    if not token_df or corpus <= 0:
        return True
    try:
        import closure_scrubber as cs
        ceiling = cs._rare_df_ceiling(corpus)
    except Exception:
        ceiling = max(4, int(0.05 * corpus))
    return any(token_df.get(t, 0) <= ceiling for t in tokens)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Closure-automation metrics")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_rep = sub.add_parser("report")
    p_rep.add_argument("--days", type=int, default=7)
    fmt = p_rep.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true")
    fmt.add_argument("--md", action="store_true")

    p_bt = sub.add_parser("backtest")
    p_bt.add_argument("--days", type=int, default=90)
    p_bt.add_argument("--min-overlap-tokens", type=int, default=0)
    p_bt.add_argument("--require-nongeneric-overlap", action="store_true")
    p_bt.add_argument("--require-distinctive-overlap", action="store_true")
    p_bt.add_argument("--drop-note-events", action="store_true")
    p_bt.add_argument("--respect-reopen-veto", action="store_true")
    p_bt.add_argument("--signal", action="append", default=[],
                      choices=list(_OVERLAP_SIGNALS),
                      help="restrict tightening to this signal (repeatable)")
    p_bt.add_argument("--add-confirmation-sender", default="")
    p_bt.add_argument("--add-ack-pattern", default="")

    args = ap.parse_args()
    if args.cmd == "report":
        rep = build_report(args.days)
        print(render_md(rep) if args.md else json.dumps(rep, indent=2))
    else:
        res = backtest(
            args.days,
            min_overlap_tokens=args.min_overlap_tokens,
            require_nongeneric_overlap=args.require_nongeneric_overlap,
            require_distinctive_overlap=args.require_distinctive_overlap,
            drop_note_events=args.drop_note_events,
            respect_reopen_veto=args.respect_reopen_veto,
            signals=tuple(args.signal) if args.signal else None,
            add_confirmation_sender=args.add_confirmation_sender,
            add_ack_pattern=args.add_ack_pattern,
        )
        print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
