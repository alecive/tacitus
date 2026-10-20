### Scheduling poll filling (When2Meet, WhenIsGood, Doodle, Timeful)

When the user asks you to fill a scheduling poll:
1. Find the poll URL from the referenced email
2. Read the poll: `python3 /home/ea-bot/ea-system/preprocessor/poll_filler.py read-poll --url <url>`
3. Use the propose-meeting solver: treat the poll as an external attendee
   whose availability is the poll's time grid. The solver checks the user's
   calendar and returns which slots are free vs conflicted.
4. Post conflict summary to chat, ask about standing meeting overbookings
5. Fill: `python3 /home/ea-bot/ea-system/preprocessor/poll_filler.py fill-poll --url <url> --available-file <path-to-json-array>`
   (the filler puts the user's configured name on the poll and refuses any other `--name`)
   (`--available '<inline JSON>'` also works, but the file form avoids
   hand-transcription errors on long slot lists)
6. Report what was filled and any overbookings

Read `skills/fill-poll.md` for the full workflow. The poll filler enforces
a hardcoded URL allowlist (when2meet.com, whenisgood.net, doodle.com,
timeful.app only).
This is ask-first tier: always show the conflict summary before filling.
