"""Three owner-side reads and opens follow the owner trust rules.

preprocess's delta loaders read their state files through resident_io and
keep only the expected shape; style_profiles opens its database through
ledger.guarded_connect; graph_client picks its token branch by the same
rule as the Gmail and Calendar clients.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Sandbox before import: owner_trust counts a process whose EA_DIR is the
# live tree as the owner.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-followups-")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import pytest  # noqa: E402

import preprocess  # noqa: E402


def test_loaders_read_through_resident_io(monkeypatch):
    seen = []

    def fake_read(path):
        seen.append(Path(path).name)
        return {"seen-messages.json": json.dumps(["a", 3, "b"]),
                "seen-conversations.json": json.dumps({"c1": "t1", "c2": 5}),
                "surfaced-responses.json": json.dumps(["x", "y"])}[Path(path).name]

    monkeypatch.setattr(preprocess._rio, "read_text", fake_read)
    assert preprocess.load_seen_messages() == {"a", "b"}
    assert preprocess.load_seen_conversations() == {"c1": "t1"}
    assert preprocess.load_surfaced_responses() == {"x": "", "y": ""}
    assert seen == ["seen-messages.json", "seen-conversations.json",
                    "surfaced-responses.json"]


def test_a_wrong_shape_is_refused(monkeypatch):
    monkeypatch.setattr(preprocess._rio, "read_text", lambda p: json.dumps({"k": "v"}))
    with pytest.raises(ValueError):
        preprocess.load_seen_messages()
    monkeypatch.setattr(preprocess._rio, "read_text", lambda p: json.dumps(["k"]))
    with pytest.raises(ValueError):
        preprocess.load_seen_conversations()


def test_an_unreadable_file_raises_and_a_missing_one_is_empty(tmp_path, monkeypatch):
    present = tmp_path / "seen-messages.json"
    present.write_text("[]")
    monkeypatch.setattr(preprocess, "SEEN_MESSAGES_FILE", present)
    monkeypatch.setattr(preprocess._rio, "read_text", lambda p: None)
    with pytest.raises(OSError):
        preprocess.load_seen_messages()     # never read as empty, then saved over
    monkeypatch.setattr(preprocess, "SEEN_MESSAGES_FILE", tmp_path / "absent.json")
    assert preprocess.load_seen_messages() == set()


def test_style_profiles_opens_through_the_guard(tmp_path, monkeypatch):
    import ledger
    import style_profiles as sp
    calls = []
    real = ledger.guarded_connect

    def spy(path, **kw):
        calls.append(Path(path))
        return real(path, **kw)

    monkeypatch.setattr(ledger, "guarded_connect", spy)
    monkeypatch.setattr(sp, "DB_PATH", tmp_path / "state" / "style-profiles.db")
    con = sp._connect()
    con.close()
    assert calls == [tmp_path / "state" / "style-profiles.db"]


def test_graph_token_branch_matches_gmail(monkeypatch, tmp_path):
    import gmail_client
    import graph_client
    import owner_trust
    for tree in (tmp_path / "scratch", owner_trust.install_ea_dir()):
        monkeypatch.setattr(owner_trust, "ea_dir", lambda t=tree: t)
        assert graph_client._checked_token_io() == gmail_client._checked_token_io()
    monkeypatch.setattr(owner_trust, "ea_dir", owner_trust.install_ea_dir)
    assert graph_client._checked_token_io() is True   # the install tree is checked
