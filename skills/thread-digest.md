# Skill: Thread Digest
<!-- scopes: mail-triage -->

Summarize noisy group email threads so the user can skip the reply-all
chaos and only act when they're actually needed.

## Problem this solves

The user is on several high-volume group threads (a neighborhood
association, a volunteer committee, sports teams and the like): committee logistics, event RSVPs,
budget votes, game availability, meeting follow-ups. The lists and their
member addresses are in `~/.ea/config/group-senders.json`
(`{"<group name>": [addresses]}`).

These generate 5-30 reply-all messages per thread where the user is CC'd
but rarely needs to act. Reading them all wastes 15-30 minutes per day.

## Trigger

User says in chat:
- "catch me up on the [group] thread"
- "summarize the [group] emails"
- "what happened in the [group] thread"
- "digest my group threads"
- "tldr on [group name]"

Or: the morning/afternoon briefing detects a group thread with >5 new
messages since last run.

## Steps

### 1. Identify the group thread(s)

Known group senders/lists come from `~/.ea/config/group-senders.json`:
```
{
  "<group name>": ["<list address>", "<member address>", ...],
  ...
}
```
A sender not in any list is not a group thread. When the user names a
new group or sender in chat, say so and record it with
`preferences.py add --kind instruction --scope mail-triage`; the owner
moves it into `group-senders.json`.

### 2. Fetch the thread

Search Gmail for the thread:
- Use Gmail MCP: `search_threads` with sender/subject keywords
- Read the full thread: `get_thread` on the thread ID
- If multiple threads match, process each separately

### 3. Produce a structured digest

For each group thread, output:

```
📋 **[Group Name]: [Subject]** (N messages since [date])

**TL;DR:** [1-2 sentence summary of what was discussed/decided]

**Decisions made:**
- [bullet points of any conclusions reached]

**Action items for the user:**
- [ONLY items where the user is specifically asked to do something]
- [or "(none)" if the user isn't called out]

**Action items for others:**
- [Who is doing what, useful context but no action needed from the user]

**Open questions:**
- [Anything still unresolved that the user might want to weigh in on]

**Vibe check:** [one-liner — e.g., "Everyone's aligned", "Getting heated
about the budget", "Low energy, few responses"]
```

### 4. Smart filtering rules

- If a thread has >3 messages and the user hasn't been **directly addressed**
  (name mentioned, question directed at the user), mark as "FYI only"
- If a thread is pure RSVP collection ("I'll be there", "bringing salad"),
  collapse to: "RSVP thread: 12/20 confirmed attending. You haven't responded."
- If thread contains a **deadline or vote** mentioning the user, flag it prominently
- If thread is a **reply-all social conversation** (no action items), summarize
  in one line and suggest archiving

### 5. Post to chat

Post one message per group with digest. If there are action items for the user,
use a separate thread reply with just the action items so the user can respond.

### 6. Offer quick actions

After the digest:
```
Reply with:
- **"mute [group]"**: suppress that group's digests until next week
- **"archive"** — mark all digested threads as read
- **"reply to [group] saying [message]"** — draft a reply
```

### 7. Update state

- Mark digested messages as seen in delta tracking
- Update obligations ledger if any action items were extracted
- Note muted groups in `~/.ea/state/muted-groups.json` with expiry timestamp
