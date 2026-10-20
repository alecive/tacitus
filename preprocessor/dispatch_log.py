#!/usr/bin/env python3
"""Dispatch instrumentation log backed by SQLite.

Records every message dispatch decision (handler match, Claude invocation)
with entity matching quality, tool call details, and outcome classification.
Not a feedback loop, only structured measurement for precision analysis.

The database sits in the resident's state directory and the bot, an owner
process, writes it, so every open goes through ledger.guarded_connect
(docs/SECURITY.md, "Owner trust rules").

Usage:
    python3 dispatch_log.py stats [--handler <name>] [--days <n>]
    python3 dispatch_log.py redundant [--days <n>]
    python3 dispatch_log.py gaps [--days <n>]
    python3 dispatch_log.py recent [--limit <n>]

Programmatic API:
    from preprocessor.dispatch_log import log_claude, log_correction
    from preprocessor.dispatch_log import analyze_tool_calls
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import ledger as _ledger  # guarded_connect: the shared database's owner-side open
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import ledger as _ledger

EA_DIR = Path(os.environ.get("EA_DIR", os.path.expanduser("~/.ea")))
DB_PATH = EA_DIR / "state" / "dispatch-log.db"

# ── Schema ──────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT NOT NULL,
    message_hash          TEXT,
    message_preview       TEXT,

    -- Dispatch path
    path                  TEXT NOT NULL,
    handler_name          TEXT,

    -- Entity matching
    entities_extracted    TEXT,
    ledger_match_id       TEXT,
    ledger_match_score    REAL,
    ledger_runner_up_score REAL,
    briefing_ids_injected TEXT,

    -- Claude execution (null for handler path)
    tool_calls            TEXT,
    tool_call_count       INTEGER,
    redundant_searches    INTEGER DEFAULT 0,
    used_preloaded_ids    INTEGER DEFAULT 0,
    duration_ms           INTEGER,
    cost_usd              REAL,
    num_turns             INTEGER,

    -- Outcome
    produced_draft        INTEGER DEFAULT 0,
    produced_event        INTEGER DEFAULT 0,
    produced_ledger_op    INTEGER DEFAULT 0,
    user_correction       INTEGER DEFAULT 0,

    -- Classification
    category              TEXT
);

CREATE INDEX IF NOT EXISTS idx_dispatch_timestamp ON dispatch_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_dispatch_path ON dispatch_log(path);
CREATE INDEX IF NOT EXISTS idx_dispatch_hash ON dispatch_log(message_hash);
CREATE INDEX IF NOT EXISTS idx_dispatch_category ON dispatch_log(category);
"""

# ── DB helpers ──────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    """The dispatch-log database, opened through ledger.guarded_connect. An
    owner process opens it only after the shared-database checks and never
    creates the directory or the file by path; a missing file is created
    with the resident's access. The resident side creates the directory."""
    if not _ledger._is_owner():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _ledger.guarded_connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _msg_hash(msg: str) -> str:
    return hashlib.sha256(msg.encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Logging API (called from ea-chat-bot.py) ───────────────────────────

def log_claude(msg: str, path: str, tool_calls: list[dict],
               briefing_ids: list[str], entities: list[str],
               duration_ms: int = 0, cost_usd: float = 0.0,
               num_turns: int = 0, produced_draft: bool = False,
               produced_event: bool = False,
               produced_ledger_op: bool = False) -> int:
    """Log a Claude invocation with tool call analysis. Returns the row ID."""
    redundant, used_preloaded = analyze_tool_calls(tool_calls, briefing_ids, entities)

    # Auto-classify
    if redundant > 0:
        category = "claude-redundant-search"
    elif len(tool_calls) <= 3 and used_preloaded > 0:
        category = "claude-efficient"
    elif len(tool_calls) > 5:
        category = "claude-complex"
    elif any(tc.get("name") in ("Bash",) and "search" in tc.get("args_summary", "")
             for tc in tool_calls):
        category = "claude-needed-search"
    else:
        category = "claude-efficient"

    db = _get_db()
    try:
        cur = db.execute(
            """INSERT INTO dispatch_log
               (timestamp, message_hash, message_preview, path, handler_name,
                entities_extracted, briefing_ids_injected,
                tool_calls, tool_call_count, redundant_searches,
                used_preloaded_ids, duration_ms, cost_usd, num_turns,
                produced_draft, produced_event, produced_ledger_op, category)
               VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now_iso(), _msg_hash(msg), msg[:120], path,
             json.dumps(entities), json.dumps(briefing_ids),
             json.dumps(tool_calls), len(tool_calls), redundant,
             used_preloaded, duration_ms, cost_usd, num_turns,
             int(produced_draft), int(produced_event),
             int(produced_ledger_op), category),
        )
        db.commit()
        return cur.lastrowid
    finally:
        db.close()


def log_correction(message_hash: str) -> bool:
    """Flag a prior dispatch as user-corrected. Returns True if found."""
    db = _get_db()
    try:
        # The newest matching row, chosen in a subquery: UPDATE with
        # ORDER BY and LIMIT is a build option SQLite leaves out on macOS.
        cur = db.execute(
            """UPDATE dispatch_log SET user_correction = 1,
               category = CASE
                   WHEN path = 'handler' THEN 'handler-wrong'
                   ELSE category
               END
               WHERE id = (SELECT id FROM dispatch_log
                           WHERE message_hash = ? AND user_correction = 0
                           ORDER BY id DESC LIMIT 1)""",
            (message_hash,),
        )
        db.commit()
        return cur.rowcount > 0
    finally:
        db.close()


# ── Analysis ────────────────────────────────────────────────────────────

def analyze_tool_calls(tool_calls: list[dict], briefing_ids: list[str],
                       entities: list[str]) -> tuple[int, int]:
    """Compare Claude's tool calls against pre-loaded data.

    Returns (redundant_searches, used_preloaded_ids).
    """
    briefing_id_set = set(briefing_ids)
    entity_set = {e.lower() for e in entities}
    redundant = 0
    used_preloaded = 0

    for tc in tool_calls:
        name = tc.get("name", "")
        args = tc.get("args_summary", "")
        args_lower = args.lower()

        # Detect redundant searches: search-mail or ledger search for
        # entities already in the prompt
        if "search-mail" in args_lower or "search" in args_lower:
            if any(ent in args_lower for ent in entity_set):
                redundant += 1

        # Detect pre-loaded ID usage: tool call references a Message ID
        # that was already injected in the briefing
        if briefing_id_set:
            for bid in briefing_id_set:
                # Check first 20 chars of ID (enough to match)
                if bid[:20] in args:
                    used_preloaded += 1
                    break

    return redundant, used_preloaded


# ── Query helpers ───────────────────────────────────────────────────────

def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def query_precision(handler_name: str | None = None, days: int = 7) -> dict:
    """Return category counts. Optionally filter by handler."""
    db = _get_db()
    try:
        cutoff = _cutoff(days)
        if handler_name:
            rows = db.execute(
                """SELECT category, COUNT(*) as cnt FROM dispatch_log
                   WHERE timestamp > ? AND handler_name = ?
                   GROUP BY category ORDER BY cnt DESC""",
                (cutoff, handler_name),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT category, COUNT(*) as cnt FROM dispatch_log
                   WHERE timestamp > ?
                   GROUP BY category ORDER BY cnt DESC""",
                (cutoff,),
            ).fetchall()
        return {r["category"]: r["cnt"] for r in rows}
    finally:
        db.close()


def query_path_summary(days: int = 7) -> list[dict]:
    """Return dispatch count by path."""
    db = _get_db()
    try:
        cutoff = _cutoff(days)
        rows = db.execute(
            """SELECT path, COUNT(*) as cnt, AVG(duration_ms) as avg_ms,
                      SUM(cost_usd) as total_cost,
                      AVG(tool_call_count) as avg_tools,
                      SUM(redundant_searches) as total_redundant
               FROM dispatch_log WHERE timestamp > ?
               GROUP BY path ORDER BY cnt DESC""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def query_redundant_searches(days: int = 7) -> list[dict]:
    """Return dispatches where Claude searched for pre-loaded data."""
    db = _get_db()
    try:
        cutoff = _cutoff(days)
        rows = db.execute(
            """SELECT timestamp, message_preview, entities_extracted,
                      tool_calls, redundant_searches, tool_call_count
               FROM dispatch_log
               WHERE timestamp > ? AND redundant_searches > 0
               ORDER BY timestamp DESC""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def query_preload_gaps(days: int = 7) -> list[dict]:
    """Return dispatches where Claude needed data not in the prompt."""
    db = _get_db()
    try:
        cutoff = _cutoff(days)
        rows = db.execute(
            """SELECT timestamp, message_preview, entities_extracted,
                      tool_calls, category
               FROM dispatch_log
               WHERE timestamp > ? AND category = 'claude-needed-search'
               ORDER BY timestamp DESC""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def query_recent(limit: int = 20) -> list[dict]:
    """Return most recent dispatches."""
    db = _get_db()
    try:
        rows = db.execute(
            """SELECT timestamp, message_preview, path, handler_name,
                      category, tool_call_count, redundant_searches,
                      used_preloaded_ids, duration_ms, cost_usd,
                      user_correction
               FROM dispatch_log ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


# ── CLI ─────────────────────────────────────────────────────────────────

def _fmt_table(rows: list[dict], columns: list[str] | None = None) -> str:
    """Simple ASCII table formatter."""
    if not rows:
        return "(no data)"
    if columns is None:
        columns = list(rows[0].keys())
    # Compute column widths
    widths = {c: len(c) for c in columns}
    for r in rows:
        for c in columns:
            val = str(r.get(c, ""))
            widths[c] = max(widths[c], min(len(val), 60))

    header = " | ".join(c.ljust(widths[c])[:widths[c]] for c in columns)
    sep = "-+-".join("-" * widths[c] for c in columns)
    lines = [header, sep]
    for r in rows:
        line = " | ".join(
            str(r.get(c, "")).ljust(widths[c])[:widths[c]] for c in columns
        )
        lines.append(line)
    return "\n".join(lines)


def cmd_stats(args):
    precision = query_precision(handler_name=args.handler, days=args.days)
    path_summary = query_path_summary(days=args.days)

    print(f"=== Dispatch Stats (last {args.days} days) ===\n")

    if path_summary:
        print("By path:")
        print(_fmt_table(path_summary,
                         ["path", "cnt", "avg_ms", "total_cost", "avg_tools", "total_redundant"]))
        print()

    if precision:
        print("By category:")
        total = sum(precision.values())
        for cat, cnt in sorted(precision.items(), key=lambda x: -x[1]):
            pct = cnt / total * 100
            print(f"  {cat:30s}  {cnt:4d}  ({pct:5.1f}%)")
        print(f"  {'TOTAL':30s}  {total:4d}")
    else:
        print("No dispatches recorded yet.")

    print(json.dumps({"precision": precision, "paths": path_summary}))


def cmd_redundant(args):
    rows = query_redundant_searches(days=args.days)
    print(f"=== Redundant Searches (last {args.days} days): {len(rows)} ===\n")
    for r in rows:
        print(f"  {r['timestamp'][:19]}  {r['message_preview']}")
        print(f"    entities: {r['entities_extracted']}")
        print(f"    redundant: {r['redundant_searches']} / {r['tool_call_count']} tool calls")
        print()
    print(json.dumps(rows))


def cmd_gaps(args):
    rows = query_preload_gaps(days=args.days)
    print(f"=== Pre-load Gaps (last {args.days} days): {len(rows)} ===\n")
    for r in rows:
        print(f"  {r['timestamp'][:19]}  {r['message_preview']}")
        print(f"    entities: {r['entities_extracted']}")
        tc = json.loads(r.get("tool_calls", "[]") or "[]")
        searches = [t for t in tc if "search" in t.get("args_summary", "").lower()]
        for s in searches:
            print(f"    -> {s.get('name', '?')}: {s.get('args_summary', '')[:80]}")
        print()
    print(json.dumps(rows))


def cmd_recent(args):
    rows = query_recent(limit=args.limit)
    print(f"=== Recent Dispatches (last {args.limit}) ===\n")
    print(_fmt_table(rows, ["timestamp", "path", "handler_name", "category",
                            "tool_call_count", "redundant_searches",
                            "duration_ms", "cost_usd", "user_correction"]))
    print()
    print(json.dumps(rows))


def main():
    parser = argparse.ArgumentParser(description="Dispatch log analysis")
    sub = parser.add_subparsers(dest="command")

    p_stats = sub.add_parser("stats", help="Precision stats by category/path")
    p_stats.add_argument("--handler", type=str, default=None)
    p_stats.add_argument("--days", type=int, default=7)

    p_red = sub.add_parser("redundant", help="Redundant search report")
    p_red.add_argument("--days", type=int, default=7)

    p_gaps = sub.add_parser("gaps", help="Pre-load gap report")
    p_gaps.add_argument("--days", type=int, default=7)

    p_recent = sub.add_parser("recent", help="Recent dispatches")
    p_recent.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()

    commands = {
        "stats": cmd_stats,
        "redundant": cmd_redundant,
        "gaps": cmd_gaps,
        "recent": cmd_recent,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
