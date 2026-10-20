# TODO: LLM Backend Abstraction

## Motivation

The EA system currently invokes Claude via `claude -p` subprocesses
(Claude Code CLI). This ties the system to the CLI's tool sandbox, MCP
integration, and process isolation model. Abstracting the LLM layer
would allow swapping backends (local LLM, Anthropic API, other
providers) for cost, latency, or independence reasons.

## Current LLM invocations

There are 4 distinct invocation types, each with different requirements:

| Type | Model | Input tokens | Output tokens | Latency | Tools |
|---|---|---|---|---|---|
| **Decomposition** | Haiku | ~2-3K | ~200 | 12-27s | None (pure JSON) |
| **Mono execution** | Sonnet | 38K-544K (median ~200K) | 200-5K | 8-108s | Bash, Read/Write/Edit, MCP |
| **Step execution** | Sonnet | 450-500K | 4-6K | 100-130s | Same as mono |
| **Briefing generation** | Sonnet | ~20-30K | ~5-10K | ~300s | Read/Write/Edit only |

Token counts from production logs (2026-04-07). The `cache` column in
logs (e.g., `213027cache`) shows prompt caching is critical — 90%+ of
input tokens are cached across turns.

### Decomposition (`ea-chat-bot.py:1210`)

- Runs Haiku via `subprocess.run` with `--output-format json`
- No tools, no MCP — pure text-in, JSON-out
- System prompt is ~1500 chars, user message appended
- Timeout: 90s
- **Easiest to swap.** Any 7-8B model or cheap API endpoint can handle this.

### Mono execution (`ea-chat-bot.py:3801`)

- Runs Sonnet via `_run_claude_streaming` with full tool access
- Prompt includes: CLAUDE.md (conditional), user profile, briefing
  extract, scheduling context, skill instructions, thread history
- 8-14 tool turns per invocation (sequential: LLM → tool → LLM → ...)
- Tools: Bash (graph_actions.py, ledger.py, scheduling_engine.py, etc.),
  Read/Write/Edit, MCP (Gmail, Google Calendar, Slack read)
- Timeout: 600s
- **Hardest to swap.** Requires reimplementing the agentic tool loop.

### Step execution (`ea-chat-bot.py:1606`)

- Same as mono but scoped to a single step from a decomposed plan
- Runs in parallel via ThreadPoolExecutor (up to 10 concurrent)
- Per-step unique files for parallel safety (reply-body, step-output)
- Timeout: 420s

### Briefing generation

The **morning briefing** (`preprocessor/preprocess.py:run_full_daily`) is
pure Python — no LLM. It calls the Graph API, reads the SQLite
obligations ledger, and writes `briefing-latest.json` directly. No
abstraction needed here.

The **context-export briefing** (`ea-chat-bot.py:3408`) uses an LLM:
- Runs Sonnet with Read/Write/Edit only (no Bash, no MCP)
- Phase 1 (Python): collects emails, attachments, and ledger entries
  from the SQLite DB into a manifest JSON
- Phase 2 (LLM): reads the manifest + ledger data, writes a briefing
  markdown file
- Single-shot with fallback if Claude doesn't produce output
- Timeout: 300s
- **Moderate difficulty.** Needs a basic tool loop for file I/O.

## What Claude Code CLI provides for free

Swapping to a raw API or local model means reimplementing:

1. **Agentic tool loop** — Parse tool_use blocks from model output,
   execute tools, feed tool_result back, repeat until the model stops
   calling tools. Claude Code handles all of this including parallel
   tool calls and error recovery.

2. **MCP client** — Gmail, Google Calendar, and Slack are connected via
   MCP servers configured in Claude Code. Alternative: replace with
   direct API calls (google-api-python-client, slack_sdk, msgraph).

3. **Sandbox/permission guards** — The `--allowedTools` flag restricts
   which tools the model can call. Bash commands are allowlisted by
   pattern. Write/Edit have the "read before write" safety check.
   Output redirection is blocked. Without this, the model could
   execute arbitrary commands.

4. **Process isolation** — `sudo -u ea-bot` runs the subprocess as a
   restricted Unix user. The LLM cannot access the owner's home
   directory, modify source code, or write to config files.

5. **Prompt caching** — Claude Code automatically caches the prompt
   prefix across turns within an invocation. With 200K+ token prompts
   and 8-14 turns, this saves ~90% of input token costs.

## Option comparison

### A. Local LLM (e.g., Llama 70B on resident GPU)

**Pros:**
- Zero per-token cost (amortized hardware)
- No network latency, no rate limits
- Full data privacy

**Cons:**
- **Context window is the blocker.** Mono/step prompts hit 200-500K
  tokens. Most local models max at 8-32K usable context. Llama 3's
  128K exists but quality degrades past 32K. Would require aggressive
  prompt summarization or RAG.
- **No prompt caching.** Every tool turn re-encodes the full context.
  With 14 turns × 200K tokens, that's 2.8M tokens processed per
  invocation vs. ~200K with caching.
- **Generation speed.** 70B at ~40 tok/s on one A100. A 3K output
  takes 75s. Multiply by multi-turn overhead.
- **Tool use quality.** Frontier models are significantly better at
  structured tool calling, instruction following, and judgment
  (triage, tone matching, name accuracy). Errors here are visible
  to external contacts.
- **GPU cost.** A100 cloud: ~$1-2/hr. Owned hardware: $0/hr marginal
  but $10-20K capital.

**Feasible for:** Decomposition only (2-3K context, no tools, simple
JSON output). A quantized 8B model on a consumer GPU would handle this.

### B. Anthropic API (claude-sonnet-4-6 / claude-haiku-4-5)

**Pros:**
- Same model quality as current
- Prompt caching available (90% input cost reduction)
- Direct control over the agentic loop (custom retry, batching,
  observability)
- No subprocess startup overhead (~2-5s saved per invocation)
- Can use cheaper models (Haiku) for simple tasks

**Cons:**
- Must build the tool execution loop (or use Claude Agent SDK)
- Must implement MCP client or replace with direct API calls
- Must implement sandbox/permission guards yourself
- Separate billing (~$0.09 per typical mono invocation with caching;
  current spend is ~$3-10/day from Pro/Max plan allocation)

**Cost estimate (Sonnet 4.6 API, with prompt caching):**
- Input: $3/M tokens, Cached: $0.30/M, Output: $15/M
- Typical mono: 200K cached + 10K fresh input + 3K output = $0.108
- Typical decomposition (Haiku): 3K input + 200 output = $0.001
- ~20-40 invocations/day → $2-5/day

**Feasible for:** All invocation types. The Claude Agent SDK
(`claude_agent_sdk`) provides the agentic loop out of the box.

### C. Current approach (claude -p subprocess)

**Pros:**
- Everything works today
- Tool sandbox, MCP, process isolation, streaming — all free
- Prompt caching is automatic
- Cost comes from Pro/Max plan (predictable monthly)

**Cons:**
- 2-5s subprocess startup overhead per invocation
- Tied to Claude Code CLI availability and behavior
- Less control over the agentic loop (can't customize tool batching,
  retry logic, or model routing per-turn)
- Log truncation at 150 chars makes debugging harder

**Current cost from production logs (2026-04-07):**
- Per invocation: $0.05-$0.39 (median ~$0.17)
- Decomposition (Haiku): ~$0.01
- Simple ledger queries: ~$0.05
- Complex multi-step: ~$0.35-$0.40

## Recommended migration path

### Phase 1: Decomposition → cheap endpoint (low risk, high value)

Move the Haiku decomposition call to either:
- A local 8B model (if GPU available), or
- Anthropic API with Haiku ($0.001/call)

This is pure JSON-in/JSON-out, no tools, ~2K tokens. Any capable model
handles it. Eliminates one `claude -p` subprocess per multi-step
message.

**Effort:** ~2 hours. Replace `subprocess.run(_sudo_claude_cmd(...))` at
line 1210 with an API call or local inference wrapper.

### Phase 2: Briefing generation → API (moderate risk)

Move briefing generation to Anthropic API with a simple tool loop
(Read/Write/Edit). No MCP needed. The briefing prompt is large (~20-30K)
but manageable.

**Effort:** ~1 day. Build a minimal tool executor for file I/O, wire up
the API call with prompt caching.

### Phase 3: Mono/step execution → API or Agent SDK (high effort)

Replace the core agentic loop. Options:
- **Claude Agent SDK** — provides tool loop, handles multi-turn
  execution. Closest to drop-in replacement.
- **Custom loop** — more control but more code. Need tool dispatching,
  MCP client (or direct API replacements), error handling, timeout
  management, permission guards.

**Effort:** 1-2 weeks. The tool execution, MCP replacement, and
sandbox reimplementation are the bulk of the work.

### Phase 4: Model routing (optimization)

Once abstracted, route by task complexity:
- Simple lookups (ledger search, status check) → Haiku or local 8B
- Draft composition, scheduling → Sonnet
- Complex multi-step with judgment calls → Opus (if needed)

This requires a lightweight classifier at the entry point (the
decomposition step could double as the router).

## Files to modify

| File | Change |
|---|---|
| `ea-chat-bot.py` | Replace `_run_claude_streaming` / `subprocess.run` with abstracted LLM client |
| `ea-chat-bot.py` | Replace `_sudo_claude_cmd` with API call wrapper |
| `ea-chat-bot.py` | Extract tool definitions from `--allowedTools` strings into a tool registry |
| New: `preprocessor/llm_client.py` | Abstract LLM interface (prompt → response with tool calls) |
| New: `preprocessor/tool_executor.py` | Tool dispatch loop (currently inside Claude Code) |

---

# TODO: Persistent Claude Sessions

## Motivation

Currently every Slack message spawns a fresh `claude -p` subprocess. The
full prompt (CLAUDE.md, user profile, briefing extract, thread history,
tool definitions) is re-sent each time. Could a long-lived Claude session
reduce token cost and latency?

## Current per-message cost breakdown (production data, 2026-04-07)

| Metric | Value |
|---|---|
| Invocations/day | ~35 |
| Median input tokens | ~214K |
| Cache hit rate | ~100% (prompt prefix is stable) |
| Cached input cost | $0.30/M tokens |
| Fresh input cost | $3.00/M tokens |
| Median cached input cost | 214K × $0.30/M = **$0.064/message** |
| Median fresh input (per-message delta) | ~5-10K tokens |
| Median output tokens | ~3K |
| Subprocess startup overhead | 2-5s |
| Median mono latency | 56.6s (startup + prompt load + tools) |

The prompt caching already eliminates ~90% of input token cost. The
214K cached tokens cost $0.064 vs. $0.642 uncached — a 10x saving that
is automatic with the current approach.

## What a persistent session would change

### Token savings: minimal

With prompt caching, the per-message cached input cost is already $0.064.
A persistent session would eliminate this entirely (the context is already
in memory), but:

- Cached reads at $0.30/M are already cheap
- The savings per message: ~$0.064 → $0.00 = $0.064 saved
- At 35 messages/day: ~$2.24/day saved on input
- But output tokens ($15/M) and fresh input ($3/M) are unchanged

### Latency savings: 2-5s per invocation

Subprocess startup (fork + load Claude Code + parse config + connect MCP)
takes 2-5s. A persistent session eliminates this. On a 57s median
invocation, that's a 4-9% improvement.

### The context growth problem

This is the dealbreaker for naive persistent sessions. In a stateless
invocation, the context is:

```
[system prompt + CLAUDE.md + briefing + thread history] = ~200K tokens
[tool call 1] → [tool result 1] → ... → [final output]
```

After the invocation ends, all tool results are discarded. The next
message starts fresh with the same ~200K base.

In a persistent session, tool results accumulate monotonically:

```
Message 1: 200K base + 8 tool turns (~20K) = 220K
Message 2: 220K + new message + 8 tool turns = 250K
Message 3: 250K + new message + 8 tool turns = 280K
...
Message 10: ~400K+ and growing
```

At 200K tokens/hr of context growth (conservative), a 1-hour session
would hit 400-600K tokens. Claude's 200K context window would overflow
within 1-2 hours. Even with the Messages API's 1M token window (Opus),
a full day would exhaust it.

**Mitigation options:**
- Summarize and compact prior turns (lossy, complex to implement)
- Only persist the system prompt, drop tool results between messages
  (but then you lose the conversational continuity benefit)
- Hybrid: persist for conversational threads, restart for new topics

## Technical options

### Option 1: Anthropic Messages API with message accumulator

Build a long-lived Python process that holds a `messages` list and
appends each new user message + assistant response. Use prompt caching
with cache breakpoints to avoid re-encoding the system prompt.

**Pros:** Full control over context management, can implement
compaction, can switch models mid-session.

**Cons:** Must build the tool loop, MCP client, and permission system.
This is effectively Phase 3 of the LLM abstraction (see above) plus
session state management.

### Option 2: Claude Code `--resume` flag

Claude Code supports `--resume` to continue a prior session. The bot
could track session IDs and resume within a time window:

```python
if session_age < 3600:  # 1 hour
    cmd = ["claude", "-p", "--resume", session_id, ...]
else:
    cmd = ["claude", "-p", ...]  # fresh session
```

**Pros:** No tool loop reimplementation. Claude Code handles context
management internally. Gets conversational continuity for free.

**Cons:** Context growth problem still applies (Claude Code doesn't
compact between resumes). Session state lives in Claude Code's internal
storage, not under our control. Unclear how `--resume` interacts with
`--allowedTools` changes between invocations.

### Option 3: Claude Code interactive mode as daemon

Run `claude` (interactive, not `-p`) as a background process. Pipe
messages to stdin, read responses from stdout.

**Pros:** True persistent session, no startup overhead.

**Cons:** Fragile process management. If the daemon crashes, all
session state is lost. Must handle stdin/stdout framing. Interactive
mode wasn't designed for programmatic use. Process isolation (`sudo -u
ea-bot`) complicates daemon management.

## Comparison

| | Current (`-p` per message) | Persistent (`--resume`) | Full API session |
|---|---|---|---|
| Startup latency | 2-5s | 0s (after first) | 0s |
| Cached input cost | $0.064/msg | $0/msg (in context) | $0/msg |
| Context growth | None (reset each time) | Unbounded | Managed (can compact) |
| Max session length | Unlimited | ~1-2 hrs before overflow | Configurable |
| Tool loop | Free (Claude Code) | Free (Claude Code) | Must build |
| Conversational continuity | None | Full | Full |
| Implementation effort | Done | ~4 hours | 1-2 weeks (= Phase 3) |

## Recommendation

**`--resume` is the pragmatic middle ground.** It gives conversational
continuity (the model remembers what it just did for the user) and
eliminates startup overhead, without requiring the full API migration.

**Suggested approach:**
1. Track session IDs per Slack channel in `~/.ea/state/sessions.json`
2. Resume if last interaction was <30 minutes ago
3. Start fresh if >30 minutes or if the session errors out
4. Monitor context usage and force-restart if approaching limits

**Token budget estimate with 30-min resume window:**
- Average 3-5 messages per conversational burst
- Context grows from 200K → ~280K over 5 messages
- Still well within the 200K window (cached prefix doesn't count
  against the context limit in the same way)
- Fall back to fresh session if context approaches limits

This is independent of the LLM backend abstraction — it can be done
now with the current `claude -p` approach and carried forward if/when
the backend is swapped.

# TODO: Intake reads a forward as a reply

`detect_needs_response` in `preprocess.py` skips any conversation the
user has sent mail in (`conv in sent_convs`) unless a newer inbound
exists. A forward of a request to third parties therefore stops the
intake from minting a row for that request, the same hole the scrubber's
`user_replied_outlook` had until `_forward_only_reason` (a send whose
recipients omit the correspondent is not a reply). The intake needs the
same recipient test; it is a separate change with its own fixtures
because it changes which rows are created, not which rows close.

# Comment style (standing rule)

A comment states what the code does and the non-obvious constraint it
satisfies, in the present tense. No dates, no quoted messages, no
incident narration, no audit ticket numbers, no cross-references to
other incidents. The story of why belongs in the commit message, which
`git log -p` and `git blame` already tie to the line, or in a companion
doc under `docs/`. Tests keep an incident in a docstring only as a
one-sentence statement of the invariant they pin.

The file-by-file audit of the accumulated comments is done (every
source, script and test file, behaviour pinned by the wall). Keep it
that way: the check below should stay near zero, and a new dated
comment in a review is a defect.

```bash
grep -nE "#.*\b20[0-9]{2}-[0-9]{2}\b|audit B[0-9]" preprocessor/*.py ea-chat-bot.py run-agent.sh scripts/*.py | grep -v "fixture\|example"
```

