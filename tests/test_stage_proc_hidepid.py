"""deploy/stage-proc-hidepid.sh: the /proc line it stages for the next boot.

The script is a root step and never runs here. Its fstab edit is a set of
functions that take a file and write a file, so these tests source the
script (its main runs only when it is executed) and drive them on scratch
copies of an fstab:

  * staging adds the tag and the hidepid line after a timestamped backup,
    keeps every other line and the file's mode, and libmount reads the line
    back with the options the boot applies;
  * a second run writes nothing and leaves no second backup; a new gid
    replaces the line where it stands;
  * an active /proc line the script did not write is commented out, and
    rollback puts it back byte for byte;
  * the audit reads the staged file as staged;
  * the script mounts nothing, and refuses to stage without root.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# /proc with hidepid is a Linux mount option; there is nothing to test on macOS.
pytestmark = pytest.mark.skipif(sys.platform == "darwin", reason="Linux /proc only")

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "stage-proc-hidepid.sh"

_spec = importlib.util.spec_from_file_location("boundary_audit", ROOT / "scripts" / "boundary-audit.py")
audit = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("boundary_audit", audit)
_spec.loader.exec_module(audit)

BASE = """\
# /etc/fstab: static file system information.
/dev/disk/by-uuid/5c980b47 / ext4 defaults 0 1
/swap.img\tnone\tswap\tsw\t0\t0
LABEL=ea-backup  /mnt/backup  ext4  defaults,noatime,nofail  0  2
"""
OPTS = "nosuid,nodev,noexec,relatime"


def _call(func: str, *args: str) -> subprocess.CompletedProcess:
    """One function of the script, sourced into a fresh bash."""
    return subprocess.run(["bash", "-c", 'script="$1"; shift; source "$script" && "$@"', "bash",
                           str(SCRIPT), func, *args],
                          capture_output=True, text=True, timeout=30,
                          env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"})


def _stage(fstab: Path, gid: int = 991, group: str = "procview") -> str:
    r = _call("stage_fstab", str(fstab), str(gid), group)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _unstage(fstab: Path) -> str:
    r = _call("unstage_fstab", str(fstab))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _backups(fstab: Path) -> list[Path]:
    return sorted(fstab.parent.glob(fstab.name + ".bak-proc-hidepid-*"))


def _active_proc(text: str) -> list[str]:
    return [ln for ln in text.splitlines()
            if len(ln.split()) >= 2 and not ln.lstrip().startswith("#") and ln.split()[1] == "/proc"]


@pytest.fixture
def fstab(tmp_path) -> Path:
    p = tmp_path / "etc" / "fstab"
    p.parent.mkdir()
    p.write_text(BASE)
    p.chmod(0o644)
    return p


def test_staging_adds_the_line_after_a_timestamped_backup(fstab):
    backup = _stage(fstab)
    assert re.fullmatch(re.escape(str(fstab)) + r"\.bak-proc-hidepid-\d{8}T\d{6}Z", backup)
    assert Path(backup).read_text() == BASE
    text = fstab.read_text()
    assert text.startswith(BASE), "every other line stays as it was"
    tag, line = text[len(BASE):].splitlines()
    assert tag.startswith("# ea-proc-hidepid: ") and "gid 991 (procview)" in tag
    assert line.split() == ["proc", "/proc", "proc", f"{OPTS},hidepid=2,gid=991", "0", "0"]
    assert oct(fstab.stat().st_mode & 0o777) == "0o644"
    assert not list(fstab.parent.glob(".fstab.proc-hidepid.*")), "no scratch file left behind"


@pytest.mark.skipif(not shutil.which("findmnt"), reason="no findmnt (util-linux)")
def test_libmount_reads_the_staged_options(fstab):
    _stage(fstab)
    r = subprocess.run(["findmnt", "--tab-file", str(fstab), "-M", "/proc", "-no", "OPTIONS"],
                       capture_output=True, text=True, timeout=15)
    assert r.stdout.strip() == f"{OPTS},hidepid=2,gid=991"


def test_a_second_run_writes_nothing(fstab):
    _stage(fstab)
    staged = fstab.read_text()
    assert _stage(fstab) == "", "unchanged: no backup path printed"
    assert fstab.read_text() == staged
    assert len(_backups(fstab)) == 1


def test_a_new_gid_replaces_the_line_where_it_stands(fstab):
    _stage(fstab)
    later = "LABEL=experiments  /mnt/experiments  ext4  defaults,nofail  0  2\n"
    fstab.write_text(fstab.read_text() + later)
    assert _stage(fstab, gid=992) != ""
    text = fstab.read_text()
    assert _active_proc(text) == [f"proc  /proc  proc  {OPTS},hidepid=2,gid=992  0  0"]
    assert text.count("# ea-proc-hidepid:") == 1 and "gid 992 (procview)" in text
    assert text.endswith(later), "the block keeps its place above the later line"


def test_a_foreign_proc_line_is_disabled_and_rollback_restores_it(fstab):
    foreign = "proc /proc proc defaults,hidepid=1 0 0"
    original = BASE.replace("/swap.img", foreign + "\n/swap.img", 1)
    fstab.write_text(original)
    _stage(fstab)
    text = fstab.read_text()
    assert f"# ea-proc-hidepid-disabled: {foreign}" in text.splitlines()
    assert _active_proc(text) == [f"proc  /proc  proc  {OPTS},hidepid=2,gid=991  0  0"]
    assert _unstage(fstab) != ""
    assert fstab.read_text() == original
    assert len(_backups(fstab)) >= 1


def test_rollback_of_an_unstaged_fstab_writes_nothing(fstab):
    assert _unstage(fstab) == ""
    assert fstab.read_text() == BASE and _backups(fstab) == []


def test_rollback_after_staging_returns_the_original(fstab):
    _stage(fstab)
    assert _unstage(fstab) != ""
    assert fstab.read_text() == BASE
    assert len(_backups(fstab)) >= 1


def test_the_audit_reads_the_staged_file_as_staged(fstab, tmp_path):
    _stage(fstab)
    opts = audit.fstab_proc_options(fstab.read_text())
    assert audit._hidepid(opts) == "invisible" and audit._proc_gid(opts) == 991
    _unstage(fstab)
    assert audit.fstab_proc_options(fstab.read_text()) is None


def test_the_staged_line_is_found_after_its_tag(fstab):
    _stage(fstab)
    r = _call("staged_line", str(fstab))
    assert r.stdout.split() == ["proc", "/proc", "proc", f"{OPTS},hidepid=2,gid=991", "0", "0"]


def test_the_script_mounts_nothing():
    """Nothing but a printed hint names mount: no line runs it."""
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    run = []
    for raw in SCRIPT.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"(echo|printf|row|red|grn|ylw|fail)\b", line):
            continue
        if re.search(r"(^|[\s;&|(`$])(u?mount|systemctl\s+(daemon-reload|restart|reload))\b", line):
            run.append(line)
    assert run == []


@pytest.mark.skipif(os.geteuid() == 0, reason="the root check cannot refuse root")
def test_staging_needs_root():
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "run as root" in r.stdout
