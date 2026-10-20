#!/usr/bin/env python3
"""Tests for preprocessor/style_guard.py + diction-rule wiring pins.

The dash rewrite covers only Outlook draft bodies, so Slack reply text
needs its own guard:
  - strip_em_dashes() is the deterministic backstop on all Slack output
  - the resident dispatch protocol injects STYLE_RULES per message
"""

import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

from style_guard import strip_em_dashes  # noqa: E402

BOT_SRC = (HERE.parent / "ea-chat-bot.py").read_text()


def test_em_dash_joint_becomes_comma():
    out, n = strip_em_dashes("The PSR is in — approvals are next.")
    assert out == "The PSR is in, approvals are next."
    assert n == 1


def test_em_dash_no_spaces():
    out, n = strip_em_dashes("deadline—June 19")
    assert out == "deadline, June 19"
    assert n == 1


def test_double_hyphen_joint():
    out, n = strip_em_dashes("it works -- mostly.")
    assert out == "it works, mostly."
    assert n == 1


def test_en_dash_ranges_preserved():
    # Briefings legitimately use en-dash ranges; only em-dash is banned.
    text = "Panel 7:00 AM – 1:00 PM, Jun 19–30 window"
    out, n = strip_em_dashes(text)
    assert out == text
    assert n == 0


def test_code_spans_untouched():
    text = "Run `graph_actions.py --body x — test` now — really."
    out, n = strip_em_dashes(text)
    assert "`graph_actions.py --body x — test`" in out
    assert out.endswith("now, really.")
    assert n == 1


def test_fenced_block_untouched():
    text = "```\na — b\n```\noutside — text"
    out, n = strip_em_dashes(text)
    assert "a — b" in out
    assert "outside, text" in out
    assert n == 1


def test_cli_double_dash_flags_untouched():
    text = "use --body-file and --conversation-id"
    out, n = strip_em_dashes(text)
    assert out == text and n == 0


def test_clean_text_fast_path():
    out, n = strip_em_dashes("no dashes here at all")
    assert n == 0


def test_multiple_em_dashes():
    out, n = strip_em_dashes("a — b — c")
    assert out == "a, b, c"
    assert n == 2


# ---------------------------------------------------------------------------
# Wiring pins (source-level): the diction rules must reach every lane
# ---------------------------------------------------------------------------

def test_resident_protocol_injects_style_rules():
    """The resident must carry STYLE_RULES + per-recipient style guidance.
    They live in resident_context.PRIMER, injected once per session by
    build_dispatch, not inline in _try_resident_dispatch."""
    # The dispatch path must route through the smart-context builder.
    start = BOT_SRC.index("def _try_resident_dispatch")
    end = BOT_SRC.index("def handle_message_async")
    block = BOT_SRC[start:end]
    assert "build_dispatch" in block, \
        "resident dispatch no longer routes through resident_context.build_dispatch"
    # The PRIMER must embed STYLE_RULES and the per-recipient lookup.
    import resident_context as rcx
    from prompt_logic import STYLE_RULES
    assert STYLE_RULES.strip()[:60] in rcx.PRIMER, \
        "PRIMER lost its STYLE_RULES embedding"
    assert "style_profiles.py" in rcx.PRIMER and "--contact" in rcx.PRIMER, \
        "PRIMER lost the per-recipient style lookup"


def test_scrub_wired_into_all_slack_output_paths():
    for fn, needle in [
        ("_try_resident_dispatch", "strip_em_dashes"),
        ("resident_router_loop", "strip_em_dashes"),
        ("handle_message_async", "strip_em_dashes"),
    ]:
        start = BOT_SRC.index(f"def {fn}")
        # the whole function: up to the next column-0 def (nested defs are indented)
        end = BOT_SRC.find("\ndef ", start + 1)
        block = BOT_SRC[start:end if end > 0 else len(BOT_SRC)]
        assert needle in block, f"{fn} lost its style_guard scrub"


def test_style_rules_still_ban_em_dashes():
    from prompt_logic import STYLE_RULES
    assert "em-dash" in STYLE_RULES.lower() or "em dash" in STYLE_RULES.lower()
    # Per-recipient voice matching is driven by the style_profiles DB.
    assert "style_profiles.py" in STYLE_RULES and "--contact" in STYLE_RULES


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
