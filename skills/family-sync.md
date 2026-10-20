# Skill: Family Sync

Coordinate family logistics across multiple calendars, activity signups,
and co-parenting communication.

## Problem this solves

The user manages family logistics across a fragmented set of calendars.
Who is in the family, and which calendars carry their events, come from
config, never from this file:
- `~/.ea/config/user-profile.json` `family`: a list of
  `{"name", "relation", "notes"}` (spouse, children, their ages and
  activities as the user wrote them).
- `~/.ea/config/user-profile.json` `calendar_accounts`: the Google
  accounts the EA can read (the shared family calendar is one of them;
  `scheduling-preferences.json` `calendars` names it `google_family`).
- `~/.ea/config/scheduling-preferences.json` `family_calendars` (when
  present): a list of `{"name", "id"}` for the secondary and imported
  calendars (school calendars, the district's academic dates, a youth
  club, a spouse's work schedule, the user's own teams or clubs).

Typical shape of the set: a shared family calendar, one or two school
calendars, the district's academic dates (snow days, breaks), a youth
club's event and committee calendars, a spouse's work schedule, and the
user's own activity calendars.

The challenge: these calendars don't talk to each other. Conflicts between
kids' school events, club activities, the user's own activities, a
spouse's shifts, and the user's work calendar require manual cross-checking.

## Trigger

User says in chat:
- "family schedule"
- "what's on this weekend"
- "any conflicts this week"
- "kids schedule"
- "family sync"
- "what are the kids doing [day]"

Or: morning briefing includes a family-relevant calendar conflict.

## Steps

### 1. Gather all family calendars

Read `family`, `calendar_accounts` and `family_calendars` from the config
files above, then query the relevant time range with the Google Calendar
MCP across:
```python
family_calendars = [
    "primary",                                   # the user's personal Google calendar
    "<the family calendar in calendar_accounts>", # shared family calendar
    # plus every id in scheduling-preferences.json family_calendars:
    # school calendars, district academic dates, youth club calendars,
    # a spouse's schedule import, the user's activity calendars
]
```
Label each calendar in the output with its `name` from config. If
`family_calendars` is absent, use `google_calendar_client.py
list-calendars --email <account>` and ask the user which ones are family
calendars; suggest adding them to config.

### 2. Merge and detect conflicts

Layer events by priority:
1. **Fixed commitments**: school days, district dates (non-negotiable)
2. **Scheduled activities**: club events, a spouse's shifts
3. **The user's work**: from Outlook (via preprocessor availability_merged)
4. **The user's personal**: activities, social
5. **Family events**: from shared calendars

Detect conflicts:
- One of the user's activities overlaps with a club event: flag
- A spouse's shift during a time the user assumed they'd be with kids: flag
- School holiday (district calendar) + no childcare plan: flag
- Two kids' events at same time in different locations: flag

### 3. Present family view

Use the names from `family` (first names) and the calendar names from
config. Example shape:

```
👨‍👩‍👦‍👦 **Family Schedule, [Week of Date]**

**Monday [date]:**
  🏫 School day ([school calendar name])
  💼 [Spouse]: shift 3:00-5:00 PM
  → [User] on kid duty after 3 PM

**Tuesday [date]:**
  🏫 School day
  🏅 [User]: [activity] 7:50 PM @ [venue]
  → Need sitter or [Spouse] covers evening

**Wednesday [date]:**
  🏫 School day
  ⛺ [Club]: meeting 6:00 PM
  → [User] + [child]

**Thursday [date]:**
  🏫 School day
  (clear evening)

**Friday [date]:**
  🏫 School day
  ❄️ [District]: No school (teacher workday)  ← ALERT
  → Need childcare plan

**Saturday [date]:**
  ⛺ [Club]: [event] 10:00 AM
  → Whole family

**Sunday [date]:**
  🏅 [User]: [activity] 9:00 PM
  → Late start, kids in bed first
```

### 4. Conflict alerts

```
⚠️ CONFLICTS DETECTED:

• Fri [date]: No school ([district]) but no childcare event on calendar.
  Need: babysitter, parent coverage, or camp signup
  → **"find sitter"** to search for options
  → **"add to family cal: kids home"** to mark it

• Tue [date]: [Activity] (7:50 PM) + [Spouse]'s evening shift (7:00-8:00 PM)
  Both parents unavailable 7:00-8:00 PM.
  → **"find sitter for tue evening"**
  → **"skip [activity]"** to reply "I'm out" to its thread
```

### 5. Childcare coordination

When a gap is detected:
- Check if a known sitter has been used before (the `notes` on a
  `family` entry may name one; otherwise search past mail)
- If the user has a sitter list in contacts, suggest reaching out
- For school breaks, check if camp registration emails exist and flag
  deadlines

### 6. Quick actions

```
Reply with:
- **"add [event] to family cal"**: create event on shared calendar
- **"text [spouse] about [day]"**: (not implemented yet, note for future)
- **"find sitter for [date]"**: search contacts for sitter options
- **"rsvp club [event] for [#]"**: submit a club RSVP
```

### 7. School calendar intelligence

- The district calendar has snow days, breaks, teacher workdays
- Each school calendar has school-specific events (conferences, performances)
- When a no-school day is detected, proactively flag it 3 days in advance:
  "📢 Heads up: no school [day] ([reason]). No childcare plan on calendar yet."

### 8. Update state

- Do NOT store family data in obligations ledger (too noisy)
- Flag unresolved childcare gaps as a deadline-type obligation
