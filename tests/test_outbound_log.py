"""The modules that reach the network beside fetch_url log each request to
the reviewed log: booking_link's cal.com reads, list_digest's redirect walk,
poll_filler's reads and submissions (under the configured name only), and
email_to_pdf's renderer, which runs offline and refuses every request.
A resident process writes fetch-url.log; an owner process writes the
owner-private log. Nothing here reaches the network."""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))
import outbound_log as OL  # noqa: E402
import owner_trust  # noqa: E402


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A resident process on a scratch tree: the records go to its
    fetch-url.log."""
    t = tmp_path / "ea"
    (t / "config").mkdir(parents=True)
    monkeypatch.setenv("EA_DIR", str(t))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "private"))
    assert not owner_trust.is_owner_process()
    return t


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines()]


def test_a_resident_process_writes_fetch_url_log_and_never_the_owner_log(tree):
    OL.note("fetch", "unit", url="https://Example.org/a?b=1", method="GET", status=200, x=None)
    assert OL.note_owner("remote_brief", "unit", brief_chars=3) is False
    recs = _records(tree / "logs" / "fetch-url.log")
    assert len(recs) == 1
    assert recs[0]["host"] == "example.org" and recs[0]["source"] == "unit"
    assert "x" not in recs[0] and recs[0]["ts"].endswith("Z")
    assert not OL.owner_log_path().exists()


def test_a_planted_link_at_the_log_name_is_not_followed(tree, tmp_path):
    (tree / "logs").mkdir()
    victim = tmp_path / "victim"
    victim.write_text("")
    (tree / "logs" / "fetch-url.log").symlink_to(victim)
    OL.note("fetch", "unit", url="https://a.example/")              # never raises
    assert victim.read_text() == ""


def test_an_owner_process_writes_the_owner_private_log(tmp_path, monkeypatch):
    t = tmp_path / "ea"
    t.mkdir()
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    monkeypatch.setenv("EA_DIR", str(t))
    monkeypatch.setenv("EA_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(owner_trust, "install_ea_dir", lambda: t)
    assert owner_trust.is_owner_process()
    OL.note("fetch", "unit", url="https://a.example/")
    assert OL.note_owner("remote_brief", "unit", **OL.brief_fields("x" * 300))
    recs = _records(priv / "logs" / OL.OWNER_LOG_NAME)
    assert [r["event"] for r in recs] == ["fetch", "remote_brief"]
    assert recs[1]["brief_chars"] == 300 and len(recs[1]["brief_head"]) == 200
    assert len(recs[1]["brief_sha256"]) == 64
    assert (priv / "logs" / OL.OWNER_LOG_NAME).stat().st_mode & 0o777 == 0o600
    assert not (t / "logs" / "fetch-url.log").exists()


# ── poll_filler ───────────────────────────────────────────────────────

@pytest.fixture
def poll(tree, monkeypatch):
    import poll_filler as P
    monkeypatch.setattr(P, "EA_DIR", str(tree))
    (tree / "config" / "user-profile.json").write_text(json.dumps({"name": "Robin  Vale"}))
    return P


def test_the_poll_name_is_the_configured_one(poll):
    assert poll._pinned_name(None) == "Robin Vale"
    assert poll._pinned_name(" robin vale ") == "Robin Vale"
    with pytest.raises(SystemExit):
        poll._pinned_name("Robin Vale 5f3a secret notes")


def test_no_configured_name_refuses_the_fill(poll, tree):
    (tree / "config" / "user-profile.json").write_text(json.dumps({"email": "a@b.example"}))
    with pytest.raises(SystemExit):
        poll._pinned_name(None)


def test_a_fill_logs_host_poll_id_fields_and_sizes(poll, tree, monkeypatch, capsys):
    seen = {}

    def fake_fill(url, name, available):
        seen.update(url=url, name=name, available=available)
        return {"status": "filled", "filled": len(available)}
    monkeypatch.setitem(poll.API_FILLERS, "timeful", fake_fill)
    args = types.SimpleNamespace(url="https://timeful.app/e/AbC123", name=None,
                                 available='["1", "2", "3"]', available_file=None, visible=False)
    poll.cmd_fill_poll(args)
    assert seen["name"] == "Robin Vale"
    rec = _records(tree / "logs" / "fetch-url.log")[-1]
    assert rec["event"] == "poll_submit" and rec["source"] == "poll_filler"
    assert rec["host"] == "timeful.app" and rec["poll_id"] == "AbC123"
    assert rec["platform"] == "timeful" and rec["outcome"] == "ok"
    assert rec["fields"] == ["available", "name"] and rec["slots"] == 3
    assert rec["name"] == "Robin Vale" and rec["name_chars"] == 10
    assert rec["bytes"] == len(json.dumps({"name": "Robin Vale", "available": ["1", "2", "3"]}))
    # a refused name sends nothing and logs nothing
    args.name = "Someone Else"
    with pytest.raises(SystemExit):
        poll.cmd_fill_poll(args)
    assert len(_records(tree / "logs" / "fetch-url.log")) == 1


def test_a_failed_fill_is_still_logged(poll, tree, monkeypatch):
    def boom(url, name, available):
        raise RuntimeError("site down")
    monkeypatch.setitem(poll.API_FILLERS, "timeful", boom)
    args = types.SimpleNamespace(url="https://timeful.app/e/AbC123", name=None,
                                 available='["1"]', available_file=None, visible=False)
    with pytest.raises(RuntimeError):
        poll.cmd_fill_poll(args)
    rec = _records(tree / "logs" / "fetch-url.log")[-1]
    assert rec["event"] == "poll_submit" and rec["outcome"] == "error"


def test_poll_ids(poll):
    assert poll._poll_id("https://www.when2meet.com/?123-AbC") == "123-AbC"
    assert poll._poll_id("https://whenisgood.net/abc12/results") == "abc12"
    assert poll._poll_id("https://doodle.com/meeting/participate/id/xYz9") == "xYz9"


# ── booking_link ──────────────────────────────────────────────────────

def test_a_cal_com_read_is_logged(tree, monkeypatch):
    import booking_link as B

    class Resp:
        status_code = 200

        def json(self):
            return {"status": "success", "data": {"ok": 1}}
    monkeypatch.setattr(B.requests, "get", lambda *a, **kw: Resp())
    assert B._get("/slots", {"username": "jdoe", "eventTypeSlug": "30min"}, "v") == {"ok": 1}

    def down(*a, **kw):
        raise B.requests.ConnectionError("no route")
    monkeypatch.setattr(B.requests, "get", down)
    with pytest.raises(B.LinkError):
        B._get("/slots", {}, "v")
    recs = _records(tree / "logs" / "fetch-url.log")
    assert [r["event"] for r in recs] == ["fetch", "error"]
    assert recs[0]["host"] == "api.cal.com" and recs[0]["source"] == "booking_link"
    assert recs[0]["url"] == "https://api.cal.com/v2/slots?username=jdoe&eventTypeSlug=30min"
    assert recs[0]["status"] == 200


# ── list_digest ───────────────────────────────────────────────────────

def test_each_redirect_hop_is_logged(tree, monkeypatch):
    import contextlib
    import fetch_url
    import list_digest as L
    import requests

    def validate(url):
        if "10.0.0.1" in url:
            raise fetch_url.BlockedURL("private address")
        return {"host": OL.host_of(url), "ips": ["192.0.2.7"], "addrinfo": []}
    monkeypatch.setattr(fetch_url, "validate_url", validate)
    monkeypatch.setattr(fetch_url, "pinned_dns", lambda h, a: contextlib.nullcontext())
    hops = {"https://t.example/c/1": (302, "https://news.example/item"),
            "https://news.example/item": (200, ""),
            "https://t.example/c/2": (302, "http://10.0.0.1/admin")}

    def head(url, **kw):
        status, loc = hops[url]
        return types.SimpleNamespace(status_code=status, headers={"Location": loc} if loc else {})
    monkeypatch.setattr(requests, "head", head)
    assert L.resolve_redirect("https://t.example/c/1") == "https://news.example/item"
    assert L.resolve_redirect("https://t.example/c/2") == "https://t.example/c/2"
    recs = _records(tree / "logs" / "fetch-url.log")
    assert [(r["event"], r["host"]) for r in recs] == [
        ("fetch", "t.example"), ("fetch", "news.example"), ("fetch", "t.example"),
        ("blocked", "10.0.0.1")]
    assert all(r["source"] == "list_digest" for r in recs)
    assert recs[0]["method"] == "HEAD" and recs[0]["status"] == 302 and recs[1]["hop"] == 1


# ── email_to_pdf ──────────────────────────────────────────────────────

class _Route:
    def __init__(self, url):
        self.request = types.SimpleNamespace(url=url)
        self.done = ""

    def continue_(self):
        self.done = "continue"

    def abort(self):
        self.done = "abort"


def test_the_renderer_refuses_every_remote_request_and_logs_the_count(tree):
    import email_to_pdf as E
    r = E.Refused()
    routes = [_Route(u) for u in ("https://track.example/p.gif?u=1", "http://127.0.0.1:9/x",
                                  "https://track.example/logo.png", "data:image/png;base64,AAAA",
                                  "file:///etc/passwd")]
    for rt in routes:
        r.route(rt, rt.request)
    assert [rt.done for rt in routes] == ["abort", "abort", "abort", "continue", "abort"]
    r.log()
    rec = _records(tree / "logs" / "fetch-url.log")[-1]
    assert rec["event"] == "render" and rec["source"] == "email_to_pdf"
    assert rec["refused"] == 4 and rec["hosts"][0] == "track.example"


def test_the_renderer_is_offline_with_scripts_off():
    src = (Path(__file__).resolve().parent.parent / "preprocessor" / "email_to_pdf.py").read_text()
    assert "java_script_enabled=False" in src and "offline=True" in src
    assert 'page.route("**/*", refused.route)' in src
    assert "networkidle" not in src


def _chromium_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


def test_a_real_render_fetches_nothing(tree, tmp_path, monkeypatch):
    if os.environ.get("EA_SKIP_BROWSER") or not _chromium_ok():
        pytest.skip("no headless chromium here")
    import http.server
    import threading
    import email_to_pdf as E
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        body = (f"<html><body><p>Receipt</p><img src='http://127.0.0.1:{port}/pixel.gif'>"
                f"<link rel='stylesheet' href='http://127.0.0.1:{port}/s.css'>"
                f"<script>fetch('http://127.0.0.1:{port}/js')</script></body></html>")
        monkeypatch.setattr(E, "_fetch_message", lambda mid, g=None: {
            "subject": "Receipt", "sender": "shop@example.com", "date": "2026-10-19",
            "body": body, "body_type": "html"})
        out = tmp_path / "r.pdf"
        E.message_to_pdf("m1", str(out))
        assert out.read_bytes().startswith(b"%PDF")
    finally:
        srv.shutdown()
    assert hits == []
    rec = _records(tree / "logs" / "fetch-url.log")[-1]
    assert rec["event"] == "render" and rec["refused"] >= 2
