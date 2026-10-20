# Fill Scheduling Poll
<!-- scopes: scheduling -->

You are filling a scheduling poll (When2Meet, WhenIsGood, Doodle, or Timeful)
on behalf of the user. This uses the propose-meeting solver to determine
which slots are available.

## Phase 1 — Analyze (first message)

1. **Find the poll URL** from the referenced email. Look for links
   containing when2meet.com, whenisgood.net, doodle.com, or timeful.app.

2. **Read the poll**:
   ```bash
   python3 /home/ea-bot/ea-system/preprocessor/poll_filler.py read-poll --url "<poll_url>"
   ```
   This returns JSON with all available time slots, their IDs, and
   the slot duration.

3. **Run the solver** to determine which slots are free. The poll is
   treated as an external attendee whose availability is the poll's
   entire time grid — ONE command:
   ```bash
   python3 /home/ea-bot/ea-system/preprocessor/scheduling_engine.py propose-meeting \
     --duration <slot_duration> --num-options 999 \
     --attendees '[{"email":"poll@placeholder","name":"<event name>","type":"external",
       "availability":<poll slots as [{day,start,end}]>}]'
   ```

   The solver checks the user's full merged calendar (Outlook + Google),
   applies all preferences (working hours, lunch, no-meeting days,
   buffers), and returns which poll slots are free.

4. **Categorize the results** using the token layer as ground truth.
   Every standing series on the user's calendars has an `[auto]` mirror on
   the EA-Managed calendar whose token level declares its movability,
   and the solver output's `calendar_preferences.tokens` lists them all
   for the poll's date range:

   - **Free slots** (solver returned them): mark available
   - **Hard conflicts**: any slot overlapping a `p0` or `nm` token
     (hand-placed or `[auto]`: teaching, group and department meetings,
     defenses, holds), plus flights, exams, and one-off external
     meetings the solver excluded. Mark unavailable, do NOT offer as
     overbookable, and do not ask. A `p0` someone promoted by hand is a
     statement, and no poll outranks it.
   - **Overbookable** (`p1 [auto]` mirrors, and conditional_options
     from the movable-1:1 relaxation for one-off 1:1s): 1:1s and
     recurring syncs the user can move. ASK which to overbook.

   **High-stakes polls get a different default.** When the poll is for
   a hard-to-schedule room (a dean, a dissertation defense, a sponsor,
   a large committee), scarce mutual availability beats protecting a
   movable 1:1. Lead the conflict summary with a recommendation to
   offer the overbookable slots, named per slot ("offering this means
   moving Uma"), and let the user trim, instead of a neutral list. The
   ask-first gate still applies; the framing changes, the tier does
   not. Signals a poll is high-stakes: the requester or attendees
   include a dean, chair, program officer, or sponsor; the email says a
   defense, review, or visit; or many participants with few open slots.

5. **Post the conflict summary** to chat:
   ```
   :calendar: *{Platform}: {event_name}*
   {N} slots across {date range}

   :white_check_mark: *{X} slots free* — will mark available
   :warning: *{Y} standing meetings overlap* — your call:
     • {Day} {Time} — _{event subject}_
     • {Day} {Time} — _{event subject}_
   :x: *{Z} hard conflicts* — will mark unavailable:
     • {Day} {Time} — _{event subject}_

   Which standing meetings can I overbook? Or say "overbook all" / "keep all"
   ```

   If there are NO standing meeting conflicts (only hard + free), skip
   the question and proceed directly to Phase 2.

## Phase 2 — Fill (after user replies, or if no conflicts to ask about)

1. **Parse the user's reply**:
   - "overbook all" / "book over everything" → include all standing meetings
   - "keep all" / "don't overbook" → exclude all standing meetings
   - Specific mentions → include only those

2. **Compute final available slot IDs**:
   - All free slots from solver output (map ranked options back to poll slot_ids)
   - Plus approved overbooking slots (from conditional_options)
   - Minus standing meetings the user said to keep

3. **Get the user's display name** from `~/.ea/config/user-profile.json`
   (the `name` field).

4. **Fill the poll** — use `--available-file`, NOT `--available`:

   **Write the slot list to a file first** via the `Write` tool (not
   shell `>` redirection, which is blocked by the sandbox). Pick a
   descriptive path under `/home/ea-bot/.ea/state/`:
   ```
   /home/ea-bot/.ea/state/poll-slots-<short-event-label>.json
   ```
   The file contents must be a single JSON array of slot_ids, e.g.:
   ```
   ["1777474800", "1777475700", "1777476600", ...]
   ```

   Then fill with `--available-file`:
   ```bash
   python3 /home/ea-bot/ea-system/preprocessor/poll_filler.py fill-poll \
     --url "<poll_url>" \
     --available-file /home/ea-bot/.ea/state/poll-slots-<label>.json
   ```
   The name on the poll is the user's configured display name; the
   filler adds it and refuses any other `--name`.

   **Why `--available-file` is required for multi-slot fills.** On
   2026-04-21 the agent hand-built a 20-element `--available` JSON
   array inline from the solver's output and silently dropped one
   slot (1:15 PM). The fill CLI was given 20 IDs and correctly wrote
   20 server-side — but the missing slot never made it into the
   input in the first place. Writing the solver's slot list to a
   file and passing the path eliminates that transcription step
   entirely. The inline `--available` form still exists for tiny
   test fills (≤5 slots) but argparse rejects passing both flags.

   **Do NOT build the slot list by hand-copying from the solver's
   textual output.** Extract slot_ids programmatically — e.g., write
   the solver's JSON output to a file, Read it back, filter/map
   slot_ids, and Write the final list. The slot file is your
   contract with fill-poll: if the file is right, `_when2meet_fill`'s
   server-side verification guarantees the save matches the file
   exactly (HTTP 200 + verified UserID membership in AvailableAtSlot).

5. **Post the report**:
   ```
   :white_check_mark: *Filled {event_name}* — {filled}/{total} slots marked available

   Overbooked (you approved):
     • {Day} {Time} ({event subject})

   Want me to email {organizer name} with a note?
   ```

6. If the user says yes to the follow-up email, draft it via the
   standard `create-reply` pathway using the original email's message ID.

## Notes

- The poll URL is validated against a hardcoded allowlist before any
  browser launches. Only when2meet.com, whenisgood.net, doodle.com,
  and timeful.app (including betterwhen2meet.com) are permitted.
- For When2Meet: slot_ids are Unix timestamps. The poll auto-saves.
- For WhenIsGood: slot_ids are DOM identifiers. Submit button is clicked.
- For Doodle: slot_ids are option indices. Save button is clicked.
- For Timeful: slot_ids are UTC ISO timestamps. Uses REST API directly
  (no browser needed). Submitted as guest with the user's name/email.
- Screenshots are saved to `~/.ea/exports/` for the user's reference
  (not applicable for Timeful, which uses the API).
