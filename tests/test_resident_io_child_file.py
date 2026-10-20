"""The resident_io child as its own file.

An owner process names preprocessor/resident_io_child.py (as installed under
/opt/ea-system) on the child's command line in place of the program text, so
sudo logs a short line. These pin:

  * the committed file is `_CHILD` after comment lines only, so the two forms
    run the same program;
  * the file form's argv, and the same ops through it as inline;
  * the file is named only when the resident can read it, only root or the
    owner can change it and every directory above it, no component is a
    link, and it holds `_CHILD`; otherwise the inline form is used;
  * off the install (a scratch EA tree) the inline form is used;
  * a sudo policy that refuses the file form but allows the inline one keeps
    the inline form, and one that refuses both selects the direct call.

No test runs sudo: the child runs as the test user through `_spawn`, and the
preflight runs against a fake subprocess.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

EA_DIR = Path(tempfile.mkdtemp(prefix="ea-test-rio-child-file-"))
os.environ["EA_DIR"] = str(EA_DIR)
(EA_DIR / "state").mkdir(parents=True, exist_ok=True)

import resident_io as rio  # noqa: E402

from conftest import NEEDS_RESIDENT, resident_child_available

# The resident child needs the two-user install; a bare runner skips.
pytestmark = pytest.mark.skipif(not resident_child_available(), reason=NEEDS_RESIDENT)

FILE = ROOT / "preprocessor" / "resident_io_child.py"
# A resident that is neither this user nor in any of its groups.
STRANGER = (2 ** 31 - 7, {2 ** 31 - 7})


def _pipes(argv):
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd="/", env=dict(rio.CHILD_ENV))


@pytest.fixture
def file_form(monkeypatch):
    """An owner process with the child allowed, the program named by file."""
    calls = []

    def spawn(argv):
        calls.append(list(argv))
        return _pipes(argv)

    monkeypatch.setattr(rio, "_owner_check", lambda: True)
    monkeypatch.setattr(rio, "_availability", lambda: True)
    monkeypatch.setattr(rio, "_spawn", spawn)
    monkeypatch.setattr(rio, "_child_file", lambda: str(FILE))
    return calls


def _stat(mode: int, uid: int = 0, gid: int = 0) -> os.stat_result:
    return os.stat_result((mode, 1, 1, 1, uid, gid, 0, 0, 0, 0))


# ── one program, two forms ────────────────────────────────────────────

def test_the_file_is_the_inline_program_after_comments():
    data = FILE.read_bytes()
    assert rio._holds_program(data)
    head = data[:-len(rio._CHILD.encode())].decode()
    assert head.strip() and all(line.startswith("#") for line in head.splitlines())


def test_other_text_is_not_the_program():
    body = rio._CHILD.encode()
    assert not rio._holds_program(b"import os\n" + body)
    assert not rio._holds_program(b"# header\n" + body + b"print(1)\n")
    assert not rio._holds_program(b"# header\n" + body.replace(b"MAX_READ = ", b"MAX_READ = 1"))
    assert not rio._holds_program(b"")


def test_the_file_form_argv_is_short(monkeypatch):
    monkeypatch.setattr(rio, "_child_file", lambda: "/opt/ea-system/preprocessor/resident_io_child.py")
    argv = rio._child_argv("append", "/x/y.log", 0o664)
    assert argv == [rio._resident_python(), "-I", "/opt/ea-system/preprocessor/resident_io_child.py",
                    "append", "664", "/x/y.log"]
    assert rio._CHILD not in argv and sum(map(len, argv)) < 200


def test_the_ops_through_the_file_form(file_form):
    with tempfile.TemporaryDirectory(dir=EA_DIR / "state") as td:
        d = Path(td)
        log = d / "logs" / "a.log"
        assert rio.append_text(log, "one\n") and rio.append_text(log, "two\n")
        assert log.read_text() == "one\ntwo\n"
        assert rio.replace_text(d / "r.json", "{}", mode=0o640)
        assert stat.S_IMODE(os.lstat(d / "r.json").st_mode) == 0o640
        assert rio.update_text(d / "r.json", lambda old: old + "\n")
        assert rio.read_text(d / "r.json") == "{}\n"
        assert rio.read_bytes(d / "missing") is None
        assert rio.make_dir(d / "sub", 0o2770) and (d / "sub").is_dir()
        assert rio.ensure_file(d / "sub" / "x.lock") and (d / "sub" / "x.lock").exists()
        assert rio.move(d / "r.json", d / "sub" / "r.json") and (d / "sub" / "r.json").exists()
        victim = d / "victim"
        victim.write_text("owner only")
        (d / "link.log").symlink_to(victim)
        assert rio.append_text(d / "link.log", "x") is False
        assert victim.read_text() == "owner only"
    assert file_form and all(argv[2] == str(FILE) for argv in file_form)


# ── when the file is usable ───────────────────────────────────────────

def test_a_component_must_be_fixed_and_open_to_the_resident():
    uid, gids = STRANGER
    fd = os.open("/", os.O_RDONLY)
    try:
        ok = rio._fixed_for
        assert ok(fd, _stat(0o40755), uid, gids, 1)
        assert ok(fd, _stat(0o100644, uid=os.geteuid()), uid, gids, 4)
        assert ok(fd, _stat(0o40750, gid=next(iter(gids))), uid, gids, 1)
        assert not ok(fd, _stat(0o40775), uid, gids, 1), "group-writable"
        assert not ok(fd, _stat(0o40757), uid, gids, 1), "writable by others"
        assert not ok(fd, _stat(0o40755, uid=uid), uid, gids, 1), "the resident's own"
        assert not ok(fd, _stat(0o40750), uid, gids, 1), "not searchable by the resident"
        assert not ok(fd, _stat(0o100640), uid, gids, 4), "not readable by the resident"
    finally:
        os.close(fd)


def test_the_file_is_not_usable_through_a_link_or_a_writable_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(rio, "_resident_ids", lambda: STRANGER)
    monkeypatch.setattr(rio, "_holds_program", lambda data: True)
    real = tmp_path / "child.py"
    real.write_text(FILE.read_text())
    (tmp_path / "link.py").symlink_to(real)
    for path in (str(real), str(tmp_path / "link.py"), "relative/child.py",
                 "/opt/../opt/child.py", "/", str(tmp_path / "missing.py")):
        assert rio._child_file_usable(path) is False, path


def test_the_file_is_usable_in_a_fixed_tree(monkeypatch):
    """A root-owned tree every user can read (the interpreter's own library
    stands in for /opt/ea-system)."""
    path = os.path.realpath(os.__file__)
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        st = os.lstat("/".join(parts[:i]) or "/")
        if st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022 or not st.st_mode & 0o005:
            pytest.skip(f"{path} is not in a fixed tree here")
    monkeypatch.setattr(rio, "_resident_ids", lambda: STRANGER)
    monkeypatch.setattr(rio, "_holds_program", lambda data: True)
    assert rio._child_file_usable(path) is True
    monkeypatch.setattr(rio, "_holds_program", lambda data: False)
    assert rio._child_file_usable(path) is False


def test_off_the_install_the_inline_form_is_used(monkeypatch):
    monkeypatch.setattr(rio, "CHILD_FILE", os.path.realpath(os.__file__))
    monkeypatch.setattr(rio, "_child_file_usable", lambda path: True)
    assert rio._default_child_file() is None
    assert rio._child_argv("read", "/a", 0o664)[2:4] == ["-c", rio._CHILD]


def test_the_owner_process_names_a_usable_file_once(monkeypatch):
    seen = []
    monkeypatch.setattr(rio, "_owner_trust",
                        lambda: types.SimpleNamespace(is_owner_process=lambda: True))
    monkeypatch.setattr(rio, "CHILD_FILE", "/opt/x/resident_io_child.py")
    monkeypatch.setattr(rio, "_CHILD_FILE_OK", {})
    monkeypatch.setattr(rio, "_child_file_usable", lambda path: seen.append(path) or True)
    assert rio._default_child_file() == "/opt/x/resident_io_child.py"
    assert rio._default_child_file() == "/opt/x/resident_io_child.py"
    assert seen == ["/opt/x/resident_io_child.py"]
    monkeypatch.setattr(rio, "_CHILD_FILE_OK", {})
    monkeypatch.setattr(rio, "_child_file_usable", lambda path: False)
    assert rio._default_child_file() is None


def test_a_broken_file_decision_is_the_inline_form(monkeypatch):
    def boom():
        raise RuntimeError("no answer")

    monkeypatch.setattr(rio, "_child_file", boom)
    assert rio._child_argv("read", "/a", 0o664)[2:4] == ["-c", rio._CHILD]


# ── the preflight with the file form ──────────────────────────────────

def _policy(allow_file: bool, allow_inline: bool, seen: list):
    def run(cmd, **kw):
        seen.append(cmd)
        inline = "-c" in cmd
        ok = allow_inline if inline else allow_file
        return types.SimpleNamespace(returncode=0 if ok else 1, stdout=b"", stderr=b"")
    return types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)


@pytest.mark.parametrize("allow_file, allow_inline, available, inline_kept", [
    (True, True, True, False),
    (False, True, True, True),
    (False, False, False, True),
])
def test_the_preflight_asks_for_the_inline_form_when_the_file_form_is_refused(
        monkeypatch, allow_file, allow_inline, available, inline_kept):
    seen = []
    monkeypatch.setattr(rio, "pwd", types.SimpleNamespace(getpwnam=lambda n: object()))
    monkeypatch.setattr(rio, "subprocess", _policy(allow_file, allow_inline, seen))
    monkeypatch.setattr(rio, "_child_file", lambda: str(FILE))
    monkeypatch.setattr(rio, "_INLINE_ONLY", False)
    assert rio._probe_resident() is available
    assert all(cmd[1:3] == ["-n", "-l"] for cmd in seen)
    assert seen[0][7] == str(FILE)
    assert rio._INLINE_ONLY is inline_kept
    form = rio._child_argv("append", "/a", 0o664)[2]
    assert form == ("-c" if inline_kept else str(FILE))
