# Skill: RSVP Handler
<!-- scopes: mail-triage, scheduling -->

Detect calendar invitations, event RSVPs, and attendance requests, then
present them as simple one-tap decisions in chat.

## Problem this solves

The user receives many invitations that require a simple yes/no:
- Google Calendar invitations (from unknown senders, forwarded from Outlook)
- Event RSVPs (school events, parties via invitation sites, community potlucks)
- Meeting requests with proposed times
- Forwarded calendar invites from the work Outlook → personal Gmail

Currently each one requires opening the email, reading context, and clicking
through. This skill batches them into chat for rapid-fire responses.

## Trigger

User says in chat:
- "rsvps"
- "pending invitations"
- "what needs an rsvp"
- "invites"

Or: the morning briefing detects unresponded calendar invitations.

Or: the preprocessor detects an email with RSVP/invitation keywords and
the user hasn't responded.

## Steps

### 1. Gather pending invitations

From multiple sources:

**Google Calendar (MCP):**
- List events where `myResponseStatus` is `needsAction` or `tentative`
- Check primary calendar + the family calendar (`calendar_accounts` in user-profile.json)
- Include events from the next 30 days

**Gmail (MCP):**
- Search: `subject:(invitation OR invited OR RSVP OR register) is:unread newer_than:14d`
- Filter to actual invitations (not LinkedIn, not marketing)

**Outlook (preprocessor):**
- Check briefing JSON for calendar invitations with `responseStatus: none`

### 2. Categorize and prioritize

Group invitations by urgency:

**Urgent (event within 7 days):**
```
🔴 **Project Meeting**: Fri Mar 27, 10:00-11:00 AM MT
   From: Jane Doe (jdoe@example.edu)
   Video link included
   → **accept** / **decline** / **tentative**
```

**This month:**
```
🟡 **School spring event**: Sat Apr 12, 10:00 AM
   RSVP via form link
   → **accept** / **decline** / **"rsvp for 4"** (family)
```

**Future:**
```
⚪ **A Holiday Party**: date TBD
   → **accept** / **decline** / **snooze 1 week**
```

### 3. Conflict detection

Before presenting, check all calendars for conflicts:
- If the invitation overlaps with an existing event, flag it:
  ```
  ⚠️ Conflicts with: "Faculty Meeting" 10:00-11:00
  → **accept anyway** / **decline** / **decline with note**
  ```

### 4. Batch presentation in chat

Post a single message with all pending invitations:
```
📬 **Pending RSVPs** (5 items)

🔴 URGENT (this week):
1. Project Meeting: Fri 10:00 AM → accept / decline / tentative
2. Rec league game: Wed 7:50 PM → in / out

🟡 THIS MONTH:
3. Community potluck: Mon Mar 30 6:00 PM → accept / decline
4. School spring event: Apr 12 → rsvp for [#]

⚪ LATER:
5. Summer camp registration — due Apr 15 → open link / snooze

Reply with number + action: e.g., "1 accept" or "2 out" or "all accept"
```

### 5. Process responses

When user replies:
- **"1 accept"** → Accept via Google Calendar MCP (`gcal_respond_to_event`)
  or draft acceptance email for Outlook events
- **"2 out"** → Reply to the league thread: "I'm out, good luck!"
  (match the user's typical reply style for that league: terse, supportive)
- **"3 decline with note"** → Ask for the note, then draft
- **"all accept"** → Accept all pending invitations
- **"4 rsvp for 4"** → Open RSVP form link, or draft email with headcount

### 6. Handle cross-account invitations

When an invitation comes to the work Outlook and needs to be on
the personal Google Calendar too:
- After accepting, offer: "Also add to your personal Google Calendar?"
- If yes, create a matching Google Calendar event via MCP

### 7. Auto-detect recreational league game responses

The user's reply pattern for a league game is consistent:
- "I'm in" or "I'm out" + optional one-liner
- For each team: reply-all to the game thread

When a league game email is detected:
```
⚽ **[Team] vs [Opponent]**: Wed 7:50 PM @ [field]
   → **in** / **out**
```
On "in" → draft reply-all: "In"
On "out" → draft reply-all: "I'm out, good luck!"

### 8. Update state

- Record responses in obligations ledger
- Mark invitation emails as processed in delta tracking
- For recurring events, note acceptance pattern to suggest auto-accept
  in the future
