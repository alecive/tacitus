#!/usr/bin/env python3
"""Shell-level tests for run-agent.sh's preamble stripper.

The stripper drops the reasoning paragraphs Claude sometimes emits above
the `:sunny:` anchor. Scanning the WHOLE document for the first
`:shortcode:` line turns it into "keep only the tail" for any agent that
does not open with a shortcode: an agent whose headers are unicode emoji
keeps only its last section, and `validate_output` anchor-checks only
morning-briefing and afternoon-catchup, so the truncated tail would pass
validation and post.

These tests drive the REAL `strip_preamble` function out of run-agent.sh
through bash, so a rewrite of the shell has to keep the behaviour.

Usage:
    python3 tests/test_run_agent_preamble.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUN_AGENT = REPO / "run-agent.sh"

_FUNC_RE = re.compile(r"^strip_preamble\(\)\s*\{\n(.*?)\n\}\s*$",
                      re.DOTALL | re.MULTILINE)


def _extract_func() -> str:
    """Pull the strip_preamble() definition out of run-agent.sh.

    Fails loudly rather than silently skipping: a rename or a rewrite to
    an inline copy must break this file, since that drift is what put a
    second, unbounded scanner in the script.
    """
    src = RUN_AGENT.read_text()
    matches = _FUNC_RE.findall(src)
    assert len(matches) == 1, (
        f"expected exactly 1 strip_preamble() definition in {RUN_AGENT}, "
        f"found {len(matches)} — did someone inline a second copy?")
    return f"strip_preamble() {{\n{matches[0]}\n}}"


def _run(text: str, limit: str = "12") -> str:
    script = (
        f'PYTHON3="{sys.executable}"\n'
        f'STRIP_PREAMBLE_MAX_LINES={limit}\n'
        f"{_extract_func()}\n"
        'strip_preamble\n'
    )
    r = subprocess.run(["bash", "-c", script], input=text,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (r.returncode, r.stderr)
    return r.stdout


# ── the behaviour the stripper is FOR ───────────────────────────────

def test_preamble_above_anchor_is_dropped():
    text = ("I'll write the briefing now.\n"
            "The ledger command needs approval, so I skipped it.\n"
            ":sunny: *Morning Briefing*\n"
            "- item one\n")
    out = _run(text)
    assert out.startswith(":sunny:"), out
    assert "I'll write the briefing" not in out


def test_output_already_anchored_is_unchanged():
    text = ":sunny: *Morning Briefing*\n- item one\n- item two"
    assert _run(text) == text


def test_indented_anchor_still_matches():
    text = "preamble line\n   :coffee: *Afternoon catchup*\nbody"
    out = _run(text)
    assert out.startswith("   :coffee:"), out


# ── the mid-document anchor ─────────────────────────────────────────

def test_mid_document_anchor_does_not_truncate():
    """A document with unicode headers whose only :shortcode: line is the
    last section keeps every section above it."""
    body = ["📊 *Weekly Audit — Week of Aug 3*", ""]
    for i in range(8):
        body += [f"📋 section {i} line", "  detail", ""]
    body += ["⚠️ Plus *104 open items invisible to the briefing*", "",
             ":gear: *Closure automation (last 7d):*", "  4 auto-closures"]
    text = "\n".join(body)
    out = _run(text)
    assert out == text, (
        "mid-document anchor truncated the report: "
        f"{len(text.splitlines())} lines in, {len(out.splitlines())} out")
    assert out.startswith("📊"), out


def test_no_anchor_at_all_is_verbatim():
    text = "State updated.\nHere is a summary of what was done.\n"
    # Emitted verbatim on purpose, so validate_output can flag it.
    assert _run(text) == text


def test_limit_is_the_boundary():
    """An anchor exactly at the limit strips; one past it does not."""
    at_limit = "\n".join(["pre"] * 4 + [":sunny: body"])
    assert _run(at_limit, limit="5").startswith(":sunny:")
    past_limit = "\n".join(["pre"] * 5 + [":sunny: body"])
    assert _run(past_limit, limit="5") == past_limit


def test_empty_input():
    assert _run("") == ""


# ── both call sites share one implementation ────────────────────────

def test_both_call_sites_use_the_shared_function():
    src = RUN_AGENT.read_text()
    calls = re.findall(r"^\s*CLAUDE_OUTPUT=\$\(strip_preamble\b", src,
                       re.MULTILINE)
    assert len(calls) == 2, (
        f"expected 2 strip_preamble call sites (main + retry), found "
        f"{len(calls)}")


def test_scan_is_bounded_and_truncates_in_one_place():
    """The anchor scan is bounded and truncates in exactly one place. An
    unbounded `enumerate(lines)` scan whose body writes `lines[i:]` keeps
    only the tail."""
    func = _extract_func()
    assert "lines[:limit]" in func, (
        "the anchor scan is not bounded — an unbounded scan turns "
        "'strip the preamble' into 'keep only the tail'")
    assert func.count("join(lines[i:])") == 1, (
        "more than one truncation site in strip_preamble(); there must be "
        "exactly one, inside the bounded scan")


# ── the rules this lane cannot read anywhere else ───────────────────

def test_prompt_carries_the_rules_claude_md_cannot_reach_here():
    """run-agent.sh runs `cd $EA_BOT_HOME && claude -p`, so the CLAUDE.md
    under $EA_BOT_HOME/ea-system is NOT auto-loaded and the prompt only
    prints its PATH. The agent opens the briefing and nothing else, so
    every rule the cron lane needs has to be IN this prompt.
    """
    src = RUN_AGENT.read_text()
    prompt = src[src.index('PROMPT="You are running as the EA agent'):]
    prompt = prompt[:prompt.index("\nBegin now.")]
    for needle, why in (
        ("NOT PROOF THAT NOTHING HAPPENED",
         "a search that finds nothing licensed the false 'lost/deleted' claim"),
        ("--folder reads ONE folder",
         "sentitems is exactly where an archived reply is not"),
        ("get-conversation --conversation-id",
         "the check that would have found the real thread"),
        ("NEVER answer an existing thread with create-draft",
         "the threadless re-draft with the invented subject"),
    ):
        assert needle in prompt, f"cron prompt lost the rule about {why}"


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
