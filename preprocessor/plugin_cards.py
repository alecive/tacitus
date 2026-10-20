"""Plugin confirmation cards: render one, mint its click token, run its
executor (docs/plugins.md, "Cards").

A plugin declares a card in its manifest: the pending file a lane or a cron
job stages items in, the card's wording as templates over each item's
fields, and the command that runs when the owner approves (and, optionally,
when they cancel). The bot claims the staged file with the same protocol as
the core cards, posts one card per item, and on a click calls `execute()`.

The executor contract:

- The item arrives as JSON on stdin and in EA_CARD_ITEM. EA_PLUGIN_NAME,
  EA_PLUGIN_DIR, EA_CARD_ID and EA_CARD_ACTION are set.
- On approve, a one-time token arrives in EA_CARD_TOKEN (and wherever the
  command names `{token}`). A resident executor checks it with
  `consume_token()` before it does the gated thing; the bot mints a token
  only after a click.
- The last line of stdout that is a JSON object with an "ok" key is the
  result: {"ok": true|false, "status": "<line for the card>",
  "message": "<line for the thread>"}. Without one, the bot reports the
  exit status and the output's tail.

Where the tokens live. On a mediated install the bot mints them into the
owner-private directory, which the resident cannot write, and a resident
executor's `consume_token()` asks the mail mediator (an owner process) to
take the token; nothing the resident writes can pass that check. On an
install with no mediator the tokens sit in the resident's state directory,
where the executor reads them itself.

A resident executor still runs as the resident, with the resident's files
and credentials, so an injected model could call the same client the
executor calls, without the executor. The token check stops the executor
from running without a click; it does not make the action itself
impossible for the resident. An action that must never run without a
click belongs in an owner-zone plugin, whose executor runs as the owner
from the owner's private directory.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import re
import secrets
import string
import subprocess
import sys
import time
from pathlib import Path

try:
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.plugin_cards
    from preprocessor import resident_io as _rio
try:
    import owner_trust as _owner_trust
except ModuleNotFoundError:
    from preprocessor import owner_trust as _owner_trust
try:
    import card_text as _ct
except ModuleNotFoundError:
    from preprocessor import card_text as _ct

TOKEN_FILE = "plugin-card-tokens.json"
MEDIATED_TOOL = "plugin_cards"
CONSUME_VERB = "consume-token"
TOKEN_TTL_S = 900
VALUE_MAX = 300
RESIDENT_USER = "ea-bot"
# Payload keys that route a staged item, never part of what it says.
ROUTING_KEYS = ("thread_ts", "channel")


# ── items and templates ───────────────────────────────────────────────

def clean_item(item: dict) -> dict:
    """The item without its routing keys."""
    return {k: v for k, v in (item or {}).items() if k not in ROUTING_KEYS}


def item_digest(item: dict) -> str:
    blob = json.dumps(clean_item(item), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def action_of(plugin: str, card_id: str, item: dict) -> str:
    """What a token authorizes: this card, for this exact item."""
    return f"card:{plugin}:{card_id}:{item_digest(item)}"


# The shape of an action string, checked where one arrives over the
# mediator rather than from this module's own call.
ACTION_RE = re.compile(r"^card:[a-z0-9][a-z0-9-]{0,63}:[A-Za-z0-9_.-]{1,64}:[0-9a-f]{64}$")


class _Fields(dict):
    """Template fields: a missing name reads as ''."""

    def __missing__(self, key):
        return ""


class _SafeFormatter(string.Formatter):
    """str.format over item fields, without attribute or index access.
    Each field holds the item's whole value; format_field applies the
    template's spec to it first and then the card's cut (_value), so a
    precision spec that shortens a value shows the cut marker and never
    splits an inert span."""

    def __init__(self, inert: bool = False):
        super().__init__()
        self.inert = inert

    _ABSENT = object()

    def get_field(self, field_name, args, kwargs):
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", field_name or ""):
            return self._ABSENT, field_name
        return kwargs.get(field_name, self._ABSENT), field_name

    def convert_field(self, value, conversion):
        return value if value is self._ABSENT else super().convert_field(value, conversion)

    def format_field(self, value, format_spec):
        if value is self._ABSENT:   # a name the item lacks, or no plain name
            return ""
        return _value(_apply_spec(_full_value(value), format_spec), self.inert)


# The precision in a format spec ("{x:.20}", "{x:>30.5}"): how much of a
# string value it keeps.
_PRECISION_RE = re.compile(r"\.(\d+)")


def _spec_precision(spec: str) -> tuple[int | None, str]:
    """(the precision `spec` names or None, `spec` without it)."""
    m = _PRECISION_RE.search(spec or "")
    if not m:
        return None, spec or ""
    return int(m.group(1)), spec[:m.start()] + spec[m.end():]


def _apply_spec(s: str, spec: str) -> str:
    """`s` formatted by a template's spec. A precision that would shorten
    `s` keeps that much and adds the visible cut marker instead of
    dropping the rest unseen. A spec that does not apply to a string
    raises ValueError, and fill() falls back to the template."""
    if not spec:
        return s
    keep, rest = _spec_precision(spec)
    if keep is not None and len(s) > keep:
        s = s[:keep].rstrip() + " " + _ct.CUT_MARK.format(n=len(s) - keep)
    return format(s, rest)


def _spec_cuts(s: str, spec: str) -> bool:
    """True when the spec's precision shows less than all of `s`."""
    keep, _rest = _spec_precision(spec)
    return keep is not None and len(s) > keep


def _full_value(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if v is None:
        return ""
    return v if isinstance(v, str) else json.dumps(v, default=str)


def _value(v, inert: bool = False) -> str:
    """A field's value for a template, cut at VALUE_MAX characters with a
    visible marker. `inert` makes it a card_text.inert span: one line, and
    shown as written by every frontend (a sentinel in the value shows as a
    mark). Plain text drops the sentinels, so it cannot open a span."""
    s = _full_value(v)
    if inert:
        return _ct.inert(s, VALUE_MAX)
    s = _ct.strip_inert(s)
    if len(s) > VALUE_MAX:
        keep = VALUE_MAX - len(_ct.CUT_MARK) - 6
        return s[:keep] + " " + _ct.CUT_MARK.format(n=len(s) - keep)
    return s


def _template_specs(template) -> list[tuple[str, str]]:
    """(field name, format spec) for each field a template reads."""
    try:
        return [(name, spec or "") for _lit, name, spec, _conv
                in string.Formatter().parse(str(template or "")) if name]
    except ValueError:
        return []


def _template_fields(template) -> set:
    """The field names a template reads."""
    return {name for name, _spec in _template_specs(template)}


def cut_fields(templates, item: dict) -> list[str]:
    """The fields the templates read whose value a card shows cut, at
    VALUE_MAX or by a template's precision: the owner would approve a
    value they did not see in full."""
    item = clean_item(item)
    out = set()
    for t in templates or []:
        for name, spec in _template_specs(t):
            if name not in item:
                continue
            full = _full_value(item[name])
            if _ct.was_cut(_ct.one_line(full, VALUE_MAX)) or _spec_cuts(full, spec):
                out.add(name)
    return sorted(out)


def fill(template: str, item: dict, inert: bool = False) -> str:
    """The template with the item's fields; `inert` for card text, where
    each value renders as written (card_text.inert)."""
    fields = _Fields({k: v for k, v in clean_item(item).items() if isinstance(k, str)})
    try:
        return _SafeFormatter(inert).vformat(str(template or ""), (), fields)
    except (ValueError, IndexError, KeyError, TypeError):
        return str(template or "")


def _matches(when: dict, item: dict) -> bool:
    return all(item.get(k) == v for k, v in when.items())


def select(spec: dict, item: dict) -> dict:
    """The card spec with the first matching variant's keys laid over it."""
    out = {k: v for k, v in spec.items() if k != "variants"}
    for v in spec.get("variants") or []:
        if _matches(v.get("when") or {}, item):
            out.update({k: val for k, val in v.items() if k != "when"})
            break
    return out


def render(plugin: str, spec: dict, item: dict) -> dict:
    """{"ok": True, title, lines, approve_label, approve_word, cancel_label,
    cancel_word, button_noun, busy_status, fallback, key, cut} for a card
    the item can show, or {"ok": False, "text": ...} when a required field
    is missing (the bot posts the text in the thread, and no card).

    The card text (title, lines, labels, busy status, fallback) carries
    each item value as an inert span, so a value renders as written and
    cannot forge card text. `cut` names the fields the card shows cut at
    VALUE_MAX characters; the bot offers no approve action for such a
    card, since the executor gets the whole value."""
    s = select(spec, item)
    missing = [f for f in s.get("required") or [] if not item.get(f)]
    if missing:
        text = s.get("missing_text") or (
            f":warning: The {plugin} card was not posted: it needs "
            + ", ".join(missing) + ".")
        return {"ok": False, "text": fill(text, item, inert=True), "missing": missing}
    title = fill(s.get("title", ""), item, inert=True)
    lines = [fill(line, item, inert=True) for line in s.get("lines") or []]
    shown = [s.get("title", ""), *(s.get("lines") or []), s.get("approve_label", ""),
             s.get("cancel_label", "")]
    key_fields = s.get("key_fields") or []
    key_src = {k: item.get(k) for k in key_fields} if key_fields else clean_item(item)
    key = hashlib.sha256(json.dumps(key_src, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return {"ok": True, "title": title, "lines": lines,
            "approve_label": fill(s.get("approve_label", ""), item, inert=True),
            "approve_word": str(s.get("approve_word") or "approve"),
            "cancel_label": fill(s.get("cancel_label", ""), item, inert=True),
            "cancel_word": str(s.get("cancel_word") or "cancel"),
            "button_noun": fill(s.get("button_noun") or
                                f"The {s.get('approve_label', 'Approve')} button", item,
                                inert=True),
            "busy_status": fill(s.get("busy_status") or ":hourglass_flowing_sand: Working...",
                                item, inert=True),
            "fallback": fill(s.get("fallback") or s.get("title", ""), item, inert=True),
            "key": key, "cut": cut_fields(shown, item)}


def items_of(data) -> list[dict]:
    """The items in one staged payload: a JSON object or an array of them."""
    items = data if isinstance(data, list) else [data]
    return [i for i in items if isinstance(i, dict) and i]


# ── tokens ────────────────────────────────────────────────────────────

def _is_resident() -> bool:
    try:
        return os.geteuid() == pwd.getpwnam(RESIDENT_USER).pw_uid
    except (KeyError, OSError):
        return False


def mediated_install() -> bool:
    """True when the resident is mediated on this host (mail_mediation
    decides from owner-controlled facts), so the owner holds the tokens
    and the mediator answers a resident executor's check."""
    try:
        try:
            import mail_mediation as _mm
        except ModuleNotFoundError:
            from preprocessor import mail_mediation as _mm
        return bool(_mm.mediation_active_for_resident())
    except Exception:
        return False


def token_path(ea_dir=None) -> Path:
    """The token file: owner-private in an owner process on a mediated
    install (the resident cannot write there, so no token can be forged),
    else the resident's state directory."""
    if ea_dir is None and _owner_trust.is_owner_process() and mediated_install():
        return _owner_trust.private_dir() / TOKEN_FILE
    base = Path(ea_dir) if ea_dir else Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
    return base / "state" / TOKEN_FILE


def _private(path: Path) -> bool:
    try:
        return path.parent == _owner_trust.private_dir()
    except Exception:
        return False


def _update_private(path: Path, fn) -> bool:
    """Read-modify-write of the owner-private token file under a lock file
    beside it (the bot mints and the mediator consumes, two processes),
    written through owner_trust at 0600."""
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            text = _owner_trust.read_owner_file(path)
        except FileNotFoundError:
            text = ""
        _owner_trust.write_owner_file(path, fn(text), mode=0o600)
        return True
    finally:
        os.close(fd)


def _update(path: Path, fn) -> bool:
    if _private(path):
        try:
            return _update_private(path, fn)
        except (OSError, _owner_trust.OwnerTrustError):
            return False
    return bool(_rio.make_dir(path.parent) and _rio.update_text(path, fn, 0o660))


def _load(text) -> list:
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def mint_token(plugin: str, card_id: str, item: dict, *, path=None, now=None) -> str:
    """A one-time token for this card and item, valid TOKEN_TTL_S. The bot
    calls it after a click. Raises OSError when it cannot be written."""
    path = Path(path) if path else token_path()
    now = time.time() if now is None else now
    while True:
        raw = secrets.token_urlsafe(24)
        if not raw.startswith("-"):  # never read as a flag on a command line
            break
    entry = {"sha256": hashlib.sha256(raw.encode()).hexdigest(),
             "action": action_of(plugin, card_id, item), "expires": now + TOKEN_TTL_S}

    def add(text: str) -> str:
        toks = [t for t in _load(text) if isinstance(t, dict) and t.get("expires", 0) > now]
        return json.dumps(toks + [entry], indent=1)

    if not _update(path, add):
        raise OSError(f"could not write the card token to {path}")
    return raw


def _consume_action(raw: str, action: str, path: Path, now=None) -> bool:
    now = time.time() if now is None else now
    want_hash = hashlib.sha256(raw.encode()).hexdigest()
    found = []

    def take(text: str):
        keep = []
        for t in _load(text):
            if not isinstance(t, dict):
                continue
            if (not found and t.get("sha256") == want_hash and t.get("action") == action
                    and t.get("expires", 0) > now):
                found.append(True)
                continue
            if t.get("expires", 0) > now:
                keep.append(t)
        return json.dumps(keep, indent=1)

    if not os.path.exists(path):
        return False
    return _update(path, take) and bool(found)


def _consume_via_mediator(raw: str, action: str) -> bool:
    """A resident executor's check on a mediated install: this module's
    CLI forwards the verb to the mediator, which takes the token from the
    owner-private file as the owner."""
    try:
        p = subprocess.run([sys.executable, str(Path(__file__).resolve()), CONSUME_VERB,
                            "--action", action, "--token", raw],
                           capture_output=True, text=True, timeout=90)
    except Exception:
        return False
    res = parse_result(p.stdout)
    return p.returncode == 0 and bool(res and res.get("ok") is True)


def consume_token(raw: str, plugin: str, card_id: str, item: dict, *, path=None,
                  now=None) -> bool:
    """True once for a token minted for this card and item and not yet used
    or expired; the token is gone afterwards. A plugin's executor calls it
    before the gated step. In the resident on a mediated install the check
    goes through the mediator; elsewhere it reads the file."""
    if not raw or not ACTION_RE.match(action_of(plugin, card_id, item)):
        return False
    action = action_of(plugin, card_id, item)
    if path is None and _is_resident() and mediated_install():
        return _consume_via_mediator(raw, action)
    return _consume_action(raw, action, Path(path) if path else token_path(), now)


# ── running an executor ───────────────────────────────────────────────

def resident_python(home: Path, tree: Path) -> str:
    """A python3 the resident can run: its own venv, the deployed tree's
    venv, else the system one (as run-plugin.sh picks)."""
    for cand in (home / ".local" / "venv" / "bin" / "python3", tree / ".venv" / "bin" / "python3"):
        if os.access(cand, os.X_OK):
            return str(cand)
    return "/usr/bin/python3"


def build_argv(command: list, plugin_dir, python: str, token: str = "") -> list[str]:
    fields = {"plugin_dir": str(plugin_dir), "python": python, "token": token}
    out = []
    for part in command:
        for k, v in fields.items():
            part = part.replace("{" + k + "}", v)
        out.append(part)
    if out and out[0] == "python3":
        out[0] = python
    return out


def parse_result(stdout: str) -> dict | None:
    """The last JSON object line with an "ok" key, or None."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "ok" in obj:
            return obj
    return None


def _default_runner(argv, *, stdin, env, cwd, timeout):
    p = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env,
                       cwd=cwd, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def execute(lp, spec: dict, item: dict, which: str = "approve", *, token: str = "",
            runner=None, ea_dir=None, resident_home=None, resident_tree=None) -> dict:
    """Run the card's approve or cancel executor for one item.

    `lp` is the plugin_registry.LockedPlugin. A resident executor runs
    through `sudo -n -u ea-bot env ...`; an owner executor runs here, after
    the owner and integrity checks. Returns {"ok", "status", "message",
    "rc", "tail"}; never raises for a failed run."""
    ex = spec.get(which)
    item = clean_item(item)
    label = f"{lp.name} {spec.get('id')}"
    if not ex:
        return {"ok": which == "cancel", "status": "", "message": "", "rc": 0, "tail": ""}
    ea_dir = str(ea_dir or os.environ.get("EA_DIR", str(Path.home() / ".ea")))
    item_json = json.dumps(item, default=str)
    env_vars = {"EA_DIR": ea_dir, "EA_PLUGIN_NAME": lp.name, "EA_PLUGIN_DIR": str(lp.dir),
                "EA_CARD_ID": str(spec.get("id")), "EA_CARD_ITEM": item_json,
                "EA_CARD_ACTION": action_of(lp.name, spec.get("id"), item)}
    if which == "approve" and token:
        env_vars["EA_CARD_TOKEN"] = token
    run = runner or _default_runner
    try:
        if ex.get("user") == "owner":
            try:
                from plugin_registry import _require_owner_code
            except ModuleNotFoundError:
                from preprocessor.plugin_registry import _require_owner_code
            _require_owner_code(lp)
            argv = build_argv(ex["command"], lp.dir, sys.executable, token)
            env = {**os.environ, **env_vars}
            rc, out, err = run(argv, stdin=item_json, env=env, cwd=str(lp.dir),
                               timeout=ex.get("timeout_seconds", 300))
        else:
            if resident_home is None:
                resident_home = Path(pwd.getpwnam(RESIDENT_USER).pw_dir)
            home = Path(resident_home)
            tree = Path(resident_tree) if resident_tree else home / "ea-system"
            python = resident_python(home, tree)
            argv = build_argv(ex["command"], lp.dir, python, token)
            env_args = [f"HOME={home}", f"PYTHONPATH={tree}/preprocessor:{tree}",
                        "PATH=/usr/local/bin:/usr/bin:/bin"]
            env_args += [f"{k}={v}" for k, v in env_vars.items()]
            full = ["sudo", "-n", "-u", RESIDENT_USER, "env"] + env_args + argv
            rc, out, err = run(full, stdin=item_json, env=None, cwd="/",
                               timeout=ex.get("timeout_seconds", 300))
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": f":x: {label}: timed out", "rc": -1,
                "message": f":warning: The {label} step timed out. It may have run; "
                           f"check before you try again.", "tail": ""}
    except Exception as e:
        return {"ok": False, "status": f":x: {label}: {str(e)[:160]}", "rc": -1,
                "message": "", "tail": str(e)[-300:]}
    tail = ((err or "") + (out or ""))[-300:]
    res = parse_result(out)
    if res is None:
        ok = rc == 0
        return {"ok": ok, "rc": rc, "tail": tail,
                "status": (f":white_check_mark: {label}: done" if ok
                           else f":x: {label}: failed (exit {rc})"),
                "message": "" if ok else f":warning: The {label} step failed. "
                                         f"Tail: ```{tail}```"}
    return {"ok": bool(res.get("ok")), "rc": rc, "tail": tail,
            "status": str(res.get("status") or ""), "message": str(res.get("message") or "")}


# ── CLI: the mediated token check ─────────────────────────────────────
# python3 plugin_cards.py consume-token --action <card:...> --token <raw>
# As the resident on a mediated install the request goes to the mail
# mediator (mail_mediation.SAFE lists the verb); as the owner it takes the
# token from the owner-private file. Prints {"ok": true|false}; exit 0 only
# when the token was taken.

def build_parser():
    """The CLI's argument parser, also what the mediator's argv tripwire
    parses with (tests/test_mediator_argv_tripwire.py)."""
    import argparse
    ap = argparse.ArgumentParser(description="Plugin card tokens", allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser(CONSUME_VERB, help="take a one-time card token", allow_abbrev=False)
    p.add_argument("--action", required=True)
    p.add_argument("--token", required=True)
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        import mail_mediation as _mm
    except ModuleNotFoundError:
        from preprocessor import mail_mediation as _mm
    _mm.forward_or_none(MEDIATED_TOOL, argv)
    ap = build_parser()
    a = ap.parse_args(argv)
    _mm.check_child_parse(ap, a)  # the verb the mediator checked
    if not ACTION_RE.match(a.action) or not a.token or len(a.token) > 128:
        print(json.dumps({"ok": False, "status": "malformed action or token"}))
        return 2
    ok = _consume_action(a.token, a.action, token_path())
    print(json.dumps({"ok": bool(ok)}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
