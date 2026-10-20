"""Old test sandboxes leave /tmp.

Test files make their sandboxes with tempfile.mkdtemp(prefix=...) when they
are imported, and nothing removes them, so a sandbox stays after its run
for a look at what a failed test wrote. The tripwire test's holds about
4,700 files, and /tmp is a tmpfs with a fixed number of inodes: a few
hundred runs filled it, and the deploy's export then failed for want of a
temporary directory. Every pytest session (conftest.py) and every wall run
(run_wall.py) removes this user's sandboxes older than KEEP_HOURS. The
prefixes are read from the test files themselves, so a new test's sandbox
is covered without a list to keep.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path

KEEP_HOURS = 24
TESTS = Path(__file__).resolve().parent
_PREFIX_RE = re.compile(r"""mkdtemp\(\s*prefix=["']([A-Za-z0-9_.-]+)["']""")


def prefixes(tests: Path = TESTS) -> set[str]:
    """Every mkdtemp prefix a test file names. A prefix shorter than four
    characters is too loose to match on and is left out."""
    found: set[str] = set()
    for path in tests.rglob("*.py"):
        try:
            found.update(m for m in _PREFIX_RE.findall(path.read_text(errors="replace"))
                         if len(m) >= 4)
        except OSError:
            continue
    return found


def prune(keep_hours: float = KEEP_HOURS, *, tmp: str | None = None,
          now: float | None = None, tests: Path = TESTS) -> int:
    """Remove this user's test sandboxes in `tmp` older than `keep_hours`.
    Returns how many went. Never raises: a test run must not fail on it."""
    tmp = tmp or tempfile.gettempdir()
    cutoff = (now if now is not None else time.time()) - keep_hours * 3600
    names = tuple(prefixes(tests))
    if not names:
        return 0
    uid, gone = os.getuid(), 0
    try:
        entries = list(os.scandir(tmp))
    except OSError:
        return 0
    for e in entries:
        try:
            if not e.name.startswith(names) or not e.is_dir(follow_symlinks=False):
                continue
            st = e.stat(follow_symlinks=False)
            if st.st_uid != uid or st.st_mtime > cutoff:
                continue
            shutil.rmtree(e.path, ignore_errors=True)
            gone += 1
        except OSError:
            continue
    return gone
