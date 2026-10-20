#!/usr/bin/env python3
"""Monthly voice measurement from sent mail.

`style_profiles.py` answers "how does the user write to THIS person" at
drafting time. This answers the questions that only a whole corpus can:
how long is a typical email, how often does the user sign off bare, how does
hedging move between a faculty argument and a sponsor report — and,
across months, which of those have drifted away from what the drafting
guide (docs/ea/drafting-voice.md) claims.

The measurement is pure Python and costs no tokens. The judgment of
what a drift means, and whether the style guide should change, is
dispatched to the resident pane, which can read the guide and decide.

    python3 style_analysis.py run                    # measure + snapshot
    python3 style_analysis.py run --dispatch         # + hand to resident
    python3 style_analysis.py run --corpus dump.json # offline, no Graph
    python3 style_analysis.py show                   # last report

Audience segmentation is domain-based by default. Per-address overrides
live in ~/.ea/config/style-audiences.json so the mapping is visible and
editable rather than buried in this file:

    {"chair@example.edu": "leadership/peers", ...}
"""

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import style_profiles as sp  # noqa: E402
import atomic_io as _atomic  # noqa: E402
import owner_trust as _owner_trust  # noqa: E402
import resident_io as _rio  # noqa: E402

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
REPORT_DIR = EA_DIR / "state" / "style-analysis"
LATEST = REPORT_DIR / "latest.json"
AUDIENCE_CONFIG = EA_DIR / "config" / "style-audiences.json"

DEFAULT_DAYS = 90
# Below this a segment's rates are noise, so drift is not reported.
MIN_SEGMENT_MESSAGES = 12
# Report a rate change only past this fraction, so ordinary
# month-to-month wobble does not read as a finding.
DRIFT_FRACTION = 0.25


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

def fetch(days: int = DEFAULT_DAYS) -> list:
    """Sent mail with real bodies.

    Deliberately not `graph.get_sent`, which selects bodyPreview and
    caps every message at 255 characters — the endings would all be
    missing, and endings are half of what this measures.
    """
    import graph_client as graph
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return graph.graph_get_all("/me/mailfolders/sentitems/messages", {
        "$select": graph.MAIL_FIELDS_FULL,
        "$filter": f"sentDateTime ge {cutoff}",
        "$orderby": "sentDateTime desc",
        "$top": 100,
    })


def _addresses(msg: dict, field: str) -> list:
    out = []
    for r in msg.get(field) or []:
        a = ((r.get("emailAddress") or {}).get("address") or "").lower()
        if a:
            out.append(a)
    return out


def build_rows(msgs: list) -> list:
    """One feature row per usable message.

    Shares `style_profiles`' text extraction, so quoted history, Outlook
    disclaimers and Zoom invite boilerplate come out the same way here
    as they do in the per-contact profiles.
    """
    rows = []
    for m in msgs or []:
        if m.get("isDraft"):
            continue
        to = _addresses(m, "toRecipients")
        if not to or len(to) > sp.MAX_CONVERSATION_RECIPIENTS:
            continue
        body = m.get("body")
        content = body.get("content") if isinstance(body, dict) else ""
        text = sp._own_text(sp._html_to_text(content or ""))
        if len(text.split()) < 5:
            continue
        f = sp._extract_features(text)
        rows.append({
            "to": to,
            "sent_at": (m.get("sentDateTime") or "")[:10],
            **f,
        })
    return rows


# ---------------------------------------------------------------------------
# Audience
# ---------------------------------------------------------------------------

def load_audiences() -> dict:
    try:
        return {k.lower(): v
                for k, v in json.loads(
                    AUDIENCE_CONFIG.read_text(encoding="utf-8")).items()}
    except Exception:
        return {}


def segment(to: list, overrides: dict, own_domain: str) -> str:
    """Audience label for one message.

    An explicit override wins, so the interesting split inside the
    user's own institution (students vs leadership, who otherwise share
    a domain) survives.
    """
    for addr in to:
        if addr in overrides:
            return overrides[addr]
    for addr in to:
        if addr.endswith((".mil", ".gov")):
            return "government"
        if own_domain and addr.endswith(own_domain):
            return "internal"
        if addr.endswith((".edu", ".ac.uk")):
            return "external academic"
        if addr.endswith(("gmail.com", "outlook.com", "yahoo.com",
                          "icloud.com", "hotmail.com")):
            return "personal"
        if addr.endswith(".com"):
            return "industry"
    return "other"


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def _rates(rows: list) -> dict:
    words = sorted(r["words"] for r in rows)
    total = sum(words) or 1
    n = len(rows)
    bare = sum(1 for r in rows if not r["closing"])

    def per_1k(field):
        return round(1000 * sum(r[field] for r in rows) / total, 2)

    return {
        "messages": n,
        "words": total,
        "median_words": words[n // 2],
        "p90_words": words[min(n - 1, int(0.9 * n))],
        "max_words": words[-1],
        "bare_sign_off_pct": round(100 * bare / n),
        "hedges_per_1k": per_1k("hedges"),
        "questions_per_1k": per_1k("questions"),
        "exclamations_per_1k": per_1k("exclamations"),
    }


def analyze(rows: list, own_domain: str) -> dict:
    if not rows:
        return {"messages": 0}
    overrides = load_audiences()
    by = defaultdict(list)
    for r in rows:
        by[segment(r["to"], overrides, own_domain)].append(r)

    dates = sorted(r["sent_at"] for r in rows if r["sent_at"])
    # Em-dashes are a standing rule in CLAUDE.md, so the rule should be
    # re-checked against the corpus rather than asserted from memory.
    # Split out pasted content: markdown in an email means the text came
    # from a document or a model, and its punctuation is not the user's hand.
    true_rows = [r for r in rows if r.get("true_em_dashes")]
    hyphen_rows = [r for r in rows if r.get("double_hyphens")]
    report = {
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "window": {"from": dates[0] if dates else "",
                   "to": dates[-1] if dates else ""},
        "overall": _rates(rows),
        "greetings": dict(Counter(
            r["greeting"] for r in rows if r["greeting"]).most_common(5)),
        "closings": dict(Counter(
            r["closing"] for r in rows if r["closing"]).most_common(5)),
        "em_dashes": {
            "corpus_messages": len(rows),
            "true_em_dash": {
                "occurrences": sum(r["true_em_dashes"] for r in true_rows),
                "messages": len(true_rows),
                "in_pasted_markdown": sum(1 for r in true_rows
                                          if r.get("pasted_markdown")),
                "typed_by_hand": sum(1 for r in true_rows
                                     if not r.get("pasted_markdown")),
            },
            "double_hyphen": {
                "occurrences": sum(r["double_hyphens"] for r in hyphen_rows),
                "messages": len(hyphen_rows),
                "in_pasted_markdown": sum(1 for r in hyphen_rows
                                          if r.get("pasted_markdown")),
                "typed_by_hand": sum(1 for r in hyphen_rows
                                     if not r.get("pasted_markdown")),
            },
        },
        "length_bands": {},
        "segments": {},
    }
    for lo, hi, name in ((0, 25, "one_liner"), (25, 75, "short"),
                         (75, 175, "medium"), (175, 10 ** 9, "long")):
        k = [r for r in rows if lo <= r["words"] < hi]
        report["length_bands"][name] = {
            "n": len(k), "pct": round(100 * len(k) / len(rows))}
    for name, seg_rows in sorted(by.items(), key=lambda x: -len(x[1])):
        report["segments"][name] = _rates(seg_rows)
    return report


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------

_TRACKED = ("median_words", "p90_words", "bare_sign_off_pct",
            "hedges_per_1k", "questions_per_1k", "exclamations_per_1k")


def drift(current: dict, previous: dict) -> list:
    """Changes worth a human's attention, and nothing else.

    A monthly report that lists every number invites being skimmed, so
    only movement past DRIFT_FRACTION on a segment with enough messages
    to mean anything is returned.
    """
    if not previous or not previous.get("overall"):
        return []
    out = []
    scopes = [("overall", current.get("overall", {}),
               previous.get("overall", {}))]
    for name, cur in (current.get("segments") or {}).items():
        prev = (previous.get("segments") or {}).get(name)
        if prev:
            scopes.append((name, cur, prev))
    for scope, cur, prev in scopes:
        if min(cur.get("messages", 0),
               prev.get("messages", 0)) < MIN_SEGMENT_MESSAGES:
            continue
        for field in _TRACKED:
            a, b = prev.get(field), cur.get(field)
            if not isinstance(a, (int, float)) or not a:
                continue
            if not isinstance(b, (int, float)):
                continue
            change = (b - a) / a
            if abs(change) >= DRIFT_FRACTION:
                out.append({
                    "scope": scope, "field": field,
                    "was": a, "now": b,
                    "change_pct": round(100 * change),
                })
    return sorted(out, key=lambda d: -abs(d["change_pct"]))


# ---------------------------------------------------------------------------
# Persistence + rendering
# ---------------------------------------------------------------------------

def _resident_space(path: Path) -> bool:
    """True when this is an owner process (the monthly cron) and `path`
    lies in resident space (the EA tree as resolved, or the resident's
    home). A path of the owner's own, or any path in another process, is
    reached directly."""
    if not _rio.is_owner():
        return False
    parent = os.path.realpath(Path(path).parent)
    roots = [os.path.realpath(_owner_trust.ea_dir())]
    home = os.path.expanduser(f"~{_rio.RESIDENT_USER}")
    if not home.startswith("~"):
        roots.append(os.path.realpath(home))
    return any(parent == r or parent.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def save(report: dict) -> Path:
    """Write the month's snapshot and latest.json. The report directory is
    resident space (the resident pane reads the report), so an owner
    process writes each file with the resident's access and never creates
    the directory itself; elsewhere the write is an atomic replace."""
    stamp = (report.get("window", {}).get("to") or "")[:7] or "unknown"
    path = REPORT_DIR / f"{stamp}.json"
    text = json.dumps(report, indent=2)
    for target in (path, LATEST):
        if _resident_space(target):
            if not _rio.replace_text(target, text, mode=0o664):
                raise OSError(f"could not write {target}")
        else:
            _atomic.write_text(target, text, mode=0o664)
    return path


def load_previous() -> dict:
    """The last report. It feeds the drift digest the resident reads, so an
    owner process reads it with the resident's access (resident_io, which
    refuses a link at the name); anything but a JSON object is no report."""
    try:
        if _resident_space(LATEST):
            text = _rio.read_text(LATEST)
        else:
            text = LATEST.read_text(encoding="utf-8")
        data = json.loads(text) if text else {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def render(report: dict, changes: list) -> str:
    o = report.get("overall", {})
    w = report.get("window", {})
    d = report.get("em_dashes") or {}
    lines = [
        f"*Voice measurement* {w.get('from','?')} to {w.get('to','?')} "
        f"({o.get('messages',0)} emails, {o.get('words',0)} words)",
        f"median {o.get('median_words')}w | p90 {o.get('p90_words')}w | "
        f"bare sign-off {o.get('bare_sign_off_pct')}% | "
        f"hedges {o.get('hedges_per_1k')}/1k",
        f"em-dash '—': {(d.get('true_em_dash') or {}).get('occurrences', 0)}"
        f" in {(d.get('true_em_dash') or {}).get('messages', 0)}"
        f"/{d.get('corpus_messages', 0)} msgs "
        f"({(d.get('true_em_dash') or {}).get('in_pasted_markdown', 0)} pasted)"
        f" | ' -- ': "
        f"{(d.get('double_hyphen') or {}).get('occurrences', 0)} in "
        f"{(d.get('double_hyphen') or {}).get('messages', 0)} msgs",
        "",
        "Segment            n   med  p90  hedge/1k  excl/1k  bare%",
    ]
    for name, s in (report.get("segments") or {}).items():
        lines.append(f"{name[:18]:18s} {s['messages']:3d} {s['median_words']:5d} "
                     f"{s['p90_words']:4d} {s['hedges_per_1k']:9.2f} "
                     f"{s['exclamations_per_1k']:8.2f} "
                     f"{s['bare_sign_off_pct']:5d}%")
    if changes:
        lines += ["", f"*Drift since last run* ({len(changes)}):"]
        for c in changes[:10]:
            lines.append(f"  {c['scope']} {c['field']}: "
                         f"{c['was']} -> {c['now']} ({c['change_pct']:+d}%)")
    else:
        lines += ["", "No drift past threshold since the last run."]

    edits = report.get("draft_edits") or {}
    if edits.get("pairs"):
        lines += ["", f"*Draft vs sent* — {edits['pairs']} pairs, "
                      f"{edits.get('untouched', 0)} sent unedited, "
                      f"median length change "
                      f"{edits.get('median_length_change_pct', 0):+d}%"]
        for p in (edits.get("most_edited") or [])[:3]:
            lines.append(f"  {p['subject'][:48]:48s} "
                         f"{p['draft_words']}w -> {p['sent_words']}w "
                         f"({p['overlap_pct']}% kept)")
    return "\n".join(lines)


RESIDENT_PROMPT = """\
[style-analysis] Monthly voice measurement finished. It is a background
task, so finish whatever is in front of you first.

Report: {path}
Digest:
{digest}

Read the report, then compare it against the "Measured from sent mail"
section of ~/ea-system/docs/ea/drafting-voice.md. Those figures were
written from an earlier window and go stale.

`draft_edits.most_edited` in the report holds the full before/after text
of the drafts the user changed most before sending. Read those first. Corpus
rates say how the user writes; these say what the user takes OUT of what we write,
which is the thing the guide keeps getting wrong. They are kept for one
month only and this run purged the rest, so anything you do not draw out
of them now is gone.

Post a short note to the EA Slack channel: which figures still hold,
which have moved, and for anything that moved, whether it looks durable
or like a one-month artifact. Quote the specific line of the guide that
is now wrong and the number it should read.

Do NOT try to edit the guide. The source tree is read-only to you by
design, so the note IS the deliverable: the owner applies it from the
owner's own session.
"""


def _readable_path(path: Path) -> str:
    """Path as the resident can reach it.

    It runs as ea-bot, which cannot traverse the owner's home, and this
    module usually runs from cron as the owner, where EA_DIR resolves
    through the owner's symlink. `~/.ea/...` is correct for both.
    """
    text = str(path)
    for prefix in (str(EA_DIR), str(Path.home() / ".ea"), "/home/ea-bot/.ea"):
        if text.startswith(prefix):
            return "~/.ea" + text[len(prefix):]
    return text


def dispatch(report_path: Path, digest: str) -> str:
    """Hand the interpretation to the resident pane.

    Falls back to posting the digest directly: the measurement already
    cost a Graph fetch, and a report nobody sees is worse than one
    without commentary.
    """
    text = RESIDENT_PROMPT.format(path=_readable_path(report_path),
                                  digest=digest)
    try:
        import resident_bridge
        if resident_bridge.enabled():
            if not resident_bridge.pane_alive():
                os.system(f"{resident_bridge.PANE_SCRIPT} ensure "
                          ">/dev/null 2>&1")
            if resident_bridge.send(text):
                return "dispatched"
    except Exception as exc:
        print(f"  resident dispatch failed: {exc}", file=sys.stderr)
    try:
        import slack_notifier
        slack_notifier.post_to_slack("ea-assistant", digest)
        return "posted-direct"
    except Exception as exc:
        print(f"  slack post failed: {exc}", file=sys.stderr)
    return "undelivered"


def harvest_edits(msgs: list) -> dict:
    """Pair EA drafts against what the user actually sent, then purge.

    This is the only signal that shows what the user *changed* rather than
    what the user writes, so the pairs are the most valuable thing in the
    report. They are also a shadow copy of outgoing mail, so they live
    exactly one harvest cycle and no longer.
    """
    try:
        import draft_capture
    except Exception:
        return {}
    try:
        pairs = draft_capture.pair_with_sent(msgs)
    except Exception as exc:
        print(f"  draft pairing failed: {exc}", file=sys.stderr)
        pairs = []
    summary = {"pairs": len(pairs)}
    if pairs:
        summary["median_length_change_pct"] = int(statistics.median(
            [p["length_change_pct"] for p in pairs]))
        summary["untouched"] = sum(1 for p in pairs
                                   if p["overlap_pct"] >= 98)
        # Most-edited first: those are the ones worth reading.
        summary["most_edited"] = [
            {k: p[k] for k in ("subject", "to", "sent_at", "revisions",
                               "draft_words", "sent_words",
                               "length_change_pct", "overlap_pct",
                               "drafted", "sent")}
            for p in pairs[:5]]
    try:
        summary["purged"] = draft_capture.purge()
    except Exception:
        pass
    return summary


def cmd_run(args) -> int:
    if args.corpus:
        msgs = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
    else:
        msgs = fetch(args.days)
    rows = build_rows(msgs)
    if not rows:
        print(json.dumps({"error": "no usable messages",
                          "fetched": len(msgs)}))
        return 1
    previous = load_previous()
    if not args.own_domain:
        import institution
        args.own_domain = institution.own_domain()
    report = analyze(rows, args.own_domain)
    changes = drift(report, previous)
    report["drift"] = changes
    report["draft_edits"] = harvest_edits(msgs)
    path = save(report)
    digest = render(report, changes)
    result = {"messages": report["overall"]["messages"],
              "report": str(path), "drift": len(changes),
              "draft_pairs": report["draft_edits"].get("pairs", 0)}
    if args.dispatch:
        result["delivery"] = dispatch(path, digest)
    else:
        print(digest, file=sys.stderr)
    print(json.dumps(result))
    return 0


def cmd_show(args) -> int:
    report = load_previous()
    if not report:
        print(json.dumps({"error": "no report yet"}))
        return 1
    print(render(report, report.get("drift") or []))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="measure, snapshot, optionally dispatch")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--dispatch", action="store_true",
                   help="hand the report to the resident pane")
    p.add_argument("--corpus", help="read messages from a JSON dump")
    p.add_argument("--own-domain", default=None,
                   help="the user's own mail domain (default: from the profile)")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("show", help="print the last report")
    p.set_defaults(fn=cmd_show)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
