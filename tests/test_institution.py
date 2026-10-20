"""institution: the profile's institution block and the fallbacks derived
from the user's own address."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import institution  # noqa: E402


@pytest.fixture
def profile(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("EA_DIR", str(tmp_path))

    def write(data):
        (tmp_path / "config" / "user-profile.json").write_text(json.dumps(data))
    return write


def test_without_a_block_the_users_own_domain_is_internal(profile):
    profile({"email": "Ana.Lopez@Example.EDU", "university": "Example University"})
    assert institution.domains() == ("example.edu",)
    assert institution.related_domains() == ()
    assert institution.is_internal("bo@example.edu") and institution.is_internal("bo@cs.example.edu")
    assert not institution.is_internal("someone@example.edu.evil.com") and not institution.is_internal("someone@gmail.com")
    assert not institution.is_internal("someone@notexample.edu")
    assert institution.own_suffix() == "@example.edu"
    assert institution.safety_rail_domain() == "example.edu"
    assert institution.safety_rail_phrase() == "a first email outside example.edu"
    assert institution.name() == "Example University"
    assert institution.boilerplate_tokens() == ("example",)
    assert institution.boilerplate_phrases() == ("www.example.edu",)


def test_the_block_overrides_every_fallback(profile):
    profile({"email": "me@example.edu", "zoom_link": "https://zoom.example/me",
             "institution": {"name": "Example U", "domains": ["example.edu", "@sys.example.org"],
                             "related_domains": ["med.example.net", "example.edu"],
                             "safety_rail_domain": "example.edu",
                             "boilerplate_tokens": ["exu"], "boilerplate_phrases": ["Example U news"],
                             "letterhead": {"template_dir": "/srv/lh", "required_assets": ["lh.sty"]}}})
    assert institution.domains() == ("example.edu", "sys.example.org")
    assert institution.related_domains() == ("med.example.net",)
    assert institution.is_internal("x@sys.example.org") and not institution.is_internal("x@med.example.net")
    assert institution.is_internal_or_related("x@med.example.net")
    assert institution.video() == {"platform": "Zoom", "link": "https://zoom.example/me"}
    assert institution.video_platform() == "Zoom" and institution.video_link() == "https://zoom.example/me"
    assert institution.video_meeting_id() == "" and institution.video_join_block() == "Join Zoom meeting\nhttps://zoom.example/me"
    assert institution.boilerplate_tokens()[0] == "exu" and "example" in institution.boilerplate_tokens()
    assert "example u news" in institution.boilerplate_phrases()
    lh = institution.letterhead()
    assert str(lh["template_dir"]) == "/srv/lh" and lh["required_assets"] == ["lh.sty"]
    assert lh["asset_dirs"] == []


def test_a_letterhead_asset_comes_from_the_template_dir_then_the_asset_dirs(profile, tmp_path):
    own, plugin = tmp_path / "own", tmp_path / "plugin-letterhead"
    own.mkdir()
    plugin.mkdir()
    (own / "logo.pdf").write_text("own")
    (plugin / "logo.pdf").write_text("plugin")
    (plugin / "style.sty").write_text("plugin")
    profile({"email": "me@example.edu",
             "institution": {"letterhead": {"template_dir": str(own), "asset_dirs": str(plugin),
                                            "required_assets": ["style.sty", "logo.pdf"]}}})
    lh = institution.letterhead()
    assert lh["asset_dirs"] == [plugin]
    import compile_letter
    find = compile_letter.find_asset
    assert find("logo.pdf", lh["template_dir"], lh["asset_dirs"]) == own / "logo.pdf"
    assert find("style.sty", lh["template_dir"], lh["asset_dirs"]) == plugin / "style.sty"
    assert find("missing.sty", lh["template_dir"], lh["asset_dirs"]) is None
    assert find("style.sty", lh["template_dir"], []) is None


def test_no_profile_names_no_domain_and_matches_nothing(profile):
    assert institution.domains() == () and institution.own_suffix() == ""
    assert not institution.is_internal("anyone@example.edu")
    assert institution.safety_rail_phrase() == "a first email outside the user's institution"
    assert institution.video() == {"platform": "", "link": ""}
    assert institution.video_platform() == "" and institution.video_link() == "" and institution.video_join_block() == ""


def test_the_video_block_names_the_platform_link_and_meeting_id(profile):
    # the block wins over the legacy zoom_link; the id goes under the link
    profile({"email": "me@example.edu", "zoom_link": "https://zoom.example/old", "zoom_meeting_id": "111",
             "institution": {"video": {"platform": "Microsoft Teams", "link": "https://teams.example/me",
                                       "meeting_id": "222 333"}}})
    assert institution.video_platform() == "Microsoft Teams" and institution.video_link() == "https://teams.example/me"
    assert institution.video_meeting_id() == "222 333"
    assert institution.video_join_block() == "Join Microsoft Teams meeting\nhttps://teams.example/me\nMeeting ID: 222 333"
    # legacy keys alone: Zoom, the old link, the old id
    profile({"email": "me@example.edu", "zoom_link": "https://zoom.example/old", "zoom_meeting_id": "111"})
    assert institution.video_join_block() == "Join Zoom meeting\nhttps://zoom.example/old\nMeeting ID: 111"
    # a link with no platform named
    profile({"email": "me@example.edu", "institution": {"video": {"link": "https://meet.example/me"}}})
    assert institution.video_join_block() == "Join the meeting online\nhttps://meet.example/me"
