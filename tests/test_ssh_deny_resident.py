"""deploy/ssh-deny-resident.sh refuses SSH logins for the resident.

The resident controls its own authorized_keys, so sshd must deny it. The
script writes one DenyUsers drop-in, validates with sshd -t before any
reload, removes the drop-in when validation fails, and reloads (never
restarts) so open sessions stay.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "ssh-deny-resident.sh"
SRC = SCRIPT.read_text()


def test_the_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_the_dropin_denies_the_resident_and_loads_first():
    assert 'DROPIN="$DROPIN_DIR/01-ea-deny-resident.conf"' in SRC
    assert "DenyUsers $RESIDENT" in SRC
    assert 'RESIDENT="${EA_RESIDENT_USER:-ea-bot}"' in SRC


def test_sshd_validates_before_any_reload():
    stage = SRC[SRC.index("stage(){"):SRC.index("status(){")]
    assert stage.index("sshd -t") < stage.index("reload_sshd")
    assert 'rm -f "$DROPIN"' in stage[stage.index("sshd -t"):stage.index("reload_sshd")]


def test_reload_never_restart():
    assert "systemctl restart" not in SRC
    assert 'systemctl reload "$u"' in SRC


def test_it_refuses_a_config_that_ignores_the_dropins():
    assert "includes_dropins || fail" in SRC
