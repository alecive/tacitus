#!/usr/bin/env python3
"""Tests for preprocessor/plugin_registry.py (interactive-plugin discovery).

Interactive plugins declare a `slack` block in plugin.json; the registry
scans ~/.ea/plugins and returns one InteractivePlugin per enabled plugin
that has one. These tests drive a synthetic plugins dir so nothing touches
the real ~/.ea.
"""

import json
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import plugin_registry as pr  # noqa: E402


def _reset_cache():
    pr._CACHE["sig"] = None
    pr._CACHE["plugins"] = None


def _make_plugin(plugins_dir: Path, name: str, manifest: dict,
                 skill: str | None = "skill body"):
    d = plugins_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps(manifest))
    if skill is not None:
        (d / "skill.md").write_text(skill)
    return d


def _slack_manifest(name, keywords=None, patterns=None, enabled=True, **extra):
    m = {"name": name, "enabled": enabled,
         "slack": {"skill": "skill.md"}}
    if keywords is not None:
        m["slack"]["keywords"] = keywords
    if patterns is not None:
        m["slack"]["patterns"] = patterns
    m.update(extra)
    return m


def test_discovers_enabled_slack_plugin():
    _reset_cache()
    with tempfile.TemporaryDirectory() as td:
        pdir = Path(td)
        _make_plugin(pdir, "property",
                     _slack_manifest("property", keywords=["cleaner", "gutter"]))
        plugins = pr.discover_interactive_plugins(pdir)
        assert len(plugins) == 1, plugins
        p = plugins[0]
        assert p.name == "property"
        assert "cleaner" in p.keywords
        assert p.skill_path.name == "skill.md" and p.skill_path.exists()


def test_chat_block_is_an_alias_and_slack_wins():
    """A manifest may name its interactive block `chat`; with both, the
    `slack` block is the one read."""
    _reset_cache()
    with tempfile.TemporaryDirectory() as td:
        pdir = Path(td)
        _make_plugin(pdir, "neutral", {"name": "neutral", "enabled": True,
                                       "chat": {"skill": "skill.md", "keywords": ["gutter"]}})
        _make_plugin(pdir, "both", {"name": "both", "enabled": True,
                                    "slack": {"skill": "skill.md", "keywords": ["slackword"]},
                                    "chat": {"skill": "skill.md", "keywords": ["chatword"]}})
        plugins = {p.name: p for p in pr.discover_interactive_plugins(pdir)}
        assert "gutter" in plugins["neutral"].keywords
        assert "slackword" in plugins["both"].keywords
        assert "chatword" not in plugins["both"].keywords


def test_skips_disabled_and_scheduled_only():
    _reset_cache()
    with tempfile.TemporaryDirectory() as td:
        pdir = Path(td)
        _make_plugin(pdir, "off",
                     _slack_manifest("off", keywords=["x"], enabled=False))
        # scheduled-only: enabled but NO slack block
        _make_plugin(pdir, "sched",
                     {"name": "sched", "enabled": True, "schedule": "0 3 * * *"})
        plugins = pr.discover_interactive_plugins(pdir)
        assert plugins == [], [p.name for p in plugins]


def test_bad_manifest_does_not_raise():
    _reset_cache()
    with tempfile.TemporaryDirectory() as td:
        pdir = Path(td)
        d = pdir / "broken"
        d.mkdir()
        (d / "plugin.json").write_text("{ this is not json ")
        _make_plugin(pdir, "good", _slack_manifest("good", keywords=["hi"]))
        plugins = pr.discover_interactive_plugins(pdir)
        # broken skipped, good still discovered
        assert [p.name for p in plugins] == ["good"]


def test_missing_skill_file_skipped():
    _reset_cache()
    with tempfile.TemporaryDirectory() as td:
        pdir = Path(td)
        _make_plugin(pdir, "noskill",
                     _slack_manifest("noskill", keywords=["x"]), skill=None)
        plugins = pr.discover_interactive_plugins(pdir)
        assert plugins == []


def test_bad_regex_dropped_plugin_kept():
    _reset_cache()
    with tempfile.TemporaryDirectory() as td:
        pdir = Path(td)
        _make_plugin(pdir, "p",
                     _slack_manifest("p", keywords=["ok"], patterns=["(unclosed"]))
        plugins = pr.discover_interactive_plugins(pdir)
        assert len(plugins) == 1
        assert plugins[0].patterns == ()  # bad regex dropped
        assert "ok" in plugins[0].keywords


def test_no_triggers_skipped():
    _reset_cache()
    with tempfile.TemporaryDirectory() as td:
        pdir = Path(td)
        _make_plugin(pdir, "empty", _slack_manifest("empty"))  # no kw/patterns
        assert pr.discover_interactive_plugins(pdir) == []


def test_patterns_compile():
    _reset_cache()
    with tempfile.TemporaryDirectory() as td:
        pdir = Path(td)
        _make_plugin(pdir, "p",
                     _slack_manifest("p", patterns=[r"\bclean(ing|er)\b"]))
        plugins = pr.discover_interactive_plugins(pdir)
        assert len(plugins) == 1
        assert plugins[0].patterns[0].search("schedule a cleaning")


def test_cache_hit_then_invalidates_on_new_plugin():
    _reset_cache()
    with tempfile.TemporaryDirectory() as td:
        pdir = Path(td)
        _make_plugin(pdir, "a", _slack_manifest("a", keywords=["a"]))
        first = pr.discover_interactive_plugins(pdir)
        again = pr.discover_interactive_plugins(pdir)
        assert again is first, "same dir signature should hit cache"
        # Add a second plugin -> signature changes -> rescan.
        _make_plugin(pdir, "b", _slack_manifest("b", keywords=["b"]))
        after = pr.discover_interactive_plugins(pdir)
        assert {p.name for p in after} == {"a", "b"}


def test_missing_dir_returns_empty():
    _reset_cache()
    assert pr.discover_interactive_plugins(Path("/nonexistent/xyz")) == []


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
    sys.exit(1 if failed else 0)
