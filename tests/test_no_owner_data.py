"""No tracked file carries data derived from the first user's mailbox,
ledger or machines: tailnet addresses, mailbox-scoped Outlook ids, ledger
ids hashed from real messages, real manuscript or proposal numbers, and a
list of terms (programs, places, a startup) known to come from the owner's
history. Fixtures use invented values (tests/fixtures/closure_corpus_synthetic.py
shows the shapes); the owner's real corpus stays in the private directory.

The terms are not in the tree, not even as hashes: the hash of one word
falls to a dictionary. They live in an owner-private file, one word per
line, `#` for a comment (default ~/.ea-private/owner-terms.txt,
EA_OWNER_TERMS overrides the path). Without that file the term check
skips and the other checks still run."""
import base64
import os
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_no_institution_strings as inst  # noqa: E402

REPO = inst.REPO
SELF = "tests/test_no_owner_data.py"
SKIP_PREFIXES = ("CHANGELOG.md", ".claude/worktrees/")

# Tailnet (CGNAT 100.64.0.0/10) addresses that are documentation values:
# the range itself, Tailscale's fixed service address and the invented
# host addresses the tests use.
TAILNET = re.compile(r"(?<![\d.])100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}(?![\d.])")
TAILNET_OK = {"100.64.0.0", "100.64.0.1", "100.100.100.100",
              "100.101.102.103", "100.101.102.104"}

# A ledger detector id with the 12-character tail ledger.py hashes from a
# real message id ("response-" + 11 base64 characters + "=").
HASHED_LEDGER_ID = re.compile(r"\b(?:response|priority|thread|sched)-[A-Za-z0-9_-]{11}=")

# Manuscript ids under a real journal's prefix. Tests use JRNL/EXJ/TXR.
JOURNAL_ID = re.compile(
    r"\b(?:CSUR|IJRR|T-?RO|RA-?L|T-?FR|FR|TASE|TPAMI|JFR|AURO|IJCV)-\d{2,4}-\d{3,6}\b")

# Ten-digit proposal numbers ("0000123456"); invented ones start 00009.
PROPOSAL_NUMBER = re.compile(r"(?<![\d.])0000[1-8]\d{5}(?![\d.])")

# An Outlook id: the mailbox GUID is base64 text right after the prefix.
OUTLOOK_ID = re.compile(r"AA[MQ]k[A-Za-z0-9+/_=-]{16,}")
GUID_TEXT = re.compile(rb"[0-9a-f]{6,8}-[0-9a-f]{4}")

TOKEN = re.compile(r"[a-z0-9]+")
TERM_HIT = "a term from the owner's history"


def terms_path() -> Path:
    """The owner-private term list: EA_OWNER_TERMS, else ~/.ea-private/owner-terms.txt."""
    return Path(os.environ.get("EA_OWNER_TERMS")
                or Path.home() / ".ea-private" / "owner-terms.txt")


def read_terms(path: Path | None = None) -> frozenset | None:
    """The lowercase words of the term list, or None when it is absent."""
    try:
        text = (path or terms_path()).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    words: set = set()
    for line in text.splitlines():
        words.update(TOKEN.findall(line.split("#", 1)[0].lower()))
    return frozenset(words)


def _guid_in_outlook_id(oid: str) -> bool:
    """True when the id decodes to a GUID with real (non-zero) digits."""
    for start in range(0, 4):
        chunk = oid[start:]
        chunk = chunk[: len(chunk) - len(chunk) % 4]
        try:
            raw = base64.b64decode(chunk.replace("-", "+").replace("_", "/"), validate=False)
        except (ValueError, base64.binascii.Error):
            continue
        for m in GUID_TEXT.finditer(raw):
            if set(m.group(0)) - set(b"0-"):
                return True
    return False


def line_hits(rel: str, line: str, terms: frozenset = frozenset()) -> list[str]:
    hits = []
    for ip in TAILNET.findall(line):
        if ip not in TAILNET_OK:
            hits.append(f"tailnet address {ip}")
    if HASHED_LEDGER_ID.search(line):
        hits.append("ledger id hashed from a real message")
    if JOURNAL_ID.search(line):
        hits.append("manuscript id under a real journal prefix")
    if PROPOSAL_NUMBER.search(line):
        hits.append("ten-digit proposal number")
    for oid in OUTLOOK_ID.findall(line):
        if _guid_in_outlook_id(oid):
            hits.append("Outlook id carrying a mailbox GUID")
            break
    if terms and not terms.isdisjoint(TOKEN.findall(line.lower())):
        hits.append(TERM_HIT)
    return hits


def lint_hits(files=None, terms: frozenset = frozenset()) -> list[str]:
    out = []
    for rel in files if files is not None else inst._files():
        if (rel == SELF or inst._under(rel, SKIP_PREFIXES)
                or rel in inst._FILES_ASSERTING_ABSENCE
                or rel.endswith(inst.SKIP_SUFFIXES)):
            continue
        try:
            text = (REPO / rel).read_text(errors="ignore")
        except (OSError, IsADirectoryError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for why in line_hits(rel, line, terms):
                out.append(f"{rel}:{n}: {why}")
    return out


def test_no_tracked_file_carries_owner_data():
    hits = lint_hits()
    assert not hits, "owner-derived data in tracked files:\n" + "\n".join(hits[:60])


def test_no_tracked_file_spells_an_owner_term():
    terms = read_terms()
    if terms is None:
        pytest.skip(f"no owner term list at {terms_path()} (EA_OWNER_TERMS names another)")
    hits = [h for h in lint_hits(terms=terms) if h.endswith(TERM_HIT)]
    # The hit names the file and line only, so a failure does not spell the term.
    assert not hits, "owner terms in tracked files:\n" + "\n".join(hits[:60])


def test_the_line_rules():
    assert line_hits("x.py", "host 100.101.102.103 and 100.100.100.100") == []
    assert line_hits("x.py", "host 100.77.1.2") == ["tailnet address 100.77.1.2"]
    assert line_hits("x.py", "10.100.64.1 and 100.64.0.1") == []
    assert line_hits("x.py", '"response-AbCdEfGhIjK="') == ["ledger id hashed from a real message"]
    assert line_hits("x.py", '"response-syn-r001" "response-x1="') == []
    assert line_hits("x.py", "JRNL-2026-0804 and EXJ-2030-9101") == []
    assert "manuscript id under a real journal prefix" in line_hits("x.py", "IJRR-2026-0101")
    assert line_hits("x.py", "proposal 0000900016") == []
    assert line_hits("x.py", "proposal 0000123456") == ["ten-digit proposal number"]
    fake = "AAMk" + base64.b64encode(b"\x00\x00" + b"00000000-0000-4000-8000").decode()
    real = "AAMk" + base64.b64encode(b"\x00\x00" + b"3f2a9c1e-77b0-4d1e-9a2b").decode()
    assert line_hits("x.py", fake) == []
    assert line_hits("x.py", real) == ["Outlook id carrying a mailbox GUID"]
    assert line_hits("x.py", '"AAMkAQQQQQQQQQQQQQQQQQQQQQQQ="') == []


def test_the_term_rule(tmp_path):
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("# a comment line\nZebrafish\nquokka  # a trailing note\n\n")
    terms = read_terms(terms_file)
    assert terms == {"zebrafish", "quokka"}
    assert line_hits("x.py", "the ZEBRAFISH lab", terms) == [TERM_HIT]
    assert line_hits("x.py", "zebrafishes are another word", terms) == []
    assert line_hits("x.py", "the zebrafish lab") == []
    assert read_terms(tmp_path / "absent.txt") is None


def test_the_term_list_path_follows_the_env(monkeypatch, tmp_path):
    monkeypatch.setenv("EA_OWNER_TERMS", str(tmp_path / "t.txt"))
    assert terms_path() == tmp_path / "t.txt"
    monkeypatch.delenv("EA_OWNER_TERMS")
    assert terms_path() == Path.home() / ".ea-private" / "owner-terms.txt"


def test_this_file_holds_no_term_list():
    """No hashed or spelled term list comes back into the tree."""
    text = (REPO / SELF).read_text()
    assert "hash" + "lib" not in text and "sha" + "256" not in text.lower()
