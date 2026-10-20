#!/usr/bin/env python3
"""The example-notes collector: prints the JSON the skill reads."""
import json
import sys

if "--sweep" in sys.argv:
    print(json.dumps({"post": ":memo: Sweep done: no stale notes."}))
else:
    print(json.dumps({"notes": [{"id": 1, "title": "A made-up note"}]}))
