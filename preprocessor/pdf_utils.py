"""
PDF attachment scanner for the EA preprocessor.

Extracts actionable information from PDF attachments on emails:
- Submission email addresses and instructions
- Deadlines and due dates
- Required documents or forms
- Portal URLs

Runs at zero Claude tokens — text extraction + regex, no model call.
"""

import base64
import io
import re
import shutil
import subprocess
import sys
from typing import Optional

# Extraction backends, in preference order: the maintained `pypdf`, the
# legacy `PyPDF2` name, then poppler's pdftotext. A host with none of them
# must never return an empty string that reads like "this PDF holds no
# text"; extract_text_from_pdf keeps the two apart.
try:
    from pypdf import PdfReader
    _PDF_LIB = "pypdf"
except ImportError:
    try:
        from PyPDF2 import PdfReader
        _PDF_LIB = "PyPDF2"
    except ImportError:
        PdfReader = None
        _PDF_LIB = ""

PDFTOTEXT = shutil.which("pdftotext")
# Kept for callers that only ask "can this host read a PDF at all?".
HAS_PDF = bool(_PDF_LIB or PDFTOTEXT)

# extract_text_from_pdf statuses.
OK = "ok"                       # an extractor ran and produced text
EMPTY = "empty"                 # it ran clean; the PDF genuinely has no text
NO_EXTRACTOR = "no_extractor"   # nothing on this host can read a PDF


# Patterns for actionable info buried in PDFs.
EMAIL_PATTERN = re.compile(
    r'[\w.+-]+@[\w-]+\.[\w.-]+',
    re.IGNORECASE,
)

# Submission / send-to instructions
SUBMISSION_PATTERNS = [
    re.compile(r'(?:submit|send|email|forward|deliver|return)\s+(?:to|your|a|the|materials?|application|documents?|cv|letter|interest)\b[^.]{0,80}?(' + EMAIL_PATTERN.pattern + r')', re.IGNORECASE),
    re.compile(r'(' + EMAIL_PATTERN.pattern + r')[^.]{0,40}?(?:submit|send|email|forward|application|materials)', re.IGNORECASE),
    re.compile(r'(?:applications?\s+(?:are|should be)\s+(?:reviewed|sent|submitted|emailed))[^.]{0,80}?(' + EMAIL_PATTERN.pattern + r')', re.IGNORECASE),
]

# Deadline patterns
DEADLINE_PATTERNS = [
    re.compile(r'(?:deadline|due date|due by|submit by|no later than|before)\s*[:\-]?\s*(.{5,40}?)(?:\.|$)', re.IGNORECASE),
    re.compile(r'(?:by|before|until)\s+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4})', re.IGNORECASE),
]

# Required documents
REQUIRED_DOC_PATTERNS = [
    re.compile(r'(?:please\s+(?:submit|provide|include|attach)|required\s+(?:documents?|materials?))[:\s]+(.{10,200}?)(?:\n\n|\.\s)', re.IGNORECASE | re.DOTALL),
    re.compile(r'(?:application\s+(?:requires?|consists?\s+of|includes?)|to\s+apply[,:])\s*(.{10,200}?)(?:\n\n|\.\s)', re.IGNORECASE | re.DOTALL),
]

# Portal / upload URLs
PORTAL_PATTERNS = [
    re.compile(r'(?:submit|upload|apply|portal|online\s+(?:form|application|system))[^.]{0,60}?(https?://[\w./\-?&=%#@!+~]+)', re.IGNORECASE),
    re.compile(r'(https?://[\w./\-?&=%#@!+~]+)[^.]{0,60}?(?:submit|upload|apply|portal|application)', re.IGNORECASE),
]


def _extract_with_library(pdf_bytes: bytes, max_pages: int) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = reader.pages[:max_pages]
    return "\n".join(page.extract_text() or "" for page in pages)


def _extract_with_pdftotext(pdf_bytes: bytes, max_pages: int) -> str:
    """poppler's pdftotext, reading the PDF on stdin and writing to stdout.

    Nothing touches the filesystem: the bytes come from a Graph attachment
    and the resident must not be able to steer a temp-file path.
    """
    proc = subprocess.run(
        [PDFTOTEXT, "-q", "-f", "1", "-l", str(max_pages), "-", "-"],
        input=pdf_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
            or f"pdftotext exit {proc.returncode}")
    return proc.stdout.decode("utf-8", "replace")


def extract_text_from_pdf(pdf_bytes: bytes,
                          max_pages: int = 10) -> tuple[str, str]:
    """Extract text from PDF bytes. Returns (text, status).

    status is OK, EMPTY, NO_EXTRACTOR, or "failed: <reason>". The split
    matters: a caller acting on "no text" must be able to tell a scanned
    image PDF (EMPTY, there really is nothing to read) from an extraction
    that never happened (NO_EXTRACTOR / failed).

    Every non-OK status is also reported on stderr, so an empty return is
    never silent.
    """
    reasons = []
    ran_clean = False
    for name, fn in ((_PDF_LIB, _extract_with_library),
                     ("pdftotext" if PDFTOTEXT else "", _extract_with_pdftotext)):
        if not name:
            continue                      # that backend is not on this host
        try:
            text = fn(pdf_bytes, max_pages)
        except Exception as e:
            reasons.append(f"{name}: {type(e).__name__}: {e}")
            continue
        ran_clean = True
        if text.strip():
            return text, OK

    if ran_clean and not reasons:
        # Every backend present ran and found nothing: a scan, or a PDF whose
        # text lives in images. That IS "no text in this PDF".
        print("  PDF holds no extractable text (scanned image?)",
              file=sys.stderr)
        return "", EMPTY
    if reasons:
        # Something errored, so "no text" is not an answer we can stand
        # behind, even if another backend returned empty cleanly.
        print("  PDF extraction failed: " + "; ".join(reasons), file=sys.stderr)
        return "", "failed: " + "; ".join(reasons)
    print("  PDF extraction unavailable: install pypdf (requirements.txt) "
          "or poppler-utils for pdftotext", file=sys.stderr)
    return "", NO_EXTRACTOR


def extract_text_from_pdf_bytes(pdf_bytes: bytes, max_pages: int = 10) -> str:
    """Text only, for callers that do not care why it is empty.

    Prefer extract_text_from_pdf: this one cannot tell you whether the PDF
    was empty or unreadable.
    """
    return extract_text_from_pdf(pdf_bytes, max_pages)[0]


def extract_actionable_info(text: str) -> dict:
    """
    Scan PDF text for actionable information.
    Returns a dict with found items; empty dict if nothing actionable.
    """
    if not text.strip():
        return {}

    info = {}

    all_emails = set(EMAIL_PATTERN.findall(text))
    # Strip trailing periods and drop image/document filenames.
    all_emails = {
        e.rstrip('.')
        for e in all_emails
        if not e.rstrip('.').endswith(('.png', '.jpg', '.gif', '.pdf'))
        and '@' in e
        and '.' in e.split('@')[1]
    }

    # Find submission-specific email addresses (with surrounding context)
    submission_emails = set()
    submission_context = []
    for pattern in SUBMISSION_PATTERNS:
        for match in pattern.finditer(text):
            email = match.group(1).rstrip('.')
            submission_emails.add(email)
            ctx = match.group(0).strip()
            if len(ctx) > 200:
                ctx = ctx[:200]
            submission_context.append(ctx)

    if submission_emails:
        info["submission_emails"] = sorted(submission_emails)
        info["submission_instructions"] = submission_context[:3]

    # Non-submission emails that might still be relevant contacts
    other_emails = all_emails - submission_emails
    # Filter to likely-relevant ones (not generic image/tracking addresses)
    other_emails = {
        e for e in other_emails
        if not any(skip in e.lower() for skip in [
            'noreply', 'no-reply', 'donotreply', 'tracking',
            'unsubscribe', 'bounce', 'mailer',
        ])
    }
    if other_emails:
        info["other_contacts"] = sorted(other_emails)

    deadlines = []
    for pattern in DEADLINE_PATTERNS:
        for match in pattern.finditer(text):
            deadline_text = match.group(1).strip() if match.lastindex else match.group(0).strip()
            if len(deadline_text) > 5:
                deadlines.append(deadline_text)
    if deadlines:
        info["deadlines"] = deadlines[:3]

    required_docs = []
    for pattern in REQUIRED_DOC_PATTERNS:
        for match in pattern.finditer(text):
            doc_text = match.group(1).strip()
            if len(doc_text) > 10:
                required_docs.append(doc_text)
    if required_docs:
        info["required_documents"] = required_docs[:3]

    portal_urls = set()
    for pattern in PORTAL_PATTERNS:
        for match in pattern.finditer(text):
            url = match.group(1)
            portal_urls.add(url)
    if portal_urls:
        info["portal_urls"] = sorted(portal_urls)

    return info


def scan_message_pdf_attachments(
    message_id: str,
    graph_module,
    max_pdfs: int = 3,
    max_size_bytes: int = 2_000_000,
) -> Optional[dict]:
    """
    Fetch PDF attachments for a message and extract actionable info.

    Args:
        message_id: Outlook message ID
        graph_module: The graph_client module (to avoid circular imports)
        max_pdfs: Max number of PDFs to scan per message
        max_size_bytes: Skip PDFs larger than this (2MB default)

    Returns:
        Dict with attachment hints, or None if nothing actionable found.
    """
    try:
        attachments = graph_module.get_attachments(message_id)
    except Exception as e:
        print(f"  Failed to fetch attachments for ...{message_id[-20:]}: {e}", file=sys.stderr)
        return None

    pdf_attachments = [
        a for a in attachments
        if a.get("contentType", "").lower() == "application/pdf"
        or (a.get("name", "").lower().endswith(".pdf"))
    ]

    if not pdf_attachments:
        return None

    combined_info = {}
    scanned_names = []
    unreadable = []

    for att in pdf_attachments[:max_pdfs]:
        name = att.get("name", "unknown.pdf")
        size = att.get("size", 0)

        if size > max_size_bytes:
            print(f"  Skipping large PDF: {name} ({size} bytes)", file=sys.stderr)
            continue

        # Get content bytes (inline attachments have contentBytes directly)
        content_b64 = att.get("contentBytes", "")
        if not content_b64:
            continue

        try:
            pdf_bytes = base64.b64decode(content_b64)
        except Exception:
            continue

        text, status = extract_text_from_pdf(pdf_bytes)
        if not text:
            # EMPTY means the PDF really holds no text. Any other status
            # means it was never read, which the caller must hear about.
            if status != EMPTY:
                unreadable.append({"name": name, "reason": status})
            continue

        info = extract_actionable_info(text)
        if info:
            scanned_names.append(name)
            for key, value in info.items():
                if key not in combined_info:
                    combined_info[key] = value
                elif isinstance(value, list):
                    combined_info[key] = list(set(combined_info[key] + value))

    if not combined_info and not unreadable:
        return None

    if scanned_names:
        combined_info["scanned_attachments"] = scanned_names
    if unreadable:
        # Reported even when nothing else was found: "I could not read this"
        # is the one hint a caller must not mistake for "there was nothing
        # in it".
        combined_info["unreadable_attachments"] = unreadable
    return combined_info
