#!/usr/bin/env python3
"""The example-notes card executor: the shape every card executor takes.

The item arrives on stdin. On approve, the one-time token must check out
before the gated step runs. The last stdout line is the result."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.environ.get("EA_SYSTEM_DIR", ""), "preprocessor"))


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    item = json.load(sys.stdin)
    title = item.get("title", "")
    if which == "approve":
        import plugin_cards
        token = os.environ.get("EA_CARD_TOKEN", "")
        if not plugin_cards.consume_token(token, os.environ["EA_PLUGIN_NAME"],
                                          os.environ["EA_CARD_ID"], item):
            print(json.dumps({"ok": False, "status": f":x: Not published: {title}",
                              "message": "The token did not check out; nothing ran."}))
            return 0
        print(json.dumps({"ok": True, "status": f":white_check_mark: Published: {title}",
                          "message": f":memo: Published *{title}*."}))
        return 0
    print(json.dumps({"ok": True, "status": f":no_entry_sign: Skipped: {title}", "message": ""}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
