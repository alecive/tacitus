"""Record a 'missed closure' event for the bug-squashing backlog.

When the user has to tell the bot in Slack that an item is done ('the paper
was submitted', 'handled X'), that counts as a bug in the autonomous
closure pipeline. Every such event is logged with the candidate signals
that ARE present in the data, so the closure_scrubber can grow to catch
them next time.

This script is called by the EA Slack bot's skill flow immediately
after closing a ledger item in response to a user-stated completion.

Usage:
    python3 preprocessor/log_missed_closure.py \\
        --ledger-id <id> \\
        --user-message "The paper was submitted." \\
        [--source chat-thread]

Writes one JSONL line per call to ~/.ea/logs/missed-closures.log:

    {
      "ts": "2026-06-01T18:00:00Z",
      "ledger_id": "workshop-paper-20260531",
      "user_message": "The paper was submitted.",
      "source": "chat-thread",
      "entry_summary": {
        "type": "deadline",
        "description": "Submit the workshop paper",
        "date_identified": "2026-05-18T04:31:04Z",
        "conversation_id": "",
        "source_message_id": false
      },
      "evidence_fetch": {"route": "graph_client", "inbox": "ok (312 messages)",
                         "sent": "ok (88 messages)", "calendar": "ok (41 events)",
                         "unevaluable_signals": {}, "complete": true},
      "evidence_complete": true,
      "scrubber_signals_present": [
        {"signal": "calendar_event_match", "would_fire": false,
         "evaluated": true, "reason": "not scheduling-shaped"},
        {"signal": "confirmation_email", "would_fire": false,
         "evaluated": true,
         "reason": "no inbound from confirmation sender with matching tokens"},
        ...
      ],
      "candidate_evidence": [
        "user sent email 2026-05-31T22:14Z subject='Workshop submission ready' (cross-conversation; current scrubber needs same conv_id)",
        ...
      ],
      "candidate_evidence_status": "searched: inbox, sent, calendar",
      "suggested_signal": "..."
    }

`would_fire` is TRI-state. `false` means the signal ran and said no;
`null` with `"evaluated": false` means it could not run. The difference is
the whole value of this log: an autopsy that reports all-false because the
mailbox was unreachable is worse than no autopsy, because it reads as
evidence that no signal existed. Same for `candidate_evidence`: an empty
list means nothing when `candidate_evidence_status` says "not searched".

The log is append-only and never read back automatically; a human
reviews it periodically. The act of writing the entry is the
deliverable: the line itself is the bug ticket.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import ledger as ledger_mod  # noqa: E402
import closure_scrubber as cs  # noqa: E402
import resident_io  # noqa: E402

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
LOG_PATH = EA_DIR / "logs" / "missed-closures.log"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summarize_entry(entry: dict) -> dict:
    """Light fingerprint of the ledger entry for log readability."""
    return {
        "type": entry.get("type"),
        "description": (entry.get("description") or "")[:200],
        "status": entry.get("status"),
        "date_identified": entry.get("date_identified"),
        "deadline": entry.get("deadline"),
        "conversation_id": entry.get("conversation_id") or "",
        "gmail_thread_id": entry.get("gmail_thread_id") or "",
        "has_source_message_id": bool(entry.get("source_message_id")),
    }


def _unevaluated(signal: str, why: str) -> dict:
    """A signal we could not run. Distinct from a signal that ran and said no.

    `would_fire` is None, never False: the whole point of this file is to
    tell "we looked and found nothing" apart from "we could not look".
    Downstream readers must treat None as no information.
    """
    return {"signal": signal, "would_fire": None, "evaluated": False,
            "reason": f"not evaluated: {why}"}


def _run_signals(entry: dict, *, inbox: list, sent: list,
                 calendar_events: list,
                 unevaluable: dict[str, str] | None = None) -> list[dict]:
    """Run each closure signal against this entry; record what fired or
    why it didn't. Same logic as scrub_closures' main loop, but per-entry
    and surfaced as structured data instead of just a boolean.

    `unevaluable` maps a signal name to the reason its inputs are missing.
    Those signals are reported as unevaluated rather than run against empty
    lists, which would manufacture a false "no signal existed" verdict."""
    unevaluable = unevaluable or {}
    sent_conv_times = cs.build_sent_conv_index(sent)
    inbox_by_conv = cs._index_inbox_by_conv(inbox)

    out: list[dict] = []

    if "user_replied_outlook" in unevaluable:
        out.append(_unevaluated("user_replied_outlook",
                                unevaluable["user_replied_outlook"]))
    else:
        fired, reason = cs._user_replied_outlook(entry, sent_conv_times)
        out.append({"signal": "user_replied_outlook",
                    "would_fire": fired, "evaluated": True,
                    "reason": reason or "no Sent message in same conversationId post-entry"})

    # Gmail signal: build sent indices for configured accounts so the
    # autopsy has parity with the live scrubber. Best-effort: a Gmail API
    # failure degrades to "index unavailable", never raises.
    gmail_reason = "no Gmail reply in entry's thread post-entry"
    gmail_fired: bool | None = False
    gmail_evaluated = True
    if not entry.get("gmail_thread_id"):
        gmail_reason = ("entry has no gmail_thread_id — Gmail signal "
                        "cannot apply (backfill candidate)")
    else:
        try:
            accounts = cs._load_user_gmail_accounts()
            if not accounts:
                gmail_reason = "no gmail_accounts configured in user-profile"
            else:
                indices = {a: cs._build_gmail_sent_index(a)
                           for a in accounts}
                gmail_fired, r = cs._user_replied_gmail(entry, indices)
                if gmail_fired:
                    gmail_reason = r
        except Exception as e:
            # An unreachable Gmail API is "could not look", not "nothing
            # there", the same distinction the rest of this function makes.
            gmail_fired = None
            gmail_evaluated = False
            gmail_reason = (f"not evaluated: Gmail index unavailable "
                            f"({type(e).__name__}: {e})")
    out.append({"signal": "user_replied_gmail",
                "would_fire": gmail_fired,
                "evaluated": gmail_evaluated,
                "reason": gmail_reason})

    if "latest_inbound_is_ack" in unevaluable:
        out.append(_unevaluated("latest_inbound_is_ack",
                                unevaluable["latest_inbound_is_ack"]))
    else:
        fired, reason = cs._latest_inbound_is_ack(entry, inbox_by_conv)
        out.append({"signal": "latest_inbound_is_ack",
                    "would_fire": fired, "evaluated": True,
                    "reason": reason or "no ack-shaped reply in same conversation"})

    if "confirmation_email" in unevaluable:
        out.append(_unevaluated("confirmation_email",
                                unevaluable["confirmation_email"]))
    else:
        fired, reason = cs._confirmation_email_satisfies(entry, inbox)
        out.append({"signal": "confirmation_email",
                    "would_fire": fired, "evaluated": True,
                    "reason": reason or "no inbound from confirmation sender with token overlap"})

    if "calendar_event_match" in unevaluable:
        out.append(_unevaluated("calendar_event_match",
                                unevaluable["calendar_event_match"]))
    else:
        fired, reason = cs._calendar_event_satisfies(entry, calendar_events)
        out.append({"signal": "calendar_event_match",
                    "would_fire": fired, "evaluated": True,
                    "reason": reason or ("not scheduling-shaped" if not cs._is_scheduling_entry(entry)
                                         else "no future event with attendee/subject overlap")})

    # The two conversation-id-free signals. Autopsy parity matters most
    # here: these exist precisely for entries the other signals cannot
    # see, so "no signal existed" must never be the answer for them.
    if "sender_reply_match" in unevaluable:
        out.append(_unevaluated("sender_reply_match",
                                unevaluable["sender_reply_match"]))
    else:
        fired, reason = cs._user_replied_to_correspondent(entry, sent)
        out.append({"signal": "sender_reply_match",
                    "would_fire": fired, "evaluated": True,
                    "reason": reason or "no Sent message to from_email with subject overlap"})

    if "project_member_completion" in unevaluable:
        out.append(_unevaluated("project_member_completion",
                                unevaluable["project_member_completion"]))
    else:
        # Build the index over the LIVE open set, not over this entry
        # alone: "sole open row of its project" is a statement about the
        # whole ledger, and a one-entry scope would answer yes for an
        # entry whose project has five other open rows. The entry is
        # added back because the autopsy usually runs after it closed.
        try:
            scope = list(ledger_mod.get_open_entries())
        except Exception:
            scope = []
        if not any(e.get("id") == entry.get("id") for e in scope):
            scope.append(entry)
        index = cs.build_project_index(scope)
        fired, reason = cs._project_member_completion(
            entry, inbox, project_index=index)
        out.append({"signal": "project_member_completion",
                    "would_fire": fired, "evaluated": True,
                    "reason": reason or (
                        "entry is not the sole open row of a project with "
                        "named members" if not index else
                        "no inbound from a project member whose subject "
                        "asserts completion")})

    return out


# ── Evidence fetch ─────────────────────────────────────────────────────
#
# Two routes, tried in order:
#
#   1. `import graph_client` — full Graph message shape, every signal
#      evaluable. Works owner-side.
#   2. `graph_actions.py` subprocess: the mediation-aware route
#      (mail_mediation.SAFE covers list-mail and calendar-view), which is
#      the ONLY route available to ea-bot. Its output is a flattened
#      summary shape, so some signals could stay unevaluable; those are
#      reported as unevaluated, never as "did not fire". A route-1
#      PermissionError swallowed into empty lists would assert that no
#      closure signal was available, which is a false autopsy.

# Signals that route 2 cannot answer, and why. Empty: list-mail rows
# carry conversationId, the join key for both same-conversation signals,
# and to/cc, which is what sender_reply_match joins on, so the CLI route
# evaluates every signal the import route does. Keep the mechanism: the
# next flattened field the signals need will land here.
_CLI_UNEVALUABLE: dict = {}


def _graph_cli_json(*args):
    """Run graph_actions.py and parse its JSON payload (object OR array).

    Same mediation-aware shell-out pattern as ledger._graph_cli; reproduced
    here because that helper only parses an object tail and list-mail
    prints an array. graph_client prints an MSAL banner to stdout ahead of
    the payload, hence the scan for the first bracket.
    """
    import subprocess
    cmd = [sys.executable, str(_HERE / "graph_actions.py"),
           *[str(a) for a in args]]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    out = p.stdout or ""
    idx = min([i for i in (out.find("["), out.find("{")) if i >= 0],
              default=-1)
    if idx < 0:
        return None, (f"no JSON from graph_actions (rc={p.returncode}): "
                      f"{(p.stderr or out)[:160]}")
    try:
        payload, _ = json.JSONDecoder().raw_decode(out[idx:])
    except Exception as e:
        return None, f"unparseable graph_actions output: {e}"
    if isinstance(payload, dict) and payload.get("error"):
        return None, str(payload["error"])[:200]
    return payload, ""


def _normalize_cli_messages(items) -> list:
    """graph_actions list-mail rows → the Graph message shape the signals read."""
    out: list = []
    for m in items or []:
        if not isinstance(m, dict) or "_note" in m:
            continue
        ts = m.get("received") or ""
        out.append({
            "id": m.get("id"),
            "subject": m.get("subject") or "",
            "bodyPreview": m.get("preview") or "",
            "receivedDateTime": ts,
            "sentDateTime": ts,
            "from": {"emailAddress": {"address": m.get("from") or ""}},
            # to/cc give sender_reply_match its join key on the mediated
            # route. Absent (an older graph_actions) they stay empty,
            # which is honest: the signal finds no recipient and says no.
            "toRecipients": [{"emailAddress": {"address": a}}
                             for a in (m.get("to") or []) if a],
            "ccRecipients": [{"emailAddress": {"address": a}}
                             for a in (m.get("cc") or []) if a],
            "conversationId": m.get("conversation_id") or "",
        })
    return out


def _normalize_cli_events(payload) -> list:
    """graph_actions calendar-view rows → the Graph event shape."""
    evs = payload.get("events") if isinstance(payload, dict) else payload
    out: list = []
    for e in evs or []:
        if not isinstance(e, dict):
            continue
        out.append({
            "id": e.get("id"),
            "subject": e.get("subject") or "",
            "start": {"dateTime": e.get("start") or ""},
            "organizer": {"emailAddress": {"name": e.get("organizer") or "",
                                           "address": ""}},
            # calendar-view returns an attendee COUNT, not names.
            "attendees": [],
        })
    return out


def _fetch_evidence(*, no_fetch: bool = False) -> tuple[list, list, list, dict]:
    """Collect inbox / sent / calendar evidence and say honestly how it went.

    Returns (inbox, sent, calendar_events, status). `status` always names
    the route taken and the per-source outcome, so a reader can tell an
    empty inbox list from an inbox that was never fetched."""
    status: dict = {"route": "", "inbox": "", "sent": "", "calendar": "",
                    "unevaluable_signals": {}, "complete": False}
    if no_fetch:
        status.update({
            "route": "skipped (--no-fetch)",
            "inbox": "not fetched", "sent": "not fetched",
            "calendar": "not fetched",
            "unevaluable_signals": {
                k: "evidence fetch skipped (--no-fetch)"
                for k in ("user_replied_outlook", "latest_inbound_is_ack",
                          "confirmation_email", "calendar_event_match",
                          "sender_reply_match",
                          "project_member_completion")},
        })
        return [], [], [], status

    inbox: list = []
    sent: list = []
    events: list = []

    try:
        import graph_client as graph
    except (ImportError, PermissionError) as e:
        graph = None  # type: ignore
        direct_err = f"{type(e).__name__}"
    else:
        direct_err = ""

    if graph is not None:
        status["route"] = "graph_client"
        try:
            inbox = graph.get_messages("inbox", days_back=14)
            status["inbox"] = f"ok ({len(inbox)} messages)"
        except Exception as e:
            status["inbox"] = f"unavailable: {type(e).__name__}: {e}"
        try:
            sent = graph.get_sent(days_back=14)
            status["sent"] = f"ok ({len(sent)} messages)"
        except Exception as e:
            status["sent"] = f"unavailable: {type(e).__name__}: {e}"
        try:
            events = graph.get_calendar_view(days_ahead=30)
            status["calendar"] = f"ok ({len(events)} events)"
        except Exception as e:
            status["calendar"] = f"unavailable: {type(e).__name__}: {e}"
    else:
        status["route"] = (f"graph_actions CLI (graph_client unavailable: "
                           f"{direct_err})")
        since = (datetime.now(timezone.utc) - timedelta(days=14)).strftime(
            "%Y-%m-%d")
        payload, err = _graph_cli_json("list-mail", "--folder", "inbox",
                                       "--top", "200", "--since", since)
        if err:
            status["inbox"] = f"unavailable: {err}"
        else:
            inbox = _normalize_cli_messages(payload)
            status["inbox"] = f"ok via CLI ({len(inbox)} messages)"
        payload, err = _graph_cli_json("list-mail", "--folder", "sentitems",
                                       "--top", "200", "--since", since)
        if err:
            status["sent"] = f"unavailable: {err}"
        else:
            sent = _normalize_cli_messages(payload)
            status["sent"] = f"ok via CLI ({len(sent)} messages)"
        payload, err = _graph_cli_json("calendar-view", "--days", "30")
        if err:
            status["calendar"] = f"unavailable: {err}"
        else:
            events = _normalize_cli_events(payload)
            status["calendar"] = f"ok via CLI ({len(events)} events)"
        status["unevaluable_signals"].update(_CLI_UNEVALUABLE)

    # A source that failed makes its dependent signals unevaluable.
    if status["inbox"].startswith("unavailable"):
        status["unevaluable_signals"].setdefault(
            "latest_inbound_is_ack", f"inbox {status['inbox']}")
        status["unevaluable_signals"].setdefault(
            "confirmation_email", f"inbox {status['inbox']}")
    if status["sent"].startswith("unavailable"):
        status["unevaluable_signals"].setdefault(
            "user_replied_outlook", f"sent {status['sent']}")
    if status["calendar"].startswith("unavailable"):
        status["unevaluable_signals"].setdefault(
            "calendar_event_match", f"calendar {status['calendar']}")

    status["complete"] = not status["unevaluable_signals"]
    return inbox, sent, events, status


def _candidate_evidence(entry: dict, *, inbox: list, sent: list,
                        calendar_events: list) -> list[str]:
    """Free-form pointers to data that *might* have signaled closure
    but didn't fit any existing signal cleanly. Used to suggest where
    a new scrubber rule could live."""
    desc_tokens = cs._extract_content_tokens(entry.get("description") or "")
    if not desc_tokens:
        return []
    desc_tokens -= cs._GENERIC_NOISE_TOKENS
    if not desc_tokens:
        return []
    entry_date = cs._entry_iso(entry)

    out: list[str] = []
    # User's sent emails (any conv) where subject tokens overlap entry
    for m in (sent or [])[:200]:
        sent_at = m.get("sentDateTime") or m.get("receivedDateTime") or ""
        if entry_date and sent_at and sent_at < entry_date:
            continue
        subj = m.get("subject") or ""
        if desc_tokens & cs._extract_content_tokens(subj):
            to_addrs = ", ".join(
                ((r.get("emailAddress") or {}).get("address") or "")
                for r in (m.get("toRecipients") or [])
            )
            out.append(f"sent {sent_at}: subject={subj[:80]!r} to={to_addrs[:80]}")

    # Inbound from non-confirmation senders that nonetheless reference entry
    for m in (inbox or [])[:200]:
        received = m.get("receivedDateTime") or ""
        if entry_date and received and received < entry_date:
            continue
        subj = m.get("subject") or ""
        if not (desc_tokens & cs._extract_content_tokens(subj)):
            continue
        addr = ((m.get("from") or {}).get("emailAddress", {}).get("address", "") or "")
        # Skip the ones already covered by existing signals.
        if any(s in addr.lower() for s in cs.CONFIRMATION_SENDER_SUBSTRINGS):
            continue
        out.append(f"inbox {received}: subject={subj[:80]!r} from={addr}")

    # Calendar events past the entry date with subject/attendee overlap
    for ev in (calendar_events or [])[:100]:
        subj = ev.get("subject") or ""
        att_text = " ".join(
            ((a.get("emailAddress") or {}).get("name") or "") for a in (ev.get("attendees") or [])
        )
        ev_tokens = cs._extract_content_tokens(subj + " " + att_text)
        overlap = (desc_tokens & ev_tokens) - cs._GENERIC_NOISE_TOKENS
        if not overlap:
            continue
        start = ((ev.get("start") or {}).get("dateTime") or "")
        out.append(f"calendar event start={start}: subject={subj[:80]!r} overlap={sorted(overlap)[:5]}")

    return out[:10]  # cap so the log line stays readable


def log_missed_closure(*, ledger_id: str, user_message: str,
                       source: str = "chat",
                       no_fetch: bool = False) -> dict:
    """Build + write a missed-closure record. Returns the record dict.

    Best-effort: catches all exceptions internally and writes a minimal
    record on failure (the user shouldn't lose their chat-reply event
    just because Graph is down)."""
    record: dict = {
        "ts": _now_iso(),
        "ledger_id": ledger_id,
        "user_message": user_message,
        "source": source,
    }

    try:
        entry = ledger_mod.get_entry(ledger_id)
    except Exception as e:
        entry = None
        record["entry_lookup_error"] = f"{type(e).__name__}: {e}"
    if not entry:
        record["entry_summary"] = None
        record["scrubber_signals_present"] = []
        record["candidate_evidence"] = []
        record["_logged"] = _append(record)
        return record

    record["entry_summary"] = _summarize_entry(entry)

    inbox, sent, calendar_events, fetch = _fetch_evidence(no_fetch=no_fetch)
    record["evidence_fetch"] = fetch
    record["evidence_complete"] = bool(fetch.get("complete"))
    # Legacy field kept for readers of older lines: one entry per source
    # that could not be fetched.
    fetch_errors = [f"{k}: {fetch[k]}" for k in ("inbox", "sent", "calendar")
                    if str(fetch.get(k, "")).startswith("unavailable")]
    if fetch_errors:
        record["fetch_errors"] = fetch_errors

    unevaluable = dict(fetch.get("unevaluable_signals") or {})

    try:
        record["scrubber_signals_present"] = _run_signals(
            entry, inbox=inbox, sent=sent, calendar_events=calendar_events,
            unevaluable=unevaluable,
        )
    except Exception as e:
        record["signals_error"] = f"{type(e).__name__}: {e}"
        record["scrubber_signals_present"] = []

    # An evidence search that never ran must not be recorded as a search
    # that came back empty. closure_metrics' recall path reads this list,
    # so an unqualified [] here reads as "the data held nothing".
    searched = [src for src in ("inbox", "sent", "calendar")
                if str(fetch.get(src, "")).startswith("ok")]
    if not searched:
        record["candidate_evidence"] = []
        record["candidate_evidence_status"] = (
            f"not searched — {fetch.get('route') or 'no evidence route'}")
    else:
        try:
            record["candidate_evidence"] = _candidate_evidence(
                entry, inbox=inbox, sent=sent, calendar_events=calendar_events
            )
        except Exception as e:
            record["evidence_error"] = f"{type(e).__name__}: {e}"
            record["candidate_evidence"] = []
            record["candidate_evidence_status"] = f"errored: {e}"
        else:
            record["candidate_evidence_status"] = (
                "searched: " + ", ".join(searched)
                + ("" if record["evidence_complete"]
                   else "; partial (see evidence_fetch)"))

    signals = record["scrubber_signals_present"]
    evaluated = [s for s in signals if s.get("evaluated", True)]
    unevaluated = [s for s in signals if not s.get("evaluated", True)]

    # Heuristic suggestion: if no signals fired but candidate evidence
    # exists, name the most-relevant axis.
    suggested = ""
    if signals and not any(s.get("would_fire") for s in signals):
        if record["candidate_evidence"]:
            ev0 = record["candidate_evidence"][0]
            if ev0.startswith("sent"):
                suggested = ("Consider broadening Outlook-reply signal to "
                             "match on subject-token overlap across "
                             "conversations (currently same-conversation "
                             "only).")
            elif ev0.startswith("calendar"):
                suggested = ("Consider expanding calendar-match signal to "
                             "this entry type / loosening attendee-name "
                             "matching.")
            elif ev0.startswith("inbox"):
                suggested = ("Consider adding this sender domain to "
                             "CONFIRMATION_SENDER_SUBSTRINGS if it's a "
                             "confirmation system.")
        elif unevaluated:
            # The honest branch. Asserting "no detectable signal" here
            # would be a conclusion drawn from a failed fetch.
            names = ", ".join(s["signal"] for s in unevaluated)
            suggested = (
                f"INCONCLUSIVE — evidence unavailable, {len(unevaluated)} of "
                f"{len(signals)} signals not evaluated ({names}). Route: "
                f"{fetch.get('route')}. This record is NOT evidence that no "
                f"closure signal existed. Re-run log_missed_closure.py "
                f"--ledger-id {ledger_id} once mail access is restored.")
        elif evaluated:
            suggested = ("No detectable signal in data — this may be a "
                         "genuine 'user decided to pass' case best handled "
                         "by auto-snooze rather than auto-close.")
    record["suggested_signal"] = suggested

    record["_logged"] = _append(record)
    return record


def _append(record: dict) -> bool:
    """Append one record. Returns whether it landed.

    Never raises: a logging failure must not break the close that
    triggered it. It is not silent either. This log is the only feedback
    signal the project has on closure-scrubber gaps (see CLAUDE.md,
    "User-stated closure is a bug"), so a write that quietly does nothing
    would retire the feedback loop while every caller still reports
    success. A permission failure on the log file is the usual cause.

    The log is in the shared ~/.ea/logs and both the resident and owner
    processes (the dashboard's item feedback, the pre-send audit) write it,
    so the append goes through resident_io: an owner process appends with
    the resident's access.
    """
    try:
        line = json.dumps(record, default=str) + "\n"
        ok = resident_io.append_text(LOG_PATH, line)
        reason = "see the resident_io line above"
    except Exception as e:
        ok, reason = False, f"{type(e).__name__}: {e}"
    if not ok:
        print(f"WARNING: missed-closure record NOT logged ({reason}). "
              f"Path: {LOG_PATH}", file=sys.stderr)
    return ok


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    ap.add_argument("--ledger-id", required=True)
    ap.add_argument("--user-message", required=True,
                    help="The user's chat message that triggered the close.")
    ap.add_argument("--source", default="chat",
                    help="Where the closure-trigger originated.")
    ap.add_argument("--no-fetch", action="store_true",
                    help="Skip Graph API calls; only emit the entry "
                         "lookup + structural fields.")
    ap.add_argument("--quiet", action="store_true",
                    help="Don't print the record to stdout; just append "
                         "to the log.")
    args = ap.parse_args()

    record = log_missed_closure(
        ledger_id=args.ledger_id,
        user_message=args.user_message,
        source=args.source,
        no_fetch=args.no_fetch,
    )

    if not args.quiet:
        print(json.dumps(record, indent=2, default=str))
    # Non-zero when the record did not land, so a caller (or a cron) can
    # tell a logged closure from a lost one.
    return 0 if record.get("_logged", True) else 1


if __name__ == "__main__":
    sys.exit(_main())
