"""The wall runs every test with no terminal on stdin. With one, a Graph
sign-in started by code under test (a ledger close archives through Graph)
opens a listener and waits for a browser, and the deploy hook's wall hung
on it until the 240 s timeout."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_wall_gives_each_test_no_terminal():
    src = (ROOT / "tests" / "run_wall.py").read_text()
    call = re.search(r"subprocess\.run\(cmd,[^)]*\)", src, re.S).group(0)
    assert "stdin=subprocess.DEVNULL" in call


def test_the_audit_block_test_runs_its_shell_with_no_terminal():
    src = (ROOT / "tests" / "test_open_claims_audit.py").read_text()
    calls = re.findall(r"subprocess\.run\((.*?)\)\n", src, re.S)
    assert calls and all("stdin=subprocess.DEVNULL" in c for c in calls)
