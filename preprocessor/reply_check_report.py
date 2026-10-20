#!/usr/bin/env python3
"""reply_check_report: what the project-room reply check did over a window.

The Slack bot appends one JSONL record per bounce to ~/.ea/logs/reply-check.jsonl
(a `pre-move/` copy beside it is read as well; a catch is a changed reply
with a read behind it; the cost is each pass's own, paired from the bot log
for records that predate the `cost_is_delta` field)
(ea-chat-bot.py _reply_check_log): the reasons, the reads the turn made before
and after the second pass, whether the second pass changed the reply, and its
time and cost. This prints the numbers that decide whether the check earns its
keep and whether a room model can be trusted with the ledger:

  catches        bounces whose second pass changed the reply (the first reply
                 would have posted something a read contradicted)
  false alarms   bounces whose second pass returned the reply unchanged
  reads gained   ledger / mail / web reads the second pass made

With --bot-log the room turns in the same window are counted from the bot's
log (Session: RESUME/NEW lines whose session belongs to a project room), so
the bounce rate has a denominator.

    python3 reply_check_report.py [--since 2026-09-24] [--until ...]
        [--log ~/.ea/logs/reply-check.jsonl] [--bot-log ~/.ea/logs/slack-bot.log]
        [--registry ~/.ea/config/ea-channels.json] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from log_paths import owner_log as _owner_log  # noqa: E402
# The bot writes these to the owner-private dir (docs/SECURITY.md).
LOG = _owner_log("reply-check.jsonl")
BOT_LOG = _owner_log("slack-bot.log")
REGISTRY = EA_DIR / "config" / "ea-channels.json"
_SESSION_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) [\d:,]+ .*Session: (?:RESUME|NEW) ([0-9a-f]{8})")


def _day(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def load(log: Path, since: datetime | None, until: datetime | None) -> list[dict]:
    """Bounce records in the window, oldest first. A `pre-move/` copy of
    the log beside it (the lines written before the logs moved to the
    owner-private dir) is read too; a record present in both counts once."""
    seen: dict[tuple, dict] = {}
    for path in (log.parent / "pre-move" / log.name, log):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                ts = datetime.fromisoformat(r["ts"])
            except Exception:
                continue
            if since and ts < since:
                continue
            if until and ts >= until:
                continue
            seen[(r["ts"], r.get("thread_ts"))] = r
    return [seen[k] for k in sorted(seen)]


_DONE_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) INFO \[(mono|mono-check)\] done in [\d.]+s"
    r" \| turns=\d+ \| tokens=[^|]* \| cost=\$([\d.]+)")


def marginal_costs(bot_log: Path) -> list[tuple[datetime, float]]:
    """Second-pass costs by time, for records written while the bot logged
    the session's running total instead of the pass's own cost. The bot
    log's `done` lines carry that total; a mono-check line minus the line
    before it is the pass. Times are UTC; the bot log is in local time."""
    if not bot_log.exists():
        return []
    local = datetime.now().astimezone().tzinfo
    prev_cost = None
    out: list[tuple[datetime, float]] = []
    for line in bot_log.read_text(errors="replace").splitlines():
        m = _DONE_LINE.match(line)
        if not m:
            continue
        cost = float(m.group(4))
        if m.group(3) == "mono-check" and prev_cost is not None:
            when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
                microsecond=int(m.group(2)) * 1000, tzinfo=local)
            out.append((when.astimezone(timezone.utc), max(cost - prev_cost, 0.0)))
        prev_cost = cost
    return out


def _pass_cost(r: dict, legacy: list[tuple[datetime, float]]) -> float | None:
    """The pass's own cost: the record's when it was logged as a delta,
    else the bot-log pass logged within two seconds of the record, else
    unknown."""
    if r.get("cost_is_delta"):
        return float(r.get("second_pass_cost_usd") or 0)
    try:
        ts = datetime.fromisoformat(r["ts"]).astimezone(timezone.utc)
    except Exception:
        return None
    best = None
    for when, cost in legacy:
        gap = abs((when - ts).total_seconds())
        if gap <= 2.0 and (best is None or gap < best[0]):
            best = (gap, cost)
    return best[1] if best else None


def _read_made(r: dict) -> bool:
    if "read_made" in r:
        return bool(r["read_made"])
    return sum(int(v or 0) for v in (r.get("reads_after") or {}).values()) > 0


def room_turns(bot_log: Path, registry: Path, since: datetime | None,
               until: datetime | None) -> dict[str, int]:
    """Turns per room in the window, from the bot log's Session lines."""
    if not bot_log.exists() or not registry.exists():
        return {}
    rooms = {}
    try:
        for row in json.loads(registry.read_text()).values():
            if row.get("kind") == "project" and row.get("session_id"):
                rooms[row["session_id"][:8]] = row.get("name", "")
    except Exception:
        return {}
    counts: Counter = Counter()
    for line in bot_log.read_text(errors="replace").splitlines():
        m = _SESSION_LINE.match(line)
        if not m or m.group(2) not in rooms:
            continue
        day = _day(m.group(1))
        if since and day < since.replace(hour=0, minute=0, second=0, microsecond=0):
            continue
        if until and day >= until:
            continue
        counts[rooms[m.group(2)]] += 1
    return dict(counts)


def summarize(rows: list[dict], turns: dict[str, int],
              legacy_costs: list[tuple[datetime, float]] | None = None) -> dict:
    """A catch is a second pass that read something and changed the reply.
    A change with no read behind it is a rewrite, counted apart: the check
    exists to put a read behind a claim, and a reply rewritten without one
    is not evidence either way."""
    legacy_costs = legacy_costs or []
    by_room: dict[str, dict] = defaultdict(lambda: {"bounces": 0, "catches": 0, "failed": 0,
                                                    "unverified": 0,
                                                    "reasons": Counter(), "model": ""})
    reasons: Counter = Counter()
    by_model: dict[str, dict] = defaultdict(lambda: {"bounces": 0, "catches": 0})
    gained = Counter()
    secs = []
    cost = 0.0
    costed = 0
    code_held = sum(1 for r in rows if r.get("verified_by") == "code")
    for r in rows:
        room = r.get("room") or "?"
        b = by_room[room]
        b["bounces"] += 1
        b["model"] = r.get("model") or b["model"]
        for reason in r.get("reasons") or []:
            b["reasons"][reason] += 1
            reasons[reason] += 1
        m = by_model[r.get("model") or "?"]
        m["bounces"] += 1
        if not r.get("second_pass_ok"):
            b["failed"] += 1
        elif r.get("changed") and _read_made(r):
            b["catches"] += 1
            m["catches"] += 1
        elif r.get("changed"):
            b["unverified"] += 1
        for k, v in (r.get("reads_after") or {}).items():
            gained[k] += int(v or 0)
        if r.get("second_pass_s") is not None:
            secs.append(float(r["second_pass_s"]))
        c = _pass_cost(r, legacy_costs)
        if c is not None:
            cost += c
            costed += 1
    total = len(rows)
    catches = sum(b["catches"] for b in by_room.values())
    failed = sum(b["failed"] for b in by_room.values())
    unverified = sum(b["unverified"] for b in by_room.values())
    return {
        "bounces": total, "catches": catches, "unverified_changes": unverified,
        "false_alarms": total - catches - unverified - failed,
        "second_pass_failed": failed, "held_on_code_reads": code_held,
        "reasons": dict(reasons),
        "reads_gained": dict(gained),
        "second_pass_mean_s": round(sum(secs) / len(secs), 1) if secs else None,
        "second_pass_cost_usd": round(cost, 2), "passes_costed": costed,
        "passes_uncosted": total - costed,
        "room_turns": sum(turns.values()) if turns else None,
        "by_room": {k: {**v, "reasons": dict(v["reasons"]), "turns": turns.get(k)}
                    for k, v in sorted(by_room.items())},
        "by_model": dict(by_model),
    }


def render(s: dict, since: str, until: str) -> str:
    out = [f"Reply check, {since} to {until}"]
    denom = f" of {s['room_turns']} room turns" if s["room_turns"] is not None else ""
    out.append(f"  bounces: {s['bounces']}{denom}   catches (changed after a read): {s['catches']}   "
               f"unverified changes (changed, no read): {s['unverified_changes']}   "
               f"false alarms (unchanged): {s['false_alarms']}   second pass failed: {s['second_pass_failed']}")
    if s.get("held_on_code_reads"):
        out.append(f"  held on code reads, no second pass: {s['held_on_code_reads']} (counted with the false alarms)")
    if s["reasons"]:
        out.append("  reasons: " + ", ".join(f"{k} {v}" for k, v in sorted(s["reasons"].items())))
    if s["reads_gained"]:
        out.append("  reads the second pass made: " + ", ".join(f"{k} {v}" for k, v in sorted(s["reads_gained"].items())))
    if s["second_pass_mean_s"] is not None:
        unc = f", {s['passes_uncosted']} passes without a cost" if s.get("passes_uncosted") else ""
        per = (f", ${s['second_pass_cost_usd'] / s['passes_costed']:.2f} per pass"
               if s.get("passes_costed") else "")
        out.append(f"  second pass: mean {s['second_pass_mean_s']} s; marginal cost "
                   f"${s['second_pass_cost_usd']} over {s['passes_costed']} passes{per}{unc}")
    if s["by_model"]:
        out.append("  by model: " + "; ".join(f"{k}: {v['catches']} catches / {v['bounces']} bounces"
                                             for k, v in sorted(s["by_model"].items())))
    if s["by_room"]:
        out.append("  by room:")
        for k, v in s["by_room"].items():
            t = f" of {v['turns']} turns" if v.get("turns") is not None else ""
            rs = ", ".join(f"{a} {b}" for a, b in sorted(v["reasons"].items()))
            unv = f", {v['unverified']} unverified" if v.get("unverified") else ""
            out.append(f"    {k}: {v['bounces']} bounces{t}, {v['catches']} catches{unv} ({rs}) [{v['model']}]")
    if not s["bounces"]:
        out.append("  (no bounces logged in the window)")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="YYYY-MM-DD (inclusive, UTC)")
    ap.add_argument("--until", help="YYYY-MM-DD (exclusive, UTC)")
    ap.add_argument("--log", type=Path, default=LOG)
    ap.add_argument("--bot-log", type=Path, default=BOT_LOG)
    ap.add_argument("--registry", type=Path, default=REGISTRY)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    since = _day(a.since) if a.since else None
    until = _day(a.until) if a.until else None
    rows = load(a.log, since, until)
    turns = room_turns(a.bot_log, a.registry, since, until)
    s = summarize(rows, turns, marginal_costs(a.bot_log))
    if a.json:
        print(json.dumps(s, indent=1))
    else:
        print(render(s, a.since or "start", a.until or "now"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
