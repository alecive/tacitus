#!/usr/bin/env python3
"""PDF text extraction, and the difference between empty and unread.

A missing python PDF library must not make every PDF read as empty: an
ImportError swallowed at import returns "" before anything looks at the
bytes, and scan-attachments then reports "no actionable info found" for
attachments it never opened.

Two things are pinned here: the pdftotext fallback (so a host with no
python PDF library still reads PDFs), and the status split, so a caller
can tell "this PDF holds no text" from "extraction did not happen".
"""

import io
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch

os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-pdf-")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import pdf_utils as P  # noqa: E402


def _pdf_with_text(text: str) -> bytes:
    """A minimal one-page PDF carrying `text` in an uncompressed stream."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n"
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n")
    return out.encode("latin-1")


def test_a_text_pdf_extracts_on_this_host():
    """Real text out of a real PDF, whatever combination of pypdf /
    pdftotext this host happens to have."""
    text, status = P.extract_text_from_pdf(_pdf_with_text("Rechnung 3460"))
    assert status == P.OK, status
    assert "Rechnung" in text


def test_pdftotext_alone_is_enough():
    """With the python library path forced off, poppler alone must still
    extract the text."""
    if not P.PDFTOTEXT:
        return                       # nothing to fall back to; nothing to pin
    with patch.object(P, "_PDF_LIB", ""):
        text, status = P.extract_text_from_pdf(_pdf_with_text("Motel One"))
    assert status == P.OK and "Motel One" in text


def test_the_library_result_wins_when_it_finds_text():
    called = []

    def fake_lib(pdf_bytes, max_pages):
        called.append("lib")
        return "from the library"

    def fake_poppler(pdf_bytes, max_pages):
        called.append("poppler")
        return "from poppler"

    with patch.object(P, "_PDF_LIB", "pypdf"), \
            patch.object(P, "_extract_with_library", fake_lib), \
            patch.object(P, "_extract_with_pdftotext", fake_poppler):
        text, status = P.extract_text_from_pdf(b"%PDF-1.4")
    assert (text, status) == ("from the library", P.OK)
    assert called == ["lib"], "pdftotext ran even though the library answered"


def test_library_returning_nothing_falls_through_to_pdftotext():
    """The shape of the real failure: a library that loads but yields no text
    (image-only page objects, a parser that gives up) must not end the run."""
    # PDFTOTEXT is patched too: the fallback is consulted only when a binary
    # is known, and a host without poppler would otherwise skip it.
    with patch.object(P, "_PDF_LIB", "pypdf"), \
            patch.object(P, "PDFTOTEXT", "/usr/bin/pdftotext"), \
            patch.object(P, "_extract_with_library", lambda b, n: ""), \
            patch.object(P, "_extract_with_pdftotext", lambda b, n: "poppler got it"):
        text, status = P.extract_text_from_pdf(b"%PDF-1.4")
    assert (text, status) == ("poppler got it", P.OK)


def test_an_image_only_pdf_reports_empty_not_failure():
    with patch.object(P, "_PDF_LIB", "pypdf"), \
            patch.object(P, "_extract_with_library", lambda b, n: "  \n "), \
            patch.object(P, "PDFTOTEXT", None):
        text, status = P.extract_text_from_pdf(b"%PDF-1.4")
    assert (text, status) == ("", P.EMPTY)


def test_no_extractor_is_not_reported_as_an_empty_pdf():
    """With no extractor installed, the answer is NO_EXTRACTOR, never a
    quiet "this PDF has no text"."""
    err = io.StringIO()
    with patch.object(P, "_PDF_LIB", ""), patch.object(P, "PDFTOTEXT", None), \
            patch("sys.stderr", err):
        text, status = P.extract_text_from_pdf(b"%PDF-1.4")
    assert (text, status) == ("", P.NO_EXTRACTOR)
    assert "unavailable" in err.getvalue()          # never silent


def test_a_broken_pdf_reports_failure_with_the_reason():
    err = io.StringIO()
    with patch("sys.stderr", err):
        text, status = P.extract_text_from_pdf(b"this is not a pdf")
    assert text == ""
    assert status.startswith("failed:"), status
    assert "failed" in err.getvalue()


def test_the_text_only_wrapper_still_returns_a_string():
    """Old callers keep working; they just cannot tell the empties apart."""
    assert P.extract_text_from_pdf_bytes(_pdf_with_text("hi there")).strip()
    with patch.object(P, "_PDF_LIB", ""), patch.object(P, "PDFTOTEXT", None):
        assert P.extract_text_from_pdf_bytes(b"%PDF-1.4") == ""


def test_max_pages_is_passed_to_pdftotext():
    seen = {}
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return real_run([sys.executable, "-c", "print('ok')"],
                        stdout=kw.get("stdout"), stderr=kw.get("stderr"))

    if not P.PDFTOTEXT:
        return
    with patch.object(P.subprocess, "run", fake_run):
        P._extract_with_pdftotext(b"%PDF-1.4", 3)
    assert "-l" in seen["cmd"] and seen["cmd"][seen["cmd"].index("-l") + 1] == "3"


# ── what the scanner tells its caller ──────────────────────────────────

class _FakeGraph:
    def __init__(self, attachments):
        self._a = attachments

    def get_attachments(self, message_id):
        return self._a


def _attachment(name, data: bytes):
    import base64
    return {"name": name, "contentType": "application/pdf",
            "size": len(data), "contentBytes": base64.b64encode(data).decode()}


def test_scan_reports_a_pdf_it_could_not_read():
    """A PDF the scanner cannot read is reported. Skipping it and
    returning None reads as "nothing actionable in the attachments", the
    same answer a genuinely empty PDF gives."""
    graph = _FakeGraph([_attachment("invoice.pdf", b"not a pdf")])
    hints = P.scan_message_pdf_attachments("m1", graph)
    assert hints and hints["unreadable_attachments"][0]["name"] == "invoice.pdf"
    assert hints["unreadable_attachments"][0]["reason"].startswith("failed:")


def test_scan_stays_quiet_about_a_pdf_that_is_simply_empty():
    graph = _FakeGraph([_attachment("scan.pdf", _pdf_with_text(" "))])
    with patch.object(P, "extract_text_from_pdf", lambda b, *a: ("", P.EMPTY)):
        assert P.scan_message_pdf_attachments("m1", graph) is None


def test_scan_still_returns_the_hints_it_finds():
    pdf = _pdf_with_text("Send materials to nico.ellery6@example.edu to apply")
    graph = _FakeGraph([_attachment("call.pdf", pdf)])
    hints = P.scan_message_pdf_attachments("m1", graph)
    assert hints and hints["scanned_attachments"] == ["call.pdf"]
    assert "unreadable_attachments" not in hints
    found = (hints.get("submission_emails") or []) + (hints.get("other_contacts") or [])
    assert "nico.ellery6@example.edu" in found



def test_this_host_can_actually_extract_a_pdf():
    """The wall's guard against a silent-empty host.

    Every other test in this file supplies its own backend, so none of
    them notices a host with no working extractor at all. This one
    asserts the REAL host has one, and is deliberately not mocked. pypdf
    is optional and absent here (PEP 668), so poppler's pdftotext carries
    it; if poppler is ever removed, this goes red instead of the
    assistant going blind to attachments.
    """
    import pdf_utils as real
    assert real.HAS_PDF and (real.PDFTOTEXT or real._PDF_LIB), (
        "no PDF text extractor on this host: install poppler-utils for "
        "pdftotext, or pypdf into an environment that accepts it. Without "
        "one, every PDF extracts to an empty string.")

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
