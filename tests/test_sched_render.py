"""sched_render: every clock time in a scheduling reply is written by code."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import sched_render as sr  # noqa: E402

MT = ZoneInfo("America/Denver")


def _dt(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=MT)


def test_label_is_the_users_clock_with_weekday_and_date():
    assert sr.option_label(_dt(2026, 11, 3, 10, 30), _dt(2026, 11, 3, 11, 0)) == "Tue Nov 3, 10:30-11:00 AM MT"
    # AM/PM named on both halves when they differ
    assert sr.option_label(_dt(2026, 11, 4, 11, 30), _dt(2026, 11, 4, 12, 0)) == "Wed Nov 4, 11:30 AM-12:00 PM MT"


def test_clocks_use_the_real_offset_on_each_date():
    att = [{"email": "sol.ashcombe@example.org", "timezone": "America/New_York"},
           {"email": "ira.yarborough39@example.com", "timezone": "Europe/London"},
           {"email": "taylor.corvino56@example.com", "timezone": "Asia/Tokyo"},
           {"email": "uma.kestrel8@example.org"}]
    # Nov 3: both US zones on standard time, London on GMT (+7h), Tokyo next day
    c = sr.option_clocks(_dt(2026, 11, 3, 10, 30), _dt(2026, 11, 3, 11, 0), att)
    assert c["sol.ashcombe@example.org"] == "12:30-1:00 PM ET"
    assert c["ira.yarborough39@example.com"] == "5:30-6:00 PM in London"
    assert c["taylor.corvino56@example.com"] == "2:30-3:00 AM Wed in Tokyo"
    assert "uma.kestrel8@example.org" not in c
    # Oct 28: US on daylight time, UK already on GMT: London is +6h
    c = sr.option_clocks(_dt(2026, 10, 28, 9, 0), _dt(2026, 10, 28, 9, 30), att)
    assert c["ira.yarborough39@example.com"] == "3:00-3:30 PM in London"
    assert c["sol.ashcombe@example.org"] == "11:00-11:30 AM ET"


def test_label_options_gives_ids_lines_and_skips_malformed():
    opts = [{"start": "2026-11-02T14:30:00-07:00", "end": "2026-11-02T15:00:00-07:00"},
            {"start": "garbage", "end": "x"},
            {"start": "2026-11-05T09:00:00-07:00", "end": "2026-11-05T09:30:00-07:00"}]
    sr.label_options(opts, [{"email": "sol.ashcombe@example.org", "timezone": "America/New_York"}])
    assert opts[0]["id"] == "o1" and opts[2]["id"] == "o3" and "id" not in opts[1]
    assert opts[0]["line"] == "Mon Nov 2, 2:30-3:00 PM MT (4:30-5:00 PM ET)"
    assert opts[2]["line"] == "Thu Nov 5, 9:00-9:30 AM MT (11:00-11:30 AM ET)"
    sr.label_options(opts[:1], [], prefix="c")
    assert opts[0]["id"] == "c1" and opts[0]["line"] == "Mon Nov 2, 2:30-3:00 PM MT"


def test_names_a_clock_and_clean_prose():
    assert sr.names_a_clock("How about 10:30 AM?")
    assert sr.names_a_clock("say 2 pm")
    assert not sr.names_a_clock("Friday does not work for me this time.")
    assert sr.clean_prose("Friday is out — the workshop runs all morning.  ") == "Friday is out, the workshop runs all morning."


def test_render_body_only_the_lines_carry_times():
    body = sr.render_body(first_name="Sol", opening="Friday is out for me. Here are three times that work:",
                          lines=["Mon Nov 2, 2:30-3:00 PM MT (4:30-5:00 PM ET)", "Tue Nov 3, 10:30-11:00 AM MT (12:30-1:00 PM ET)"],
                          closing="Pick one and I will send a Zoom invite.", signature="Kit")
    assert body.startswith("Hi Sol,\n\nFriday is out for me.")
    assert "\n\nMon Nov 2, 2:30-3:00 PM MT (4:30-5:00 PM ET)\nTue Nov 3, 10:30-11:00 AM MT (12:30-1:00 PM ET)\n\n" in body
    assert body.rstrip().endswith("Pick one and I will send a Zoom invite.\n\nKit")
    assert "—" not in body
    assert sr.render_body(first_name="", opening="", lines=[], closing="Thanks.").startswith("Hi,\n\nThanks.")
