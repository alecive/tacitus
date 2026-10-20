#!/usr/bin/env python3
"""ea-plugin: install and manage EA plugins (docs/plugins.md).

Run as the owner. Every command reads and writes the owner's lock file
(plugins.lock.json in the owner-private directory). Nothing here pushes.

    ea-plugin.py install git:<url>[@<ref>] [--accept-capabilities] [--disabled]
    ea-plugin.py adopt <name> [--zone agent|owner] [--accept-capabilities]
    ea-plugin.py verify [<name> ...]
    ea-plugin.py update <name> [--ref <ref>] [--accept-capabilities]
    ea-plugin.py enable <name> [--accept-capabilities]
    ea-plugin.py disable <name>
    ea-plugin.py uninstall <name> --yes
    ea-plugin.py list [--json]
    ea-plugin.py show <name>
    ea-plugin.py cron-lines
    ea-plugin.py run-owner <name> <label>
    ea-plugin.py test [<name> ...]

A source on `trusted_sources` (the owner-private plugins-policy.json, else
`plugins` in ea-config.json) installs without --accept-capabilities unless
the plugin runs code as the owner; a source on `blocked_sources` never
installs. Exit status: 0 done, 1 problems found, 2 usage or a bad
manifest, 3 consent needed, 4 blocked by the policy.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "preprocessor"))

import plugin_install as I  # noqa: E402
import plugin_manifest as PM  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ea-plugin.py", description="Install and manage EA plugins.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="install a plugin from git")
    p.add_argument("source", help="git:<url>[@<ref>] or git:<url>#<ref>")
    p.add_argument("--accept-capabilities", action="store_true")
    p.add_argument("--disabled", action="store_true", help="install it disabled")
    p = sub.add_parser("adopt", help="record a plugin already in place")
    p.add_argument("name")
    p.add_argument("--zone", choices=PM.ZONES, default="agent")
    p.add_argument("--accept-capabilities", action="store_true")
    p = sub.add_parser("verify", help="check installed files against the lock")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("update", help="fetch a newer release")
    p.add_argument("name")
    p.add_argument("--ref", default=None)
    p.add_argument("--accept-capabilities", action="store_true")
    p = sub.add_parser("enable")
    p.add_argument("name")
    p.add_argument("--accept-capabilities", action="store_true")
    p = sub.add_parser("disable")
    p.add_argument("name")
    p = sub.add_parser("uninstall")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("list")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="a plugin's capabilities as the lock holds them")
    p.add_argument("name")
    sub.add_parser("cron-lines", help="crontab lines for the owner plugins' cron jobs")
    p = sub.add_parser("run-owner", help="run an owner plugin's cron job (from cron)")
    p.add_argument("name")
    p.add_argument("label")
    p = sub.add_parser("test", help="run the installed plugins' own test suites")
    p.add_argument("names", nargs="*")
    args = ap.parse_args(argv)

    ctx = I.Context()
    try:
        if args.cmd == "install":
            I.install(ctx, args.source, accept=args.accept_capabilities, enable=not args.disabled)
        elif args.cmd == "adopt":
            I.adopt(ctx, args.name, zone=args.zone, accept=args.accept_capabilities)
        elif args.cmd == "verify":
            bad = 0
            for name, problems in I.verify(ctx, args.names).items():
                print(f"{name}: {'ok' if not problems else 'PROBLEMS'}")
                for line in problems:
                    print(f"  - {line}")
                bad += bool(problems)
            return I.EXIT_PROBLEMS if bad else I.EXIT_OK
        elif args.cmd == "update":
            I.update(ctx, args.name, ref=args.ref, accept=args.accept_capabilities)
        elif args.cmd == "enable":
            I.set_enabled(ctx, args.name, True, accept=args.accept_capabilities)
        elif args.cmd == "disable":
            I.set_enabled(ctx, args.name, False)
        elif args.cmd == "uninstall":
            I.uninstall(ctx, args.name, yes=args.yes)
        elif args.cmd == "list":
            rows = I.listing(ctx)
            if args.json:
                print(json.dumps(rows, indent=1))
            else:
                for r in rows:
                    print(f"{r['name']:28s} {r['zone']:6s} v{r['manifest'] or '?'} "
                          f"{'enabled ' if r['enabled'] else 'disabled'} "
                          f"{'managed  ' if r['managed'] else 'unmanaged'} {r['commit']} {r['url']}")
        elif args.cmd == "show":
            _lock, entry = I._locked(ctx, args.name)
            for line in PM.describe(PM.capability_summary(entry["manifest"])):
                print(f"- {line}")
        elif args.cmd == "cron-lines":
            log_dir = ctx.private / "logs"
            for line in I.cron_lines(ctx, HERE, log_dir):
                print(line)
        elif args.cmd == "run-owner":
            return I.run_owner(ctx, args.name, args.label)
        elif args.cmd == "test":
            bad = 0
            for name, (rc, tail) in I.run_tests(ctx, HERE.parent.parent, args.names).items():
                print(f"{name}: {'ok' if rc == 0 else 'no tests' if rc is None else 'FAILED'}")
                if rc:
                    print(tail)
                bad += bool(rc)
            return I.EXIT_PROBLEMS if bad else I.EXIT_OK
    except I.PluginError as e:
        print(f"ea-plugin: {e}", file=sys.stderr)
        return e.code
    return I.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
