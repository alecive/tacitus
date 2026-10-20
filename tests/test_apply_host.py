"""deploy/apply-host.sh: one root command that runs only the root scripts the
boundary audit names for a failing check, in a fixed order, and audits again."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "apply-host.sh"


def _order():
    return re.search(r'^ORDER="([^"]+)"', SCRIPT.read_text(), re.M).group(1).split()


def _findings(results):
    src = SCRIPT.read_text()
    py = src[src.index("python3 -c '") + len("python3 -c '"):]
    py = py[:py.index("\n' \"$ORDER\"")]
    r = subprocess.run(["python3", "-c", py, " ".join(_order())], input=json.dumps({"results": results}),
                       capture_output=True, text=True)
    return [ln.split("\t") for ln in r.stdout.splitlines()]


def test_every_root_script_it_runs_exists_and_is_explained():
    src = SCRIPT.read_text()
    for name in _order():
        assert (ROOT / "deploy" / f"{name}.sh").is_file(), name
        assert re.search(rf"^\s+{re.escape(name)}\)\s+echo \"", src, re.M), name


def test_a_check_maps_to_its_root_script_and_anything_else_is_manual():
    got = _findings([
        {"status": "PASS", "name": "fine", "fix": "sudo bash deploy/harden-permissions.sh"},
        {"status": "WARN", "name": "auth: pinned", "fix": "sudo bash /opt/ea-system/deploy/pin-auth-dir.sh"},
        {"status": "FAIL", "name": "ssh", "fix": "sudo bash deploy/ssh-deny-resident.sh"},
        {"status": "WARN", "name": "other", "fix": "sudo bash deploy/rename-chat-bot.sh"},
        {"status": "FAIL", "name": "by hand", "fix": "edit the file"},
    ])
    assert [(s, n) for s, n, *_ in got] == [("WARN", "pin-auth-dir"), ("FAIL", "ssh-deny-resident"),
                                            ("WARN", "-"), ("FAIL", "-")]


def test_every_root_script_the_audit_names_is_in_the_order_or_known_manual():
    audit = (ROOT / "scripts" / "boundary-audit.py").read_text()
    # tree-custody.sh is the library the setup scripts source, not a fix
    named = set(re.findall(r"deploy/([a-z-]+)\.sh", audit)) - {"tree-custody"}
    assert named <= set(_order()), named - set(_order())


def test_it_refuses_without_root_and_from_the_residents_tree():
    r = subprocess.run(["bash", str(SCRIPT), "--plan"], capture_output=True, text=True)
    assert r.returncode != 0 and "run as root" in r.stdout
    src = SCRIPT.read_text()
    assert "/home/ea-bot/*) red" in src
