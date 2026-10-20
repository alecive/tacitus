# Incidents and measurements behind the operating rules

CLAUDE.md states each rule and stops. This file keeps the dated evidence
that made a rule necessary, so the rule can stay short and the reason
stays on record. CLAUDE.md links here; it does not repeat what is here.

## Rule 7a: pass the source message id

Measured 2026-08-28: ledger rows created without a source message id or a
conversation id scored 0% closure recall at every revision of the closure
scrubber ever shipped. Identical rows that carried the ids scored 100%.
At the time, 45 of 67 open rows had neither id. Every email closure
signal joins on `conversation_id`, so a row without one stays open until
a person closes it. `ledger.py add` now derives the conversation id when
it is given the message id.

## Rule 8a: record the work in the same turn

2026-08-27: one section of a document the user owed was written in full
at 17:05, and the turn ended by asking the user two questions. The
write-back waited for the answers, so `last_action` stayed empty for 15
hours. The 04:00 briefing read the dependent item's stale note and told
the user to write the section that was already on disk.

The failure mode: a turn that finishes real work and ends on a question
feels unfinished, so the ledger write waits for the answer. If the answer
comes the next day, the ledger says nothing happened. The rule: record
the deliverable before the reply, then ask. The same applies to a sibling
entry whose note says the work is blocked or pending: a dependency claim
nobody revised is what the next briefing repeats.

## User-stated closure is a bug

Set 2026-06-01. When the user has to say an item is done, the closure
pipeline missed evidence it should have found (a reply, a calendar event,
a confirmation email). Every such close is logged with
`log_missed_closure.py` to `~/.ea/logs/missed-closures.log`, which is the
backlog for new closure_scrubber signals. Closes the data would have
supported anyway are logged too: they tune how aggressive the scrubber is.
