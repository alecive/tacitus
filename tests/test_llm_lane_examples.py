"""docs/llm-lanes.md shows what the lanes produce: its example blocks must
match what scripts/llm_lane_examples.py prints from the lanes' own code.
The script runs in a subprocess, on a made-up mailbox in a throwaway
private dir; nothing reaches the network or the real store."""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_the_doc_examples_match_the_code():
    p = subprocess.run([sys.executable, str(REPO / "scripts" / "llm_lane_examples.py"),
                        "--check", str(REPO / "docs" / "llm-lanes.md")],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr or p.stdout
