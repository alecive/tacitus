#!/usr/bin/env python3
"""One content fingerprint for a draft, Outlook or Gmail.

The owner approves what a Send card shows, and the draft id alone does not
say what that is: update-draft, attach-file and remove-attachment are SAFE
verbs, so the resident can change a draft after its card is up. The
fingerprint binds the approval to the content. It is a SHA-256 over a
canonical form of the address headers (To, Cc, Bcc, From, Reply-To, both
parsed and as the raw values), the subject, every body part and the
attachment list (name, type, size and a hash of the bytes, or the
attachment id where the API gives no bytes).

The owner side computes it from its own read of the draft when it renders a
card, stores it in the owner-private card registry, and recomputes it from a
fresh read before every send. A mismatch means the content changed after
the owner looked at it, and nothing is sent. The fingerprint is not a
secret: knowing it lets nobody send anything, because the comparison is
always against a value the owner side computed itself.

    canonical(parts) -> bytes      the bytes that are hashed
    fingerprint(parts) -> str      "sha256:<hex>"
    outlook_parts(message, attachments) -> dict
    gmail_parts(raw_mime_bytes) -> dict
    outlook_fingerprint(message_id, graph) -> (fingerprint, message, attachments)
"""

import base64
import hashlib
import json
from email import message_from_bytes
from email import policy as _email_policy
from email.utils import getaddresses

VERSION = 2
PREFIX = "sha256:"

# The message fields an Outlook fingerprint reads. bccRecipients is not in
# the briefing's field list, so the read names its own.
OUTLOOK_SELECT = ("id,subject,toRecipients,ccRecipients,bccRecipients,body,"
                  "hasAttachments,conversationId,isDraft,from,replyTo")

# The address headers whose raw values the fingerprint also covers. The
# parsed form lower-cases and strips, and a parser maps some malformed
# values to the same pairs, so two different headers could give the same
# parsed list; their raw values never do.
RAW_HEADERS = ("to", "cc", "bcc", "from", "reply_to")


def _addr_list(pairs) -> list:
    """[[address lower-cased, display name], ...] sorted. The display name
    is part of what the recipient sees, so it is part of the content."""
    out = []
    for name, addr in pairs or []:
        out.append([str(addr or "").strip().lower(), str(name or "").strip()])
    return sorted(out)


def _attachment_list(items) -> list:
    """[[name, content type, size, content hash or id], ...] sorted."""
    out = []
    for a in items or []:
        out.append([str(a.get("name") or ""), str(a.get("content_type") or "").lower(),
                    int(a.get("size") or 0), str(a.get("content") or "")])
    return sorted(out)


def _raw_digest(value) -> str:
    """A hash of one raw header value. Bytes a parser kept as surrogate
    escapes are hashed as the bytes they were."""
    if isinstance(value, bytes):
        data = value
    else:
        data = str(value).encode("utf-8", "surrogateescape")
    return PREFIX + hashlib.sha256(data).hexdigest()


def _raw_headers(raw: dict | None) -> dict:
    """{header: [hash of each raw value, sorted]} for RAW_HEADERS."""
    raw = raw or {}
    return {h: sorted(_raw_digest(v) for v in raw.get(h) or []) for h in RAW_HEADERS}


def canonical(parts: dict) -> bytes:
    """The bytes the fingerprint hashes: compact JSON with sorted keys, so
    the same content always gives the same bytes on both mail systems."""
    doc = {
        "v": VERSION,
        "to": _addr_list(parts.get("to")),
        "cc": _addr_list(parts.get("cc")),
        "bcc": _addr_list(parts.get("bcc")),
        "from": _addr_list(parts.get("from")),
        "reply_to": _addr_list(parts.get("reply_to")),
        "raw_headers": _raw_headers(parts.get("raw_headers")),
        "subject": str(parts.get("subject") or ""),
        "body": [[str(t or "").lower(), str(c or "")] for t, c in parts.get("body") or []],
        "attachments": _attachment_list(parts.get("attachments")),
    }
    return json.dumps(doc, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def fingerprint(parts: dict) -> str:
    return PREFIX + hashlib.sha256(canonical(parts)).hexdigest()


def _sha256(data: bytes) -> str:
    return PREFIX + hashlib.sha256(data).hexdigest()


# ── Outlook (Graph) ──────────────────────────────────────────────────

def _graph_recipients(rows) -> list:
    pairs = []
    for r in rows or []:
        ea = (r or {}).get("emailAddress") or {}
        pairs.append((ea.get("name", ""), ea.get("address", "")))
    return pairs


def _graph_raw(rows) -> list:
    """Each recipient object exactly as Graph returned it, as JSON with
    sorted keys: no lower-casing, no stripping."""
    return [json.dumps(r, sort_keys=True, ensure_ascii=True) for r in rows or []]


def outlook_parts(message: dict, attachments: list) -> dict:
    """The canonical fields of a Graph message and its attachment list (the
    /attachments read, which carries contentBytes for a file attachment).
    An attachment with no bytes (an item or reference attachment) is
    named by its id."""
    body = (message or {}).get("body") or {}
    atts = []
    for a in attachments or []:
        raw = a.get("contentBytes")
        if raw:
            try:
                content = _sha256(base64.b64decode(raw))
            except (ValueError, TypeError):
                content = "undecodable:" + str(a.get("id") or "")
        else:
            content = "id:" + str(a.get("id") or "")
        atts.append({"name": a.get("name"), "content_type": a.get("contentType"),
                     "size": a.get("size"), "content": content})
    sender = [message["from"]] if message.get("from") else []
    fields = {"to": message.get("toRecipients"), "cc": message.get("ccRecipients"),
              "bcc": message.get("bccRecipients"), "from": sender,
              "reply_to": message.get("replyTo")}
    return {
        "to": _graph_recipients(fields["to"]),
        "cc": _graph_recipients(fields["cc"]),
        "bcc": _graph_recipients(fields["bcc"]),
        "from": _graph_recipients(fields["from"]),
        "reply_to": _graph_recipients(fields["reply_to"]),
        "raw_headers": {k: _graph_raw(v) for k, v in fields.items()},
        "subject": message.get("subject") or "",
        "body": [[body.get("contentType", ""), body.get("content", "")]],
        "attachments": atts,
    }


def outlook_fingerprint(message_id: str, graph) -> tuple:
    """(fingerprint, message, attachments) from one owner-side read of an
    Outlook draft through `graph` (the graph_client module). The message is
    read first and the attachments second; the card shows the same pair the
    hash covers, so a change between the two reads is caught at send. The
    id often comes from resident state, so one that is not a plain mail id
    is refused (ValueError) before any read."""
    try:
        import mail_mediation as _mm
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import mail_mediation as _mm
    if not _mm.is_plain_id(message_id):
        raise ValueError(f"draft id {str(message_id)[:24]!r} is not a plain mail id")
    message = graph.graph_get(f"/me/messages/{message_id}", {"$select": OUTLOOK_SELECT})
    attachments = graph.get_attachments(message_id) if message.get("hasAttachments") else []
    return fingerprint(outlook_parts(message, attachments)), message, attachments


# ── Gmail ────────────────────────────────────────────────────────────

def gmail_parts(raw: bytes) -> dict:
    """The canonical fields of one Gmail message in RFC 822 form (the
    format="raw" read). Every text part that is not a file goes into the
    body in tree order; every part with a file name or an attachment
    disposition is an attachment, hashed over its decoded bytes."""
    msg = message_from_bytes(raw or b"", policy=_email_policy.default)
    body, atts = [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        if name or part.get_content_disposition() == "attachment":
            data = part.get_payload(decode=True) or b""
            atts.append({"name": name or "", "content_type": part.get_content_type(),
                         "size": len(data), "content": _sha256(data)})
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError, KeyError):
            content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if isinstance(content, bytes):
            content = _sha256(content)
        body.append([part.get_content_type(), str(content)])

    def _pairs(header: str) -> list:
        return getaddresses([str(v) for v in msg.get_all(header, [])])

    # The raw values, as the message carries them, before any parsing.
    names = {"to": "to", "cc": "cc", "bcc": "bcc", "from": "from", "reply-to": "reply_to"}
    raw_headers = {k: [] for k in RAW_HEADERS}
    for name, value in msg.raw_items():
        key = names.get(str(name).strip().lower())
        if key:
            raw_headers[key].append(str(value))

    return {"to": _pairs("to"), "cc": _pairs("cc"), "bcc": _pairs("bcc"),
            "from": _pairs("from"), "reply_to": _pairs("reply-to"),
            "raw_headers": raw_headers,
            "subject": str(msg.get("subject") or ""), "body": body,
            "attachments": atts}


def gmail_fingerprint_raw(raw_b64url: str) -> str:
    """The fingerprint of a Gmail message from its base64url `raw` field."""
    raw = base64.urlsafe_b64decode((raw_b64url or "").encode("ascii") + b"==")
    return fingerprint(gmail_parts(raw))
