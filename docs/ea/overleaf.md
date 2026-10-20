### Overleaf projects (overleaf_tools.py)

You have an Overleaf account of your own. The user invites it to a project in
Overleaf (Share: Editor to write, Viewer to read), and your git token
reaches those projects and no other. You work in a clone in ea-bot space
and push as the EA's own account. The tool is
`python3 /home/ea-bot/ea-system/preprocessor/overleaf_tools.py`; every verb
prints one JSON object. Use it for every git step; raw git is not allowed.
Read and Edit the `.tex` files in the clone directly.

`overleaf_tools.py list` names the clones you hold and their projects.
Clones live under `~/.ea/docs/<project>/overleaf/` (or `~/proposals/`,
`~/letters-of-recommendation/`, `~/.ea/research/`, `~/.ea/letters/`); the
tool refuses any other place.

**Start on a project.** `clone --project <id or editor URL> --dir
/home/ea-bot/.ea/docs/<store-project>/overleaf`. Use the full path. "the
EA's Overleaf account is not a member of this project" means the user has not
invited it yet: ask them to share the project with the EA account.

**Make a change.**
1. `status --dir <clone>`. When `overleaf_commits_missing` is not 0, someone
   edited the project on Overleaf: run `pull --dir <clone>` first.
2. Edit the files with Read and Edit.
3. `diff --dir <clone>` and read it. It shows everything not yet on Overleaf.
4. `commit --dir <clone> --message "<one line>"`. It returns the full
   `commit`; nothing leaves the host.
5. To put it on Overleaf: when `status` shows `overleaf_commits_missing`,
   run `pull --dir <clone>` first (it merges Overleaf's edits and returns
   the new `commit`). Then stage the push: write
   `~/.ea/state/pending-publish.json` with `{"overleaf": "<clone dir>",
   "commit": "<commit>", "thread_ts": "<this thread>"}`. The bot posts a
   :lock: Push card in the user's DM: the project, the files the commit
   changes, and a scan of the added lines for email headers, email
   addresses, phone numbers and message ids. `preview --dir <clone>
   --commit <commit>` shows you the same, read only. Never call `push`: it
   is denied, and it refuses without the card's one-time token. On the
   click the bot pushes exactly that commit and posts the result in the
   thread.
6. Tell the user what changed, the `project_url`, and that the Push button
   is in their DM. Record it: `ledger.py update --id <id> --last-action
   "<date>: staged <what> for Overleaf (commit <short id>)"`.

**When to push.** Stage a push when the user asks for the work on Overleaf,
in this thread or in a standing project instruction. Otherwise commit and
tell them it is ready to push. A push replaces the project's files, and
Overleaf drops comments and tracked changes on text a push rewrites, so on a
project where co-authors comment, stage a push only when the user asks.

**When Overleaf and the clone changed the same lines.** pull stops, leaves
the clone as it was, and names the files. Run `diff --dir <clone>
--overleaf` to see Overleaf's change. When Overleaf's change is plainly an
accident (a stray character) or the user said the clone wins, run `pull --dir
<clone> --prefer local`, then stage the push. When it is real writing,
describe it and ask the user which side keeps those lines; `--prefer
overleaf` keeps Overleaf's. Every other change from both sides is kept
either way. The tool never forces a push.

**When the tool refuses.** A refused preview posts no card; the bot puts
the reason in the thread. A refused push puts it on the card.
- "em-dash on an added line in <file>:<line>": rewrite with a comma, colon
  or parentheses. A LaTeX `---` counts; `--` for a range does not.
- "uncommitted change(s)": run `commit` first.
- "does not build on Overleaf's <branch>", or "not the <id> the card
  showed": someone edited the project after the commit or the card. Run
  `pull`, then stage the new `commit`.
- ".git/config sets <key>": something wrote an unsafe setting into the
  clone. Do not edit it out yourself; report it to the user.
- "Overleaf did not accept the EA's token": the token expired or was
  revoked. Tell the user; the owner re-mints it on the EA account.
- Any other git error: report it with the text. Do not work around it.

The document is in the user's voice (drafting-voice.md). Text for a program
manager or a sponsor stands alone: no internal file names, no notes to
co-authors, no pointers to unpublished internal studies unless the user put
them there.
