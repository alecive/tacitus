"""No file outside the allowlist names the institution the EA was built
at, its time zone or its first user. Institution values live in config
(the `institution` block of user-profile.json, preprocessor/institution.py)
and in the worked example under config/examples/; code, prompts and
templates stay generic."""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORD = re.compile(
    r"colorado|cuboulder|cu[ -]boulder|\bcu\b\s+(oit|research|boulder)"
    r"|america/denver|heckman|crheckman|christoffer", re.I)
# The institution's name alone, without the first user's: fixtures under
# tests/ may carry these.
INSTITUTION_ONLY = re.compile(r"colorado|america/denver", re.I)
# An IANA zone name other than the institution's own.
OTHER_ZONE = re.compile(
    r"\b(?:America|US|Pacific|Europe|Asia|Africa|Australia|Atlantic|Indian"
    r"|Antarctica|Etc)/(?!Denver\b)[A-Za-z_]+")

# Paths (or path prefixes) that may name it, and why.
ALLOWED_PREFIXES = (
    "config/examples/",          # the worked example for one institution
    "CHANGELOG.md",              # the project's history
    "AUTHORS",                   # the authors, named under the release agreement
)
# Paths (or path prefixes) that may name the institution and its zone but
# never the first user: test fixtures use the example institution's values.
INSTITUTION_ONLY_PREFIXES = (
    "tests/",
)
# A line carrying this marker names the legacy launchd prefix that macOS
# installs deployed before the rename keep (preprocessor/launchd_labels.py).
LEGACY_LABEL_MARKER = "legacy-launchd-label"
LEGACY_LABEL_FILES = {
    "preprocessor/launchd_labels.py",
    "deploy/enable-mail-mediation.sh",
    "deploy/macos/deploy.sh",
    "deploy/macos/install-services.sh",
}
# Tests that spell a name only to assert it is absent from the product.
# The second test below checks each still carries its hit, so a stale
# entry is removed rather than kept.
_FILES_ASSERTING_ABSENCE = {
    "tests/test_install_overlay.py": "asserts the first user's name is absent",
    "tests/test_deck_serve.py": "asserts the first institution's theme name is absent",
}
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".svg", ".zip", ".gz")
SELF = "tests/test_no_institution_strings.py"  # spells the patterns it looks for


def _files() -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "--cached", "--others",
                              "--exclude-standard"], capture_output=True, text=True, check=True).stdout
        return [f for f in out.splitlines() if f]
    except (OSError, subprocess.CalledProcessError):
        return [str(p.relative_to(REPO)) for p in REPO.rglob("*")
                if p.is_file() and ".git" not in p.parts]


def _under(rel: str, prefixes) -> bool:
    return any(rel == p or rel.startswith(p) for p in prefixes)


def _line_allowed(rel: str, line: str) -> bool:
    """True when every hit on this line is one the allowlist covers."""
    if rel in LEGACY_LABEL_FILES and LEGACY_LABEL_MARKER in line:
        return True
    rest = line
    if _under(rel, INSTITUTION_ONLY_PREFIXES):
        rest = INSTITUTION_ONLY.sub("", rest)
    # A path into the worked example directory names the example, not the
    # product; the prose around it must not name the institution.
    rest = re.sub(r"config/examples/[A-Za-z0-9_-]+", "", rest)
    # A map of zone abbreviations or names lists the institution's zone
    # beside others; the zone alone, anywhere else, is a hit.
    if OTHER_ZONE.search(rest):
        rest = re.sub(r"america/denver", "", rest, flags=re.I)
    return not WORD.search(rest)


def lint_hits(files=None) -> list[str]:
    hits = []
    for rel in files if files is not None else _files():
        if (_under(rel, ALLOWED_PREFIXES) or rel in _FILES_ASSERTING_ABSENCE or rel == SELF
                or rel.endswith(SKIP_SUFFIXES) or rel.startswith(".claude/worktrees/")):
            continue
        path = REPO / rel
        try:
            text = path.read_text(errors="ignore")
        except (OSError, IsADirectoryError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if WORD.search(line) and not _line_allowed(rel, line):
                hits.append(f"{rel}:{n}: {line.strip()[:100]}")
    return hits


def test_no_file_outside_the_allowlist_names_the_institution():
    hits = lint_hits()
    assert not hits, "institution named outside config:\n" + "\n".join(hits)


def test_the_legacy_label_exception_names_only_the_label():
    for rel in LEGACY_LABEL_FILES:
        for line in (REPO / rel).read_text().splitlines():
            if LEGACY_LABEL_MARKER in line and WORD.search(line):
                assert re.search(r"edu\.colorado", line), f"{rel}: {line.strip()}"


def test_the_pending_entries_still_carry_a_hit():
    """An entry whose file is clean is stale: delete it."""
    for rel in sorted(_FILES_ASSERTING_ABSENCE):
        path = REPO / rel
        if not path.exists():
            continue
        text = path.read_text(errors="ignore")
        assert any(WORD.search(l) and not _line_allowed(rel, l)
                   for l in text.splitlines()), f"{rel}: clean, drop its pending entry"


def test_the_line_rules():
    assert _line_allowed("preprocessor/x.py", '"America/Denver": "MT", "America/Boise": "MT",')
    assert not _line_allowed("preprocessor/x.py", 'os.environ["TZ"] = "America/Denver"')
    assert _line_allowed("tests/test_x.py", 'ZoneInfo("America/Denver")')
    assert _line_allowed("tests/test_x.py", '"you@colorado.edu"')
    assert not _line_allowed("tests/test_x.py", '"Heckman"')
    assert not _line_allowed("docs/x.md", "CU Boulder OIT provisions")
    assert not _line_allowed("docs/x.md", "the cu-boulder plugin")
    assert _line_allowed("docs/x.md", "a CU sender")
