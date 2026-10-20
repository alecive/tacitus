#!/usr/bin/env python3
"""
Unit tests for preprocessor/context_export.py.

Tests pure functions (html_to_text, extract_message_ids, find_related_ledger_entries)
and the package subcommand. Tests that depend on Graph API are skipped.
"""

import argparse
import json
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))

from context_export import (
    html_to_text,
    extract_message_ids,
    find_related_ledger_entries,
    cmd_package,
)


# ---------------------------------------------------------------------------
# html_to_text
# ---------------------------------------------------------------------------

def test_html_to_text_basic():
    """Plain text passthrough."""
    assert html_to_text("Hello world") == "Hello world"


def test_html_to_text_strips_tags():
    """HTML tags removed, text preserved."""
    result = html_to_text("<b>Bold</b> and <i>italic</i>")
    assert result == "Bold and italic"


def test_html_to_text_block_elements_add_newlines():
    """Block elements (p, div, br, tr, li) should create newlines."""
    html = "<p>Paragraph 1</p><p>Paragraph 2</p>"
    result = html_to_text(html)
    assert "Paragraph 1" in result
    assert "Paragraph 2" in result
    assert "\n" in result


def test_html_to_text_br_newline():
    """<br> should create a newline."""
    result = html_to_text("Line 1<br>Line 2")
    assert "Line 1\nLine 2" in result


def test_html_to_text_strips_style_and_script():
    """Style and script content should be removed."""
    html = "<style>.foo { color: red; }</style><p>Visible</p><script>alert('x')</script>"
    result = html_to_text(html)
    assert "Visible" in result
    assert "color" not in result
    assert "alert" not in result


def test_html_to_text_collapses_blank_lines():
    """Multiple consecutive blank lines should collapse to two newlines."""
    html = "<p>A</p><p></p><p></p><p></p><p>B</p>"
    result = html_to_text(html)
    assert "\n\n\n" not in result
    assert "A" in result
    assert "B" in result


def test_html_to_text_table_rows():
    """Table rows should get newlines between them."""
    html = "<table><tr><td>Row 1</td></tr><tr><td>Row 2</td></tr></table>"
    result = html_to_text(html)
    assert "Row 1" in result
    assert "Row 2" in result


def test_html_to_text_list_items():
    """List items should get newlines."""
    html = "<ul><li>Item A</li><li>Item B</li><li>Item C</li></ul>"
    result = html_to_text(html)
    lines = [l.strip() for l in result.split("\n") if l.strip()]
    assert "Item A" in lines
    assert "Item B" in lines
    assert "Item C" in lines


def test_html_to_text_real_email():
    """Realistic Outlook email HTML."""
    html = """<html><head><style>body{font-family:Calibri}</style></head>
    <body>
    <p>Hi Kit,</p>
    <p>Here is the draft for your review.<br>Let me know if you have questions.</p>
    <p>Best,<br>Dana</p>
    </body></html>"""
    result = html_to_text(html)
    assert "Hi Kit," in result
    assert "draft for your review" in result
    assert "Best," in result
    assert "Dana" in result
    assert "font-family" not in result


# ---------------------------------------------------------------------------
# extract_message_ids
# ---------------------------------------------------------------------------

def test_extract_message_ids_basic():
    """Finds AAMk IDs in text."""
    text = "Check this email AAMkAQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ about the meeting"
    ids = extract_message_ids(text)
    assert len(ids) == 1
    assert ids[0].startswith("AAMk")


def test_extract_message_ids_multiple():
    """Finds multiple distinct IDs."""
    text = ("First: AAMkAQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ "
            "Second: AAMkQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ "
            "Dupe: AAMkAQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ")
    ids = extract_message_ids(text)
    assert len(ids) == 2  # deduped


def test_extract_message_ids_none():
    """No IDs in text returns empty list."""
    ids = extract_message_ids("Just a normal message with no email references")
    assert ids == []


def test_extract_message_ids_short_rejected():
    """Too-short AAMk strings should not match (need 20+ chars after prefix)."""
    ids = extract_message_ids("AAMkShort")
    assert ids == []


def test_extract_message_ids_in_url():
    """IDs embedded in URLs should still be found."""
    text = "https://outlook.office365.com/mail/AAMkAQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ/view"
    ids = extract_message_ids(text)
    assert len(ids) == 1


def test_extract_message_ids_with_special_chars():
    """IDs with base64 chars (+, /, =) should match."""
    text = "ID: AAMkAQQQQQQQ+QQQQQQQQ/QQQQ=QQQQ"
    ids = extract_message_ids(text)
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# find_related_ledger_entries (with mocked DB)
# ---------------------------------------------------------------------------

def _mock_ledger_entries():
    """Create test ledger entries for matching tests."""
    return [
        {
            "id": "reply-nsf-001",
            "type": "needs_response",
            "status": "open",
            "description": "Reply to Avery Merriweather about NSF CAREER proposal budget justification",
            "source_message_id": "AAMkNSFCareer001",
            "conversation_id": "AAQkNSFConv001",
            "draft_id": "",
            "from_email": "noor.prescott40@example.edu",
        },
        {
            "id": "lor-smith-002",
            "type": "lor_upload",
            "status": "open",
            "description": "Upload letter of recommendation for Sarah Smith to GRFP portal",
            "source_message_id": "AAMkLORSmith002",
            "conversation_id": "",
            "draft_id": "",
            "from_email": "bo.whitlock30@example.edu",
        },
        {
            "id": "sched-meeting-003",
            "type": "scheduling",
            "status": "completed",
            "description": "Schedule faculty retreat planning meeting with department",
            "source_message_id": "AAMkSchedRetreat003",
            "conversation_id": "",
            "draft_id": "",
            "from_email": "",
        },
    ]


def _make_mock_db(entries):
    """Create an in-memory SQLite DB matching the real ledger schema."""
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    # Use the real schema from ledger.py
    db.executescript("""
        CREATE TABLE IF NOT EXISTS ledger (
            id              TEXT PRIMARY KEY,
            type            TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'open',
            description     TEXT,
            from_email      TEXT,
            from_name       TEXT,
            source_message_id TEXT,
            conversation_id TEXT,
            date_identified TEXT,
            deadline        TEXT,
            follow_up_after TEXT,
            snooze_until    TEXT,
            last_action     TEXT,
            notes           TEXT,
            priority        TEXT,
            email_archived  INTEGER NOT NULL DEFAULT 0,
            metadata        TEXT DEFAULT '{}',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            closed_at       TEXT
        );
    """)
    for e in entries:
        db.execute(
            "INSERT INTO ledger (id, type, status, description, source_message_id, "
            "conversation_id, from_email, created_at, updated_at, email_archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '', '', 0)",
            (e["id"], e["type"], e["status"], e["description"],
             e["source_message_id"], e["conversation_id"], e["from_email"]))
    db.commit()
    return db


def test_find_related_by_message_id(monkeypatch):
    """Exact match on source_message_id."""
    entries = _mock_ledger_entries()
    monkeypatch.setattr("context_export.ledger_mod._get_db",
                        lambda: _make_mock_db(entries))
    results = find_related_ledger_entries(["AAMkNSFCareer001"], "")
    assert len(results) == 1
    assert results[0]["id"] == "reply-nsf-001"


def test_find_related_by_conversation_id(monkeypatch):
    """Match on conversation_id."""
    entries = _mock_ledger_entries()
    monkeypatch.setattr("context_export.ledger_mod._get_db",
                        lambda: _make_mock_db(entries))
    results = find_related_ledger_entries(["AAQkNSFConv001"], "")
    assert len(results) == 1
    assert results[0]["id"] == "reply-nsf-001"


def test_find_related_by_keyword_overlap(monkeypatch):
    """Fuzzy match by keyword overlap with thread text."""
    entries = _mock_ledger_entries()
    monkeypatch.setattr("context_export.ledger_mod._get_db",
                        lambda: _make_mock_db(entries))
    thread = "The NSF CAREER proposal budget justification from Avery Merriweather looks good"
    results = find_related_ledger_entries(["FAKE-ID"], thread)
    assert any(r["id"] == "reply-nsf-001" for r in results)


def test_find_related_no_match(monkeypatch):
    """No matches when thread has no related content."""
    entries = _mock_ledger_entries()
    mock_db = lambda: _make_mock_db(entries)

    monkeypatch.setattr("context_export.ledger_mod._get_db", mock_db)
    results = find_related_ledger_entries(["NONEXISTENT"], "unrelated topic about weather")
    assert len(results) == 0


# ---------------------------------------------------------------------------
# cmd_package
# ---------------------------------------------------------------------------

def test_package_markdown_only():
    """Package with no attachments creates markdown only."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        briefing = td_path / "briefing.md"
        briefing.write_text("# Context Export\n\nTest briefing content.")

        args = argparse.Namespace(export_dir=str(td_path), briefing="briefing.md")

        import io
        from unittest.mock import patch
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            cmd_package(args)

        result = json.loads(captured.getvalue())
        assert result["status"] == "ok"
        assert result["has_attachments"] is False
        assert (td_path / "context-export.md").exists()
        assert (td_path / "context-export.md").read_text() == "# Context Export\n\nTest briefing content."


def test_package_with_attachments():
    """Package with attachments creates markdown + tarball."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        briefing = td_path / "briefing.md"
        briefing.write_text("# Export with attachments")

        att_dir = td_path / "attachments"
        att_dir.mkdir()
        (att_dir / "receipt.pdf").write_bytes(b"fake pdf content")
        (att_dir / "photo.jpg").write_bytes(b"fake jpg content")

        args = argparse.Namespace(export_dir=str(td_path), briefing="briefing.md")

        import io
        from unittest.mock import patch
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            cmd_package(args)

        result = json.loads(captured.getvalue())
        assert result["status"] == "ok"
        assert result["has_attachments"] is True
        assert "tarball_path" in result

        tarball = Path(result["tarball_path"])
        assert tarball.exists()
        with tarfile.open(tarball, "r:gz") as tar:
            names = tar.getnames()
            assert "context-export.md" in names
            assert "attachments/receipt.pdf" in names
            assert "attachments/photo.jpg" in names


def test_package_missing_briefing():
    """Package with missing briefing file exits with error."""
    with tempfile.TemporaryDirectory() as td:
        class Args:
            export_dir = td
            briefing = "nonexistent.md"

        import io
        from unittest.mock import patch
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            try:
                cmd_package(Args())
            except SystemExit:
                pass

        result = json.loads(captured.getvalue())
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Integration: skill detection → context-export routing
# ---------------------------------------------------------------------------

def test_dump_this_triggers_context_export():
    """REGRESSION: 'dump this' must route to context-export skill."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))
    from prompt_logic import detect_skills

    # All real user messages that failed (from dispatch log)
    must_match = [
        "Dump this",
        "dump this",
        "Dump to markdown the whole email chain",
        "Dump this email thread.",
        "dump this into a markdown only",
        "export this",
        "package this",
    ]
    for msg in must_match:
        skills = detect_skills(msg, is_thread_reply=True)
        assert "context-export" in skills, f"{msg!r} did not trigger context-export, got {skills}"


def test_dump_this_not_confused_with_other_skills():
    """'dump this' should only trigger context-export, not other skills."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))
    from prompt_logic import detect_skills

    skills = detect_skills("dump this", is_thread_reply=True)
    assert skills == ["context-export"], f"Expected only context-export, got {skills}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Simple runner without pytest
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    skipped = 0

    for test in tests:
        name = test.__name__
        # Skip monkeypatch tests when running without pytest
        if "monkeypatch" in test.__code__.co_varnames:
            print(f"  SKIP {name} (needs pytest)")
            skipped += 1
            continue
        try:
            test()
            print(f"  \u2705 {name}")
            passed += 1
        except Exception as e:
            print(f"  \u274c {name}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n  {passed} passed, {failed} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)
