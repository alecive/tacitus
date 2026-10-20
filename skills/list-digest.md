# Skill: Mailing-list Digest Triage
<!-- scopes: none -->
<!-- rules arrive per list inside list-digest-latest.json (list_digest.py selects newsletters:<list> + the list's own scopes) -->

You read one or more mailing-list digests and newsletters, pick out what
the user would want to see, and write a short chat post. Which lists
exist and how each is handled come from `~/.ea/config/list-digests.json`.
What counts as relevant, who the user is, and how high the bar sits come
from the user's own rules in `~/.ea/config/preferences.json`, selected
per list and attached to each entry as `rules`. This skill is the
mechanism. The judgment is the user's, and it outranks anything written
here. Job postings were
already removed in code. The orchestrator posts your text and then
archives the emails. You archive nothing and you send nothing.

## 1. Read the input

`~/.ea/state/list-digest-latest.json` is the only file you need. Do not
call graph_actions, do not read the briefing JSON, do not read the chat.

- `profile`: the user's role, department and research blurb.
- `digests[]`: one entry per email. Each has `items[]` (the candidates),
  `dropped[]` (job posts removed in code, with a reason), `items_total`,
  `parse_warnings`, `kind` (`digest`, `newsletter`, `post`, or `unparsed`
  with `raw_text` holding the whole message), the list's settings
  (`mode`: `filter` or `prioritize`; `follow_links`; `emoji`; `notes`,
  the list's conventions, for example which due dates are internal
  deadlines and which line names a contact), and `rules`.
- `rules` on each entry: the user's judgment, in their words, selected
  from the preference store for the scopes `newsletters`,
  `newsletters:<list>` and the list's own `scopes`.
  - `instructions[]`: standing instructions, quoted, with a date and
    scopes. These outrank everything below.
  - `prefer[]` / `avoid[]`: categories to post and to skip. An `avoid`
    match is final.
  - `facts[]`: the user's interests, career stage and status, which
    decide fit and which awards are open to them.
  - `calibration[]`: how past runs landed and what the user said about
    them. This sets the bar. A run that posted too much is a warning to
    post less; a correction names a category to skip.
- Each item: `title`, `tags`, `from_name`, `body`, `links`. Newsletter
  items also carry `section`, `deadline` (as printed, e.g. `09/09`,
  `10/1 & 10/8`, `rolling`), `stage` (LOI, Pre-Proposal), `is_new`, and
  `links[0]` is already the real page behind the tracking link.

Work through the entries in order. Every entry gets its own header in the
post (section 3), so a run with two emails yields two headers.

## 2. Judge the items

### 2a. `mode: filter`

Post an item only if it matches a `prefer` rule or the user's work as
the `facts` describe it, and no `avoid` rule. Read `calibration` for how
high the bar sits. When unsure, drop: a missed borderline item costs nothing, and a
noisy post teaches the user to ignore the channel. Do not fetch URLs for
a `filter` list; the digest body is the evidence.

### 2b. `mode: prioritize`

These are opportunity newsletters. The user cannot follow every item and
wants a ranked shortlist, then the email gone. Read the entry's `notes`
first.

**Pass 1, by name.** Set aside items whose title alone places them
outside the user's field (the `facts`) or inside an `avoid` rule. Do not
fetch those. Count them by category for the tally.

**Pass 2, follow the links.** For EVERY item still standing, fetch its
page:

```
python3 /home/ea-bot/ea-system/preprocessor/fetch_url.py --url "<links[0]>" --max-chars 2500
```

Read for: what it funds, eligibility (career stage, years since PhD,
citizenship, discipline, institution limits), award size and duration,
the real deadline (internal versus sponsor), whether it is a limited
submission, and what to do next. If the page is empty or errors, keep the
item on the strength of its title and deadline and say the page could not
be read. Budget: at most 20 fetches per email. If more items survive Pass
1, fetch the ones with the nearest deadlines first and list the rest by
title under "Not checked".

**Eligibility.** Apply the `facts` about the user's career stage and
status. Skip awards the user cannot hold and count them as "not eligible"
in the tally. When eligibility turns on a fact the rules do not state,
keep the item if the topic fits and state the condition in one bullet.

**Rank.** First fit with the `prefer` rules and the user's `facts`, then
deadline proximity, then award size and prestige. Something the user could submit
to this cycle ranks above a prestige prize with long odds. Events count
only when they change a funding outcome for the user (a sponsor briefing,
an information session for a program they would apply to, a partnership
workshop): at most two, as one-liners.

Post three to six ranked items. Fewer is fine. If none survive, use the
sentinel in section 3.

## 3. Write the post

Write in the assistant voice: short sentences, active voice, one idea per
sentence, no idioms. mrkdwn: `*bold*` with single asterisks, emoji
shortcodes. Never use an em-dash. The header shortcode is the
entry's `emoji`.

A `filter` entry with at least one relevant item looks like this:

```
:newspaper: *robotics-worldwide digest* (Tue 01 Sep): 1 of 17 items is relevant.

*Winter Drive dataset* (Flo Isenberg, Example University; IEEE RA-L 2026)
• 20K multimodal frames from snowy urban and highway driving.
• 128-beam LiDAR, RGB and thermal cameras, GNSS/IMU.
• 5K annotated LiDAR scans: 3D boxes, per-point semantics, labelled falling snow.
• Joint benchmark for de-snowing, semantic segmentation and object detection.
Paper: https://example.org/papers/winter-drive | Code: https://github.com/example/winter-drive

Dropped 16: 7 job posts, 6 workshop or CFP notices, 2 special issues, 1 press piece.
```

A `prioritize` entry looks like this:

```
:moneybag: *Research Bulletin* (Sep 1): 2 of 44 items are worth your time, ranked.

*1. Faculty Seed Grant* (due 10/12; internal)
• Up to $60K over 18 months, the university's internal seed program.
• New rule this cycle: a team of two or more faculty PIs from the university.
• Information session 09/30.
https://www.example.edu/research/...

*2. NSF SBIR/STTR Pilot Emphasis on Scientific Instrumentation* (due 11/04)
• Translational track for instrumentation; the contact named in the bulletin is Lane Delacroix.
https://www.nsf.gov/...

Also on the radar: NSF town hall with a federal relations firm (09/23); Industry Pathways Workshop (10/1 & 10/8).

Skipped 42: 21 out-of-field programs, 5 not eligible, 1 standing BAA, 10 events, 5 news items.
```

Rules for the body:

- The first line is the header: the entry's `emoji`, the list name in
  bold, the email date, and the count `N of M items`. One header per email.
- One section per posted item. Bold title (numbered in `prioritize` mode),
  then in parentheses the deadline as printed and whether it is internal,
  a Limited Submission or rolling; for a paper, the venue; for a digest
  post, the poster's name and affiliation when stated. Then two to four
  bullet facts: what it is, size, sensors, task, award amount, duration,
  eligibility condition, contact named in a Note. Facts only.
- Copy links verbatim from `links`. Never shorten, retype or invent a URL.
  Prefer the page you fetched (`links[0]`) or the paper, code, data or
  submission link over a homepage. At most three links per item.
- The last line is the tally: how many items were dropped or skipped and
  a count by category. Name the categories the way the `avoid` rules name
  them, so the user can audit the filter against their own rules.
- Do not explain why an item matters to the user beyond one bullet on
  fit. Do not write about archiving, posting, fetching, or what you did.
  Nothing after the tally line.
- No ledger entries and no project-store writes. These items are
  information, not obligations.

If NOTHING in the file is worth posting, your entire output is this one
line, exactly:

```
:no_bell: NOTHING_RELEVANT
```

The orchestrator then archives the emails silently. Do not add a tally,
a greeting or any other text to that line. If `digests` is empty, output
the same sentinel line.

## 4. Output discipline

`claude -p` keeps only your final turn. Read the JSON first, run every
fetch, then write the post as your last message with no tool call after
it. Your first characters must be the shortcode of the first header (or
of the sentinel).
