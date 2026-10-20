# Slack card goldens

Block Kit captured from the card builders in `ea-chat-bot.py` as they stand
before the ConfirmationCard refactor (plan step 7). The Slack renderer must
reproduce these outputs. `tests/test_slack_card_goldens.py` checks that each
file is well formed; the renderer test compares against the content.

## Regenerate or check

```
cd /home/crh/ea-system
.venv/bin/python3 tests/fixtures/slack_cards/capture.py           # rewrite every golden
.venv/bin/python3 tests/fixtures/slack_cards/capture.py --check   # exit 1 on any difference
```

`capture.py` exits nonzero before it writes anything when a builder is
missing, defined twice, or its `inspect.signature` differs from
`EXPECTED_SIGNATURES`. After step 7 the old builders are gone, so the
script stops working by design; the goldens stay as the reference.

## File layout

One file per case: `<builder without leading underscore>__<case>.json`,
written with `json.dumps(indent=2, sort_keys=True, ensure_ascii=True)`.
Non-ASCII characters (the ellipsis, curly quotes, arrows and dashes in some
card text) appear as `\uXXXX` escapes; `json.loads` returns the original
strings.

| key | content |
| --- | --- |
| `inputs.args` | the call arguments, keyed by parameter name |
| `inputs.stubs` | the stub settings for this case (see below); absent keys use the defaults |
| `text` | the fallback `text` the builder produced (`null` when nothing was posted) |
| `blocks` | the Block Kit list (`[]` when the call carries no blocks) |
| `kwargs` | the kwargs of the call that carries the card: the first `chat_postMessage` or `chat_update` with `blocks`, else the first such call; `{}` for the two builders that return instead of post |
| `calls` | every recorded client call in order, `{"method", "kwargs"}` |
| `result` | `_room_picker` only: the returned dict without `text` and `blocks` (`ok`, `picker`) |

Compare blocks as parsed JSON (key order is sorted in the file, not the
builder's order). Button `value` and `block_id` strings are compared as
strings: their inner JSON key order is the builder's own and must match.

## How each builder runs

Each function is sliced from the source text (from `\ndef <name>(` up to the
next top-level `def`, `@app.action(`, `@app.view(` or `@app.event(`) and
exec'd into one namespace per case, the technique of
`tests/test_staged_button_payloads.py`. Real code exec'd with the builders:
`_format_send_after`, `_repeat_summary`, `_resolve_online_location`,
`_deliver_confirmation`, `_post_dm_breadcrumb`, the `_ONLINE_PLACEHOLDERS`
constant, and the whole "Staged button payloads" block.

Namespace stubs (defaults; a case overrides through `inputs.stubs`):

| name | stub |
| --- | --- |
| `app.client.chat_postMessage` | records kwargs, returns `{"ok": true, "ts": "1700000001.000200"}` |
| `app.client.chat_update` | records kwargs, returns `{"ok": true}` |
| `app.client.chat_getPermalink` | records kwargs, returns `{"permalink": "https://example.slack.com/archives/D_OWNER/p1700000001000200"}`; raises when `permalink_error` is true |
| `_fetch_draft_body` | returns a copy of `stubs.draft` |
| `_scheduling_preflight` | returns `stubs.sched_warning` (default `""`) |
| `_resolve_dm_channel` | returns `stubs.dm` (default `"D_OWNER"`; `null` means no DM; the `no_dm` goldens keep the card render from when such a card posted in the thread, and the bot now posts no card outside the owner's DM) |
| `_stash_button_payload` | returns `stubs.stash_ref` (default `"0123456789abcdef0123456789abcdef"`; `""` forces the inline-payload fallback) |
| `_zoom_link` | returns `"https://example.zoom.us/j/1234567890"` |
| `_note_run_post`, `_note_draft_confirmation`, `_retire_confirmation_message` | no-ops |
| `_load_draft_confirms` | returns `stubs.confirms` |
| `_BULK_PENDING` | `{"ids": stubs.bulk.ids, "dm_channel": stubs.bulk.dm_channel, "timer": null}` |
| `_load_bulk_batches`, `_save_bulk_batches` | an in-memory dict |
| `uuid.uuid4().hex` | `"b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0"` (the bulk batch id is its first 12 characters) |
| `preprocessor.project_store.list_active` | returns `stubs.active` (swapped into `sys.modules` for the `_room_picker` call only) |
| `log` | silent |

Fixed values: thread_ts `1700000000.000100`, EA channel `C_MAIN`, DM
`D_OWNER`, draft id `AAMkDRAFT1`, posted card ts `1700000001.000200`,
schedule `send_after` `2030-04-21T09:00` (in the future, so the past-time
guard does not fire).

## Cases

The exact arguments of every case are in its file under `inputs`; the
source of truth is `cases()` in `capture.py`.

- `build_send_confirmation`: `basic` (to, cc, subject, short body,
  conversation id), `no_cc`, `long_body` (3780 characters, cut at 2800 with
  `\n_(truncated)_`), `empty_body` (renders `_(empty body)_`),
  `sched_warning` (extra section after the header), `gmail` (Gmail draft id
  and account; the preflight is skipped even though the stub would warn).
- `post_schedule_send_confirmation`: `basic` (description and ledger id),
  `sched_warning_long_body` (warning section, truncated body, a 400-character
  description cut to 300 in the button value), `no_dm` (posted in the thread,
  empty description becomes `Scheduled send`).
- `post_bulk_card`: `two_live`, `three_live_one_sent` (four queued ids, one
  not live, so the card counts three).
- `post_event_confirmation`: `basic`, `full` (placeholder location `Zoom`
  plus `online` resolves to the Zoom URL, a 3.3 KB agenda body shown in
  full with the join block after it, a repeat dict), `hold` (no attendees,
  `hold=True`), `no_dm`, `inline_fallback` (the store refuses the payload:
  the approve value would carry the event inline with `body` dropped
  because it passes 2000 characters, so the card has no Create button, and
  the cancel value carries `"payload_ref": ""`).
- `post_event_update_confirmation`: `full` (the owner-side read in
  `stubs.event`: every attendee, one added, one removed, a room kept, the
  location changed; the new time in `stubs.change_when`), `minimal` (no
  attendees, no new time).
- `post_event_delete_confirmation`: `with_time` (the owner organizes a
  series with two attendees: the cancellation line), `no_time` (another
  organizer, and a staged label that does not match the event's title).

The event goldens were rewritten when the event cards began to show the
whole invite text, every attendee and, for a move or a delete, the bot's
own read of the event (`stubs.event`) instead of the staged labels.
- `room_picker`: `query_three_candidates` (three Open buttons, a
  five-option menu where an empty name falls back to the id, Create with the
  query), `thread_three_candidates` (empty query wording), 
  `query_no_candidates_no_active` (no Open buttons, no menu, Create only).
- `replace_buttons_with_status`: `short`, `long` (cut to 2900 characters
  plus `…`, the same string in `text` and in the context block).
- `post_dm_breadcrumb`: `with_permalink`, `permalink_error` (text ends in a
  period instead of a link), `no_dm_ts` (no permalink lookup),
  `skipped_same_channel` (EA channel equals the DM, so nothing posts:
  `calls` is empty).

## Shapes the renderer must reproduce

- `block_id` embeds the thread_ts: `confirm_send_<ts>`,
  `confirm_schedule_send_<ts>`, `confirm_event_<ts>`,
  `confirm_event_update_<ts>` and `confirm_event_delete_<ts>`. The bulk
  card uses
  `bulk_send_<batch id>`. The room picker's `block_id` is
  `room-pick::` plus `json.dumps({"c": channel, "t": thread_ts})`, cut to
  255 characters.
- Button values are `json.dumps` strings with the builder's key order. Send
  and schedule cards give every button the same value. Event cards give the
  approve and cancel buttons different values: approve has the base plus
  `payload_ref` (or the inline `event`); cancel always has
  `thread_ts, ea_channel, payload_ref`. Room picker values use the short
  keys `p`, `n`, `c`, `t`.
  The bulk card's values are the bare 12-character batch id, not JSON.
- Button text uses `plain_text` with `"emoji": true`, except the room
  picker's menu options and placeholder, which have no `emoji` key. Room
  picker labels are cut to 75 characters, option values too.
- `style` is absent on some buttons (Edit, Handled, Sent myself, Keep, One
  by one, Open) rather than set to a default.
- The event delete card puts `danger` on the approve button and no style on
  Keep.
- Cards go to the DM without `thread_ts`; with no DM they post in the EA
  thread with `thread_ts`. Only the breadcrumb (and the room back-link, not
  captured here) passes `unfurl_links=False, unfurl_media=False`.
- `_replace_buttons_with_status` sends one `context` block whose single
  `mrkdwn` element repeats the `text`.
