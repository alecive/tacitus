#!/usr/bin/env python3
"""Discovery-based test wall: run EVERY tests/test_*.py, however styled.

Discovery finds every test file, and the structural dead-zone check
finds tests defined below a file's __main__ guard, which never run.

Per file:
  - has `if __name__` -> run `python3 <file>`; else -> pytest -q.
  - FAIL on: nonzero exit, "N failed" with N>0, pytest failures, or any
    top-level `def test_` BELOW the __main__ guard (dead zone).
Exit 0 only when every file passes. The post-commit hook calls this
once instead of maintaining a per-file block list.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
TIMEOUT = 240
# WALL_VERBOSE=1 prints the tail of a failing file's output, so a run on a
# machine no one is sitting at (a CI runner) still shows the traceback.
VERBOSE = bool(os.environ.get("WALL_VERBOSE"))
TAIL_LINES = 60



def _test_env() -> dict:
    """The environment for a test file: this one without git's repository
    variables (a hook exports GIT_DIR, GIT_INDEX_FILE and similar, and a
    fixture's git would otherwise act on the real repository), in the zone
    the suite states its clocks in (conftest.SUITE_TZ) unless one is set."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if not env.get("TZ"):
        sys.path.insert(0, str(TESTS))
        import conftest
        env["TZ"] = conftest.SUITE_TZ
    return env

def check_file(path: Path) -> tuple:
    """(ok, label) for one test file."""
    src = path.read_text()
    lines = src.splitlines()
    main_i = next((i for i, l in enumerate(lines)
                   if l.startswith("if __name__")), None)
    if main_i is not None:
        dead = [l for i, l in enumerate(lines)
                if i > main_i and re.match(r"^def test_", l)]
        if dead:
            return False, f"{len(dead)} test(s) DEFINED BELOW the runner"
        cmd = [sys.executable, str(path)]
    else:
        cmd = [sys.executable, "-m", "pytest", str(path), "-q"]
    try:
        # No terminal: code that would prompt (a Graph sign-in started from a
        # terminal waits for a browser) fails fast instead of hanging the wall.
        r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=TIMEOUT, cwd=str(TESTS.parent), env=_test_env())
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {TIMEOUT}s"
    out = r.stdout + r.stderr
    m = re.search(r"(\d+) passed(?:, (\d+) failed)?", out)
    if r.returncode != 0:
        tail = out.strip().splitlines()[-1][:80] if out.strip() else "no output"
        if VERBOSE:
            for line in out.splitlines()[-TAIL_LINES:]:
                print(f"      | {line}")
        return False, f"exit {r.returncode}: {tail}"
    if m and m.group(2) and int(m.group(2)) > 0:
        return False, f"{m.group(2)} failed"
    if re.search(r"OVERALL:.*\[FAIL\]", out):
        return False, "OVERALL [FAIL]"
    label = (f"{m.group(1)} passed" if m else
             "ok" if r.returncode == 0 else "?")
    om = re.search(r"OVERALL: (\d+)/(\d+)", out)
    if om:
        label = f"{om.group(1)}/{om.group(2)}"
    return True, label


def main() -> int:
    sys.path.insert(0, str(TESTS))
    import _tmp_retention
    _tmp_retention.prune()
    failures = 0
    files = sorted(p for p in TESTS.glob("test_*.py"))
    for path in files:
        ok, label = check_file(path)
        mark = "✅" if ok else "❌"
        print(f"  {mark} {path.name:38s} {label}")
        failures += (not ok)
    print(f"\n  wall: {len(files) - failures}/{len(files)} files clean")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
