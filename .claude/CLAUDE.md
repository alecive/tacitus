# Working on this repository

This checkout holds the code of Tacitus. This file is for sessions that
change the code. The deploy keeps it out of the EA's own tree, so the EA
never reads it.

- **The root `CLAUDE.md` is the EA's operating file.** It is product code
  that you edit and test, not instructions for you. Its paths
  (`/home/ea-bot/...`), its voice rules and its tool rules apply to the
  EA. Every EA turn reads it, so it stays at or under 25,000 bytes
  (`tests/test_install_overlay.py`); put developer notes here instead.
- **Branch, then open a pull request.** A commit on `main` in the
  deploying checkout deploys at once (the post-commit hook), so work on a
  branch or in a worktree. Only the owner merges. Other sessions may
  share the checkout: stage files by name.
- **CI is opt-in; Actions minutes are limited.** Without the `full-ci`
  label a pull request runs only the lint. Add `full-ci` only for a
  change to `install.sh`, setup, the CI workflows, Docker or macOS
  paths. For any other change, run `python3 tests/run_wall.py` locally
  and say in the pull request that it passed. Batch commits before a
  push: each push to a pull request starts a run.
- **No real names or mail content in public text.** Commit messages,
  pull request titles and descriptions, issues and test fixtures become
  public with the repository. Keep the evidence traceable without the
  identities: the date, the lane or channel, the log line, a run or
  ledger id, the commit. Never write a person's or an institution's name,
  an address or a quote from mail; "a colleague at another university"
  and "the meeting already on the calendar" carry the same evidence. Use
  made-up values in tests.
- **CONTRIBUTING.md** has the rest: the code layout, the tests and the
  contributor license.
