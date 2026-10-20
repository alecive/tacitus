"""The conversation index shows a bot dispatch as the words asked.

The resident's bridge reads dispatch markers in tool output, so an index
the resident runs must never print a marker back into the pane: an echo
of the marker of the dispatch in progress once hid its reply.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import resident_bridge as rb  # noqa: E402
import session_index as si  # noqa: E402


def test_dispatch_shown_without_its_marker():
    marker = rb.make_marker("stream:4:topic:Property%20Dashboard", "2583")
    text = f"{marker} Kit says:\nAll this is closed"
    assert si._dispatch_words(text) == "All this is closed"
    assert rb.DISPATCH_MARKER_PREFIX not in si._dispatch_words(f"PRIMER\n\n{text}")
    assert si._dispatch_words("typed in the shell") == "typed in the shell"


if __name__ == "__main__":
    test_dispatch_shown_without_its_marker()
    print("OVERALL: [PASS]")
