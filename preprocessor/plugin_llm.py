#!/usr/bin/env python3
"""The post of a plugin schedule that Claude does not write (docs/plugins.md).

run-plugin.sh calls this for a v2 schedule whose `llm` is not `claude`:

- `none`: the plugin's collect.py wrote the post itself, as the string
  field `post` in its JSON. It is posted as is; no model runs.
- `remote` and `remote-first`: the remote model writes the post from the
  skill and the collected JSON, with no tools. `remote-first` falls back
  to Claude (exit 3) when the remote model is off, fails or writes a post
  that does not pass the checks; `remote` posts a warning instead.

The checks are run-plugin.sh's: no meta-commentary, and the first line
opens with one of the manifest's format anchors.

    plugin_llm.py run --plugin NAME --mode MODE --channel-key KEY
                      --collected-file PATH [--skill-file PATH] [--anchors JSON]

The files are the owner's private copies run-plugin.sh makes of the skill
and of collect.py's JSON.

Exit 0: posted (or nothing to post). Exit 3: Claude should write it. Exit 4:
the run failed and a warning was posted.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

META_PATTERNS = ("state updated", "here is a summary", "here is what was",
                 "update complete", "plugin run complete", "posted to slack")
STRIP_PREAMBLE_MAX_LINES = 12
REMOTE_MAX_TOKENS = 2500

PROMPT = """You are writing one chat post for the EA plugin '{name}'.

Follow the skill instructions below. The data they refer to is the JSON
after them; use only that data. You have no tools: do not say you ran
anything. Write the post itself and nothing else.

The first line must start with one of these emoji shortcodes: {anchors}
Use emoji shortcodes (:sunny:), not unicode emoji. Use *single asterisks*
for bold. If there is nothing to report and the skill allows a quiet run,
write nothing.

Skill instructions:

{skill}
"""


def strip_preamble(text: str) -> str:
    lines = text.splitlines()
    anchor = re.compile(r"^\s*:[a-z0-9_]+:")
    for i, ln in enumerate(lines[:STRIP_PREAMBLE_MAX_LINES]):
        if anchor.match(ln):
            return "\n".join(lines[i:])
    return text


def check(text: str, anchors: list) -> str:
    """'ok' or why the post fails the checks."""
    lower = text[:200].lower()
    for pat in META_PATTERNS:
        if pat in lower:
            return f"meta-commentary: {pat}"
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if anchors and not re.match(r"\s*(" + "|".join(re.escape(a) for a in anchors) + ")", first):
        return f"missing-format-anchor: first line did not start with any of {anchors}"
    return "ok"


def _post(key: str, text: str) -> bool:
    import slack_notifier as slack
    return bool(slack.post_to_slack(key, text))


def write_remote(name: str, skill: str, collected: str, anchors: list, chat=None) -> str:
    """The remote model's post. Raises RuntimeError when the remote model
    is off or the call fails."""
    import remote_llm
    cfg = remote_llm.config()
    if not cfg.get("endpoint") or not cfg.get("model"):
        raise RuntimeError("the remote model is not configured")
    call = chat or remote_llm.chat
    prompt = PROMPT.format(name=name, anchors=", ".join(anchors) or ":sparkles:", skill=skill)
    try:
        res = call([{"role": "system", "content": prompt},
                    {"role": "user", "content": collected}],
                   purpose=f"plugin:{name}", max_tokens=REMOTE_MAX_TOKENS)
    except Exception as e:
        raise RuntimeError(f"remote call failed: {e}") from None
    return strip_preamble(str(res.get("content") or "").strip())


def run(name: str, mode: str, key: str, anchors: list, payload: dict,
        post=None, chat=None) -> int:
    post = post or _post
    collected = str(payload.get("collected") or "")
    if mode == "none":
        try:
            text = str((json.loads(collected or "{}") or {}).get("post") or "").strip()
        except ValueError:
            text = ""
        if not text:
            print("no post in the collected JSON; nothing to post", file=sys.stderr)
            return 0
        return 0 if post(key, text) else 4
    try:
        text = write_remote(name, str(payload.get("skill") or ""), collected, anchors, chat=chat)
    except RuntimeError as e:
        print(f"plugin_llm: {e}", file=sys.stderr)
        if mode == "remote-first":
            return 3
        post(key, f":warning: *plugin:{name}*: the remote model could not write the post ({e}).")
        return 4
    if not text.strip():
        print("empty post: a quiet run", file=sys.stderr)
        return 0
    verdict = check(text, anchors)
    if verdict != "ok":
        print(f"plugin_llm: the remote post failed the checks: {verdict}", file=sys.stderr)
        if mode == "remote-first":
            return 3
        post(key, f":warning: *plugin:{name}*: the remote model's post failed validation "
                  f"(`{verdict}`); nothing posted.")
        return 4
    return 0 if post(key, text) else 4


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--plugin", required=True)
    r.add_argument("--mode", required=True, choices=("none", "remote", "remote-first"))
    r.add_argument("--channel-key", required=True)
    r.add_argument("--collected-file", required=True)
    r.add_argument("--skill-file", default="")
    r.add_argument("--anchors", default="[]")
    args = ap.parse_args(argv)

    def text_of(path: str) -> str:
        try:
            return Path(path).read_text() if path else ""
        except OSError:
            return ""
    payload = {"skill": text_of(args.skill_file), "collected": text_of(args.collected_file)}
    try:
        anchors = [a for a in json.loads(args.anchors or "[]") if isinstance(a, str)]
    except ValueError:
        anchors = []
    return run(args.plugin, args.mode, args.channel_key, anchors, payload)


if __name__ == "__main__":
    sys.exit(main())
