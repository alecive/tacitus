"""The owner-side record of the model's own web requests.

The model's WebFetch and WebSearch calls run inside the Claude CLI, not
through fetch_url.py, so fetch_url's log never sees them. Claude Code's
WebFetch fetches the page from this host, as ea-bot, with none of
fetch_url's address policy; WebSearch's query travels in the model request
and the search runs on the API side. The owner
processes that read a lane's stream-json (the chat bot for the chat and
room lanes, log_claude_cost.py for the cron pipes) append one JSONL record
per call here, in the owner-private log directory, which ea-bot cannot
reach. The session transcripts under ea-bot's home hold the same calls, but
ea-bot can rewrite them; this log is the copy outbound_review.py trusts.

Record: {"ts", "event": "webfetch"|"websearch", "lane", "run_id", "url",
"host", "query"}; the URL and query are capped at URL_MAX characters.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from datetime import datetime, timezone

try:
    from log_paths import owner_log
except ModuleNotFoundError:  # imported as preprocessor.web_tool_log
    from preprocessor.log_paths import owner_log

LOG_NAME = "web-tools.jsonl"
WEB_TOOLS = ("WebFetch", "WebSearch")
URL_MAX = 4000


def log_path():
    return owner_log(LOG_NAME)


def record_for(tool: str, tool_input, *, lane: str = "", run_id: str = "") -> dict | None:
    """The log record for one tool call, or None when it is not a web tool."""
    if tool not in WEB_TOOLS:
        return None
    inp = tool_input if isinstance(tool_input, dict) else {}
    rec = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "event": tool.lower(), "lane": str(lane)[:64], "run_id": str(run_id)[:128]}
    if tool == "WebFetch":
        url = str(inp.get("url") or "")[:URL_MAX]
        rec["url"] = url
        try:
            rec["host"] = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            rec["host"] = ""
    else:
        rec["query"] = str(inp.get("query") or "")[:URL_MAX]
        for key in ("allowed_domains", "blocked_domains"):
            if isinstance(inp.get(key), list):
                rec[key] = [str(d)[:200] for d in inp[key][:20]]
    return rec


def note(tool: str, tool_input, *, lane: str = "", run_id: str = "", path=None) -> None:
    """Append the record for one web tool call. Never raises: the record
    must not break a run."""
    try:
        rec = record_for(tool, tool_input, lane=lane, run_id=run_id)
        if rec is None:
            return
        target = path or log_path()
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
