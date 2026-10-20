#!/usr/bin/env python3
"""ghost_backtest: the ghost week's judge on past exchanges, in a sandbox.

The ghost week (ghost_week.py) grades reads as they happen. This replays
exchanges that already happened: the EA's own Claude transcripts hold,
for every turn, the user's message, each mail read the turn made with
the text it returned, the reply it posted and the draft bodies it wrote.
For each such exchange the backtest seals each read the way `mail =
sealed` would (`mail_privacy.seal_output`, with the remote model reading
any message it has not read yet), composes the ledger's thread brief
for the same threads (`mail_privacy.thread_brief`), and asks the judge
two questions at once: could the turn have been done from the sealed
record alone, and could it have been done from the ledger brief alone?
The second answer is what the ledger still needs. With `--replay N`, up
to N exchanges are also run again on Claude with no tools and only the
sealed inputs (an A/B of the reply itself), and a second judge compares
the replayed reply with the one the user saw.

Nothing is posted, sent, staged or written to the mailbox or the
ledger. The only writes are the backtest's own files under
`<private>/remote-llm/backtest/<stamp>/` and the remote model's records
of messages it had not read (the same store the tick writes). The judge
and the replay run on Claude as ea-bot, with text that those turns had
already shown Claude.

    collect --days N [--max N]     exchanges from the transcripts, counts only
    run --days N [--max N] [--replay N] [--model M]
                                   collect, seal, brief, judge, (replay), report
    report <stamp>                 print a run's report again

The transcripts are ea-bot's (mode 600); the collector runs as ea-bot
through sudo and hands back what the owner side needs, size-capped.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import owner_trust as _owner_trust  # noqa: E402
import remote_llm  # noqa: E402
import ghost_week  # noqa: E402

EA_BOT_HOME = Path("/home/ea-bot")
TRANSCRIPTS = EA_BOT_HOME / ".claude" / "projects" / "-home-ea-bot-ea-system"
BACKTEST_DIR = "backtest"
READ_OUT_CAP = 120_000
REPLY_CAP = 6_000
USER_CAP = 6_000
BODY_CAP = 6_000
DEFAULT_DAYS = 30
DEFAULT_MAX = 40
REPLAY_MODEL = "sonnet"
REPLAY_TIMEOUT_S = 600
MAIL_VERBS = ("get-conversation", "get-message", "search-mail", "list-mail")

# Runs as ea-bot (python3 -I, script on stdin): one JSON line per exchange
# that made a mail read, newest transcripts last. Sizes are capped here,
# before anything crosses to the owner side.
EXTRACTOR = r'''
import json, glob, os, sys, re, datetime
days, cap_out, cap_reply, cap_user, cap_body = (int(sys.argv[1]), int(sys.argv[2]),
                                                int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]))
d = "/home/ea-bot/.claude/projects/-home-ea-bot-ea-system"
cut = (datetime.datetime.now() - datetime.timedelta(days=days)).timestamp()
VERBS = ("get-conversation", "get-message", "search-mail", "list-mail")
files = sorted((f for f in glob.glob(d + "/*.jsonl") if os.path.getmtime(f) >= cut), key=os.path.getmtime)

def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(str(b.get("text") or ""))
    return "\n".join(out)

def result_text(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(str(b.get("text") or ""))
    return "\n".join(out)

for f in files:
    sid = os.path.basename(f)[:-6]
    rows = []
    with open(f) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") in ("user", "assistant"):
                rows.append(r)
    turn = None
    pending = {}
    def flush():
        if turn and turn["reads"]:
            print(json.dumps(turn))
    for r in rows:
        m = r.get("message") or {}
        content = m.get("content")
        if r["type"] == "user":
            blocks = content if isinstance(content, list) else []
            results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            if results:
                for b in results:
                    tu = pending.pop(b.get("tool_use_id"), None)
                    if tu and turn is not None:
                        tu["output"] = result_text(b.get("content"))[:cap_out]
                        tu["is_error"] = bool(b.get("is_error"))
                continue
            flush()
            turn = {"session": sid, "ts": r.get("timestamp") or "", "user": text_of(content)[:cap_user],
                    "reads": [], "reply": "", "drafts": [], "writes": [], "tools": 0}
            pending = {}
        elif turn is not None:
            blocks = content if isinstance(content, list) else []
            texts = [str(b.get("text") or "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                turn["reply"] = ("\n".join(texts))[:cap_reply]
            for b in blocks:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                turn["tools"] += 1
                inp = b.get("input") or {}
                if b.get("name") == "Bash":
                    cmd = str(inp.get("command") or "")
                    if "graph_actions.py" in cmd:
                        verb = next((v for v in VERBS if " " + v in cmd), "")
                        if verb:
                            tu = {"verb": verb, "cmd": cmd[:600], "output": ""}
                            turn["reads"].append(tu)
                            pending[b.get("id")] = tu
                        elif re.search(r" (create-reply|create-draft|update-draft)\b", cmd):
                            turn["drafts"].append(cmd[:600])
                elif b.get("name") == "Write":
                    path = str(inp.get("file_path") or "")
                    if re.search(r"(body|brief|pending-event[^/]*)\.(txt|json|md)$", path):
                        turn["writes"].append({"path": path[-80:], "content": str(inp.get("content") or "")[:cap_body]})
    flush()
'''


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def collect(days: int = DEFAULT_DAYS, *, runner=None) -> list[dict]:
    """The exchanges with a mail read from the last `days` of transcripts,
    oldest first, each with its reads' text, reply, drafts and writes."""
    if runner is None:
        def runner(argv):
            p = subprocess.run(["sudo", "-n", "-u", "ea-bot", "python3", "-I", "-", *argv],
                               input=EXTRACTOR, capture_output=True, text=True, timeout=600)
            if p.returncode:
                raise RuntimeError(f"collector failed (rc={p.returncode}): {p.stderr[-300:]}")
            return p.stdout
    out = runner([str(days), str(READ_OUT_CAP), str(REPLY_CAP), str(USER_CAP), str(BODY_CAP)])
    exchanges = []
    for line in out.splitlines():
        try:
            ex = json.loads(line)
        except ValueError:
            continue
        if isinstance(ex, dict) and ex.get("reads"):
            exchanges.append(ex)
    return exchanges


_JSON_START = re.compile(r"[\[{]")


def _parse_output(text: str):
    """The JSON a graph_actions read printed, or None when the turn piped
    it through something else."""
    t = str(text or "")
    m = _JSON_START.search(t)
    if not m:
        return None
    for end in (t.rfind("}"), t.rfind("]")):
        if end > m.start():
            try:
                return json.loads(t[m.start():end + 1])
            except ValueError:
                continue
    return None


def _user_message(text: str) -> str:
    """The user's own words inside a dispatch prompt, when the prompt
    quotes them; else the prompt's first lines."""
    m = re.search(r"(?:NEW message|new message|wrote|says|asked)[^\n]*:\s*\n+((?:>.*\n?)+)", text)
    if m:
        return re.sub(r"^> ?", "", m.group(1), flags=re.M).strip()[:1500]
    return text.strip()[:1500]


def _verb_rows(verb: str, data) -> list[dict]:
    return ghost_week._rows(verb, data)


def _redo(rows: list[dict], *, cfg: dict, graph=None) -> int:
    """Extract these messages again with the extractor as it is now, so
    the sealed copy and the brief reflect the current record shape, not
    the one stored when the turn ran. Returns how many were read."""
    import mail_privacy
    user = mail_privacy._user_addresses()
    graph = graph or mail_privacy._default_graph()
    msgs = [mail_privacy._graph_message(m) for m in rows if ghost_week._has_text(m) and m.get("id")]
    if not msgs:
        return 0
    mail_privacy._inline_records(msgs, graph=graph, cfg=cfg, budget_s=240.0, user=user)
    mail_privacy._INDEX_CACHE.update(mtime=None, index=None)
    return len(msgs)


def prepare(ex: dict, *, cfg: dict | None = None, budget_s: float | None = None,
            redo: bool = False) -> dict:
    """Seal each parsable read of one exchange and compose the thread
    briefs; returns what the judge needs. With `redo`, every message in
    the reads is extracted again first."""
    import mail_privacy
    cfg = cfg if cfg is not None else remote_llm.config()
    reads, convs = [], []
    unread_before = unread_after = redone = 0
    for r in ex.get("reads") or []:
        data = _parse_output(r.get("output"))
        rows = _verb_rows(r["verb"], data) if data is not None else []
        if not rows:
            reads.append({"verb": r["verb"], "parsed": False})
            continue
        top_conv = str(data.get("conversation_id") or "") if isinstance(data, dict) else ""
        for m in rows:
            if top_conv and not (m.get("conversation_id") or m.get("conversationId")):
                m["conversation_id"] = top_conv
        index = mail_privacy.load_index()
        text_rows = [m for m in rows if ghost_week._has_text(m)]
        unread_before += sum(1 for m in text_rows if index["by_id"].get(m.get("id") or "") is None)
        if redo and r["verb"] in ("get-conversation", "get-message"):
            redone += _redo(rows, cfg=cfg)
            index = mail_privacy.load_index()
        t0 = time.monotonic()
        try:
            sealed = mail_privacy.seal_output(r["verb"], json.loads(json.dumps(data)), index=index, cfg=cfg,
                                              **({"budget_s": budget_s} if budget_s else {}))
            err = ""
        except Exception as e:
            sealed, err = None, str(e)[:200]
        unread_after += sum(1 for m in _verb_rows(r["verb"], sealed) if m.get("remote_unread")) if sealed else 0
        for m in rows:
            c = str(m.get("conversation_id") or m.get("conversationId") or
                    (index["by_id"].get(m.get("id") or "") or {}).get("conversation_id") or "")
            if c and c not in convs:
                convs.append(c)
        reads.append({"verb": r["verb"], "parsed": True, "messages": len(rows), "text_messages": len(text_rows),
                      "raw": json.dumps(data, default=str), "sealed": sealed, "seal_s": round(time.monotonic() - t0, 1),
                      "seal_error": err})
    mail_privacy._INDEX_CACHE.update(mtime=None, index=None)
    briefs = {}
    for c in convs[:6]:
        b = mail_privacy.thread_brief(c)
        if b:
            briefs[c] = b
    return {"reads": reads, "conversations": convs, "briefs": briefs,
            "rows": _ledger_rows(convs), "redone": redone,
            "unread_before": unread_before, "unread_after": unread_after}


def _ledger_rows(convs: list[str]) -> dict:
    """{conversation id: open | closed} for the threads that have a
    ledger row today."""
    if not convs:
        return {}
    try:
        import ledger
        conn = ledger.guarded_connect(ledger.DB_PATH)
        try:
            out = {}
            for c in convs:
                row = conn.execute("SELECT status FROM ledger WHERE conversation_id = ? "
                                   "ORDER BY (status NOT IN ('completed','closed')) DESC LIMIT 1", (c,)).fetchone()
                if row:
                    out[c] = "closed" if row[0] in ("completed", "closed") else "open"
            return out
        finally:
            conn.close()
    except Exception:
        return {}


JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "facts_used": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"fact": {"type": "string"}, "in_record": {"type": "boolean"},
                           "in_brief": {"type": "boolean"},
                           "category": {"type": "string", "enum": list(ghost_week.FACT_CATEGORIES)}},
            "required": ["fact", "in_record", "in_brief", "category"]}},
        "record_errors": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"field": {"type": "string"}, "record": {"type": "string"}, "thread": {"type": "string"}},
            "required": ["field", "record", "thread"]}},
        "verdict_sealed": {"type": "string", "enum": ["same", "degraded", "blocked"]},
        "verdict_brief": {"type": "string", "enum": ["same", "degraded", "blocked"]},
        "enhancement_record": {"type": "string"},
        "enhancement_brief": {"type": "string"},
    },
    "required": ["facts_used", "record_errors", "verdict_sealed", "verdict_brief",
                 "enhancement_record", "enhancement_brief"],
}

JUDGE_PROMPT = """You grade one past exchange for a professor's assistant that is moving from reading email text to reading (1) a model's record of each message and (2) the obligations ledger's brief of the thread. Answer with JSON only, in this shape:
{{"facts_used": [{{"fact": "...", "in_record": true, "in_brief": false, "category": "url"}}], "record_errors": [{{"field": "deadline", "record": "...", "thread": "..."}}], "verdict_sealed": "same", "verdict_brief": "degraded", "enhancement_record": "...", "enhancement_brief": "..."}}

THE USER ASKED: {user}

A. What the assistant saw (the text of its mail reads):
{raw}

B. What it would see with sealed mail (the record of each message):
{sealed}

C. The thread store's brief of the same threads (what a ledger row carries when one exists; the brief is composed from the records whether or not a row exists). Rows today: {rows}
{briefs}

D. What the assistant did: its reply, the drafts it wrote, the files it staged:
{did}

1. facts_used: each fact from A that D relies on, or that the assistant needed to produce D. One short line each. in_record: B states it or gives enough to infer it. in_brief: C does. category: one of {categories}.
2. record_errors: each place where B contradicts A (field: ask, deadline, offered_times, people, state, needs_reply, summary, scheduling, other). An empty deadline, offered_times or needs_reply in B that A supports counts.
3. verdict_sealed: "same" when D could have been produced from B alone (plus the user's message); "degraded" when D would lose detail but stay correct; "blocked" when D could not be produced from B, or B would lead to a wrong action.
4. verdict_brief: the same for C alone.
5. enhancement_record: one line, what the record should carry to close the largest gap. "" when none.
6. enhancement_brief: one line, what the ledger brief should carry or how a row should change so the turn could run from the row alone. "" when none."""

REPLAY_PROMPT = """REPLAY. Nothing you write is posted and you have no tools. You are the executive assistant for the user named below. You answer in a chat room. Write the reply you would post and, if the request calls for an email draft, the draft body under a line that reads DRAFT:. Short sentences, active voice, no em-dash. Email bodies in the user's voice: plain, direct, no closing clause about why it matters.

The user: {profile}

THE USER ASKED: {user}

The thread, as sealed mail returns it (the remote model's record of each message; text or body is its summary):
{sealed}

The ledger's brief of the thread:
{briefs}

Write the reply now."""

COMPARE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"verdict": {"type": "string", "enum": ["same", "minor", "major"]},
                   "missing_in_b": {"type": "array", "items": {"type": "string"}},
                   "wrong_in_b": {"type": "array", "items": {"type": "string"}},
                   "note": {"type": "string"}},
    "required": ["verdict", "missing_in_b", "wrong_in_b", "note"],
}

COMPARE_PROMPT = """Two assistants answered the same request. A read the email text; B read only a model's record of each message and the ledger's brief. Compare what they did. Answer with JSON only: {{"verdict": "same" | "minor" | "major", "missing_in_b": [...], "wrong_in_b": [...], "note": "one line"}}. "major" means B would mislead the user or take a wrong action; "minor" means B lost detail; "same" means the user would not mind which they got.

THE USER ASKED: {user}

=== A (what the user saw) ===
{a}

=== B (replayed on sealed inputs) ===
{b}"""


def _render_reads(reads: list[dict], key: str) -> str:
    out = []
    for r in reads:
        if not r.get("parsed"):
            out.append(f"({r['verb']}: output not parsable, not shown)")
            continue
        if key == "raw":
            out.append(ghost_week._render_raw(r["verb"], r["raw"]))
        else:
            out.append(ghost_week._render_sealed(r.get("sealed")))
    return ("\n===\n".join(out))[:ghost_week.PROMPT_RAW_CHARS if key == "raw" else ghost_week.PROMPT_SEALED_CHARS]


def _render_briefs(briefs: dict) -> str:
    if not briefs:
        return "(none)"
    return json.dumps(list(briefs.values()), indent=1, default=str)[:ghost_week.PROMPT_SEALED_CHARS]


def _render_did(ex: dict) -> str:
    out = []
    if ex.get("reply"):
        out.append("REPLY POSTED: " + str(ex["reply"])[:3000])
    for w in ex.get("writes") or []:
        out.append(f"WROTE {w.get('path')}:\n{str(w.get('content'))[:3000]}")
    for d in ex.get("drafts") or []:
        out.append("DRAFT COMMAND: " + str(d)[:300])
    return ("\n\n".join(out))[:ghost_week.PROMPT_ARTIFACT_CHARS] or "(nothing on file)"


def _profile_line() -> str:
    try:
        import mail_privacy
        return mail_privacy._profile_line()
    except Exception:
        return "a professor"


def judge_exchange(ex: dict, prep: dict, *, runner=None) -> dict:
    prompt = JUDGE_PROMPT.format(
        user=_user_message(ex.get("user") or ""), raw=_render_reads(prep["reads"], "raw"),
        sealed=_render_reads(prep["reads"], "sealed"), briefs=_render_briefs(prep["briefs"]),
        rows=(json.dumps(prep.get("rows") or {}) if prep.get("rows") else "none"),
        did=_render_did(ex), categories=", ".join(ghost_week.FACT_CATEGORIES))
    ans, meta = ghost_week._judge(prompt, JUDGE_SCHEMA, judge="claude", runner=runner)
    facts = [f for f in ans.get("facts_used") or [] if isinstance(f, dict)]
    errors = [e for e in ans.get("record_errors") or [] if isinstance(e, dict)]
    ok = ("same", "degraded", "blocked")
    return {"verdict_sealed": ans.get("verdict_sealed") if ans.get("verdict_sealed") in ok else "degraded",
            "verdict_brief": ans.get("verdict_brief") if ans.get("verdict_brief") in ok else "degraded",
            "facts": len(facts),
            # A fact the judge could not place (null) is neither held nor missing.
            "missing_record": [{"fact": str(f.get("fact"))[:200], "category": f.get("category") or "other"}
                               for f in facts if f.get("in_record") is False][:12],
            "missing_brief": [{"fact": str(f.get("fact"))[:200], "category": f.get("category") or "other"}
                              for f in facts if f.get("in_brief") is False][:12],
            "unplaced": sum(1 for f in facts if f.get("in_record") is None),
            "record_errors": [{"field": str(e.get("field"))[:30], "record": str(e.get("record"))[:160],
                               "thread": str(e.get("thread"))[:160]} for e in errors][:12],
            "critical_errors": sum(1 for e in errors if str(e.get("field")) in ghost_week.CRITICAL_FIELDS),
            "enhancement_record": str(ans.get("enhancement_record") or "")[:300],
            "enhancement_brief": str(ans.get("enhancement_brief") or "")[:300],
            "cost_usd": meta.get("cost_usd")}


def replay_exchange(ex: dict, prep: dict, *, model: str = REPLAY_MODEL, runner=None, claude=None) -> dict:
    """Run the turn again on the sealed inputs with no tools, then compare
    the two replies."""
    import mail_privacy
    claude = claude or (lambda prompt, m: mail_privacy._run_claude(prompt, m, ""))
    prompt = REPLAY_PROMPT.format(profile=_profile_line(), user=_user_message(ex.get("user") or ""),
                                  sealed=_render_reads(prep["reads"], "sealed"),
                                  briefs=_render_briefs(prep["briefs"]))
    r = claude(prompt, model)
    b = str(r.get("text") or "")
    out = {"replay_ok": bool(b), "replay_cost_usd": r.get("cost_usd"), "replay": b[:REPLY_CAP]}
    if not b:
        out["replay_error"] = str(r.get("stderr") or "")[-200:]
        return out
    cmp_prompt = COMPARE_PROMPT.format(user=_user_message(ex.get("user") or ""), a=_render_did(ex), b=b[:6000])
    try:
        ans, meta = ghost_week._judge(cmp_prompt, COMPARE_SCHEMA, judge="claude", runner=runner)
        out.update({"compare": ans.get("verdict") if ans.get("verdict") in ("same", "minor", "major") else "minor",
                    "missing_in_b": [str(x)[:160] for x in ans.get("missing_in_b") or []][:8],
                    "wrong_in_b": [str(x)[:160] for x in ans.get("wrong_in_b") or []][:8],
                    "note": str(ans.get("note") or "")[:300], "compare_cost_usd": meta.get("cost_usd")})
    except Exception as e:
        out["compare_error"] = str(e)[:200]
    return out


def _select(exchanges: list[dict], max_n: int) -> list[dict]:
    """The exchanges worth judging: a parsable thread or message read,
    newest first, up to max_n; search and list reads alone do not count."""
    picked = []
    for ex in reversed(exchanges):
        good = [r for r in ex.get("reads") or [] if r["verb"] in ("get-conversation", "get-message")
                and _verb_rows(r["verb"], _parse_output(r.get("output")))]
        if good:
            picked.append(ex)
        if len(picked) >= max_n:
            break
    return picked


def run(*, days: int = DEFAULT_DAYS, max_n: int = DEFAULT_MAX, replay: int = 0, model: str = REPLAY_MODEL,
        collector=None, judge_runner=None, claude=None, budget_s: float | None = None,
        stamp: str | None = None, progress=None, redo: bool = False) -> dict:
    stamp = stamp or _now().strftime("%Y%m%d-%H%M")
    out_dir = remote_llm.store_dir() / BACKTEST_DIR / stamp
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    exchanges = collect(days, runner=collector)
    picked = _select(exchanges, max_n)
    # A run resumed under the same stamp keeps the exchanges already judged.
    rows = []
    done = set()
    try:
        for line in (out_dir / "exchanges.jsonl").read_text().splitlines():
            r = json.loads(line)
            rows.append(r)
            done.add((r.get("session"), r.get("ts")))
    except (OSError, ValueError):
        rows, done = [], set()
    for i, ex in enumerate(picked, 1):
        if (ex.get("session"), ex.get("ts")) in done:
            continue
        if progress:
            progress(f"{i}/{len(picked)} {ex.get('ts', '')[:16]} reads={len(ex['reads'])}")
        prep = prepare(ex, budget_s=budget_s, redo=redo)
        row = {"i": i, "session": ex.get("session"), "ts": ex.get("ts"), "user": _user_message(ex.get("user") or ""),
               "reads": [{k: v for k, v in r.items() if k not in ("raw", "sealed")} for r in prep["reads"]],
               "conversations": prep["conversations"], "briefs": len(prep["briefs"]),
               "brief_on_row": bool(prep["briefs"]), "ledger_rows": prep.get("rows") or {},
               "redone": prep.get("redone", 0), "unread_before": prep["unread_before"],
               "unread_after": prep["unread_after"], "tools": ex.get("tools"),
               "drafts": len(ex.get("drafts") or []), "writes": len(ex.get("writes") or [])}
        try:
            row.update(judge_exchange(ex, prep, runner=judge_runner))
        except Exception as e:
            row["error"] = str(e)[:200]
        if replay and i <= replay and not row.get("error"):
            try:
                row.update(replay_exchange(ex, prep, model=model, runner=judge_runner, claude=claude))
            except Exception as e:
                row["replay_error"] = str(e)[:200]
        rows.append(row)
        with open(out_dir / "exchanges.jsonl", "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    rep = summarize(rows, stamp=stamp, collected=len(exchanges), days=days)
    _owner_trust.write_owner_file(out_dir / "report.md", rep["markdown"].encode(), mode=0o600)
    _owner_trust.write_owner_file(out_dir / "report.json",
                                  json.dumps({k: v for k, v in rep.items() if k != "markdown"},
                                             indent=1, default=str).encode(), mode=0o600)
    rep["path"] = str(out_dir / "report.md")
    return rep


def summarize(rows: list[dict], *, stamp: str, collected: int, days: int) -> dict:
    judged = [r for r in rows if r.get("verdict_sealed")]
    cnt = ghost_week._count
    m = {"stamp": stamp, "days": days, "collected": collected, "selected": len(rows), "judged": len(judged),
         "errors": sum(1 for r in rows if r.get("error")),
         "reads": sum(len(r.get("reads") or []) for r in rows),
         "reads_parsed": sum(1 for r in rows for x in r.get("reads") or [] if x.get("parsed")),
         "seal_failures": sum(1 for r in rows for x in r.get("reads") or [] if x.get("seal_error")),
         "seal_p95_s": ghost_week._quantile([float(x["seal_s"]) for r in rows for x in r.get("reads") or []
                                             if x.get("seal_s") is not None], 0.95),
         "unread_before": sum(r.get("unread_before") or 0 for r in rows),
         "unread_after": sum(r.get("unread_after") or 0 for r in rows),
         "with_brief": sum(1 for r in rows if r.get("brief_on_row")),
         "with_ledger_row": sum(1 for r in rows if r.get("ledger_rows")),
         "redone": sum(r.get("redone") or 0 for r in rows),
         "verdict_sealed": cnt(judged, lambda r: r.get("verdict_sealed")),
         "verdict_brief": cnt(judged, lambda r: r.get("verdict_brief")),
         "verdict_brief_when_row_exists": cnt([r for r in judged if r.get("brief_on_row")],
                                              lambda r: r.get("verdict_brief")),
         "missing_record_by_category": cnt([x for r in judged for x in r.get("missing_record") or []],
                                           lambda x: x.get("category")),
         "missing_brief_by_category": cnt([x for r in judged for x in r.get("missing_brief") or []],
                                          lambda x: x.get("category")),
         "errors_by_field": cnt([e for r in judged for e in r.get("record_errors") or []], lambda e: e.get("field")),
         "exchanges_with_critical_error": sum(1 for r in judged if r.get("critical_errors")),
         "replayed": sum(1 for r in rows if r.get("replay_ok")),
         "compare": cnt([r for r in rows if r.get("compare")], lambda r: r.get("compare")),
         "judge_cost_usd": round(sum(float(r.get("cost_usd") or 0) + float(r.get("replay_cost_usd") or 0)
                                     + float(r.get("compare_cost_usd") or 0) for r in rows), 3)}
    m["blocked_share_sealed"] = ghost_week._pct(m["verdict_sealed"].get("blocked", 0), len(judged))
    m["blocked_share_brief"] = ghost_week._pct(m["verdict_brief"].get("blocked", 0), len(judged))
    lines = [f"# Ghost backtest {stamp}", "",
             f"{collected} exchanges with a mail read in the last {days} days of the EA's transcripts; "
             f"{len(rows)} with a thread or message read selected (newest first); {len(judged)} judged, "
             f"{m['errors']} judge errors. Sealing: {m['reads_parsed']} of {m['reads']} reads parsable, "
             f"{m['seal_failures']} failures, p95 {ghost_week._fmt(m['seal_p95_s'])} s; the remote model had not "
             f"read {m['unread_before']} messages, {m['unread_after']} stayed unread.", "",
             "## Sealed mail (the record in place of the text)", "",
             f"- Verdicts: {json.dumps(m['verdict_sealed'])} (blocked {ghost_week._fmt(m['blocked_share_sealed'], True)})",
             f"- Facts missing from the record, by kind: {json.dumps(m['missing_record_by_category'])}",
             f"- Record errors by field: {json.dumps(m['errors_by_field'])}; exchanges with a critical error: "
             f"{m['exchanges_with_critical_error']}",
             "", "### Enhancements for the record, ranked", ""]
    lines += [f"- {ln}" for ln in ghost_week._rank_lines(
        [{"line": r.get("enhancement_record")} for r in judged if r.get("enhancement_record")])] or ["- (none)"]
    lines += ["", "## The thread brief alone", "",
              f"- Threads with records in the store (a brief could be composed): {m['with_brief']} of {len(rows)}; "
              f"with a ledger row today: {m['with_ledger_row']}",
              f"- Verdicts, all exchanges: {json.dumps(m['verdict_brief'])} (blocked "
              f"{ghost_week._fmt(m['blocked_share_brief'], True)})",
              f"- Verdicts where a brief existed: {json.dumps(m['verdict_brief_when_row_exists'])}",
              f"- Facts missing from the brief, by kind: {json.dumps(m['missing_brief_by_category'])}",
              "", "### Changes the ledger needs, ranked", ""]
    lines += [f"- {ln}" for ln in ghost_week._rank_lines(
        [{"line": r.get("enhancement_brief")} for r in judged if r.get("enhancement_brief")])] or ["- (none)"]
    if m["replayed"]:
        lines += ["", "## Replay A/B (the turn run again on sealed inputs, no tools)", "",
                  f"- Replayed {m['replayed']}; compared {json.dumps(m['compare'])}"]
        for r in rows:
            if r.get("compare"):
                lines.append(f"- {r['ts'][:16]} {r['compare']}: {r.get('note', '')}"
                             + (f" Missing: {'; '.join(r['missing_in_b'])}" if r.get("missing_in_b") else "")
                             + (f" Wrong: {'; '.join(r['wrong_in_b'])}" if r.get("wrong_in_b") else ""))
    lines += ["", "## Exchanges", ""]
    for r in rows:
        lines.append(f"- {r['ts'][:16]} [{r.get('verdict_sealed', 'error')}/{r.get('verdict_brief', '-')}"
                     f"{', row' if r.get('brief_on_row') else ', no row'}] "
                     f"{str(r.get('user') or '')[:90].replace(chr(10), ' ')}"
                     + (f" | missing: {'; '.join(x['fact'] for x in r.get('missing_brief') or [])[:200]}"
                        if r.get("missing_brief") else "")
                     + (f" | error: {r['error']}" if r.get("error") else ""))
    lines += ["", f"Judge and replay cost: ${m['judge_cost_usd']}.", ""]
    m["markdown"] = "\n".join(lines)
    return m


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("collect")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p = sub.add_parser("run")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--max", type=int, default=DEFAULT_MAX)
    p.add_argument("--replay", type=int, default=0)
    p.add_argument("--model", default=REPLAY_MODEL)
    p.add_argument("--budget", type=float, default=None, help="seconds for inline reads per sealed read")
    p.add_argument("--stamp", default=None, help="resume the run with this stamp")
    p.add_argument("--redo", action="store_true", help="extract every message in the reads again first")
    p = sub.add_parser("report")
    p.add_argument("stamp")
    a = ap.parse_args(argv)
    if not _owner_trust.is_owner_process():
        print("ghost_backtest runs in the owner's processes only", file=sys.stderr)
        return 2
    if a.cmd == "collect":
        ex = collect(a.days)
        verbs = ghost_week._count([r for e in ex for r in e["reads"]], lambda r: r["verb"])
        print(json.dumps({"exchanges": len(ex), "reads_by_verb": verbs,
                          "with_thread_read": len(_select(ex, 10_000)),
                          "with_drafts": sum(1 for e in ex if e.get("drafts"))}))
        return 0
    if a.cmd == "run":
        rep = run(days=a.days, max_n=a.max, replay=a.replay, model=a.model, budget_s=a.budget,
                  stamp=a.stamp, redo=a.redo, progress=lambda s: print(s, file=sys.stderr, flush=True))
        print(rep["markdown"])
        print(f"\n(written to {rep['path']})", file=sys.stderr)
        return 0
    path = remote_llm.store_dir() / BACKTEST_DIR / a.stamp / "report.md"
    print(path.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
