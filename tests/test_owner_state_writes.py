#!/usr/bin/env python3
"""Owner processes write into resident space only with the resident's access,
by atomic replace, or not at all.

The resident (ea-bot) owns every directory in ~/.ea, so it can put a link or
a hard link at any name there. These pin the owner-side writers and readers
that a link at the name must not steer:

  * the shared SQLite databases open through ledger.guarded_connect, which
    refuses a link or hard link at the database or a sidecar, a link below
    the install tree, and never creates the file as the owner;
  * locks are never truncated and never followed;
  * read-modify-write state is read with the resident's access and only its
    expected shape is carried over, so a rewrite cannot publish a file only
    the owner may read;
  * owner-only state (the one-time confirmation tokens, the review cursors,
    the dedupe files) lives in the owner-private directory;
  * the token modules take the plain path branch only for the resident or a
    scratch tree, never because the private directory is missing.

No test runs sudo: resident_io's child program runs as the test user through
its `_spawn` seam.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

EA_TMP = Path(tempfile.mkdtemp(prefix="ea-test-owner-writes-"))
os.environ["EA_DIR"] = str(EA_TMP)
for _sub in ("config", "state", "logs"):
    (EA_TMP / _sub).mkdir(parents=True, exist_ok=True)

import owner_trust as ot  # noqa: E402
import resident_io as rio  # noqa: E402
import ledger  # noqa: E402
import project_store  # noqa: E402
import closure_metrics  # noqa: E402
import graph_actions as ga  # noqa: E402
import graph_client as gc  # noqa: E402
import scheduled_sends as S  # noqa: E402
import preferences as P  # noqa: E402
import outbound_review as R  # noqa: E402
import calendar_reconciler as cr  # noqa: E402
import scheduling_engine as se  # noqa: E402
import style_analysis as sa  # noqa: E402
import list_digest as ld  # noqa: E402
import gmail_client  # noqa: E402
import google_calendar_client  # noqa: E402
import preprocess as pp  # noqa: E402

VICTIM = "owner-only content\n"


# ── fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def owner(tmp_path, monkeypatch):
    """An owner process: this euid owns the private dir, and the EA tree in
    use is the one the owner's ~/.ea names."""
    tree = tmp_path / "ea"
    for sub in ("state", "logs", "config"):
        (tree / sub).mkdir(parents=True)
    (tree / "auth").mkdir(mode=0o750)
    (tree / "auth").chmod(0o750)
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_DIR", str(tree))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(ot, "install_ea_dir", lambda: tree)
    assert ot.is_owner_process()
    return types.SimpleNamespace(tree=tree, priv=priv, state=tree / "state",
                                 auth=tree / "auth", logs=tree / "logs", tmp=tmp_path)


@pytest.fixture
def child(monkeypatch):
    """resident_io as in an owner process, its child program run as the
    test user; returns the list of child argvs."""
    calls: list = []

    def spawn(argv):
        calls.append(list(argv))
        return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd="/", env=dict(rio.CHILD_ENV))

    monkeypatch.setattr(rio, "_owner_check", lambda: True)
    monkeypatch.setattr(rio, "_availability", lambda: True)
    monkeypatch.setattr(rio, "_spawn", spawn)
    return calls


@pytest.fixture
def direct(monkeypatch):
    """resident_io as in a non-owner process: direct, no child."""
    def no_spawn(argv):
        raise AssertionError("the child was started")
    monkeypatch.setattr(rio, "_owner_check", lambda: False)
    monkeypatch.setattr(rio, "_spawn", no_spawn)


def _victim(tmp_path: Path, text: str = VICTIM) -> Path:
    v = tmp_path / "victim"
    v.write_text(text)
    return v


def _ops(calls: list) -> list:
    """The operation of each resident child call, in the inline form
    (python3 -I -c CODE op ...) or the file form (python3 -I FILE op ...)."""
    return [a[4] if a[2] == "-c" else a[3] for a in calls]


# ── the shared databases ──────────────────────────────────────────────

@pytest.fixture
def db_owner(monkeypatch, tmp_path, direct):
    """guarded_connect's owner branch on a scratch database."""
    monkeypatch.setattr(ledger, "_is_owner", lambda: True)
    monkeypatch.setattr(ledger, "_install_tree", lambda: tmp_path / "no-tree")
    db = tmp_path / "state" / "obligations-ledger.db"
    db.parent.mkdir()
    return db


def _make_db(path: Path, wal: bool = True) -> None:
    conn = sqlite3.connect(str(path))
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(ledger.SCHEMA)
    conn.commit()
    conn.close()


def test_a_link_at_a_sidecar_is_refused_and_its_target_untouched(db_owner, tmp_path):
    _make_db(db_owner)
    victim = _victim(tmp_path)
    for suffix in ledger.SQLITE_SIDECARS:
        link = Path(str(db_owner) + suffix)
        link.symlink_to(victim)
        with pytest.raises(ledger.UnsafeDatabase):
            ledger.guarded_connect(db_owner)
        monkey_db = db_owner
        ledger.DB_PATH, saved = monkey_db, ledger.DB_PATH
        try:
            with pytest.raises(sqlite3.DatabaseError):
                ledger._get_db()
        finally:
            ledger.DB_PATH = saved
        link.unlink()
    assert victim.read_text() == VICTIM


def test_a_link_or_hard_link_at_the_database_is_refused(db_owner, tmp_path):
    other = tmp_path / "other.db"
    _make_db(other)
    db_owner.symlink_to(other)
    with pytest.raises(ledger.UnsafeDatabase, match="not a regular file"):
        ledger.guarded_connect(db_owner)
    db_owner.unlink()
    os.link(other, db_owner)
    with pytest.raises(ledger.UnsafeDatabase, match="hard links"):
        ledger.guarded_connect(db_owner)


def test_a_missing_database_is_created_by_the_resident_path_never_the_owner(db_owner, monkeypatch):
    made = []
    monkeypatch.setattr(rio, "ensure_file", lambda p: made.append(p) or False)
    with pytest.raises(ledger.UnsafeDatabase, match="could not be created"):
        ledger.guarded_connect(db_owner)
    assert not db_owner.exists() and made == [str(db_owner)]
    monkeypatch.undo()
    monkeypatch.setattr(ledger, "_is_owner", lambda: True)
    monkeypatch.setattr(ledger, "_install_tree", lambda: db_owner.parent.parent / "no-tree")
    monkeypatch.setattr(rio, "_owner_check", lambda: False)
    conn = ledger.guarded_connect(db_owner)
    conn.execute("CREATE TABLE t (x)")
    conn.close()
    assert db_owner.is_file()
    with pytest.raises(ledger.UnsafeDatabase, match="does not exist"):
        ledger.guarded_connect(db_owner.with_name("absent.db"), read_only=True)


def test_a_link_below_the_install_tree_is_refused(monkeypatch, tmp_path, direct):
    tree = tmp_path / "tree"
    tree.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _make_db(elsewhere / "obligations-ledger.db")
    (tree / "state").symlink_to(elsewhere)
    monkeypatch.setattr(ledger, "_is_owner", lambda: True)
    monkeypatch.setattr(ledger, "_install_tree", lambda: tree)
    with pytest.raises(ledger.UnsafeDatabase, match="link below the install tree"):
        ledger.guarded_connect(tree / "state" / "obligations-ledger.db")


def test_owner_connections_distrust_the_schema(db_owner):
    _make_db(db_owner)
    conn = ledger.guarded_connect(db_owner)
    try:
        assert conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_owner_side_schema_and_writes_work_under_the_guard(db_owner, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", db_owner)
    ledger._get_db().close()                  # a fresh database: schema and migrations
    project_store._get_db(db_owner).close()
    assert ledger.add_entry("row-1", "response_needed", "d", db_path=db_owner)
    assert ledger.entry_exists("row-1", db_path=db_owner)
    assert [e["id"] for e in ledger.get_open_entries(db_path=db_owner)] == ["row-1"]


def test_the_project_store_opens_through_the_guard(db_owner, tmp_path):
    _make_db(db_owner)
    victim = _victim(tmp_path)
    Path(str(db_owner) + "-shm").symlink_to(victim)
    with pytest.raises(sqlite3.DatabaseError):
        project_store._get_db(db_owner)
    assert victim.read_text() == VICTIM


def test_closure_metrics_does_not_read_a_linked_ledger(db_owner, tmp_path):
    other = tmp_path / "other.db"
    _make_db(other, wal=False)
    conn = sqlite3.connect(str(other))
    conn.execute("INSERT INTO ledger (id, type, status, closed_at, created_at, updated_at) "
                 "VALUES ('x', 'response_needed', 'closed', '2999-01-01T00:00:00Z', 'a', 'b')")
    conn.commit()
    conn.close()
    db_owner.symlink_to(other)
    assert closure_metrics.ledger_closures(3650, db_path=db_owner) == []


# ── graph_actions: tokens, locks, snoozes ─────────────────────────────

def test_pending_confirmations_live_in_the_private_dir(owner):
    assert ga._owner_state_dir() == owner.priv


def test_pending_store_in_the_private_dir(owner, monkeypatch):
    monkeypatch.setattr(ga, "PENDING_CONFIRMS_FILE", owner.priv / "pending-confirmations.json")
    monkeypatch.setattr(ga, "PENDING_CONFIRMS_LOCK", owner.priv / "pending-confirmations.lock")
    token = ga._create_confirmation("send", "AAMkD=", "test")
    f = ga.PENDING_CONFIRMS_FILE
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert token in json.loads(f.read_text())
    assert ga._validate_and_consume_token(token, "send", "AAMkD=")["action"] == "send"
    assert ga._validate_and_consume_token(token, "send", "AAMkD=") is None


def test_pending_loader_keeps_only_token_entries(monkeypatch, tmp_path):
    f = tmp_path / "pending.json"
    good = {"action": "send", "message_id": "m", "expires": "2999-01-01T00:00:00"}
    f.write_text(json.dumps({"tok": good, "bad": {"RefreshToken": "r"}, "n": 3}))
    monkeypatch.setattr(ga, "PENDING_CONFIRMS_FILE", f)
    assert ga._load_pending() == {"tok": good}


def test_the_library_gate_consumes_stored_tokens_through_graph_actions(monkeypatch, tmp_path):
    monkeypatch.setattr(ga, "PENDING_CONFIRMS_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(ga, "PENDING_CONFIRMS_LOCK", tmp_path / "pending.lock")
    monkeypatch.setattr(gc, "_graph_actions_module", lambda: ga)
    fp = "sha256:" + "ab" * 32
    token = ga._create_confirmation("send", "AAMkD=", fingerprint=fp)
    assert not gc._validate_library_token(token, "send", "AAMkOther=", fp)
    assert gc._validate_library_token(token, "send", "AAMkD=", fp)
    assert not gc._validate_library_token(token, "send", "AAMkD=", fp)
    # a stored send token is bound to the reviewed content
    other = ga._create_confirmation("send", "AAMkD=", fingerprint=fp)
    assert not gc._validate_library_token(other, "send", "AAMkD=", "sha256:" + "cd" * 32)


def test_a_lock_is_never_truncated_or_followed(tmp_path):
    victim = _victim(tmp_path)
    lock = tmp_path / "state.lock"
    lock.symlink_to(victim)
    with pytest.raises(OSError):
        ga._with_file_lock(lock, lambda: None)
    assert victim.read_text() == VICTIM
    lock.unlink()
    lock.write_text("kept")
    assert ga._with_file_lock(lock, lambda: 7) == 7
    assert lock.read_text() == "kept"


def test_a_resident_lock_is_made_with_the_residents_access(owner, child, tmp_path):
    lock = owner.state / "snoozed-messages.lock"
    assert ga._with_file_lock(lock, lambda: 1) == 1
    assert "touch" in _ops(child) and lock.is_file()
    lock.unlink()
    victim = _victim(tmp_path)
    lock.symlink_to(victim)
    with pytest.raises(OSError):
        ga._with_file_lock(lock, lambda: None)
    assert victim.read_text() == VICTIM


def test_the_snooze_loader_reads_with_the_residents_access(owner, child, tmp_path):
    snooze = owner.state / "snoozed-messages.json"
    victim = _victim(tmp_path, json.dumps([{"message_id": "owner-only"}]))
    snooze.symlink_to(victim)
    with pytest.raises(OSError):
        ga._load_snoozes(snooze)
    snooze.unlink()
    snooze.write_text(json.dumps([{"message_id": "m"}, "stray", 4]))
    assert ga._load_snoozes(snooze) == [{"message_id": "m"}]
    assert "read" in _ops(child)


# ── scheduled_sends ───────────────────────────────────────────────────

def test_the_queue_lock_is_never_truncated_or_followed(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "SENDS_FILE", tmp_path / "scheduled-sends.json")
    victim = _victim(tmp_path)
    (tmp_path / "scheduled-sends.lock").symlink_to(victim)
    with pytest.raises(OSError):
        S._with_sends_lock(lambda: None)
    assert victim.read_text() == VICTIM


def test_the_queue_is_read_and_locked_with_the_residents_access(owner, child, monkeypatch, tmp_path):
    q = owner.state / "scheduled-sends.json"
    monkeypatch.setattr(S, "SENDS_FILE", q)
    assert S._with_sends_lock(lambda: S._load_sends()) == []
    assert "touch" in _ops(child)
    q.symlink_to(_victim(tmp_path, json.dumps([{"draft_id": "owner-only"}])))
    with pytest.raises(OSError):
        S._load_sends()
    q.unlink()
    q.write_text(json.dumps([{"draft_id": "d"}, {"x": 1}, "s"]))
    assert S._load_sends() == [{"draft_id": "d"}]


def test_the_key_is_checked_on_the_install_tree_without_a_private_dir(monkeypatch, tmp_path):
    tree = tmp_path / "ea"
    (tree / "auth").mkdir(parents=True)
    (tree / "auth").chmod(0o775)
    key = tree / "auth" / "scheduled-send-hmac.key"
    key.write_bytes(b"k" * 48)
    key.chmod(0o664)
    monkeypatch.setenv("EA_DIR", str(tree))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(ot, "install_ea_dir", lambda: tree)
    monkeypatch.setattr(S, "HMAC_KEY_FILE", key)
    assert not ot.is_owner_process()          # the old branch took the plain read
    with pytest.raises(S.ApprovalKeyError, match="refused"):
        S._load_key()
    monkeypatch.setattr(ot, "install_ea_dir", lambda: tmp_path / "other-tree")
    assert S._load_key() == b"k" * 48         # a scratch tree reads plainly


# ── owner-only state in the private dir ───────────────────────────────

def test_the_digest_cursor_lives_in_the_private_dir(owner, child, monkeypatch):
    assert P._owner_state_dir() == owner.priv
    cur = owner.priv / "preferences-digest-seen.json"
    legacy = owner.state / "preferences-digest-seen.json"
    monkeypatch.setattr(P, "DIGEST_CURSOR", cur)
    monkeypatch.setattr(P, "LEGACY_DIGEST_CURSOR", legacy)
    assert P.read_digest_cursor() is None
    legacy.write_text(json.dumps({"rules": {"r1": "active", "bad": 3}}))
    assert P.read_digest_cursor() == {"rules": {"r1": "active"}}   # seeded once
    P.write_digest_cursor({"rules": {"r2": "active"}})
    assert stat.S_IMODE(cur.stat().st_mode) == 0o600
    assert P.read_digest_cursor() == {"rules": {"r2": "active"}}
    cur.write_text(json.dumps({"rules": "not a map"}))
    cur.chmod(0o600)
    assert P.read_digest_cursor() is None


def test_the_outbound_cursor_lives_in_the_private_dir(owner, child, monkeypatch, tmp_path):
    assert R._owner_state_dir() == owner.priv
    cur = owner.priv / "outbound-review-seen.json"
    legacy = owner.state / "outbound-review-seen.json"
    monkeypatch.setattr(R, "CURSOR", cur)
    # ea-bot can write the old copy in its state, so it never seeds the cursor
    legacy.write_text(json.dumps({"last_ts": "2099-09-01T00:00:00Z"}))
    assert R.read_cursor() is None and R.read_state() is None
    R.write_cursor("2026-09-02T00:00:00Z")
    assert stat.S_IMODE(cur.stat().st_mode) == 0o600
    assert R.read_cursor() == "2026-09-02T00:00:00Z"


def test_the_outbound_review_reads_the_log_with_the_residents_access(owner, child, tmp_path):
    log = owner.logs / "fetch-url.log"
    log.symlink_to(_victim(tmp_path, json.dumps({"ts": "2026-09-01T00:00:00Z",
                                                  "event": "fetch", "url": "secret"}) + "\n"))
    assert R.read_records(None, path=log) == []
    log.unlink()
    log.write_text(json.dumps({"ts": "2026-09-01T00:00:00Z", "event": "fetch"}) + "\n")
    assert len(R.read_records(None, path=log)) == 1


def test_the_gap_dedupe_lives_in_the_private_dir(owner, child, monkeypatch):
    assert cr._owner_state_dir() == owner.priv
    monkeypatch.setattr(cr, "GAP_WARNED_FILE", owner.priv / "reconciler-gap-warned.json")
    monkeypatch.setattr(cr, "LEGACY_GAP_WARNED_FILE", owner.state / "reconciler-gap-warned.json")
    cr.LEGACY_GAP_WARNED_FILE.write_text(json.dumps(["old|2026-08-01", 5]))
    assert cr._read_gap_warned() == {"old|2026-08-01"}          # seeded from the legacy file
    cr._write_gap_warned(["a|2026-09-01"])
    assert stat.S_IMODE(cr.GAP_WARNED_FILE.stat().st_mode) == 0o600
    assert cr._read_gap_warned() == {"a|2026-09-01"}


def test_the_checkin_marker_lives_in_the_private_dir(owner):
    spec = importlib.util.spec_from_file_location(
        "closure_metrics_checkin", ROOT / "scripts" / "closure-metrics-checkin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.MARKER.parent == owner.priv
    assert not mod._done()
    mod._mark_done()
    assert mod._done() and stat.S_IMODE(mod.MARKER.stat().st_mode) == 0o600


# ── read-modify-write state in resident space ─────────────────────────

def test_the_decisions_log_is_rewritten_with_the_residents_access(owner, child, monkeypatch, tmp_path):
    f = owner.state / "scheduling-decisions.json"
    monkeypatch.setattr(cr, "DECISIONS_FILE", f)
    monkeypatch.setattr(se, "DECISIONS_FILE", f)
    f.write_text(json.dumps([1, {"event": "old"}]))
    cr.log_release("key|x", "Lecture")
    se._append_decision({"event": "Sync", "action": "moved"})
    data = json.loads(f.read_text())
    assert [d.get("event") for d in data] == ["old", "Lecture", "Sync"]
    assert "update" in _ops(child)
    assert cr.released_keys_from_log() == {"key|x"}
    f.unlink()
    victim = _victim(tmp_path, json.dumps([{"event": "owner-only", "action": "released",
                                            "source_key": "k"}]))
    f.symlink_to(victim)
    with pytest.raises(OSError):
        cr.log_release("key|y", "Lecture")
    with pytest.raises(OSError):
        se._append_decision({"event": "x"})
    assert se._load_decisions() == [] and cr.released_keys_from_log() == set()
    assert json.loads(victim.read_text())[0]["event"] == "owner-only"


def test_the_style_report_replaces_a_link_and_reads_with_the_residents_access(
        owner, child, monkeypatch, tmp_path):
    rep = owner.state / "style-analysis"
    monkeypatch.setattr(sa, "REPORT_DIR", rep)
    monkeypatch.setattr(sa, "LATEST", rep / "latest.json")
    rep.mkdir()
    victim = _victim(tmp_path, json.dumps({"overall": {"owner": "only"}}))
    sa.LATEST.symlink_to(victim)
    assert sa.load_previous() == {}
    sa.save({"window": {"to": "2026-09-30"}, "overall": {}})
    assert victim.read_text() == json.dumps({"overall": {"owner": "only"}})
    assert not sa.LATEST.is_symlink() and "replace" in _ops(child)
    assert sa.load_previous()["window"]["to"] == "2026-09-30"


def test_the_digest_history_is_appended_with_the_residents_access(owner, child, monkeypatch, tmp_path):
    hist = owner.state / "list-digest-history.jsonl"
    monkeypatch.setattr(ld, "HISTORY_FILE", hist)
    ld._append_history({"list": "a"})
    assert json.loads(hist.read_text()) == {"list": "a"} and "append" in _ops(child)
    hist.unlink()
    victim = _victim(tmp_path)
    hist.symlink_to(victim)
    ld._append_history({"list": "b"})
    assert victim.read_text() == VICTIM


def test_the_proactive_queue_merge_does_not_publish_a_linked_file(owner, child, monkeypatch, tmp_path):
    monkeypatch.setattr(pp, "sched_channel_configured", lambda chat=None: True)
    q = owner.state / "scheduling-proactive-queue.json"
    victim = _victim(tmp_path, json.dumps([{"conversation_id": "owner-only", "received": ""}]))
    q.symlink_to(victim)
    pp.write_proactive_queue([{"conversation_id": "c1", "received": ""}], path=q)
    assert not q.is_symlink()
    assert [r["conversation_id"] for r in json.loads(q.read_text())] == ["c1"]
    assert "owner-only" in victim.read_text()


# ── the token modules' branch choice ──────────────────────────────────

@pytest.mark.parametrize("mod", [gmail_client, google_calendar_client])
def test_google_token_io_is_checked_on_the_install_tree_without_a_private_dir(
        mod, monkeypatch, tmp_path):
    tree = tmp_path / "ea"
    tree.mkdir()
    monkeypatch.setenv("EA_DIR", str(tree))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(ot, "install_ea_dir", lambda: tree)
    assert not ot.is_owner_process()
    assert mod._checked_token_io()
    monkeypatch.setattr(ot, "install_ea_dir", lambda: tmp_path / "other-tree")
    assert not mod._checked_token_io()


def test_the_graph_cache_write_never_creates_the_auth_dir(owner, monkeypatch):
    owner.auth.rmdir()
    monkeypatch.setattr(gc, "CACHE_FILE", owner.auth / "msal_token_cache.json")
    with pytest.raises((OSError, ot.OwnerTrustError)):
        gc._write_cache_file("{}")
    assert not owner.auth.exists()
