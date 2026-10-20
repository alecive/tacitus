#!/usr/bin/env python3
"""The closure ratchet: precision AND recall.

A false-positive count alone cannot justify a scrubber change: a
scrubber that closed NOTHING would score perfectly on it. This file
scores the live scrubber against a labeled corpus in BOTH directions
and fails when either regresses.

Two corpora, one scorer (preprocessor/closure_harness.py):

  - tests/fixtures/closure_corpus.json, committed: invented people,
    subjects and ids, written by tests/fixtures/closure_corpus_synthetic.py.
    Each case reproduces the condition one signal tests, in both
    directions. Its floors are below.
  - the owner's real corpus, built by scripts/build_closure_corpus.py from
    the ledger, the logs and the mailbox. It is owner-private and never
    committed (closure_harness.real_corpus_path). When it is present the
    REAL_* floors run against it too; when it is absent that run is
    skipped and says so.

To see the detail behind a failure:

    python3 preprocessor/closure_harness.py score [--real]
    python3 preprocessor/closure_harness.py compare --rev <rev>

Since the closure policy (preprocessor/closure.py), a fired signal either
closes the row (decisive evidence) or proposes the close for the user's
one-click confirm. Precision counts closes only: a false close is the
failure that loses an obligation. Recall counts a should-close case that
was closed or proposed, since a proposal is in front of the user; the
auto-recall floor counts the closes alone and is the number to raise.

FLOORS ARE A RATCHET, NOT A TARGET. They carry no slack on the
load-bearing signals, so one lost true close or one new false close
fails the wall. If a change is a deliberate improvement, re-measure and
raise the floor in the same commit that earns it. Do not lower a floor
to make a red wall green. A new synthetic case is added to the generator,
the fixture regenerated, and the floor it moves re-measured.
"""

import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))
sys.path.insert(0, str(HERE / "fixtures"))

import closure_harness as ch  # noqa: E402
import closure_scrubber as cs  # noqa: E402
import closure_corpus_synthetic as syn  # noqa: E402

CORPUS = HERE / "fixtures" / "closure_corpus.json"

# ── the floors on the committed synthetic corpus ──────────────────────
MIN_PRECISION = 1.0           # 0 false closes
MIN_RECALL = 0.95             # closed or proposed: 42/44
MIN_AUTO_RECALL = 0.52        # closed with no click: 23/44 under the policy
MIN_CASES = 75                # 79 cases
MIN_UNACTIONABLE = 10         # 12 recorded
ID_LESS_SIGNALS = {"sender_reply_match", "project_member_completion",
                   "meeting_response_sent", "identifier_siblings_closed"}
MIN_OUTLOOK_REPLY_RECALL = 1.0        # 25/25, and this signal carries the load
MIN_CALENDAR_FP_HELD = 12             # of 12
MIN_CONFIRMATION_FP_HELD = 9          # of 9
MIN_INBOUND_ACK_FP_HELD = 2           # of 2: out-of-office, a new ask
MIN_MEETING_RESPONSE_FP_HELD = 1      # of 1: the tag-up (signal 9)
MIN_IDENTIFIER_SIBLINGS_FP_HELD = 3   # of 3 (signal 10)
MIN_SENDER_REPLY_FP_HELD = 2          # of 2 (signal 7)
MIN_ID_REPAIRED_RECALL = 1.0          # 2/2
MIN_GMAIL_REPLY_RECALL = 1.0          # 1/1
MIN_GMAIL_OUTREACH_FP_HELD = 2        # of 2
# The id-less hand cases that close, and the signal each must close on.
ORPHAN_SIGNALS = {
    "orphan-syn-harborview-rsvp": "meeting_response_sent",
    "orphan-syn-kestrel-committee": "sender_reply_match",
    "orphan-syn-quarry-loan": "sender_reply_match",
    "orphan-syn-exj-reviews": "identifier_siblings_closed",
}

# ── the floors on the owner-private real corpus, when present ─────────
REAL_MIN_PRECISION = 1.0
REAL_MIN_RECALL = 0.95
REAL_MIN_AUTO_RECALL = 0.68
REAL_COHORT_HELD = {"calendar_event_match_false_positive": 20,
                    "confirmation_email_false_positive": 8,
                    "gmail_outreach_false_positive": 1,
                    "meeting_response_false_positive": 1,
                    "identifier_siblings_false_positive": 1}
REAL_COHORT_RECALL = {"user_replied_outlook_true_positive": 1.0,
                      "gmail_reply_true_positive": 1.0,
                      "closed_sibling_true_positive": 1.0}
REAL_LINKAGE_RECALL = {"id_repaired": 1.0}

_REPORT = {}


def _report():
    """Score once; every test reads the same run."""
    if "rep" not in _REPORT:
        _REPORT["rep"] = ch.score(ch.load_corpus(CORPUS), cs)
    return _REPORT["rep"]


def _cohort(name):
    return _report()["by_cohort"].get(name, {})


def _linkage(name):
    return _report()["by_linkage"].get(name, {})


def _fmt_failures(rep, kind):
    return "; ".join(f"{f['case_id']}: {f['reason'][:60]}"
                     for f in rep["failures"] if f["verdict"] == kind) or "none"


def _held(rep, cohort):
    return (rep["by_cohort"].get(cohort) or {}).get("correct_holds", 0)


# ── the corpus itself ──────────────────────────────────────────────────

def test_corpus_is_present_and_two_sided():
    doc = ch.load_corpus(CORPUS)
    labels = {c["label"] for c in doc["cases"]}
    assert labels == {"should_close", "must_not_close"}, labels
    assert len(doc["cases"]) >= MIN_CASES, len(doc["cases"])
    # Ballast gives the rarity test a realistic document frequency. Below
    # _RARE_MIN_CORPUS it is skipped entirely, so a thin fixture would
    # score a code path production never takes.
    assert len(doc["ballast"]) >= cs._RARE_MIN_CORPUS, len(doc["ballast"])


def test_committed_corpus_is_the_generators_output():
    """The fixture is generated, never edited by hand: a stale copy would
    score cases the generator no longer describes."""
    assert CORPUS.read_text() == syn.render(), (
        "tests/fixtures/closure_corpus.json is stale: run "
        "python3 tests/fixtures/closure_corpus_synthetic.py")


def test_committed_corpus_is_synthetic():
    doc = ch.load_corpus(CORPUS)
    assert doc["built_by"] == "tests/fixtures/closure_corpus_synthetic.py"
    assert {c["world_fidelity"] for c in doc["cases"]} == {"synthetic"}


def test_corpus_carries_no_raw_outlook_ids():
    """Outlook conversation and message ids are 100+ char mailbox-scoped
    base64; the signals only compare them for equality, so the fixture
    uses handles."""
    blob = json.dumps(ch.load_corpus(CORPUS))
    assert "AAQkAD" not in blob, "raw Outlook conversationId in the fixture"
    assert "AAMkAD" not in blob, "raw Outlook message id in the fixture"


def test_every_signal_is_exercised_in_both_directions():
    """Each signal the harness can reach has a case it must close and a
    case it must hold. Signal 8 reads the project store, which the
    sandbox does not seed."""
    doc = ch.load_corpus(CORPUS)
    sides = {}
    for c in doc["cases"]:
        sides.setdefault(c["signal_under_test"], set()).add(c["label"])
    for sig in ("user_replied_outlook", "user_replied_gmail", "inbound_ack",
                "confirmation_email", "calendar_event_match", "closed_sibling",
                "sender_reply_match", "meeting_response_sent",
                "identifier_siblings_closed"):
        assert sides.get(sig) == {"should_close", "must_not_close"}, (sig, sides.get(sig))
    kinds = {k for c in doc["cases"] for k, v in c["world"].items() if v}
    assert kinds == {"sent", "inbox", "events", "closed_conversations",
                     "gmail_sent", "gmail_threads", "closed_entries"}, kinds


# ── the two floors ─────────────────────────────────────────────────────

def test_precision_floor():
    rep = _report()
    p = rep["overall"]["precision"]
    assert p is not None and p >= MIN_PRECISION, (
        f"precision {p} < {MIN_PRECISION}. New false closes: "
        f"{_fmt_failures(rep, 'false_close')}")


def test_recall_floor():
    rep = _report()
    r = rep["overall"]["recall"]
    assert r is not None and r >= MIN_RECALL, (
        f"recall {r} < {MIN_RECALL}. Obligations that finished and did not "
        f"close: {_fmt_failures(rep, 'missed_close')}")


def test_auto_recall_floor():
    """The closes that need no click. Raise it with `closes_on` on rows and
    `--ledger-id` on drafts, never by making a signal decisive that the
    corpus shows can be wrong."""
    rep = _report()
    r = rep["overall"]["auto_recall"]
    assert r is not None and r >= MIN_AUTO_RECALL, (
        f"auto-recall {r} < {MIN_AUTO_RECALL}. Now proposals: "
        f"{[x['case_id'] for x in rep['proposals'] if x['verdict'] == 'proposed_close']}")


def test_no_must_not_close_case_is_even_proposed():
    """A false proposal loses nothing, but it is noise on the user's card."""
    rep = _report()
    assert rep["overall"]["false_proposals"] == 0, [
        x["case_id"] for x in rep["proposals"] if x["verdict"] == "false_proposal"]


def test_outlook_reply_signal_still_closes_everything():
    """Signal 1 carries most closures. It is the load-bearing signal, so
    the floor is 100%."""
    c = _cohort("user_replied_outlook_true_positive")
    assert c and c["recall"] >= MIN_OUTLOOK_REPLY_RECALL, c


# ── the precision cases, one cohort each ──────────────────────────────

def test_calendar_false_closures_stay_suppressed():
    c = _cohort("calendar_event_match_false_positive")
    assert c["correct_holds"] >= MIN_CALENDAR_FP_HELD, (
        f"{c['false_closes']} calendar false closures; held "
        f"{c['correct_holds']} of {c['n']}")


def test_banner_and_generic_vocabulary_stay_suppressed():
    """A confirmation system's mail must not close unrelated entries on
    the external-sender banner (['caution', 'email', 'external']) or on
    registration vocabulary."""
    c = _cohort("confirmation_email_false_positive")
    assert c["correct_holds"] >= MIN_CONFIRMATION_FP_HELD, c


def test_inbound_acks_that_finish_nothing_stay_suppressed():
    c = _cohort("inbound_ack_false_positive")
    assert c and c["correct_holds"] >= MIN_INBOUND_ACK_FP_HELD, c


def test_gmail_outreach_false_closure_stays_suppressed():
    """A thread the user started must never feed the reply index. In
    Gmail the thread id equals the first message id, so the user's own
    outreach otherwise reads as a reply and closes the entry it
    opened."""
    c = _cohort("gmail_outreach_false_positive")
    assert c and c["correct_holds"] >= MIN_GMAIL_OUTREACH_FP_HELD, c


def test_one_threads_evidence_closes_only_what_it_answers():
    """A reply older than the row's reopen, the user's nudge on a wait,
    a forward that omits the correspondent and a task swept with its
    thread's reply row all stay open; a detector row whose sibling closed
    still goes with it."""
    rep = _report()
    held = {r["case_id"] for r in rep.get("results", [])
            if r.get("verdict") == "correct_hold"}
    for cid in ("fp-user_replied_outlook-reopened-tidepool-whitepaper",
                "fp-user_replied_outlook-waiting-orchard-agenda",
                "fp-user_replied_outlook-forward-only",
                "fp-closed_sibling-task-from-thread"):
        assert cid in held, (cid, _fmt_failures(rep, "false_close"))
    c = _cohort("closed_sibling_true_positive")
    assert c and c["recall"] == 1.0, c


def test_gmail_reply_signal_still_closes():
    """The other direction of the outreach guard: a genuine reply on a
    thread someone ELSE started still closes. Without this case, deleting
    the gmail signal outright would pass the FP test above."""
    c = _cohort("gmail_reply_true_positive")
    assert c and c["recall"] >= MIN_GMAIL_REPLY_RECALL, c


def test_cross_thread_signals_still_fire():
    """Signal 4 and signal 5 each still reach the case they exist for, so
    a change that silences either one entirely fails here."""
    for name in ("confirmation_email_true_positive",
                 "calendar_event_match_true_positive",
                 "inbound_ack_true_positive"):
        c = _cohort(name)
        assert c and c["recall"] == 1.0, (name, c)


def test_person_token_calendar_matches_stay_fixed():
    """The floor on calendar false closures is zero, with no allowed set:
    a person token shared with a group invite is not evidence that the
    obligation was met."""
    rep = _report()
    still_closing = {f["case_id"] for f in rep["failures"]
                     if f["verdict"] == "false_close"}
    assert not still_closing, f"false closures are back: {still_closing}"
    cal = rep["by_cohort"]["calendar_event_match_false_positive"]
    assert cal["correct_holds"] == cal["n"], (
        f"calendar false positives held {cal['correct_holds']}/{cal['n']}, "
        f"expected all")


# ── the recall ceiling: id capture, not the signals ────────────────────

def test_id_repaired_twins_close():
    """Each id_orphan miss has a twin that differs ONLY by carrying the
    conversation_id `ledger.py add` never captured. The twins close. If
    this ever fails, the counterfactual is broken and the id-capture
    finding cannot be read off the numbers."""
    c = _linkage("id_repaired")
    assert c and c["recall"] >= MIN_ID_REPAIRED_RECALL, c


def test_id_orphans_close_only_through_the_id_less_signals():
    """A row carrying neither a conversation_id nor a source_message_id
    is reachable only by the id-less signals: signal 7, signal 9 and
    signal 10. Any orphan that closes must have come through one of
    those; a conversation-joined signal firing on an orphan means the
    fixture or the join is broken."""
    rep = _report()
    orphan_rows = [r for r in rep["results"] if r["linkage"] == "id_orphan"]
    closed = [r for r in orphan_rows if r["verdict"] in ("true_close", "proposed_close")]
    assert len(closed) == len(ORPHAN_SIGNALS), (
        f"expected the {len(ORPHAN_SIGNALS)} id-less hand cases to close or be "
        f"proposed, got {[(r['case_id'], r['fired_signal']) for r in closed]}")
    bad = [r for r in closed if r["fired_signal"] not in ID_LESS_SIGNALS]
    assert not bad, f"an orphan closed through a conversation-joined signal: {bad}"
    repaired = _linkage("id_repaired")
    assert repaired["true_closes"] + repaired["proposed_closes"] == repaired["n"], (
        "the id_repaired twins no longer close; the counterfactual pairing "
        f"is broken: {repaired}")


def test_id_less_hand_cases_close_on_their_own_signal():
    """Each id-less row the mailbox has answered closes, and on the signal
    built for it, not on a lucky overlap somewhere else."""
    by_id = {r["case_id"]: r for r in _report()["results"]}
    for case_id, signal in ORPHAN_SIGNALS.items():
        r = by_id.get(case_id)
        assert r and r["verdict"] in ("true_close", "proposed_close"), (case_id, r)
        assert r["fired_signal"] == signal, (case_id, r["fired_signal"], r["reason"])


def test_id_less_false_positives_stay_suppressed():
    """What loose builds of signals 7, 9 and 10 get wrong, pinned: a
    meeting acceptance that merely shares tokens with an RSVP row, a
    standing pipeline row whose NOTES mention a manuscript id, a sibling
    the scrubber closed itself, a meeting-button reply and a subject that
    shares only the correspondent's name."""
    for name, floor in (("meeting_response_false_positive", MIN_MEETING_RESPONSE_FP_HELD),
                        ("identifier_siblings_false_positive", MIN_IDENTIFIER_SIBLINGS_FP_HELD),
                        ("sender_reply_false_positive", MIN_SENDER_REPLY_FP_HELD)):
        c = _cohort(name)
        assert c and c["correct_holds"] >= floor, (name, c)


# ── observability ──────────────────────────────────────────────────────

def test_vacuous_holds_are_reported_not_hidden():
    """Some must_not_close cases hold under every scored scrubber build:
    the fence shipped with the signal's first committed build. They stay
    in the corpus (closing them would still be wrong) but they must never
    be counted as bugs the current code catches."""
    rep = _report()
    assert rep["vacuous_holds"] >= 1, rep["vacuous_holds"]
    assert rep["discriminating_only"]["n"] < rep["overall"]["n"]


def test_unscored_buckets_are_carried():
    """`unactionable` (the user closed it and no artifact exists that any
    signal could have seen) and `contested` (closed on an ack label about
    the conversation, not the obligation) are recorded and NOT scored.
    Both counts belong in any report of these numbers."""
    doc = ch.load_corpus(CORPUS)
    assert len(doc["unactionable"]) >= MIN_UNACTIONABLE, len(doc["unactionable"])
    assert "contested" in doc


# ── the owner-private real corpus ──────────────────────────────────────

def _real_failures(rep) -> list:
    o = rep["overall"]
    bad = []
    if o["precision"] is None or o["precision"] < REAL_MIN_PRECISION:
        bad.append(f"precision {o['precision']} < {REAL_MIN_PRECISION}: "
                   f"{_fmt_failures(rep, 'false_close')}")
    if o["recall"] is None or o["recall"] < REAL_MIN_RECALL:
        bad.append(f"recall {o['recall']} < {REAL_MIN_RECALL}: "
                   f"{_fmt_failures(rep, 'missed_close')}")
    if o["auto_recall"] is None or o["auto_recall"] < REAL_MIN_AUTO_RECALL:
        bad.append(f"auto-recall {o['auto_recall']} < {REAL_MIN_AUTO_RECALL}")
    if o["false_proposals"]:
        bad.append(f"{o['false_proposals']} false proposal(s)")
    for name, floor in REAL_COHORT_HELD.items():
        if _held(rep, name) < floor:
            bad.append(f"{name}: held {_held(rep, name)} < {floor}")
    for name, floor in REAL_COHORT_RECALL.items():
        r = (rep["by_cohort"].get(name) or {}).get("recall")
        if r is None or r < floor:
            bad.append(f"{name}: recall {r} < {floor}")
    for name, floor in REAL_LINKAGE_RECALL.items():
        r = (rep["by_linkage"].get(name) or {}).get("recall")
        if r is None or r < floor:
            bad.append(f"{name}: recall {r} < {floor}")
    return bad


def test_real_corpus_floors_when_present():
    """The owner's corpus, rebuilt from production history, holds its own
    floors. It is never committed, so on any other machine this run is
    skipped."""
    path = ch.real_corpus_path()
    if path is None:
        print(f"    (real corpus absent: set {ch.REAL_CORPUS_ENV} or build "
              f"one into {ch.default_real_corpus_path()}; skipped)")
        return
    rep = ch.score(ch.load_corpus(path), cs)
    bad = _real_failures(rep)
    assert not bad, "real corpus: " + "; ".join(bad)


def test_the_real_corpus_path_is_never_tracked():
    """The rebuilt corpus holds real subjects and ledger text: the
    in-repo copy is gitignored, and the builder writes outside the repo
    by default."""
    gi = (HERE.parent / ".gitignore").read_text().splitlines()
    assert f"tests/fixtures/{ch.REAL_CORPUS_NAME}" in gi, gi
    assert ch.REPO not in ch.default_real_corpus_path().parents





def _fresh_db():
    """An empty ledger DB in a temp dir, schema applied the way
    test_closure_scrubber.py does it."""
    import sqlite3
    import tempfile
    import ledger as L
    db = Path(tempfile.mkdtemp()) / "t.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(L.SCHEMA)
    # SCHEMA is the original table; columns added later (gmail_thread_id,
    # deadline, ...) live in MIGRATIONS, which _get_db replays on every open.
    for m in L.MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    return db



# ── the recall ceiling, attacked at the source ────────────────────────

def test_add_entry_derives_the_conversation_id_from_a_message_id():
    """The exact half of the id-capture fix. A caller that names the message
    should not also have to name the thread: one Graph read gets it, and
    without it the row joins nothing and can never auto-close."""
    import sqlite3
    import ledger as L
    from unittest.mock import patch
    db = _fresh_db()
    with patch.object(L, "_graph_cli",
                      lambda *a, **k: {"conversation_id": "CONV-XYZ"}):
        L.add_entry("e1", "task", "d", source_message_id="MSG-1", db_path=db)
    row = sqlite3.connect(str(db)).execute(
        "SELECT conversation_id FROM ledger WHERE id='e1'").fetchone()
    assert row[0] == "CONV-XYZ", row


def test_add_entry_survives_a_graph_failure():
    """A ledger row without a conversation id is worse than one with it, and
    far better than none. The derivation must never block the add."""
    import sqlite3
    import ledger as L
    from unittest.mock import patch
    db = _fresh_db()
    with patch.object(L, "_graph_cli", lambda *a, **k: {"error": "boom"}):
        assert L.add_entry("e2", "task", "d", source_message_id="MSG-1",
                           db_path=db) is True
    row = sqlite3.connect(str(db)).execute(
        "SELECT conversation_id FROM ledger WHERE id='e2'").fetchone()
    assert row[0] == ""


def test_add_entry_does_not_call_graph_when_the_id_is_already_there():
    import ledger as L
    from unittest.mock import patch
    db = _fresh_db()
    called = []
    with patch.object(L, "_graph_cli", lambda *a, **k: called.append(a) or {}):
        L.add_entry("e3", "task", "d", source_message_id="M",
                    conversation_id="ALREADY", db_path=db)
    assert not called, "spent a Graph call on an id we already had"


def test_an_id_less_row_says_it_can_never_auto_close():
    """It is legitimate to create a row from no email at all ("register for
    X"). It is not legitimate for that to be silent, because the row is then
    invisible to every closure signal forever."""
    import io
    from contextlib import redirect_stderr
    import ledger as L
    db = _fresh_db()
    err = io.StringIO()
    with redirect_stderr(err):
        L.add_entry("e4", "task", "no email behind this", db_path=db)
    assert "can ever auto-close" in err.getvalue(), err.getvalue()


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
    rep = _report()
    print(f"\n  corpus: precision {rep['overall']['precision']} "
          f"recall {rep['overall']['recall']} "
          f"({rep['overall']['n']} cases, "
          f"{rep['vacuous_holds']} vacuous holds)")
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
