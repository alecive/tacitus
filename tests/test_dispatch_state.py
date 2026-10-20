#!/usr/bin/env python3
"""In-flight dispatch bookkeeping in ea-chat-bot.py.

Two mechanisms:

  _active_threads is the Slack-event dedupe guard. It must be added and
    discarded on the SAME key: for a thread reply the message ts and the
    thread root differ, so a mismatch leaks the reply's key forever and
    evicts the parent's still-in-flight key, letting a Socket-Mode
    redelivery double-run a dispatch (duplicate draft / reply / ledger
    op).

  _active_dispatches is a registry of live Claude turns, so the
    out-of-band sweepers can tell "nobody is working" from "a turn is
    mid-flight" and can attribute an orphaned staging file to the one
    dispatch that could have written it.

ea-chat-bot.py is hyphenated and pulls in slack_bolt, so the registry is
exec'd out of the source by markers (the test_draft_chain_e2e pattern) and
the key discipline is asserted at the text level.
"""

import re
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()


def _exec_block(start_marker: str, end_marker: str, ns: dict) -> dict:
    start = BOT_SRC.index(start_marker)
    end = BOT_SRC.index(end_marker, start)
    exec(compile(BOT_SRC[start:end], "ea-chat-bot.py[block]", "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# _active_threads key discipline
# ---------------------------------------------------------------------------

def test_every_discard_uses_the_key_that_was_added():
    """add() takes the message ts; so must every discard()."""
    bad = [m.group(1) for m in
           re.finditer(r"_active_threads\.discard\(([^)]*)\)", BOT_SRC)
           if m.group(1).strip() != "msg_ts"]
    assert not bad, (
        f"_active_threads.discard() called with {bad}; the key added by "
        f"on_message/_desk_dispatch/_restart_catchup is the MESSAGE ts, so "
        f"discarding thread_ts leaks the reply's key and evicts the "
        f"parent's still-in-flight one")


def test_every_add_uses_the_message_ts():
    adds = re.findall(r"_active_threads\.add\(([^)]*)\)", BOT_SRC)
    assert adds, "the dedupe guard has no add() left"
    assert set(a.strip() for a in adds) == {"ts"}, (
        f"_active_threads.add() called with {adds}; only the raw Slack "
        f"message ts may be added")


def test_button_handlers_do_not_touch_the_dedupe_guard():
    """A button click never ADDS a key, so it must never discard one — it
    was evicting whatever dispatch happened to be live in that thread."""
    for handler in ("_do_send", "_do_create", "_do_update", "_do_delete"):
        start = BOT_SRC.index(f"    def {handler}():")
        end = BOT_SRC.index("threading.Thread(target=", start)
        assert "_active_threads.discard" not in BOT_SRC[start:end], (
            f"{handler} still discards from _active_threads")


def test_discards_are_taken_under_the_lock():
    for m in re.finditer(r"_active_threads\.discard\(", BOT_SRC):
        window = BOT_SRC[max(0, m.start() - 200):m.start()]
        assert "with _active_lock:" in window, (
            "every _active_threads mutation must hold _active_lock")


# ---------------------------------------------------------------------------
# _active_dispatches registry
# ---------------------------------------------------------------------------

def _registry():
    ns = {"threading": threading, "time": time, "uuid": __import__("uuid"),
          "TIMEOUT_SECONDS": 600, "ROOM_TIMEOUT_SECONDS": 1200}
    # The watchdog globals are the next thing at module scope after the
    # registry, so they are the end marker.
    _exec_block("# Dispatch run identity",
                "# Watchdog: track last message received", ns)
    return ns


def test_run_ids_are_unique():
    ns = _registry()
    ids = {ns["_new_run_id"]() for _ in range(200)}
    assert len(ids) == 200


def test_register_and_unregister():
    ns = _registry()
    assert ns["_dispatches_active"]() == 0
    ns["_register_dispatch"]("r1", "C1", "T1")
    assert ns["_dispatches_active"]() == 1
    ns["_unregister_dispatch"]("r1")
    assert ns["_dispatches_active"]() == 0


def test_a_registration_expires_so_a_crashed_turn_cannot_wedge_the_sweeper():
    ns = _registry()
    ns["_register_dispatch"]("r1", "C1", "T1", ttl_s=-1)
    assert ns["_dispatches_active"]() == 0, (
        "an expired registration must be pruned; otherwise a dispatch that "
        "dies without unregistering pins the sweepers into 'busy' forever")


def test_a_timed_out_turn_can_extend_its_registration():
    ns = _registry()
    ns["_register_dispatch"]("r1", "C1", "T1", ttl_s=-1)
    ns["_active_dispatches"]["r1"] = {
        "channel": "C1", "thread_ts": "T1",
        "started_at": time.time() - 10, "expires_at": time.time() - 1}
    ns["_extend_dispatch"]("r1", 600)
    assert ns["_dispatches_active"]() == 1


def test_attribution_picks_the_single_dispatch_that_was_running():
    ns = _registry()
    ns["_register_dispatch"]("r1", "C-SCHED", "T-SCHED")
    owner = ns["_dispatch_that_could_have_written"](time.time())
    assert owner is not None
    assert owner["channel"] == "C-SCHED"
    assert owner["thread_ts"] == "T-SCHED"


def test_attribution_declines_when_nothing_was_running():
    ns = _registry()
    assert ns["_dispatch_that_could_have_written"](time.time()) is None


def test_attribution_declines_when_two_dispatches_overlap():
    """Ambiguous is ambiguous: the sweeper falls back to the channel root
    rather than dropping a Create button into the wrong conversation."""
    ns = _registry()
    ns["_register_dispatch"]("r1", "C1", "T1")
    ns["_register_dispatch"]("r2", "C2", "T2")
    assert ns["_dispatch_that_could_have_written"](time.time()) is None


def test_attribution_ignores_a_dispatch_that_started_after_the_write():
    ns = _registry()
    ns["_register_dispatch"]("r1", "C1", "T1")
    assert ns["_dispatch_that_could_have_written"](time.time() - 60) is None


# ---------------------------------------------------------------------------
# EA_RUN_ID plumbing
# ---------------------------------------------------------------------------

def test_run_id_is_exported_to_the_claude_subprocess():
    """draft_events.record reads $EA_RUN_ID; the mono lane is the one path
    that can set it."""
    start = BOT_SRC.index("def _sudo_claude_cmd(")
    end = BOT_SRC.index("\n@dataclass", start)
    block = BOT_SRC[start:end]
    assert 'env_vars.append(f"EA_RUN_ID={run_id}")' in block, (
        "the Claude subprocess must carry EA_RUN_ID or its draft records "
        "are unattributable")
    assert "_sudo_claude_cmd(args, run_id=run_id, extra_env=extra_env)" in BOT_SRC, (
        "_run_claude_streaming must forward the dispatch's run id")
    assert BOT_SRC.count('label="mono", run_id=run_id') == 4, (
        "all four mono spawn sites (fresh, resume, resume-fallback, the "
        "Claude rerun of a remote-arm turn) must pass the run id")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
