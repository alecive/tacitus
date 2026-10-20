"""draft_review: the pairs it shows, the blind side draw, the vote record
and tally, and the server's owner check. Nothing here reaches tailscale or
the real private dir."""
import http.client
import sys
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import remote_llm as rl  # noqa: E402
import draft_backtest as bt  # noqa: E402
import draft_review as dr  # noqa: E402

OWNER = "owner@example.edu"


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    rl.append(bt.ITEMS, {"draft_id": "d1", "agent_ts": "2026-10-02T10:00:00", "recipient": "morgan.vasquez41@example.edu",
                         "prompt": "Say yes <b>tonight</b>.", "sent_body": "Hi Pat,\nYes.\nKit",
                         "claude_body": "Body for Pat. Note: attach it.\n\nHi Pat,\n\nYes.\n\nKit"})
    rl.append(bt.ITEMS, {"draft_id": "d0", "agent_ts": "2026-10-01T10:00:00", "recipient": "emerson.yamamura4@example.edu",
                         "prompt": "Decline.", "claude_body": "Hi Kim,\n\nNo.\n\nKit"})
    rl.append(bt.ITEMS, {"draft_id": "d2", "agent_ts": "2026-10-03T10:00:00", "claude_body": "x"})
    rl.append(bt.REMOTE, {"draft_id": "d1", "qwen_body": "Hi Pat,\n\nYes, tonight.\n\nKit"})
    rl.append(bt.REMOTE, {"draft_id": "d0", "qwen_body": "Hi Kim,\n\nSorry, no.\n\nKit"})
    rl.append(bt.REMOTE, {"draft_id": "d2", "qwen_body": "NEED: the date", "need": True})
    rl.append(bt.JUDGED, {"draft_id": "d1", "kind": "quality", "order": "claude_first",
                          "better": "B", "why": "shorter"})
    return d


def test_items_have_both_bodies_oldest_first_with_notes_cut(store):
    items = dr.load_items()
    assert [x["draft_id"] for x in items] == ["d0", "d1"]          # d2: the remote asked for more
    assert items[1]["claude"] == "Hi Pat,\n\nYes.\n\nKit"
    assert items[1]["judged"][0]["why"] == "shorter"


def test_side_draw_is_stable_and_votes_map_to_writers(store):
    tok = dr.token()
    assert tok == dr.token() and len(tok) >= 16
    item = dr.load_items()[1]
    c = dr.claude_side(tok, "d1")
    r = "2" if c == "1" else "1"
    assert {dr.claude_side(tok, f"x{i}") for i in range(40)} == {"1", "2"}
    with pytest.raises(ValueError):
        dr.record(item, tok, {})
    rec = dr.record(item, tok, {"pick": r, f"send{c}": "light", f"send{r}": "as_is", "note": "tighter"})
    assert rec["pick"] == "remote" and rec["claude_send"] == "light" and rec["remote_send"] == "as_is"
    dr.record(item, tok, {"pick": c})                                # a later vote replaces it
    dr.record(dr.load_items()[0], tok, {"pick": "same"})
    s = dr.summary()
    assert s["votes"] == 2 and s["claude"] == 1 and s["same"] == 1 and s["remote"] == 0


def _serve(store):
    srv = dr.make_server(0, items=dr.load_items(), tok=dr.token(), owner_login=OWNER)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _req(srv, method, path, login=OWNER, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    headers = {"Tailscale-User-Login": login} if login else {}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=headers)
    r = conn.getresponse()
    return r.status, r.read().decode(), r.getheader("Location")


def test_server_serves_the_owner_only_and_reveals_after_the_vote(store):
    srv = _serve(store)
    try:
        b = srv.base
        assert _req(srv, "GET", b + "/", login=None)[0] == 403
        assert _req(srv, "GET", b + "/", login="someone@else.org")[0] == 403
        assert _req(srv, "GET", "/draft-review-wrong/")[0] == 404
        code, page, _ = _req(srv, "GET", b + "/item/2")
        assert code == 200 and "&lt;b&gt;tonight&lt;/b&gt;" in page   # task text is escaped
        assert "Claude" not in page and "What you sent" not in page    # blind before the vote
        assert _req(srv, "GET", b + "/item/9")[0] == 404
        assert _req(srv, "POST", b + "/item/2", body=urlencode({"note": "x"}))[0] == 400
        code, _, loc = _req(srv, "POST", b + "/item/2", body=urlencode({"pick": "neither"}))
        assert code == 303 and loc == b + "/item/2"
        code, page, _ = _req(srv, "GET", b + "/item/2")
        assert "Claude" in page and "What you sent" in page and "shorter" in page
    finally:
        srv.shutdown()
        srv.server_close()
