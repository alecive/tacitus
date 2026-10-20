#!/usr/bin/env python3
"""
Context Export — collect thread context and package for external use.

Two subcommands:
  collect  — gather emails, attachments, and ledger entries from a thread
  package  — bundle briefing markdown + attachments into deliverables

Runs as the owner (needs Graph API access for email fetching). Its workspace is
an owner-private directory (the bot uses ~/.ea-private/exports/<stamp>/):
an owner process refuses a workspace inside the resident's EA tree, since
its writes there would follow whatever the resident put at the names. An
attachment is saved under a safe basename of the name the email gives it.

Usage:
  python3 context_export.py collect --thread-json /path/to/thread.json --output-dir ~/.ea-private/exports/20260331-150000/
  python3 context_export.py package --export-dir ~/.ea-private/exports/20260331-150000/ --briefing briefing.md
"""

import argparse
import base64
import json
import os
import re
import sys
import tarfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import ledger as ledger_mod
import owner_trust
import resident_io

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # 5MB cap per file


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip = True
        elif tag in ("br", "p", "div", "tr", "li"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def get_text(self):
        text = "".join(self._parts)
        # Collapse multiple blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(html: str) -> str:
    stripper = _HTMLStripper()
    stripper.feed(html)
    return stripper.get_text()


def extract_message_ids(text: str) -> list[str]:
    """Extract Outlook AAMk... message IDs from text."""
    pattern = r"AAMk[A-Za-z0-9_\-+/=]{20,}"
    ids = list(set(re.findall(pattern, text)))
    return ids


def fetch_email_context(message_id: str) -> dict | None:
    """Fetch full email body and metadata via Graph API."""
    try:
        import graph_client as graph
        msg = graph.get_message_full(message_id)
        if not msg or "error" in msg:
            return None

        body_raw = msg.get("body", {}).get("content", "")
        body_type = msg.get("body", {}).get("contentType", "text")
        body_text = html_to_text(body_raw) if body_type.lower() == "html" else body_raw

        from_addr = msg.get("from", {}).get("emailAddress", {})
        to_list = [
            r.get("emailAddress", {}).get("address", "")
            for r in msg.get("toRecipients", [])
        ]

        return {
            "id": message_id,
            "subject": msg.get("subject", "(no subject)"),
            "from_name": from_addr.get("name", ""),
            "from_email": from_addr.get("address", ""),
            "to": to_list,
            "date": msg.get("receivedDateTime", ""),
            "body_text": body_text,
            "has_attachments": msg.get("hasAttachments", False),
        }
    except Exception as e:
        return {"id": message_id, "error": str(e)}


def safe_attachment_name(name) -> str:
    """The name an attachment is saved under: the last path component of
    the name the email gives it, reduced to letters, digits, dot, dash,
    underscore and space, with no leading dot, at most 120 characters.
    The email's sender picks that name, so it never chooses a directory."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip().lstrip(".")
    return base[:120] or "attachment"


def check_workspace(path: Path) -> None:
    """Refuse, in an owner process, a workspace inside the resident's EA
    tree: this module writes there by path."""
    if not resident_io.is_owner():
        return
    ea = os.path.realpath(owner_trust.ea_dir())
    here = os.path.realpath(path)
    if here == ea or here.startswith(ea + os.sep):
        raise ValueError(f"{path} is in the resident's space; use an "
                         f"owner-private workspace")


def download_attachments(message_id: str, output_dir: Path) -> list[dict]:
    """Download attachments for an email. Returns list of {name, path, size}."""
    try:
        import graph_client as graph
        attachments = graph.get_attachments(message_id)
        downloaded = []

        att_dir = output_dir / "attachments"
        att_dir.mkdir(parents=True, exist_ok=True)

        for att in attachments:
            name = safe_attachment_name(att.get("name", "unnamed"))
            size = att.get("size", 0)

            if size > MAX_ATTACHMENT_BYTES:
                downloaded.append({
                    "name": name,
                    "size": size,
                    "skipped": True,
                    "reason": f"Too large ({size // 1024 // 1024}MB > 5MB cap)",
                })
                continue

            # File attachments have contentBytes
            content_b64 = att.get("contentBytes")
            if not content_b64:
                # Try direct download
                att_id = att.get("id")
                if att_id:
                    try:
                        raw = graph.download_attachment(message_id, att_id)
                        file_path = att_dir / name
                        file_path.write_bytes(raw)
                        downloaded.append({
                            "name": name,
                            "path": str(file_path),
                            "size": len(raw),
                        })
                    except Exception:
                        downloaded.append({"name": name, "skipped": True, "reason": "Download failed"})
                continue

            raw = base64.b64decode(content_b64)
            file_path = att_dir / name
            file_path.write_bytes(raw)
            downloaded.append({
                "name": name,
                "path": str(file_path),
                "size": len(raw),
            })

        return downloaded
    except Exception as e:
        return [{"error": str(e)}]


def find_related_ledger_entries(message_ids: list[str], thread_text: str) -> list[dict]:
    """Find ledger entries related to the thread by message ID or keyword overlap."""
    try:
        db = ledger_mod._get_db()
        all_entries = [ledger_mod._row_to_dict(r) for r in
                       db.execute("SELECT * FROM ledger").fetchall()]
        db.close()
    except Exception:
        return []

    matches = []
    seen_ids = set()

    # Match by source_message_id
    for entry in all_entries:
        src_id = entry.get("source_message_id", "")
        conv_id = entry.get("conversation_id", "")
        draft_id = entry.get("draft_id", "")
        for mid in message_ids:
            if mid in (src_id, conv_id, draft_id):
                if entry["id"] not in seen_ids:
                    matches.append(entry)
                    seen_ids.add(entry["id"])

    # Match by keyword overlap
    if message_ids:
        thread_lower = thread_text.lower()
        for entry in all_entries:
            if entry["id"] in seen_ids:
                continue
            desc = entry.get("description", "").lower()
            words = [w for w in desc.split() if len(w) > 4]
            if words:
                overlap = sum(1 for w in words if w in thread_lower)
                if overlap >= 3 and overlap / len(words) > 0.3:
                    matches.append(entry)
                    seen_ids.add(entry["id"])

    return matches


def _refused(e: Exception) -> None:
    print(json.dumps({"status": "error", "error": str(e)}))
    sys.exit(2)


def cmd_collect(args):
    output_dir = Path(args.output_dir)
    try:
        check_workspace(output_dir)
    except ValueError as e:
        _refused(e)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    # Read thread messages
    thread_json = Path(args.thread_json)
    thread_messages = json.loads(thread_json.read_text())
    thread_text = "\n".join(
        m.get("text", "") if isinstance(m, dict) else str(m)
        for m in thread_messages
    )

    # Extract message IDs from the thread
    message_ids = extract_message_ids(thread_text)

    # Fetch emails
    emails = []
    all_attachments = []
    for mid in message_ids:
        email_ctx = fetch_email_context(mid)
        if email_ctx and "error" not in email_ctx:
            emails.append(email_ctx)
            if email_ctx.get("has_attachments"):
                atts = download_attachments(mid, output_dir)
                email_ctx["attachments"] = atts
                all_attachments.extend(a for a in atts if not a.get("skipped"))

    # Find related ledger entries
    ledger_entries = find_related_ledger_entries(message_ids, thread_text)

    # Build manifest
    manifest = {
        "exported_at": datetime.now().isoformat(),
        "thread_messages": thread_messages,
        "message_ids_found": message_ids,
        "emails": emails,
        "ledger_entries": ledger_entries,
        "has_attachments": len(all_attachments) > 0,
        "attachment_count": len(all_attachments),
        "export_dir": str(output_dir),
    }

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))

    print(json.dumps({
        "status": "ok",
        "manifest": str(manifest_path),
        "emails_found": len(emails),
        "attachments_downloaded": len(all_attachments),
        "ledger_entries_matched": len(ledger_entries),
    }, indent=2))


def cmd_package(args):
    export_dir = Path(args.export_dir)
    try:
        check_workspace(export_dir)
    except ValueError as e:
        _refused(e)
    briefing_path = export_dir / safe_attachment_name(args.briefing)

    if not briefing_path.exists():
        print(json.dumps({"status": "error", "error": f"Briefing not found: {briefing_path}"}))
        sys.exit(1)

    briefing_text = briefing_path.read_text()

    # Write the standalone markdown
    md_path = export_dir / "context-export.md"
    md_path.write_text(briefing_text)

    result = {"status": "ok", "markdown_path": str(md_path)}

    # Check for attachments
    att_dir = export_dir / "attachments"
    has_attachments = att_dir.exists() and any(att_dir.iterdir())

    if has_attachments:
        tarball_path = export_dir / "context-export.tar.gz"
        with tarfile.open(tarball_path, "w:gz") as tar:
            tar.add(md_path, arcname="context-export.md")
            for f in att_dir.iterdir():
                if f.is_file():
                    tar.add(f, arcname=f"attachments/{f.name}")
        result["tarball_path"] = str(tarball_path)
        result["has_attachments"] = True
    else:
        result["has_attachments"] = False

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Context Export")
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--thread-json", required=True, help="Path to thread messages JSON")
    p_collect.add_argument("--output-dir", required=True, help="Export output directory")
    p_collect.set_defaults(func=cmd_collect)

    p_package = sub.add_parser("package")
    p_package.add_argument("--export-dir", required=True, help="Export directory with manifest")
    p_package.add_argument("--briefing", required=True, help="Briefing markdown filename (relative to export-dir)")
    p_package.set_defaults(func=cmd_package)

    args = parser.parse_args()
    args.func(args)
