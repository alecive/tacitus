"""Ledger types declare traits, and the rules read sets derived from the
traits (issue #71, step a). This step changes no behaviour: each derived
set must equal the literal set it replaced."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import closure_scrubber as cs  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import project_dashboard as pd  # noqa: E402


def test_each_derived_set_is_the_literal_it_replaced():
    assert set(ledger_mod.REPLY_TYPES) == {"response_needed", "follow_up", "draft_pending"}
    assert set(cs._SCHEDULING_TYPES) == {"scheduling"}
    assert set(cs._DELIVERABLE_TYPES) == {
        "deadline", "task", "reimbursement", "draft_pending", "ae_review_request"}
    assert set(cs._REPLY_SHAPED_TYPES) == {"response_needed", "scheduling", "follow_up"}
    assert set(pd._ON_YOU) == {"response_needed", "deadline", "task", "scheduling"}
    assert set(ledger_mod.WAITING_TYPES) == {"waiting"}


def test_the_types_and_their_meanings_are_unchanged():
    assert {k: v.meaning for k, v in ledger_mod.TYPES.items()} == {
        "response_needed": "a reply to someone is owed",
        "follow_up": "the user owes someone a nudge or a follow-up message",
        "scheduling": "a meeting to arrange or attend",
        "task": "work the user owes: a document, form, purchase, introduction, decision",
        "deadline": "a dated submission, registration or report",
        "waiting": "someone else owes the user a deliverable",
        "reimbursement": "receipts and reimbursement paperwork",
        "dispute": "a billing or refund dispute",
        "draft_pending": "a staged draft waiting for the user to send it",
    }


def test_the_rules_read_the_traits():
    assert ledger_mod.is_waiting({"type": "awaiting"})
    assert not ledger_mod.is_waiting({"type": "task"})
    assert ledger_mod.closes_on_reply({"id": "x", "type": "follow_up"})
    assert not ledger_mod.closes_on_reply({"id": "x", "type": "task"})


# The code that still names a type outside ledger.py. Step (d) turns this
# into a contract test once each one reads a trait or states why not.
LITERAL_SITES = {
    ("preprocessor/closure_scrubber.py", '_etype(entry) == "draft_pending"'): 3,
    ("preprocessor/closure_scrubber.py", '_PLUGIN_OWNED_TYPE_PREFIXES = ("ff_",)'): 1,
    ("preprocessor/closure_scrubber.py", '| {"ae_review_request"}'): 1,
    ("preprocessor/preprocess.py", 'ob.get("type") == "deadline"'): 1,
    ("preprocessor/remote_audit.py", 'o.get("type") == "reimbursement"'): 1,
    ("preprocessor/mail_privacy.py", 'kept_row_type(res) == "scheduling"'): 1,
}


def test_the_literal_type_sites_are_the_known_ones():
    for (path, text), n in LITERAL_SITES.items():
        assert (ROOT / path).read_text().count(text) == n, (path, text)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  {len(tests)} passed, 0 failed")
