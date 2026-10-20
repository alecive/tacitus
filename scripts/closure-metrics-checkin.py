#!/usr/bin/env python3
"""One-time closure-signal check-in after the 2026-08-22 scrubber changes.

Two closure signals changed then and neither had run in production:
calendar_event_match was wired to real calendar events for the first
time, and _user_identity_tokens was tightened (the user's own address
and the user's own domain label stopped counting as overlap), which
also touches the live confirmation_email signal.

This script posts one 7-day closure-metrics report to Slack with review
instructions. It is idempotent: a marker file makes every later cron
firing a silent no-op, so the dated cron line is harmless if left in
place and nothing has to rewrite a crontab.

Cron (owner, one-shot by marker): 0 9 26 8 *  python3 .../closure-metrics-checkin.py
"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))

# REPO makes `from preprocessor import slack_notifier` importable; the
# preprocessor directory itself makes the FLAT imports inside those
# modules work. slack_notifier does `import quiet_hours`, so with only
# REPO on the path the quiet-hours gate is skipped and the post goes
# straight through.
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "preprocessor"))

import owner_trust  # noqa: E402

# Owner-only state (no resident process reads it): the owner-private
# directory in an owner process, the EA tree's state directory otherwise.
# LEGACY_MARKER is where an earlier run left it.
_STATE = owner_trust.private_dir() if owner_trust.is_owner_process() else EA_DIR / "state"
MARKER = _STATE / "closure-metrics-checkin.done"
LEGACY_MARKER = EA_DIR / "state" / "closure-metrics-checkin.done"


def _done() -> bool:
    return MARKER.exists() or LEGACY_MARKER.exists()


def _mark_done() -> None:
    if owner_trust.is_owner_process():
        owner_trust.write_owner_file(MARKER, "posted\n", mode=0o600)
    else:
        import atomic_io
        atomic_io.write_text(MARKER, "posted\n")


def main() -> int:
    if _done():
        return 0

    proc = subprocess.run(
        [sys.executable, str(REPO / "preprocessor" / "closure_metrics.py"),
         "report", "--days", "7", "--md"],
        capture_output=True, text=True, timeout=120)
    report = (proc.stdout or "").strip() or f"(report failed: {proc.stderr.strip()[:400]})"
    if len(report) > 2600:
        report = report[:2600] + "\n... (truncated; run the report command below for the rest)"

    text = (
        ":memo: *Closure-signal check-in* (one-time, scheduled 2026-08-22)\n"
        "Two closure signals changed on Aug 22. This is their first week in "
        "production. Please review:\n\n"
        f"{report}\n\n"
        "*What to check*\n"
        "1. Rows with signal `calendar_event_match`. This signal is new. "
        "For each closure, confirm the item was really done.\n"
        "2. The `confirmation_email` closure rate. The identity-token fix "
        "can only remove matches. A drop to zero would mean it now "
        "suppresses real confirmations.\n\n"
        "*What to do*\n"
        "- A false closure: reopen it (`ledger.py update --id <id> --status "
        "open`) and tell the EA which signal was wrong. The EA must run the "
        "backtest before it changes the signal.\n"
        "- All genuine: nothing to do. You can delete the check-in line "
        "from `~/crontab.master` (marked one-time) and reinstall "
        "with `crontab ~/crontab.master`, or leave it: this script "
        "is now a no-op either way.\n"
        "- Re-run any time: `python3 /opt/ea-system/preprocessor/"
        "closure_metrics.py report --days 7 --md`"
    )

    from preprocessor import slack_notifier
    slack_notifier.post_to_slack("personal", text)
    _mark_done()
    return 0


if __name__ == "__main__":
    sys.exit(main())
