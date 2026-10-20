#!/usr/bin/env python3
"""The mediator transfers a resident-named file's bytes with the RESIDENT's
own access, closing audit B2-03.

Staging and O_NOFOLLOW already stop a symlink swap, but the copy-in read and
deliver-out write ran as the owner (crh), so a symlink-free path under an
allowed root that reaches a crh-owned file or directory (in /tmp, say) was
read into a draft or written by crh, which the resident itself could not do.
These pin the fix:

  * the byte transfer goes through `_run_as_resident`;
  * whether the drop is possible is decided once per process by a sudo
    list-mode preflight, and only a failed preflight falls back to the
    in-process owner read/write (audited RESIDENT-IO-FALLBACK);
  * after a passing preflight, every child outcome other than a clean framed
    success refuses that file, since the child runs with the resident's
    identity and must not be able to select the fallback;
  * the child runs as `python3 -I` with the EA directory on its argv.
"""
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import traceback
import types
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-medresio-")
# The mediator's draft locks live in the owner-private directory.
os.environ["EA_PRIVATE_DIR"] = tempfile.mkdtemp(prefix="ea-test-medresio-private-")

import mail_mediation as M  # noqa: E402


def _setup(td):
    tmp = Path(td)
    for d in ("config", "state", "uploads", "receipts", "docs", "research",
              "logs", "auth"):
        (tmp / d).mkdir(parents=True, exist_ok=True)
    M.EA_DIR = tmp
    M.QUEUE = tmp / "state" / "mail-queue"
    M.CONFIG = tmp / "config" / "ea-config.json"
    M.MEDIATOR_UNIT = tmp / "no-such-unit"
    os.environ.pop("EA_FORCE_MEDIATE", None)
    return tmp


def _mediator(tmp):
    import mail_mediator
    importlib.reload(mail_mediator)
    mail_mediator.AUDIT = tmp / "logs" / "mail-mediator.log"
    mail_mediator.STAGING_ROOT = tmp / "mediator-staging"
    mail_mediator._run_as_resident = None
    mail_mediator._RESIDENT_OK = None
    M.QUEUE.mkdir(parents=True, exist_ok=True)
    return mail_mediator


def _fake_subprocess(run):
    return types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)


def _run_request(mm, tool, verb, argv, fake_child):
    rid = uuid.uuid4().hex[:8]
    (M.QUEUE / f"req-{rid}.json").write_text(json.dumps(
        {"tool": tool, "verb": verb, "argv": argv}))
    real = mm.subprocess

    def run(cmd, **kw):
        out, rc = fake_child(cmd)
        return types.SimpleNamespace(stdout=out, stderr="", returncode=rc)

    mm.subprocess = _fake_subprocess(run)
    try:
        mm.handle(str(M.QUEUE / f"req-{rid}.json"))
    finally:
        mm.subprocess = real
    return json.loads((M.QUEUE / f"resp-{rid}.json").read_text())


def _flag_value(cmd, flag):
    for i, a in enumerate(cmd):
        if a == flag:
            return cmd[i + 1]
        if a.startswith(flag + "="):
            return a[len(flag) + 1:]
    raise AssertionError(f"{flag} not in {cmd}")


def _recording_runner(mm, calls):
    """A resident runner that records (mode, path) and performs the real
    confined I/O in-process, so a test proves the transfer went through the
    drop-to-resident step rather than the owner-access shortcut."""
    def runner(mode, path, data):
        calls.append((mode, path))
        return mm._inprocess_resident_io(mode, path, data)
    return runner


def _no_inprocess(mm, used):
    """Spy that fails the test if the in-process owner path is taken."""
    def spy(mode, path, data):
        used.append((mode, path))
        return b"OWNER-READ", 0, b""
    mm._inprocess_resident_io = spy


# --- the transfer goes through the resident step ---------------------------

def test_input_read_goes_through_the_resident_runner():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        calls = []
        mm._run_as_resident = _recording_runner(mm, calls)
        body = tmp / "state" / "reply-body.txt"
        body.write_text("Hi Kit,\n")
        seen = {}

        def child(cmd):
            seen["text"] = Path(_flag_value(cmd, "--body-file")).read_text()
            return json.dumps({"draft_id": "AAMk"}), 0

        resp = _run_request(mm, "graph_actions", "create-reply",
                            ["create-reply", "--message-id", "m", "--body", "x",
                             "--body-file", str(body)], child)
        assert resp["rc"] == 0, resp
        assert ("read", str(body)) in calls
        assert seen["text"] == "Hi Kit,\n"


def test_output_write_goes_through_the_resident_runner():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        calls = []
        mm._run_as_resident = _recording_runner(mm, calls)
        dest = str(tmp / "receipts" / "hotel.pdf")

        def child(cmd):
            Path(_flag_value(cmd, "--output")).write_bytes(b"%PDF-1.4 r")
            return "{}", 0

        resp = _run_request(mm, "graph_actions", "download-attachment",
                            ["download-attachment", "--message-id", "m",
                             "--attachment-id", "a", "--output", dest], child)
        assert resp["rc"] == 0, resp
        assert ("write", dest) in calls
        assert Path(dest).read_bytes() == b"%PDF-1.4 r"


# --- (1) availability comes from a preflight only --------------------------

def test_preflight_is_a_list_mode_check_of_the_isolated_helper():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        real_pwd, real_sub = mm.pwd, mm.subprocess
        mm.pwd = types.SimpleNamespace(getpwnam=lambda n: object())
        seen = []

        def outcome(rc=None, exc=None):
            def run(cmd, **kw):
                seen.append(cmd)
                if exc:
                    raise exc
                return types.SimpleNamespace(stdout=b"", stderr=b"", returncode=rc)
            return _fake_subprocess(run)

        try:
            mm.subprocess = outcome(rc=0)
            assert mm._probe_resident() is True
            mm.subprocess = outcome(rc=1)
            assert mm._probe_resident() is False
            mm.subprocess = outcome(exc=FileNotFoundError("sudo"))
            assert mm._probe_resident() is False
            mm.subprocess = outcome(exc=subprocess.TimeoutExpired("sudo", 1))
            assert mm._probe_resident() is False
        finally:
            mm.pwd, mm.subprocess = real_pwd, real_sub
        cmd = seen[0]
        # list mode (-l): the policy is asked, nothing runs as the resident
        assert cmd[:5] == ["sudo", "-n", "-l", "-u", M.RESIDENT_USER]
        assert cmd[6] == "-I" and mm.CONFINED_IO in cmd


def test_preflight_without_a_resident_user_is_unavailable():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        real_pwd, real_sub = mm.pwd, mm.subprocess

        def missing(name):
            raise KeyError(name)

        ran = []
        mm.pwd = types.SimpleNamespace(getpwnam=missing)
        mm.subprocess = _fake_subprocess(lambda *a, **k: ran.append(a))
        try:
            assert mm._probe_resident() is False
        finally:
            mm.pwd, mm.subprocess = real_pwd, real_sub
        assert ran == []


def test_failed_preflight_falls_back_in_process_and_audits():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        src = tmp / "state" / "body.txt"
        src.write_text("fallback-body\n")
        mm._probe_resident = lambda: False
        execs = []
        real_sub = mm.subprocess
        mm.subprocess = _fake_subprocess(lambda cmd, **k: execs.append(cmd))
        try:
            out, rc, err = mm._sudo_resident_io("read", str(src), b"")
        finally:
            mm.subprocess = real_sub
        assert rc == 0 and out == b"fallback-body\n", (rc, out, err)
        assert execs == []                               # no child was started
        assert "RESIDENT-IO-FALLBACK" in mm.AUDIT.read_text()


def test_preflight_runs_once_per_process():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        src = tmp / "state" / "body.txt"
        src.write_text("x")
        probes = []
        mm._probe_resident = lambda: probes.append(1) or False
        for _ in range(3):
            mm._sudo_resident_io("read", str(src), b"")
        assert probes == [1]


# --- (2) after a passing preflight, only a clean reply succeeds ------------

def _child_outcomes(mm):
    """Every bad reply shape a resident child could produce."""
    ok = mm.CONFINED_OK
    return {
        "nonzero-no-marker": dict(rc=1, out=b"", err=b"boom"),
        "nonzero-with-marker": dict(rc=4, out=b"",
                                    err=(mm.REFUSED_MARK + "denied").encode()),
        "zero-empty-output": dict(rc=0, out=b"", err=b""),
        "zero-garbage-output": dict(rc=0, out=b"raw bytes, no header", err=b""),
        "zero-bad-count": dict(rc=0, out=ok + b"99\nshort", err=b""),
        "zero-non-numeric-count": dict(rc=0, out=ok + b"x\ndata", err=b""),
        "zero-no-newline": dict(rc=0, out=ok + b"4", err=b""),
        "launch-oserror": dict(exc=OSError("exec failed")),
        "launch-timeout": dict(exc=subprocess.TimeoutExpired("sudo", 1)),
    }


def _drive(mm, mode, path, data, rc=0, out=b"", err=b"", exc=None):
    def run(cmd, **kw):
        if exc:
            raise exc
        return types.SimpleNamespace(stdout=out, stderr=err, returncode=rc)

    real_sub = mm.subprocess
    mm.subprocess = _fake_subprocess(run)
    try:
        return mm._sudo_resident_io(mode, path, data)
    finally:
        mm.subprocess = real_sub


def test_every_bad_child_read_outcome_refuses_and_never_falls_back():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        src = tmp / "state" / "body.txt"
        src.write_text("owner-must-not-read-this")
        mm._RESIDENT_OK = True
        used = []
        _no_inprocess(mm, used)
        for name, shape in _child_outcomes(mm).items():
            out, rc, err = _drive(mm, "read", str(src), b"", **shape)
            assert rc != 0, name
            assert out == b"", name
        assert used == [], "a passing preflight must never take the owner path"


def test_every_bad_child_write_outcome_refuses_and_never_falls_back():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        mm._RESIDENT_OK = True
        used = []
        _no_inprocess(mm, used)
        shapes = dict(_child_outcomes(mm))
        # a write reply that counts the wrong length, or carries a payload
        shapes["zero-short-count"] = dict(rc=0, out=mm._frame(2), err=b"")
        shapes["zero-trailing-payload"] = dict(rc=0, out=mm._frame(5, b"extra"),
                                               err=b"")
        for name, shape in shapes.items():
            out, rc, err = _drive(mm, "write", str(tmp / "state" / "o"),
                                  b"12345", **shape)
            assert rc != 0, name
        assert used == []


def test_clean_framed_replies_are_accepted():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        mm._RESIDENT_OK = True
        out, rc, _ = _drive(mm, "read", "/tmp/x", b"",
                            rc=0, out=mm._frame(4, b"DATA"))
        assert (out, rc) == (b"DATA", 0)
        out, rc, _ = _drive(mm, "read", "/tmp/x", b"",
                            rc=0, out=mm._frame(0))          # an empty file
        assert (out, rc) == (b"", 0)
        out, rc, _ = _drive(mm, "write", "/tmp/x", b"12345",
                            rc=0, out=mm._frame(5))
        assert rc == 0


def test_handle_fails_closed_when_the_child_misbehaves():
    """End to end: a passing preflight and a child that exits non-zero with no
    marker must refuse the draft, not re-read the body file as the owner."""
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        mm._run_as_resident = mm._sudo_resident_io
        mm._RESIDENT_OK = True
        used = []
        _no_inprocess(mm, used)
        body = tmp / "state" / "reply-body.txt"
        body.write_text("owner-only\n")
        tool_ran = []
        rid = uuid.uuid4().hex[:8]
        (M.QUEUE / f"req-{rid}.json").write_text(json.dumps(
            {"tool": "graph_actions", "verb": "create-reply",
             "argv": ["create-reply", "--message-id", "m", "--body", "x",
                      "--body-file", str(body)]}))

        def run(cmd, **kw):
            if cmd[0] == "sudo":
                return types.SimpleNamespace(stdout=b"", stderr=b"crash",
                                             returncode=1)
            tool_ran.append(cmd)
            return types.SimpleNamespace(stdout="{}", stderr="", returncode=0)

        real_sub = mm.subprocess
        mm.subprocess = _fake_subprocess(run)
        try:
            mm.handle(str(M.QUEUE / f"req-{rid}.json"))
        finally:
            mm.subprocess = real_sub
        resp = json.loads((M.QUEUE / f"resp-{rid}.json").read_text())
        assert resp["rc"] == 2 and "refused" in resp["stderr"], resp
        assert tool_ran == [] and used == []


# --- (3) the child is isolated and told its EA directory -------------------

def test_exec_command_is_isolated_and_passes_ea_dir_on_argv():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        mm._RESIDENT_OK = True
        captured = {}

        def run(cmd, **kw):
            captured["cmd"] = cmd
            return types.SimpleNamespace(stdout=mm._frame(4, b"DATA"),
                                         stderr=b"", returncode=0)

        real_sub = mm.subprocess
        mm.subprocess = _fake_subprocess(run)
        try:
            out, rc, err = mm._sudo_resident_io("read", "/tmp/x", b"")
        finally:
            mm.subprocess = real_sub
        cmd = captured["cmd"]
        assert cmd[:4] == ["sudo", "-n", "-u", M.RESIDENT_USER]
        assert cmd[5] == "-I"                       # no user site, no PYTHON*
        assert "env" not in cmd and not any(a.startswith("EA_DIR=") for a in cmd)
        assert cmd[-4:] == [mm.CONFINED_IO, "read", str(M.EA_DIR), "/tmp/x"]
        assert (out, rc) == (b"DATA", 0)


def test_confined_io_entrypoint_frames_and_refuses():
    with tempfile.TemporaryDirectory() as td:
        tmp = _setup(td)
        mm = _mediator(tmp)
        src = tmp / "state" / "b.txt"
        src.write_text("payload")
        out, err = io.BytesIO(), io.StringIO()
        real_o, real_e = sys.stdout, sys.stderr
        sys.stdout = types.SimpleNamespace(buffer=out)
        sys.stderr = err
        try:
            rc = mm._confined_io("read", str(src))
            rc2 = mm._confined_io("read", "/etc/shadow")
        finally:
            sys.stdout, sys.stderr = real_o, real_e
        assert rc == 0 and out.getvalue() == mm._frame(7, b"payload")
        assert rc2 == 4 and mm.REFUSED_MARK in err.getvalue()


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
