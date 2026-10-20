"""user_tz: the user's zone comes from the profile, then the scheduling
preferences, then the system, and a module with no config of its own
uses it in place of a hard-coded zone."""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preprocessor"))
import user_tz  # noqa: E402


def _cfg(tmp_path, **files):
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    for name, data in files.items():
        (cfg / name.replace("_", "-")).write_text(json.dumps(data))
    return tmp_path


def test_profile_wins(tmp_path, monkeypatch):
    ea = _cfg(tmp_path, **{"user_profile.json": {"timezone": "Europe/Lisbon"},
                           "scheduling_preferences.json": {"timezone": "Asia/Tokyo"}})
    monkeypatch.setenv("EA_DIR", str(ea))
    assert user_tz.name() == "Europe/Lisbon"
    assert str(user_tz.zone()) == "Europe/Lisbon"


def test_scheduling_preferences_when_the_profile_names_none(tmp_path, monkeypatch):
    ea = _cfg(tmp_path, **{"user_profile.json": {"name": "Avery"},
                           "scheduling_preferences.json": {"timezone": "Asia/Tokyo"}})
    assert user_tz.name(ea) == "Asia/Tokyo"


def test_system_zone_when_config_names_none(tmp_path, monkeypatch):
    ea = _cfg(tmp_path, **{"user_profile.json": {}, "scheduling_preferences.json": {"timezone": ""}})
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    assert user_tz.name(ea) == "Pacific/Auckland"


def test_bad_names_fall_through(tmp_path, monkeypatch):
    ea = _cfg(tmp_path, **{"user_profile.json": {"timezone": "Mars/Olympus"}})
    monkeypatch.setenv("TZ", "Not/AZone")
    monkeypatch.setattr(user_tz, "system_name", lambda: "")
    assert user_tz.name(ea) == "UTC"


def test_system_name_reads_tz_then_localtime(monkeypatch):
    monkeypatch.setenv("TZ", ":America/Chicago")
    assert user_tz.system_name() == "America/Chicago"
    monkeypatch.delenv("TZ")
    monkeypatch.setattr(user_tz.os.path, "realpath",
                        lambda p: "/usr/share/zoneinfo/Europe/Oslo" if p == "/etc/localtime" else p)
    assert user_tz.system_name() == "Europe/Oslo"


@pytest.mark.parametrize("name", [
    "ea-chat-bot.py", "preprocessor/alert.py", "preprocessor/booking_link.py",
    "preprocessor/calendar_reconciler.py", "preprocessor/google_calendar_client.py",
    "preprocessor/graph_actions.py", "preprocessor/graph_client.py",
    "preprocessor/poll_filler.py", "preprocessor/quiet_hours.py",
    "preprocessor/sched_agent.py", "preprocessor/sched_pipeline.py",
    "preprocessor/scheduling_engine.py"])
def test_no_hard_coded_home_zone(name):
    text = (REPO / name).read_text()
    assert '"America/Denver"' not in text and "'America/Denver'" not in text


def test_booking_link_and_google_calendar_follow_the_profile(tmp_path, monkeypatch):
    ea = _cfg(tmp_path, **{"user_profile.json": {"timezone": "Europe/Lisbon"}})
    monkeypatch.setenv("EA_DIR", str(ea))
    import booking_link
    monkeypatch.setattr(booking_link, "EA_DIR", ea)
    assert str(booking_link._user_tz()) == "Europe/Lisbon"
    import google_calendar_client as g
    assert g._zone_name("") == "Europe/Lisbon"
    assert g._zone_name("Asia/Tokyo") == "Asia/Tokyo"
    body = g._event_patch(start="2027-02-11T11:00:00")
    assert body["start"]["timeZone"] == "Europe/Lisbon"
