#!/usr/bin/env python3
"""The conversation index: what every other conversation on this host
is doing right now.

The EA talks in many rooms at once (EA-channel threads, #ea-<project>
rooms, #tmp-<id8> session mirrors, the DM, the ea-bot shells), and each
is a Claude session that knows nothing of the others. This module reads
what they all leave behind and writes one screen:

    CONVERSATION INDEX (other sessions on this host, newest first)
    - #tmp-fable-arctic (session room), claude-fable-5-1 | 12m ago | 231 prompts
        asked: then send me your response above to the slack thread verbatim
        last:  Feasible, with two corrections to the premise ...
    - Slack thread "Draft a reply to Sol..." | 3h ago | 9 prompts
        asked: ...

Sources, all local:
  ~/.claude/history.jsonl            every prompt, with session id and time
  ~/.claude/projects/<dir>/*.jsonl   the transcripts (tail-read for the
                                     last assistant text; never read whole)
  ~/.ea/state/slack-sessions.json    thread <-> session, titles, rooms
  ~/.ea/config/ea-channels.json      channel <-> project / session

It runs as the ea-bot user (the transcripts are its own, mode 600). The
Slack bot, which runs as the owner, calls it through `sudo -n -u ea-bot`
the way it runs claude itself, and injects the text into dispatches from
any room; a shell session runs it directly:

    python3 preprocessor/session_index.py [--exclude <sid>] [--days 7]
                                          [--max-chars 2200] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
CLAUDE_HOME = Path(os.environ.get("EA_CLAUDE_HOME", str(Path.home() / ".claude")))
HISTORY = CLAUDE_HOME / "history.jsonl"
from slack_sessions import LANES, ea_bot_home, lane_dir, project_dir_name  # noqa: E402
PROJECT_DIR_NAME = os.environ.get("EA_CLAUDE_PROJECT_DIR") or project_dir_name(
    os.path.realpath(os.path.join(ea_bot_home(), "ea-system")))
SESSIONS_DIR = CLAUDE_HOME / "projects" / PROJECT_DIR_NAME
# The lanes that run outside the tree (slack_sessions.LANES) keep their
# transcripts in their own working directory's project folder.
LANE_SESSIONS_DIRS = [CLAUDE_HOME / "projects" / project_dir_name(os.path.realpath(str(lane_dir(lane))))
                      for lane in LANES]
TAIL_BYTES = 64 * 1024
_ROUTING_PREFIX_RE = re.compile(r"^\s*\[[^\]]{0,600}\]\s*", re.S)
_PASTE_RE = re.compile(r"^\[Pasted text #\d+ \+\d+ lines\]$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_to_dt(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _clean(text: str, limit: int) -> str:
    text = _ROUTING_PREFIX_RE.sub("", text or "", count=1)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


# ── sources ───────────────────────────────────────────────────────────

def read_history(path: Path = HISTORY, days: int = 7) -> dict:
    """session_id -> {"prompts": n, "last_prompt": str, "last_ts": dt}
    from history.jsonl. Bot dispatches arrive as pasted blocks; their real
    text is in the transcript, so they are counted but not quoted here."""
    out: dict = {}
    cutoff = _now() - timedelta(days=days)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        sid = d.get("sessionId") or ""
        ts = d.get("timestamp") or 0
        if not sid or not ts:
            continue
        dt = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, tz=timezone.utc)
        if dt < cutoff:
            continue
        row = out.setdefault(sid, {"prompts": 0, "last_prompt": "", "last_ts": dt})
        row["prompts"] += 1
        if dt >= row["last_ts"]:
            row["last_ts"] = dt
            disp = (d.get("display") or "").strip()
            if disp and not _PASTE_RE.match(disp):
                row["last_prompt"] = disp
    return out


_DISPATCH_MARKER_RE = re.compile(r"\[slack-dispatch [^\]]*\]\s*(?:(?:The user|[A-Z]\w*) says:\s*)?")


def _dispatch_words(text: str) -> str:
    """A bot dispatch shown as the words asked, without its marker. The
    resident's bridge reads markers in tool output, so the index must not
    print them back into the pane."""
    m = _DISPATCH_MARKER_RE.search(text)
    return text[m.end():].strip() if m else text


def tail_transcript(path: Path, tail_bytes: int = TAIL_BYTES) -> dict:
    """{"last_user": str, "last_assistant": str, "last_ts": dt,
    "model": str} from the end of a transcript."""
    out = {"last_user": "", "last_assistant": "", "last_ts": None, "model": ""}
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return out
    lines = chunk.splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]  # the first line is a torn record
    for ln in lines:
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        t = d.get("type")
        if t not in ("user", "assistant"):
            continue
        msg = d.get("message") or {}
        content = msg.get("content")
        parts: list[str] = []
        tool_result = False
        if isinstance(content, str):
            parts.append(content)
        else:
            for c in content or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text":
                    parts.append(c.get("text") or "")
                elif c.get("type") == "tool_result":
                    tool_result = True
        text = "\n".join(p for p in parts if p).strip()
        ts = _iso_to_dt(d.get("timestamp") or "")
        if ts and (out["last_ts"] is None or ts > out["last_ts"]):
            out["last_ts"] = ts
        if t == "user" and text and not tool_result:
            out["last_user"] = _dispatch_words(text)
        elif t == "assistant":
            if msg.get("model"):
                out["model"] = msg["model"]
            if text:
                out["last_assistant"] = text
    return out


def _registries() -> tuple[dict, dict]:
    """(session_id -> thread row, session_id -> channel row)."""
    threads: dict = {}
    rooms: dict = {}
    try:
        import slack_sessions as ss
        for ts, entry in ss._load().items():
            sid = (entry.get("session_id") or "").lower()
            if sid:
                threads[sid] = {"thread_ts": ts, **(entry.get("meta") or {})}
            old = ((entry.get("meta") or {}).get("expired_session_id") or "").lower()
            if old and old not in threads:
                threads[old] = {"thread_ts": ts, "expired": True,
                                **(entry.get("meta") or {})}
    except Exception:
        pass
    try:
        import ea_channels as ch
        for cid, e in ch.load().items():
            sid = (e.get("session_id") or "").lower()
            if sid:
                rooms[sid] = {"channel": cid, **e}
    except Exception:
        pass
    return threads, rooms


# ── the index ─────────────────────────────────────────────────────────

def scan(days: int = 7, sessions_dir: Path | None = None,
         history_path: Path | None = None, exclude: set[str] | None = None,
         limit: int = 12) -> list[dict]:
    """Every session active in the last `days`, newest first."""
    dirs = [sessions_dir] if sessions_dir else [SESSIONS_DIR, *LANE_SESSIONS_DIRS]
    history = read_history(history_path or HISTORY, days=days)
    threads, rooms = _registries()
    resident = ""
    try:
        resident = (json.loads((EA_DIR / "state" / "resident-pane.json").read_text())
                    .get("session_id") or "").lower()
    except (OSError, json.JSONDecodeError):
        pass
    cutoff = _now() - timedelta(days=days)
    exclude = {s.lower() for s in (exclude or set())}
    rows: list[dict] = []
    files = []
    for d in dirs:
        try:
            files += list(d.glob("*.jsonl"))
        except OSError:
            pass
    for p in files:
        sid = p.stem.lower()
        if sid in exclude:
            continue
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        tail = tail_transcript(p)
        h = history.get(sid, {})
        last_prompt = h.get("last_prompt") or tail["last_user"]
        if not last_prompt and not tail["last_assistant"]:
            continue  # a transcript with nothing said in it
        room = rooms.get(sid)
        thread = threads.get(sid)
        if room:
            where = f"#{room.get('name') or room['channel']} (session room)"
            kind = "room"
        elif thread:
            title = thread.get("title") or ""
            where = f"Slack thread {title!r}" if title else f"Slack thread {thread['thread_ts']}"
            kind = "thread"
        elif sid == resident:
            where = "the EA resident (Slack, main channel)"
            kind = "resident"
        elif h.get("last_prompt"):
            where = "shell session"
            kind = "shell"
        else:
            where = "bot dispatch (a Slack turn or a cron agent)"
            kind = "dispatch"
        rows.append({
            "session_id": sid, "kind": kind, "where": where,
            "model": (room or {}).get("model") or (thread or {}).get("model") or tail["model"],
            "last_ts": max([t for t in (tail["last_ts"], h.get("last_ts"), mtime) if t]),
            "prompts": h.get("prompts", 0),
            "last_prompt": _clean(last_prompt, 110),
            "last_reply": _clean(tail["last_assistant"], 160),
        })
    rows.sort(key=lambda r: r["last_ts"], reverse=True)
    return rows[:limit]


def _age(dt: datetime) -> str:
    s = int((_now() - dt).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def brief(rows: list[dict], max_chars: int = 2200) -> str:
    """The text a session reads. Empty when there is nothing else."""
    if not rows:
        return ""
    head = "CONVERSATION INDEX (other sessions on this host, newest first):"
    lines = [head]
    for r in rows:
        model = f", {r['model']}" if r.get("model") else ""
        lines.append(f"- {r['where']}{model} | {_age(r['last_ts'])} | {r['prompts']} prompts | `{r['session_id'][:8]}`")
        if r.get("last_prompt"):
            lines.append(f"    asked: {r['last_prompt']}")
        if r.get("last_reply"):
            lines.append(f"    last:  {r['last_reply']}")
    tail = ("(Before starting work another conversation may already be doing, check "
            "here; refer to it by its room or title. `session_index.py --json` for detail.)")
    out: list[str] = []
    size = 0
    for ln in lines:
        if size + len(ln) + 1 > max_chars - len(tail) - 1:
            out.append("- … (cut)")
            break
        out.append(ln)
        size += len(ln) + 1
    out.append(tail)
    return "\n".join(out)


def _main() -> int:
    ap = argparse.ArgumentParser(description="what every other conversation on this host is doing", allow_abbrev=False)
    ap.add_argument("--exclude", action="append", default=[], help="session id(s) to leave out (yourself)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--max-chars", type=int, default=2200)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rows = scan(days=a.days, exclude=set(a.exclude), limit=a.limit)
    if a.json:
        print(json.dumps([{**r, "last_ts": r["last_ts"].isoformat()} for r in rows], indent=1))
    else:
        print(brief(rows, max_chars=a.max_chars))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
