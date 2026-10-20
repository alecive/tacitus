"""publish_gate: the owner's click before a site publish or an Overleaf push.

What must hold: a staged request names one target and one commit; the scan
flags email headers, email addresses, phone numbers and message ids on the
lines a diff adds, and nothing on removed or context lines; a token is
bound to the target, the commit and the base, works once, and a plugin card
id can never mint one; the bot runs the tool verbs as the resident with a
clean environment and the token in EA_CARD_TOKEN, never on the command
line; the card names the files and the scan result. The verbs' own refusals
are in test_site_tools.py and test_overleaf_tools.py.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import plugin_cards  # noqa: E402
import plugin_manifest  # noqa: E402
import publish_gate as G  # noqa: E402
import resident_io  # noqa: E402

A = "a" * 40
B = "b" * 40


@pytest.fixture(autouse=True)
def _tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "TOKEN_PATH", tmp_path / "card-tokens.json")
    monkeypatch.setattr(resident_io, "_owner_check", lambda: False)


DIFF = """diff --git a/index.html b/index.html
index 1111111..2222222 100644
--- a/index.html
+++ b/index.html
@@ -1,3 +1,8 @@
 <p>context kit.calloway@example.edu</p>
-<p>removed rio.sorensen85@example.com</p>
+<p>Write to mika.kowalczyk75@example.edu or call (303) 555-0142.</p>
+From: Rio Sorensen <rio.sorensen85@example.com>
+<p>Thread <CAF3x9q2abcd@mail.example.com></p>
+<p>Outlook id AAMkAGI2THVSAAA1234567890abcdefghijklmnop</p>
+<p>Talk on 2026-10-19 at 10:30, room 1B40, ISBN 978-3-16-148410-0</p>
"""


def test_the_scan_flags_each_kind_on_added_lines_only():
    scan = G.scan_diff(DIFF)
    kinds = [(f["kind"], f["line"]) for f in scan["findings"]]
    assert ("email address", 2) in kinds and ("phone number", 2) in kinds
    assert ("email header", 3) in kinds
    assert ("message id", 4) in kinds and ("message id", 5) in kinds
    assert scan["counts"] == {"email address": 1, "phone number": 1, "email header": 1,
                              "message id": 2}
    # the context and removed lines carry addresses too; neither counts
    assert not any("kit.calloway" in f["match"] or f["line"] == 1 for f in scan["findings"])
    # a date, a time and an ISBN are not phone numbers
    assert not any(f["line"] == 6 for f in scan["findings"])
    assert G.scan_diff("") == {"findings": [], "counts": {}}


def test_added_lines_number_the_new_side():
    lines = G.added_lines(DIFF)
    assert lines[0] == ("index.html", 2,
                        "<p>Write to mika.kowalczyk75@example.edu or call (303) 555-0142.</p>")
    assert [n for _, n, _ in lines] == [2, 3, 4, 5, 6]


@pytest.mark.parametrize("payload,why", [
    ({"site": "t-site", "commit": "abc1234"}, ""),
    ({"overleaf": "/home/ea-bot/.ea/docs/p/overleaf", "commit": A}, ""),
    ({"commit": A}, "one of"),
    ({"site": "t", "overleaf": "/x", "commit": A}, "one of"),
    ({"site": "t"}, "commit"),
    ({"site": "t", "commit": "HEAD"}, "commit"),
    ({"site": "../x", "commit": A}, "slug"),
    ({"overleaf": "docs/p", "commit": A}, "absolute"),
    ([], "JSON object"),
])
def test_a_staged_request_names_one_target_and_one_commit(payload, why):
    req, err = G.parse_request(payload)
    if why:
        assert req is None and why in err
    else:
        assert err == "" and req["commit"] == payload["commit"]


def test_the_token_is_bound_to_target_commit_and_base_and_works_once():
    item = {"target": "site", "name": "t-site", "commit": A, "base": B}
    tok = G.mint(item)
    assert not G.consume(tok, {**item, "commit": B})
    assert not G.consume(tok, {**item, "base": A})
    assert not G.consume(tok, {**item, "name": "other"})
    assert not G.consume(tok, {**item, "target": "overleaf"})
    assert G.consume(tok, item)
    assert not G.consume(tok, item)
    with pytest.raises(G.GateError, match="already used"):
        G.require_token(tok, item)
    with pytest.raises(G.GateError, match="runs only after the owner clicks"):
        G.require_token("", item)


def test_no_plugin_card_can_mint_a_publish_token():
    # plugin card ids follow the manifest's name rule, which has no underscore
    for card in G.CARD_IDS.values():
        assert not plugin_manifest.NAME_RE.match(card)
        assert plugin_cards.ACTION_RE.match(plugin_cards.action_of(G.TOKEN_NAMESPACE, card, {}))
    item = {"target": "site", "name": "t-site", "commit": A, "base": B}
    raw = plugin_cards.mint_token("core", "site-publish", item, path=G.TOKEN_PATH)
    assert not G.consume(raw, item)


def test_the_bot_runs_the_verbs_as_the_resident_with_the_token_in_the_environment(tmp_path):
    calls = []

    def runner(argv, timeout):
        calls.append((argv, timeout))
        return 0, json.dumps({"status": "published", "url": "https://t/", "commit": "aaaaaaa",
                              "commit_url": "https://github.com/o/t/commit/" + A, "live": True}), ""

    item = {"target": "site", "name": "t-site", "commit": A, "base": B}
    res = G.execute(item, "tok-123", tree=tmp_path, ea_dir="/home/ea-bot/.ea",
                    runner=runner, home=Path("/home/ea-bot"))
    assert res["status"] == "published"
    argv, timeout = calls[0]
    assert argv[:6] == ["sudo", "-n", "-u", "ea-bot", "env", "-i"]
    assert f"{G.TOKEN_ENV}=tok-123" in argv and "HOME=/home/ea-bot" in argv
    tail = argv[argv.index("-s") + 1:]
    assert tail == [str(tmp_path / "preprocessor" / "site_tools.py"), "publish", "--site",
                    "t-site", "--commit", A, "--base", B]
    assert "tok-123" not in " ".join(tail) and timeout == G.PUBLISH_TIMEOUT_S
    status, message = G.result_lines(item, res)
    assert status.startswith(":white_check_mark: Published `aaaaaaa`")
    assert "serves it now" in message

    calls.clear()
    G.preview({"target": "overleaf", "name": "/d/overleaf", "commit": "abc1234"},
              tree=tmp_path, ea_dir="/e", runner=runner, home=Path("/home/ea-bot"))
    argv, _ = calls[0]
    assert argv[-6:] == [str(tmp_path / "preprocessor" / "overleaf_tools.py"), "preview",
                         "--dir", "/d/overleaf", "--commit", "abc1234"][-6:]
    assert not any(a.startswith(G.TOKEN_ENV) for a in argv)


def test_a_refusal_comes_back_as_the_tools_error():
    def refused(argv, timeout):
        return 2, "", json.dumps({"status": "error", "error": "BLOCKED: invalid token"})

    item = {"target": "overleaf", "name": "/d", "commit": A, "base": B}
    res = G.execute(item, "t", tree=Path("/x"), ea_dir="/e", runner=refused, home=Path("/h"))
    assert res == {"status": "error", "error": "BLOCKED: invalid token"}
    status, message = G.result_lines(item, res)
    assert status.startswith(":x: Not published") and "aaaaaaa" in message


def test_the_card_names_the_files_and_the_scan():
    req = {"target": "site", "name": "t-site", "commit": "aaaaaaa"}
    pv = {"title": "Workshop", "url": "https://w.example/", "private": False, "commit": A,
          "base": B, "subject": "Agenda", "commits": 1, "repo": "o/workshop",
          "files": [{"path": "index.html", "change": "modified"}], "diff": DIFF}
    facts = G.site_facts(req, pv, fetch=lambda repo: (200, {"visibility": "public",
                                                            "full_name": repo}),
                         private_dir=Path("/nonexistent-private"))
    r = G.render(req, pv, G.scan_diff(DIFF), facts)
    # the commit's own words and github.json's values are inert spans (card_text.inert)
    text = G._ct.strip_inert("\n".join(r["lines"]))
    assert G._ct.strip_inert(r["title"]) == ":lock: *Publish to Workshop*"
    assert "GitHub reports o/workshop public, so anyone on the internet reads it" in text
    assert "github.json, which the EA can edit" in text
    assert "Commit `aaaaaaa` on top of `bbbbbbb`: Agenda" in text
    assert "index.html (modified)" in text
    assert G._ct.inert("Agenda", 200) in "\n".join(r["lines"])
    assert "1 email header" in text and "2 message ids" in text and "1 phone number" in text
    assert r["approve_word"] == "publish"
    clean = G.render(req, {**pv, "diff": ""}, G.scan_diff(""))
    assert any("Scan: no email headers" in ln for ln in clean["lines"])
    assert G.item_of(req, pv) == {"target": "site", "name": "t-site", "repo": "o/workshop",
                                  "commit": A, "base": B}
    with pytest.raises(G.GateError):
        G.item_of(req, {**pv, "base": "bbbbbbb"})
    with pytest.raises(G.GateError, match="repository"):
        G.item_of(req, {**pv, "repo": "o/x; rm"})


REQ = {"target": "site", "name": "t-site", "commit": "aaaaaaa"}
PV = {"title": "Lab notes", "url": "https://lab.example/", "private": True, "commit": A,
      "base": B, "subject": "Notes", "commits": 1, "repo": "o/lab", "files": [], "diff": ""}


def test_the_reach_comes_from_github_not_from_github_json(tmp_path):
    """github.json is ea-bot space: its private flag does not say who reads
    the site. GitHub's answer for the repository does, and a public
    repository behind a site marked private is a warning."""
    none = tmp_path / "private"
    pub = G.site_facts(REQ, PV, fetch=lambda r: (200, {"private": False}), private_dir=none)
    assert pub["visibility"] == "public" and any("reports its repository public" in w
                                                 for w in pub["warnings"])
    text = "\n".join(G.render(REQ, PV, G.scan_diff(""), pub)["lines"])
    assert "anyone on the internet reads it" in text and ":warning:" in text
    hidden = G.site_facts(REQ, PV, fetch=lambda r: (404, {}), private_dir=none)
    assert hidden["visibility"] == "not public" and not hidden["warnings"]
    down = G.site_facts(REQ, PV, fetch=lambda r: (0, {}), private_dir=none)
    assert "reach is unchecked" in "\n".join(G.render(REQ, PV, G.scan_diff(""), down)["lines"])
    renamed = G.site_facts(REQ, PV, fetch=lambda r: (200, {"visibility": "private",
                                                           "full_name": "o/other"}),
                           private_dir=none)
    assert any("o/other" in w for w in renamed["warnings"])


def test_the_owners_sites_file_wins_and_must_name_the_same_repository(tmp_path):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    own = priv / "sites.json"
    own.write_text(json.dumps({"t-site": {"repo": "o/lab", "title": "Lab", "private": False,
                                          "url": "https://lab.mine/"}}))
    own.chmod(0o600)
    facts = G.site_facts(REQ, PV, fetch=lambda r: (404, {}), private_dir=priv)
    assert (facts["source"], facts["url"], facts["private"]) == ("owner", "https://lab.mine/", False)
    text = "\n".join(G.render(REQ, PV, G.scan_diff(""), facts)["lines"])
    assert "https://lab.mine/" in text and "your sites.json" in text and "EA can edit" not in text
    own.write_text(json.dumps({"t-site": {"repo": "o/real"}}))
    assert "o/real" in G.site_facts(REQ, PV, fetch=lambda r: (200, {}), private_dir=priv)["refuse"]
    own.chmod(0o666)
    assert "refused" in G.site_facts(REQ, PV, fetch=lambda r: (200, {}), private_dir=priv)["refuse"]


def test_the_card_shows_the_added_text_with_counts():
    diff = "".join(f"diff --git a/f{i}.html b/f{i}.html\n--- a/f{i}.html\n+++ b/f{i}.html\n"
                   "@@ -0,0 +1,8 @@\n" + "".join(f"+line {j} `x` of f{i}\n" for j in range(8))
                   for i in range(8))
    lines = G.added_text_lines(diff)
    text = "\n".join(lines)
    # each path and line is an inert span, so a backtick in it stays literal
    assert G._ct.inert("f0.html", 80) + " (+8 lines):" in text
    assert "+ " + G._ct.inert("line 0 `x` of f0", G.CARD_ADDED_WIDTH) in text
    assert "line 5 `x` of f0" not in text and "and 3 more added lines" in text
    assert "and 2 more files with added lines" in text
    assert sum(len(ln) for ln in lines) < G.CARD_ADDED_BUDGET + 400
    r = G.render(REQ, {**PV, "diff": diff}, G.scan_diff(diff))
    assert "Added text (the first lines of each file):" in r["lines"]
    assert G.added_text_lines("") == []


def test_the_bot_refuses_a_card_the_owner_side_facts_refuse():
    body = _fn("_post_publish_card")
    assert '_pg.site_facts(req, pv) if req["target"] == "site" else None' in body
    assert 'facts.get("refuse")' in body and "_pg.render(req, pv," in body


# --- the bot and the lanes --------------------------------------------------

BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()
DENY = [ln.strip() for ln in (ROOT / "scripts" / "disallowed-tools.txt").read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")]


def _fn(name: str) -> str:
    m = re.search(rf"^(?:@app\.action\([^)]*\)\n)?def {name}\(.*?(?=^\S)", BOT_SRC, re.M | re.S)
    assert m, name
    return m.group(0)


def test_the_lanes_cannot_call_the_gated_verbs():
    for verb in ("site_tools.py publish", "overleaf_tools.py push"):
        assert f"Bash(python3 *{verb} *)" in DENY and f"Bash(*{verb} *)" in DENY
    assert "Bash(*git push *)" in DENY
    # doc_review (the private review repo) stays un-gated
    assert not any("doc_review" in d for d in DENY)


def test_only_the_owners_click_mints_a_publish_token():
    assert BOT_SRC.count("_pg.mint(") == 1
    approve = _fn("handle_approve_publish")
    assert "_verify_button_user(ev)" in approve and "_reject_if_stale_confirmation" in approve
    assert "_claim_plugin_card(ev)" in approve
    assert "_pg.mint(" in _fn("_run_publish")
    assert "mint" not in _fn("handle_cancel_publish")
    assert '("publish", "pending-publish.json")' in BOT_SRC
    assert 'elif kind == "publish":' in _fn("_post_staged_action")
    assert "PUBLISH_ACTION_FILES" in _fn("_pending_events_poller")
