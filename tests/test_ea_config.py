#!/usr/bin/env python3
"""Unit tests for preprocessor/ea_config.py."""

import json
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))

import ea_config  # noqa: E402
from ea_config import load, is_ea_enabled, \
    is_eval_postcommit_enabled, get_log_level, DEFAULTS


def test_missing_file_returns_defaults():
    """Missing config file returns all defaults (everything enabled)."""
    cfg = load(Path("/nonexistent/ea-config.json"))
    assert cfg == DEFAULTS


def test_malformed_json_returns_defaults():
    """Malformed JSON falls back to defaults."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("NOT VALID JSON {{{")
        f.flush()
        cfg = load(Path(f.name))
    assert cfg == DEFAULTS


def test_empty_file_returns_defaults():
    """Empty file falls back to defaults."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("")
        f.flush()
        cfg = load(Path(f.name))
    assert cfg == DEFAULTS


def test_ea_enabled_false():
    """ea_enabled: false is correctly read."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"ea_enabled": False}, f)
        f.flush()
        assert is_ea_enabled(Path(f.name)) is False


def test_ea_enabled_true():
    """ea_enabled: true is correctly read."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"ea_enabled": True}, f)
        f.flush()
        assert is_ea_enabled(Path(f.name)) is True


def test_owner_run_gates_are_not_read_from_ea_config():
    """The full-mode eval and release-test switches start a model as the
    owner, so they live in the owner's evals.json (tests/evals/eval_gates.py),
    never in the resident-writable ea-config.json."""
    assert "eval_full_enabled" not in DEFAULTS
    assert "release_tests_enabled" not in DEFAULTS
    assert not hasattr(ea_config, "is_eval_full_enabled")
    assert not hasattr(ea_config, "is_release_tests_enabled")


def test_eval_postcommit_disabled():
    """eval_postcommit_enabled: false disables post-commit tests."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"eval_postcommit_enabled": False}, f)
        f.flush()
        assert is_eval_postcommit_enabled(Path(f.name)) is False


def test_underscore_keys_stripped():
    """Keys starting with _ are stripped (documentation fields)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"_doc": "should be stripped", "ea_enabled": True}, f)
        f.flush()
        cfg = load(Path(f.name))
    assert "_doc" not in cfg
    assert cfg["ea_enabled"] is True


def test_note_keys_preserved():
    """*_note keys are preserved (don't start with _)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"ea_enabled_note": "Master switch", "ea_enabled": True}, f)
        f.flush()
        cfg = load(Path(f.name))
    assert "ea_enabled_note" in cfg
    assert cfg["ea_enabled_note"] == "Master switch"


def test_partial_config_merges_with_defaults():
    """Config with only some keys merges with defaults for the rest."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"ea_enabled": False}, f)
        f.flush()
        cfg = load(Path(f.name))
    assert cfg["ea_enabled"] is False
    assert cfg["eval_postcommit_enabled"] is True  # default
    assert cfg["log_level"] == "INFO"  # default


def test_unknown_keys_preserved():
    """Unknown keys in the file don't break loading."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"ea_enabled": True, "future_toggle": "something"}, f)
        f.flush()
        cfg = load(Path(f.name))
    assert cfg["ea_enabled"] is True
    assert cfg["future_toggle"] == "something"


def test_log_level_valid():
    """Valid log levels are returned uppercased."""
    for level in ("debug", "INFO", "Warning", "ERROR"):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"log_level": level}, f)
            f.flush()
            assert get_log_level(Path(f.name)) == level.upper()


def test_log_level_invalid_returns_info():
    """Invalid log level falls back to INFO."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"log_level": "TRACE"}, f)
        f.flush()
        assert get_log_level(Path(f.name)) == "INFO"


def test_log_level_non_string_returns_info():
    """Non-string log level falls back to INFO."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"log_level": 42}, f)
        f.flush()
        assert get_log_level(Path(f.name)) == "INFO"


def test_missing_file_all_enabled():
    """Missing file means all toggles return True/enabled."""
    p = Path("/nonexistent/ea-config.json")
    assert is_ea_enabled(p) is True
    assert is_eval_postcommit_enabled(p) is True
    assert get_log_level(p) == "INFO"



def test_efforts_defaulted_and_validated():
    """Per-lane effort levels default to high for the room specialist and
    empty (CLI default) for the scheduler; a value that is not an effort
    level is ignored, an empty override is honored."""
    import ea_config
    assert ea_config.get_efforts(Path("/nonexistent/ea-config.json")) == {
        "room_specialist": "high", "scheduler": ""}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"efforts": {"room_specialist": "", "scheduler": "high",
                               "bogus_lane": "turbo"}}, f)
        f.flush()
        e = ea_config.get_efforts(Path(f.name))
    assert e == {"room_specialist": "", "scheduler": "high"}


def test_chat_sessions_default_to_one_session_per_topic():
    import ea_config
    assert ea_config.get_chat_sessions(Path("/nonexistent/ea-config.json")) == "per-topic"
    for val, want in (("resident", "resident"), ("per-topic", "per-topic"), ("bogus", "per-topic"),
                      (None, "per-topic")):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"chat_sessions": val}, f)
            f.flush()
            assert ea_config.get_chat_sessions(Path(f.name)) == want, val


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  \u2705 {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  \u274c {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
