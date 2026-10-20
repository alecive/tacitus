"""Daily review of the EA's outbound web requests.

Three logs feed it, each read from where its own cursor stopped:

  * `~/.ea/logs/fetch-url.log` (ea-bot space): fetch_url.py's own records
    (events: fetch, done, blocked, error) and, from a resident process,
    outbound_log.py's records for the other modules that reach the network
    (booking_link, poll_filler's reads and submissions, email_to_pdf's
    renderer, list_digest's redirect walk). ea-bot can write this log, so
    the review checks it: a record with a time in the future, a time that
    is not ISO, a time out of order, a line that is not JSON, and a log
    that shrank, changed under the cursor or was replaced by a new file
    are each named in the post, and a record with a bad time is left out
    of the counts.
  * the owner-private `web-tools.jsonl` (web_tool_log.py): the model's own
    WebFetch and WebSearch calls. They run inside the Claude CLI, not
    through fetch_url: WebFetch fetches the page from this host, and
    WebSearch's query travels in the model request to a search run on the
    API side. So the owner processes that read a lane's stream-json (the
    chat bot for the chat and room lanes, log_claude_cost.py for the cron
    pipes) record each call. The session transcripts under ea-bot's home hold the same calls,
    but ea-bot can rewrite them, so the review reads the owner-side log
    only. The cron lanes deny both tools, so any cron record there is
    itself worth a look.
  * the owner-private `outbound-owner.jsonl` (outbound_log.py): requests
    an owner process made (list_digest's redirect walk in an owner cron)
    and each brief the remote email writer sent to the endpoint (size, a
    hash, the first 200 characters).

Each log has its own cursor, a byte offset with the file's inode and a
hash of the bytes before the offset, kept in the owner-private directory.
A time in one log never moves another log's cursor, and no time moves any
cursor: a forged far-future time in the ea-bot log is a flagged record,
not a skipped day. With no cursor file, or one that is refused or is not
JSON, each log is read from its start (the last FRESH_MAX_BYTES at most)
and the post says why; a bad file is set aside, never silently replaced.
The bytes before a cursor are hashed, so a rewrite of reviewed records is
named; records ea-bot rewrites or drops after the cursor, before the
review reads them, look like records it never wrote (docs/SECURITY.md).

What it flags:
  * blocked: the address policy refused the fetch (SSRF guard fired).
  * errored: the fetch failed (timeout, DNS); counted.
  * exfil-shaped URLs (fetch_url and WebFetch): an email address, on every
    host; a long or encoded (high-entropy) path segment or query value; a
    hex run of HEX_MIN or more, or a one-case base32-like run; a long or
    encoded DNS label. Identity and link-wrapper hosts, whose URLs carry
    long tokens by design, skip the length and encoding checks (the hop
    they redirect to is logged and checked on its own); document and code
    hosts skip the entropy check on the path, where their ids live. A
    tenant host (a SharePoint site) is exempt only when the owner names it
    exactly in the owner-private OWNER_HOSTS_FILE.
  * exfil-shaped model requests: a WebFetch URL or a WebSearch query with
    an email address, a long query string or an encoded payload.
  * many subdomains: more than SUBDOMAIN_N distinct hosts under one
    registrable domain in the window.
  * volume: more than `spike_n` requests to one host, or a count well
    above the host's own recent days (host history, owner-private).
  * new hosts: hosts first seen today are listed on one line; a new host
    with NEW_HOST_VOLUME requests or more is a flag.
  * poll submissions: counted; a name that is not the owner's own
    (owner_trust.owner_profile_name) is a flag. With no owner-private
    profile.json the names are listed, marked unchecked.
  * log anomalies: as above, for the ea-bot log.
The remote writer's briefs get one count and size line.

The pure functions (`analyze`, `analyze_web_tools`, `analyze_hosts`,
`read_window`, `render`) take records and a config; `review()` reads the
logs and the state, and scripts/outbound-review.py posts and advances it.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import atomic_io as _atomic
    import owner_trust as _owner_trust
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import atomic_io as _atomic
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import resident_io as _rio

try:
    import outbound_log as _out_log
    import web_tool_log as _web_log
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import outbound_log as _out_log
    from preprocessor import web_tool_log as _web_log

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
LOG_PATH = EA_DIR / "logs" / "fetch-url.log"


def _owner_state_dir() -> Path:
    """The owner-private directory in an owner process (the review cron),
    the EA tree's state directory in any other (a test on a scratch tree)."""
    if _owner_trust.is_owner_process():
        return _owner_trust.private_dir()
    return EA_DIR / "state"


# The cursor decides which log records the review reads, so the owner keeps
# it where the resident cannot advance it past records it wants unseen. The
# copy an older version kept in ea-bot space is never read: ea-bot could
# have moved it, so with no cursor here each log is read from its start.
CURSOR = _owner_state_dir() / "outbound-review-seen.json"
# Exact hosts of the owner's own tenants that may skip the shape checks
# (owner_hosts); owner-private, so ea-bot cannot widen the exemptions.
OWNER_HOSTS_FILE = "outbound-hosts.json"


def _under(path: Path, root) -> bool:
    root = os.path.realpath(root)
    parent = os.path.realpath(Path(path).parent)
    return parent == root or parent.startswith(root.rstrip(os.sep) + os.sep)


def _in_private(path: Path) -> bool:
    return _under(path, _owner_trust.private_dir())


def _in_resident_space(path: Path) -> bool:
    """The EA tree as resolved, or the resident's home."""
    roots = [_owner_trust.ea_dir()]
    home = os.path.expanduser(f"~{_rio.RESIDENT_USER}")
    if not home.startswith("~"):
        roots.append(home)
    return any(_under(path, r) for r in roots)


def _read_shared_bytes(path: Path) -> bytes | None:
    """The bytes of a file the review reads, or None when it is absent or
    unreadable. An owner process reads a file in resident space (the EA
    tree as resolved, or the resident's home) with the resident's access;
    a file of its own, or any file in another process, is read directly."""
    if _rio.is_owner() and _in_resident_space(path):
        return _rio.read_bytes(path)
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _read_shared_text(path: Path) -> str | None:
    data = _read_shared_bytes(path)
    return None if data is None else data.decode("utf-8", "replace")


def _read_owner_log(path: Path, what: str) -> bytes | None:
    """An owner-private log's bytes, or None when absent or refused. In the
    owner-private directory it is read through owner_trust's checks; it is
    never read from ea-bot space, since ea-bot could rewrite a copy there."""
    path = Path(path)
    if _in_private(path):
        try:
            return _owner_trust.read_owner_file(path)
        except FileNotFoundError:
            return None
        except (OSError, _owner_trust.OwnerTrustError) as e:
            print(f"outbound_review: {what} refused: {e}", file=sys.stderr)
            return None
    if _rio.is_owner() and _in_resident_space(path):
        print(f"outbound_review: {path} is in ea-bot space; the {what} is read "
              f"only from the owner-private directory", file=sys.stderr)
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _inode(path: Path) -> int | None:
    try:
        return os.lstat(path).st_ino
    except OSError:
        return None


# ── the cursor ────────────────────────────────────────────────────────

def _read_cursor_doc(path: Path | None = None) -> tuple[dict | None, str]:
    """(the cursor file as a dict, ''), (None, '') when there is none, or
    (None, why) when a file is there but is refused or is not a JSON
    object. In the owner-private directory it is read through
    owner_trust's checks."""
    path = CURSOR if path is None else path
    if _in_private(path):
        try:
            text = _owner_trust.read_owner_file(path).decode("utf-8", "replace")
        except FileNotFoundError:
            return None, ""
        except (OSError, _owner_trust.OwnerTrustError) as e:
            print(f"outbound_review: cursor refused: {e}", file=sys.stderr)
            return None, f"was refused ({e})"
    else:
        text = _read_shared_text(path)
        if text is None:
            return None, ("could not be read" if os.path.lexists(path) else "")
    try:
        data = json.loads(text)
    except ValueError:
        return None, "is not JSON"
    if not isinstance(data, dict):
        return None, "is not a JSON object"
    return data, ""


def read_cursor(path: Path | None = None) -> str | None:
    """The last reviewed timestamp, or None when there is no cursor yet."""
    doc, _why = _read_cursor_doc(path)
    ts = doc.get("last_ts") if doc else None
    return ts if isinstance(ts, str) else None


def read_state(path: Path | None = None) -> dict | None:
    """The review state: {"last_ts", "logs": {name: cursor}, "hosts": {...}},
    or None when there is no cursor file. A file that is refused or is not
    a JSON object comes back as an empty state with `cursor_problem` set:
    the review reads each log from its start and says so, and the daily
    script sets the file aside before it writes a new one. A time-only
    cursor (the older format) comes back with no per-log cursors and
    `legacy` set, so its first run windows each log by that time once."""
    doc, why = _read_cursor_doc(path)
    if why:
        return {"last_ts": "", "logs": {}, "hosts": None, "cursor_problem": why}
    if doc is None:
        return None
    logs = doc.get("logs") if isinstance(doc.get("logs"), dict) else None
    state = {"last_ts": doc.get("last_ts") if isinstance(doc.get("last_ts"), str) else "",
             "logs": logs or {},
             "hosts": doc.get("hosts") if isinstance(doc.get("hosts"), dict) else None}
    if logs is None:
        state["legacy"] = True
    return state


def set_aside_cursor(path: Path | None = None, *, now: datetime | None = None) -> Path | None:
    """Rename a cursor file that read_state could not use to
    `<name>.bad-<time>` beside it, so the next write neither overwrites it
    nor refuses to, and the owner can look at it. The new path, or None
    when there was nothing to move or the rename failed."""
    path = Path(CURSOR if path is None else path)
    if not os.path.lexists(path):
        return None
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    aside = path.with_name(f"{path.name}.bad-{stamp}")
    n = 1
    while os.path.lexists(aside):
        n += 1
        aside = path.with_name(f"{path.name}.bad-{stamp}-{n}")
    try:
        os.rename(path, aside)
    except OSError as e:
        print(f"outbound_review: cannot set the cursor aside: {e}", file=sys.stderr)
        return None
    return aside


def _write_doc(doc: dict, path: Path | None) -> None:
    path = CURSOR if path is None else path
    text = json.dumps(doc, sort_keys=True)
    if _in_private(path):
        _owner_trust.write_owner_file(path, text, mode=0o600)
    else:
        _atomic.write_text(path, text)


def write_cursor(last_ts: str, path: Path | None = None) -> None:
    """Persist a time-only cursor: through owner_trust (mode 0600) in the
    owner-private directory, by atomic replace anywhere else."""
    _write_doc({"last_ts": last_ts}, path)


def write_state(state: dict, path: Path | None = None) -> None:
    """Persist the review state (per-log cursors and the host history)."""
    doc = {"version": 2, "last_ts": state.get("last_ts") or "",
           "logs": state.get("logs") or {}, "hosts": state.get("hosts") or {}}
    _write_doc(doc, path)


# ── reading one log from its cursor ───────────────────────────────────

SKEW_S = 300
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


def _parse_ts(ts) -> datetime | None:
    if not isinstance(ts, str) or not _ISO_RE.match(ts):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# A read with no cursor (a first run, a lost or refused cursor, a log that
# shrank) covers at most this many bytes, the latest; the post names what
# it left out.
FRESH_MAX_BYTES = 8 * 1024 * 1024


def _fresh_start(data: bytes, max_bytes: int, label: str) -> tuple[int, list[str]]:
    """Where a read with no cursor starts: 0, or the first line within the
    last `max_bytes` of a larger log, with an anomaly that says so."""
    size = len(data)
    if size <= max_bytes:
        return 0, []
    nl = data.find(b"\n", size - max_bytes - 1)
    start = size if nl < 0 else nl + 1
    return start, [f"{label}: only its last {size - start} of {size} bytes were read; "
                   f"the {start} bytes before them were not reviewed"]


def read_window(data: bytes | None, prior: dict | None, *, inode: int | None = None,
                label: str = "log", check_ts: bool = False, legacy_ts: str | None = None,
                now: datetime | None = None,
                max_bytes: int = FRESH_MAX_BYTES) -> tuple[list[dict], list[str], dict]:
    """(records, anomalies, the new cursor) for one log's bytes.

    `prior` is the log's cursor ({"offset", "inode", "sha", "last_ts"}):
    the window is the complete lines after the offset. A log that shrank
    is read from the start; one whose earlier bytes changed, or whose
    inode changed, is read from the offset. Each is an anomaly. With no
    cursor the window is the whole log, and with `legacy_ts` (the first
    run after the cursor format changed) the records after that time. A
    read from the start covers at most the last `max_bytes`, and the
    anomalies say what it left out. A cursor or legacy time in the future
    is ignored and named.

    `check_ts` is for a log ea-bot can write: a record whose time is not
    ISO or is more than SKEW_S in the future is left out and counted; one
    more than SKEW_S before an earlier record is kept and counted."""
    now = now or datetime.now(timezone.utc)
    limit = now + timedelta(seconds=SKEW_S)
    anomalies: list[str] = []
    prior = prior if isinstance(prior, dict) else None
    from_legacy = prior is None and bool(legacy_ts)
    prior_last = str((prior or {}).get("last_ts") or legacy_ts or "")
    last_dt = _parse_ts(prior_last)
    if last_dt is not None and last_dt > limit:
        what = "the legacy cursor time" if from_legacy else "the cursor time"
        anomalies.append(f"{label}: {what} {prior_last} is in the future; ignored")
        prior_last, last_dt = "", None
    if data is None:
        if prior and isinstance(prior.get("offset"), int) and prior["offset"] > 0:
            anomalies.append(f"{label} is gone (it held {prior['offset']} bytes at the "
                             f"last review)")
        return [], anomalies, {"offset": 0, "inode": None, "sha": _sha(b""),
                               "last_ts": prior_last}
    size = len(data)
    off = (prior or {}).get("offset")
    if isinstance(off, int) and not isinstance(off, bool) and off >= 0:
        if prior.get("inode") and inode and prior["inode"] != inode:
            anomalies.append(f"{label} was replaced by a new file since the last review")
        if size < off:
            anomalies.append(f"{label} shrank from {off} to {size} bytes since the last "
                             f"review (truncated or rewritten); read from the start")
            start, more = _fresh_start(data, max_bytes, label)
            anomalies += more
        else:
            if _sha(data[:off]) != prior.get("sha"):
                anomalies.append(f"{label}: records before the last review changed")
            start = off
    else:
        start, more = _fresh_start(data, max_bytes, label)
        anomalies += more
    end = data.rfind(b"\n", start) + 1
    if end <= start:
        end = start
    chunk = data[start:end]

    records: list[dict] = []
    bad_json = bad_ts = future = backwards = 0
    running = last_dt
    legacy_cut = last_dt if from_legacy else None
    for line in chunk.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            bad_json += 1
            continue
        if not isinstance(d, dict):
            bad_json += 1
            continue
        when = _parse_ts(d.get("ts"))
        if check_ts:
            if when is None:
                bad_ts += 1
                continue
            if when > limit:
                future += 1
                continue
            if running is not None and when < running - timedelta(seconds=SKEW_S):
                backwards += 1
            running = when if running is None or when > running else running
        if legacy_cut is not None and (when is None or when <= legacy_cut):
            continue
        records.append(d)
    if bad_json:
        anomalies.append(f"{label}: {bad_json} line(s) that are not JSON records")
    if bad_ts:
        anomalies.append(f"{label}: {bad_ts} record(s) with no ISO time, left out")
    if future:
        anomalies.append(f"{label}: {future} record(s) with a time in the future, left out")
    if backwards:
        anomalies.append(f"{label}: {backwards} record(s) earlier than a record before them")
    last = running.strftime("%Y-%m-%dT%H:%M:%SZ") if running else prior_last
    if not check_ts:
        last = max([prior_last] + [str(r.get("ts") or "") for r in records])
    return records, anomalies, {"offset": end, "inode": inode, "sha": _sha(data[:end]),
                                "last_ts": last}


def prior_hosts(data: bytes | None, upto: int) -> set[str]:
    """The hosts of the records before byte `upto`: what a fresh host
    history starts from, so the first day does not list every host."""
    out = set()
    for line in (data or b"")[:upto].decode("utf-8", "replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("host"):
            out.add(str(d["host"]).lower())
    return out


# ── exfil shapes ──────────────────────────────────────────────────────

DEFAULT_SPIKE_N = 30
_URL_LEN_FLAG = 300
_QUERY_LEN_FLAG = 200
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# A path segment or query value this long reads as data.
LONG_COMPONENT = 160
# An opaque token: this long, this much entropy per character, and its
# character class (upper, lower, digit) changing at least every third
# character. Random base64 changes class about two times in three; a slug
# or a CamelCase title far less often; a hex id has too little entropy.
OPAQUE_MIN = 24
ENTROPY_MIN = 4.0
SWITCH_RATIO = 1 / 3
# A DNS label: longer than LONG_LABEL, or OPAQUE_MIN long with this much
# entropy and a digit/letter change every fifth character.
LONG_LABEL = 40
LABEL_ENTROPY_MIN = 3.5
LABEL_SWITCH_RATIO = 1 / 5
SUBDOMAIN_N = 12

# Hex: a run (or a segment of hex and separators) this long reads as data.
# A UUID holds 32 hex digits and a git commit id 40.
HEX_MIN = 48
# Hex runs of HEX_PART_MIN or more across one URL's path, this many in all.
HEX_PART_MIN = 16
HEX_TOTAL = 96
# One-case letters and digits (base32, base36): this long, with at least
# two digits, a letter/digit change every B32_SWITCH characters and this
# much entropy. A slug with a year in it changes class once or twice.
B32_MIN = 32
B32_SWITCH = 8
B32_ENTROPY_MIN = 3.5

# Hosts whose URLs carry long tokens by design (sign-in, link wrappers):
# no length or entropy check, though the email check still runs. Each is a
# provider's own name, so a host under it (a region, a cluster) is the
# provider's too; an attacker cannot read what reaches them, and a
# wrapper's target is its own logged hop. A tenant's host (a SharePoint
# site, a Zoom or Box vanity name) is anyone's for the price of a tenant,
# so none is built in: the owner lists their own in OWNER_HOSTS_FILE.
TOKEN_HOSTS = ("login.microsoftonline.com", "login.microsoft.com", "login.live.com",
               "accounts.google.com", "safelinks.protection.outlook.com",
               "urldefense.com", "urldefense.proofpoint.com")
# Safe Links puts the recipient's mailbox in `data=`; the owner's own
# addresses (OWNER_HOSTS_FILE "own_addresses") may stand there.
SAFELINKS_HOSTS = ("safelinks.protection.outlook.com",)
# Hosts whose paths hold opaque document or object ids: no entropy check on
# the path (query values, hex and one-case runs are still checked). Matched
# by suffix: each is a provider's name with no tenant hosts under it.
ID_PATH_HOSTS = ("docs.google.com", "drive.google.com", "github.com", "gitlab.com",
                 "githubusercontent.com", "dropbox.com", "overleaf.com", "youtube.com",
                 "youtu.be", "doi.org", "arxiv.org")
# The same, matched exactly: these names have tenant hosts under them.
ID_PATH_EXACT = ("zoom.us", "www.zoom.us", "box.com", "www.box.com", "app.box.com")
# Code hosts: a path segment that is a commit id (up to 64 hex) or a
# compare range of two (`a...b`) is not a hex payload.
CODE_HOSTS = ("github.com", "gitlab.com", "githubusercontent.com")
_COMMIT_RE = re.compile(r"[0-9a-f]{7,64}(?:\.\.\.?[0-9a-f]{7,64})?")


def _query(url: str) -> str:
    return url.split("?", 1)[1] if "?" in url else ""


def _host_in(host: str, suffixes) -> bool:
    host = (host or "").lower()
    return any(host == s or host.endswith("." + s) for s in suffixes)


_HOST_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")


def _owner_hosts_doc() -> dict:
    """The owner-private OWNER_HOSTS_FILE as a dict; {} when absent, when it
    fails owner_trust's checks or is not JSON, and in any process but an
    owner process (ea-bot must not widen the exemptions)."""
    if not _owner_trust.is_owner_process():
        return {}
    path = _owner_trust.private_dir() / OWNER_HOSTS_FILE
    try:
        doc = json.loads(_owner_trust.read_owner_file(path))
    except FileNotFoundError:
        return {}
    except (OSError, _owner_trust.OwnerTrustError, ValueError) as e:
        print(f"outbound_review: {path} not used: {e}", file=sys.stderr)
        return {}
    return doc if isinstance(doc, dict) else {}


_OWNER_HOSTS: dict | None = None


def owner_hosts() -> dict:
    """{"token_hosts", "id_path_hosts", "own_addresses"}: frozensets from the
    owner-private OWNER_HOSTS_FILE, read once per process. Hosts match
    exactly, never by suffix, so listing `ours.sharepoint.com` exempts
    that site and no other tenant's."""
    global _OWNER_HOSTS
    if _OWNER_HOSTS is None:
        doc = _owner_hosts_doc()

        def strings(key):
            v = doc.get(key)
            return [s.strip() for s in v if isinstance(s, str)] if isinstance(v, list) else []
        _OWNER_HOSTS = {
            "token_hosts": frozenset(h.lower() for h in strings("token_hosts")
                                     if _HOST_RE.match(h.lower())),
            "id_path_hosts": frozenset(h.lower() for h in strings("id_path_hosts")
                                       if _HOST_RE.match(h.lower())),
            "own_addresses": frozenset(a.casefold() for a in strings("own_addresses")
                                       if _EMAIL_RE.fullmatch(a))}
    return _OWNER_HOSTS


def _token_host(host: str) -> bool:
    host = (host or "").lower()
    return _host_in(host, TOKEN_HOSTS) or host in owner_hosts()["token_hosts"]


def _id_path_host(host: str) -> bool:
    host = (host or "").lower()
    return (_host_in(host, ID_PATH_HOSTS) or host in ID_PATH_EXACT
            or host in owner_hosts()["id_path_hosts"])


def entropy(text: str) -> float:
    """Shannon entropy of `text` in bits per character."""
    if not text:
        return 0.0
    n = len(text)
    return -sum(c / n * math.log2(c / n) for c in Counter(text).values())


def _cls(ch: str) -> str:
    if ch.isdigit():
        return "d"
    if ch.isupper():
        return "u"
    if ch.islower():
        return "l"
    return ""


def _switches(text: str) -> int:
    classes = [c for c in (_cls(ch) for ch in text) if c]
    return sum(1 for a, b in zip(classes, classes[1:]) if a != b)


_HEX_PART_RE = re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{8,}(?![A-Za-z0-9])")


def opaque_token(text: str) -> bool:
    """`text` reads as an encoded payload (base64, a random token), not as
    words, a slug, a date or a hex id."""
    core = (text or "").strip("=")
    if re.fullmatch(r"[0-9a-fA-F-]+", core):
        return False   # hex: encoded_run judges it by length
    # A content hash between separators (main.3f2a9c1d.js) is hex too.
    core = _HEX_PART_RE.sub("", core)
    if len(core) < OPAQUE_MIN:
        return False
    return entropy(core) >= ENTROPY_MIN and _switches(core) >= len(core) * SWITCH_RATIO


_HEX_SEP_RE = re.compile(r"[-_.~:]")
_HEX_LONG_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9a-fA-F]{%d,}(?![0-9A-Fa-f])" % HEX_PART_MIN)
_ONE_CASE_RE = re.compile(r"[a-z0-9]{%d,}|[A-Z0-9]{%d,}" % (B32_MIN, B32_MIN))


def _one_case_run(run: str) -> bool:
    """A one-case run of letters and digits that reads as base32 or base36,
    not as a word with a number in it or a hex id."""
    if re.fullmatch(r"[0-9a-fA-F]+", run) or run.isdigit():
        return False
    digits = sum(ch.isdigit() for ch in run)
    return (digits >= 2 and _switches(run) * B32_SWITCH >= len(run)
            and entropy(run) >= B32_ENTROPY_MIN)


def _hex_digits(text: str) -> int:
    """The hex digits in `text` when it is hex and separators only, else 0."""
    core = _HEX_SEP_RE.sub("", text or "")
    return len(core) if core and re.fullmatch(r"[0-9a-fA-F]+", core) else 0


def encoded_run(text: str, *, code_host: bool = False) -> str:
    """Why `text` (one path segment or query value) holds encoded data that
    the entropy test lets through, or '': a hex run or a hex segment of
    HEX_MIN digits or more (a code host's whole commit id excepted), or a
    one-case base32-like run."""
    core = (text or "").strip("=")
    if code_host and _COMMIT_RE.fullmatch(core):
        return ""
    n = max([_hex_digits(core)] + [len(m.group(0)) for m in _HEX_LONG_RE.finditer(core)])
    if n >= HEX_MIN:
        return f"hex run of {n} characters"
    if any(_one_case_run(m.group(0)) for m in _ONE_CASE_RE.finditer(core)):
        return "base32-like run"
    return ""


def _label_reason(host: str) -> str:
    """A DNS label that reads as data (the subdomain carries the payload)."""
    labels = (host or "").lower().split(".")
    for lab in labels[:-2]:
        if len(lab) > LONG_LABEL:
            return f"DNS label of {len(lab)} characters"
        core = lab.replace("-", "")
        if (len(core) >= OPAQUE_MIN and not lab.startswith("xn--")
                and entropy(core) >= LABEL_ENTROPY_MIN
                and _switches(core) >= len(core) * LABEL_SWITCH_RATIO):
            return "encoded DNS label"
    return ""


def component_reason(url: str) -> str:
    """A long or encoded path segment or query value, or a DNS label that
    reads as data; '' when none."""
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if _token_host(host):
        return ""
    why = _label_reason(host)
    if why:
        return why
    id_path = _id_path_host(host)
    code = _host_in(host, CODE_HOSTS)
    hex_total = 0
    for seg in parts.path.split("/"):
        seg = urllib.parse.unquote(seg)
        if len(seg) > LONG_COMPONENT:
            return f"path segment of {len(seg)} characters"
        why = encoded_run(seg, code_host=code)
        if why:
            return f"{why} in the path"
        if not id_path and opaque_token(seg):
            return "encoded path segment"
        if not (code and _COMMIT_RE.fullmatch(seg)):
            hex_total += sum(len(m.group(0)) for m in _HEX_LONG_RE.finditer(seg))
    if hex_total >= HEX_TOTAL:
        return f"hex runs of {hex_total} characters across the path"
    for _key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if len(value) > LONG_COMPONENT:
            return f"query value of {len(value)} characters"
        if value.startswith(("http://", "https://")):
            continue   # a wrapped URL: its own hop is logged and checked
        why = encoded_run(value)
        if why:
            return f"{why} in a query value"
        if opaque_token(value):
            return "encoded query value"
    return ""


def _email_reason(url: str) -> str:
    """An email address anywhere in the URL, on every host. The one value
    let through: the owner's own address (OWNER_HOSTS_FILE) in a Safe
    Links `data=`, where the wrapper names the mailbox it was sent to."""
    text = urllib.parse.unquote(url or "")
    if not _EMAIL_RE.search(text):
        return ""
    own = owner_hosts()["own_addresses"]
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return "email address in the URL"
    if own and _host_in(host, SAFELINKS_HOSTS):
        def keep(m):
            return "" if m.group(0).casefold() in own else m.group(0)
        pairs = [(k, _EMAIL_RE.sub(keep, v) if k == "data" else v)
                 for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
        text = " ".join([urllib.parse.unquote(f"{parts.netloc}{parts.path}")]
                        + [f"{k}={v}" for k, v in pairs]
                        + [urllib.parse.unquote(parts.fragment)])
    return "email address in the URL" if _EMAIL_RE.search(text) else ""


def exfil_reason(url: str) -> str:
    """Why a fetch_url URL looks like it carries data OUT, or '' when it
    does not: an email address (on every host), or a long or encoded path
    segment, query value or DNS label (component_reason). `long_url`
    reports length alone as an informational stat, not a flag."""
    if not url:
        return ""
    return _email_reason(url) or component_reason(url)


def long_url(url: str) -> bool:
    """Informational: the URL is unusually long. Not a flag on its own
    (OAuth flows are long), but worth a count in the summary."""
    return bool(url) and (len(url) > _URL_LEN_FLAG or len(_query(url)) > _QUERY_LEN_FLAG)


def analyze(records: list[dict], *, spike_n: int = DEFAULT_SPIKE_N) -> dict:
    """A report over already-windowed records (each a parsed log line)."""
    fetches = [r for r in records if r.get("event") == "fetch"]
    blocked = [r for r in records if r.get("event") == "blocked"]
    errored = [r for r in records if r.get("event") == "error"]
    hosts = Counter(r.get("host") for r in fetches if r.get("host"))
    spikes = [(h, n) for h, n in hosts.most_common() if n > spike_n]
    seen_exfil, exfil, long_urls = set(), [], 0
    for r in fetches:
        url = r.get("url") or ""
        if long_url(url):
            long_urls += 1
        why = exfil_reason(url)
        if why and url not in seen_exfil:
            seen_exfil.add(url)
            exfil.append({"url": url, "host": r.get("host"), "why": why})
    polls = [r for r in records if r.get("event") == "poll_submit"]
    refused = sum(int(r.get("refused") or 0) for r in records
                  if r.get("event") == "render" and isinstance(r.get("refused"), int))
    return {
        "fetches": len(fetches),
        "distinct_hosts": len(hosts),
        "host_counts": dict(hosts),
        "blocked": [{"url": r.get("url"), "host": r.get("host"),
                     "reason": r.get("reason")} for r in blocked],
        "errored": len(errored),
        "errored_sample": [{"url": r.get("url"), "reason": r.get("reason")}
                           for r in errored[:3]],
        "exfil": exfil,
        "long_urls": long_urls,
        "spikes": spikes,
        "polls": [{"host": r.get("host") or "", "poll_id": str(r.get("poll_id") or ""),
                   "name": str(r.get("name") or ""), "slots": r.get("slots"),
                   "bytes": r.get("bytes"), "outcome": r.get("outcome")} for r in polls],
        "render_refused": refused,
    }


def analyze_polls(report: dict, owner_name: str | None) -> None:
    """In place: a poll submitted under a name that is not the owner's own
    (the owner-private profile.json) is a flag. Without that file there is
    nothing trusted to compare with, and the names are only listed
    (`poll_names_unchecked`), marked unchecked in the post."""
    if not owner_name:
        report["poll_names"] = []
        report["poll_names_unchecked"] = sorted(
            {" ".join(str(p.get("name") or "").split()) for p in report.get("polls") or []})
        return
    report["poll_names_unchecked"] = []
    want = " ".join(owner_name.split()).casefold()
    report["poll_names"] = [p for p in report.get("polls") or []
                            if " ".join(p["name"].split()).casefold() != want]


def analyze_briefs(records: list[dict]) -> dict:
    """The remote writer's briefs in the window: count and sizes."""
    briefs = [r for r in records if r.get("event") == "remote_brief"]

    def n(r, k):
        v = r.get(k)
        return v if isinstance(v, int) else 0
    return {"briefs": len(briefs),
            "brief_chars": sum(n(r, "brief_chars") for r in briefs),
            "brief_sent_chars": sum(n(r, "sent_chars") for r in briefs),
            "brief_max_sent": max((n(r, "sent_chars") for r in briefs), default=0),
            "brief_hosts": sorted({str(r.get("host")) for r in briefs if r.get("host")})}


# Model-chosen requests (WebFetch, WebSearch): an opaque run this long in a
# URL or a query reads as data, not as words or an id. A base64 run has no
# separators and mixes upper case, lower case and digits; a base32 run is
# one case with digits mixed in; a slug or a title has separators. A hex
# run of 32 counts off the path; on the path, where a commit id in a
# code-host URL is normal, component_reason judges hex by HEX_MIN.
_B64_RUN_RE = re.compile(r"[A-Za-z0-9]{32,}")
_HEX_RUN_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9a-fA-F]{32,}(?![0-9A-Fa-f])")
_PCT_RUN_RE = re.compile(r"(?:%[0-9A-Fa-f]{2}){12,}")
_SEARCH_LEN_FLAG = 200


def _opaque(text: str, *, hex_runs: bool = True) -> bool:
    """A long base64, base32, hex or percent-encoded run in `text`."""
    text = text or ""
    for m in _B64_RUN_RE.finditer(text):
        run = m.group(0)
        if (re.search(r"[a-z]", run) and re.search(r"[A-Z]", run)
                and re.search(r"\d", run)):
            return True
    if any(_one_case_run(m.group(0)) for m in _ONE_CASE_RE.finditer(text)):
        return True
    if hex_runs and _HEX_RUN_RE.search(text):
        return True
    return bool(_PCT_RUN_RE.search(text))


def web_tool_reason(rec: dict) -> str:
    """Why a model web request looks like it carries data OUT, or ''."""
    if rec.get("event") == "websearch":
        q = str(rec.get("query") or "")
        if _EMAIL_RE.search(q):
            return "email address in the search"
        if len(q) > _SEARCH_LEN_FLAG:
            return f"search of {len(q)} characters"
        if _opaque(q) or any(opaque_token(w) for w in q.split()):
            return "encoded payload in the search"
        return ""
    url = str(rec.get("url") or "")
    why = _email_reason(url)
    if why:
        return why
    query = _query(url)
    if len(query) > _QUERY_LEN_FLAG:
        return f"query string of {len(query)} characters"
    path = url.split("?", 1)[0].split("#", 1)[0].split("://", 1)[-1]
    if _opaque(query) or _opaque(path, hex_runs=False):
        return "encoded payload in the URL"
    # Hex on the path (HEX_MIN, with the commit-id exception) and the
    # segment, value and DNS label checks.
    return component_reason(url)


def analyze_web_tools(records: list[dict], *, spike_n: int = DEFAULT_SPIKE_N) -> dict:
    """The model-request half of the report, over already-windowed records
    of web_tool_log."""
    fetches = [r for r in records if r.get("event") == "webfetch"]
    searches = [r for r in records if r.get("event") == "websearch"]
    hosts = Counter(r.get("host") for r in fetches if r.get("host"))
    flagged, seen = [], set()
    for r in fetches + searches:
        why = web_tool_reason(r)
        key = r.get("url") or r.get("query") or ""
        if why and key not in seen:
            seen.add(key)
            flagged.append({"tool": "WebFetch" if r.get("event") == "webfetch" else "WebSearch",
                            "what": key, "host": r.get("host") or "", "lane": r.get("lane") or "",
                            "run_id": r.get("run_id") or "", "why": why})
    cron = sorted({r.get("lane") for r in fetches + searches
                   if str(r.get("lane") or "").startswith("cron:")})
    return {"web_fetches": len(fetches), "web_searches": len(searches),
            "web_hosts": len(hosts), "web_host_counts": dict(hosts),
            "web_exfil": flagged, "web_cron_lanes": cron,
            "web_spikes": [(h, n) for h, n in hosts.most_common() if n > spike_n]}


# ── hosts across days ─────────────────────────────────────────────────

HISTORY_DAYS = 14
HOST_KEEP_DAYS = 180
HOST_KEEP_MAX = 20000
VOLUME_MIN = 10
VOLUME_FACTOR = 5
VOLUME_HISTORY_MIN = 3
NEW_HOST_VOLUME = 10
_MULTI_SLD = {"co", "com", "ac", "edu", "gov", "org", "net", "ne", "or", "go"}


def registrable(host: str) -> str:
    """An approximation of the registrable domain (no public-suffix list):
    the last two labels, three under a two-letter country code with a
    generic second level (example.co.uk, uni.edu.au)."""
    host = (host or "").lower().rstrip(".")
    if not host or re.fullmatch(r"[\d.]+", host) or ":" in host:
        return host
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _MULTI_SLD:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def analyze_hosts(counts: Counter | dict, history: dict | None, today: str) -> dict:
    """New hosts, unusual volume and many subdomains over the window's
    request counts per host (fetch_url, the other modules and WebFetch
    together). `history` is {host: {"first", "last", "days": {date: n}}}
    from earlier reviews; None means there is none yet, and nothing is new."""
    counts = Counter({h: n for h, n in dict(counts).items() if h})
    new, volume = [], []
    for host, n in counts.most_common():
        past = (history or {}).get(host)
        if history is not None and not past:
            new.append((host, n))
            continue
        days = {d: c for d, c in ((past or {}).get("days") or {}).items() if d != today}
        if len(days) >= VOLUME_HISTORY_MIN:
            mean = sum(days.values()) / HISTORY_DAYS
            if n >= max(VOLUME_MIN, VOLUME_FACTOR * mean, 1):
                volume.append((host, n, round(mean, 1)))
    by_domain: dict[str, set] = {}
    for host in counts:
        by_domain.setdefault(registrable(host), set()).add(host)
    subdomains = sorted(((d, len(hs)) for d, hs in by_domain.items() if len(hs) > SUBDOMAIN_N),
                        key=lambda x: -x[1])
    return {"new_hosts": new,
            "new_host_volume": [(h, n) for h, n in new if n >= NEW_HOST_VOLUME],
            "volume": volume, "subdomains": subdomains}


def update_history(history: dict | None, counts: Counter | dict, today: str,
                   seen: set[str] = frozenset()) -> dict:
    """The host history after today's window (a new dict). `seen` adds hosts
    from before the window when the history starts."""
    hist = {h: dict(v) for h, v in (history or {}).items() if isinstance(v, dict)}
    for host in seen:
        hist.setdefault(host, {"first": today, "last": today, "days": {}})
    for host, n in dict(counts).items():
        if not host:
            continue
        rec = hist.setdefault(host, {"first": today, "last": today, "days": {}})
        days = dict(rec.get("days") or {})
        days[today] = days.get(today, 0) + int(n)
        rec["days"] = days
        rec["last"] = today
    try:
        cut_days = (datetime.fromisoformat(today) - timedelta(days=HISTORY_DAYS)).date().isoformat()
        cut_keep = (datetime.fromisoformat(today) - timedelta(days=HOST_KEEP_DAYS)).date().isoformat()
    except ValueError:
        cut_days = cut_keep = ""
    for host in list(hist):
        rec = hist[host]
        rec["days"] = {d: c for d, c in (rec.get("days") or {}).items() if d > cut_days}
        if str(rec.get("last") or "") < cut_keep:
            del hist[host]
    if len(hist) > HOST_KEEP_MAX:
        keep = sorted(hist, key=lambda h: str(hist[h].get("last") or ""), reverse=True)
        hist = {h: hist[h] for h in keep[:HOST_KEEP_MAX]}
    return hist


# ── the report ────────────────────────────────────────────────────────

def has_flags(report: dict) -> bool:
    return bool(report.get("blocked") or report.get("exfil") or report.get("spikes")
                or report.get("web_exfil") or report.get("web_cron_lanes")
                or report.get("web_spikes") or report.get("log_anomalies")
                or report.get("new_host_volume") or report.get("volume")
                or report.get("subdomains") or report.get("poll_names"))


MAX_CHECK_LINES = 8
MAX_NEW_HOSTS = 10


def _capped(lines: list[str], cap: int = MAX_CHECK_LINES) -> list[str]:
    if len(lines) <= cap:
        return lines
    return lines[:cap] + [f"- (and {len(lines) - cap} more of these)"]


def _notes(report: dict) -> list[str]:
    """The informational lines: new hosts, polls, the remote writer."""
    out = []
    new = report.get("new_hosts") or []
    if new:
        shown = ", ".join(h for h, _ in new[:MAX_NEW_HOSTS])
        more = f" (+{len(new) - MAX_NEW_HOSTS} more)" if len(new) > MAX_NEW_HOSTS else ""
        out.append(f"First seen today ({len(new)}): {shown}{more}.")
    polls = report.get("polls") or []
    if polls:
        hosts = ", ".join(sorted({p["host"] for p in polls if p.get("host")}))
        line = f"Polls: {len(polls)} submission(s)" + (f" to {hosts}" if hosts else "")
        names = report.get("poll_names_unchecked") or []
        if names:
            shown = ", ".join(repr(n[:60]) for n in names[:MAX_NEW_HOSTS])
            more = f" (+{len(names) - MAX_NEW_HOSTS} more)" if len(names) > MAX_NEW_HOSTS else ""
            line += (f", under {shown}{more} (names unchecked: no owner-private "
                     f"profile.json to compare with)")
        out.append(line + ".")
    if report.get("briefs"):
        out.append(f"Remote writer: {report['briefs']} brief(s), "
                   f"{report.get('brief_chars', 0)} characters of brief, "
                   f"{report.get('brief_sent_chars', 0)} characters sent in all "
                   f"(largest {report.get('brief_max_sent', 0)}).")
    return out


def render(report: dict, *, window: str = "since the last review") -> str:
    """The report as an assistant-voice message (STE). Always returns a
    line: a short all-clear when nothing tripped, so the user knows the
    review ran."""
    n, h = report.get("fetches", 0), report.get("distinct_hosts", 0)
    head = f":globe_with_meridians: Outbound web review ({window}): {n} request(s) to {h} host(s)."
    if "web_fetches" in report:
        head += (f" Model web tools: {report['web_fetches']} WebFetch to "
                 f"{report.get('web_hosts', 0)} host(s), {report.get('web_searches', 0)} WebSearch.")
    extra = []
    if report.get("errored"):
        extra.append(f"{report['errored']} failed")
    if report.get("long_urls"):
        extra.append(f"{report['long_urls']} long URL(s), e.g. OAuth")
    if report.get("render_refused"):
        extra.append(f"PDF renderer refused {report['render_refused']} remote request(s)")
    tail = (" " + "; ".join(extra) + ".") if extra else ""
    notes = _notes(report)
    if not has_flags(report):
        return "\n".join([head + " Nothing looked off." + tail] + notes)
    lines = [head + " Review these:"]
    for a in report.get("log_anomalies") or []:
        lines.append(f"- LOG CHECK {a}")
    for b in report.get("blocked") or []:
        lines.append(f"- BLOCKED {b.get('host') or b.get('url')}: {b.get('reason')}")
    lines += _capped([f"- CHECK URL ({e['why']}) to {e.get('host')}: {e['url'][:160]}"
                      for e in report.get("exfil") or []])
    for host, cnt in report.get("spikes") or []:
        lines.append(f"- HIGH VOLUME {host}: {cnt} requests")
    lines += _capped([f"- CHECK {e['tool']} ({e['why']})"
                      f"{' in ' + e['lane'] if e.get('lane') else ''}: {e['what'][:160]}"
                      for e in report.get("web_exfil") or []])
    for lane in report.get("web_cron_lanes") or []:
        lines.append(f"- WEB TOOL IN A CRON LANE {lane}: the cron lanes deny WebFetch and "
                     f"WebSearch, so the deny list did not hold")
    for host, cnt in report.get("web_spikes") or []:
        lines.append(f"- HIGH VOLUME WebFetch {host}: {cnt} requests")
    for host, cnt in report.get("new_host_volume") or []:
        lines.append(f"- NEW HOST {host}: {cnt} requests on its first day")
    for host, cnt, mean in report.get("volume") or []:
        lines.append(f"- UNUSUAL VOLUME {host}: {cnt} requests (about {mean} a day before)")
    for dom, cnt in report.get("subdomains") or []:
        lines.append(f"- MANY SUBDOMAINS {dom}: {cnt} hosts")
    for p in report.get("poll_names") or []:
        lines.append(f"- POLL NAME {p['host']} {p['poll_id']}: submitted as "
                     f"{p['name'][:60]!r}, not the configured name")
    lines += notes
    if tail:
        lines.append("(" + tail.strip() + ")")
    return "\n".join(lines)


# ── the logs, read for the daily run ──────────────────────────────────

def _log_specs() -> list[tuple[str, Path, str]]:
    """(cursor name, path, kind): kind "resident" is ea-bot-writable and
    checked; "owner" is owner-private."""
    return [("fetch_url", LOG_PATH, "resident"),
            ("web_tools", Path(_web_log.log_path()), "owner"),
            ("owner_outbound", Path(_out_log.owner_log_path()), "owner")]


NO_CURSOR = ("no review cursor in the owner-private directory (a first run, or the file "
             "was removed); each log was read from its start")


def review(state: dict | None, *, now: datetime | None = None,
           spike_n: int = DEFAULT_SPIKE_N, specs=None,
           max_bytes: int = FRESH_MAX_BYTES) -> tuple[dict, dict]:
    """(report, the new state) for one run. With no state (no cursor file)
    or a state read_state could not use (`cursor_problem`), each log is
    read from its start (the last `max_bytes` at most), the post names
    the reason as a log check, and the host history starts from what the
    logs hold. No run seeds a cursor at the end of a log unread."""
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    anomalies: list[str] = []
    fresh = state is None or bool(state.get("cursor_problem"))
    if state is None:
        anomalies.append(NO_CURSOR)
    elif fresh:
        anomalies.append(f"the review cursor {state['cursor_problem']}; it is set aside "
                         f"and each log was read from its start")
    legacy_ts = state.get("last_ts") if (not fresh and state.get("legacy")) else None
    legacy_dt = _parse_ts(legacy_ts)
    if legacy_dt is not None and legacy_dt > now + timedelta(seconds=SKEW_S):
        anomalies.append(f"the legacy cursor time {legacy_ts} is in the future; ignored, "
                         f"and each log was read from its start")
        legacy_ts = ""
    cursors = {} if fresh else dict(state.get("logs") or {})
    history = None if fresh else state.get("hosts")
    windows, seen = {}, set()
    for name, path, kind in (specs or _log_specs()):
        data = (_read_shared_bytes(path) if kind == "resident"
                else _read_owner_log(path, f"{name} log"))
        label = Path(path).name
        prior = cursors.get(name) if isinstance(cursors.get(name), dict) else None
        if not fresh and legacy_ts is None and prior is None and data:
            anomalies.append(f"{label} had no cursor in the review state; read from its start")
        recs, anom, cur = read_window(
            data, prior, inode=_inode(path), label=label,
            check_ts=(kind == "resident"),
            legacy_ts=legacy_ts if prior is None else None,
            now=now, max_bytes=max_bytes)
        if history is None:
            # The host history starts now: every host the log holds is
            # known, so the first day lists nothing as new.
            seen |= prior_hosts(data, len(data or b""))
        windows[name], cursors[name] = (recs, kind), cur
        anomalies += anom
    new_state = {"logs": cursors, "last_ts": now.strftime("%Y-%m-%dT%H:%M:%SZ")}

    fetch_recs = windows.get("fetch_url", ([], ""))[0]
    owner_recs = windows.get("owner_outbound", ([], ""))[0]
    web_recs = windows.get("web_tools", ([], ""))[0]
    # A writer brief counts only from the owner-private log; the ea-bot
    # log cannot vouch for one.
    report = analyze([r for r in fetch_recs if r.get("event") != "remote_brief"]
                     + [r for r in owner_recs if r.get("event") != "remote_brief"],
                     spike_n=spike_n)
    report.update(analyze_web_tools(web_recs, spike_n=spike_n))
    report.update(analyze_briefs(owner_recs))
    analyze_polls(report, _owner_trust.owner_profile_name())
    counts = Counter(report.get("host_counts") or {})
    counts.update(report.get("web_host_counts") or {})
    report.update(analyze_hosts(counts, history, today))
    report["log_anomalies"] = anomalies
    new_state["hosts"] = update_history(history, counts, today, seen)
    return report, new_state


# ── compatibility readers (time windows) ──────────────────────────────

def _records_after(text: str | None, since_ts: str | None) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and (since_ts is None or (d.get("ts") or "") > since_ts):
            out.append(d)
    return out


def read_records(since_ts: str | None, path: Path = LOG_PATH) -> list[dict]:
    """Parsed fetch_url log records with ts strictly after `since_ts` (all
    when None). The log is resident space and its records go into a chat
    post, so an owner process reads it with the resident's access
    (resident_io). The daily run reads it by byte offset (`review`)."""
    return _records_after(_read_shared_text(path), since_ts)


def read_web_records(since_ts: str | None, path: Path | None = None) -> list[dict]:
    """web_tool_log records with ts strictly after `since_ts`. The log is
    the owner-private copy; in the owner-private directory it is read
    through owner_trust's checks, and a log that fails them counts as
    absent (the review says so on stderr). It is never read from ea-bot
    space, since ea-bot could rewrite a copy there."""
    path = Path(path) if path is not None else _web_log.log_path()
    data = _read_owner_log(path, "web tool log")
    return _records_after(None if data is None else data.decode("utf-8", "replace"), since_ts)


def latest_ts(records: list[dict]) -> str:
    return max((r.get("ts") or "" for r in records), default="")
