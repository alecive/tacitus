"""mail_tuning: outcome labels, label batches that never share a thread,
rendering with thread context, scoring, the kept-mail rule, evaluation
with a fake model, and the frozen setup the live tick uses. Nothing here
reaches the network, Claude, or the real private dir."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
import mail_privacy as mp  # noqa: E402
import mail_tuning as mt  # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
ANSWER = {"needs_reply": True, "reply_by": "2026-10-09", "ask": "Send the slides.",
          "action": "reply", "urgency": "normal", "sensitive": False, "sensitive_kind": "none",
          "scheduling": "none", "offered_times": [], "people": [], "tone": [],
          "state": "needs-reply", "domain": "research", "brief": "Pat wants the slides."}


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "_breaker", lambda *a: None)
    monkeypatch.setattr(mt, "_profile_line", lambda: "The user is a professor.")
    monkeypatch.setattr(mt, "_user_addresses", lambda: {"emerson.vasquez27@example.edu"})
    cfg = {"endpoint": "https://llm.example.edu/v1", "model": "m-1", "mail": "shadow",
           "scheduling": "off", "shadow_until": "2099-01-01",
           "keep_from_anthropic": {"domains": ["mil"], "addresses": [], "admissions": True}}
    owner_trust.write_owner_file(d / rl.CONFIG_FILE, json.dumps(cfg).encode(), mode=0o600)
    return d


def _row(i, conv=None, received=None, kept="", after=(), thread=()):
    received = received or f"2026-10-0{1 + i % 8}T15:00:00Z"
    conv = conv or f"conv{i}"
    return {"key": f"{conv}|{received}", "id": f"m{i}", "conversation_id": conv,
            "received": received, "from": "pat@example.edu", "from_name": "Pat",
            "to": ["emerson.vasquez27@example.edu"], "cc": [], "subject": f"Subject {i}",
            "has_attachments": False, "body": f"Body {i}. Could you send the slides?",
            "thread": list(thread), "after": list(after), "keep_from_anthropic": kept}


def _label(row, needs_reply=True, sensitive=False):
    return {"key": row["key"], "model": "sonnet", "needs_reply": needs_reply, "reply_by": "",
            "ask": "", "action": "reply" if needs_reply else "none", "urgency": "normal",
            "sensitive": sensitive, "sensitive_kind": "personnel" if sensitive else "none",
            "brief": "b", "why": "w"}


def test_outcome_direct_reply_and_maturity():
    row = _row(1, received="2026-10-05T10:00:00Z",
               after=[{"own": True, "sent": "2026-10-05T12:00:00Z"}])
    assert mt.outcome_for(row, NOW) == {"mature": True, "replied": True, "replied_direct": True,
                                        "automated": False}
    # someone else wrote before the user's reply: a reply in the thread, not to this message
    row["after"] = [{"own": False, "sent": "2026-10-05T11:00:00Z"},
                    {"own": True, "sent": "2026-10-05T12:00:00Z"}]
    assert mt.outcome_for(row, NOW)["replied_direct"] is False
    # a reply after the three-day window does not count
    row["after"] = [{"own": True, "sent": "2026-10-09T12:00:00Z"}]
    assert mt.outcome_for(row, NOW)["replied"] is False
    young = _row(2, received="2026-10-19T10:00:00Z")
    assert mt.outcome_for(young, NOW)["mature"] is False


def test_direct_reply_must_go_to_the_sender_and_skip_automated_mail():
    row = _row(1, received="2026-10-05T10:00:00Z",
               after=[{"own": True, "sent": "2026-10-05T12:00:00Z", "to": ["someone@else.edu"]}])
    assert mt.outcome_for(row, NOW)["replied_direct"] is False
    row["after"][0]["to"] = ["pat@example.edu"]
    assert mt.outcome_for(row, NOW)["replied_direct"] is True
    auto = dict(row, subject="Accepted: Lab meeting")
    assert mt.outcome_for(auto, NOW)["replied_direct"] is False
    assert mt.outcome_for(auto, NOW)["automated"] is True
    assert mt.automated(dict(row, **{"from": "no-reply@service.com"}))


def test_label_batches_never_share_a_thread():
    rows = [_row(i, conv=f"conv{i % 3}", received=f"2026-10-0{1 + i % 8}T1{i % 10}:00:00Z")
            for i in range(12)]
    batches = mt._batches(rows, 10)
    assert sum(len(b) for b in batches) == 12
    for b in batches:
        assert len({r["conversation_id"] for r in b}) == len(b)


def test_render_includes_the_requested_thread():
    row = _row(1, thread=[{"from": "kit.bancroft59@example.edu", "name": "A", "own": False,
                           "sent": "2026-10-01T09:00:00Z", "text": "alpha note"},
                          {"from": "emerson.vasquez27@example.edu", "name": "", "own": True,
                           "sent": "2026-10-01T10:00:00Z", "text": "beta note"}])
    assert "EARLIER IN THIS THREAD" not in mt.render(row, 0)
    two = mt.render(row, 2)
    assert "alpha note" in two and "From the user" in two and "THE MESSAGE TO TRIAGE" in two
    one = mt.render(row, 1)
    assert "beta note" in one and "alpha note" not in one


def test_label_skips_kept_mail(store, monkeypatch):
    rl.append(mt.MESSAGES, _row(1))
    rl.append(mt.MESSAGES, _row(2, kept="listed domain mil"))
    prompts = []

    def fake(prompt, model, timeout=0):
        prompts.append(prompt)
        return json.dumps({"labels": [{"i": 1, "needs_reply": True, "action": "reply"}]})
    monkeypatch.setattr(mt, "run_claude", fake)
    assert mt.label(limit=10)["labelled"] == 1
    assert len(prompts) == 1 and "Subject 2" not in prompts[0]
    assert list(mt.labels()) == [_row(1)["key"]]


def test_score_reads_outcomes_and_labels(store):
    rows = [_row(i) for i in range(4)]
    for r, nr in zip(rows, (True, True, False, False)):
        rl.append(mt.LABELS, _label(r, needs_reply=nr))
    outs = {rows[0]["key"]: {"mature": True, "replied_direct": True},
            rows[1]["key"]: {"mature": True, "replied_direct": True}}
    mt._write(mt.OUTCOMES, json.dumps(outs))
    answers = {r["key"]: dict(ANSWER, needs_reply=nr)
               for r, nr in zip(rows, (True, False, False, True))}
    m = mt.score(rows, answers)
    assert m["nr_recall_outcome"] == 0.5 and m["nr_outcome_n"] == 2
    assert m["nr_recall_reference"] == 1.0
    assert m["nr_f1_teacher"] == 0.5
    assert 0 < m["score"] < 1


def test_disputes_are_settled_by_what_the_user_did(store):
    rows = [_row(i) for i in range(3)]
    # Claude says no reply; the user replied directly; the candidate said reply
    rl.append(mt.LABELS, _label(rows[0], needs_reply=False))
    # Claude says reply; the user never answered; the candidate said no reply
    rl.append(mt.LABELS, _label(rows[1], needs_reply=True))
    # Claude says reply; the user replied to someone else; unsettled
    rl.append(mt.LABELS, _label(rows[2], needs_reply=True))
    outs = {rows[0]["key"]: {"mature": True, "replied": True, "replied_direct": True},
            rows[1]["key"]: {"mature": True, "replied": False, "replied_direct": False},
            rows[2]["key"]: {"mature": True, "replied": True, "replied_direct": False}}
    mt._write(mt.OUTCOMES, json.dumps(outs))
    answers = {rows[0]["key"]: dict(ANSWER, needs_reply=True),
               rows[1]["key"]: dict(ANSWER, needs_reply=False),
               rows[2]["key"]: dict(ANSWER, needs_reply=False)}
    m = mt.score(rows, answers)
    assert (m["disputes_won"], m["disputes_lost"]) == (2, 0)
    # the corrected reference turns the first message into a reply, so the
    # candidate matches it there; Claude's raw labels count it as an error
    assert m["nr_f1_corrected"] > m["nr_f1_teacher"]
    assert mt.outcome_truth({"mature": True, "replied": False, "automated": True}) is None


def test_evaluate_with_a_fake_model_and_kept_outcomes(store):
    dev = [_row(i, received=f"2026-10-0{1 + i % 4}T15:00:00Z") for i in range(6)]
    kept = _row(9, received="2026-10-02T15:00:00Z", kept="listed domain mil")
    for r in dev:
        rl.append(mt.MESSAGES, r)
        rl.append(mt.LABELS, _label(r))
    rl.append(mt.MESSAGES, kept)
    mt._write(mt.OUTCOMES, json.dumps({kept["key"]: {"mature": True, "replied_direct": True}}))
    c = mt.add_candidate("Triage it.", context=0)
    seen = []

    def chat(messages, **kw):
        seen.append(messages[-1]["content"])
        return dict(ANSWER), {"latency_ms": 900}
    m = mt.evaluate(c["id"], "dev", chat=chat, log=lambda *_: None)
    assert m["answered"] == 6 and m["errors"] == 0
    assert m["nr_recall_outcome_kept"] == 1.0 and m["nr_outcome_kept_n"] == 1
    # the kept message ran on the remote model but stays out of the answers file
    assert any("Subject 9" in s for s in seen)
    assert kept["key"] not in mt.answers_of(c["id"], "dev")
    assert mt.board("dev")[0]["candidate"] == c["id"]


def test_freeze_drives_the_live_tick(store, monkeypatch):
    # a small scored sample, so dev holds messages outside it for examples
    monkeypatch.setattr(mt, "DEV_SAMPLE", 2)
    for i in range(12):
        r = _row(i, received=f"2026-10-0{1 + i % 4}T15:00:00Z")
        rl.append(mt.MESSAGES, r)
        rl.append(mt.LABELS, _label(r, needs_reply=bool(i % 2)))
    c = mt.add_candidate("Triage it.", context=2, shots=3)
    res = mt.freeze(c["id"])
    assert res["shots"] == 3 and mt.frozen()["candidate"] == c["id"]

    calls = []

    def chat(messages, **kw):
        calls.append(messages)
        return dict(ANSWER, sensitive=True, sensitive_kind="letter"), {"latency_ms": 700}

    class Graph:
        MAIL_FIELDS = MAIL_FIELDS_FULL = "id"

        def get_message_full(self, mid):
            return {"body": {"contentType": "text", "content": "Could you send the slides?"}}

        def graph_get_all(self, endpoint, params=None):
            return [{"id": "old", "receivedDateTime": "2026-10-08T09:00:00Z",
                     "from": {"emailAddress": {"address": "pat@example.edu", "name": "Pat"}},
                     "body": {"contentType": "text", "content": "Earlier note."}}]

    msg = {"id": "AAMk" + "z" * 40, "conversationId": "convL", "subject": "Slides",
           "receivedDateTime": "2026-10-09T15:00:00Z", "hasAttachments": False,
           "from": {"emailAddress": {"name": "Pat", "address": "pat@example.edu"}},
           "toRecipients": [{"emailAddress": {"address": "emerson.vasquez27@example.edu"}}], "ccRecipients": []}
    rec = mp.process(msg, graph=Graph(), cfg=rl.config(), chat=chat)
    assert rec["status"] == "ok" and rec["setup"] == c["id"]
    assert rec["result"]["sensitivity"] == "confidential"
    assert rec["result"]["confidential_kind"] == "letter"
    assert rec["result"]["deadline"] == "2026-10-09" and rec["result"]["summary"] == "Pat wants the slides."
    sent = calls[0]
    assert len(sent) == 1 + 2 * 3 + 1  # system, three worked examples, the message
    assert "Earlier note." in sent[-1]["content"]  # the thread reached the model


def test_votes_take_the_majority_of_one_n3_request(store):
    c = mt.add_candidate("Triage it.", context=0, votes=3)
    calls = []

    def chat(messages, **kw):
        calls.append(kw)
        objs = [dict(ANSWER, needs_reply=False), dict(ANSWER, needs_reply=True),
                dict(ANSWER, needs_reply=True)]
        return objs[0], {"latency_ms": 900, "objects": objs}
    a, meta = mt.run_candidate(c, _row(1), [], chat=chat)
    assert a["needs_reply"] is True and meta["votes"] == 3
    assert len(calls) == 1 and calls[0]["n"] == 3
