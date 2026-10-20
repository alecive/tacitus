"""A project room's specialist session starts over when it outgrows
fast-turn size (ea-chat-bot.py _maybe_rotate_room_session).

The room keeps its model and effort, every thread bound to the old
session moves to the new one, a follow-up in a thread the session served
waits until twice the limit, and a session mid-turn is never rotated.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path

EA_DIR = Path(tempfile.mkdtemp(prefix="ea-test-room-rotation-"))
os.environ["EA_DIR"] = str(EA_DIR)
(EA_DIR / "config").mkdir(parents=True, exist_ok=True)
(EA_DIR / "state").mkdir(parents=True, exist_ok=True)

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "preprocessor"))

from preprocessor import ea_channels as ch  # noqa: E402
from preprocessor import ea_config  # noqa: E402
from preprocessor import slack_sessions as ss  # noqa: E402

BOT_SRC = (REPO / "ea-chat-bot.py").read_text()
CHANNEL = "C0ROOMROT01"
MAIN = "C0MAINCHAN1"


def _fresh(limit: int | None = None) -> str:
    ch.REGISTRY = EA_DIR / "config" / "ea-channels.json"
    ch.REGISTRY.unlink(missing_ok=True)
    ss.REGISTRY = EA_DIR / "state" / "slack-sessions.json"
    ss.REGISTRY.unlink(missing_ok=True)
    cfg = EA_DIR / "config" / "ea-config.json"
    cfg.write_text(json.dumps({} if limit is None else {"rooms": {"rotate_bytes": limit}}))
    old = str(uuid.uuid4())
    ch.register(CHANNEL, "lab-visits", "ea-lab-visits")
    ch.set_session(CHANNEL, old, model="claude-opus-5-5[1m]", effort="xhigh")
    ss.adopt("t-1", old, source="project-room", require_file=False,
             model="claude-opus-5-5[1m]", effort="xhigh")
    ss.adopt("t-2", old, source="project-room", require_file=False,
             model="claude-opus-5-5[1m]", effort="xhigh")
    return old


def _rotation_ns(size, busy: set[str] | None = None) -> dict:
    start = BOT_SRC.index("\n_ROOM_ROTATE_LOCKS: dict")
    end = BOT_SRC.index("\n# ---- Verify before you assert", start)
    locks: dict[str, threading.Lock] = {}

    def _session_lock(sid):
        lk = locks.setdefault(sid, threading.Lock())
        if busy and sid in busy and not lk.locked():
            lk.acquire()
        return lk

    class _Log:
        def info(self, *a, **k):
            pass

        warning = info

    ns = {"uuid": uuid, "threading": threading, "log": _Log(), "CHANNEL_ID": MAIN,
          "_CHANNEL_SCOPE_CACHE": {},
          "_channel_scope": lambda c: ch.scope_of(c),
          "_session_bytes": size if callable(size) else (lambda sid: size),
          "_session_lock": _session_lock}
    exec(compile(BOT_SRC[start:end], "ea-chat-bot.py[rotation]", "exec"), ns)
    return ns


def _rotator(size, busy: set[str] | None = None):
    return _rotation_ns(size, busy)["_maybe_rotate_room_session"]


def test_rotate_bytes_default_and_override():
    cfg = EA_DIR / "config" / "rot.json"
    assert ea_config.get_room_rotate_bytes(Path("/nonexistent/ea-config.json")) == 1_000_000
    cfg.write_text(json.dumps({"rooms": {"rotate_bytes": 0}}))
    assert ea_config.get_room_rotate_bytes(cfg) == 0
    cfg.write_text(json.dumps({"rooms": {"rotate_bytes": "big"}}))
    assert ea_config.get_room_rotate_bytes(cfg) == 1_000_000


def test_small_session_is_kept():
    old = _fresh()
    assert _rotator(10_000)(CHANNEL, "t-new", False) == ""
    assert ch.load()[CHANNEL]["session_id"] == old


def test_outgrown_session_rotates_and_moves_its_threads():
    old = _fresh(limit=1000)
    new = _rotator(5000)(CHANNEL, "t-new", False)
    assert new and new != old
    row = ch.load()[CHANNEL]
    assert row["session_id"] == new
    assert row["model"] == "claude-opus-5-5[1m]" and row["effort"] == "xhigh"
    reg = json.loads(ss.REGISTRY.read_text())
    for ts in ("t-1", "t-2"):
        assert reg[ts]["session_id"] == new
        assert reg[ts]["meta"]["rotated_from"] == old
        assert reg[ts]["meta"]["effort"] == "xhigh"


def test_follow_up_waits_until_twice_the_limit():
    old = _fresh(limit=1000)
    assert _rotator(1500)(CHANNEL, "t-1", True) == ""
    assert ch.load()[CHANNEL]["session_id"] == old
    assert _rotator(2500)(CHANNEL, "t-1", True) != ""


def test_mid_turn_session_is_not_rotated():
    old = _fresh(limit=1000)
    assert _rotator(5000, busy={old})(CHANNEL, "t-new", False) == ""
    assert ch.load()[CHANNEL]["session_id"] == old


def test_off_main_channel_and_session_rooms_only():
    _fresh(limit=1000)
    assert _rotator(5000)(MAIN, "t-new", False) == ""
    ch.REGISTRY.unlink(missing_ok=True)
    ch.register("C0TMPROOM01", "", "tmp-d488ed1f", session_id=str(uuid.uuid4()))
    assert _rotator(5000)("C0TMPROOM01", "t-new", False) == ""


def test_rotation_off_at_zero():
    _fresh(limit=0)
    assert _rotator(10**9)(CHANNEL, "t-new", False) == ""


def test_repoint_session_validates_ids():
    _fresh()
    assert ss.repoint_session("bad", str(uuid.uuid4())) == 0
    sid = str(uuid.uuid4())
    assert ss.repoint_session(sid, sid) == 0


def test_dispatch_rotates_before_binding_and_notes_the_new_session():
    i = BOT_SRC.index("_maybe_rotate_room_session(channel, thread_ts, is_thread_reply)")
    j = BOT_SRC.index("_bind_room_session(channel, thread_ts)", i)
    assert i < j
    assert 'session_meta.get("rotated_from")' in BOT_SRC
    assert "prompt + \"\\n\\n\" + ROOM_ROTATION_NOTE" in BOT_SRC


def test_two_new_topics_at_once_rotate_the_room_once():
    """The size read is slow, so both topics pass it before either writes;
    the room lock and the re-read inside it keep the room on one session."""
    import time as _time
    old = _fresh(limit=1000)

    def size(sid):
        _time.sleep(0.2)
        return 5000 if sid == old else 10

    rotate = _rotator(size)
    got = []
    threads = [threading.Thread(target=lambda ts=ts: got.append(rotate(CHANNEL, ts, False)))
               for ts in ("t-new-a", "t-new-b")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    fresh = [g for g in got if g]
    assert len(fresh) == 1 and len(got) == 2
    reg = json.loads(ss.REGISTRY.read_text())
    assert ch.load()[CHANNEL]["session_id"] == fresh[0]
    assert {reg[ts]["session_id"] for ts in ("t-1", "t-2")} == {fresh[0]}


def test_the_check_runs_under_the_dispatch_room_lock():
    """The dispatch holds the room's lock around the rotation check, which
    takes the same lock again: a non-reentrant lock hangs every message in
    a project room that has a session."""
    for size in (10, 5000):
        _fresh(limit=1000)
        ns = _rotation_ns(size)
        got = []

        def dispatch():
            with ns["_room_rotate_lock"](CHANNEL):
                got.append(ns["_maybe_rotate_room_session"](CHANNEL, "t-new", False))

        th = threading.Thread(target=dispatch, daemon=True)
        th.start()
        th.join(timeout=5)
        assert not th.is_alive(), "rotation check deadlocked on the room lock"
        assert len(got) == 1 and bool(got[0]) == (size == 5000)
