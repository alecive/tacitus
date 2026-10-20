#!/usr/bin/env python3
"""Prompt evolution for the scheduling pipeline, offline, on the remote endpoint.

The pipeline's model steps (sched_pipeline: intent, pick) are judged by
code, so a prompt can be scored without anyone reading the answers. This
harness replays past scheduling-channel turns through those steps with
several prompt variants in parallel, scores each variant against a
reference, and keeps the best.

  corpus    the fresh scheduling-channel turns of the last N days
            (arm_backtest.collect), each with its request, the thread as
            it stood at the turn (messages after the turn dropped), and
            what Claude did (its reply, drafts and tool commands).
  label     the reference intent for each turn, from the judge model
            (Claude, tool-less) reading the request, the thread and what
            Claude did on the turn. Written once; `--redo` relabels.
  eval      one prompt set over the corpus: the intent step's agreement
            with the labels, field by field; for turns the label calls a
            proposal, the solver runs (today's calendar) and the pick
            step's code checks are counted. Variants run in parallel up
            to the endpoint's slots.
  evolve    G generations: the judge model proposes K rewrites of the
            current best intent prompt from the mismatch table, each is
            evaluated, the best survives. The winner goes to
            <store>/tuning/sched-pipeline-prompts.json, which the pipeline
            loads; every generation is recorded in sched-evolve.jsonl.

Scores are code's: a field agrees or it does not. The judge model writes
labels and proposes prompts; it never grades.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remote_llm as _rl  # noqa: E402
import sched_pipeline as sp  # noqa: E402

TUNING = Path("tuning")
CORPUS = TUNING / "sched-corpus.jsonl"
LABELS = TUNING / "sched-labels.jsonl"
HISTORY = TUNING / "sched-evolve.jsonl"
PROMPTS = sp.PROMPTS_FILE
JUDGE_MODEL = "opus"
DEFAULT_DAYS = 45
FIELD_WEIGHTS = {"mode": 3, "sender_email": 2, "window_start": 1, "window_end": 1, "sender_timezone": 1,
                 "duration_minutes": 1, "preferred_days": 1, "excluded_days": 1, "offered_times": 1,
                 "other_attendees": 1}
PLACEHOLDERS = ("{user_name}", "{user_email}", "{user_first}", "{user_zone}", "{today}", "{weeks}")

LABEL_SUFFIX = """

For reference, this is what the assistant did on that turn (its reply to the user, any draft it wrote, the tools it ran). Use it to resolve what the request meant; the intent must describe the request as it stood, not the reply.

ASSISTANT'S REPLY:
{reply}

DRAFTS:
{drafts}

TOOLS RUN:
{tools}"""

MUTATE_PROMPT = """You improve the system prompt of a small on-prem model that turns a scheduling request plus an email thread into a JSON intent. The prompt below is scored by code against reference intents: a field agrees or it does not. Rewrite it {k} different ways so the model gets the fields right more often. Keep every placeholder exactly as written: {placeholders}. Keep the field list and the JSON answer rule. You may add examples, reorder, sharpen definitions, or add rules for the mistakes listed. Each rewrite must be a complete prompt under 2800 characters.

CURRENT PROMPT:
<<<
{prompt}
>>>

CURRENT SCORE: {score:.3f} over {n} turns.

MISTAKES (field, how often, examples of label vs the model's answer, with the request):
{mistakes}

Answer with one JSON object and nothing else: {{"prompts": ["...", "..."]}} with exactly {k} strings."""


def _store() -> Path:
    return _rl.store_dir()


def _rows(name: Path) -> list[dict]:
    path = _store() / name
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _write_rows(name: Path, rows: list[dict]) -> None:
    path = _store() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))


def _append(name: Path, row: dict) -> None:
    path = _store() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")


# ── corpus ────────────────────────────────────────────────────────────

_QUOTED = re.compile(r"^>\s?(.*)$", re.M)
_CONV_ID = re.compile(r"--conversation-id\s+[\"']?([A-Za-z0-9+/=_-]{20,})")
_QUERY = re.compile(r"--query\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))")


def request_text(user: str) -> str:
    """The user's own words in a lane prompt: the quoted lines of the
    task block, else the tail of the prompt."""
    import arm_backtest as ab
    tail = ab._request_tail(user)
    quoted = [m.group(1) for m in _QUOTED.finditer(tail)]
    if quoted:
        return "\n".join(quoted).strip()
    return tail.strip()[-600:]


def conversation_from_reads(reads: list[dict]) -> str:
    for r in reads or []:
        m = _CONV_ID.search(str(r.get("cmd") or ""))
        if m:
            return m.group(1)
    return ""


def build_corpus(days: int = DEFAULT_DAYS, *, collector=None, runner=None, llm=None,
                 user: dict | None = None) -> list[dict]:
    """The corpus rows, written to the store. Each: ts, session, request,
    conversation_id, messages (frozen at ts), claude {reply, drafts,
    tools}. A turn whose thread cannot be found is kept with no
    messages and skipped by eval."""
    import arm_backtest as ab
    collector = collector or ab.collect
    user = user or sp.user_profile()
    prompts = sp.load_prompts()
    rows = []
    for ex in collector(days):
        req = request_text(str(ex.get("user") or ""))
        ts = str(ex.get("ts") or "")
        rid = f"corpus-{str(ex.get('session') or '')[:8]}"
        cid = conversation_from_reads(ex.get("reads") or [])
        why = "from Claude's reads" if cid else ""
        if not cid:
            try:
                cid, detail = sp.locate_thread(req, run_id=rid, runner=runner, llm=llm, user=user, prompts=prompts)
                why = detail.get("why", "")
            except Exception as e:
                cid, why = "", f"locate failed: {str(e)[:120]}"
        messages = []
        if cid:
            parsed, _err = sp.read_tool("get-conversation", {"conversation_id": cid, "max_chars": sp.THREAD_CHARS}, rid, runner)
            messages = (parsed.get("messages") if isinstance(parsed, dict) else parsed) or []
            messages = [m for m in messages if str(m.get("sent") or m.get("received") or "") <= ts]
        rows.append({"ts": ts, "session": ex.get("session"), "request": req, "conversation_id": cid,
                     "located": why, "messages": messages,
                     "claude": {"reply": str(ex.get("reply") or "")[:3000],
                                "drafts": [str(d)[:2000] for d in ex.get("drafts") or []][:3],
                                "tools": [str(r.get("cmd") or r.get("verb") or "")[:300] for r in ex.get("reads") or []][:12]}})
    _write_rows(CORPUS, rows)
    return rows


# ── labels ────────────────────────────────────────────────────────────

def _judge_json(prompt: str, schema: dict) -> dict:
    import ghost_week
    obj, _meta = ghost_week._judge(prompt, schema, judge="claude")
    return obj


def label_turn(row: dict, *, user: dict, judge=None) -> dict:
    """The reference intent for one corpus row, from the judge model."""
    judge = judge or _judge_json
    today = date.fromisoformat(row["ts"][:10])
    system = sp.INTENT_PROMPT.format(
        user_name=user["name"], user_email=user["email"], user_first=user["first"],
        user_zone=user["zone"], today=today.strftime("%A %Y-%m-%d"), weeks=sp.weeks_table(today))
    c = row.get("claude") or {}
    suffix = LABEL_SUFFIX.format(reply=c.get("reply") or "(none)", drafts="\n---\n".join(c.get("drafts") or []) or "(none)",
                                 tools="\n".join(c.get("tools") or []) or "(none)")
    content = (f"REQUEST from {user['first']}: {row['request']}\n\nTHREAD (oldest first):\n"
               f"{sp.thread_text(row.get('messages') or [])}{suffix}")
    obj = judge(system + "\n\n" + content, sp.INTENT_SCHEMA)
    return sp.normalize_intent(obj, row.get("messages") or [], user)


def build_labels(*, redo: bool = False, user: dict | None = None, judge=None) -> list[dict]:
    user = user or sp.user_profile()
    have = {} if redo else {r["session"]: r for r in _rows(LABELS)}
    out = []
    for row in _rows(CORPUS):
        if not row.get("messages"):
            continue
        if row["session"] in have:
            out.append(have[row["session"]])
            continue
        try:
            intent = label_turn(row, user=user, judge=judge)
            out.append({"session": row["session"], "ts": row["ts"], "intent": intent})
        except Exception as e:
            out.append({"session": row["session"], "ts": row["ts"], "error": str(e)[:200]})
    _write_rows(LABELS, out)
    return out


# ── scoring ───────────────────────────────────────────────────────────

def _norm(k: str, v):
    if isinstance(v, list):
        return sorted(str(x)[:16] for x in v)
    if k == "sender_timezone":
        return {"US/Eastern": "America/New_York", "America/Detroit": "America/New_York"}.get(str(v), str(v))
    return v


def score_intent(got: dict, ref: dict) -> tuple[float, dict]:
    """(weighted agreement in [0, 1], {field: (ref, got)} for the misses)."""
    total, agree, misses = 0, 0, {}
    for k, w in FIELD_WEIGHTS.items():
        if ref.get("mode") == "other" and k not in ("mode", "sender_email"):
            continue   # an 'other' label has no scheduling fields to match
        total += w
        if _norm(k, got.get(k)) == _norm(k, ref.get(k)):
            agree += w
        else:
            misses[k] = (ref.get(k), got.get(k))
    return (agree / total if total else 1.0), misses


def eval_turn(row: dict, label: dict, prompts: dict, *, user: dict, llm=None, runner=None,
              with_pick: bool = True) -> dict:
    today = date.fromisoformat(row["ts"][:10])
    out = {"session": row["session"], "request": row["request"][:160]}
    try:
        intent, meta = sp.extract_intent(row["request"], row["messages"], today=today, user=user, prompts=prompts, llm=llm)
    except Exception as e:
        out.update(score=0.0, error=str(e)[:200])
        return out
    score, misses = score_intent(intent, label["intent"])
    out.update(score=round(score, 3), misses=misses, intent_mode=intent.get("mode"), latency_ms=meta.get("latency_ms"))
    if with_pick and label["intent"].get("mode") == "propose" and intent.get("mode") == "propose":
        try:
            tool, args = sp.solver_args(intent, today=date.today(), user_email=user["email"])
            if tool == "propose-meeting":
                # the window as of the turn is in the past; shift it to the same weekdays ahead
                args = _shift_window(args)
            res, err = sp.read_tool(tool, args, f"evolve-{row['session'][:8]}", runner)
            options = [o for o in (res or {}).get("options") or [] if o.get("id")] if isinstance(res, dict) else []
            if options:
                pick, _m = sp.pick_options(intent, options, row["request"], today=date.today(), user=user, prompts=prompts, llm=llm)
                problems, ids = sp.check_pick(pick, intent, options)
                out.update(pick_problems=problems, picked=ids, options=len(options))
            else:
                out.update(pick_problems=["no-options"], options=0, solver_error=(err or "")[:120])
        except Exception as e:
            out.update(pick_problems=["pick-failed"], pick_error=str(e)[:200])
    return out


def _shift_window(args: dict) -> dict:
    """A past window moved forward by whole weeks until it starts after
    today, so the solver has a calendar to read; preferred and excluded
    days in the attendee availability move with it."""
    from datetime import timedelta
    start = date.fromisoformat(args["earliest_date"])
    end = date.fromisoformat(args["latest_date"])
    shift = 0
    while start + timedelta(days=shift) <= date.today():
        shift += 7
    if not shift:
        return args
    d = timedelta(days=shift)
    args = dict(args, earliest_date=(start + d).isoformat(), latest_date=(end + d).isoformat())
    atts = []
    for a in args.get("attendees") or []:
        a = dict(a)
        if a.get("availability"):
            a["availability"] = [dict(w, day=(date.fromisoformat(w["day"]) + d).isoformat()) for w in a["availability"]]
        atts.append(a)
    args["attendees"] = atts
    return args


def evaluate(prompts: dict, *, parallel: int | None = None, user: dict | None = None, llm=None,
             runner=None, with_pick: bool = True, limit: int = 0) -> dict:
    """One prompt set over the corpus; the summary with per-turn rows."""
    user = user or sp.user_profile()
    labels = {r["session"]: r for r in _rows(LABELS) if r.get("intent")}
    rows = [r for r in _rows(CORPUS) if r.get("messages") and r["session"] in labels]
    if limit:
        rows = rows[:limit]
    parallel = parallel or _rl.max_parallel()
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        results = list(ex.map(lambda r: eval_turn(r, labels[r["session"]], prompts, user=user, llm=llm,
                                                  runner=runner, with_pick=with_pick), rows))
    scores = [r["score"] for r in results]
    picks = [r for r in results if "pick_problems" in r]
    misses = {}
    for r in results:
        for k in (r.get("misses") or {}):
            misses[k] = misses.get(k, 0) + 1
    return {"name": prompts.get("name", "default"), "n": len(results),
            "score": round(sum(scores) / len(scores), 4) if scores else 0.0,
            "perfect": sum(1 for s in scores if s >= 0.999),
            "misses": dict(sorted(misses.items(), key=lambda kv: -kv[1])),
            "pick_turns": len(picks), "pick_clean": sum(1 for r in picks if not r["pick_problems"]),
            "pick_problems": _count([p for r in picks for p in r["pick_problems"]]),
            "seconds": round(time.monotonic() - t0, 1), "rows": results}


def _count(items: list) -> dict:
    out = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def summary_text(rep: dict) -> str:
    return (f"{rep['name']}: intent agreement {rep['score']:.3f} over {rep['n']} turns "
            f"({rep['perfect']} exact); misses {rep['misses'] or 'none'}; "
            f"pick step clean {rep['pick_clean']}/{rep['pick_turns']} ({rep['pick_problems'] or 'no problems'}); "
            f"{rep['seconds']}s")


# ── evolution ─────────────────────────────────────────────────────────

def mistakes_table(rep: dict, labels: dict, corpus: dict, limit: int = 12) -> str:
    by_field = {}
    for r in rep["rows"]:
        for k, (ref, got) in (r.get("misses") or {}).items():
            by_field.setdefault(k, []).append((r["session"], ref, got))
    lines = []
    for k, items in sorted(by_field.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- {k}: {len(items)} of {rep['n']} turns")
        for session, ref, got in items[:3]:
            req = corpus.get(session, {}).get("request", "")[:140].replace("\n", " ")
            lines.append(f"    label {json.dumps(ref, default=str)[:80]} | model {json.dumps(got, default=str)[:80]} | request: {req!r}")
        if len(lines) > limit * 4:
            break
    return "\n".join(lines) or "(none)"


def propose_prompts(current: str, rep: dict, k: int, *, judge=None) -> list[str]:
    judge = judge or _judge_json
    labels = {r["session"]: r for r in _rows(LABELS)}
    corpus = {r["session"]: r for r in _rows(CORPUS)}
    prompt = MUTATE_PROMPT.format(k=k, placeholders=", ".join(PLACEHOLDERS), prompt=current,
                                  score=rep["score"], n=rep["n"], mistakes=mistakes_table(rep, labels, corpus))
    obj = judge(prompt, {"type": "object", "properties": {"prompts": {"type": "array", "items": {"type": "string"}}},
                         "required": ["prompts"]})
    out = []
    for p in obj.get("prompts") or []:
        p = str(p).strip()
        if all(ph in p for ph in PLACEHOLDERS) and 200 < len(p) <= 4000:
            try:
                p.format(user_name="", user_email="", user_first="", user_zone="", today="", weeks="")
            except (KeyError, ValueError, IndexError):
                continue
            out.append(p)
    return out[:k]


def evolve(generations: int = 3, children: int = 4, *, parallel: int | None = None, user: dict | None = None,
           llm=None, runner=None, judge=None, with_pick: bool = False, start: dict | None = None) -> dict:
    """Keep the best intent prompt over G generations of K rewrites each.
    Returns the winner's summary; writes the prompts file and the history."""
    user = user or sp.user_profile()
    best_prompts = dict(start or sp.load_prompts())
    best = evaluate(best_prompts, parallel=parallel, user=user, llm=llm, runner=runner, with_pick=with_pick)
    _append(HISTORY, {"ts": datetime.now().isoformat(timespec="seconds"), "gen": 0, "name": best["name"],
                      "score": best["score"], "perfect": best["perfect"], "misses": best["misses"]})
    print(summary_text(best), flush=True)
    for g in range(1, generations + 1):
        candidates = propose_prompts(best_prompts["intent"], best, children, judge=judge)
        if not candidates:
            print(f"gen {g}: no usable rewrite", flush=True)
            continue
        variants = [dict(best_prompts, intent=c, name=f"gen{g}-{i + 1}") for i, c in enumerate(candidates)]
        with ThreadPoolExecutor(max_workers=len(variants)) as ex:
            reports = list(ex.map(lambda v: evaluate(v, parallel=max(1, (parallel or _rl.max_parallel()) // len(variants)),
                                                      user=user, llm=llm, runner=runner, with_pick=with_pick), variants))
        for v, rep in zip(variants, reports):
            _append(HISTORY, {"ts": datetime.now().isoformat(timespec="seconds"), "gen": g, "name": v["name"],
                              "score": rep["score"], "perfect": rep["perfect"], "misses": rep["misses"],
                              "prompt": v["intent"]})
            print(summary_text(rep), flush=True)
        winner_i = max(range(len(reports)), key=lambda i: (reports[i]["score"], reports[i]["perfect"], -len(variants[i]["intent"])))
        if (reports[winner_i]["score"], reports[winner_i]["perfect"]) > (best["score"], best["perfect"]):
            best, best_prompts = reports[winner_i], variants[winner_i]
            print(f"gen {g}: {best['name']} takes the lead at {best['score']:.3f}", flush=True)
        else:
            print(f"gen {g}: no child beat {best['name']} ({best['score']:.3f})", flush=True)
    path = _store() / PROMPTS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": best_prompts.get("name", "default"), "intent": best_prompts["intent"],
                                "pick": best_prompts["pick"], "locate": best_prompts["locate"],
                                "score": best["score"], "n": best["n"],
                                "written": datetime.now().isoformat(timespec="seconds")}, indent=1))
    best.pop("rows", None)
    return best


# ── CLI ───────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("corpus")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p = sub.add_parser("label")
    p.add_argument("--redo", action="store_true")
    p = sub.add_parser("eval")
    p.add_argument("--prompts", default="", help="a prompts JSON file; default: the tuned set or the defaults")
    p.add_argument("--parallel", type=int, default=0)
    p.add_argument("--no-pick", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--rows", action="store_true", help="print every turn's row")
    p = sub.add_parser("evolve")
    p.add_argument("--generations", type=int, default=3)
    p.add_argument("--children", type=int, default=4)
    p.add_argument("--parallel", type=int, default=0)
    p.add_argument("--with-pick", action="store_true")
    p.add_argument("--from-defaults", action="store_true", help="start from the built-in prompts, not the tuned file")
    a = ap.parse_args(argv)
    if a.cmd == "corpus":
        rows = build_corpus(a.days)
        with_thread = sum(1 for r in rows if r["messages"])
        print(f"{len(rows)} turns, {with_thread} with a thread, written to {_store() / CORPUS}")
        for r in rows:
            print(f"  {r['ts'][:10]} {'thread' if r['messages'] else 'NO THREAD'} {len(r['messages']):2d} msgs  {r['request'][:90]!r}")
        return 0
    if a.cmd == "label":
        rows = build_labels(redo=a.redo)
        ok = [r for r in rows if r.get("intent")]
        print(f"{len(ok)} labels ({len(rows) - len(ok)} failed): modes {_count([r['intent']['mode'] for r in ok])}")
        return 0
    if a.cmd == "eval":
        prompts = sp.load_prompts()
        if a.prompts:
            d = json.loads(Path(a.prompts).read_text())
            prompts.update({k: d[k] for k in ("intent", "pick", "locate", "name") if k in d})
        rep = evaluate(prompts, parallel=a.parallel or None, with_pick=not a.no_pick, limit=a.limit)
        print(summary_text(rep))
        if a.rows:
            for r in rep["rows"]:
                print(f"  {r['score']:.2f} {r.get('intent_mode', '?'):9s} {r.get('pick_problems', '')} {r['request'][:80]!r} misses={list((r.get('misses') or {}).keys())}")
        return 0
    if a.cmd == "evolve":
        start = {"intent": sp.INTENT_PROMPT, "pick": sp.PICK_PROMPT, "locate": sp.LOCATE_PROMPT, "name": "default"} if a.from_defaults else None
        best = evolve(a.generations, a.children, parallel=a.parallel or None, with_pick=a.with_pick, start=start)
        print(f"winner: {best['name']} at {best['score']:.3f}; written to {_store() / PROMPTS}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
