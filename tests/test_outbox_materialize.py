#!/usr/bin/env python3
"""The owner uploads a staged outbox file only with the RESIDENT's access.

The outbox is ea-bot-writable, so a file staged for upload can be swapped for a
symlink to a crh-only file before the owner (the bot, crh) reads it. These pin
the fix: the owner reads the staged file with the resident's access into an
owner-private copy and uploads that copy; a symlink at the staged path is
refused; the direct owner read is used only when the once-per-process sudo
list-mode preflight fails; after a passing preflight every child outcome other
than a clean framed reply raises; the child runs as `python3 -I`.
"""
import io
import os
import stat
import subprocess
import sys
import tempfile
import traceback
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import slack_outbox as SO  # noqa: E402


def _mkfile(d: Path, name="pic.png", data=b"\x89PNG\r\n\x1a\nDATA") -> Path:
    p = d / name
    p.write_bytes(data)
    return p


def _fake_subprocess(run):
    return types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)


class _State:
    """Save and restore the module state a test changes."""

    def __enter__(self):
        self.saved = (SO._RESIDENT_OK, SO.subprocess, SO._probe_resident,
                      SO._read_staged_file, SO._read_runner)
        return self

    def __exit__(self, *exc):
        (SO._RESIDENT_OK, SO.subprocess, SO._probe_resident,
         SO._read_staged_file, SO._read_runner) = self.saved


def test_materialize_copies_into_owner_private_reading_as_resident():
    with tempfile.TemporaryDirectory() as td, _State():
        base = Path(td)
        src = _mkfile(base, "meme.png", b"IMG-BYTES")
        priv = base / "private"
        calls = []
        real_read = SO._read_staged_file
        SO._read_runner = lambda p, **kw: calls.append(p) or real_read(p)
        tmp = SO.materialize_for_owner(src, private_root=priv)
        assert calls == [str(src)]
        assert str(tmp).startswith(str(priv / "upload-staging"))
        assert tmp != src
        assert tmp.read_bytes() == b"IMG-BYTES"
        assert stat.S_IMODE(os.stat(tmp).st_mode) == 0o600
        SO.discard_materialized(tmp)
        assert not tmp.exists()


def test_read_staged_file_refuses_a_symlink():
    """The old consumer uploaded a swapped symlink's target; the O_NOFOLLOW
    open refuses it."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        victim = _mkfile(base, "secret.txt", b"CRH-ONLY")
        link = base / "staged"
        try:
            os.symlink(victim, link)
        except OSError:
            return
        try:
            SO._read_staged_file(str(link))
            assert False, "a symlinked staged path must be refused"
        except OSError:
            pass


def test_read_staged_file_refuses_multiply_linked():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        victim = _mkfile(base, "victim", b"CRH-ONLY")
        hard = base / "staged"
        try:
            os.link(victim, hard)
        except OSError:
            return
        try:
            SO._read_staged_file(str(hard))
            assert False, "a multiply-linked staged path must be refused"
        except SO.OutboxReadError:
            pass


def test_materialize_propagates_a_read_refusal():
    with tempfile.TemporaryDirectory() as td, _State():
        base = Path(td)
        src = _mkfile(base)

        def refuse(p, **kw):
            raise SO.OutboxReadError("cannot read as the resident")

        SO._read_runner = refuse
        try:
            SO.materialize_for_owner(src, private_root=base / "p")
            assert False, "a read refusal must propagate"
        except SO.OutboxReadError:
            pass
        assert not (base / "p" / "upload-staging").exists() or \
            not list((base / "p" / "upload-staging").iterdir())


# --- (1) availability comes from a preflight only --------------------------

def test_preflight_is_a_list_mode_check_of_the_isolated_reader():
    with _State():
        seen = []

        def outcome(rc=None, exc=None):
            def run(cmd, **kw):
                seen.append(cmd)
                if exc:
                    raise exc
                return types.SimpleNamespace(stdout=b"", stderr=b"", returncode=rc)
            return _fake_subprocess(run)

        import pwd
        real_getpwnam = pwd.getpwnam
        pwd.getpwnam = lambda n: object()
        try:
            SO.subprocess = outcome(rc=0)
            assert SO._probe_resident() is True
            SO.subprocess = outcome(rc=1)
            assert SO._probe_resident() is False
            SO.subprocess = outcome(exc=FileNotFoundError("sudo"))
            assert SO._probe_resident() is False
        finally:
            pwd.getpwnam = real_getpwnam
        cmd = seen[0]
        assert cmd[:5] == ["sudo", "-n", "-l", "-u", SO.RESIDENT_USER]
        assert cmd[6] == "-I" and SO.READ_AS_RESIDENT in cmd


def test_failed_preflight_reads_directly_without_starting_a_child():
    with tempfile.TemporaryDirectory() as td, _State():
        base = Path(td)
        src = _mkfile(base, "s.txt", b"DIRECT-BYTES")
        SO._probe_resident = lambda: False
        SO._RESIDENT_OK = None
        execs = []
        SO.subprocess = _fake_subprocess(lambda cmd, **k: execs.append(cmd))
        assert SO._resident_read(str(src)) == b"DIRECT-BYTES"
        assert execs == []


def test_preflight_runs_once_per_process():
    with tempfile.TemporaryDirectory() as td, _State():
        base = Path(td)
        src = _mkfile(base, "s.txt", b"x")
        probes = []
        SO._probe_resident = lambda: probes.append(1) or False
        SO._RESIDENT_OK = None
        for _ in range(3):
            SO._resident_read(str(src))
        assert probes == [1]


# --- (2) after a passing preflight, only a clean reply succeeds ------------

def test_every_bad_child_outcome_raises_and_never_reads_directly():
    ok = SO.READ_OK
    shapes = {
        "nonzero-no-marker": dict(rc=1, out=b"", err=b"boom"),
        "nonzero-with-marker": dict(rc=4, out=b"",
                                    err=(SO.REFUSED_MARK + "denied").encode()),
        "zero-empty-output": dict(rc=0, out=b"", err=b""),
        "zero-garbage-output": dict(rc=0, out=b"raw bytes", err=b""),
        "zero-bad-count": dict(rc=0, out=ok + b"99\nshort", err=b""),
        "zero-non-numeric-count": dict(rc=0, out=ok + b"x\ndata", err=b""),
        "launch-oserror": dict(exc=OSError("exec failed")),
        "launch-timeout": dict(exc=subprocess.TimeoutExpired("sudo", 1)),
    }
    with tempfile.TemporaryDirectory() as td, _State():
        src = _mkfile(Path(td), "s.txt", b"OWNER-MUST-NOT-READ")
        SO._RESIDENT_OK = True
        direct = []
        SO._read_staged_file = lambda p, *a: direct.append(p) or b"OWNER-READ"
        for name, shape in shapes.items():
            def run(cmd, _s=shape, **kw):
                if _s.get("exc"):
                    raise _s["exc"]
                return types.SimpleNamespace(stdout=_s["out"], stderr=_s["err"],
                                             returncode=_s["rc"])
            SO.subprocess = _fake_subprocess(run)
            try:
                SO._resident_read(str(src))
                assert False, f"{name}: a bad child outcome must raise"
            except SO.OutboxReadError:
                pass
        assert direct == [], "a passing preflight must never read directly"


def test_clean_framed_reply_returns_payload_from_an_isolated_child():
    with _State():
        SO._RESIDENT_OK = True
        captured = {}

        def run(cmd, **kw):
            captured["cmd"] = cmd
            return types.SimpleNamespace(stdout=SO.READ_OK + b"4\nDATA",
                                         stderr=b"", returncode=0)

        SO.subprocess = _fake_subprocess(run)
        assert SO._resident_read("/tmp/x") == b"DATA"
        cmd = captured["cmd"]
        assert cmd[:4] == ["sudo", "-n", "-u", SO.RESIDENT_USER]
        assert cmd[5] == "-I"
        assert cmd[-2:] == [SO.READ_AS_RESIDENT, "/tmp/x"]


def test_read_as_resident_main_frames_and_refuses():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        good = _mkfile(base, "g.txt", b"abc")
        link = base / "staged"
        try:
            os.symlink(good, link)
        except OSError:
            return
        out, err = io.BytesIO(), io.StringIO()
        real_o, real_e = sys.stdout, sys.stderr
        sys.stdout = types.SimpleNamespace(buffer=out)
        sys.stderr = err
        try:
            rc = SO._read_as_resident_main(str(good))
            rc2 = SO._read_as_resident_main(str(link))
        finally:
            sys.stdout, sys.stderr = real_o, real_e
        assert rc == 0 and out.getvalue() == SO.READ_OK + b"3\nabc"
        assert rc2 == 4 and err.getvalue().startswith(SO.REFUSED_MARK)


# --- (3) the limits a reply-named file is read under -----------------------

def test_read_staged_file_refuses_a_file_outside_the_limits():
    with tempfile.TemporaryDirectory() as td:
        f = _mkfile(Path(td), "old.pdf", b"x" * 100)
        os.utime(f, (1_000_000, 1_000_000))
        assert SO._read_staged_file(str(f), 999_000.0, 100) == b"x" * 100
        for newer_than, max_bytes in ((1_000_001.0, 100), (0.0, 99)):
            try:
                SO._read_staged_file(str(f), newer_than, max_bytes)
                assert False, (newer_than, max_bytes)
            except SO.OutboxReadError:
                pass


def test_limits_ride_to_the_child_only_when_set():
    assert SO._helper_argv("/p")[-2:] == [SO.READ_AS_RESIDENT, "/p"]
    assert SO._helper_argv("/p", 123.5, 1000)[-4:] == [
        SO.READ_AS_RESIDENT, "/p", "123.5", "1000"]
    with _State():
        SO._RESIDENT_OK = True
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            return types.SimpleNamespace(stdout=SO.READ_OK + b"1\nD",
                                         stderr=b"", returncode=0)

        SO.subprocess = _fake_subprocess(run)
        assert SO._resident_read("/p", newer_than=7.0, max_bytes=9) == b"D"
        assert seen[0][-3:] == ["/p", "7.0", "9"]


def test_no_fallback_refuses_when_the_child_is_unavailable():
    with tempfile.TemporaryDirectory() as td, _State():
        src = _mkfile(Path(td), "s.pdf", b"OWNER-MUST-NOT-READ")
        SO._probe_resident = lambda: False
        SO._RESIDENT_OK = None
        direct = []
        SO._read_staged_file = lambda p, *a: direct.append(p) or b"OWNER-READ"
        try:
            SO.materialize_for_owner(src, private_root=Path(td) / "p",
                                     fallback=False)
            assert False, "no resident read and no fallback must refuse"
        except SO.OutboxReadError:
            pass
        assert direct == []


def test_read_as_resident_main_applies_the_limits():
    with tempfile.TemporaryDirectory() as td:
        f = _mkfile(Path(td), "g.pdf", b"abc")
        out, err = io.BytesIO(), io.StringIO()
        real_o, real_e = sys.stdout, sys.stderr
        sys.stdout = types.SimpleNamespace(buffer=out)
        sys.stderr = err
        try:
            ok = SO._read_as_resident_main(str(f), "0", "3")
            big = SO._read_as_resident_main(str(f), "0", "2")
            old = SO._read_as_resident_main(str(f), repr(os.stat(f).st_mtime + 60), "3")
            bad = SO._read_as_resident_main(str(f), "soon", "3")
        finally:
            sys.stdout, sys.stderr = real_o, real_e
        assert ok == 0 and out.getvalue() == SO.READ_OK + b"3\nabc"
        assert big == old == bad == 4


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok {t.__name__}")
            passed += 1
        except Exception as e:
            traceback.print_exc()
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
