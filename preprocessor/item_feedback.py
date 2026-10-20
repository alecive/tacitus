#!/usr/bin/env python3
"""Item feedback: what the user says about a ledger item, recorded where
it teaches something.

Closing an item from the dashboard only changes the ledger. A
disposition also says WHY the item was wrong, and that reaches the two
places that learn from it: the missed-closure log (the scrubber's bug
backlog, CLAUDE.md "User-stated closure is a bug") and the preferences
store (what to surface at all).

One disposition per click, each with an optional note:

  done        the user finished it. Close through `ledger.py close` (the
              email is archived, the note lands in last_action) and log a
              missed closure: a human had to say it, so a signal is missing.
  not_mine    not the user's obligation, or never open. Close without
              archiving the mail, and add a calibration line to the
              preferences store so the briefing learns the shape.
  snooze      park it until a date: ledger status `snoozed`, and the source
              email snoozed out of the inbox when the row has one.
  still_open  the user confirms it is open. Stamp `confirmed_open_at` so
              the pre-send audit stops asking for CONFIRMED_OPEN_DAYS, and
              note it in last_action.

Every disposition is appended to ~/.ea/logs/item-feedback.jsonl. The
dashboard's /api/op routes {"op": "disposition", ...} here; a Slack or
App Home caller can use the same function.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import ledger as ledger_mod  # noqa: E402
import resident_io  # noqa: E402

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
FEEDBACK_LOG = EA_DIR / "logs" / "item-feedback.jsonl"

KINDS = ("done", "not_mine", "snooze", "still_open")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(cmd: list[str], timeout: int = 120) -> tuple[bool, dict, str]:
    """Run one of our CLIs; (ok, last JSON line, error text)."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out: dict = {}
    for line in reversed((r.stdout or "").strip().splitlines()):
        try:
            out = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    ok = r.returncode == 0 and not out.get("error")
    err = "" if ok else (out.get("error") or (r.stderr or "").strip()[-300:] or "failed")
    return ok, out, err


def _ledger(*args: str) -> tuple[bool, dict, str]:
    return _run([sys.executable, str(_HERE / "ledger.py"), *args])


def _append(record: dict, log_path: Path | None = None) -> bool:
    """Append the disposition to the feedback log. The dashboard runs this
    as the owner and the log is in the shared ~/.ea/logs, so the append is
    made with the resident's access (resident_io); a failure costs one
    stderr line and returns False."""
    path = log_path or FEEDBACK_LOG
    return resident_io.append_text(path, json.dumps(record, ensure_ascii=False) + "\n")


def _valid_date(s: str) -> str:
    try:
        return date.fromisoformat((s or "").strip()[:10]).isoformat()
    except ValueError:
        return ""


def apply_disposition(ledger_id: str, kind: str, note: str = "",
                      until: str = "", *, source: str = "dashboard",
                      fetch_for_log: bool = True, snooze_mail: bool = True,
                      log_path: Path | None = None) -> dict:
    """Apply one disposition. Returns {ok, kind, ledger_id, ...} or
    {ok: False, error}. Side effects go through the CLIs, never raw SQL."""
    ledger_id = (ledger_id or "").strip()
    kind = (kind or "").strip().lower()
    note = " ".join((note or "").split())[:400]
    if not ledger_id:
        return {"ok": False, "error": "ledger_id required"}
    if kind not in KINDS:
        return {"ok": False, "error": f"kind must be one of {', '.join(KINDS)}"}
    entry = ledger_mod.get_entry(ledger_id)
    if not entry:
        return {"ok": False, "error": f"no ledger item {ledger_id}"}
    today = date.today().isoformat()
    desc = (entry.get("description") or "")[:120]
    result: dict = {"ok": True, "kind": kind, "ledger_id": ledger_id}

    if kind == "done":
        ok, _, err = _ledger("close", "--id", ledger_id,
                             "--user-said", note or f"marked done from the {source}",
                             "--note", f"done, per user ({source}){': ' + note if note else ''}")
        if not ok:
            return {"ok": False, "error": err}
        # A human had to say it: that is a scrubber gap, on the record.
        try:
            import log_missed_closure as lmc
            lmc.log_missed_closure(
                ledger_id=ledger_id,
                user_message=note or f"marked done from the {source}",
                source=source, no_fetch=not fetch_for_log)
            result["missed_closure_logged"] = True
        except Exception as e:
            result["missed_closure_logged"] = False
            print(f"item_feedback: missed-closure log failed: {e}", file=sys.stderr)

    elif kind == "not_mine":
        ok, _, err = _ledger("close", "--id", ledger_id, "--skip-archive",
                             "--user-said", note or f"not my obligation, from the {source}",
                             "--note", f"not my obligation, per user ({source})"
                                       f"{': ' + note if note else ''}")
        if not ok:
            return {"ok": False, "error": err}
        line = (f"{today}: not the user's obligation, said via {source}: "
                f"{desc!r}" + (f" ({note})" if note else ""))
        ok, _, err = _run([sys.executable, str(_HERE / "preferences.py"), "calibrate",
                           "--scope", "briefing", "--note", line])
        result["calibration_recorded"] = ok
        if not ok:
            print(f"item_feedback: calibration failed: {err}", file=sys.stderr)

    elif kind == "snooze":
        day = _valid_date(until)
        if not day:
            return {"ok": False, "error": "snooze needs a date (YYYY-MM-DD)"}
        if day <= today:
            return {"ok": False, "error": "snooze date must be after today"}
        ok, _, err = _ledger("update", "--id", ledger_id, "--status", "snoozed",
                             "--snooze-until", day,
                             "--last-action", f"{today}: snoozed until {day} by user ({source})"
                                              f"{': ' + note if note else ''}")
        if not ok:
            return {"ok": False, "error": err}
        result["until"] = day
        mid = entry.get("source_message_id") or ""
        if mid and snooze_mail and not entry.get("email_archived"):
            ok, _, err = _run([sys.executable, str(_HERE / "graph_actions.py"),
                               "snooze-message", "--message-id", mid, "--until", day,
                               "--description", desc[:80], "--ledger-id", ledger_id])
            result["mail_snoozed"] = ok
            if not ok:
                print(f"item_feedback: mail snooze failed: {err}", file=sys.stderr)

    elif kind == "still_open":
        stamp = _now_iso()
        ok, _, err = _ledger("update", "--id", ledger_id,
                             "--set", f"confirmed_open_at={stamp}",
                             "--last-action", f"{today}: user confirmed still open ({source})"
                                              f"{': ' + note if note else ''}")
        if not ok:
            return {"ok": False, "error": err}
        result["confirmed_open_at"] = stamp

    _append({"ts": _now_iso(), "ledger_id": ledger_id, "kind": kind,
             "note": note, "until": result.get("until", ""), "source": source,
             "description": desc}, log_path)
    return result


def apply_op(op: dict, **kw) -> dict:
    """The dashboard's {"op": "disposition", ...} body."""
    return apply_disposition(op.get("ledger_id", ""), op.get("kind", ""),
                             op.get("note", ""), op.get("until", ""), **kw)


if __name__ == "__main__":  # a tiny CLI for a shell or a Slack lane
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--id", required=True)
    ap.add_argument("--kind", required=True, choices=KINDS)
    ap.add_argument("--note", default="")
    ap.add_argument("--until", default="")
    ap.add_argument("--source", default="cli")
    a = ap.parse_args()
    print(json.dumps(apply_disposition(a.id, a.kind, a.note, a.until, source=a.source)))
