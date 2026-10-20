"""The reply check's code reads (preprocessor/reply_verify.py).

A flagged status claim that one ledger or thread read confirms posts
without Claude's second pass; a contradiction, a claim the code cannot
tie to one row or one thread, and every negative claim still go to it.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preprocessor"))

import reply_verify as rv  # noqa: E402

TODAY = date(2026, 3, 10)
USER = "owner@example.edu"
ROWS = {
    "deadline-grant-report": {"id": "deadline-grant-report", "status": "open",
                              "deadline": "2026-03-21", "conversation_id": "CONV-GRANT"},
    "response-visit-1": {"id": "response-visit-1", "status": "completed",
                         "deadline": "", "conversation_id": "CONV-VISIT"},
}
THREADS = {
    "CONV-GRANT": [{"from": {"address": "jules.whitlock55@example.com"}, "is_draft": False},
                   {"from": {"address": USER}, "is_draft": False}],
    "CONV-VISIT": [{"from": {"address": USER}, "is_draft": False},
                   {"from": {"address": "dana.quill@example.org"}, "is_draft": False}],
}


def _settle(check, reply, tool_text=""):
    reads = []

    def read_thread(c):
        reads.append(c)
        return THREADS.get(c)

    out = rv.settle(check, reply, tool_text,
                    known=lambda ids: {i for i in ids if i in ROWS},
                    get_entry=ROWS.get, read_thread=read_thread,
                    user_email=USER, today=TODAY)
    out["threads_read"] = reads
    return out


def test_open_claim_holds_against_an_open_row():
    s = "The grant report (deadline-grant-report) is still open."
    out = _settle({"ledger": [s], "mail": [], "negative": []}, s)
    assert out["settled"] and out["results"][0]["outcome"] == "held"
    assert out["reads"]["ledger"] == 1


def test_done_claim_against_open_row_goes_to_claude():
    s = "The grant report (deadline-grant-report) was already submitted."
    out = _settle({"ledger": [s], "mail": [], "negative": []}, s)
    assert not out["settled"] and out["results"][0]["outcome"] == "contradicted"


def test_due_date_is_compared_with_the_row():
    good = "deadline-grant-report is due on Mar 21."
    bad = "deadline-grant-report is due on Mar 14."
    assert _settle({"ledger": [good], "mail": [], "negative": []}, good)["settled"]
    out = _settle({"ledger": [bad], "mail": [], "negative": []}, bad)
    assert out["results"][0]["outcome"] == "contradicted"
    weekday = "The deadline is Friday for deadline-grant-report."
    assert _settle({"ledger": [weekday], "mail": [], "negative": []},
                   weekday)["results"][0]["outcome"] == "unresolved"


def test_claim_without_a_single_row_is_unresolved():
    s = "That one is still open."
    reply = s + " See deadline-grant-report and response-visit-1."
    out = _settle({"ledger": [s], "mail": [], "negative": []}, reply)
    assert not out["settled"] and out["results"][0]["outcome"] == "unresolved"


def test_sentence_naming_no_id_is_unresolved():
    s = "It is still open."
    out = _settle({"ledger": [s], "mail": [], "negative": []}, "deadline-grant-report: " + s)
    assert not out["settled"] and out["results"][0]["outcome"] == "unresolved"
    assert out["results"][0]["ref"] == ""
    # An id the turn's commands name does not stand in either.
    out = _settle({"ledger": [s], "mail": [], "negative": []}, s,
                  tool_text="ledger.py update --id deadline-grant-report --last-action x")
    assert out["results"][0]["outcome"] == "unresolved"


def test_due_claim_with_no_date_is_unresolved():
    for s in ("deadline-grant-report is due soon.",
              "The deadline is close for deadline-grant-report."):
        out = _settle({"ledger": [s], "mail": [], "negative": []}, s)
        assert not out["settled"] and out["results"][0]["outcome"] == "unresolved", s


def _settle_rows(rows, s):
    return rv.settle({"ledger": [s], "mail": [], "negative": []}, s, "",
                     known=lambda ids: {i for i in ids if i in rows},
                     get_entry=rows.get, read_thread=lambda c: None,
                     user_email=USER, today=TODAY)


def test_due_date_must_match_the_year():
    rows = {"deadline-grant-report": dict(ROWS["deadline-grant-report"], deadline="2027-03-21")}
    for s in ("deadline-grant-report is due on Mar 21.",
              "deadline-grant-report is due on 2026-03-21."):
        assert _settle_rows(rows, s)["results"][0]["outcome"] == "contradicted", s
    for s in ("deadline-grant-report is due on Mar 21, 2027.",
              "deadline-grant-report is due 3/21/2027.",
              "deadline-grant-report is due on 2027-03-21."):
        assert _settle_rows(rows, s)["settled"], s


def test_today_and_tomorrow_resolve_against_today():
    rows = {"deadline-grant-report": dict(ROWS["deadline-grant-report"], deadline="2026-03-11")}
    assert _settle_rows(rows, "deadline-grant-report is due tomorrow.")["settled"]
    out = _settle_rows(rows, "deadline-grant-report is due today.")
    assert out["results"][0]["outcome"] == "contradicted"


def test_mail_claims_read_the_rows_thread_once():
    they = "The program officer hasn't replied since your note on deadline-grant-report."
    out = _settle({"ledger": [], "mail": [they], "negative": []}, they)
    assert out["settled"] and out["threads_read"] == ["CONV-GRANT"]
    owe = "You still owe Dana a reply on response-visit-1."
    out = _settle({"ledger": [], "mail": [owe], "negative": []}, owe)
    assert out["settled"]
    wrong = "No reply yet from Dana on response-visit-1."
    out = _settle({"ledger": [], "mail": [wrong], "negative": []}, wrong)
    assert out["results"][0]["outcome"] == "contradicted"


def test_mail_claim_naming_no_id_is_unresolved():
    s = "Dana went unanswered."
    out = _settle({"ledger": [], "mail": [s], "negative": []}, s,
                  tool_text="graph_actions.py get-message --conversation-id CONV-VISIT-0000000000")
    assert out["results"][0]["ref"] == ""
    assert out["results"][0]["outcome"] == "unresolved" and out["threads_read"] == []
    # The sole ledger row elsewhere in the reply does not name this sentence.
    out = _settle({"ledger": [], "mail": [s], "negative": []}, s + " See response-visit-1.")
    assert out["results"][0]["outcome"] == "unresolved" and out["threads_read"] == []


def test_mail_claim_naming_its_conversation_reads_it():
    s = "Thread CONV-VISIT-0000000000 went unanswered."
    out = _settle({"ledger": [], "mail": [s], "negative": []}, s,
                  tool_text="graph_actions.py get-message --conversation-id CONV-VISIT-0000000000")
    assert out["results"][0]["ref"] == "CONV-VISIT-0000000000"
    assert out["results"][0]["outcome"] == "unresolved"  # no such thread in the fake


def test_negative_claims_always_go_to_claude():
    out = _settle({"ledger": [], "mail": [], "negative": ["There is no other contact."]}, "x")
    assert not out["settled"] and out["reads"] == {"ledger": 0, "mail": 0, "web": 0}


def test_kinds():
    assert rv.ledger_kind("marked done yesterday") == "done"
    assert rv.ledger_kind("not yet submitted") == "open"
    assert rv.ledger_kind("is overdue") == "due"
    assert rv.mail_kind("you already replied") == "user-sent"
    assert rv.mail_kind("still waiting on them") == "they"
    assert rv.mail_kind("you owe him an answer") == "user"


def test_a_reply_claim_needs_the_user_replying_on_a_thread():
    for s in ("You already replied to Dana on response-visit-1.",
              "You replied yesterday.",
              "You sent Dana the reply on Friday.",
              "You've already sent a reply.",
              "Yes, you responded to Lee."):
        assert rv.mail_kind(s) == "user-sent", s
    # "you sent" in prose, or someone else replying, claims nothing.
    for s in ("Here is the file you sent.",
              "The deck you sent Bo looks fine.",
              "The question you answered is closed.",
              "Lee already replied.",
              "Bo already sent the slides.",
              "If you sent it, the form is complete.",
              "You sent the slides to Bo."):
        assert rv.mail_kind(s) == "", s


def test_a_slash_date_is_a_month_and_day_not_a_fraction():
    assert rv._dates_in("due 3/21/2027", TODAY) == [date(2027, 3, 21)]
    assert rv._dates_in("It is due 10/31.", TODAY)
    assert rv._dates_in("12/25 is the deadline", TODAY)
    for s in ("2/3 of the budget is spent", "a 3/4 majority", "rated 4/5",
              "a ratio of 2/3", "13/40 reviewers", "steps 1/2/3/4"):
        assert rv._dates_in(s, TODAY) == [], s
    rows = {"deadline-grant-report": dict(ROWS["deadline-grant-report"], deadline="2027-03-21")}
    s = "deadline-grant-report is due once 2/3 of the budget is spent."
    assert _settle_rows(rows, s)["results"][0]["outcome"] == "unresolved"


def test_bot_tries_code_before_the_second_pass():
    src = (REPO / "ea-chat-bot.py").read_text()
    i = src.index("code = _reply_check_code(check, response, cr.tool_calls)")
    j = src.index('elif check["bounce"]:', i)
    k = src.index("_reply_check_prompt(check, code)", j)
    assert i < j < k
    assert '"verified_by": "code"' in src and '"verified_by": "claude"' in src
