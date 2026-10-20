# Skill: Weekly Admissions-Inquiry Digest
<!-- scopes: admissions -->

Prospective PhD and MS students write to the user in volume. Many of
these emails are machine-written: a name-dropped paper, a claimed "gap"
in the user's research, then the ask. The admissions filter
(`preprocessor/admissions_filter.py`) moves each one out of the inbox
into the Outlook folder `admissions-inquiries` as it arrives. Once a
week you read that folder's last seven days and write one short post
that tells the user which, if any, deserve their time. You send nothing,
draft nothing, move nothing and add nothing to the ledger. None of these
emails is an obligation.

## 1. Read the input

`~/.ea/state/admissions-digest-latest.json` is the only file you need.
Do not call graph_actions, do not read the briefing JSON, do not read
chat.

- `profile`: the user's role and research blurb. Fit is judged against it.
- `digests[]`: one entry per sender, oldest first. Each has `from`,
  `name`, `domain`, `count`, `follow_ups` and `messages[]`, each with
  `subject`, `received`, `has_attachments`, `kinds` (which applicant
  signals the filter saw) and `text` (the new text, quoted history cut).
- `senders`, `messages`, `window_days`, `errors[]`.
- The `## User rules` block below this skill, when present, holds the
  user's own judgment for the scope `admissions`. It outranks this file.

## 2. Judge each sender

Put a sender on the shortlist only when the mail shows real fit, and
say why in one clause. Real fit means at least one of:

- The work named is the user's actual work, with a detail that only a
  reader of the paper would know, and the student's own prior work
  connects to it concretely: a system they built, a dataset, a paper.
- A referral: a named faculty member or collaborator sent them.
- A strong, checkable record: a first-author paper at a robotics or
  vision venue, a top program, a named lab they worked in.
- They are applying to the user's own program (the institution in
  the profile) this cycle, with a specific plan, not "PhD programs" in
  general.

Treat as templated, and do not shortlist:

- Praise that could fit any robotics lab, or a "gap" the student says
  they found that is generic ("robustness in adverse conditions").
- A long list of the user's topics with no link to the student's work.
- Follow-ups whose only content is the earlier ask.
- One email that names several labs or programs, or says "Dear Professor".

When unsure, leave the sender off the shortlist. A missed borderline
student can still apply through the program; a long shortlist teaches
the user to skip the post. Three or fewer names is normal. Zero is fine.

## 3. Write the post

Your FINAL output is the post, and nothing else. The first characters
are the header. Write in plain short sentences. Use mrkdwn
(single *asterisks*), shortcode emoji, no em-dashes.

```
:mortar_board: *Admissions inquiries — week of <Mon date>*
<N> emails from <S> senders went to the admissions-inquiries folder.

*Worth a look* (<k>)
- *<Name>* (<school or domain>): <subject, shortened>. <One clause on the fit.>

*The rest* (<S-k>): <Name>, <Name>, <Name> ... <one sentence on the pattern, e.g. "mostly templated Fall 2027 asks; 4 sent follow-ups">.
```

- With no shortlist, write `*Worth a look*: none this week.` and keep
  the rest line.
- List every remaining sender by name in the rest line, so the user can
  search the folder for one. Do not describe them one by one.
- If `errors[]` is not empty, end with one line: `:warning: <n> message(s) could not be read in full.`
- Never write a reply, a suggested reply, or a reason to reply.
