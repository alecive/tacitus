#!/usr/bin/env python3
"""The usage tracker reports the account the EA runs on.

What must hold: from any process that is not the resident, get_usage()
runs the tracker as the resident and returns what it prints; the
resident's own process (or an explicit token) reads its own credentials;
a failed delegation is an error, not a silent fallback to the caller's
account; the report names the account; the time helpers are sane.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import claude_usage as cu  # noqa: E402


def test_should_delegate_only_from_another_user_with_a_resident_present():
    assert cu.should_delegate("resident", "crh", Path("/home/ea-bot"))
    assert not cu.should_delegate("resident", cu.RESIDENT_USER, Path("/home/ea-bot"))
    assert not cu.should_delegate("resident", "crh", None)
    assert not cu.should_delegate("self", "crh", Path("/home/ea-bot"))


def test_delegation_returns_the_residents_payload_and_fails_closed():
    real = cu.subprocess.run
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"session_percent_used": 2.0, "session_reset": "2026-09-12T14:39:59+00:00",
             "daily_percent_used": 11.0, "daily_reset": "2026-09-17T11:59:59+00:00",
             "error": None, "account": "jo.ambrose4@example.com", "plan": "max"}))
    cu.subprocess.run = fake_run
    try:
        out = cu._usage_as_resident()
    finally:
        cu.subprocess.run = real
    assert out["account"] == "jo.ambrose4@example.com" and out["session_percent_used"] == 2.0
    cmd = seen[0]
    assert cmd[:4] == ["sudo", "-n", "-u", cu.RESIDENT_USER] and "--self" in cmd and "--json" in cmd
    assert cmd[5].endswith("claude_usage.py")

    def failing_run(cmd, **kw):
        return types.SimpleNamespace(returncode=1, stderr="sudo: a password is required", stdout="")
    cu.subprocess.run = failing_run
    try:
        out = cu._usage_as_resident()
    finally:
        cu.subprocess.run = real
    assert out["error"] and cu.RESIDENT_USER in out["error"]
    assert "session_percent_used" not in out, "never fall back to the caller's account"


def test_get_usage_with_a_token_reads_self_and_names_the_account():
    real_get = cu.requests.get
    real_info = cu.account_info
    cu.requests.get = lambda url, **kw: types.SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"five_hour": {"utilization": 11.0, "resets_at": "2026-09-12T19:19:59+00:00"},
                      "seven_day": {"utilization": 52.0, "resets_at": "2026-09-13T08:59:59+00:00"}})
    cu.account_info = lambda: {"account": "wren.fairbanks84@example.edu", "plan": "team"}
    try:
        u = cu.get_usage(token="tok")
    finally:
        cu.requests.get = real_get
        cu.account_info = real_info
    assert u["session_percent_used"] == 11.0 and u["daily_percent_used"] == 52.0
    assert u["account"].endswith("example.edu") and u["plan"] == "team" and u["error"] is None
    report = cu.format_usage_report(u)
    assert "*Account:* wren.fairbanks84@example.edu (team)" in report
    assert "Session (5h rolling):* 11% used" in report and "Weekly (7d rolling):* 52% used" in report


def test_time_helpers():
    now = datetime.now(timezone.utc)
    assert cu._relative_time((now + timedelta(hours=4, minutes=53, seconds=30)).isoformat()) == "4h 53m"
    assert cu._relative_time((now + timedelta(days=2, hours=3, seconds=30)).isoformat()) == "2d 3h"
    assert cu._relative_time((now - timedelta(minutes=1)).isoformat()) == "now"
    assert cu._relative_time("garbage") == "unknown"
    today = cu._absolute_time((now + timedelta(minutes=5)).isoformat())
    assert today and " " in today and not today.split()[0].isalpha()
    later = cu._absolute_time((now + timedelta(days=3)).isoformat())
    assert later.split()[0] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    footer = cu.format_usage_footer({"session_percent_used": 2.0, "session_reset": (now + timedelta(hours=1, seconds=30)).isoformat(),
                                     "daily_percent_used": 11.0, "daily_reset": (now + timedelta(days=5, seconds=30)).isoformat()}, 12.0)
    assert "2% session (resets in 1h 0m)" in footer and "11% weekly (resets in 5d)" in footer
    assert cu.format_usage_footer({"error": "x"}, 3.0) == "\n\n---\n_EA: 3s_"
    assert cu.format_usage_footer({"error": "x"}, 3.0, label="EA resident") == \
        "\n\n---\n_EA resident: 3s_"
    both = cu.format_usage_footer({"session_percent_used": 9.0, "daily_percent_used": 2.0},
                                  20.0, label="EA resident")
    assert both.startswith("\n\n---\n_EA resident: 20s · 9% session") and "2% weekly" in both


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL  {name}: {e!r}")
    print(f"{'OVERALL: [FAIL]' if failed else 'OVERALL: [PASS]'} ({failed} failed)")
    sys.exit(1 if failed else 0)
