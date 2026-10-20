#!/usr/bin/env python3
"""Tests for interactive-plugin trigger injection into prompt_logic.detect_skills.

detect_skills stays pure: callers inject plugin signals (duck-typed objects
with .name/.keywords/.patterns). These tests confirm a plugin's keyword and
regex triggers route to its skill, unrelated text doesn't, and built-in
skills are unaffected by the new parameter.
"""

import re
import sys
import traceback
from collections import namedtuple
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import prompt_logic  # noqa: E402

# Mirror plugin_registry.InteractivePlugin's duck-typed shape.
Sig = namedtuple("Sig", ["name", "keywords", "patterns"])

PROPERTY = Sig(
    name="property",
    keywords=("cleaner", "cleaners", "cleaning", "boathouse", "garden shed"),
    patterns=(re.compile(r"\b(re)?schedule\b.{0,40}\bclean(ing|er)\b"),),
)


def test_plugin_keyword_triggers_skill():
    skills = prompt_logic.detect_skills(
        "Inform our cleaners of the new reservation", False,
        plugin_signals=[PROPERTY])
    assert "property" in skills, skills


def test_plugin_pattern_triggers_skill():
    # No bare keyword "cleaning" here would be needed; the regex catches it.
    skills = prompt_logic.detect_skills(
        "Can you reschedule the weekly cleaner for the unit?", False,
        plugin_signals=[PROPERTY])
    assert "property" in skills, skills


def test_unrelated_message_no_plugin():
    skills = prompt_logic.detect_skills(
        "When is the NSF deadline?", False, plugin_signals=[PROPERTY])
    assert "property" not in skills, skills


LAKE = Sig(name="property", keywords=("lake", "str ", "condo"), patterns=())
NOTES = Sig(name="example-notes",
            keywords=("more reviewer", "ticket-202", "reopen tk-"), patterns=())


def test_a_keyword_inside_another_word_does_not_trigger():
    """A common false trigger is the keyword inside
    a longer word ("lake" in "Blake"); a keyword must start a word."""
    for msg in ("Blake can join the call next week",
                "The flakes are from the old paint",
                "Is the snowflake logo final?",
                "They construct the prototype this fall"):
        assert "property" not in prompt_logic.detect_skills(msg, False, plugin_signals=[LAKE]), msg


def test_a_keyword_that_starts_a_word_still_triggers():
    for msg in ("Lake condo cleaning on Friday", "renew the STR license", "(lake) update"):
        assert "property" in prompt_logic.detect_skills(msg, False, plugin_signals=[LAKE]), msg


def test_prefix_keywords_still_match_longer_words():
    for msg in ("find more reviewers for this one", "status of TICKET-2026-0312?",
                "please reopen TK-25-0417"):
        assert "example-notes" in prompt_logic.detect_skills(
            msg, False, plugin_signals=[NOTES]), msg


def test_builtin_skills_unaffected_by_injection():
    # A core skill ("reply") must still resolve with plugin_signals present.
    skills = prompt_logic.detect_skills(
        "draft a reply to Gus about the manuscript", False,
        plugin_signals=[PROPERTY])
    assert "reply" in skills
    assert "property" not in skills


def test_default_no_signals_is_core_only():
    # Backwards-compat: omitting plugin_signals behaves exactly as before.
    skills = prompt_logic.detect_skills("draft a reply to Bob", False)
    assert "reply" in skills


def test_plugin_name_not_duplicated():
    skills = prompt_logic.detect_skills(
        "cleaning cleaning boathouse garden shed", False, plugin_signals=[PROPERTY])
    assert skills.count("property") == 1, skills


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
