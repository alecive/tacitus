#!/usr/bin/env python3
"""publish_gate: the owner's click before the EA publishes outside the host.

Two verbs put the EA's work where other people read it: `site_tools.py
publish` (a GitHub Pages site, public, or a private status site that
partners read) and `overleaf_tools.py push` (a shared Overleaf project).
Each runs only with a one-time token the bot mints after the owner clicks
the card, and each publishes exactly the commit the card showed.

The flow:

1. The EA commits in its clone (`site_tools.py commit`, `overleaf_tools.py
   commit`) and stages ~/.ea/state/pending-publish.json naming the target
   and that commit:

       {"site": "<slug>", "commit": "<sha>", "thread_ts": "..."}
       {"overleaf": "<clone dir>", "commit": "<sha>", "thread_ts": "..."}

2. The bot claims the file (the staged-action claim protocol), runs the
   tool's read-only `preview` verb as the resident, and posts a :lock: card
   with the files the commit changes, the commit it lands on, and a scan
   of the added lines for email headers, email addresses, phone numbers and
   message-id-like strings (`scan_diff`, run owner side on the diff text).

3. The click mints a token bound to {target, name, commit, base}, plus
   the project id for an Overleaf push: the commit, the remote head and
   the project the card showed. The bot runs the gated
   verb as the resident with the token in EA_CARD_TOKEN. The verb refuses
   unless the remote head still equals `base` and the token takes, then
   pushes that commit (never a force) and nothing else.

The token store is plugin_cards' (the same mint and consume, under a card
id no plugin can declare, since plugin card ids admit no underscore). On a
mediated install the bot mints into the owner-private directory and the
resident's check goes through the mediator, so no file the resident writes
passes it. The git credentials stay in ea-bot space, so the gate binds the
EA's tools, not the token: a lane with raw git and the token could push
without it (docs/SECURITY.md, "Public publishing").
"""
from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import card_text as _ct
    import owner_trust as _owner_trust
    import plugin_cards as _pcards
except ModuleNotFoundError:  # imported as preprocessor.publish_gate
    from preprocessor import card_text as _ct
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import plugin_cards as _pcards

STAGE_FILE = "pending-publish.json"
TOKEN_NAMESPACE = "core"
CARD_IDS = {"site": "site_publish", "overleaf": "overleaf_push"}
TOKEN_ENV = "EA_CARD_TOKEN"
# Tests point this at a scratch file; None is plugin_cards' own choice.
TOKEN_PATH: Path | None = None
RESIDENT_USER = "ea-bot"
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PROJECT_RE = re.compile(r"^[0-9a-f]{24}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")
# The owner-private copy of the sites (slug -> repo, url, title, private),
# when the owner keeps one; github.json is ea-bot space.
OWNER_SITES_FILE = "sites.json"
GITHUB_API = "https://api.github.com"
GITHUB_API_TIMEOUT_S = 10
CARD_ADDED_FILES = 6
CARD_ADDED_PER_FILE = 5
CARD_ADDED_WIDTH = 110
CARD_ADDED_BUDGET = 1800
PREVIEW_TIMEOUT_S = 180
PUBLISH_TIMEOUT_S = 480
CARD_FILES_MAX = 12
CARD_FINDINGS_MAX = 6
SCAN_LINE_MAX = 4000


class GateError(Exception):
    """A refusal of the publish gate, reported in the tool's error shape."""


# ── the staged request ────────────────────────────────────────────────

def parse_request(payload) -> tuple[dict | None, str]:
    """(request, "") for a staged payload, or (None, why). A request is
    {"target": "site"|"overleaf", "name": slug or clone dir, "commit": sha}."""
    if not isinstance(payload, dict):
        return None, "the publish request is not a JSON object"
    site = str(payload.get("site") or "").strip()
    clone = str(payload.get("overleaf") or "").strip()
    commit = str(payload.get("commit") or "").strip().lower()
    if bool(site) == bool(clone):
        return None, 'the publish request names one of "site" or "overleaf"'
    if not SHA_RE.match(commit):
        return None, f'the publish request needs "commit": a commit id, not {commit!r}'
    if site:
        if not SLUG_RE.match(site):
            return None, f"not a site slug: {site!r}"
        return {"target": "site", "name": site, "commit": commit}, ""
    if (not clone.startswith("/") or len(clone) > 1024
            or any(c in clone for c in "\0\n\r")):
        return None, f'"overleaf" must be the clone\'s absolute path, not {clone!r}'
    return {"target": "overleaf", "name": clone, "commit": commit}, ""


def item_of(req: dict, preview: dict) -> dict:
    """What the token authorizes: the target, the full commit and the
    remote head it lands on, both as the preview resolved them; for a site
    the repository the push goes to (github.json can be edited after the
    card, so the slug alone does not bind it), and for an Overleaf push the
    project id the clone pushes to."""
    commit, base = str(preview.get("commit") or ""), str(preview.get("base") or "")
    if not (FULL_SHA_RE.match(commit) and FULL_SHA_RE.match(base)):
        raise GateError("the preview did not name a full commit and base")
    if req["target"] == "site":
        repo = str(preview.get("repo") or "")
        if not REPO_RE.match(repo):
            raise GateError("the preview did not name the site's repository")
        return {"target": "site", "name": req["name"], "repo": repo, "commit": commit,
                "base": base}
    project = str(preview.get("project") or "")
    if not PROJECT_RE.match(project):
        raise GateError("the preview did not name the Overleaf project id")
    return {"target": "overleaf", "name": str(preview.get("dir") or req["name"]),
            "commit": commit, "base": base, "project": project}


def _card_id(target: str) -> str:
    try:
        return CARD_IDS[target]
    except KeyError:
        raise GateError(f"unknown publish target {target!r}") from None


# ── the token ─────────────────────────────────────────────────────────

def mint(item: dict, *, path=None) -> str:
    """A one-time token for this exact item. The bot calls it after a click."""
    return _pcards.mint_token(TOKEN_NAMESPACE, _card_id(item["target"]), item,
                              path=path or TOKEN_PATH)


def consume(raw: str, item: dict, *, path=None) -> bool:
    """True once for a token minted for this item; the token is gone after."""
    if not raw:
        return False
    return _pcards.consume_token(raw, TOKEN_NAMESPACE, _card_id(item["target"]), item,
                                 path=path or TOKEN_PATH)


def require_token(raw: str | None, item: dict) -> None:
    """Raise GateError unless `raw` is a live token for exactly this item.
    The gated verbs call it after their own checks, right before the push."""
    raw = raw if raw is not None else os.environ.get(TOKEN_ENV, "")
    if not raw:
        raise GateError("BLOCKED: this verb publishes outside the host and runs only "
                        "after the owner clicks the card. Commit, then stage "
                        f"~/.ea/state/{STAGE_FILE}; the bot posts the card")
    if not consume(raw, item):
        raise GateError("BLOCKED: invalid, expired or already used confirmation token, "
                        "or one minted for another commit. Stage the commit again")


# ── the scan ──────────────────────────────────────────────────────────

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
HEADER_RE = re.compile(
    r"^\s*(?:>\s*)*(?:From|To|Cc|Bcc|Reply-To|Subject|Date|Sent|Message-ID|In-Reply-To"
    r"|References|Return-Path|Received|Delivered-To|X-[A-Za-z0-9-]+):\s+\S", re.I)
EMAIL_RE = re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE_RE = re.compile(
    r"(?<![\w/.-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w/-])")
MSGID_RE = re.compile(r"<[^<>\s@]{6,}@[^<>\s@]+\.[^<>\s@]+>"
                      r"|\bAA[MQ]k[A-Za-z0-9+/_=-]{30,}")
SCAN_KINDS = (("email header", HEADER_RE), ("message id", MSGID_RE),
              ("email address", EMAIL_RE), ("phone number", PHONE_RE))


def added_lines(diff: str) -> list[tuple[str, int, str]]:
    """(file, line number, text) for each line a unified diff adds."""
    out, path, n = [], "", 0
    for line in (diff or "").splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
            continue
        if line.startswith("--- "):
            continue
        m = HUNK_RE.match(line)
        if m:
            n = int(m.group(1))
            continue
        if line.startswith("+") and path:
            out.append((path, n, line[1:]))
            n += 1
        elif line.startswith(" ") and path:
            n += 1
    return out


def scan_diff(diff: str) -> dict:
    """Findings in the lines a diff adds: email headers, message ids, email
    addresses and phone numbers. A message id's address counts once, as the
    message id. Returns {"findings": [...], "counts": {kind: n}}."""
    findings, counts = [], {}
    for path, n, text in added_lines(diff):
        text = text[:SCAN_LINE_MAX]
        taken: list[tuple[int, int]] = []
        for kind, rx in SCAN_KINDS:
            for m in rx.finditer(text):
                span = m.span()
                if any(s <= span[0] < e for s, e in taken):
                    continue
                if kind == "email header":
                    span = (0, len(text))
                taken.append(span)
                counts[kind] = counts.get(kind, 0) + 1
                findings.append({"kind": kind, "file": path, "line": n,
                                 "match": m.group(0).strip()[:80]})
                if kind == "email header":
                    break
    return {"findings": findings, "counts": counts}


# ── running the tools as the resident ─────────────────────────────────

TOOLS = {"site": "site_tools.py", "overleaf": "overleaf_tools.py"}


def _resident_home() -> Path:
    return Path(pwd.getpwnam(RESIDENT_USER).pw_dir)


def resident_argv(target: str, args: list[str], *, tree: Path, home: Path, ea_dir: str,
                  token: str = "") -> list[str]:
    """The command line that runs a tool verb as the resident: the code of
    `tree` (the bot's own, deployed tree), the system interpreter with the
    environment's Python variables ignored, and a clean environment."""
    python = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"
    env = [f"HOME={home}", f"EA_DIR={ea_dir}", "PATH=/usr/local/bin:/usr/bin:/bin",
           "GIT_TERMINAL_PROMPT=0"]
    if token:
        env.append(f"{TOKEN_ENV}={token}")
    script = str(Path(tree) / "preprocessor" / TOOLS[target])
    return ["sudo", "-n", "-u", RESIDENT_USER, "env", "-i", *env, python, "-E", "-s",
            script, *args]


def _default_runner(argv, timeout):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd="/",
                       stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout, p.stderr


def _last_json(text: str) -> dict | None:
    text = (text or "").strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def run_tool(target: str, args: list[str], *, tree: Path, ea_dir: str, token: str = "",
             timeout: int = PREVIEW_TIMEOUT_S, runner=None, home: Path | None = None) -> dict:
    """Run one verb as the resident. Returns the tool's JSON object, or
    {"status": "error", "error": ...} for a refusal, a failure or a timeout."""
    argv = resident_argv(target, args, tree=tree, home=home or _resident_home(),
                         ea_dir=ea_dir, token=token)
    run = runner or _default_runner
    try:
        rc, out, err = run(argv, timeout)
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"timed out after {timeout} s; it may have run"}
    except OSError as e:
        return {"status": "error", "error": str(e)[:300]}
    if rc == 0:
        res = _last_json(out)
        return res if res is not None else {"status": "error", "error": "no JSON result"}
    res = _last_json(err) or _last_json(out)
    if res and res.get("error"):
        return {"status": "error", "error": str(res["error"])[:600]}
    return {"status": "error", "error": f"exit {rc}: {((err or '') + (out or ''))[-300:]}"}


def preview(req: dict, **kw) -> dict:
    """The tool's read-only preview of the staged commit."""
    if req["target"] == "site":
        args = ["preview", "--site", req["name"], "--commit", req["commit"]]
    else:
        args = ["preview", "--dir", req["name"], "--commit", req["commit"]]
    return run_tool(req["target"], args, **kw)


def execute(item: dict, token: str, **kw) -> dict:
    """The gated verb for an approved item, with its token."""
    if item["target"] == "site":
        args = ["publish", "--site", item["name"]]
    else:
        args = ["push", "--dir", item["name"], "--project", str(item.get("project") or "")]
    args += ["--commit", item["commit"], "--base", item["base"]]
    kw.setdefault("timeout", PUBLISH_TIMEOUT_S)
    return run_tool(item["target"], args, token=token, **kw)


# ── the facts the card shows, read owner side ─────────────────────────

def _owner_sites(private_dir=None) -> dict:
    """{slug: site} from the owner-private sites.json, {} when the owner
    keeps none. A file that fails the owner checks raises OwnerTrustError."""
    path = Path(private_dir or _owner_trust.private_dir()) / OWNER_SITES_FILE
    try:
        doc = json.loads(_owner_trust.read_owner_file(path))
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise _owner_trust.OwnerTrustError(f"{path} is not valid JSON: {e}") from None
    sites = doc.get("sites", doc) if isinstance(doc, dict) else {}
    return {k: v for k, v in sites.items() if isinstance(v, dict)} if isinstance(sites, dict) else {}


def _github_repo(repo: str, timeout: int = GITHUB_API_TIMEOUT_S) -> tuple[int, dict]:
    """(HTTP status, JSON object) for an anonymous read of the repository
    from the GitHub API; (0, {}) when GitHub could not be reached."""
    url = f"{GITHUB_API}/repos/{urllib.parse.quote(repo, safe='/')}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "tacitus-publish-card"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read(1_000_000) or b"{}")
            return resp.status, data if isinstance(data, dict) else {}
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (OSError, ValueError):
        return 0, {}


def site_facts(req: dict, pv: dict, *, fetch=None, private_dir=None) -> dict:
    """What the card says about where a site publish lands, from owner-side
    sources. The preview's title, URL and private flag come from
    github.json, which the EA can edit, so the card takes them from the
    owner-private sites.json when the owner keeps one, and the reach from
    GitHub's own answer for the repository (an anonymous read, no token).
    Returns {"repo", "visibility" ("public", "not public" or "unknown"),
    "title", "url", "private", "source" ("owner" or "github.json"),
    "warnings": [...]} or {"refuse": why}."""
    repo = str(pv.get("repo") or "")
    if not REPO_RE.match(repo):
        return {"refuse": "the preview did not name the site's repository"}
    out = {"repo": repo, "title": pv.get("title") or req["name"], "url": pv.get("url") or "",
           "private": bool(pv.get("private")), "source": "github.json", "warnings": []}
    try:
        own = _owner_sites(private_dir).get(req["name"])
    except (_owner_trust.OwnerTrustError, OSError) as e:
        return {"refuse": f"the owner-private {OWNER_SITES_FILE} was refused: {e}"}
    if own is not None:
        own_repo = str(own.get("repo") or "")
        if own_repo.lower() != repo.lower():
            return {"refuse": f"github.json sends site {req['name']!r} to {repo}, but your "
                              f"{OWNER_SITES_FILE} names {own_repo or 'no repository'}"}
        out.update(title=own.get("title") or req["name"], url=own.get("url") or "",
                   private=bool(own.get("private")), source="owner")
    status, data = (fetch or _github_repo)(repo)
    if status == 200:
        vis = str(data.get("visibility") or ("private" if data.get("private") else "public"))
        out["visibility"] = "public" if vis == "public" else "not public"
        full = str(data.get("full_name") or "")
        if full and full.lower() != repo.lower():
            out["warnings"].append(f"GitHub names the repository {full}, not {repo}")
    elif status == 404:
        out["visibility"] = "not public"
    else:
        out["visibility"] = "unknown"
    if out["visibility"] == "public" and out["private"]:
        out["warnings"].append("the site is marked private, but GitHub reports its "
                               "repository public: anyone can read the pushed files there")
    return out


def _reach_line(facts: dict) -> str:
    repo = _ct.inert(facts["repo"], 120)
    vis = facts.get("visibility")
    if vis == "public":
        where = f"public: GitHub reports {repo} public, so anyone on the internet reads it"
    elif vis == "not public":
        where = f"GitHub shows no public {repo} (private to its collaborators, or missing)"
    else:
        where = f"GitHub did not answer for {repo}; its reach is unchecked"
    if facts.get("source") == "owner":
        site = ("private, behind sign-in" if facts.get("private")
                else "public") + " (your sites.json)"
    else:
        site = ("marked private" if facts.get("private") else "marked public") + \
               " in github.json, which the EA can edit"
    return f"Reach: {where}. The site is {site}."


def added_text_lines(diff: str) -> list[str]:
    """The text the diff adds, for the card: the first CARD_ADDED_PER_FILE
    added lines of the first CARD_ADDED_FILES files, each clipped and shown
    as an inert span (card_text.inert: no markup in it renders), with
    the count of what is left out, inside CARD_ADDED_BUDGET characters."""
    per_file: dict[str, list[str]] = {}
    for path, _n, text in added_lines(diff):
        per_file.setdefault(path, []).append(text)
    if not per_file:
        return []
    out = ["Added text (the first lines of each file):"]
    used, shown = 0, 0
    for path, rows in per_file.items():
        if shown >= CARD_ADDED_FILES or used >= CARD_ADDED_BUDGET:
            break
        out.append(f"  {_ct.inert(path, 80)} (+{len(rows)} line{'' if len(rows) == 1 else 's'}):")
        for text in rows[:CARD_ADDED_PER_FILE]:
            line = f"    + {_ct.inert(text, CARD_ADDED_WIDTH)}" if text.strip() else "    +"
            used += len(line)
            out.append(line)
            if used >= CARD_ADDED_BUDGET:
                break
        left = len(rows) - min(len(rows), CARD_ADDED_PER_FILE)
        if left:
            out.append(f"    and {left} more added line{'' if left == 1 else 's'}")
        shown += 1
    rest = len(per_file) - shown
    if rest:
        out.append(f"  and {rest} more file{'' if rest == 1 else 's'} with added lines")
    return out


# ── the card ──────────────────────────────────────────────────────────

def render(req: dict, pv: dict, scan: dict, facts: dict | None = None) -> dict:
    """Title, lines, button labels and the fallback text for the card. For a
    site, `facts` is site_facts(); without it the card says the reach is
    unchecked. The commit subject, the file paths, the added text and the
    scan's matches come from the commit, so each is an inert span
    (card_text.inert): shown as written, and no markup in it renders."""
    inert = _ct.inert
    commit, base = str(pv.get("commit") or "")[:7], str(pv.get("base") or "")[:7]
    extra: list[str] = []
    if req["target"] == "site":
        facts = facts or {"repo": str(pv.get("repo") or req["name"]), "visibility": "unknown",
                          "title": pv.get("title") or req["name"], "url": pv.get("url") or "",
                          "private": bool(pv.get("private")), "source": "github.json",
                          "warnings": []}
        where = facts.get("url") or req["name"]
        title = f":lock: *Publish to {inert(facts.get('title') or req['name'], 120)}*"
        first = f"Site: {inert(where, 200)}, repository {inert(facts['repo'], 120)}"
        if facts.get("source") != "owner":
            first += " (title and address from github.json, which the EA can edit)"
        extra.append(_reach_line(facts))
        extra += [f":warning: {inert(w, 300)}." for w in facts.get("warnings") or []]
        approve, word, noun = ":rocket: Publish", "publish", "The Publish button for this change"
    else:
        where = pv.get("project_url") or req["name"]
        title = ":lock: *Push to Overleaf*"
        first = (f"Project: {inert(where, 200)} (co-authors read it; a push can drop their "
                 f"comments on rewritten text)")
        approve, word, noun = ":outbox_tray: Push", "push", "The Push button for this change"
    lines = [first, *extra,
             f"Commit `{commit}` on top of `{base}`: {inert(pv.get('subject') or '', 200)}"]
    if int(pv.get("commits") or 1) > 1:
        lines[-1] += f" ({pv['commits']} commits)"
    files = pv.get("files") or []
    shown = ", ".join(f"{inert(f.get('path'))} ({inert(f.get('change'), 40)})"
                      for f in files[:CARD_FILES_MAX])
    more = f", and {len(files) - CARD_FILES_MAX} more" if len(files) > CARD_FILES_MAX else ""
    lines.append(f"Files ({len(files)}): {shown}{more}" if files else "Files: none")
    counts = scan.get("counts") or {}
    if not counts:
        lines.append(":white_check_mark: Scan: no email headers, email addresses, phone "
                     "numbers or message ids on the added lines.")
    else:
        summary = ", ".join(f"{n} {k}{'' if n == 1 else 's'}" for k, n in counts.items())
        lines.append(f":warning: Scan of the added lines: {summary}.")
        for f in (scan.get("findings") or [])[:CARD_FINDINGS_MAX]:
            lines.append(f"  - {inert(str(f['file']) + ':' + str(f['line']))} {f['kind']}: "
                         f"{inert(f['match'], 120)}")
        if len(scan.get("findings") or []) > CARD_FINDINGS_MAX:
            lines.append(f"  - and {len(scan['findings']) - CARD_FINDINGS_MAX} more")
    if pv.get("truncated"):
        lines.append("The diff was longer than the scan limit; the scan covers its start only.")
    lines += added_text_lines(pv.get("diff") or "")
    fallback = "\n".join([title.replace("*", "")] + lines)
    return {"title": title, "lines": lines, "approve_label": approve, "approve_word": word,
            "button_noun": noun, "fallback": fallback,
            "busy": (":hourglass_flowing_sand: Publishing..." if req["target"] == "site"
                     else ":hourglass_flowing_sand: Pushing to Overleaf...")}


def result_lines(item: dict, res: dict) -> tuple[str, str]:
    """(card status, thread message) for a finished gated run."""
    short = item["commit"][:7]
    if res.get("status") == "error":
        err = str(res.get("error") or "failed")[:300]
        return (f":x: Not published: {err}",
                f":warning: `{short}` was not published: {err}")
    if item["target"] == "site":
        url, commit_url = res.get("url") or "", res.get("commit_url") or ""
        live = res.get("live")
        state = ("The site serves it now." if live is True
                 else str(res.get("note") or "GitHub has it; the site has not updated yet."))
        return (f":white_check_mark: Published `{short}` to {url}",
                f":white_check_mark: Published `{short}` to {url} ({commit_url}). {state}")
    return (f":white_check_mark: Pushed `{short}` to Overleaf",
            f":white_check_mark: Pushed `{short}` to {res.get('project_url') or 'Overleaf'}.")
