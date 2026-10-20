#!/usr/bin/env python3
"""Merge KEY=VALUE pairs into an env file, as the user who owns that file.

The entrypoint's setup role runs this as ea-bot for ~/.ea/.env (mode
0640: the owner's loader reads the config keys through group ea) and as
the owner for the owner-private secrets.env (mode 0600). The pairs come
on stdin as a JSON object, never on the command line, so a secret is not
in the process list. Keys already in the file keep their place and take
the new value; other lines are kept as they are. The file is written in
place through a descriptor opened without following a symlink, so a
link planted at the path is refused rather than written through.

  python3 env_file.py --path FILE --mode 0640 < pairs.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

KEY = re.compile(r"[A-Z][A-Z0-9_]*")


def merge(text: str, pairs: dict[str, str]) -> str:
    lines = text.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = m.group(1) if m else None
        if key in pairs and key not in seen:
            out.append(f"{key}={pairs[key]}")
            seen.add(key)
        elif key in pairs:
            continue
        else:
            out.append(line)
    for key, value in pairs.items():
        if key not in seen:
            out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def write(path: Path, pairs: dict[str, str], mode: int) -> None:
    for key, value in pairs.items():
        if not KEY.fullmatch(key):
            raise SystemExit(f"env_file: refusing key {key!r}")
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            raise SystemExit(f"env_file: {key}: the value must be one line of text")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "r+", encoding="utf-8") as f:
        text = f.read()
        f.seek(0)
        f.truncate()
        f.write(merge(text, pairs))
    os.chmod(path, mode)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--path", type=Path, required=True)
    ap.add_argument("--mode", default="0600", help="octal, as chmod takes it")
    a = ap.parse_args(argv)
    try:
        pairs = json.load(sys.stdin)
    except ValueError as e:
        raise SystemExit(f"env_file: stdin is not JSON: {e}")
    if not isinstance(pairs, dict):
        raise SystemExit("env_file: stdin must hold a JSON object")
    pairs = {k: ("" if v is None else str(v)) for k, v in pairs.items()}
    write(a.path, pairs, int(a.mode, 8))
    print(f"[env_file] {len(pairs)} key(s) -> {a.path} (mode {a.mode})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
