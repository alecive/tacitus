#!/usr/bin/env python3
"""The admissions-inquiry filter (preprocessor/admissions_filter.py).

Pins what counts as a prospective-student inquiry, what is never moved
whatever it says, how a sweep moves mail (folder created once, a body
fetched once, sent mail read only when something needs judging), how a
message the user drags back is honoured, what the weekly collection
hands the skill, and how preprocess.py and run-agent.sh wire it in.

Usage:
    python3 tests/test_admissions_filter.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

EA_DIR = Path(tempfile.mkdtemp(prefix="ea-test-admissions-"))
os.environ["EA_DIR"] = str(EA_DIR)
for sub in ("config", "state", "logs"):
    (EA_DIR / sub).mkdir(parents=True, exist_ok=True)
(EA_DIR / "config" / "user-profile.json").write_text(json.dumps(
    {"name": "Kit Calloway", "email": "wren.fairbanks84@example.edu",
     "bio_blurb": "Robotics, perception, field autonomy."}))

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import admissions_filter as af  # noqa: E402

NOW = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
USER = "wren.fairbanks84@example.edu"


def _rec(addr, name=""):
    return {"emailAddress": {"address": addr, "name": name}}


def _msg(mid, frm, subject, received="2026-10-01T17:00:00Z", conv=None, to=(USER,), cc=(),
         name=""):
    return {"id": mid, "from": _rec(frm, name), "subject": subject,
            "receivedDateTime": received, "conversationId": conv or f"conv-{mid}",
            "toRecipients": [_rec(a) for a in to], "ccRecipients": [_rec(a) for a in cc],
            "hasAttachments": True, "bodyPreview": ""}


def _ctx(**kw):
    c = {"user": {USER}, "known_addrs": set(), "known_names": set(), "sent_convs": set(),
         "vips": set(), "lists": None, "allowed_senders": set(), "allowed_convs": set()}
    c.update(kw)
    return c


APPLICANT = ("Dear Professor Calloway,\nMy name is Vale Jamison, and I am a recent computer "
             "science graduate. I am planning to apply to PhD programs for Fall 2027 and your "
             "work on field robotics fits my interests. I have attached my CV.\nBest, Vale")
GAP = ("Dear Prof. Calloway,\nI read your radar odometry paper closely and noticed a gap: the "
       "method assumes static scenes. I built a dynamic-object filter for my thesis. I am "
       "currently searching for PhD positions and would be grateful to know whether you are "
       "accepting new students.\nRegards")


# ── classify ──────────────────────────────────────────────────────────

def test_applicant_mail_matches_by_subject_or_by_body():
    for subj in ("Prospective PhD Student (Fall 2027) - Generalist Agile Robotics",
                 "Prospective Ph.D. Student for Fall 2027: Weather-Robust Radar Perception",
                 "[Prospective PhD Student] A. Drew Dalby - 3D Vision",
                 "PhD Vacancy Inquiry - AI System Engineering",
                 "Master Research Opportunity Inquiry",
                 "Fall 2027 PhD - a deployed multi-robot fleet"):
        v = af.classify(_msg("m", "dov.foxworth79@example.com", subj), "Dear Professor,", _ctx())
        assert v["match"], (subj, v)
    # a neutral subject: the body alone carries it
    v = af.classify(_msg("m", "dov.foxworth79@example.com", "Your radar perception research"), GAP, _ctx())
    assert v["match"] and "degree-position" in v["kinds"], v
    v = af.classify(_msg("m", "dov.foxworth79@example.com", "Hello"), APPLICANT, _ctx())
    assert v["match"], v


def test_ordinary_external_mail_does_not_match():
    cases = [
        ("RE: ONR Science of Autonomy Overview",
         "Hey Kit, my PhD student will join the call.\nDana Hargrove, PhD\nRoboticist"),
        ("Invitation to Review for Journal of Field Robotics",
         "Dear Dr. Calloway, would you review this manuscript on graduate-level SLAM?"),
        ("Undergraduate Research Internship - Robot Perception", "I have attached my CV."),
        ("NSF Frontiers in Computing", "The program funds graduate students and postdocs."),
        ("Request for Guidance in Robotics", "I am looking for guidance from faculty members."),
    ]
    for subj, body in cases:
        v = af.classify(_msg("m", "someone@navy.mil", subj), body, _ctx())
        assert not v["match"], (subj, v)


def test_never_moved_whatever_the_text_says():
    subj = "Prospective PhD Student - Fall 2027"
    m = _msg("m", "dov.foxworth79@example.com", subj, conv="c1", name="Finley Carrow")
    assert af.classify(_msg("m", "alice@example.edu", subj), APPLICANT, _ctx())["excluded"] == "internal sender"
    assert af.classify(_msg("m", USER, subj), APPLICANT, _ctx())["excluded"] == "from the user"
    assert af.classify(m, APPLICANT, _ctx(sent_convs={"c1"}))["excluded"] == \
        "the user sent in this conversation"
    assert af.classify(m, APPLICANT, _ctx(known_addrs={"dov.foxworth79@example.com"}))["excluded"] == \
        "the user has written to this sender"
    # the same person from a second address: 'Carrow, Finley' is 'Finley Carrow'
    assert af.classify(m, APPLICANT, _ctx(known_names={af._name_key("Carrow, Finley")}))["excluded"] == \
        "the user has written to someone of this name"
    cc = _msg("m", "dov.foxworth79@example.com", subj, cc=("jo.dunmore84@example.edu",))
    assert af.classify(cc, APPLICANT, _ctx())["excluded"] == "an internal colleague is on the message"
    assert af.classify(m, APPLICANT, _ctx(allowed_senders={"dov.foxworth79@example.com"}))["excluded"] == \
        "sender restored by the user"
    assert af.classify(m, APPLICANT, _ctx(vips={"dov.foxworth79@example.com"}))["excluded"] == "VIP sender"
    thanks = "Thank you for your kind reply. I will apply to the Example University PhD program."
    assert af.classify(m, thanks, _ctx())["excluded"] == "answers something the user wrote"
    referral = ("Hi Kit, I worked with Taylor at Example Labs this summer. Taylor is in his final "
                "undergrad year and interested in pursuing a PhD. Would you chat with him?")
    v = af.classify(_msg("m", "avery.oyelaran8@example.com", "PhD at Example U"), referral, _ctx())
    assert v["excluded"] == "a third party introduces a student", v
    assert af.is_internal("someone@cs.example.edu") and not af.is_internal("someone@example.edu.evil.com")


def test_body_text_cuts_quoted_history_and_html():
    full = {"body": {"contentType": "html", "content":
            "<p>Dear Prof,</p><p>I plan to apply to PhD programs.</p>"
            "<div>From: Kit Calloway<br>Sent: Monday</div><p>old quoted text</p>"}}
    t = af.body_text(full)
    assert "apply to PhD" in t and "old quoted" not in t


# ── sweep ─────────────────────────────────────────────────────────────

class FakeGraph:
    def __init__(self, inbox, sent=(), folders=()):
        self.inbox, self.sent = list(inbox), list(sent)
        self.folders = {f: f"fid-{f}" for f in folders}
        self.bodies, self.moves, self.calls = {}, [], []

    def get_messages(self, folder, days_back=None):
        self.calls.append(("get_messages", folder))
        if folder == "inbox":
            return list(self.inbox)
        if folder == "sentitems":
            return list(self.sent)
        return [m for m in self.moves_in(folder)]

    def moves_in(self, fid):
        return [m for m, dest in self.moves if dest == fid]

    def get_message_full(self, mid):
        self.calls.append(("full", mid))
        return {"body": {"contentType": "text", "content": self.bodies.get(mid, "")}}

    def graph_get(self, endpoint, params=None):
        self.calls.append(("graph_get", endpoint))
        name = af.FOLDER_NAME
        return {"value": [{"id": self.folders[name]}]} if name in self.folders else {"value": []}

    def graph_post(self, endpoint, body):
        self.calls.append(("graph_post", endpoint))
        if endpoint == "/me/mailFolders":
            self.folders[body["displayName"]] = f"fid-{body['displayName']}"
            return {"id": self.folders[body["displayName"]]}
        mid = endpoint.split("/")[3]
        if body["destinationId"] not in self.folders.values():
            raise RuntimeError("404 folder")
        msg = next(m for m in self.inbox if m["id"] == mid)
        self.inbox.remove(msg)
        self.moves.append((msg, body["destinationId"]))
        return {"id": f"new-{mid}"}


def _paths(tag):
    d = EA_DIR / tag
    d.mkdir(exist_ok=True)
    return d / "state.json", d / "moves.jsonl"


def _style_db():
    db = EA_DIR / "style.db"
    if not db.exists():
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE profiles (contact TEXT PRIMARY KEY, display_name TEXT)")
        con.execute("INSERT INTO profiles VALUES ('hollis.yarborough29@example.org', 'Dunmore,Xan')")
        con.commit()
        con.close()
    return db


def test_sweep_moves_inquiries_creates_the_folder_once_and_logs():
    st, lg = _paths("sweep1")
    g = FakeGraph([
        _msg("a", "drew.stavros88@example.org", "Prospective PhD Student (Fall 2027)"),
        _msg("b", "hollis.yarborough29@example.org", "Prospective PhD applicant", name="Xan Dunmore"),
        _msg("c", "parker.jamison23@example.com", "ONR overview"),
        _msg("d", "noor.foxworth47@example.com", "Hello", received="2026-10-01T17:05:00Z"),
    ])
    g.bodies = {"a": APPLICANT, "b": APPLICANT, "c": "Agenda attached.", "d": GAP}
    closed_for = []
    res = af.sweep(graph=g, lists=[], style_db=_style_db(), state_path=st, log_path=lg, now=NOW,
                   closer=lambda conv: closed_for.append(conv) or [f"response-{conv}"])
    assert sorted(m["from"] for m in res["moved"]) == ["drew.stavros88@example.org", "noor.foxworth47@example.com"]
    # the detector rows of each moved conversation are closed
    assert sorted(closed_for) == ["conv-a", "conv-d"]
    assert sorted(c for m in res["moved"] for c in m["ledger_closed"]) == \
        ["response-conv-a", "response-conv-d"]
    assert ("graph_post", "/me/mailFolders") in g.calls
    assert sum(1 for c in g.calls if c == ("graph_post", "/me/mailFolders")) == 1
    # the student the user wrote to is never judged, never moved
    assert ("full", "b") not in g.calls and any(m["id"] == "b" for m in g.inbox)
    state = json.loads(st.read_text())
    assert state["folder_id"] == f"fid-{af.FOLDER_NAME}" and len(state["moved"]) == 2
    log = [json.loads(l) for l in lg.read_text().splitlines()]
    assert [r["event"] for r in log] == ["moved", "moved"]

    # second sweep: nothing new, so no body fetch and no sent-mail read
    g.calls.clear()
    res = af.sweep(graph=g, lists=[], style_db=_style_db(), state_path=st, log_path=lg, now=NOW, closer=lambda c: [])
    assert res["moved"] == [] and not any(c[0] == "full" for c in g.calls)
    assert ("get_messages", "sentitems") not in g.calls


def test_a_message_dragged_back_is_never_moved_again():
    st, lg = _paths("restore")
    g = FakeGraph([_msg("a", "drew.stavros88@example.org", "Prospective PhD Student (Fall 2027)", conv="cv")])
    g.bodies = {"a": APPLICANT}
    af.sweep(graph=g, lists=[], style_db=_style_db(), state_path=st, log_path=lg, now=NOW, closer=lambda c: [])
    moved_msg, _ = g.moves[0]
    g.inbox.append(dict(moved_msg, id="a2"))      # the user drags it back
    g.bodies["a2"] = APPLICANT
    res = af.sweep(graph=g, lists=[], style_db=_style_db(), state_path=st, log_path=lg, now=NOW, closer=lambda c: [])
    assert [r["from"] for r in res["restored"]] == ["drew.stavros88@example.org"] and res["moved"] == []
    state = json.loads(st.read_text())
    assert "drew.stavros88@example.org" in state["allowed_senders"] and "cv" in state["allowed_conversations"]
    # a later follow-up from the same student stays in the inbox
    g.inbox.append(_msg("a3", "drew.stavros88@example.org", "Re: Prospective PhD Student (Fall 2027)",
                        received="2026-10-03T10:00:00Z", conv="cv"))
    g.bodies["a3"] = APPLICANT
    res = af.sweep(graph=g, lists=[], style_db=_style_db(), state_path=st, log_path=lg, now=NOW, closer=lambda c: [])
    assert res["moved"] == [] and any(m["id"] == "a3" for m in g.inbox)
    assert "restored" in lg.read_text()


def test_dry_run_moves_nothing_and_writes_no_state():
    st, lg = _paths("dry")
    g = FakeGraph([_msg("a", "drew.stavros88@example.org", "Prospective PhD Student (Fall 2027)")])
    g.bodies = {"a": APPLICANT}
    res = af.sweep(graph=g, lists=[], style_db=_style_db(), state_path=st, log_path=lg,
                   now=NOW, dry_run=True, closer=lambda c: 1 / 0)
    assert [m["from"] for m in res["would_move"]] == ["drew.stavros88@example.org"]
    assert g.moves == [] and not st.exists() and not lg.exists()


def test_a_stale_folder_id_is_rebuilt():
    st, lg = _paths("stale")
    af.save_state(dict(af._empty_state(), folder_id="fid-deleted"), st)
    g = FakeGraph([_msg("a", "drew.stavros88@example.org", "Prospective PhD Student (Fall 2027)")],
                  folders=(af.FOLDER_NAME,))
    g.bodies = {"a": APPLICANT}
    res = af.sweep(graph=g, lists=[], style_db=_style_db(), state_path=st, log_path=lg, now=NOW, closer=lambda c: [])
    assert len(res["moved"]) == 1
    assert json.loads(st.read_text())["folder_id"] == f"fid-{af.FOLDER_NAME}"


def test_sent_mail_unavailable_moves_nothing():
    st, lg = _paths("nosent")
    g = FakeGraph([_msg("a", "drew.stavros88@example.org", "Prospective PhD Student (Fall 2027)")])
    g.bodies = {"a": APPLICANT}

    def boom(folder, days_back=None):
        if folder == "sentitems":
            raise RuntimeError("503")
        return list(g.inbox)
    g.get_messages = boom
    res = af.sweep(graph=g, lists=[], style_db=_style_db(), state_path=st, log_path=lg, now=NOW, closer=lambda c: [])
    assert res["moved"] == [] and g.moves == [] and res.get("error")


def test_close_auto_rows_closes_only_detector_rows():
    import subprocess as sp
    calls = []

    class R:
        def __init__(self, out="", rc=0):
            self.stdout, self.returncode, self.stderr = out, rc, ""

    def fake_run(argv, **kw):
        calls.append(argv[2:])
        if argv[2] == "search":
            return R(json.dumps({"entries": [{"id": "response-x"}, {"id": "thread-y"},
                                             {"id": "reply-to-quinn"}]}))
        return R()
    orig = sp.run
    sp.run = fake_run
    try:
        assert af.close_auto_rows("conv-1") == ["response-x", "thread-y"]
        assert af.close_auto_rows("") == []
    finally:
        sp.run = orig
    closes = [c for c in calls if c[0] == "close"]
    assert [c[2] for c in closes] == ["response-x", "thread-y"]
    assert all("--skip-archive" in c for c in closes)


class ConvGraph:
    """A mailbox of folders for the filing path."""
    FOLDERS = {"inbox": "f-inbox", "archive": "f-archive", "sentitems": "f-sent",
               "drafts": "f-drafts", "deleteditems": "f-deleted"}

    def __init__(self, msgs, bodies):
        self.msgs, self.bodies, self.moves = msgs, bodies, []
        self.FOLDERS = dict(self.FOLDERS)

    def graph_get_all(self, endpoint, params=None):
        f = params["$filter"]
        if f.startswith("conversationId"):
            cid = f.split("'")[1]
            return [m for m in self.msgs if m["conversationId"] == cid]
        addr = f.split("'")[1]
        return [m for m in self.msgs if m["from"]["emailAddress"]["address"] == addr]

    def graph_get(self, endpoint, params=None):
        if endpoint == "/me/mailFolders":
            fid = self.FOLDERS.get(af.FOLDER_NAME)
            return {"value": [{"id": fid}] if fid else []}
        return {"id": self.FOLDERS[endpoint.rsplit("/", 1)[1]]}

    def graph_post(self, endpoint, body):
        if endpoint == "/me/mailFolders":
            self.FOLDERS[af.FOLDER_NAME] = "f-adm"
            return {"id": "f-adm"}
        mid = endpoint.split("/")[3]
        m = next(x for x in self.msgs if x["id"] == mid)
        m["parentFolderId"] = body["destinationId"]
        self.moves.append(mid)
        return {"id": "n-" + mid}

    def get_message_full(self, mid):
        return {"body": {"contentType": "text", "content": self.bodies.get(mid, "")}}


def _cm(mid, frm, folder, received, subject="Prospective PhD Student (Fall 2027)", conv="cv1"):
    m = _msg(mid, frm, subject, received=received, conv=conv)
    m["parentFolderId"] = folder
    m["isDraft"] = False
    return m


def test_file_conversation_moves_the_applicants_mail_and_nothing_of_the_users():
    st, lg = _paths("file")
    msgs = [_cm("s1", "drew.stavros88@example.org", "f-archive", "2026-09-29T10:00:00Z"),
            _cm("u1", USER, "f-sent", "2026-09-30T10:00:00Z", subject="RE: Prospective PhD"),
            _cm("s2", "drew.stavros88@example.org", "f-inbox", "2026-09-30T12:00:00Z", subject="RE: Prospective PhD"),
            _cm("s3", "drew.stavros88@example.org", "f-deleted", "2026-09-30T13:00:00Z", subject="RE: x"),
            _cm("d1", USER, "f-drafts", "2026-10-01T10:00:00Z", subject="RE: x")]
    msgs[-1]["isDraft"] = True
    g = ConvGraph(msgs, {"s1": APPLICANT})
    res = af.file_conversation("cv1", graph=g, state_path=st, log_path=lg, now=NOW)
    assert res == {"admissions": True, "moved": 2} and sorted(g.moves) == ["s1", "s2"]
    state = json.loads(st.read_text())
    assert all(r["kinds"] == ["filed-after-reply"] for r in state["moved"].values())
    # filing again moves nothing: the mail is already in the folder
    g.moves.clear()
    assert af.file_conversation("cv1", graph=g, state_path=st, log_path=lg, now=NOW)["moved"] == 0


def test_file_conversation_leaves_other_threads_unless_forced():
    st, lg = _paths("file2")
    msgs = [_cm("c1", "parker.jamison23@example.com", "f-inbox", "2026-09-29T10:00:00Z", subject="ONR overview", conv="cv2")]
    g = ConvGraph(msgs, {"c1": "Agenda attached."})
    assert af.file_conversation("cv2", graph=g, state_path=st, log_path=lg, now=NOW) == \
        {"admissions": False, "moved": 0}
    # a referral is not an applicant's own conversation
    ref = [_cm("r1", "avery.oyelaran8@example.com", "f-inbox", "2026-09-29T10:00:00Z", subject="PhD at Example U", conv="cv3")]
    g2 = ConvGraph(ref, {"r1": "Taylor is in his final undergrad year and interested in pursuing a PhD."})
    assert af.file_conversation("cv3", graph=g2, state_path=st, log_path=lg, now=NOW)["admissions"] is False
    assert af.file_conversation("cv2", force=True, graph=g, state_path=st, log_path=lg, now=NOW)["moved"] == 1


def test_file_senders_files_every_conversation_of_the_named_people():
    st, lg = _paths("file3")
    msgs = [_cm("a", "gus.zielinski77@example.org", "f-archive", "2026-09-22T10:00:00Z", conv="k1"),
            _cm("b", "gus.zielinski77@example.org", "f-archive", "2026-09-23T10:00:00Z", conv="k2"),
            _cm("c", "tam.nakashima50@example.org", "f-inbox", "2026-09-23T10:00:00Z", conv="k3")]
    g = ConvGraph(msgs, {})
    assert af.file_senders(["Gus.Zielinski77@example.org"], graph=g, state_path=st, log_path=lg) == \
        {"conversations": 2, "moved": 2}
    assert sorted(g.moves) == ["a", "b"]


# ── weekly collection ─────────────────────────────────────────────────

def test_collect_groups_the_folder_by_sender():
    st, lg = _paths("collect")
    g = FakeGraph([
        _msg("a", "gus.zielinski77@example.org", "Prospective PhD student - field robotics",
             received="2026-09-28T10:00:00Z"),
        _msg("b", "gus.zielinski77@example.org", "Re: Prospective PhD student - field robotics",
             received="2026-09-30T10:00:00Z"),
        _msg("c", "drew.stavros88@example.org", "Prospective PhD Student (Fall 2027)",
             received="2026-09-29T10:00:00Z"),
    ])
    g.bodies = {"a": APPLICANT, "b": "Following up on my email.", "c": APPLICANT}
    af.sweep(graph=g, lists=[], style_db=_style_db(), state_path=st, log_path=lg, now=NOW, closer=lambda c: [])
    out = af.collect(graph=g, state_path=st, now=NOW)
    assert out["mode"] == "admissions-digest" and out["senders"] == 2 and out["messages"] == 3
    first = out["digests"][0]
    assert first["from"] == "gus.zielinski77@example.org" and first["count"] == 2 and first["follow_ups"] == 1
    assert first["messages"][0]["kinds"] and "apply to PhD" in first["messages"][0]["text"]
    assert out["profile"]["bio_blurb"].startswith("Robotics")


# ── the install switch ────────────────────────────────────────────────

def test_off_unless_the_install_turns_it_on(monkeypatch=None):
    import ea_config
    cfg = EA_DIR / "config" / "ea-config.json"
    cfg.unlink(missing_ok=True)
    assert not ea_config.admissions_filter_enabled(cfg) and not af.enabled()
    for val, want in (({"enabled": True}, True), ({"enabled": "yes"}, False),
                      (True, False), ({}, False)):
        cfg.write_text(json.dumps({"admissions_filter": val}))
        assert ea_config.admissions_filter_enabled(cfg) is want, val
    cfg.unlink()
    # off: the hook never touches Graph, the CLI moves nothing
    called = []
    orig = af.sweep
    af.sweep = lambda **kw: called.append(kw) or {"moved": [], "restored": []}
    try:
        af.preprocess_hook()
        assert af.main(["sweep"]) == 0
    finally:
        af.sweep = orig
    assert called == []
    import preprocess
    assert preprocess.run_admissions_digest() == {"mode": "admissions-digest", "digests": [],
                                                  "disabled": True}


# ── wiring ────────────────────────────────────────────────────────────

def test_preprocess_sweeps_before_every_triage_mode():
    src = (REPO / "preprocessor" / "preprocess.py").read_text()
    assert '"admissions-digest": run_admissions_digest' in src
    main = src[src.index('if __name__ == "__main__":'):]
    assert main.index("admissions_filter.preprocess_hook()") < main.index("result = MODES[args.mode]")
    import re
    modes = re.search(r"ADMISSIONS_SWEEP_MODES = \(([^)]*)\)", src).group(1)
    for m in ("full-daily", "scheduling-check", "priority-check", "intake-check", "full-weekly"):
        assert f'"{m}"' in modes, m
    assert '"admissions-digest"' not in modes and '"list-digest"' not in modes


def test_run_agent_wires_the_weekly_digest():
    src = (REPO / "run-agent.sh").read_text()
    assert 'PREPROCESS_MODE="admissions-digest"' in src
    assert 'SKILL_FILE="$SKILL_DIR/admissions-digest.md"' in src
    assert 'admissions-digest) OUTPUT_FILE="$STATE_DIR/admissions-digest-latest.json"' in src
    assert 'log "Done (no admissions inquiries this week)."' in src
    assert "morning-briefing|afternoon-catchup|list-digest|admissions-digest)" in src
    skill = (REPO / "skills" / "admissions-digest.md").read_text()
    assert "<!-- scopes: admissions -->" in skill and "—" not in skill.split("```")[0]
    import preferences
    assert "admissions" in preferences.SCOPES


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
