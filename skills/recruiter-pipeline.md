# Skill: Recruiter Pipeline

Track job search conversations, interview scheduling, and follow-ups
across recruiters and companies.

## Problem this solves

The user is actively exploring opportunities (or being recruited) and
has scheduling-heavy back-and-forth with recruiters:
- An agency recruiter placing an industry role
- A company's in-house recruiting team
  - Multiple interview stages (initial call → technical → onsite)
- An advisory or venture contact, coffee chats
- Other inbound recruiter contacts

The active companies, recruiters and their addresses live in the ledger
(`domain: "career"`), not in this file.

Each conversation involves:
- Time-zone aware scheduling proposals
- Phone number exchanges
- Interview prep (research the company/role)
- Follow-up after interviews
- Tracking where each opportunity stands

## Trigger

User says in chat:
- "pipeline"
- "job pipeline"
- "recruiter status"
- "interviews"
- "what's active"
- "follow up with [recruiter name]"

Or: a new email from a known recruiter is detected.

## Steps

### 1. Scan for recruiter/job emails

Search Gmail:
```
from:(<recruiter domains from the career ledger rows> OR
      linkedin.com/comm/jobs OR greenhouse.io OR lever.co OR
      ashbyhq.com)
newer_than:30d
```

Also search for job-related subjects:
```
subject:(interview OR "find a time" OR "phone screen" OR
         "next steps" OR "offer" OR recruiter OR role OR position)
newer_than:30d
```

### 2. Build pipeline view

```
🎯 **Job Pipeline** (updated [date])

━━━ ACTIVE ━━━

**1. Senior Perception Engineer (REMOTE)**
   Company: [via agency]
   Recruiter: [name] ([address])
   Stage: 📞 Initial call scheduling
   Last activity: Mar 25 — phone number exchanged, call TBD Thu/Fri
   Next step: Call scheduled for [date/time] or awaiting confirmation
   → **"prep for [company]"** to get interview prep notes

**2. [Company], Staff/Principal Engineer**
   Recruiter: [name] → [scheduler] (scheduling)
   Stage: ✅ Completed (Jan 8 onsite was final stage)
   Last activity: Jan 8, video onsite interview completed
   Next step: Awaiting decision / follow up
   → **"follow up [company]"** to draft a check-in email

**3. [Firm], Advisory/Coffee**
   Contact: [name] ([address])
   Stage: ☕ Relationship building
   Last activity: Dec 12 — lunch meeting
   Next step: No pending action
   → **"coffee with [name]"** to schedule next meeting

━━━ INACTIVE / CLOSED ━━━
(items older than 60 days with no activity)
```

### 3. Scheduling assistance

When a recruiter email asks to schedule:
- Cross-reference ALL calendars (Outlook + both Google) for availability
- Factor in: the user typically proposes 2-3 specific slots
- Match the user's observed style: "I can talk about this on [day] [time1],
  [time2] [zone] or [day] [time3]. Let me know if either of those works."
- Include the user's phone number (`phone` in user-profile.json, if set) when relevant
- Include the user's time zone

### 4. Interview prep

When user says "prep for [company]":
- Search for all emails from that company/recruiter
- Extract: role title, job description, key requirements
- Search for the user's previous responses for context on what interested them
- Note: the user's background is `bio_blurb` in user-profile.json;
  tailor prep to their expertise areas
- Suggest: "Review their recent work on [X]. You might be asked about [Y]
  given your experience with [Z]."

### 5. Follow-up drafting

When user says "follow up with [name]":
- Find the last email in the thread
- Calculate days since last contact
- Draft appropriate follow-up:
  - <7 days: "Just checking in on..."
  - 7-14 days: "I wanted to follow up on our conversation about..."
  - >14 days: "I hope you're doing well. I wanted to circle back on..."
- Match the user's email style (concise, professional, includes
  their usual signature link)

### 6. Auto-detect recruiter emails

In the morning briefing, when a new email from a recruiter is detected:
- Flag it prominently: "📩 New from [recruiter]: [subject]"
- If it's asking to schedule, immediately suggest times
- If it's a follow-up, summarize the thread history

### 7. Confidentiality note

Job search emails should NEVER be:
- Mentioned in any communication from the user's work address
- Included in briefings that might be seen by others
- Cross-posted to any shared channels

All pipeline tracking stays in the private EA channel only.

### 8. Update state

- Track each opportunity in obligations ledger under `domain: "career"`
- Fields: company, role, recruiter, stage, last_activity, next_step
- Auto-advance stages based on email content (scheduling → interview →
  follow-up → decision)
