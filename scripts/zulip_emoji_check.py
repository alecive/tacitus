#!/usr/bin/env python3
"""zulip_emoji_check: which of the EA's emoji shortcodes the Zulip server
renders. Reads the shortcodes from the repo (prompts, skills, plugins and
the bot), asks the server's render endpoint about each after the converter's
aliases, and prints the ones that would show as literal text. Runs as the owner.
The server and bot come from the owner's chat settings (ea_config.get_chat:
the owner-private chat.json when it exists) and ZULIP_API_KEY by the
owner_trust rule (the environment, the owner-private secrets.env, then the
legacy ~/.ea/.env.secrets after the ownership checks).

    scripts/zulip_emoji_check.py [--site https://host:8443]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "preprocessor"))
from chat.text import EMOJI_ALIASES  # noqa: E402
import ea_config  # noqa: E402
import owner_trust  # noqa: E402

WORDS = re.compile(r":([a-z][a-z0-9_+-]{1,40}):")


def shortcodes() -> set[str]:
    # Every name costs one render call on the bot account, whose rate limit
    # the live bot shares, so installed packages and the generated table
    # (which only repeats the converter) stay out.
    out = subprocess.run(["grep", "-rhoE", r":[a-z][a-z0-9_+-]{1,40}:", "--include=*.py", "--include=*.md",
                          "--include=*.sh", "--include=*.json", "--exclude-dir=.venv", "--exclude-dir=.git",
                          "--exclude-dir=node_modules", "--exclude=zulip_emoji_names.py",
                          str(ROOT), "/home/ea-bot/.ea/plugins"],
                         capture_output=True, text=True).stdout
    return {m.group(1) for m in WORDS.finditer(out)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site")
    a = ap.parse_args()
    cfg = ea_config.get_chat()["zulip"]
    site = (a.site or cfg["site"]).rstrip("/")
    key = owner_trust.owner_secret("ZULIP_API_KEY")
    if not key:
        print("ZULIP_API_KEY not found (environment, owner secrets.env, or a legacy "
              ".env.secrets that passes the ownership checks)", file=sys.stderr)
        return 1
    auth = (cfg["bot_email"], key)
    bad = []
    for name in sorted(shortcodes()):
        target = EMOJI_ALIASES.get(name, name)
        r = requests.post(f"{site}/api/v1/messages/render", auth=auth, data={"content": f":{target}:"}, timeout=30).json()
        if 'class="emoji' not in r.get("rendered", ""):
            bad.append(f"{name} -> {target}" if target != name else name)
    print(f"{len(bad)} shortcode(s) render as text: {' '.join(bad) or 'none'}")
    print("(names that are not emoji at all, such as version strings the grep catches, are expected here)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
