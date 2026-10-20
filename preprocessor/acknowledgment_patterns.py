"""Regex-based classifier for acknowledgment-style email replies.

An "acknowledgment" is an inbound message that confirms a prior action
by the user closed out a thread, rather than asking for further action.
Examples: RIO's "Thanks for submitting your proposal, we'll be in touch";
a recipient's "Got it, thanks"; a portal's "Your application was received."

Used by `closure_scrubber.py` to recognize that a ledger item's
underlying thread is resolved, so the morning briefing stops surfacing
it.

The classifier is intentionally pattern-based, not LLM-based: it runs
inside the preprocessor on every open ledger item, so per-call cost
matters. Ambiguous cases are caught by the briefing LLM downstream
(see `skills/morning-briefing.md`).
"""

import re

# Each positive pattern is a (compiled regex, label) pair. A match on
# ANY of these flips the item to "acknowledgment-shaped" — subject to
# NEGATIVE_GUARDS below, which can veto it.
POSITIVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # "Thanks for submitting / sending / your submission"
    (re.compile(
        r"\b(thank\s+you|thanks?)\s+(for\s+)?(submitting|sending|your\s+(submission|proposal|application|email|message|reply))\b",
        re.IGNORECASE,
    ), "submission-ack"),
    # "We will be in touch / get back to you / review / let you know"
    (re.compile(
        r"\bwe('?ll|\s+will)\s+(be\s+in\s+touch|get\s+back\s+to\s+you|review|let\s+you\s+know|follow\s+up|reach\s+out)\b",
        re.IGNORECASE,
    ), "review-ack"),
    # "(Received|Got|Have) your (proposal|application|submission|...)"
    (re.compile(
        r"\b(received|got|have)\s+(your\s+)?(proposal|application|submission|message|email|request|response|reply|manuscript|abstract|paper|registration|payment|order|form)\b",
        re.IGNORECASE,
    ), "receipt-ack"),
    # "Your submission has been received / accepted / recorded".
    # Allow one optional adverb between "has been" and the verb to catch
    # "has been successfully recorded" / "was just received".
    (re.compile(
        r"\byour\s+(submission|application|proposal|request|response|manuscript|abstract|paper|registration|payment|order|form)\s+(has\s+been|was|is)(\s+\w+)?\s+(received|accepted|recorded|logged|registered|complete|successful|confirmed)\b",
        re.IGNORECASE,
    ), "portal-receipt-ack"),
    # Registration-system confirmations (NSF / ACM / IEEE / Eventbrite / Cvent / Zoom).
    # "Thank you for registering" / "registration is confirmed" / "successfully registered"
    (re.compile(
        r"\b(thank\s+you\s+for\s+(registering|your\s+registration)"
        r"|registration\s+(is\s+|has\s+been\s+)?(confirmed|complete|successful|received)"
        r"|successfully\s+registered|you('?re|\s+are)\s+(now\s+)?(registered|confirmed)\s+for)\b",
        re.IGNORECASE,
    ), "registration-ack"),
    # Submission portals: "Submitted successfully" / "Submission complete"
    (re.compile(
        r"\b(submitted\s+successfully|submission\s+(is\s+|has\s+been\s+)?(complete|successful|received|recorded))\b",
        re.IGNORECASE,
    ), "submission-success-ack"),
    # "Thanks!" / "Thank you" as a complete short reply (single sentence)
    (re.compile(
        r"^\s*(thanks?|thank\s+you|cheers|much\s+appreciated|appreciated)[\s!.’']*\s*$",
        re.IGNORECASE,
    ), "terse-ack"),
    # Each alternative is a shape where `confirmation` is the thing being
    # DELIVERED, not the thing being requested:
    #   1. an identifier follows it  ("confirmation number 8813")
    #   2. it opens a labelled line  ("Confirmation: your room is held")
    #   3. it ends the text          ("Purchase Confirmation", the
    #      subject-line shape)
    # A bare "confirmation" plus a space would also match "we need your
    # confirmation before we can proceed", which is a request, not a close.
    (re.compile(
        r"(?:\bconfirmation\s+(?:of|that|number|code|#)\b"
        r"|\bconfirmation\s*[:#]"
        r"|\bconfirmation\s*[.!]?\s*\Z)",
        re.IGNORECASE,
    ), "confirmation-ack"),
    # "No further action (needed|required)" / "nothing more (needed|required)" / "all set"
    (re.compile(
        r"\b(no\s+further\s+action\s+(needed|required|necessary)|nothing\s+(more|further)\s+(needed|required)|all\s+set)\b",
        re.IGNORECASE,
    ), "no-action-ack"),
    # OOO / vacation autoresponders — closure-equivalent for "needs response"
    # because they're not actually asking the user to do anything.
    (re.compile(
        r"\b(out\s+of\s+(the\s+)?office|on\s+vacation|away\s+until|i\s+am\s+(currently\s+)?away|currently\s+out\s+of\s+office)\b",
        re.IGNORECASE,
    ), "ooo"),
    # "Got it" / "Noted" as a COMPLETE short reply. The pattern is a
    # prefix match by construction (`^` with a tail that can match zero
    # characters and no end anchor), so it also opens any message that
    # merely STARTS with one of these words. `classify()` bounds the tail
    # (ACK_SHORT_MAX_TAIL_CHARS) so the match only counts when what
    # follows is a courtesy sign-off rather than a new message body.
    (re.compile(
        r"^\s*(got\s+it|noted|understood|sounds\s+good|will\s+do|ok|okay)[\s!.’',]*",
        re.IGNORECASE,
    ), "ack-short"),
]

# Longest trailing remainder an `ack-short` prefix match may carry and
# still count as a closure. "Noted, thanks Kit." (tail 11) closes;
# "Noted. Three action items for you: …" (tail 90) does not.
ACK_SHORT_MAX_TAIL_CHARS = 40

# Support boilerplate that ends in a question mark but asks nothing of
# the user ("If you have any questions?", "Questions?"). Removed from the
# text before the guards run, so the blanket question-mark guard below
# does not veto every portal confirmation that carries a help footer.
_BOILERPLATE_QUESTION_RE = re.compile(
    r"(?:if\s+you\s+have\s+)?(?:any\s+)?"
    r"(?:further\s+|additional\s+|more\s+|other\s+)?questions\s*\?",
    re.IGNORECASE,
)

# Negative guards veto a positive match. If ANY of these appear in the
# text, the message is NOT classified as a closure: it has follow-up
# content. The hard shape is a polite opener or a delivered confirmation
# followed by a NEW ask, stated as an obligation rather than a question,
# so the guards cover (1) any question mark, (2) request and obligation
# verbs, (3) deadline phrases.
NEGATIVE_GUARDS: list[re.Pattern] = [
    # Contrasts implying more content follows
    re.compile(r"\bbut\s+(i|we|could|can|would|will|one|another)\b", re.IGNORECASE),
    re.compile(r"\bhowever[,\s]", re.IGNORECASE),
    re.compile(r"\b(follow.?up|quick|one\s+more|additional|another)\s+(question|ask|favor|request)\b", re.IGNORECASE),
    re.compile(r"\bcan\s+you\s+(also|please)\b", re.IGNORECASE),
    re.compile(r"\bcould\s+you\s+(also|please)\b", re.IGNORECASE),
    re.compile(r"\bwould\s+you\s+(also|mind|be)\b", re.IGNORECASE),
    # ANY question mark, not just a trailing one: a mid-message ask is
    # still an ask. Support boilerplate ("Questions?") is stripped before
    # the guards run, so this does not veto help footers.
    re.compile(r"\?"),
    # Embedded forwarded content with a fresh ask buried after the OOO
    re.compile(r"\b(forwarded|forwarding|see\s+below)\b", re.IGNORECASE),

    # --- (2) request / obligation verbs -----------------------------
    # "I still need your signed W-9", "I'll need the letter", "we need
    # your slides", "the panel still requires your rebuttal".
    re.compile(r"\bneed(?:s|ed)?\s+(?:your|the|a|an|you\s+to)\b", re.IGNORECASE),
    re.compile(r"\brequire(?:s|d)?\s+(?:your|the|a|an|you\s+to)\b", re.IGNORECASE),
    # The confirmation is being ASKED FOR, not delivered.
    re.compile(
        r"\b(?:need|needs|needed|require|requires|required|await|awaiting|"
        r"expect|expecting|send|provide)\s+(?:your\s+|the\s+|a\s+|an\s+)?"
        r"confirmation\b",
        re.IGNORECASE,
    ),
    # Explicit politeness + an action verb.
    re.compile(
        r"\bplease\s+(?:send|provide|reply|respond|review|sign|complete|"
        r"confirm|submit|return|fill|upload|forward|approve|let\s+me\s+know)\b",
        re.IGNORECASE,
    ),
    # A bare imperative opening a clause: ". review the budget",
    # "; hold everything for me", "and confirm the travel dates".
    re.compile(
        r"(?:^|[.;:!?]\s+|,\s+|\band\s+)(?:please\s+)?"
        r"(?:review|sign|send|provide|complete|submit|confirm|forward|"
        r"approve|fill|upload|return|hold|mark)\s+"
        r"(?:the|your|it|this|these|those|everything|all|us|me|out|up)\b",
        re.IGNORECASE,
    ),
    # "Action items for you", "next steps for you"
    re.compile(r"\b(action\s+items?|next\s+steps?)\b", re.IGNORECASE),

    # --- (3) deadline phrases ---------------------------------------
    re.compile(
        r"\bby\s+(?:mon|tues|wednes|thurs|fri|satur|sun)day\b", re.IGNORECASE),
    re.compile(
        r"\bby\s+(?:tomorrow|noon|eod|cob|end\s+of\s+(?:day|week|the\s+week)|"
        r"next\s+week)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bby\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
        r"\s+\d{1,2}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bdeadline\s+(?:is|for|of|to)\b", re.IGNORECASE),
    re.compile(r"\bdue\s+(?:by|on|no\s+later\s+than)\b", re.IGNORECASE),
]


def classify(text: str | None) -> tuple[bool, str | None]:
    """Return (is_acknowledgment, pattern_label).

    Strips leading boilerplate, then walks POSITIVE_PATTERNS in order.
    On a positive hit, checks NEGATIVE_GUARDS; if any guard fires,
    the match is vetoed and we return (False, None). Empty / None
    inputs return (False, None).

    `ack-short` additionally requires the matched opener to be the whole
    reply: anything past ACK_SHORT_MAX_TAIL_CHARS after it is a new
    message body, not a sign-off, so the match is dropped.
    """
    if not text:
        return (False, None)
    # Take only the first ~1000 chars — quoted thread history below
    # the user's actual reply confuses the classifier otherwise.
    sample = text.strip()[:1000]
    if not sample:
        return (False, None)

    matched_label = None
    matched = None
    for pattern, label in POSITIVE_PATTERNS:
        m = pattern.search(sample)
        if m:
            matched_label = label
            matched = m
            break
    if not matched_label:
        return (False, None)

    # `ack-short` is a PREFIX match with no end anchor. Bound its tail so
    # "Ok." closes and "Ok. <a whole new request>" does not.
    if matched_label == "ack-short":
        tail = sample[matched.end():].strip()
        if len(tail) > ACK_SHORT_MAX_TAIL_CHARS:
            return (False, None)

    guard_text = _BOILERPLATE_QUESTION_RE.sub(" ", sample)
    for guard in NEGATIVE_GUARDS:
        if guard.search(guard_text):
            return (False, None)

    return (True, matched_label)


if __name__ == "__main__":
    # Smoke test: run a few cases when invoked directly.
    samples = [
        ("Thanks for submitting your proposal — we'll be in touch.", True),
        ("Thanks for your reply, but I have a follow-up question.", False),
        ("Got it — can you also send the budget?", False),
        ("I'm out of office until June 3.", True),
        ("Received your application. We will review and respond.", True),
        ("Thanks!", True),
        ("", False),
        ("Hi Ari, when is the meeting?", False),
        ("Confirmation of your registration is attached.", True),
        ("Your submission has been received.", True),
        ("All set, thanks!", True),
        ("Got it.", True),
        ("Purchase Confirmation", True),
        ("Noted, thanks Ari.", True),
        # --- openers that carry a new ask: all must be False -----------
        ("We need your confirmation before we can proceed with the contract.", False),
        ("Attached is the confirmation number for the room. I still need "
         "your signed W-9 before payment can be issued.", False),
        ("Confirmation of receipt is not the same as approval; the panel "
         "still requires your rebuttal.", False),
        ("Ok. The committee meeting moved to Thursday and we need your "
         "slides by Wednesday noon.", False),
        ("Sounds good. I'll need the letter on department letterhead, "
         "signed, before we can submit.", False),
        ("Noted. Three action items for you: review the budget, sign the "
         "MOU, and confirm the travel dates.", False),
        ("Will do. In the meantime please review the attached draft and "
         "mark it up.", False),
        ("Got it, I'll expect the revised budget from you by Monday.", False),
        ("I have your request for a letter of recommendation. The deadline "
         "is Sept 1 and I need your CV.", False),
        ("I am out of office until June 3. My colleague Avery will not be "
         "covering; hold everything for me.", False),
    ]
    fails = 0
    for text, expected in samples:
        is_ack, label = classify(text)
        ok = "PASS" if is_ack == expected else "FAIL"
        if is_ack != expected:
            fails += 1
        print(f"  {ok}  expected={expected!s:5s} got=({is_ack!s},{label!s}) | {text[:60]!r}")
    print(f"\n  {len(samples) - fails} passed, {fails} failed")
    import sys
    sys.exit(1 if fails else 0)
