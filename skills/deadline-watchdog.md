# Skill: Deadline Watchdog
<!-- scopes: mail-triage -->

Track and surface approaching deadlines from all domains of the user's
life — university admin, property management, taxes, compliance, grants,
and personal obligations.

## Problem this solves

The user has deadlines scattered across unrelated systems with no unified
tracker:
- **University**: annual compliance reports, grant milestone deadlines,
  promotion documents
- **Financial**: retirement account action items, benefits fee changes,
  insurance renewals/binders, unclaimed property claims
- **Kids/family**: camp registration form deadlines, school calendar dates,
  youth activity deadlines, volunteer training renewals
- **Personal**: billing disputes with deadlines

Missing any of these has real consequences (fees, non-compliance, lost money).

## Trigger

User says in chat:
- "deadlines"
- "what's due"
- "upcoming deadlines"
- "what am I forgetting"

Or: the weekly audit runs this skill automatically.

Or: the morning briefing detects a deadline within 7 days.

## Steps

### 1. Gather deadlines from all sources

**Obligations ledger** (via `python3 preprocessor/ledger.py list --has-deadline --status open`):
- Returns open entries with deadlines
- Also check `python3 preprocessor/ledger.py stats` for stale items

**Email scan** (targeted, not full inbox):
- Gmail MCP: `subject:(deadline OR due OR reminder OR "action required" OR "by end of" OR overdue) newer_than:30d`
- Look for date patterns in subjects and snippets
- Extract deadline dates using regex: `(by|before|due|deadline)\s+\w+\s+\d{1,2}`

**Calendar events**:
- Search Google Calendar for events with deadline-like names:
  `q="deadline" OR q="due" OR q="submission" OR q="renewal"`
- Check for all-day events that are reminders/deadlines

**Known recurring deadlines** (hardcoded awareness):

(Domain-specific recurring deadlines owned by an interactive plugin are
seeded into the ledger by that plugin and surfaced on the plugin's own
schedule, not hardcoded here. Example: property deadlines live in the
ea-plugin-property plugin.)
```yaml
university:
  - name: "Annual compliance report"
    recurrence: "annually, ~February"
    source: "the university's compliance office"
  - name: "Bi-weekly timesheet approval"
    recurrence: "bi-weekly"
    source: "University payroll calendar"

financial:
  - name: "Tax filing season"
    recurrence: "annually, Feb-April"
    source: "the state tax agency"
  - name: "Retirement account rollover decision"
    source: "the retirement plan provider"
  - name: "Benefits fee changes"
    source: "the benefits provider"

volunteering:
  - name: "Volunteer safety training renewal"
    recurrence: "annually"
    source: "the youth organization's training system"
  - name: "Activity program deadlines"
    source: "the program committee"

kids:
  - name: "Camp registration forms"
    source: "the camp registration provider"
```

### 2. Classify by urgency

```
🔴 OVERDUE (past deadline):
- [item] — was due [date] ([N days ago])

🟠 THIS WEEK:
- [item] — due [date] ([N days])

🟡 THIS MONTH:
- [item] — due [date]

🟢 NEXT 90 DAYS:
- [item] — due [date]
```

### 3. Post to chat

```
⏰ **Deadline Dashboard** (updated [date])

🔴 OVERDUE:
• Retirement account rollover: no deadline stated but "TAKE ACTION" email sent Mar 5
• Camp forms for the kids: due Feb 1 (53 days overdue!)

🟠 THIS WEEK:
• Benefits fee change acknowledgment: Mar 26 (no action needed)
• Input for the community fundraising plan: due Thu Mar 26 night (the committee chair's request)

🟡 THIS MONTH:
• (none)

🟢 UPCOMING:
• Bi-weekly timesheet approval — check payroll calendar
• Home fire extinguisher service: due by end of year

Reply:
- **"done [item]"** — mark as completed
- **"snooze [item] [date]"** — remind me later
- **"details [item]"** — show the source email/context
```

### 4. Smart deadline extraction

For emails that mention deadlines but don't have explicit dates:
- "Please try to put your input into all of the docs by Thursday night"
  → Extract: input due [Thursday of that week], convert to absolute date
- "Required forms must be completed for your child to attend camp"
  → Flag as "deadline unclear — ask user or check form"
- "TAKE ACTION: Next steps for your retirement savings"
  → Flag as "no hard deadline but action requested"

### 5. Proactive alerts

During morning briefing, if any deadline is within 3 days:
- Add a prominent warning at the top of the briefing
- Suggest specific actions to complete it

During afternoon catchup, if any deadline is TODAY:
- Post a standalone alert: "⏰ [item] is due TODAY"

### 6. Update state

- Store extracted deadlines in obligations ledger with:
  - `type: "deadline"`
  - `source_email_id` (for traceability)
  - `due_date` (absolute)
  - `domain` (university/property/financial/kids/personal)
  - `auto_resolved` (true if it's an autopay or automated process)
- When email shows the item is done, propose the close (`ledger.py propose
  --id <id> --why "<evidence>"`); the user confirms it on the card. Close
  it outright only on the user's word (`close --user-said`).
