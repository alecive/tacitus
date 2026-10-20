# Skill: Dispute Tracker

Manage multi-step billing disputes, support tickets, and refund requests
that require persistent back-and-forth with a vendor.

## Problem this solves

The user occasionally gets stuck in frustrating support loops:
- **A cloud-gaming vendor**: deleted account still being billed, multiple exchanges
  needed (provide email → provide account → provide last 4 digits → etc.)
- **Unclaimed property claims**: a state claim, requires
  90-day review periods
- **Insurance disputes**: policy overlap, cancellation coordination
- **HOA / property management**: compliance disputes

These threads are tedious, require persistence, and often stall when the
user doesn't reply to the latest request for information within a few days.

## Trigger

User says in chat:
- "dispute status"
- "what's open with [company]"
- "follow up on [billing issue]"
- "track dispute with [company]"
- "help me fight [company]"

Or: a reply arrives on a tracked dispute thread.

## Steps

### 1. Identify active disputes

Search Gmail for support/billing threads:
```
from:(support@ OR billing@ OR noreply@ OR no-reply@ OR no_reply@)
subject:(ticket OR case OR claim OR dispute OR refund OR
         cancellation OR "billed" OR charge)
newer_than:90d
```

Also check obligations ledger for entries with `type: "dispute"`.

### 2. Build dispute dashboard

```
⚔️ **Active Disputes**

**1. [Vendor]: Unauthorized billing** [ticket id]
   Status: 🟡 Waiting on user (they asked for last 4 digits of card)
   Last exchange: Mar 15, they say no active subscription on file
   Your last reply: Mar 14, provided email address
   Stale for: 11 days
   → **"reply to [vendor]"** to provide the info they asked for
   → **"escalate [vendor]"** to draft an escalation email
   → **"chargeback [vendor]"** to draft a bank dispute instead

**2. State Unclaimed Property: Claim #[number]**
   Status: 🔵 In review (90-day window)
   Filed: Jan 3, 2025
   Expected resolution: ~Apr 2025
   → **"check claim"** to look for updates
   → **"nudge claim"** to draft a status inquiry

(no other active disputes)
```

### 3. Stale dispute detection

For each tracked dispute, check if it's stalled:
- If the vendor replied and the user hasn't responded in >5 days:
  "⚠️ [Vendor] is waiting on you (11 days). Reply soon or they'll close the ticket."
- If the user replied and vendor hasn't responded in >14 days:
  "[Vendor] hasn't replied in 14 days. Want me to draft a follow-up?"

### 4. Escalation drafting

When user says "escalate [company]":
- Review full thread history
- Draft an escalation email that:
  - References the ticket/case number
  - Summarizes the timeline of the dispute
  - States what was asked and what was provided
  - Requests escalation to a supervisor/manager
  - Sets a deadline for response (7 business days)
  - Mentions consumer protection remedies if appropriate
    (FTC complaint, state AG, chargeback)
- Tone: firm but professional, factual, no threats (yet)

### 5. Chargeback preparation

When user says "chargeback [company]":
- Compile a timeline of the dispute
- List all evidence (emails, screenshots, dates)
- Draft a dispute letter for the credit card company:
  - Transaction dates and amounts
  - Merchant name
  - Description of the issue
  - Steps taken to resolve with merchant
  - Supporting documentation list
- Suggest: "You can usually file this through your card's online portal
  or by calling the number on the back."

### 6. Auto-reply suggestions

When a support reply arrives and asks for specific information:
- Parse what they're asking for (account email, last 4 digits, receipt, etc.)
- If the user has provided this info before in the thread, note it:
  "They're asking for info you already provided on [date]. Want me to
  re-send it with a note pointing that out?"
- If new info is needed, prompt the user in chat:
  "[Vendor] wants the last 4 digits of your payment card. Reply here
  and I'll draft the response."

### 7. Resolution tracking

When a dispute is resolved:
- Record outcome (refund amount, credit, resolution date)
- Archive the thread
- If the resolution involved a chargeback, note the bank's reference number
- Post: "✅ [Vendor] dispute resolved: $X refunded to card ending [XXXX]"

### 8. Update state

- Track disputes in obligations ledger under `type: "dispute"`
- Fields: company, ticket_number, issue_summary, status,
  last_vendor_reply, last_user_reply, amount_disputed,
  escalation_level, resolution
- Auto-promote urgency if stale >7 days
