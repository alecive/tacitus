"""mail_mediation.ea_managed_calendar_id: the un-gated calendar is a
per-install, owner-private setting, never the resident's config in an owner
process. owner_trust.py seed --calendar writes it once.

    python3 -m pytest tests/test_ea_managed_calendar_id.py -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-eamanaged-")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import mail_mediation as M  # noqa: E402
import owner_trust  # noqa: E402

CAL = "kai.pemberton65@example.com"


def _config(ea_dir: Path, cal: str) -> None:
    (ea_dir / "config").mkdir(parents=True, exist_ok=True)
    (ea_dir / "config" / "scheduling-preferences.json").write_text(json.dumps(
        {"ea_managed_calendar": {"email": "zev.jamison61@example.com", "calendar_id": cal}}))


@pytest.fixture
def resident(monkeypatch, tmp_path):
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: False)
    monkeypatch.setattr(M, "EA_DIR", tmp_path)
    monkeypatch.delenv("EA_MANAGED_CALENDAR_ID", raising=False)
    return tmp_path


@pytest.fixture
def owner(monkeypatch, tmp_path):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: True)
    monkeypatch.setattr(owner_trust, "private_dir", lambda: priv)
    monkeypatch.setattr(M, "EA_DIR", tmp_path)
    monkeypatch.setenv("EA_MANAGED_CALENDAR_ID", "xan.bellweather36@example.com")
    _config(tmp_path, "rio.sorensen62@example.com")
    return priv


def test_resident_takes_its_config_then_the_environment(resident, monkeypatch):
    assert M.ea_managed_calendar_id() == ""
    monkeypatch.setenv("EA_MANAGED_CALENDAR_ID", "blake.yarborough70@example.com")
    assert M.ea_managed_calendar_id() == "blake.yarborough70@example.com"
    _config(resident, CAL)
    assert M.ea_managed_calendar_id() == CAL


def test_owner_reads_only_the_private_file(owner):
    assert M.ea_managed_calendar_id() == ""
    (owner / "calendars.json").write_text(json.dumps({"ea_managed_calendar_id": CAL}))
    os.chmod(owner / "calendars.json", 0o600)
    assert M.ea_managed_calendar_id() == CAL


def test_owner_refuses_a_file_others_can_write(owner):
    (owner / "calendars.json").write_text(json.dumps({"ea_managed_calendar_id": CAL}))
    os.chmod(owner / "calendars.json", 0o666)
    assert M.ea_managed_calendar_id() == ""


def test_no_calendar_means_no_ungated_write(owner):
    ce = ["create-event", "--email", "zev.jamison61@example.com", "--summary", "Focus",
          "--start", "2026-07-20T09:00:00", "--end", "2026-07-20T10:00:00", "--calendar-id", CAL]
    assert not M.is_ea_managed_autohold("google_calendar_client", "create-event", ce)
    assert not M.is_ea_managed_autohold("google_calendar_client", "delete-event",
                                        ["delete-event", "--event-id", "E1"])
    (owner / "calendars.json").write_text(json.dumps({"ea_managed_calendar_id": CAL}))
    os.chmod(owner / "calendars.json", 0o600)
    assert M.is_ea_managed_autohold("google_calendar_client", "create-event", ce)
    assert M.is_ea_managed_autohold("google_calendar_client", "delete-event",
                                    ["delete-event", "--event-id", "E1"])
    other = ce[:-1] + ["rio.sorensen62@example.com"]
    assert not M.is_ea_managed_autohold("google_calendar_client", "create-event", other)


def test_seed_calendar_copies_the_id_once(owner, tmp_path, capsys, monkeypatch):
    _config(tmp_path, CAL)
    monkeypatch.setattr(owner_trust, "ea_dir", lambda: tmp_path)
    # the id comes from ea-bot space: the operator types it back
    monkeypatch.setattr(owner_trust, "_interactive", lambda: True)
    monkeypatch.setattr(owner_trust, "_prompt",
                        lambda q: CAL if "ea_managed_calendar_id" in q else "zev.jamison61@example.com")
    assert owner_trust.main(["seed", "--calendar"]) == 0
    out = capsys.readouterr().out
    assert "created" in out and CAL in out
    doc = json.loads((owner / "calendars.json").read_text())
    assert doc == {"ea_managed_calendar_id": CAL,
                   "ea_managed_account": "zev.jamison61@example.com"}
    assert (owner / "calendars.json").stat().st_mode & 0o777 == 0o600
    # A second run keeps the file; the config changing later changes nothing.
    _config(tmp_path, "bo.underhill39@example.com")
    assert owner_trust.main(["seed", "--calendar"]) == 0
    assert "kept" in capsys.readouterr().out
    assert M.ea_managed_calendar_id() == CAL


def test_seed_calendar_without_a_config_id_fails_clearly(owner, tmp_path, capsys, monkeypatch):
    _config(tmp_path, "")
    monkeypatch.setattr(owner_trust, "ea_dir", lambda: tmp_path)
    assert owner_trust.main(["seed", "--calendar"]) == 1
    assert "The EA-Managed calendar" in capsys.readouterr().err
    assert not (owner / "calendars.json").exists()


# ── the owner side fails closed ───────────────────────────────────────

@pytest.fixture
def misclassified(monkeypatch, tmp_path):
    """An owner process owner_trust does not recognise (the private
    directory cannot be looked at, a single-home layout): not the resident,
    on the install tree."""
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    tree = tmp_path / "tree"
    tree.mkdir()
    monkeypatch.setattr(owner_trust, "is_owner_process", lambda: False)
    monkeypatch.setattr(owner_trust, "private_dir", lambda: priv)
    monkeypatch.setattr(owner_trust, "ea_dir", lambda: tree)
    monkeypatch.setattr(owner_trust, "install_ea_dir", lambda: tree)
    monkeypatch.setattr(M, "_is_resident", lambda: False)
    monkeypatch.setattr(M, "EA_DIR", tmp_path)
    monkeypatch.delenv(M.OWNER_SIDE_ENV, raising=False)
    monkeypatch.setenv("EA_MANAGED_CALENDAR_ID", "xan.bellweather36@example.com")
    _config(tmp_path, "rio.sorensen62@example.com")
    return priv


def _seed(priv: Path, doc: dict) -> None:
    (priv / "calendars.json").write_text(json.dumps(doc))
    os.chmod(priv / "calendars.json", 0o600)


def test_a_misclassified_owner_never_takes_the_residents_id(misclassified):
    import google_calendar_client as g
    assert M.owner_side()
    assert M.ea_managed_calendar_id() == ""
    assert g._ea_managed_calendar_id() == ""
    assert not M.is_ea_managed_autohold("google_calendar_client", "delete-event",
                                        ["delete-event", "--event-id", "E1"])
    _seed(misclassified, {"ea_managed_calendar_id": CAL})
    assert M.ea_managed_calendar_id() == CAL == g._ea_managed_calendar_id()


def test_only_the_resident_or_a_scratch_tree_takes_the_hint(misclassified, monkeypatch,
                                                           tmp_path):
    monkeypatch.setattr(M, "_is_resident", lambda: True)
    assert not M.owner_side()
    assert M.ea_managed_calendar_id() == "rio.sorensen62@example.com"
    # the marker the mediator puts in its child's environment wins
    monkeypatch.setenv(M.OWNER_SIDE_ENV, "1")
    assert M.owner_side() and M.ea_managed_calendar_id() == ""
    monkeypatch.delenv(M.OWNER_SIDE_ENV)
    monkeypatch.setattr(M, "_is_resident", lambda: False)
    monkeypatch.setattr(owner_trust, "ea_dir", lambda: tmp_path / "scratch")
    assert not M.owner_side()

    # an answer that cannot be had counts as the owner side
    def boom():
        raise OSError("no passwd entry")
    monkeypatch.setattr(owner_trust, "install_ea_dir", boom)
    assert M.owner_side()


def test_the_mediator_check_reads_the_owner_file_whatever_the_process_looks_like(
        resident, monkeypatch):
    _config(resident, CAL)
    argv = ["delete-event", "--event-id", "E1", "--calendar-id", CAL]
    assert M.is_ea_managed_autohold("google_calendar_client", "delete-event", argv)
    priv = resident / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setattr(owner_trust, "private_dir", lambda: priv)
    assert not M.is_ea_managed_autohold("google_calendar_client", "delete-event", argv,
                                        owner=True)
    _seed(priv, {"ea_managed_calendar_id": CAL})
    assert M.is_ea_managed_autohold("google_calendar_client", "delete-event", argv, owner=True)


def test_the_owner_target_is_the_private_calendar_and_account(misclassified, monkeypatch):
    import google_calendar_client as g
    monkeypatch.setattr(g, "_ea_managed_source", lambda: {
        "email": "zev.jamison61@example.com", "calendar_id": "rio.sorensen62@example.com"})
    assert g.ea_managed_target() is None          # no private file: no target
    _seed(misclassified, {"ea_managed_calendar_id": CAL})
    # a file from before the account key: the account only from the config
    assert g.ea_managed_target() == {"email": "zev.jamison61@example.com", "calendar_id": CAL}
    _seed(misclassified, {"ea_managed_calendar_id": CAL,
                          "ea_managed_account": "bo.underhill39@example.com"})
    assert g.ea_managed_target() == {"email": "bo.underhill39@example.com", "calendar_id": CAL}


def test_seed_adds_the_account_to_an_older_file(owner, tmp_path, monkeypatch):
    _seed(owner, {"ea_managed_calendar_id": CAL})
    _config(tmp_path, "bo.underhill39@example.com")
    monkeypatch.setattr(owner_trust, "ea_dir", lambda: tmp_path)
    # The account comes from ea-bot space too, so it is not added unconfirmed.
    monkeypatch.setattr(owner_trust, "_interactive", lambda: False)
    assert owner_trust.main(["seed", "--calendar"]) == 0
    assert json.loads((owner / "calendars.json").read_text()) == {"ea_managed_calendar_id": CAL}
    assert owner_trust.main(["seed", "--calendar",
                             "--calendar-account", "zev.jamison61@example.com"]) == 0
    doc = json.loads((owner / "calendars.json").read_text())
    # the id is kept; only the account is added
    assert doc == {"ea_managed_calendar_id": CAL,
                   "ea_managed_account": "zev.jamison61@example.com"}
