#!/usr/bin/env python3
"""Fetch Claude plan usage (session window + weekly) from the Anthropic API.

Uses the OAuth token from Claude Code's credentials file
(~/.claude/.credentials.json) and reports the account it belongs to.

Whose usage
-----------
The Slack bot runs as the owner; the EA's Claude runs as the resident
user (ea-bot), on its own Claude account. The quota the EA consumes is the
resident's, so `get_usage()` reports THAT account by default: when the
caller is not the resident, it runs this same script as the resident
(`sudo -n -u ea-bot … --self --json`, the bot's existing privilege) and
returns what it prints. Reading the owner's own credentials from the bot
process reported the owner's other account, whose percentages and reset
times had nothing to do with the EA's work.

  from claude_usage import get_usage, format_usage_footer
  usage = get_usage()                  # the resident account
  usage = get_usage(account="self")    # this process's own credentials

  python3 claude_usage.py              # resident account, JSON + footer + report
  python3 claude_usage.py --self --json
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

RESIDENT_USER = os.environ.get("EA_USAGE_USER", "ea-bot")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DELEGATE_TIMEOUT_S = 25


def _home() -> Path:
    """The invoking user's home from passwd, not $HOME: under sudo -u the
    environment can still carry the caller's HOME."""
    try:
        return Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError:
        return Path.home()


CREDENTIALS_FILE = _home() / ".claude" / ".credentials.json"
ACCOUNT_FILE = _home() / ".claude.json"


def _current_user() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return ""


def _resident_home() -> Path | None:
    try:
        return Path(pwd.getpwnam(RESIDENT_USER).pw_dir)
    except KeyError:
        return None


def _load_token() -> str | None:
    """Read the OAuth access token from Claude Code credentials. Without a
    credentials file (macOS keeps logins in the keychain, which the resident
    has none of; docs/macos-claude-binary.md), the long-lived
    CLAUDE_CODE_OAUTH_TOKEN the resident's Claude runs on: from the
    environment, else from ~/.claude/settings.json's env block."""
    try:
        data = json.loads(CREDENTIALS_FILE.read_text())
        return data["claudeAiOauth"]["accessToken"]
    except (FileNotFoundError, PermissionError, KeyError, json.JSONDecodeError) as e:
        err = e
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not tok:
        try:
            env = json.loads((_home() / ".claude" / "settings.json").read_text()).get("env") or {}
            tok = env.get("CLAUDE_CODE_OAUTH_TOKEN", "") if isinstance(env, dict) else ""
        except (OSError, ValueError, AttributeError):
            tok = ""
    if tok:
        return tok
    print(f"claude_usage: could not load token: {err}", file=sys.stderr)
    return None


def account_info() -> dict:
    """{"account": email, "plan": subscription type} for this user's Claude
    login, each "" when unknown. Best-effort; never raises."""
    email = plan = ""
    try:
        acct = json.loads(ACCOUNT_FILE.read_text()).get("oauthAccount") or {}
        email = acct.get("emailAddress") or ""
    except Exception:
        pass
    try:
        plan = json.loads(CREDENTIALS_FILE.read_text())["claudeAiOauth"].get("subscriptionType") or ""
    except Exception:
        pass
    return {"account": email, "plan": plan}


def should_delegate(account: str, current_user: str, resident_home) -> bool:
    """Run as the resident when the resident's usage is wanted and this is
    not the resident's process (and the resident exists on this host)."""
    return (account == "resident" and current_user != RESIDENT_USER
            and resident_home is not None)


def _usage_as_resident() -> dict:
    """This script, run as the resident, reading the resident's own token."""
    home = _resident_home()
    script = (home / "ea-system" / "preprocessor" / "claude_usage.py") if home else None
    if script is None or not script.exists():
        script = Path(__file__).resolve()
    # The resident's own interpreter: its venv has requests; the bare python3
    # on a macOS host does not. From /, which the resident can always enter.
    py = (home / "ea-system" / ".venv" / "bin" / "python3") if home else None
    py = str(py) if py is not None and py.exists() else "python3"
    cmd = ["sudo", "-n", "-u", RESIDENT_USER, py, str(script), "--self", "--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DELEGATE_TIMEOUT_S, cwd="/")
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": f"could not read the {RESIDENT_USER} account's usage ({e})"}
    if r.returncode != 0 or not r.stdout.strip():
        hint = (r.stderr or "").strip()[-200:] or f"exit {r.returncode}"
        return {"error": f"could not read the {RESIDENT_USER} account's usage ({hint})"}
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": f"{RESIDENT_USER} usage returned no JSON"}
    return data if isinstance(data, dict) else {"error": "unexpected usage payload"}


def get_usage(token: str | None = None, account: str = "resident") -> dict:
    """Current claude.ai plan usage.

    account: "resident" (default) is the account the EA's Claude runs as;
    "self" is this process's own credentials. An explicit token always
    means "self".

    Returns dict with keys:
      session_percent_used  (float, 0-100)
      session_reset         (ISO timestamp str)
      daily_percent_used    (float, 0-100)      # the 7-day window
      daily_reset           (ISO timestamp str)
      account, plan         (str; who this usage belongs to)
      error                 (str or None)
    """
    if token is None and should_delegate(account, _current_user(), _resident_home()):
        return _usage_as_resident()
    if not token:
        token = _load_token()
    if not token:
        return {"error": "No OAuth token available", **account_info()}

    try:
        resp = requests.get(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "Accept": "application/json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": str(e), **account_info()}

    five_hour = data.get("five_hour") or {}
    seven_day = data.get("seven_day") or {}

    return {
        "session_percent_used": five_hour.get("utilization"),
        "session_reset": five_hour.get("resets_at"),
        "daily_percent_used": seven_day.get("utilization"),
        "daily_reset": seven_day.get("resets_at"),
        "error": None,
        **account_info(),
    }


def _relative_time(iso_str: str) -> str:
    """Convert ISO timestamp to relative string like '3h 12m' or '2d 4h'."""
    try:
        reset = datetime.fromisoformat(iso_str)
        now = datetime.now(timezone.utc)
        delta = reset - now
        total_sec = int(delta.total_seconds())

        if total_sec <= 0:
            return "now"

        hours, remainder = divmod(total_sec, 3600)
        minutes = remainder // 60

        days, rem_hours = divmod(hours, 24)
        if days > 0:
            if rem_hours > 0:
                return f"{days}d {rem_hours}h"
            return f"{days}d"

        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return "unknown"


def _absolute_time(iso_str: str) -> str:
    """Convert ISO timestamp to local time like '6:30 PM' (with the weekday
    when it is not today)."""
    try:
        reset = datetime.fromisoformat(iso_str).astimezone()
        today = datetime.now(reset.tzinfo).date()
        clock = reset.strftime("%-I:%M %p")
        if reset.date() == today:
            return clock
        return f"{reset.strftime('%a')} {clock}"
    except Exception:
        return ""


def format_usage_footer(usage: dict, elapsed_sec: float, label: str = "EA") -> str:
    """Format usage into a chat footer line; `label` names the lane
    ("EA resident" for the resident pane).

    Returns e.g.:
      '\n\n---\n_EA: 45s · 10% session (resets in 3h 12m) · 12% weekly (resets in 2d 4h)_'
    On error, falls back to just elapsed time.
    """
    base = f"{label}: {elapsed_sec:.0f}s"

    if usage.get("error"):
        return f"\n\n---\n_{base}_"

    parts = [base]

    sess = usage.get("session_percent_used")
    if sess is not None:
        reset_rel = _relative_time(usage.get("session_reset", ""))
        parts.append(f"{sess:.0f}% session (resets in {reset_rel})")

    weekly = usage.get("daily_percent_used")
    if weekly is not None:
        reset_rel = _relative_time(usage.get("daily_reset", ""))
        parts.append(f"{weekly:.0f}% weekly (resets in {reset_rel})")

    return f"\n\n---\n_{' · '.join(parts)}_"


def format_usage_report(usage: dict) -> str:
    """Format a detailed usage report for the 'usage' Slack command. Names
    the account, so a number that looks wrong can be checked against the
    right Claude login."""
    if usage.get("error"):
        return f":warning: Usage check failed: {usage['error']}"

    lines = [":bar_chart: *Claude Usage*", ""]

    who = usage.get("account") or ""
    plan = usage.get("plan") or ""
    if who or plan:
        label = who + (f" ({plan})" if plan else "")
        lines.append(f"*Account:* {label} · the login the EA runs on")

    sess = usage.get("session_percent_used")
    if sess is not None:
        reset_abs = _absolute_time(usage.get("session_reset", ""))
        reset_rel = _relative_time(usage.get("session_reset", ""))
        lines.append(f"*Session (5h rolling):* {sess:.0f}% used · resets {reset_abs} (in {reset_rel})")

    weekly = usage.get("daily_percent_used")
    if weekly is not None:
        reset_abs = _absolute_time(usage.get("daily_reset", ""))
        reset_rel = _relative_time(usage.get("daily_reset", ""))
        lines.append(f"*Weekly (7d rolling):* {weekly:.0f}% used · resets {reset_abs} (in {reset_rel})")

    lines.append("")
    lines.append("_Checked just now_")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude plan usage")
    ap.add_argument("--self", dest="self_only", action="store_true",
                    help="this process's own credentials (no delegation)")
    ap.add_argument("--json", action="store_true", help="JSON only")
    a = ap.parse_args()
    usage = get_usage(account="self" if a.self_only else "resident")
    if a.json:
        print(json.dumps(usage))
        return 0 if not usage.get("error") else 1
    print(json.dumps(usage, indent=2))
    print()
    print("Footer:", format_usage_footer(usage, 12.3))
    print()
    print("Report:")
    print(format_usage_report(usage))
    return 0


if __name__ == "__main__":
    sys.exit(main())
