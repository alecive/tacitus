"""The remote model's briefings are held to the staleness guards the Claude
briefings have. The remote model only picks refs and short lines, so each
guard sits in code (remote_briefing.candidates and check_plan) and each is
pinned here, beside the Claude-path test or eval it mirrors:

  parked, snoozed, follow-up rows     test_briefing_pipeline.test_today_top_skips_*,
                                      eval empty-calm-day (the snoozed Acme item)
  a closed row with a past deadline   eval completed-with-deadline
  a thread he answered earlier        eval already-replied-thread (the
                                      model's call; marked replied_earlier)
  mail to the sender since            test_briefing_pipeline.test_mail_to_the_sender_*
  a row that looks done               test_closure_policy (the briefing's proposals)
  a draft claim found stale           preprocess.reconcile_draft_claims
  a failed mail sync or calendar      run-agent.sh --stale, calendar_fetch_errors
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import remote_briefing as rb  # noqa: E402

from test_remote_briefing import NOW, _briefing  # noqa: E402

TODAY = NOW.date()
PAST = (TODAY - timedelta(days=3)).isoformat()
LATER = (TODAY + timedelta(days=5)).isoformat()
EMPTY = {"get_done_today": [], "flexes": [], "drafts": [], "needs_you": [], "escalate": []}


def _ids(c):
    return {x.get("ledger_id") for x in c.values()}


def _every_ref(c, key="get_done_today", **fields):
    """A model that picks every candidate: what reaches the post is then
    only what code let through."""
    return {**EMPTY, key: [{"ref": r, **fields} for r in c]}


def test_a_parked_snoozed_or_follow_up_row_is_never_a_candidate_nor_overdue():
    b = _briefing()
    b["open_obligations"] += [
        {"id": "row-parked", "description": "Parked Acme intro", "deadline": PAST,
         "follow_up_after": LATER, "type": "task"},
        {"id": "row-snoozed", "description": "Snoozed Acme item", "deadline": PAST,
         "status": "snoozed", "type": "task"},
        {"id": "row-follow", "description": "Follow up with Noor", "deadline": PAST,
         "status": "follow_up", "type": "follow_up"}]
    b["stale_obligations"] = [{"id": "row-snoozed-stale", "description": "Old snoozed form",
                               "status": "snoozed"}]
    c = rb.candidates(b, today=TODAY)
    assert not _ids(c) & {"row-parked", "row-snoozed", "row-follow", "row-snoozed-stale"}
    post = rb.render_morning(b, c, rb.check_plan(_every_ref(c), c, morning=True),
                             now=NOW, drafts=[], escalated=[])
    for word in ("Acme", "Noor", "Old snoozed form"):
        assert word not in post


def test_a_closed_row_with_a_past_deadline_is_never_overdue():
    b = _briefing()
    b["recently_closed"] = [{"id": "row-tam", "description": "Tam outreach",
                             "closed_at": NOW.isoformat(), "conversation_id": "c-tam"}]
    b["pending_rsvps"] = [{"message_id": "m-tam", "conversation_id": "c-tam",
                           "subject": "Tam outreach call", "from": "t@example.edu"}]
    c = rb.candidates(b, today=TODAY)
    assert "m-tam" not in {x.get("message_id") for x in c.values()}
    post = rb.render_morning(b, c, rb.check_plan(_every_ref(c), c, morning=True),
                             now=NOW, drafts=[], escalated=[])
    assert "Tam" not in post


def test_a_message_in_a_thread_he_answered_earlier_reaches_the_model_marked():
    """Jo and Harper both wrote in threads he had answered earlier; a
    reply after their mail would have removed them upstream
    (split_replied_items). Which one asks something new is the model's
    call (the remote eval checks it), so neither is held from a draft, and
    both reach it marked."""
    b = _briefing()
    b["new_needs_response"] = [
        {"message_id": "m-jo", "conversation_id": "c-jo", "subject": "Lab tour",
         "from": "parker.whitlock7@example.edu", "from_name": "Jo",
         "user_already_replied_in_thread": True},
        {"message_id": "m-harper", "conversation_id": "c-harper", "subject": "Travel fee",
         "from": "zev.valentijn27@example.edu", "from_name": "Harper",
         "user_already_replied_in_thread": True}]
    c = rb.candidates(b, today=TODAY)
    items = rb._prompt_items(c)
    assert {i["title"] for i in items if i.get("replied_earlier")} == {"Lab tour", "Travel fee"}
    harper = next(r for r, x in c.items() if x.get("message_id") == "m-harper")
    plan = rb.check_plan({**EMPTY, "drafts": [{"ref": harper, "reply_must_say": "yes"}]},
                         c, morning=True)
    assert [p["ref"] for p in plan["drafts"]] == [harper]


def test_no_draft_to_someone_he_has_written_to_since():
    b = _briefing()
    b["new_needs_response"][0]["wrote_to_since"] = {"at": NOW.isoformat(),
                                                    "subject": "Re: the slides"}
    c = rb.candidates(b, today=TODAY)
    plan = rb.check_plan({**EMPTY, "drafts": [{"ref": "R1", "reply_must_say": "yes"}]},
                         c, morning=True)
    assert plan["drafts"] == []


def test_a_row_that_looks_done_is_not_an_ask():
    b = _briefing()
    b["open_obligations"][0]["close_proposal"] = {"why": "you submitted it", "signal": "x"}
    c = rb.candidates(b, today=TODAY)
    assert "row-nsf" not in _ids(c)


def test_a_draft_claim_found_stale_reads_as_stale():
    b = _briefing()
    b["open_obligations"][2]["last_action"] = (
        "2026-10-09: reply drafted by the remote briefing, awaiting Send. " + "x" * 200
        + " [auto-reconciled-drafts 2026-10-12] the staged draft(s) referenced above are no "
          "longer in Drafts - sent or deleted; treat those notes as history, not pending work.")
    c = rb.candidates(b, today=TODAY)
    [row] = [x for x in c.values() if x.get("ledger_id") == "row-old"]
    assert row["last_action"].startswith("(note: the staged draft(s) referenced above are no "
                                         "longer in Drafts")
    assert len(row["last_action"]) <= 200


def test_a_failed_mail_sync_or_calendar_is_said_in_the_post():
    b = _briefing()
    b["calendar_fetch_errors"] = [{"account": "drew.prescott19@example.com"}]
    c = rb.candidates(b, today=TODAY)
    post = rb.render_morning(b, c, EMPTY, now=NOW, drafts=[], escalated=[], stale=True)
    assert "The mail sync failed this run; this briefing uses older data." in post
    assert "drew.prescott19@example.com failed to load" in post
    after = rb.render_afternoon(b, c, {"since_morning": [], "pending": []}, now=NOW, stale=True)
    assert "this update uses older data" in after
