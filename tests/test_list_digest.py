#!/usr/bin/env python3
"""Tests for the list-digest agent: preprocessor/list_digest.py (parse,
triage, discovery, archive bookkeeping) and its wiring in run-agent.sh,
preprocess.py and skills/list-digest.md.

The fixture is a made-up robotics-worldwide digest in the shape of a real
one: 17 items, 7 of them job posts, of which one dataset release is the
only item worth surfacing.

Usage:
    python3 tests/test_list_digest.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

# Sandbox before import: list_digest derives its state/config paths from
# EA_DIR at import time, and no test may touch the live ~/.ea.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-list-digest-")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "preprocessor"))

import list_digest as ld  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "robotics-worldwide-digest-2026-09-01.txt"
RIO_FIXTURE = REPO / "tests" / "fixtures" / "rio-bulletin-2026-09-01.html"
RUN_AGENT = REPO / "run-agent.sh"
SKILL = REPO / "skills" / "list-digest.md"
TEMPLATE = REPO / "config" / "list-digests.json"

LIST = {
    "name": "robotics-worldwide",
    "senders": ["robotics-worldwide-request@lists.example.edu"],
    "list_address": "robotics-worldwide@lists.example.edu",
    "subject_contains": "",
    "drop_tags": ["jobs"],
}
RIO = {
    "name": "rio-bulletin",
    "senders": ["rio@mail.example.edu"],
    "list_address": "",
    "subject_contains": "",
    "format": "newsletter",
    "mode": "prioritize",
    "follow_links": True,
    "resolve_redirects": True,
    "emoji": ":moneybag:",
    "drop_tags": [],
    "skip_link_text": ["Research & Innovation Office", "Tech Transfer website",
                       "www.example.edu"],
    "skip_sections": [],
    "notes": "Limited Submission due dates are internal.",
}
JOB_ITEMS = {2, 3, 7, 11, 12, 14, 17}


def _full(text: str, html: bool = False) -> dict:
    return {"body": {"contentType": "html" if html else "text", "content": text}}


def _text() -> str:
    return ld.clean_text(FIXTURE.read_text())


def _graph_msg(mid="m1", sender="robotics-worldwide-request@lists.example.edu",
               to=("robotics-worldwide@lists.example.edu",), cc=(),
               subject="robotics-worldwide Digest Tue, 01 Sep 2026 (1/1)",
               received="2026-09-01T21:00:39Z") -> dict:
    def rec(a):
        return {"emailAddress": {"address": a, "name": a.split("@")[0]}}
    return {
        "id": mid, "conversationId": "c-" + mid, "subject": subject,
        "receivedDateTime": received,
        "from": rec(sender),
        "toRecipients": [rec(a) for a in to],
        "ccRecipients": [rec(a) for a in cc],
    }


# ── parsing the real digest ─────────────────────────────────────────

def test_real_digest_splits_into_every_toc_item():
    text = _text()
    items = ld.split_digest(text, "robotics-worldwide")
    assert ld.count_toc(text) == 17
    assert [it["n"] for it in items] == list(range(1, 18)), \
        f"item numbering broke: {[it['n'] for it in items]}"


def test_first_item_fields_and_decoded_links():
    it = ld.split_digest(_text(), "robotics-worldwide")[0]
    assert it["tags"] == ["software", "dataset"], it["tags"]
    # the subject header wraps onto a continuation line in the digest
    assert it["title"].startswith("Release of Frosty Lanes"), it["title"]
    assert it["title"].endswith("Winter-Ready Vehicles"), it["title"]
    assert it["from_name"] == "Flo Prescott"
    assert it["from_email"] == "yuki.galloway82@example.com"
    assert "https://doi.example.org/10.0000/frosty-lanes" in it["links"]
    assert "https://github.com/example/frosty-lanes" in it["links"]
    assert not any("safelinks" in u for u in it["links"]), it["links"]
    assert "safelinks" not in it["body"]
    assert "22K" in it["body"]


def test_job_posts_dropped_in_code_by_tag():
    items = ld.split_digest(_text(), "robotics-worldwide")
    kept, dropped = ld.triage_items(items, ["jobs"], 2200)
    assert {d["n"] for d in dropped} == JOB_ITEMS, {d["n"] for d in dropped}
    assert all(d["reason"] == "tag:jobs" for d in dropped), dropped
    assert [k["n"] for k in kept] == sorted(set(range(1, 18)) - JOB_ITEMS)
    # the dataset release, the one relevant item, is the first kept row
    assert kept[0]["n"] == 1


def test_banner_removed_and_nested_google_redirect_unwrapped():
    text = _text()
    assert "[External email" not in text
    acme = next(i for i in ld.split_digest(text, "robotics-worldwide") if i["n"] == 2)
    # safelink -> google.com/url?q=... -> the company site
    assert "https://www.acmemotion.example" in acme["links"], acme["links"]


def test_truncation_marks_and_bounds_long_bodies():
    items = ld.split_digest(_text(), "robotics-worldwide")
    kept, _ = ld.triage_items(items, ["jobs"], 800)
    long_ones = [k for k in kept if k["chars"] > 800]
    assert long_ones, "fixture should contain bodies longer than 800 chars"
    for k in long_ones:
        assert k["body"].endswith("[... truncated ...]")
        assert len(k["body"]) <= 800 + len("\n[... truncated ...]")
    short = [k for k in kept if k["chars"] <= 800]
    assert all("truncated" not in k["body"] for k in short)


# ── unit behaviour ──────────────────────────────────────────────────

def test_unwrap_url_variants():
    wrapped = ("https://nam10.safelinks.protection.outlook.com/?url="
               "https%3A%2F%2Fexample.org%2Fpaper%3Fid%3D7%26v%3D2&data=05%7C02&reserved=0")
    assert ld.unwrap_url(wrapped) == "https://example.org/paper?id=7&v=2"
    assert ld.unwrap_url("https://example.org/plain") == "https://example.org/plain"
    nested = ("https://nam10.safelinks.protection.outlook.com/?url="
              "https%3A%2F%2Fwww.google.com%2Furl%3Fq%3Dhttps%3A%2F%2Fsite.kr%26source%3Dgmail")
    assert ld.unwrap_url(nested) == "https://site.kr"


def test_split_subject_drops_list_tag_and_normalises():
    tags, title = ld.split_subject(
        "[robotics-worldwide]  [Meetings] [CFP] HRI 2027 -\n Abstracts due", "robotics-worldwide")
    assert tags == ["meetings", "cfp"]
    assert title == "HRI 2027 - Abstracts due"
    tags, title = ld.split_subject("[Listname] Plain subject", "listname")
    assert tags == [] and title == "Plain subject"


def test_job_reason_tag_then_subject_backstop():
    assert ld.job_reason(["jobs"], "anything", []) == "tag:jobs"
    assert ld.job_reason(["job opening"], "anything", []) == "tag:job opening"
    assert ld.job_reason(["positions"], "anything", []) == "tag:positions"
    assert ld.job_reason(["stellen"], "x", ["stellen"]) == "tag:stellen"  # per-list extension
    assert ld.job_reason([], "Postdoctoral position in SLAM at X", []) == "subject:job"
    assert ld.job_reason([], "PostDoc position at Example Lab - ROVER project", []) == "subject:job"
    assert ld.job_reason([], "Postdoctoral Researcher/Research Assistant - AI & Robotics", []) == "subject:job"
    assert ld.job_reason([], "We are hiring: perception engineers", []) == "subject:job"
    assert ld.job_reason([], "Tenure-track opening, robotics", []) == "subject:job"
    # not jobs: a workshop, a dataset whose title contains 'positioning',
    # and newsletter events that merely mention postdocs
    assert ld.job_reason(["meetings"], "IROS workshop on SLAM", []) is None
    assert ld.job_reason(["software"], "Dataset for robot positioning in tunnels", []) is None
    assert ld.job_reason(["journals"], "Robotics Journal: Robot learns to stack blocks", []) is None
    assert ld.job_reason([], "Postdoc & Postdoc Mentor Appreciation Luncheon", []) is None
    assert ld.job_reason([], "Celebrate postdocs during National Postdoc Appreciation Week", []) is None


def test_parse_from():
    assert ld.parse_from("Flo Prescott <yuki.galloway82@example.com>") == ("Flo Prescott", "yuki.galloway82@example.com")
    assert ld.parse_from('"Abernathy, Gray" <bri.oyelaran53@example.edu>') == ("Abernathy, Gray", "bri.oyelaran53@example.edu")
    assert ld.parse_from("bare@example.org") == ("", "bare@example.org")


def test_match_list_sender_or_recipient_with_optional_subject_filter():
    lists = [LIST]
    # digest mode: the -request robot sends TO the list
    assert ld.match_list(_graph_msg(), lists) is LIST
    # individual-post mode: a poster sends to the list address
    assert ld.match_list(_graph_msg(sender="zev.holloway59@example.edu"), lists) is LIST
    # list address only in CC still counts
    assert ld.match_list(_graph_msg(sender="sage.brightwater60@example.org", to=("jo.greenhalgh55@example.edu",),
                                    cc=("robotics-worldwide@lists.example.edu",)), lists) is LIST
    # unrelated mail does not
    assert ld.match_list(_graph_msg(sender="hale.zielinski59@example.edu", to=("jo.greenhalgh55@example.edu",)), lists) is None
    # subject_contains narrows
    narrow = dict(LIST, subject_contains="Digest")
    assert ld.match_list(_graph_msg(subject="[robotics-worldwide] [Jobs] x"), [narrow]) is None
    assert ld.match_list(_graph_msg(), [narrow]) is narrow


def test_mailman_style_digest_parses():
    body = """Send Robotics mailing list submissions to robotics@lists.example.org

Today's Topics:

   1. Dataset release: TunnelSLAM (Ana Poster)
   2. Postdoc position (Bob Hiring)

----------------------------------------------------------------------

Message: 1
Date: Mon, 31 Aug 2026 09:00:00 +0000
From: Ana Poster <hollis.ellery34@example.edu>
To: robotics@lists.example.org
Subject: [Robotics] Dataset release: TunnelSLAM
Message-ID: <jo.ashcombe20@example.edu>
Content-Type: text/plain; charset="utf-8"

We release TunnelSLAM, 40 km of LiDAR in mines. https://tunnelslam.org

------------------------------

Message: 2
Date: Mon, 31 Aug 2026 10:00:00 +0000
From: Bob Hiring <bob@corp.com>
Subject: [Robotics] Postdoc position in manipulation

Apply by Friday.

------------------------------

End of Robotics Digest
"""
    items = ld.split_digest(ld.clean_text(body), "Robotics")
    assert [i["n"] for i in items] == [1, 2]
    assert items[0]["title"] == "Dataset release: TunnelSLAM"
    assert items[0]["tags"] == []  # the list's own tag is not a category
    assert items[0]["links"] == ["https://tunnelslam.org"]
    assert items[0]["body"].startswith("We release TunnelSLAM")
    kept, dropped = ld.triage_items(items, [], 2200)
    assert [k["n"] for k in kept] == [1]
    assert dropped[0]["reason"] == "subject:job"


def test_single_post_fallback_and_unparsed_raw_text():
    msg = _graph_msg(sender="zev.holloway59@example.edu",
                     subject="[robotics-worldwide] [Software] New SLAM toolbox")
    entry = ld.build_digest_entry(msg, LIST, _full("Hi all, our toolbox is at https://x.org/slam"), 2200)
    assert entry["kind"] == "post"
    assert entry["items_total"] == 1
    assert entry["items"][0]["title"] == "New SLAM toolbox"
    assert entry["items"][0]["tags"] == ["software"]
    assert entry["items"][0]["links"] == ["https://x.org/slam"]
    assert "raw_text" not in entry

    # announces a table of contents but nothing splits: format change
    weird = "digest\n\nTable of contents:\n\n* 1 - [robotics-worldwide] A thing - X\n* 2 - [robotics-worldwide] B thing - Y\n\n=== items ===\nA thing body\nB thing body\n"
    entry = ld.build_digest_entry(_graph_msg(), LIST, _full(weird), 2200)
    # every entry carries the list's settings for the skill
    assert entry["mode"] == "filter" and entry["follow_links"] is False
    assert entry["emoji"] == ":newspaper:" and entry["notes"] == ""
    assert entry["kind"] == "unparsed"
    assert entry["raw_text"].startswith("digest")
    assert any("not recognised" in w for w in entry["parse_warnings"])


def test_collect_with_injected_inbox_never_touches_graph():
    cfg = ld.load_config(Path("/nonexistent/list-digests.json"))
    cfg["lists"] = [dict(LIST)]
    inbox = [
        _graph_msg(mid="digest-1"),
        _graph_msg(mid="other", sender="hale.zielinski59@example.edu", to=("jo.greenhalgh55@example.edu",), subject="Budget"),
        _graph_msg(mid="digest-0", received="2026-08-31T21:00:00Z",
                   subject="robotics-worldwide Digest Mon, 31 Aug 2026 (1/1)"),
    ]
    bodies = {"digest-1": FIXTURE.read_text(),
              "digest-0": "Table of contents:\n\n* 1 - [robotics-worldwide] [Jobs] x - Y\n\n" + "-" * 70 + "\n\n * 1\nDate: d\nFrom: Y <gus.carrow65@example.org>\nSubject: [robotics-worldwide] [Jobs] x\n\nbody\n"}
    calls = []

    def fetch_full(mid):
        calls.append(mid)
        return {"body": {"contentType": "text", "content": bodies[mid]}}

    out = ld.collect(cfg, inbox=inbox, fetch_full=fetch_full, resolver=lambda u: u)
    assert out["mode"] == "list-digest"
    assert sorted(calls) == ["digest-0", "digest-1"], calls  # the dean's mail was never fetched
    assert [d["message_id"] for d in out["digests"]] == ["digest-0", "digest-1"]  # oldest first
    big = out["digests"][1]
    assert big["kind"] == "digest" and big["items_total"] == 17
    assert len(big["items"]) == 10 and len(big["dropped"]) == 7
    assert big["parse_warnings"] == []
    small = out["digests"][0]
    assert small["items_total"] == 1 and small["items"] == [] and len(small["dropped"]) == 1
    assert out["errors"] == []
    assert "relevance" not in out
    assert big["rules"]["scopes"] == ["newsletters", "newsletters:robotics-worldwide"]


def test_rules_ride_on_each_entry_by_scope():
    import preferences as prefs
    store = prefs._empty()
    prefs.add_rule(store, kind="avoid", scopes=["funding"], text="Standing BAAs are not opportunities.",
                   quote="just noise", date_str="2026-09-01", rule_id="baas")
    prefs.add_rule(store, kind="instruction", scopes=["newsletters:robotics-worldwide"],
                   text="Snippets then archive; never job postings.", date_str="2026-09-01", rule_id="rw")
    prefs.add_rule(store, kind="fact", scopes=["newsletters"], text="Tenured professor.", rule_id="prof")
    prefs.add_calibration(store, scope="newsletters:rio-bulletin", note="2 of 44 posted.", date_str="2026-09-01")
    rio = dict(RIO, scopes=["funding"], resolve_redirects=False)
    e_rio = ld.build_digest_entry(_graph_msg(sender="rio@mail.example.edu"), rio,
                                  _full(RIO_FIXTURE.read_text(), html=True), 2200, prefs_data=store)
    e_rw = ld.build_digest_entry(_graph_msg(), LIST, _full(FIXTURE.read_text()), 2200, prefs_data=store)
    assert e_rio["rules"]["scopes"] == ["newsletters", "newsletters:rio-bulletin", "funding"]
    assert [r["id"] for r in e_rio["rules"]["avoid"]] == ["baas"]        # via the list's own scope
    assert e_rio["rules"]["instructions"] == []                          # the robotics instruction stays there
    assert [c["note"] for c in e_rio["rules"]["calibration"]] == ["2 of 44 posted."]
    assert [r["id"] for r in e_rw["rules"]["instructions"]] == ["rw"]
    assert e_rw["rules"]["avoid"] == [] and e_rw["rules"]["calibration"] == []
    assert [r["id"] for r in e_rw["rules"]["facts"]] == ["prof"]         # parent scope applies to both
    assert [r["id"] for r in e_rio["rules"]["facts"]] == ["prof"]
    # an empty store (fresh install) still yields the shape the skill reads
    empty = ld.build_digest_entry(_graph_msg(), LIST, _full(FIXTURE.read_text()), 2200, prefs_data=prefs._empty())
    assert empty["rules"] == {"scopes": ["newsletters", "newsletters:robotics-worldwide"], "instructions": [],
                              "prefer": [], "avoid": [], "facts": [], "calibration": []}


def test_legacy_relevance_block_is_converted_not_lost():
    legacy = {
        "instructions": [{"date": "2026-09-01", "list": "robotics-worldwide", "text": "snippets then archive"},
                         {"date": "2026-09-01", "list": "rio-bulletin", "text": "prioritize then ditch"},
                         {"date": "2026-09-01", "list": "*", "text": "BAAs are garbage"}],
        "interests": "SLAM and perception",
        "always_relevant": ["dataset releases"],
        "never_relevant": ["job postings", "special issues"],
        "eligibility": "tenured",
        "calibration": ["1 of 17 posted"],
    }
    r = ld.legacy_relevance_rules(legacy, "robotics-worldwide")
    assert [i["text"] for i in r["instructions"]] == ["snippets then archive", "BAAs are garbage"]
    assert [p["text"] for p in r["prefer"]] == ["dataset releases"]
    assert [a["text"] for a in r["avoid"]] == ["job postings", "special issues"]
    assert [f["text"] for f in r["facts"]] == ["Research interests: SLAM and perception", "Eligibility: tenured"]
    assert r["calibration"][0]["note"] == "1 of 17 posted"
    cfg = ld.load_config(Path("/nonexistent"))
    assert cfg["relevance"] is None
    cfg["lists"] = [dict(LIST)]
    cfg["relevance"] = legacy
    import preferences as prefs
    out = ld.collect(cfg, inbox=[_graph_msg()], fetch_full=lambda m: _full(FIXTURE.read_text()),
                     resolver=lambda u: u, prefs_data=prefs._empty())
    rules = out["digests"][0]["rules"]
    assert rules["scopes"] == ["newsletters", "newsletters:robotics-worldwide"]
    assert [a["text"] for a in rules["avoid"]] == ["job postings", "special issues"]
    assert ld.legacy_relevance_rules("not a dict", "x")["avoid"] == []


def test_collect_reports_bad_fetch_and_missing_lists():
    cfg = ld.load_config(Path("/nonexistent"))
    assert ld.collect(cfg, inbox=[], fetch_full=lambda m: {})["errors"] == \
        ["no lists configured in list-digests.json"]
    cfg["lists"] = [dict(LIST)]

    def boom(mid):
        raise RuntimeError("graph 503")
    out = ld.collect(cfg, inbox=[_graph_msg()], fetch_full=boom)
    assert out["digests"] == []
    assert out["errors"] and "graph 503" in out["errors"][0]


def test_html_digest_is_flattened():
    full = {"body": {"contentType": "html",
                     "content": "<p>Table of contents:</p><p>* 1 - x</p>"
                                "<p>----------------------------------------------------------------------</p>"
                                "<p> * 1<br>Date: d<br>From: A &lt;flo.thornbury67@example.org&gt;<br>Subject: [L] [Software] Tool</p>"
                                "<p>Body here</p>"}}
    text = ld._message_text(full)
    items = ld.split_digest(text, "L")
    assert len(items) == 1 and items[0]["title"] == "Tool", text


def test_archive_logs_history_and_reports_failures():
    data = {"digests": [
        {"list": "robotics-worldwide", "kind": "digest", "subject": "d1", "message_id": "ok-1",
         "items_total": 17, "items": [{"n": 1}], "dropped": [{"n": 2}] * 7},
        {"list": "robotics-worldwide", "kind": "digest", "subject": "d2", "message_id": "bad-2",
         "items_total": 3, "items": [], "dropped": []},
    ]}
    moved = []

    def mover(mid):
        if mid.startswith("bad"):
            raise RuntimeError("ErrorItemNotFound")
        moved.append(mid)
        return {"id": "new-" + mid}

    summary = ld.archive(data, "posted", mover=mover)
    assert moved == ["ok-1"]
    assert summary["archived"] == 1 and summary["failed"] == 1
    rows = [json.loads(l) for l in ld.HISTORY_FILE.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["archived"] is True and rows[0]["items_kept"] == 1 and rows[0]["items_dropped"] == 7
    assert rows[1]["archived"] is False and "ErrorItemNotFound" in rows[1]["error"]
    assert all(r["disposition"] == "posted" for r in rows)
    assert str(ld.HISTORY_FILE).startswith(os.environ["EA_DIR"])  # sandboxed


# ── newsletters (RIO Bulletin) ───────────────────────────────────────

def _rio_items(lst=None) -> list[dict]:
    return ld.parse_newsletter(RIO_FIXTURE.read_text(), lst or RIO)


def test_rio_newsletter_yields_every_content_link_and_no_navigation():
    items = _rio_items()
    titles = [i["title"] for i in items]
    assert len(items) == 44, len(items)
    assert titles[0] == "Water Infrastructure Workforce Training Grant"
    assert "DARPA Defense Sciences Office BAA" in titles
    assert "Community-Engaged Research Micro-Credential (apply by 9/14)" in titles
    # navigation, sender name, tracking pixels and contact links are not items
    for junk in ("View on website", "NEW", "Funding", "Opportunities", "Faculty",
                 "Development", "Proposal", "Contact Us", "www.example.edu",
                 "Research & Innovation Office", "Tech Transfer website",
                 "Uli Ravensworth", "Wynn Isenberg", "Lane Delacroix",
                 "Click here to manage your preferences or unsubscribe"):
        assert junk not in titles, junk
    assert [i["n"] for i in items] == list(range(1, 45))


def test_rio_footer_is_dropped_even_without_a_skip_list():
    """The heading-substring and whole-block rules catch the footer nav on
    their own; the config's skip_link_text only has to name the sender."""
    bare = dict(RIO, skip_link_text=[])
    titles = [i["title"] for i in _rio_items(bare)]
    for junk in ("Funding", "Opportunities", "Faculty", "Development", "Proposal"):
        assert junk not in titles, junk
    assert "Research & Innovation Office" in titles  # the one the config must name


def test_rio_sections_deadlines_flags_and_notes():
    by_title = {i["title"]: i for i in _rio_items()}
    water = by_title["Water Infrastructure Workforce Training Grant"]
    assert water["section"] == "Limited Submission Funding Opportunities"
    assert water["deadline"] == "09/09" and water["is_new"] is True and water["stage"] == ""
    young = by_title["National Awards for Young Scientists"]
    assert young["is_new"] is True and young["deadline"] == "10/19"
    assert by_title["Neuroscience Foundation Seed Grant"]["is_new"] is False
    # only the three NEW items carry the flag
    assert sorted(i["title"][:9] for i in by_title.values() if i["is_new"]) == \
        ["Junior Fa", "National ", "Water Inf"]
    early = by_title["Early Education Research & Program Evaluations"]
    assert early["stage"] == "LOI" and early["deadline"] == "09/30"
    assert "Note : If interested, email IFR's Wynn Isenberg" in early["body"]
    assert by_title["DARPA Defense Sciences Office BAA"]["deadline"] == "rolling"
    ev = by_title["Industry Pathways Workshop: Turning Research Strengths into New Funding Streams"]
    assert ev["section"] == "Events" and ev["deadline"] == "10/1 & 10/8"
    assert by_title["Campus Sustainability Research Summit"]["deadline"] == "10/8\u20139"
    panel = by_title["Join the Seed Grant Review Panel: Learn winning strategies and insights (register by 9/16)"]
    assert panel["section"] == "News You Need" and panel["deadline"] == "9/16"
    assert by_title["The eighth annual Research & Innovation Week is Oct. 12-16"]["deadline"] == ""
    # two links on one line are two items with the same line as context
    assert by_title["Start-Up Grants"]["body"].startswith("Due 11/18 : Binational Science Foundation")
    assert by_title["Research Grants"]["deadline"] == "11/18"
    # the tracking link is kept for the record
    assert all(i["tracking_url"].startswith("https://click.mail.example.edu/?qs=TRACK") for i in by_title.values())


def test_deadline_of_variants():
    assert ld._deadline_of("Due 09/09 : X") == ("09/09", "")
    assert ld._deadline_of("10/14 & 20 : Fall Fellowships") == ("10/14 & 20", "")
    assert ld._deadline_of("Due 12/01 : Y (Pre-Proposal)") == ("12/01", "Pre-Proposal")
    assert ld._deadline_of("Rolling Deadline : DARPA BAA") == ("rolling", "")
    assert ld._deadline_of("Thing (apply by 9/14)") == ("9/14", "")
    assert ld._deadline_of("No date here") == ("", "")


def test_resolve_item_links_uses_resolver_once_per_url_and_keeps_tracking():
    items = _rio_items()[:3]
    items[1]["links"] = [items[0]["links"][0]]  # a duplicated tracking link
    calls = []

    def fake(url):
        calls.append(url)
        return "https://www.example.edu/researchinnovation/" + url[-8:].lower()
    ld.resolve_item_links(items, fake)
    assert len(calls) == 2, calls  # cached
    assert items[0]["links"][0].startswith("https://www.example.edu/researchinnovation/")
    assert items[0]["url_host"] == "www.example.edu"
    assert items[0]["tracking_url"].startswith("https://click.mail.example.edu/")
    assert items[1]["links"][0] == items[0]["links"][0]


def test_resolve_redirect_walks_hops_under_the_address_policy(monkeypatch=None):
    """One hop at a time, validate_url on every hop, DNS pinned, and any
    failure hands back the URL we had (the tracking link still works)."""
    import contextlib
    import types
    import fetch_url
    seen_hops, validated = [], []
    responses = {
        "https://click.example.edu/?qs=1": (302, "https://www.example.edu/a"),
        "https://www.example.edu/a": (301, "/b"),
        "https://www.example.edu/b": (200, ""),
    }

    def fake_head(url, headers=None, timeout=None, allow_redirects=None):
        assert allow_redirects is False
        seen_hops.append(url)
        code, loc = responses[url]
        return types.SimpleNamespace(status_code=code, headers={"Location": loc} if loc else {})

    def fake_validate(url):
        validated.append(url)
        if "blocked" in url:
            raise fetch_url.BlockedURL("private address")
        return {"host": "h", "addrinfo": [], "ips": ["203.0.113.1"]}

    @contextlib.contextmanager
    def fake_pin(host, addrinfo):
        yield

    import requests
    orig = (requests.head, fetch_url.validate_url, fetch_url.pinned_dns)
    requests.head, fetch_url.validate_url, fetch_url.pinned_dns = fake_head, fake_validate, fake_pin
    try:
        assert ld.resolve_redirect("https://click.example.edu/?qs=1") == "https://www.example.edu/b"
        assert seen_hops == ["https://click.example.edu/?qs=1", "https://www.example.edu/a",
                             "https://www.example.edu/b"]
        assert validated == seen_hops  # every hop was checked before it was fetched
        # a hop that the policy blocks stops the walk; the caller gets the
        # last hop that passed (the redirector), never the blocked target
        responses["https://click.example.edu/?qs=2"] = (302, "https://blocked.internal/x")
        assert ld.resolve_redirect("https://click.example.edu/?qs=2") == "https://click.example.edu/?qs=2"
        assert "https://blocked.internal/x" not in seen_hops
    finally:
        requests.head, fetch_url.validate_url, fetch_url.pinned_dns = orig


def test_newsletter_entry_kind_settings_and_no_job_drops():
    msg = _graph_msg(mid="rio-1", sender="rio@mail.example.edu",
                     to=("emerson.vasquez27@example.edu",),
                     subject="September 1 | Young Scientists; Junior Faculty; ARPA-H; Seed Grants; more")
    entry = ld.build_digest_entry(msg, RIO, _full(RIO_FIXTURE.read_text(), html=True), 2200,
                                  resolver=lambda u: u.replace("click.mail.example.edu/?qs=", "www.example.edu/p/"))
    assert entry["kind"] == "newsletter"
    assert entry["items_total"] == 44 and entry["dropped"] == []
    assert entry["mode"] == "prioritize" and entry["follow_links"] is True
    assert entry["emoji"] == ":moneybag:" and "internal" in entry["notes"]
    assert entry["from_name"] == "rio"  # display name from the header
    assert entry["items"][0]["links"][0].startswith("https://www.example.edu/p/TRACK")
    assert entry["parse_warnings"] == []
    # a newsletter list still matches by sender
    assert ld.match_list(msg, [LIST, RIO]) is RIO


def test_auto_format_falls_back_to_newsletter_for_html_without_digest_rules():
    auto = dict(RIO, format="auto", resolve_redirects=False)
    entry = ld.build_digest_entry(_graph_msg(sender="rio@mail.example.edu"), auto,
                                  _full(RIO_FIXTURE.read_text(), html=True), 2200)
    assert entry["kind"] == "newsletter" and entry["items_total"] == 44
    # and a digest list never runs the newsletter parser on plain text
    entry = ld.build_digest_entry(_graph_msg(), dict(LIST, format="digest"),
                                  _full(FIXTURE.read_text()), 2200)
    assert entry["kind"] == "digest" and entry["items_total"] == 17


# ── wiring ──────────────────────────────────────────────────────────

def test_preprocess_registers_the_mode():
    from preprocessor.preprocess import MODES
    assert "list-digest" in MODES
    assert MODES["list-digest"].__name__ == "run_list_digest"


def test_run_agent_arm_is_wired_end_to_end():
    src = RUN_AGENT.read_text()
    assert re.search(r'^\s*list-digest\)\s*$', src, re.M), "no list-digest case arm"
    assert 'SKILL_FILE="$SKILL_DIR/list-digest.md"' in src
    assert 'list-digest)      OUTPUT_FILE="$STATE_DIR/list-digest-latest.json"' in src
    assert "len(d.get('digests', []))" in src, "delta count must include digests"
    assert 'if [[ "$AGENT" == "list-digest" ]]; then\n            log "Done (no list digest in the inbox)."' in src
    assert "morning-briefing|afternoon-catchup|list-digest|" in src, "anchor validation"
    assert 'grep -q "NOTHING_RELEVANT"' in src, "sentinel handling"
    assert 'list_digest.py" archive' in src, "archive step"
    # archive gated on a successful post or the silent verdict, never before
    post_idx = src.index("Post to Slack via bot token")
    arch_idx = src.index('list_digest.py" archive')
    assert arch_idx > post_idx, "archive must come AFTER the Slack post"
    assert '"$LIST_DIGEST_DISPOSITION" == "posted" && "$POST_RC" == "0"' in src
    assert "The preprocessed briefing is at: ${OUTPUT_FILE}" in src


def test_skill_and_template_exist_and_agree_on_the_sentinel():
    assert SKILL.exists()
    skill = SKILL.read_text()
    assert ":no_bell: NOTHING_RELEVANT" in skill
    assert "NOTHING_RELEVANT" in RUN_AGENT.read_text()
    flat = " ".join(skill.lower().split())
    assert "job" in flat and "you archive nothing" in flat
    # the only helper the skill may shell out to is fetch_url.py (follow
    # links in prioritize mode); the cron allowlist test derives required
    # grants from these mentions, and fetch_url.py is granted
    helpers = set(re.findall(r"python3?\s+\S*?([a-z_]+\.py)", skill))
    assert helpers == {"fetch_url.py"}, helpers
    assert "NOTHING_RELEVANT" in skill and "prioritize" in skill and "follow_links" in skill
    # The skill is mechanism; the user's topical judgment lives in the
    # preference store and rides on each entry as `rules`. The skill must
    # read every part of that shape...
    for key in ("rules", "instructions", "prefer", "avoid", "facts", "calibration"):
        assert f"`{key}" in skill, f"skill never mentions rules.{key}"
    assert "preferences.json" in skill
    # ...and must not carry a quoted user rule of its own: a topical
    # correction belongs in the preference store, not in the skill
    assert "The user's words" not in skill and "garbage" not in skill
    assert "never_relevant" not in skill and "always_relevant" not in skill
    tpl = json.loads(TEMPLATE.read_text())
    assert tpl["lists"] == [] and "relevance" not in tpl
    cfg = ld.load_config(TEMPLATE)
    assert cfg["lists"] == [] and cfg["lookback_days"] == 7
    assert "_doc" not in cfg and "_example" not in cfg
    assert cfg["relevance"] is None
    ptpl = json.loads((REPO / "config" / "preferences.json").read_text())
    assert ptpl["rules"] == [] and ptpl["calibration"] == [] and ptpl["version"] == 1


# ── a college Research Support Office newsletter ────────────────────
# A marketing-cloud layout: one table cell per section, five items in
# one <h3>, item titles that are themselves <h3> links, and three house
# styles of date ("October 30, 2026 |", "Due 10/12:", "Anytime |").

CEAS_FIXTURE = REPO / "tests" / "fixtures" / "ceas-rso-2026-09-02.html"
CEAS = {
    "name": "ceas-rso",
    "senders": ["rso@com.example.edu"],
    "list_address": "",
    "subject_contains": "",
    "format": "newsletter",
    "mode": "prioritize",
    "follow_links": True,
    "resolve_redirects": True,
    "emoji": ":classical_building:",
    "drop_tags": [],
    "skip_link_text": [
        "View in Browser", "Read more", "Learn more", "Register", "Visit",
        "Manage your email preferences", "Update your contact information",
        "Faculty Resource Center", "Faculty Resource Center Refresh",
        "Research & Innovation Office", "Research Support Office",
        "example.edu/engineering", "All Limited Competition Opportunities",
    ],
    "skip_sections": [],
    "notes": "Weekly college funding newsletter.",
    "scopes": ["funding"],
}


def _ceas_items(lst=None) -> list[dict]:
    return ld.parse_newsletter(CEAS_FIXTURE.read_text(), lst or CEAS)


def test_ceas_newsletter_segments_items_out_of_shared_cells():
    items = _ceas_items()
    by = {i["title"]: i for i in items}
    assert len(items) == 50, len(items)
    assert [i["n"] for i in items] == list(range(1, 51))
    # a whole section in one cell: each item gets its own line and date
    yip = by["Young Investigator Program (YIP)"]
    assert yip["deadline"] == "10/30/2026" and yip["section"] == "Office of Naval Research"
    assert yip["body"].startswith("October 30, 2026 | Young Investigator Program (YIP) The Office of Naval Research")
    assert "Communications and Networking" not in yip["body"]  # the next item
    assert by["Communications and Networking Applied Research Opportunity"]["deadline"] == "11/16/2026"
    # five items wrapped in one <h3>
    ttp = by["NSF Translation to Practice-Explore (TTP-E)"]
    assert ttp["deadline"] == "rolling"
    assert ttp["section"] == "Translational and Commercialization Funding"
    assert ttp["body"].startswith("NSF Translation to Practice-Explore (TTP-E) ($600,000) Rolling Deadline")
    assert "NREL" not in ttp["body"]
    assert by["NREL Innovation & Entrepreneurship Center: Active Funding Opportunities"]["deadline"] == "varies"
    assert by["SBIR/STTR Commercialization Grant Programs"]["deadline"] == "rolling"
    # an <h3> that IS the link is an item under the heading above it
    assert by["Chancellor’s Postdoctoral Fellowship"]["section"] == "Announcements"
    assert by["2026 National Postdoc Appreciation Week"]["section"] == "Upcoming Events"
    # the other house styles
    assert by["Research & Innovation Seed Grant"]["deadline"] == "10/12"
    assert by["National Awards for Young Scientists"]["deadline"] == "11/19"
    assert by["Sloan Research Fellowships"]["deadline"] == "9/15/2026"
    assert by["Bioengineering Partnerships with Industry (U01 Clinical Trial Optional)"]["deadline"] == "1/27/2027"
    assert by["Engineering Biological and Biomedical Systems"]["deadline"] == "anytime"
    assert by["Techstars Accelerator Programs"]["deadline"] == "rolling"
    assert by["Dept of the Army - Materiel Command: ARMY-26-S-0001"]["deadline"] == ""
    # navigation, the sender, contacts and the link inside a Note sentence are not items
    for junk in ("View in Browser", "Read more", "Learn more", "Register", "Visit",
                 "Manage your email preferences", "Update your contact information",
                 "Bill Doe", "fern.zielinski83@example.com", "Faculty Resource Center",
                 "Faculty Resource Center Refresh", "Research & Innovation Office",
                 "Research Support Office", "example.edu/engineering",
                 "Mechanics of Advanced Materials System"):
        assert junk not in by, junk
    assert all(i["tracking_url"].startswith("https://click.com.example.edu/?qs=TRACK") for i in items)


def test_ceas_entry_is_a_prioritize_newsletter_with_funding_rules():
    msg = _graph_msg(mid="ceas-1", sender="rso@com.example.edu", to=("emerson.vasquez27@example.edu",),
                     subject="Research Support Office Funding Opportunities for Sept. 2, 2026")
    entry = ld.build_digest_entry(msg, CEAS, _full(CEAS_FIXTURE.read_text(), html=True), 2200,
                                  resolver=lambda u: u.replace("click.com.example.edu/?qs=", "www.example.edu/engineering/p/"))
    assert entry["kind"] == "newsletter" and entry["items_total"] == 50
    assert entry["mode"] == "prioritize" and entry["follow_links"] is True
    assert entry["items"][0]["links"][0].startswith("https://www.example.edu/engineering/p/TRACK")
    assert entry["parse_warnings"] == []
    assert ld.match_list(msg, [LIST, RIO, CEAS]) is CEAS
    # the same sender with a different subject line is still the list
    assert ld.match_list(_graph_msg(sender="rso@com.example.edu", to=("emerson.vasquez27@example.edu",),
                                    subject="Research Funding Insights & Strategies for June 25, 2026"),
                         [LIST, RIO, CEAS]) is CEAS


def test_lead_dates_in_every_house_style():
    cases = {
        "October 30, 2026 | Young Investigator Program": "10/30/2026",
        "Sept. 15, 2026 | Sloan Research Fellowships": "9/15/2026",
        "Jan. 27, 2027 | NIH: Bioengineering": "1/27/2027",
        "Due 10/12: Research & Innovation Seed Grant": "10/12",
        "Due: 11/19: National Awards for Young Scientists": "11/19",
        "09/09 : Coffee with the VCR": "09/09",
        "Anytime | NSF: Engineering Biological Systems": "anytime",
        "Rolling Application Deadline | Techstars": "rolling",
        "Center: Active Funding Opportunities Deadlines Vary The NREL": "varies",
        "TTP-E ($600,000) Rolling Deadline The NSF": "rolling",
        "September 21 NPAW Kickoff": "",          # no separator: a heading, not a date
        "Materials 12 | not a month": "",
    }
    for text, want in cases.items():
        assert ld._deadline_of(text)[0] == want, (text, ld._deadline_of(text))
    assert ld._has_lead_date("Anytime | NSF") and ld._has_lead_date("Oct. 5, 2026 | NIH")
    assert not ld._has_lead_date("Funding") and not ld._has_lead_date("Learn more")


def test_is_list_mail_reads_the_configured_lists(tmp_path=None):
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="ld-cfg-"))
    cfg = d / "list-digests.json"
    cfg.write_text(json.dumps({"lists": [LIST]}))
    ld._LISTS_CACHE = None
    assert ld.is_list_mail(_graph_msg(), ld.configured_lists(cfg))
    assert not ld.is_list_mail(_graph_msg(sender="rso@com.example.edu", to=("emerson.vasquez27@example.edu",)),
                               ld.configured_lists(cfg))
    # the cache follows the file
    import os, time
    cfg.write_text(json.dumps({"lists": [LIST, CEAS]}))
    os.utime(cfg, (time.time() + 5, time.time() + 5))
    assert ld.is_list_mail(_graph_msg(sender="rso@com.example.edu", to=("emerson.vasquez27@example.edu",)),
                           ld.configured_lists(cfg))
    # a message from nobody on any list
    assert not ld.is_list_mail(_graph_msg(sender="sol.dunmore11@example.edu", to=("emerson.vasquez27@example.edu",)),
                               ld.configured_lists(cfg))
    ld._LISTS_CACHE = None


def test_template_example_is_generic_and_the_cu_example_names_its_lists():
    raw = json.loads(TEMPLATE.read_text())
    names = {l["name"] for l in raw["_example"]["lists"]}
    assert "robotics-worldwide" in names, names
    assert "colorado" not in TEMPLATE.read_text().lower()
    examples = sorted((REPO / "config" / "examples").glob("*/list-digests.json"))
    assert len(examples) == 1, examples
    cu = json.loads(examples[0].read_text())
    cu_names = {l["name"] for l in cu["lists"]}
    assert {"robotics-worldwide", "rio-bulletin", "ceas-rso"} <= cu_names, cu_names
    ceas = next(l for l in cu["lists"] if l["name"] == "ceas-rso")
    assert ceas["senders"] == ["rso@com.cu.edu"] and ceas["mode"] == "prioritize"
    assert "funding" in ceas["scopes"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
