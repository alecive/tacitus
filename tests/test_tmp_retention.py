"""Old test sandboxes leave /tmp; recent ones, other users' and other
programs' directories stay (tests/_tmp_retention.py)."""
import os
import time
from pathlib import Path

import _tmp_retention as tr


def test_prune_takes_only_old_sandboxes_of_known_prefixes(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text('SANDBOX = tempfile.mkdtemp(prefix="ea-test-tripwire-")\n'
                                     'D = tempfile.mkdtemp(prefix="ab")\n')
    assert tr.prefixes(tests) == {"ea-test-tripwire-"}
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    now = time.time()
    old, new, other = tmp / "ea-test-tripwire-old", tmp / "ea-test-tripwire-new", tmp / "editor-old"
    for d in (old, new, other):
        (d / "case-1").mkdir(parents=True)
    (tmp / "ea-test-tripwire-file").write_text("not a dir")
    for d in (old, other, tmp / "ea-test-tripwire-file"):
        os.utime(d, (now - 30 * 3600, now - 30 * 3600))
    assert tr.prune(24, tmp=str(tmp), now=now, tests=tests) == 1
    assert not old.exists() and new.exists() and other.exists()
    assert (tmp / "ea-test-tripwire-file").exists()


def test_the_real_tests_name_the_tripwire_sandbox():
    assert "ea-test-tripwire-" in tr.prefixes()
