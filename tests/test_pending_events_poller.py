#!/usr/bin/env python3
"""Static wiring checks for the out-of-band calendar sweeper.

ea-chat-bot.py is hyphenated (not importable as a module) and pulls in
heavy deps, so these are text-level assertions on the source, matching
tests/test_bot_wiring.py. The behavioural tests for the claim protocol
itself live in tests/test_draft_chain_e2e.py (Stage 3).

Two properties are pinned here.

The sweeper exists because the dispatch scan only runs on Claude's output
right after a Slack-dispatched run. A cron job, a plugin or a maintenance
shell writing pending-event*.json has no run to be scanned, so the file
would sit on disk and the :lock: buttons would never appear.

The sweeper is a fallback, not a competing reader. Its tick must not
consume a LIVE dispatch's staged event mid-turn, or post the Create
button at the channel root with thread_ts=None and land the approval in
the wrong channel. It claims atomically, only takes files old enough that
no dispatch can still be writing them, and attributes what it claims to a
thread.
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()


def _body(name: str) -> str:
    start = BOT_SRC.index(f"def {name}(")
    end = BOT_SRC.index("\ndef ", start + 1)
    return BOT_SRC[start:end]


def _poller_body() -> str:
    return _body("_pending_events_poller")


def _claim_section() -> str:
    start = BOT_SRC.index("# === staged-action claim protocol")
    end = BOT_SRC.index("# === end staged-action claim protocol", start)
    return BOT_SRC[start:end]


def test_poller_exists():
    assert "def _pending_events_poller(" in BOT_SRC, (
        "the out-of-band calendar sweeper is missing; pending-event*.json "
        "written outside a dispatch would never surface buttons")


def test_poller_started_as_daemon_thread_at_startup():
    assert ("threading.Thread(target=_pending_events_poller, daemon=True)"
            ".start()") in BOT_SRC, (
        "the sweeper must be started in __main__ alongside "
        "_plugin_cards_poller, or it never runs")


def test_poller_covers_all_three_calendar_files():
    section = _claim_section()
    for name in ("pending-event.json", "pending-event-update.json",
                 "pending-event-delete.json"):
        assert name in section, f"the claim protocol does not watch {name}"
    assert "CALENDAR_ACTION_FILES" in _poller_body(), (
        "the sweeper must claim the calendar file set")


def test_pickup_is_an_atomic_claim_not_a_read_then_unlink():
    """read -> (other consumer unlinks) -> post publishes a SECOND live
    Create button for one event. Only an atomic rename makes exactly one
    consumer the owner."""
    section = _claim_section()
    assert "os.rename(src, claim)" in section, (
        "staged files must be claimed by an atomic rename; exists()+"
        "read_text()+unlink() lets two consumers both post buttons")
    assert ".unlink()" not in _poller_body(), (
        "the sweeper must not unlink the staging path by name; it releases "
        "the claim file it renamed")


def test_nothing_deletes_the_staging_path_by_name():
    """Unlinking pending-event.json on success destroys a NEWER staged
    event nobody has read."""
    src = _body("_execute_event_create")
    assert "event_file.unlink()" not in src, (
        "_execute_event_create must not delete pending-event.json: the file "
        "that fed this button was consumed when the button was posted, so "
        "anything at that path now is a different, unread staging write")


def test_sweeper_is_age_gated_against_live_dispatches():
    body = _poller_body()
    assert "EVENT_SWEEP_IDLE_AGE_S" in body and "EVENT_SWEEP_BUSY_AGE_S" in body, (
        "the sweeper must refuse files young enough that a live dispatch "
        "could still be writing them")
    assert "_dispatches_active()" in body, (
        "the age gate must widen while a dispatch is in flight")
    assert "min_age_s=min_age" in body


def test_sweeper_attributes_to_a_thread_before_falling_back_to_the_root():
    body = _poller_body()
    assert "_dispatch_that_could_have_written" in body, (
        "a claimed file must be attributed to the dispatch that could have "
        "written it; posting at the channel root detaches the button from "
        "its conversation")
    assert "or CHANNEL_ID" in body, (
        "a genuinely orphaned file still needs the channel-root fallback")


def test_staged_payload_may_carry_its_thread():
    section = _claim_section()
    assert 'STAGED_ROUTING_KEYS = ("thread_ts", "channel")' in section, (
        "the staged-event schema must accept optional routing keys so the "
        "writer can pin its confirmation to a conversation")


def test_bare_object_and_array_payloads_both_still_work():
    """Backward compatibility with the writer contract in CLAUDE.md."""
    section = _claim_section()
    assert "isinstance(data, list)" in section, (
        "an array must still fan out to one button per event")
    assert "_repeat_fields(ev)" in section, (
        "a repeating series staged out of band must still show its "
        "'Repeats: ...' line on the confirmation block")


def test_sweeper_survives_a_corrupt_file():
    section = _claim_section()
    assert "unparseable" in section, (
        "a corrupt staging file must be logged and consumed, not left to "
        "wedge the loop into erroring every 60s")


def test_sweeper_recovers_claims_abandoned_by_a_crash():
    """A claim held across a bot restart would otherwise sit in the claims
    directory forever — the same silent drop the protocol exists to stop."""
    assert "_recover_abandoned_claims(EVENT_SWEEP_BUSY_AGE_S" in _poller_body()
    assert "_recover_abandoned_claims(EVENT_SWEEP_BUSY_AGE_S" in \
        _body("_plugin_cards_poller")


def test_sweeper_loop_errors_are_visible():
    """The bot runs at INFO, so a loop catch at log.debug leaves a failed
    post with no trace anywhere."""
    body = _poller_body()
    assert "log.warning(f\"event sweeper loop error" in body, (
        "the sweeper's loop-level catch must be at least WARNING")


def test_plugin_cards_poller_shares_the_claim_protocol():
    body = _body("_plugin_cards_poller")
    assert "_claim_event_actions" in body, (
        "the plugin cards poller must claim atomically too, or it races the "
        "dispatch scan for a plugin's pending file")
    assert "PLUGIN_ACTION_FILES" in body, (
        "the sweepers must split the staged file set so they never "
        "contend for the same write")


def test_created_events_are_explicitly_busy():
    """Free/busy visibility is the whole point of writing to Outlook."""
    src = (ROOT / "preprocessor" / "graph_client.py").read_text()
    start = src.index("def create_event(")
    end = src.index("\ndef ", start + 1)
    assert '"showAs": "busy"' in src[start:end], (
        "create_event must set showAs explicitly; a teaching series that "
        "silently landed as Free would defeat the reason for creating it")
