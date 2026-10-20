#!/usr/bin/env python3
"""The example-chat-reader verbs: print a made-up reply for the verb."""
import json
import sys

verb = sys.argv[1] if len(sys.argv) > 1 else ""
print(json.dumps({"verb": verb, "args": sys.argv[2:], "messages": []}))
