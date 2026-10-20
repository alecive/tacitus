### Mailing-list digests and newsletters

The `list-digest` cron agent handles these; you do not triage them in a
briefing or a dispatch, and the preprocessor never turns one into a
needs-response, dropped-thread, deadline or priority item. Each list named
in `~/.ea/config/list-digests.json` is split into items in code (plain-text
digests by their dash rules, HTML newsletters one item per link with
section, due date and the real page behind the tracking redirect), job
posts are dropped, and Claude judges the rest against the user's profile
plus the user's rules. Two modes per list: `filter` posts the few relevant
snippets (a busy community digest); `prioritize` follows every plausible
link with `fetch_url.py`, checks eligibility and deadlines, and posts a
ranked shortlist (a funding newsletter). The install overlay names the
lists this install digests.
Either way the email is then archived, silently when nothing was worth
posting. The skill is mechanism only: every topical judgment (the user's
quoted standing instructions, such as which kinds of items are worth a
summary and which never are, their interests, skip categories,
eligibility, and a calibration log of past runs) is a rule in
`preferences.json` under the scopes `newsletters`, `newsletters:<list>`
and the list's own `scopes` (a funding newsletter adds `funding`); the
preprocessor attaches the selection to each entry. If the user says a
posted item was noise, or a dropped item should have been posted, the fix
is `preferences.py add --kind avoid|prefer --scope newsletters:<list> ...`
or `preferences.py calibrate`, or the list's `notes` in list-digests.json
for a convention of the list itself. Never a skill edit and never a
ledger entry. A digest in the inbox is not an obligation; leave it to the
agent. History of every pass is in
`~/.ea/state/list-digest-history.jsonl`.
