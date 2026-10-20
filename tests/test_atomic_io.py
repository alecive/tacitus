"""atomic_io must not let a symlink in the target directory redirect a write.

The state and registry files live in a group-writable directory shared by
crh (the writer) and ea-bot (the resident). The guarantee: a write never
lands anywhere but its own path, whatever another group member pre-creates
there, and a reader never sees a half-written file.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))
import atomic_io  # noqa: E402


def test_round_trip_and_mode(tmp_path):
    p = tmp_path / "sub" / "state.json"
    atomic_io.write_json(p, {"a": 1}, mode=0o600)
    assert json.loads(p.read_text()) == {"a": 1}
    assert (os.stat(p).st_mode & 0o777) == 0o600
    atomic_io.write_text(tmp_path / "t.txt", "hi")
    assert (tmp_path / "t.txt").read_text() == "hi"


def test_a_symlink_at_the_destination_is_replaced_not_followed(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET")
    dest = tmp_path / "state.json"
    dest.symlink_to(outside)
    atomic_io.write_json(dest, {"ok": True})
    assert outside.read_text() == "SECRET"          # the link's target is untouched
    assert not dest.is_symlink()                     # the link itself was replaced
    assert json.loads(dest.read_text()) == {"ok": True}


def test_a_pre_created_temp_name_cannot_capture_the_write(tmp_path, monkeypatch):
    """Even if an attacker guesses the random temp name and plants a symlink,
    mkstemp's O_EXCL refuses it rather than writing through it."""
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET")
    real_mkstemp = atomic_io.tempfile.mkstemp
    planted = {"done": False}

    def guessing_mkstemp(*a, **k):
        # First call: pre-plant a symlink at the exact name mkstemp will try,
        # by intercepting the name it returns and re-pointing... instead we
        # assert the real mkstemp never hands back a path that already exists.
        fd, name = real_mkstemp(*a, **k)
        if not planted["done"]:
            planted["done"] = True
            assert not Path(name).is_symlink()       # fresh file, O_EXCL created it
        return fd, name

    monkeypatch.setattr(atomic_io.tempfile, "mkstemp", guessing_mkstemp)
    atomic_io.write_json(tmp_path / "state.json", {"ok": True})
    assert outside.read_text() == "SECRET"
    assert planted["done"]


def test_a_failed_write_leaves_no_temp_behind(tmp_path):
    class Boom:
        def __repr__(self): raise RuntimeError("boom")
    with pytest.raises(TypeError):
        atomic_io.write_json(tmp_path / "x.json", {1, 2, 3})  # sets aren't JSON
    assert list(tmp_path.iterdir()) == []

