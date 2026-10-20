#!/usr/bin/env python3
"""
Integration tests for poll_filler.py.

Requires real poll URLs. Set environment variables before running:
    export TEST_WHEN2MEET_URL="https://www.when2meet.com/?XXXXX-YYYYY"
    export TEST_WHENISGOOD_URL="https://whenisgood.net/XXXXX"
    export TEST_DOODLE_URL="https://doodle.com/meeting/participate/id/XXXXX"

    python3 tests/test_poll_filler.py [--visible]

Tests run headless by default. Use --visible to watch the browser.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "preprocessor" / "poll_filler.py"
TEST_NAME = "EA Test Bot"
VISIBLE_FLAG = "--visible" if "--visible" in sys.argv else ""

passed = 0
failed = 0
skipped = 0


def run_poll_cmd(*args) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPT)] + list(args)
    if VISIBLE_FLAG:
        cmd.append(VISIBLE_FLAG)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def assert_eq(label, actual, expected):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(label, value):
    if not value:
        raise AssertionError(f"{label}: expected truthy, got {value!r}")


# -------------------------------------------------------------------
# URL validation tests (no browser needed)
# -------------------------------------------------------------------

def test_url_allowlist_rejects_bad_urls():
    """Verify that non-allowlisted URLs are rejected."""
    bad_urls = [
        "https://evil.com/steal-data",
        "http://when2meet.com/?12345-AbCdE",  # http, not https
        "https://when2meet.com.evil.com/?12345",
        "https://google.com",
        "https://when2meet.com/",  # missing event ID
    ]
    for url in bad_urls:
        result = run_poll_cmd("read-poll", "--url", url)
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        assert_true(f"reject {url}", "error" in data or result.returncode != 0)
    print("  PASS: URL allowlist rejects bad URLs")


def test_url_allowlist_accepts_good_urls():
    """Verify allowlist patterns match real URL formats (validation only, no browser)."""
    sys.path.insert(0, str(SCRIPT.parent))
    from poll_filler import ALLOWED_URL_PATTERNS

    good_urls = [
        "https://www.when2meet.com/?24681357-Abcde",
        "https://when2meet.com/?12345-AbCdE",
        "https://whenisgood.net/abc123",
        "https://www.whenisgood.net/test-event",
        "https://www.whenisgood.net/test-event/results",
        "https://doodle.com/meeting/participate/id/abc123def",
        "https://doodle.com/poll/abc123def",
    ]
    for url in good_urls:
        matched = any(p.match(url) for p in ALLOWED_URL_PATTERNS)
        assert_true(f"accept {url}", matched)
    print("  PASS: URL allowlist accepts valid URLs")


# -------------------------------------------------------------------
# When2Meet tests
# -------------------------------------------------------------------

def test_when2meet_read():
    """read-poll extracts slots from When2Meet."""
    url = os.environ.get("TEST_WHEN2MEET_URL")
    if not url:
        return "skip"

    result = run_poll_cmd("read-poll", "--url", url)
    assert_eq("exit code", result.returncode, 0)
    data = json.loads(result.stdout)

    assert_eq("platform", data["platform"], "when2meet")
    assert_true("has event_name", data["event_name"])
    assert_true("has slots", len(data["slots"]) > 0)
    assert_true("slots have start", all("start" in s for s in data["slots"]))
    assert_true("slots have slot_id", all("slot_id" in s for s in data["slots"]))
    assert_true("has dates", len(data["dates"]) > 0)
    assert_true("has slot_duration", data["slot_duration_minutes"] > 0)

    print(f"  PASS: When2Meet read — {len(data['slots'])} slots, "
          f"name={data['event_name']!r}")
    return data


def test_when2meet_fill():
    """fill-poll marks slots on When2Meet."""
    url = os.environ.get("TEST_WHEN2MEET_URL")
    if not url:
        return "skip"

    # Read first to get slot_ids
    read_result = run_poll_cmd("read-poll", "--url", url)
    slots = json.loads(read_result.stdout)["slots"]

    # Fill first 3 slots
    ids = json.dumps([s["slot_id"] for s in slots[:3]])
    fill_result = run_poll_cmd("fill-poll", "--url", url,
                               "--name", TEST_NAME, "--available", ids)
    assert_eq("exit code", fill_result.returncode, 0)
    data = json.loads(fill_result.stdout)

    assert_eq("filled count", data["filled"], 3)
    assert_true("screenshot exists", Path(data["screenshot"]).exists())

    print(f"  PASS: When2Meet fill — {data['filled']}/{data['total']} slots, "
          f"screenshot={data['screenshot']}")
    return data


# -------------------------------------------------------------------
# WhenIsGood tests
# -------------------------------------------------------------------

def test_whenisgood_read():
    """read-poll extracts slots from WhenIsGood."""
    url = os.environ.get("TEST_WHENISGOOD_URL")
    if not url:
        return "skip"

    result = run_poll_cmd("read-poll", "--url", url)
    assert_eq("exit code", result.returncode, 0)
    data = json.loads(result.stdout)

    assert_eq("platform", data["platform"], "whenisgood")
    assert_true("has event_name", data["event_name"])
    assert_true("has slots", len(data["slots"]) > 0)
    assert_true("slots have slot_id", all("slot_id" in s for s in data["slots"]))

    print(f"  PASS: WhenIsGood read — {len(data['slots'])} slots, "
          f"name={data['event_name']!r}")
    return data


def test_whenisgood_fill():
    """fill-poll marks slots on WhenIsGood."""
    url = os.environ.get("TEST_WHENISGOOD_URL")
    if not url:
        return "skip"

    read_result = run_poll_cmd("read-poll", "--url", url)
    slots = json.loads(read_result.stdout)["slots"]

    ids = json.dumps([s["slot_id"] for s in slots[:3]])
    fill_result = run_poll_cmd("fill-poll", "--url", url,
                               "--name", TEST_NAME, "--available", ids)
    assert_eq("exit code", fill_result.returncode, 0)
    data = json.loads(fill_result.stdout)

    assert_true("filled > 0", data["filled"] > 0)
    assert_true("screenshot exists", Path(data["screenshot"]).exists())

    print(f"  PASS: WhenIsGood fill — {data['filled']}/{data['total']} slots")
    return data


# -------------------------------------------------------------------
# Doodle tests
# -------------------------------------------------------------------

def test_doodle_read():
    """read-poll extracts options from Doodle."""
    url = os.environ.get("TEST_DOODLE_URL")
    if not url:
        return "skip"

    result = run_poll_cmd("read-poll", "--url", url)
    assert_eq("exit code", result.returncode, 0)
    data = json.loads(result.stdout)

    assert_eq("platform", data["platform"], "doodle")
    assert_true("has event_name", data["event_name"])
    assert_true("has slots", len(data["slots"]) > 0)
    assert_true("slots have slot_id", all("slot_id" in s for s in data["slots"]))

    print(f"  PASS: Doodle read — {len(data['slots'])} options, "
          f"name={data['event_name']!r}")
    return data


def test_doodle_fill():
    """fill-poll checks options on Doodle."""
    url = os.environ.get("TEST_DOODLE_URL")
    if not url:
        return "skip"

    read_result = run_poll_cmd("read-poll", "--url", url)
    slots = json.loads(read_result.stdout)["slots"]

    ids = json.dumps([s["slot_id"] for s in slots[:2]])
    fill_result = run_poll_cmd("fill-poll", "--url", url,
                               "--name", TEST_NAME, "--available", ids)
    assert_eq("exit code", fill_result.returncode, 0)
    data = json.loads(fill_result.stdout)

    assert_true("filled > 0", data["filled"] > 0)
    assert_true("screenshot exists", Path(data["screenshot"]).exists())

    print(f"  PASS: Doodle fill — {data['filled']}/{data['total']} options")
    return data


# -------------------------------------------------------------------
# Runner
# -------------------------------------------------------------------

def run_test(name, func):
    global passed, failed, skipped
    print(f"\n{name}:")
    try:
        result = func()
        if result == "skip":
            skipped += 1
            print(f"  SKIP: {name} (no URL set)")
        else:
            passed += 1
    except Exception as e:
        failed += 1
        print(f"  FAIL: {e}")


if __name__ == "__main__":
    print("=" * 60)
    print("Poll Filler Integration Tests")
    print("=" * 60)

    # URL validation (always run, no browser needed)
    run_test("URL allowlist rejects bad URLs", test_url_allowlist_rejects_bad_urls)
    run_test("URL allowlist accepts good URLs", test_url_allowlist_accepts_good_urls)

    # Platform tests (require URLs)
    run_test("When2Meet read", test_when2meet_read)
    run_test("When2Meet fill", test_when2meet_fill)
    run_test("WhenIsGood read", test_whenisgood_read)
    run_test("WhenIsGood fill", test_whenisgood_fill)
    run_test("Doodle read", test_doodle_read)
    run_test("Doodle fill", test_doodle_fill)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    print(f"{'=' * 60}")

    sys.exit(1 if failed > 0 else 0)
