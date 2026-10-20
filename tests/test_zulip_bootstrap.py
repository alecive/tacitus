"""The Zulip bootstrap gives the EA bot its picture.

A bot the bootstrap creates gets the portrait in assets/tacitus-avatar.png;
an existing bot keeps its picture unless --avatar asks for the portrait. A
failed upload is a log line, never a failed bootstrap. The portrait is a
square PNG at the size Slack accepts as an app icon.
"""
import struct
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import zulip_bootstrap as zb  # noqa: E402


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.uploads = []
        self.fail = fail

    def upload(self, method, path, file):
        if self.fail:
            raise zb.ApiError(f"{method} {path}: HTTP 400: Uploaded file is larger than allowed")
        self.uploads.append((method, path, file))
        return {"result": "success"}


def test_the_portrait_is_a_square_png_slack_accepts():
    head = zb.AVATAR.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", head[16:24])
    assert width == height and 512 <= width <= 2000, (width, height)
    notes = (REPO / "assets" / "README.md").read_text()
    assert "commons.wikimedia.org/wiki/File:Tacitus_portrait.jpg" in notes and "public domain" in notes


def test_a_created_bot_gets_the_portrait():
    client = FakeClient()
    line = zb.ensure_avatar(client, 42, created=True, force=False)
    assert client.uploads == [("PATCH", "/bots/42", zb.AVATAR)]
    assert line == "avatar: uploaded tacitus-avatar.png"


def test_an_existing_bot_keeps_its_picture_unless_asked():
    client = FakeClient()
    assert zb.ensure_avatar(client, 42, created=False, force=False).startswith("avatar: kept")
    assert client.uploads == []
    zb.ensure_avatar(client, 42, created=False, force=True)
    assert client.uploads == [("PATCH", "/bots/42", zb.AVATAR)]


def test_a_failed_upload_does_not_stop_the_bootstrap():
    line = zb.ensure_avatar(FakeClient(fail=True), 42, created=True, force=False)
    assert line.startswith("avatar: not uploaded:") and "larger than allowed" in line


class FakeResponse:
    status_code = 200
    reason = "OK"
    text = '{"result": "success"}'

    def json(self):
        return {"result": "success", "msg": ""}


def test_upload_sends_one_file_as_multipart_form_data():
    client = zb.Client("http://zulip.example:8080", "owner@example.com", "k")
    sent = {}

    def request(method, url, **kwargs):
        sent.update(method=method, url=url, **kwargs)
        sent["bytes"] = kwargs["files"]["file"][1].read(8)
        return FakeResponse()

    client.session.request = request
    client.upload("PATCH", "/bots/42", zb.AVATAR)
    assert sent["method"] == "PATCH" and sent["url"] == "http://zulip.example:8080/api/v1/bots/42"
    name, _, content_type = sent["files"]["file"]
    assert (name, content_type) == ("tacitus-avatar.png", "image/png")
    assert sent["bytes"] == b"\x89PNG\r\n\x1a\n" and "data" not in sent


def test_the_avatar_flag_reaches_the_bootstrap(monkeypatch):
    seen = {}
    monkeypatch.setattr(zb, "bootstrap", lambda client, site, avatar=False: seen.setdefault("avatar", avatar) and 0)
    base = ["--site", "http://zulip.example:8080", "--owner-email", "owner@example.com", "--owner-key", "k"]
    zb.main(base + ["--avatar"])
    assert seen["avatar"] is True
    with pytest.raises(SystemExit):
        zb.main(base + ["--avatar", "--check"])
