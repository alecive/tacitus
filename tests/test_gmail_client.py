#!/usr/bin/env python3
"""Tests for gmail_client.py full-body read verbs.

`search` returns only a 300-char snippet, so without a verb that fetches
a full body the bot cannot read a message it is pointed at. The skills
invoke these as CLI subcommands; a one-off `python3 -c` into the module
is rejected by the allowlist.

Two layers:
  A) _extract_body decodes real Gmail payload shapes (plain, html-only,
     nested multipart, empty) — pure, no API.
  B) the get-message / get-thread / update-draft subcommands are
     registered in the CLI (so the skill's documented invocations work).
"""

import base64
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

# No test may touch production state. gmail_client does AUTH_DIR.mkdir() at
# import, so EA_DIR must be a sandbox BEFORE the import below (and it is
# inherited by the CLI subprocesses this file spawns); unset, it resolves to
# the live ~/.ea.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-gmail-")

HERE = Path(__file__).resolve().parent
PREPROC = HERE.parent / "preprocessor"
sys.path.insert(0, str(PREPROC))

import gmail_client  # noqa: E402


def _b64(text: str) -> str:
    """Gmail bodies are base64url-encoded."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# Layer A — _extract_body
# ---------------------------------------------------------------------------

def test_extract_body_plain_text():
    payload = {
        "mimeType": "text/plain",
        "body": {"data": _b64("Checkout: June 30. Conf code ABC123.")},
    }
    out = gmail_client._extract_body(payload)
    assert "Checkout: June 30" in out
    assert "ABC123" in out


def test_extract_body_multipart_prefers_plain():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("PLAIN checkout June 30")}},
            {"mimeType": "text/html",
             "body": {"data": _b64("<p>HTML checkout <b>June 30</b></p>")}},
        ],
    }
    out = gmail_client._extract_body(payload)
    assert out.strip() == "PLAIN checkout June 30", out


def test_extract_body_html_only_strips_tags():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html",
             "body": {"data": _b64("<p>Guest: Yuki<br/>Guests: 4</p>")}},
        ],
    }
    out = gmail_client._extract_body(payload)
    assert "<" not in out and ">" not in out, f"tags not stripped: {out!r}"
    assert "Guest: Yuki" in out
    assert "Guests: 4" in out


def test_extract_body_nested_multipart():
    """Airbnb notices are often multipart/mixed wrapping
    multipart/alternative. The plain part must still surface."""
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain",
                     "body": {"data": _b64("NESTED plain body")}},
                ],
            },
        ],
    }
    out = gmail_client._extract_body(payload)
    assert "NESTED plain body" in out, out


def test_extract_body_empty():
    assert gmail_client._extract_body({}) == ""
    assert gmail_client._extract_body({"mimeType": "text/plain", "body": {}}) == ""


# ---------------------------------------------------------------------------
# Layer B — CLI subcommands exist
# ---------------------------------------------------------------------------

def _cli_help(subcommand: str) -> subprocess.CompletedProcess:
    """`--help` on a registered subparser exits 0 and prints its usage.
    An unregistered subcommand makes argparse exit 2 with an error."""
    return subprocess.run(
        [sys.executable, str(PREPROC / "gmail_client.py"), subcommand, "--help"],
        capture_output=True, text=True,
    )


def test_get_message_subcommand_registered():
    r = _cli_help("get-message")
    assert r.returncode == 0, r.stderr
    assert "--message-id" in r.stdout, r.stdout


def test_get_thread_subcommand_registered():
    r = _cli_help("get-thread")
    assert r.returncode == 0, r.stderr
    assert "--thread-id" in r.stdout, r.stdout


def test_update_draft_subcommand_registered():
    r = _cli_help("update-draft")
    assert r.returncode == 0, r.stderr
    assert "--draft-id" in r.stdout and "--body-file" in r.stdout, r.stdout


def test_functions_exist():
    for name in ("get_message", "get_thread", "update_draft", "_extract_body"):
        assert hasattr(gmail_client, name), f"gmail_client.{name} missing"


# ---------------------------------------------------------------------------
# Layer C — attachments (list / download)
#
# The Gmail MCP has no attachment verb at all, so without these the EA can
# see `filename: hotel-receipt.pdf` in a message and has no way to save
# the bytes.
# Selection semantics deliberately mirror graph_actions download-attachment:
# exact --attachment-id or substring --name-pattern, and zero or multiple
# matches are hard errors so skills get deterministic behavior.
# ---------------------------------------------------------------------------

class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Attachments:
    def __init__(self, store, fetch_log):
        self._store, self._log = store, fetch_log

    def get(self, userId=None, messageId=None, id=None):
        self._log.append(id)
        return _Exec({"data": self._store[id]})


class _Messages:
    def __init__(self, message, store, fetch_log):
        self._message, self._store, self._log = message, store, fetch_log

    def get(self, userId=None, id=None, format=None):
        return _Exec(self._message)

    def attachments(self):
        return _Attachments(self._store, self._log)


class _FakeService:
    """The three call chains gmail_client's attachment verbs use:
    users().messages().get(...) and users().messages().attachments().get()."""

    def __init__(self, message, attachments=None):
        self.attachment_fetches = []
        self._messages = _Messages(message, attachments or {},
                                   self.attachment_fetches)

    def users(self):
        return self

    def messages(self):
        return self._messages


def _b64url_bytes(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _receipt_message() -> dict:
    """A realistic multipart/mixed shape: body text (no filename), two real
    attachments (attachmentId, bytes fetched separately), and one tiny
    inline part (data on the part itself, no attachmentId)."""
    return {
        "id": "m123",
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "multipart/alternative", "filename": "", "parts": [
                    {"mimeType": "text/plain", "filename": "",
                     "body": {"data": _b64("Receipts attached.")}},
                ]},
                {"mimeType": "application/pdf",
                 "filename": "hotel-receipt.pdf",
                 "body": {"attachmentId": "att-1", "size": 4}},
                {"mimeType": "image/png", "filename": "car-gas.png",
                 "body": {"attachmentId": "att-2", "size": 3}},
                {"mimeType": "text/calendar", "filename": "invite.ics",
                 "body": {"data": _b64url_bytes(b"BEGIN:VCALENDAR"),
                          "size": 15}},
            ],
        },
    }


_RECEIPT_BYTES = {"att-1": _b64url_bytes(b"%PDF"),
                  "att-2": _b64url_bytes(b"png")}


def _with_fake_service(fake, fn):
    real = gmail_client._get_service
    gmail_client._get_service = lambda email, interactive=False: fake
    try:
        return fn()
    finally:
        gmail_client._get_service = real


def test_attachment_parts_walks_nested_and_skips_body_parts():
    parts = gmail_client._attachment_parts(_receipt_message()["payload"])
    names = [p.get("filename") for p in parts]
    assert names == ["hotel-receipt.pdf", "car-gas.png", "invite.ics"], names


def test_list_attachments_shape():
    fake = _FakeService(_receipt_message())
    out = _with_fake_service(
        fake, lambda: gmail_client.list_attachments("a@x", "m123"))
    assert out["message_id"] == "m123"
    assert [a["attachment_id"] for a in out["attachments"]] == \
        ["att-1", "att-2", ""], out
    assert out["attachments"][0]["mime_type"] == "application/pdf"


def test_get_message_lists_attachments():
    fake = _FakeService(_receipt_message())
    out = _with_fake_service(
        fake, lambda: gmail_client.get_message("a@x", "m123"))
    assert [a["filename"] for a in out.get("attachments", [])] == \
        ["hotel-receipt.pdf", "car-gas.png", "invite.ics"], out


def test_download_by_name_pattern_writes_the_bytes():
    fake = _FakeService(_receipt_message(), _RECEIPT_BYTES)
    dest = Path(os.environ["EA_DIR"]) / "receipts" / "trip" / "hotel.pdf"
    out = _with_fake_service(
        fake, lambda: gmail_client.download_attachment(
            "a@x", "m123", str(dest), name_pattern="HOTEL"))
    assert "error" not in out, out
    assert out["name"] == "hotel-receipt.pdf"
    assert Path(out["saved"]).read_bytes() == b"%PDF"
    assert fake.attachment_fetches == ["att-1"]


def test_download_by_attachment_id():
    fake = _FakeService(_receipt_message(), _RECEIPT_BYTES)
    dest = Path(os.environ["EA_DIR"]) / "state" / "gas.png"
    out = _with_fake_service(
        fake, lambda: gmail_client.download_attachment(
            "a@x", "m123", str(dest), attachment_id="att-2"))
    assert "error" not in out, out
    assert Path(out["saved"]).read_bytes() == b"png"


def test_download_inline_data_needs_no_attachment_fetch():
    fake = _FakeService(_receipt_message(), _RECEIPT_BYTES)
    dest = Path(os.environ["EA_DIR"]) / "state" / "invite.ics"
    out = _with_fake_service(
        fake, lambda: gmail_client.download_attachment(
            "a@x", "m123", str(dest), name_pattern="invite"))
    assert "error" not in out, out
    assert Path(out["saved"]).read_bytes() == b"BEGIN:VCALENDAR"
    assert fake.attachment_fetches == [], fake.attachment_fetches


def test_download_selection_is_deterministic():
    fake = _FakeService(_receipt_message(), _RECEIPT_BYTES)
    dest = str(Path(os.environ["EA_DIR"]) / "state" / "x.bin")

    no_selector = _with_fake_service(
        fake, lambda: gmail_client.download_attachment("a@x", "m123", dest))
    assert "error" in no_selector

    no_match = _with_fake_service(
        fake, lambda: gmail_client.download_attachment(
            "a@x", "m123", dest, name_pattern="zzz"))
    assert "error" in no_match
    assert len(no_match["available"]) == 3, no_match

    ambiguous = _with_fake_service(
        fake, lambda: gmail_client.download_attachment(
            "a@x", "m123", dest, name_pattern="."))
    assert "error" in ambiguous
    assert len(ambiguous["matches"]) == 3, ambiguous
    assert not Path(dest).exists(), "a failed selection must write nothing"


def test_attachment_subcommands_registered():
    r = _cli_help("list-attachments")
    assert r.returncode == 0, r.stderr
    assert "--message-id" in r.stdout, r.stdout
    r = _cli_help("download-attachment")
    assert r.returncode == 0, r.stderr
    for flag in ("--attachment-id", "--name-pattern", "--output"):
        assert flag in r.stdout, r.stdout


def test_every_mediated_gmail_verb_is_a_registered_subcommand():
    """A verb in mail_mediation's SAFE set with no matching argparse
    subparser forwards fine and then dies in the mediator's child with
    argparse exit 2. Keep the two lists in sync."""
    import mail_mediation
    for verb in sorted(mail_mediation.SAFE["gmail_client"]
                       | mail_mediation.GATED["gmail_client"]):
        r = _cli_help(verb)
        assert r.returncode == 0, (
            f"mediated verb {verb!r} is not a registered gmail_client "
            f"subcommand: {r.stderr}")


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Layer D — threading. A reply carries the original's threadId, In-Reply-To
# and References, or Gmail (and every other client) opens a new conversation.
# ---------------------------------------------------------------------------

class _ThreadingService:
    """drafts().create/update/get, messages().get (metadata) and
    threads().get, recording what the client sent."""

    def __init__(self, original: dict, existing_draft: dict | None = None, thread: dict | None = None):
        self.original, self.existing, self.thread = original, existing_draft or {}, thread or {}
        self.created, self.updated = [], []

    def users(self): return self
    def messages(self): return self
    def drafts(self): return self
    def threads(self): return self

    def get(self, userId=None, id=None, format=None, metadataHeaders=None):
        if id == self.original.get("id"):
            return _Exec(self.original)
        if id == self.existing.get("id"):
            return _Exec(self.existing)
        if id == self.thread.get("id"):
            return _Exec(self.thread)
        return _Exec({})

    def create(self, userId=None, body=None):
        self.created.append(body)
        return _Exec({"id": "d1", "message": {"id": "m-new", "threadId": body["message"].get("threadId", "m-new")}})

    def update(self, userId=None, id=None, body=None):
        self.updated.append((id, body))
        return _Exec({"id": id, "message": {"id": "m-upd", "threadId": body["message"].get("threadId", "m-upd")}})


def _original_message():
    return {"id": "orig1", "threadId": "thr9", "payload": {"headers": [
        {"name": "Message-ID", "value": "<bo.umberfield72@example.com>"},
        {"name": "References", "value": "<jules.foxworth35@example.com>"},
        {"name": "Subject", "value": "Advising - Kit Calloway"},
        {"name": "From", "value": "Tam Prescott <tam@example.org>"},
        {"name": "To", "value": "jo.ambrose4@example.com"}]}}


def _mime_headers(body: dict) -> dict:
    import email as _email
    raw = base64.urlsafe_b64decode(body["message"]["raw"])
    m = _email.message_from_bytes(raw)
    return {k: m[k] for k in ("To", "Subject", "In-Reply-To", "References", "From")}


def test_reply_draft_joins_the_thread_it_answers():
    fake = _ThreadingService(_original_message())
    res = _with_fake_service(fake, lambda: gmail_client.create_draft(
        "jo.ambrose4@example.com", [], "", "Hi Tam, works for me.", reply_to_message_id="orig1"))
    assert res["thread_id"] == "thr9" and res["in_reply_to"] == "<bo.umberfield72@example.com>", res
    body = fake.created[-1]
    assert body["message"]["threadId"] == "thr9"
    h = _mime_headers(body)
    assert h["In-Reply-To"] == "<bo.umberfield72@example.com>"
    assert h["References"] == "<jules.foxworth35@example.com> <bo.umberfield72@example.com>"
    assert h["Subject"] == "Re: Advising - Kit Calloway"      # defaulted from the original
    assert h["To"] == "tam@example.org"                             # defaulted to the sender


def test_reply_by_thread_id_answers_the_newest_message():
    thread = {"id": "thr9", "messages": [{"id": "orig1", "labelIds": ["INBOX"]},
                                          {"id": "dr", "labelIds": ["DRAFT"]}]}
    fake = _ThreadingService(_original_message(), thread=thread)
    res = _with_fake_service(fake, lambda: gmail_client.create_draft(
        "jo.ambrose4@example.com", ["tam@example.org"], "Re: Advising - Kit Calloway", "x",
        thread_id="thr9"))
    assert res["thread_id"] == "thr9"
    assert _mime_headers(fake.created[-1])["In-Reply-To"] == "<bo.umberfield72@example.com>"


def test_new_conversation_carries_no_threading():
    fake = _ThreadingService(_original_message())
    _with_fake_service(fake, lambda: gmail_client.create_draft(
        "jo.ambrose4@example.com", ["flo.thornbury67@example.org"], "Hello", "x"))
    body = fake.created[-1]
    assert "threadId" not in body["message"]
    h = _mime_headers(body)
    assert h["In-Reply-To"] is None and h["References"] is None and h["Subject"] == "Hello"


def test_update_keeps_the_draft_in_its_thread_and_can_move_it():
    existing = {"id": "dX", "message": {"id": "mX", "threadId": "thr9", "payload": {"headers": [
        {"name": "In-Reply-To", "value": "<bo.umberfield72@example.com>"},
        {"name": "References", "value": "<jules.foxworth35@example.com> <bo.umberfield72@example.com>"},
        {"name": "To", "value": "tam@example.org"},
        {"name": "Subject", "value": "Re: Advising - Kit Calloway"}]}}}
    fake = _ThreadingService(_original_message(), existing_draft=existing)
    # a plain edit: empty to/subject keep the draft's, the thread survives
    res = _with_fake_service(fake, lambda: gmail_client.update_draft(
        "jo.ambrose4@example.com", "dX", [], "", "new body"))
    did, body = fake.updated[-1]
    assert did == "dX" and body["message"]["threadId"] == "thr9" and res["thread_id"] == "thr9"
    h = _mime_headers(body)
    assert h["In-Reply-To"] == "<bo.umberfield72@example.com>" and h["To"] == "tam@example.org"
    assert h["Subject"] == "Re: Advising - Kit Calloway"
    # a repair: a draft created as a new conversation is moved into the thread
    orphan = {"id": "dY", "message": {"id": "mY", "threadId": "mY", "payload": {"headers": [
        {"name": "To", "value": "tam@example.org"},
        {"name": "Subject", "value": "Re: Advising - Kit Calloway"}]}}}
    fake2 = _ThreadingService(_original_message(), existing_draft=orphan)
    res = _with_fake_service(fake2, lambda: gmail_client.update_draft(
        "jo.ambrose4@example.com", "dY", [], "", "body", reply_to_message_id="orig1"))
    did, body = fake2.updated[-1]
    assert body["message"]["threadId"] == "thr9" and res["in_reply_to"] == "<bo.umberfield72@example.com>"


def test_reply_flags_are_registered_and_to_subject_optional_for_replies():
    src = (HERE.parent / "preprocessor" / "gmail_client.py").read_text()
    for flag in ("--reply-to-message-id", "--thread-id"):
        assert src.count(f'"{flag}"') >= 2, flag        # create-draft and update-draft
    assert "--to is required unless --reply-to-message-id" in src
    doc = (HERE.parent / "docs" / "ea" / "gmail.md").read_text()
    # The connector's draft tools are denied; the guide names only the CLI.
    assert "--reply-to-message-id" in doc and "replyToMessageId" not in doc
    assert "`create_draft` and `update_draft` are denied" in doc



# ---------------------------------------------------------------------------
# Quoted history: a reply quotes the message it answers below the new text,
# an update keeps that quote (and the draft's attachments), and a body that
# already ends in a quote gets no second one.
# ---------------------------------------------------------------------------

def _original_with_text(text="Agenda for tomorrow:\n1. sim\n\n> older line"):
    msg = _original_message()
    msg["payload"]["mimeType"] = "text/plain"
    msg["payload"]["body"] = {"data": base64.urlsafe_b64encode(text.encode()).decode()}
    msg["payload"]["headers"].append({"name": "Date", "value": "Wed, 30 Sep 2026 04:18:36 +0000"})
    return msg


def _text_of(body: dict) -> str:
    m = _parsed(body)
    part = next(p for p in m.walk() if p.get_content_type() == "text/plain")
    return part.get_payload(decode=True).decode()


def test_a_reply_quotes_the_message_it_answers():
    fake = _ThreadingService(_original_with_text())
    res = _with_fake_service(fake, lambda: gmail_client.create_draft(
        "jo.ambrose4@example.com", [], "", "Hi Tam,\n\nSee below.\n\nKit\n",
        reply_to_message_id="orig1"))
    text = _text_of(fake.created[-1])
    head, _, quote = text.partition("\n\nOn ")
    assert head == "Hi Tam,\n\nSee below.\n\nKit" and res["quoted"] is True
    attribution, _, quoted = quote.partition("\n")
    assert attribution.endswith("Tam Prescott <tam@example.org> wrote:")
    assert ", 2026 at " in attribution
    assert quoted.splitlines() == ["> Agenda for tomorrow:", "> 1. sim", ">", "> > older line"]


def test_no_quote_and_a_body_that_already_quotes_get_no_quote():
    fake = _ThreadingService(_original_with_text())
    res = _with_fake_service(fake, lambda: gmail_client.create_draft(
        "jo.ambrose4@example.com", [], "", "Short reply.", reply_to_message_id="orig1",
        quote=False))
    assert _text_of(fake.created[-1]) == "Short reply." and res["quoted"] is False
    own = "Reply.\n\nOn Tue, Sep 29, 2026 at 9:18 PM Tam wrote:\n> hi\n"
    _with_fake_service(fake, lambda: gmail_client.create_draft(
        "jo.ambrose4@example.com", [], "", own, reply_to_message_id="orig1"))
    assert _text_of(fake.created[-1]) == own
    # A new conversation quotes nothing.
    _with_fake_service(fake, lambda: gmail_client.create_draft(
        "jo.ambrose4@example.com", ["flo.thornbury67@example.org"], "Hello", "x"))
    assert _text_of(fake.created[-1]) == "x"


def _existing_reply(text: str, attachment: bytes = b""):
    """A reply draft as drafts().get returns it: the payload (format=full)
    and, when it carries a file, the raw MIME (format=raw)."""
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    headers = [{"name": "In-Reply-To", "value": "<bo.umberfield72@example.com>"},
               {"name": "References", "value": "<jules.foxworth35@example.com> <bo.umberfield72@example.com>"},
               {"name": "To", "value": "tam@example.org"},
               {"name": "Subject", "value": "Re: Advising - Kit Calloway"}]
    message = {"id": "mX", "threadId": "thr9", "payload": {
        "mimeType": "text/plain", "headers": headers,
        "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}}
    if attachment:
        mime = MIMEMultipart("mixed")
        mime.attach(MIMEText(text, "plain", "utf-8"))
        part = MIMEApplication(attachment, "pdf")
        part.add_header("Content-Disposition", "attachment", filename="letter.pdf")
        mime.attach(part)
        message["raw"] = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    return {"id": "dX", "message": message}


def test_update_keeps_the_quote_and_the_attachments():
    old = ("First try.\n\nOn Tue, Sep 29, 2026 at 9:18 PM Tam Prescott <\n"
           "tam@example.org> wrote:\n> Agenda\n>\n> 1. sim\n")
    fake = _ThreadingService(_original_message(),
                             existing_draft=_existing_reply(old, b"%PDF-1.7 letter"))
    res = _with_fake_service(fake, lambda: gmail_client.update_draft(
        "jo.ambrose4@example.com", "dX", [], "", "Second try."))
    _, body = fake.updated[-1]
    m = _parsed(body)
    assert m.get_content_type() == "multipart/mixed" and res["attachments_kept"] == 1
    assert m["In-Reply-To"] == "<bo.umberfield72@example.com>" and m["To"] == "tam@example.org"
    files = [(p.get_filename(), p.get_payload(decode=True)) for p in m.walk()
             if p.get_content_disposition() == "attachment"]
    assert files == [("letter.pdf", b"%PDF-1.7 letter")]
    assert _text_of(body) == ("Second try.\n\nOn Tue, Sep 29, 2026 at 9:18 PM Tam "
                              "Prescott <\ntam@example.org> wrote:\n> Agenda\n>\n> 1. sim\n")
    assert res["quoted"] is True
    # --no-quote drops it; a draft with no file stays one plain part.
    fake2 = _ThreadingService(_original_message(), existing_draft=_existing_reply(old))
    res2 = _with_fake_service(fake2, lambda: gmail_client.update_draft(
        "jo.ambrose4@example.com", "dX", [], "", "Third.", quote=False))
    assert _text_of(fake2.updated[-1][1]) == "Third." and res2["attachments_kept"] == 0
    assert _parsed(fake2.updated[-1][1]).get_content_type() == "text/plain"


def test_get_draft_reports_redirects_in_the_new_text_only():
    text = ("See https://www.google.com/url?q=https://arxiv.org/abs/2102.01295&sa=E\n\n"
            "On Tue, Sep 29, 2026 at 9:18 PM Tam wrote:\n"
            "> map https://www.google.com/url?q=https://maps.app.goo.gl/x&source=calendar\n")
    html = ('<div>See <a href="https://www.google.com/url?q=https://nsf.gov&amp;sa=E">NSF</a></div>'
            '<div class="gmail_quote">quoted <a href="https://www.google.com/url?q=https://q.org">q</a></div>')
    draft = {"id": "dR", "message": {"id": "mR", "threadId": "t", "payload": {
        "mimeType": "multipart/alternative", "headers": [{"name": "To", "value": "flo.thornbury67@example.org"}],
        "parts": [
            {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}},
            {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()}}]}}}
    fake = _ThreadingService({"id": "none"}, existing_draft=draft)
    res = _with_fake_service(fake, lambda: gmail_client.get_draft("jo.ambrose4@example.com", "dR"))
    assert res["redirects"] == [
        "https://www.google.com/url?q=https://arxiv.org/abs/2102.01295&sa=E",
        "https://www.google.com/url?q=https://nsf.gov&amp;sa=E"], res["redirects"]


def test_no_quote_flag_is_registered_on_both_draft_verbs():
    src = (HERE.parent / "preprocessor" / "gmail_client.py").read_text()
    assert src.count('"--no-quote"') == 2
    assert "quote=not args.no_quote" in src

# ---------------------------------------------------------------------------
# Layer E — attach-file. The Gmail API has no attachment endpoint, so the
# draft is read back raw and rebuilt as multipart/mixed with the files
# added; id, thread and headers survive, and the mediator allows the verb.
# ---------------------------------------------------------------------------

class _AttachService:
    def __init__(self, raw_mime: bytes, thread_id: str = "thr1"):
        self.raw = base64.urlsafe_b64encode(raw_mime).decode("ascii")
        self.thread_id = thread_id
        self.updated = []

    def users(self): return self
    def drafts(self): return self

    def get(self, userId=None, id=None, format=None):
        assert format == "raw"
        return _Exec({"id": id, "message": {"id": "mA", "threadId": self.thread_id, "raw": self.raw}})

    def update(self, userId=None, id=None, body=None):
        self.updated.append((id, body))
        return _Exec({"id": id, "message": {"id": "mB", "threadId": body["message"].get("threadId", "")}})


def _draft_mime() -> bytes:
    from email.mime.text import MIMEText
    m = MIMEText("Hi Tam,\n\nPhotos are attached.\n\nKit", "plain", "utf-8")
    m["To"] = "blake.lindqvist56@example.com"; m["Subject"] = "Re: Inspection INSP-2026-0000001"
    m["From"] = "jo.ambrose4@example.com"; m["In-Reply-To"] = "<jules.prescott77@example.com>"
    m["References"] = "<jules.foxworth35@example.com> <jules.prescott77@example.com>"
    return m.as_bytes()


def _parsed(body: dict):
    import email as _email
    return _email.message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]))


def test_attach_file_keeps_headers_thread_and_body_and_adds_parts():
    fake = _AttachService(_draft_mime(), thread_id="thr1")
    with tempfile.TemporaryDirectory() as td:
        f1 = Path(td) / "receipt.pdf"; f1.write_bytes(b"%PDF-1.4 receipt")
        f2 = Path(td) / "sign.txt"; f2.write_text("EXIT")
        res = _with_fake_service(fake, lambda: gmail_client.attach_files(
            "jo.ambrose4@example.com", "dX", [str(f1), str(f2)], names=[None, "exit-sign.txt"]))
    assert res["draft_id"] == "dX" and res["thread_id"] == "thr1"
    assert res["attached"] == ["receipt.pdf", "exit-sign.txt"] and res["attachment_bytes"] == len(b"%PDF-1.4 receipt") + 4
    did, body = fake.updated[-1]
    assert did == "dX" and body["message"]["threadId"] == "thr1"
    m = _parsed(body)
    assert m.get_content_type() == "multipart/mixed"
    assert m["To"] == "blake.lindqvist56@example.com" and m["In-Reply-To"] == "<jules.prescott77@example.com>"
    assert m["References"] == "<jules.foxworth35@example.com> <jules.prescott77@example.com>"
    parts = m.get_payload()
    assert parts[0].get_content_type() == "text/plain" and "Photos are attached" in parts[0].get_payload(decode=True).decode()
    names = [p.get_filename() for p in parts[1:]]
    assert names == ["receipt.pdf", "exit-sign.txt"]
    assert parts[1].get_content_type() == "application/pdf"
    assert parts[1].get_payload(decode=True) == b"%PDF-1.4 receipt"


def test_attach_file_keeps_existing_attachments_when_called_twice():
    fake = _AttachService(_draft_mime())
    with tempfile.TemporaryDirectory() as td:
        f1 = Path(td) / "a.txt"; f1.write_text("a")
        f2 = Path(td) / "b.txt"; f2.write_text("b")
        _with_fake_service(fake, lambda: gmail_client.attach_files("kit.kowalczyk1@example.com", "dX", [str(f1)]))
        # the second call reads the draft back as it now is
        fake.raw = fake.updated[-1][1]["message"]["raw"]
        _with_fake_service(fake, lambda: gmail_client.attach_files("kit.kowalczyk1@example.com", "dX", [str(f2)]))
    m = _parsed(fake.updated[-1][1])
    assert [p.get_filename() for p in m.get_payload()[1:]] == ["a.txt", "b.txt"]


def test_attach_file_refuses_missing_files_and_oversize_without_touching_the_draft():
    fake = _AttachService(_draft_mime())
    res = _with_fake_service(fake, lambda: gmail_client.attach_files("kit.kowalczyk1@example.com", "dX", ["/nonexistent/x.pdf"]))
    assert "file not found" in res["error"] and fake.updated == []
    real = gmail_client.GMAIL_ATTACHMENT_LIMIT_BYTES
    gmail_client.GMAIL_ATTACHMENT_LIMIT_BYTES = 10
    try:
        with tempfile.TemporaryDirectory() as td:
            big = Path(td) / "big.bin"; big.write_bytes(b"x" * 11)
            res = _with_fake_service(fake, lambda: gmail_client.attach_files("kit.kowalczyk1@example.com", "dX", [str(big)]))
        assert "25 MB" in res["error"] and fake.updated == []
    finally:
        gmail_client.GMAIL_ATTACHMENT_LIMIT_BYTES = real


def test_attach_file_shrinks_photos_when_asked():
    try:
        from PIL import Image
    except Exception:
        return  # no Pillow here; the client keeps originals in that case
    fake = _AttachService(_draft_mime())
    with tempfile.TemporaryDirectory() as td:
        photo = Path(td) / "IMG_1.png"
        Image.new("RGB", (4000, 3000), (200, 30, 30)).save(photo)
        original = photo.stat().st_size
        res = _with_fake_service(fake, lambda: gmail_client.attach_files(
            "kit.kowalczyk1@example.com", "dX", [str(photo)], max_image_px=1600))
    assert res["attached"] == ["IMG_1.jpg"] and res["attachment_bytes"] < original
    part = _parsed(fake.updated[-1][1]).get_payload()[1]
    assert part.get_content_type() == "image/jpeg"
    im = Image.open(__import__("io").BytesIO(part.get_payload(decode=True)))
    assert max(im.size) == 1600


def test_attach_file_is_registered_and_mediated():
    import mail_mediation as M
    src = (HERE.parent / "preprocessor" / "gmail_client.py").read_text()
    assert 'sub.add_parser("attach-file"' in src
    assert M.is_safe("gmail_client", "attach-file") and not M.is_gated("gmail_client", "attach-file")
    assert "--file" in M.FILE_FLAGS           # staged and confined by the mediator
    doc = (HERE.parent / "docs" / "ea" / "gmail.md").read_text()
    assert "gmail_client.py attach-file" in doc

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
