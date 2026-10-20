"""Plugin-scoped wrapper around preprocessor/ledger.py.

Plugins use this module instead of calling ledger.py directly so that:

- Every write is auto-tagged with `source = 'plugin:<name>'` (the env var
  EA_PLUGIN_NAME is read at module load time and cannot be overridden
  from inside the plugin's collect.py or skill run).
- Every read is scoped to the plugin's own source by default; main-EA
  rows are invisible. Cross-cutting reads require an explicit
  `include_main_ea=True` (Python API) or `--include-main-ea` (CLI).
- IDs are namespaced: every `add()` whose `id` doesn't start with the
  plugin's name prefix is rejected. Prevents accidental collisions with
  main-EA IDs or other plugins.

Two interfaces, same underlying CLI:

Python API (for collect.py scripts):
    from ea_plugins import ledger
    ledger.add(id="my-plugin-item-1234", type="my-plugin-alert",
               description="...", deadline="2026-08-15")
    items = ledger.list_()

CLI (for plugin skills via Claude's Bash tool):
    python3 -m ea_plugins.ledger add --id my-plugin-item-1234 \\
        --type my-plugin-alert --description '...'
    python3 -m ea_plugins.ledger list

The CLI mirrors preprocessor/ledger.py's subcommand surface (add, close,
get, list, search, stats, update) but with the source-tagging and
prefix-validation enforced.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# Resolve the underlying ledger.py once at import time.
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_LEDGER_CLI = _SCRIPT_DIR / "preprocessor" / "ledger.py"


def _plugin_name() -> str:
    """Read the plugin name from env. Raises if unset.

    The run-plugin.sh runner exports EA_PLUGIN_NAME before invoking
    collect.py or Claude. Setting it after import has no effect.
    """
    name = os.environ.get("EA_PLUGIN_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "EA_PLUGIN_NAME is not set; ea_plugins.ledger can only be used "
            "from inside a plugin run (via run-plugin.sh)."
        )
    return name


def _source_tag() -> str:
    return f"plugin:{_plugin_name()}"


def _id_prefix() -> str:
    return f"{_plugin_name()}-"


def _validate_id(entry_id: str) -> None:
    prefix = _id_prefix()
    if not entry_id.startswith(prefix):
        raise ValueError(
            f"Ledger ID {entry_id!r} must start with {prefix!r} "
            f"(plugin-scoped IDs are namespaced to prevent collisions)."
        )


def _run_ledger(args: list[str]) -> dict:
    """Shell out to preprocessor/ledger.py and parse the JSON response.

    Raises CalledProcessError on non-zero exit. Returns the parsed JSON
    object on success.
    """
    cmd = [sys.executable, str(_LEDGER_CLI), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr
        )
    if not proc.stdout.strip():
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"ledger.py returned non-JSON output: {proc.stdout[:200]!r}"
        ) from e


# ── Python API ────────────────────────────────────────────────────────────

def add(*, id: str, type: str, description: str = "",
        deadline: str | None = None, follow_up_after: str | None = None,
        notes: str = "", priority: str | None = None,
        source_message_id: str = "", conversation_id: str = "",
        **extra: str) -> dict:
    """Add a new ledger entry tagged with this plugin's source.

    `id` MUST start with the plugin's name prefix (e.g. 'my-plugin-').
    Extra kwargs go to the ledger's `--set KEY=VALUE` mechanism.
    """
    _validate_id(id)
    cmd = ["add",
           "--id", id,
           "--type", type,
           "--description", description]
    # All `--set k=v` pairs must go in ONE trailing invocation; see the
    # docstring on `update()` for why (CLI nargs='*' quirk).
    set_pairs: list[str] = [f"source={_source_tag()}"]
    if deadline:
        cmd += ["--deadline", deadline]
    if follow_up_after:
        cmd += ["--follow-up-after", follow_up_after]
    if notes:
        cmd += ["--notes", notes]
    if priority:
        cmd += ["--priority", priority]
    if source_message_id:
        cmd += ["--source-message-id", source_message_id]
    if conversation_id:
        cmd += ["--conversation-id", conversation_id]
    for k, v in extra.items():
        set_pairs.append(f"{k}={v}")
    cmd += ["--set", *set_pairs]
    return _run_ledger(cmd)


def update(id: str, **fields: Any) -> dict:
    """Update an existing entry. Only this plugin's own rows are visible
    for read; the underlying CLI will refuse updates to rows from other
    sources because get() / list_() filters here are source-scoped, but
    update operates by id so we trust the caller is updating their own.

    Internals: the CLI's `--set` argument uses argparse `nargs='*'`,
    which means a SECOND `--set` flag overwrites the first. So we must
    pack all metadata key=value pairs into ONE trailing `--set k=v k=v`
    invocation. (This is a CLI quirk, not a wrapper bug — keep it here
    so callers can pass any number of `**fields` naturally.)
    """
    _validate_id(id)
    cmd = ["update", "--id", id]
    direct = {
        "status", "description", "last_action", "notes", "deadline",
        "follow_up_after", "snooze_until", "priority", "conversation_id",
    }
    set_pairs: list[str] = []
    for k, v in fields.items():
        if v is None:
            continue
        if k in direct:
            cmd += [f"--{k.replace('_', '-')}", str(v)]
        elif k == "email_archived" and v:
            cmd += ["--email-archived"]
        else:
            set_pairs.append(f"{k}={v}")
    if set_pairs:
        cmd += ["--set", *set_pairs]
    return _run_ledger(cmd)


def close(id: str, note: str = "", skip_archive: bool = True) -> dict:
    """Close an entry. skip_archive defaults to True for plugins because
    plugin source_message_ids may not be Outlook IDs (the EA-core archive
    step is Outlook-specific)."""
    _validate_id(id)
    cmd = ["close", "--id", id]
    if note:
        cmd += ["--note", note]
    if skip_archive:
        cmd += ["--skip-archive"]
    return _run_ledger(cmd)


def get(id: str) -> dict | None:
    """Fetch one entry by id. Returns None if not found or not scoped
    to this plugin's source.

    Note: the underlying CLI returns the entry directly at the top
    level (not wrapped in {"entry": ...}). It also exits non-zero
    with {"error": "Entry not found: ..."} when the row doesn't exist;
    we treat that case as "return None" rather than propagating the
    error, since `get` semantically means "exists?-or-None".
    """
    _validate_id(id)
    import subprocess as _sp
    try:
        result = _run_ledger(["get", "--id", id])
    except _sp.CalledProcessError as e:
        # ledger.py exits 1 with {"error": "Entry not found: ..."} on miss.
        out = (e.output or "").strip()
        if out and '"Entry not found' in out:
            return None
        raise
    # On hit, the CLI prints the row dict at the top level (not wrapped).
    # On older callers using {"entry": ...}, handle both shapes.
    entry = result.get("entry") if isinstance(result, dict) and "entry" in result else result
    if not entry or not isinstance(entry, dict) or "id" not in entry:
        return None
    if entry.get("source") != _source_tag():
        # Not ours — don't expose
        return None
    return entry


def list_(*, status: str | None = None, type: str | None = None,
          stale: int | None = None, has_deadline: bool = False,
          limit: int = 50, include_main_ea: bool = False) -> list[dict]:
    """List this plugin's ledger entries. By default only the plugin's
    own rows are returned; pass include_main_ea=True to also surface
    main-EA rows (rare; usually only useful for debugging)."""
    cmd = ["list", "--limit", str(limit)]
    if status:
        cmd += ["--status", status]
    if type:
        cmd += ["--type", type]
    if stale is not None:
        cmd += ["--stale", str(stale)]
    if has_deadline:
        cmd += ["--has-deadline"]
    if include_main_ea:
        cmd += ["--include-plugins"]
    else:
        cmd += ["--source", _source_tag()]
    result = _run_ledger(cmd)
    return result.get("entries", [])


def search(query: str, *, status: str | None = None,
           limit: int = 20, include_main_ea: bool = False) -> list[dict]:
    """Search this plugin's ledger entries by text."""
    cmd = ["search", "--query", query, "--limit", str(limit)]
    if status:
        cmd += ["--status", status]
    if include_main_ea:
        cmd += ["--include-plugins"]
    else:
        cmd += ["--source", _source_tag()]
    result = _run_ledger(cmd)
    return result.get("entries", [])


def stats(*, include_main_ea: bool = False) -> dict:
    """Stats scoped to this plugin's rows by default."""
    cmd = ["stats"]
    if include_main_ea:
        cmd += ["--include-plugins"]
    else:
        cmd += ["--source", _source_tag()]
    return _run_ledger(cmd)


# ── CLI shim ──────────────────────────────────────────────────────────────

def _cli() -> int:
    """Plugin-facing CLI: `python3 -m ea_plugins.ledger <cmd> ...`.

    Mirrors preprocessor/ledger.py's subcommand surface but enforces
    source-tagging on writes and source-scoping on reads.
    """
    parser = argparse.ArgumentParser(
        prog="ea_plugins.ledger",
        description="Plugin-scoped ledger access. Auto-tags writes with "
                    "source=plugin:<EA_PLUGIN_NAME>; scopes reads to that "
                    "source by default.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("--id", required=True)
    p_add.add_argument("--type", required=True)
    p_add.add_argument("--description", default="")
    p_add.add_argument("--deadline")
    p_add.add_argument("--follow-up-after")
    p_add.add_argument("--notes", default="")
    p_add.add_argument("--priority")
    p_add.add_argument("--source-message-id", default="")
    p_add.add_argument("--conversation-id", default="")

    p_upd = sub.add_parser("update")
    p_upd.add_argument("--id", required=True)
    p_upd.add_argument("--status")
    p_upd.add_argument("--description")
    p_upd.add_argument("--last-action")
    p_upd.add_argument("--notes")
    p_upd.add_argument("--deadline")
    p_upd.add_argument("--follow-up-after")
    p_upd.add_argument("--snooze-until")
    p_upd.add_argument("--priority")
    p_upd.add_argument("--conversation-id")
    p_upd.add_argument("--email-archived", action="store_true")
    p_upd.add_argument("--set", nargs="*", metavar="KEY=VALUE",
                       help="Extra fields (core or metadata)")

    p_close = sub.add_parser("close")
    p_close.add_argument("--id", required=True)
    p_close.add_argument("--note", default="")

    p_get = sub.add_parser("get")
    p_get.add_argument("--id", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--status")
    p_list.add_argument("--type")
    p_list.add_argument("--stale", type=int)
    p_list.add_argument("--has-deadline", action="store_true")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--include-main-ea", action="store_true",
                       help="Also include main-EA (non-plugin) rows")

    p_search = sub.add_parser("search")
    p_search.add_argument("--query", "-q", required=True)
    p_search.add_argument("--status")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--include-main-ea", action="store_true")

    p_stats = sub.add_parser("stats")
    p_stats.add_argument("--include-main-ea", action="store_true")

    args = parser.parse_args()

    try:
        if args.cmd == "add":
            result = add(id=args.id, type=args.type,
                         description=args.description,
                         deadline=args.deadline,
                         follow_up_after=args.follow_up_after,
                         notes=args.notes, priority=args.priority,
                         source_message_id=args.source_message_id,
                         conversation_id=args.conversation_id)
        elif args.cmd == "update":
            kwargs = {
                k: getattr(args, k.replace("-", "_"))
                for k in ("status", "description", "last-action", "notes",
                          "deadline", "follow-up-after", "snooze-until",
                          "priority", "conversation-id")
                if getattr(args, k.replace("-", "_"), None) is not None
            }
            if args.email_archived:
                kwargs["email_archived"] = True
            for pair in (args.set or []):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    kwargs[k] = v
            result = update(args.id, **kwargs)
        elif args.cmd == "close":
            result = close(args.id, note=args.note)
        elif args.cmd == "get":
            entry = get(args.id)
            result = {"entry": entry}
        elif args.cmd == "list":
            entries = list_(status=args.status, type=args.type,
                            stale=args.stale, has_deadline=args.has_deadline,
                            limit=args.limit,
                            include_main_ea=args.include_main_ea)
            result = {"count": len(entries), "entries": entries}
        elif args.cmd == "search":
            entries = search(args.query, status=args.status,
                             limit=args.limit,
                             include_main_ea=args.include_main_ea)
            result = {"count": len(entries), "query": args.query,
                      "entries": entries}
        elif args.cmd == "stats":
            result = stats(include_main_ea=args.include_main_ea)
        else:
            print(json.dumps({"error": f"Unknown cmd: {args.cmd}"}))
            return 2
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    except subprocess.CalledProcessError as e:
        print(json.dumps({
            "error": "underlying ledger.py failed",
            "stderr": (e.stderr or "")[:500],
            "stdout": (e.output or "")[:500],
        }))
        return e.returncode

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
