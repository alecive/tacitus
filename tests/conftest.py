"""Shared pytest fixtures.

Script-style test files (they run under `python3 tests/<file>.py`, which is
how the test wall runs any file with a __main__ block) hand every test a
fresh temporary directory as `d`. Under pytest the same tests get it here.

Many test files set os.environ["EA_DIR"] when they are imported. The wall
gives each file its own process; one pytest process imports every file
before it runs any test, so the last file's EA_DIR would be the one every
test sees. The value each file set is recorded as it is collected and put
back while that file's tests run.
"""
import os
import tempfile

import pytest

# Temp paths with no symlinked ancestor: the tree checks open each component
# without following a link, and macOS keeps /var behind one.
tempfile.tempdir = os.path.realpath(tempfile.gettempdir())

# A hook exports git's repository variables (GIT_DIR, GIT_INDEX_FILE,
# GIT_WORK_TREE, ...). Fixtures build their own repos with plain git, which
# would act on the real repository under them, so no test inherits any.
for _name in [n for n in os.environ if n.startswith("GIT_")]:
    del os.environ[_name]

# claude_cli probes the installed CLI's --help (as ea-bot, through sudo)
# for its bare-mode opt-out. No test command line should start a real CLI.
os.environ.setdefault("EA_CLAUDE_PROBE", "off")

# The calendar and scheduling tests state their clocks in one zone, the
# first install's. A host or runner in another zone runs the suite in it:
# set here for pytest, and by run_wall.py for the script-style files.
SUITE_TZ = "America/Denver"
if not os.environ.get("TZ"):
    import time as _time
    os.environ["TZ"] = SUITE_TZ
    _time.tzset()


def resident_child_available() -> bool:
    """True on a host with the two-user install: the ea-bot user exists
    and this user may run commands as it through sudo without a password,
    which is what an owner process's resident child needs. A bare CI
    runner has neither; the tests of that boundary skip there and run in
    the Docker image and on an installed host."""
    import pwd
    import shutil
    import subprocess
    try:
        pwd.getpwnam("ea-bot")
    except KeyError:
        return False
    if not shutil.which("sudo"):
        return False
    try:
        r = subprocess.run(["sudo", "-n", "-u", "ea-bot", "true"], capture_output=True,
                           timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


# For a file or a test that needs the resident child:
#   pytestmark = pytest.mark.skipif(not resident_child_available(), reason=NEEDS_RESIDENT)
NEEDS_RESIDENT = "needs the ea-bot user and passwordless sudo to it (the two-user install)"

_EA_DIR_BY_FILE: dict[str, str | None] = {}


def pytest_sessionstart(session):
    """Old sandboxes of earlier runs leave /tmp (_tmp_retention.py)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _tmp_retention
    _tmp_retention.prune()


def pytest_collectreport(report):
    if report.nodeid.endswith(".py"):
        _EA_DIR_BY_FILE[report.nodeid] = os.environ.get("EA_DIR")


@pytest.fixture(autouse=True, scope="module")
def _file_ea_dir(request):
    want = _EA_DIR_BY_FILE.get(request.node.nodeid)
    before = os.environ.get("EA_DIR")
    if want is not None:
        os.environ["EA_DIR"] = want
    yield
    if before is None:
        os.environ.pop("EA_DIR", None)
    else:
        os.environ["EA_DIR"] = before


@pytest.fixture
def d(tmp_path):
    return tmp_path
