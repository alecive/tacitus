# Running a lane on Codex

The EA's agent lanes run on Claude Code (`claude -p`, built by
`preprocessor/claude_cli.py`). The same skill can be measured on OpenAI's
Codex CLI (`codex exec`) through `preprocessor/codex_cli.py`, the one
builder for a non-interactive Codex run. Today that covers the briefing
lane of the evals and the release suite; it is a measurement harness, not
a production lane.

## What differs

- **Instructions.** Codex loads no CLAUDE.md. The prompt carries the
  core operating instructions (`prompt_logic.build_claude_md` with
  nothing discovered) ahead of the same framing the Claude run gets
  (`tests/evals/provider.py _claude_prompt`).
- **Tools.** There is no allow list. Codex runs shell commands inside its
  own sandbox: `read-only` reads anywhere and writes nowhere;
  `workspace-write` writes only under the working root (`-C`) and the
  `--add-dir` directories. The eval sets the root to the run's throwaway
  EA_DIR, so the test ledger and a staged event land there and the code
  tree stays read-only. Network is off inside the sandbox.
- **Output.** The final agent message (`-o`) is the result; the JSONL
  event stream gives the commands it ran (`command_execution` items,
  reported as Bash tool calls), the files it changed, and the token usage.
- **Login and model.** Codex uses its own login (`codex login`) and the
  model in `~/.codex/config.toml`; `--model` names a Codex model, and
  `default` keeps the local one. Runs spend that account's quota, not the
  Claude subscription. `EA_CODEX_BIN` names another binary (a wrapper or
  a pinned version); the doctor's `EA_CODEX_CLI` names another probe
  script, which only its tests use.

## Commands

```
python3 preprocessor/codex_cli.py probe        # exit 0 when codex answers a trivial prompt
python3 preprocessor/codex_cli.py version
python3 scripts/ea-doctor.py --probe-codex     # the same, as a doctor line
python3 tests/evals/run_evals.py --mode full --harness codex --fixture regression-009
python3 tests/release/run_release.py --lane briefing --harness codex --tag codex-check
```

A release run on Codex is scored by the same assertions and judge as a
Claude run and compared only with earlier Codex runs, never with a
Claude baseline.

## Not covered

The chat lanes, the scheduling and reply lanes, sessions (`--resume`),
subagents, MCP tools and the plugin tool denials are Claude Code
mechanisms with no Codex twin here. A Codex production lane would need
the tools behind one MCP server first.
