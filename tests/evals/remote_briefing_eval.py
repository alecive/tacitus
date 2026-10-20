#!/usr/bin/env python3
"""The morning briefing's staleness evals, on the remote model.

The Claude evals (promptfooconfig.yaml) hand a fixture to the briefing
skill. Since the remote model writes the briefings (remote_llm `briefings:
remote`), the same fixtures go through remote_briefing.run here, on the
model in effect, and the post is held to the same checks:

  completed-with-deadline  a completed row with a past deadline is never
                           called overdue (regression-001)
  already-replied-thread   Jo's FYI in a thread he answered is not an
                           action; Harper's new question is (regression-002)
  empty-calm-day           a calm day stays short, invents no urgency, and
                           leaves the snoozed Acme item out (hard-success-003)

Every post is also checked for its frame: the morning header, the do-list,
and the remote model's footer. The fixtures are moved to this week
(provider.rebase_fixture) and put in the preprocessor's shape (normalize).
Nothing is written: dry run, no drafts, no hand-offs, and the pre-send
audit is left out (it reads the live ledger and mail).

  remote_briefing_eval.py              run every case on the remote model
  remote_briefing_eval.py --stub       a stand-in model that picks every ref
                                       (the code's guards alone; the cases a
                                       model must judge are left out)
  remote_briefing_eval.py --json       one JSON report on stdout

Exit 0 when every check passes, 1 when one fails, 2 when the remote model
could not be reached (the post fell back to the ranked queue).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
sys.path.insert(0, str(HERE))

FIXTURES = HERE / "fixtures"
CASES = ("completed-with-deadline", "already-replied-thread", "empty-calm-day")
# Which of Jo's and Harper's messages asks something new is the model's
# call; a stand-in that picks every ref cannot be held to it.
MODEL_JUDGED = ("already-replied-thread",)
MAIL_KEYS = ("new_needs_response", "new_dropped_threads", "pending_rsvps")
SECTIONS = {"get_done": ":dart:", "flexes": ":leaves:", "drafts": ":envelope:",
            "needs_you": ":question:", "escalated": ":arrow_up:"}


# ── the fixture, in the preprocessor's shape ─────────────────────────

def _mail(it: dict) -> dict:
    out = dict(it)
    frm = it.get("from")
    if isinstance(frm, dict):
        ea = frm.get("emailAddress") or {}
        out["from"] = ea.get("address") or ""
        out["from_name"] = it.get("from_name") or ea.get("name") or ""
    out["body_preview"] = it.get("body_preview") or it.get("bodyPreview") or ""
    out["conversation_id"] = (it.get("conversation_id") or it.get("conversationId")
                              or f"conv-{it.get('message_id') or ''}")
    out.setdefault("subject", it.get("subject") or "")
    return out


def normalize(fixture: dict) -> dict:
    """The fixture's briefing as preprocess.py writes it today: mail items
    with from/from_name/body_preview/conversation_id, the ledger's open
    rows, the week's closed rows, and today_top ranked by code."""
    from preprocess import build_today_top
    b = json.loads(json.dumps(fixture.get("briefing") or {}))
    for key in MAIL_KEYS:
        b[key] = [_mail(it) for it in b.get(key) or []]
    ledger = fixture.get("ledger_entries") or []
    opens = {o.get("id"): o for o in b.get("open_obligations") or []}
    for e in ledger:
        if e.get("status") in ("open", "follow_up", "snoozed") and e.get("id") not in opens:
            opens[e["id"]] = e
    b["open_obligations"] = list(opens.values())
    b.setdefault("recently_closed", [
        {"id": e.get("id"), "description": (e.get("description") or "")[:120],
         "closed_at": e.get("closed_at") or e.get("updated_at") or "",
         "conversation_id": e.get("conversation_id") or ""}
        for e in ledger if e.get("status") in ("completed", "closed")])
    if "today_top" not in b:
        b["today_top"], b["everything_else"] = build_today_top(
            b.get("new_needs_response") or [], b.get("new_dropped_threads") or [],
            b.get("deadline_items") or [], b.get("pending_rsvps") or [],
            b["open_obligations"], [])
    return b


def load(name: str) -> tuple[dict, dict, datetime]:
    """(the fixture moved to this week, its briefing normalized, its run time)."""
    import provider
    raw = json.loads((FIXTURES / f"{name}.json").read_text())
    moved, gen = provider.rebase_fixture(raw)
    now = datetime.fromisoformat(gen.replace("Z", "+00:00")) if gen else datetime.now(timezone.utc)
    return moved, normalize(moved), now


# ── the run ──────────────────────────────────────────────────────────

def _stub_chat(messages, **kw):
    """A stand-in model that puts every candidate everywhere it may go:
    what reaches the post is then what code let through."""
    refs = re.findall(r'"ref": "([A-Z]\d+)"', messages[-1]["content"])
    pick = [{"ref": r, "when": "first thing", "why": "stub"} for r in refs]
    return ({"get_done_today": pick[:5], "flexes": [], "drafts": [],
             "needs_you": [{"ref": r, "decision": "stub"} for r in refs],
             "escalate": []}, {"latency_ms": 0})


def run_case(name: str, *, chat=None) -> dict:
    import remote_briefing as rb
    rb._profile = lambda: "The user is a university professor (robotics)."
    rb._rules = lambda: ""
    fixture, briefing, now = load(name)
    post, rec = rb.run("morning-briefing", briefing, run_id=f"eval-{name}", dry_run=True,
                       now=now, chat=chat, audit=lambda *a, **k: {})
    checks = frame(post) + CHECKS[name](post)
    return {"case": name, "fixture_id": (fixture.get("_meta") or {}).get("id", ""),
            "fallback": rec.get("fallback", ""), "passed": all(c["ok"] for c in checks),
            "checks": checks, "post": post}


# ── the checks ───────────────────────────────────────────────────────

def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail}


def sections(post: str) -> dict[str, list[str]]:
    """The post's item lines, by section."""
    out: dict[str, list[str]] = {k: [] for k in SECTIONS}
    cur = ""
    for line in post.splitlines():
        hit = next((k for k, mark in SECTIONS.items() if line.startswith(mark)), None)
        if hit:
            cur = hit
            continue
        if not line.strip() or line.startswith(":"):
            cur = "" if not line.strip() else cur
            continue
        if cur:
            out[cur].append(line)
    return out


def frame(post: str) -> list[dict]:
    return [_check("morning header", post.startswith(":sunny: *Morning Briefing")),
            _check("do-list present", ":dart: *Get done today*" in post),
            _check("remote footer", post.rstrip().endswith("_written by the remote model_"))]


def _completed_with_deadline(post: str) -> list[dict]:
    lines = [ln for ln in post.splitlines() if "overdue" in ln.lower()]
    return [_check("no item called overdue", not lines, "; ".join(lines)[:300]),
            _check("the completed outreach is not an action",
                   not any("curriculum" in ln.lower()
                           for sec in sections(post).values() for ln in sec))]


def _already_replied(post: str) -> list[dict]:
    s = sections(post)
    acting = s["get_done"] + s["drafts"] + s["needs_you"] + s["escalated"]
    jo = [ln for ln in acting if "Jo Carrow" in ln or "Lab Meeting Agenda" in ln]
    harper = [ln for ln in acting if "Harper" in ln or "Travel Budget" in ln]
    return [_check("Jo's FYI is not an action", not jo, "; ".join(jo)[:300]),
            _check("Harper's new question is raised", bool(harper))]


def _calm_day(post: str) -> list[dict]:
    words = len(post.split())
    urgent = [w for w in ("URGENT", "OVERDUE", "DUE SOON", "ASAP", ":rotating_light:")
              if w in post]
    return [_check("short (under 300 words)", words < 300, f"{words} words"),
            _check("no invented urgency", not urgent, ", ".join(urgent)),
            _check("the snoozed Acme item stays out", "Acme" not in post),
            _check("the day's calendar is there", bool(re.search(r"\bEli\b", post)))]


CHECKS = {"completed-with-deadline": _completed_with_deadline,
          "already-replied-thread": _already_replied,
          "empty-calm-day": _calm_day}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stub", action="store_true", help="a stand-in model that picks every ref")
    ap.add_argument("--case", action="append", choices=CASES, help="run one case (repeatable)")
    ap.add_argument("--json", action="store_true", help="one JSON report on stdout")
    a = ap.parse_args(argv)
    chat = _stub_chat if a.stub else None
    cases = a.case or [c for c in CASES if not (a.stub and c in MODEL_JUDGED)]
    results = [run_case(c, chat=chat) for c in cases]
    if a.json:
        print(json.dumps({"model": "stub" if a.stub else "remote", "results": results}, indent=2))
    else:
        for r in results:
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"{mark}  {r['case']} ({r['fixture_id']})"
                  + (f"  [fallback: {r['fallback'][:80]}]" if r["fallback"] else ""))
            for c in r["checks"]:
                if not c["ok"]:
                    print(f"      x {c['check']}: {c['detail']}")
    if any(r["fallback"] for r in results) and not a.stub:
        return 2
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
