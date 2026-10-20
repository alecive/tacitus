# Skill: Calendar Bridge

Handle calendar invitations that arrive on one account but need to be
reflected on another, and manage the user's multi-account calendar chaos.

## Problem this solves

The user has three calendar accounts:
1. **Outlook (work address)**: the university work calendar (Graph API)
2. The personal Google Calendar (primary): the first address in
   `calendar_accounts` of `user-profile.json`
3. The shared family Google Calendar: `google_family` in
   `scheduling-preferences.json`

Calendar invitations frequently arrive on the wrong account:
- **Outlook → Gmail**: User manually forwards work invites to personal Gmail
  (observed: project meetings, coffee chats forwarded as FW:)
- **Gmail → Outlook**: External invitations (recruiters, advisory contacts) that
  should block the work calendar
- **Unknown sender invitations**: Google shows "from an unknown sender" for
  invitations from people the user hasn't interacted with on that account

This manual forwarding is tedious and error-prone — missed forwards mean
double-bookings.

## Trigger

User says in chat:
- "sync calendars"
- "bridge [event] to [account]"
- "add [event] to outlook"
- "add [event] to google"
- "cross-post [event]"

Or: the preprocessor detects a new event on one calendar with no
corresponding hold on the other.

Or: an "FW: Invitation:" email is detected (user's current manual workflow).

## Steps

### 1. Detect calendar gaps

Compare events across all three calendars for the next 14 days:

**Outlook events** (from preprocessor `todays_events` / briefing):
- For each Outlook event, check if there's a matching event on primary
  Google Calendar (by time overlap, not by exact title match)

**Google Calendar events** (MCP):
- For each Google Calendar event with external attendees, check if
  there's a corresponding Outlook block

### 2. Present gaps

```
🔄 **Calendar Sync Gaps** (next 14 days)

**On Outlook but NOT on Google:**
1. Faculty Meeting — Wed 10:00-11:00
2. Project Meeting: Fri 10:00-11:00
3. Office Hours — Mon/Wed 2:00-3:00 (recurring)
→ **"bridge all"** or **"bridge 1,2"** to create Google holds

**On Google but NOT on Outlook:**
4. Rec league game: Wed 7:50 PM (personal, probably fine)
5. Recruiter interview: Thu 1:45-5:30 PM ← should block Outlook!
→ **"bridge 5"** to create Outlook hold

**Unknown sender invitations (not yet accepted):**
6. Project meeting from an external organizer: needs accept on Gmail
→ **"accept 6"** to accept and bridge
```

### 3. Create bridge events

When user says "bridge [N]":
- For Outlook → Google: Create a Google Calendar event via MCP
  - Title: "[Original title]" or "Hold: [title]" (user preference)
  - Time: exact match
  - Description: "Bridged from Outlook. Original: [link/details]"
  - Calendar: primary (the personal Gmail account)
  - Set as "busy" so availability checks work

- For Google → Outlook: Create an Outlook event via Graph API
  ```bash
  python3 ~/ea-system/preprocessor/graph_actions.py create-event \
    --subject "Hold: [title]" --start "YYYY-MM-DDTHH:MM" \
    --end "YYYY-MM-DDTHH:MM" --body "Bridged from Google Calendar"
  ```

### 4. Handle forwarded invitation emails

When the preprocessor or email scan detects:
- Subject starting with "FW: Invitation:" or "FW: Updated invitation:"
- From: the user's work address (`email` in user-profile.json) to their
  personal Gmail (`gmail_accounts`)

This is the user's current manual bridge workflow. Help by:
1. Parsing the invitation details from the forwarded email
2. Checking if the event already exists on Google Calendar
3. If not, creating it: "I see you forwarded the project meeting invite.
   Created it on your Google Calendar."
4. Suggest: "I can do this automatically in the future. Say **'auto-bridge on'**."

### 5. Auto-bridge mode

If enabled (`~/.ea/state/auto-bridge.json`):
- During each preprocessor run, compare Outlook and Google events
- For any new Outlook event with ≥2 attendees (i.e., a real meeting,
  not a personal reminder), auto-create a Google Calendar hold
- For any new Google Calendar event during work hours (9-5 weekdays),
  auto-create an Outlook hold
- Post to chat: "🔄 Auto-bridged 3 events to Google Calendar"
- Exclusions: all-day events, personal reminders, recreational games

### 6. Recurring event handling

For recurring events (like weekly project meetings):
- Detect the recurrence pattern
- Create a matching recurring event on the target calendar
- Don't create individual instances — use recurrence rules

### 7. Cleanup

- When a bridged event is cancelled on the source calendar, offer to
  delete the bridge copy
- When a bridged event is moved, offer to update the bridge copy
- Track bridge relationships in `~/.ea/state/bridged-events.json`:
  ```json
  {
    "outlook_id": "AAMk...",
    "google_id": "abc123",
    "title": "Faculty Meeting",
    "created": "2026-03-26T10:00:00",
    "direction": "outlook_to_google"
  }
  ```

### 8. Update state

- Maintain `~/.ea/state/bridged-events.json` for tracking
- Report bridge actions in morning/afternoon briefings
