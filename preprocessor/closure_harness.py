"""Precision AND recall harness for the closure scrubber.

`closure_metrics.backtest` replays the production LOGS through a
parameterised rule overlay, which answers a precision question ("how many
recorded closures would a stricter rule have suppressed") and a recall
question about one knob ("which recorded misses would a looser sender
list have caught"). What it cannot do is run a scrubber build, so a
change that closed NOTHING would score perfectly there.

This harness executes `scrub_closures` itself, once per labeled case in
a corpus, and reports both directions. The committed corpus,
tests/fixtures/closure_corpus.json, is invented data written by
tests/fixtures/closure_corpus_synthetic.py. A corpus rebuilt from the
owner's own history (scripts/build_closure_corpus.py) is owner-private
and never committed: `real_corpus_path()` finds it, `--real` scores it.

    true_closes    should_close  and it closed        (recall numerator)
    missed_closes  should_close  and it did not       (recall miss)
    false_closes   must_not_close and it closed       (precision miss)
    correct_holds  must_not_close and it did not

    precision = true / (true + false)
    recall    = true / (true + missed)

It reuses closure_metrics.signal_of rather than re-parsing reason strings,
and it never touches production state: every run gets a scratch EA_DIR, a
scratch ledger DB and a scratch veto store, and always passes dry_run.

Usage:
    python3 preprocessor/closure_harness.py score [--md] [--json]
    python3 preprocessor/closure_harness.py score --real
    python3 preprocessor/closure_harness.py score --cohort calendar_event_match_false_positive
    python3 preprocessor/closure_harness.py compare --rev 9ca265c --rev b1fe734
    python3 preprocessor/closure_harness.py score --scrubber-dir /tmp/old/preprocessor

`compare --rev` checks out `<rev>:preprocessor/` into a temp directory and
re-runs the harness against it in a subprocess, so several scrubber
generations can be scored in one command without import-cache games.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

REPO = _HERE.parent
CORPUS_PATH = REPO / "tests" / "fixtures" / "closure_corpus.json"

# The corpus rebuilt from the owner's own history. It holds real mail
# subjects and ledger text, so it lives outside the repository: the path
# in REAL_CORPUS_ENV, else REAL_CORPUS_NAME in the owner's private
# directory, else the gitignored copy beside the committed fixture.
REAL_CORPUS_ENV = "EA_CLOSURE_CORPUS_REAL"
REAL_CORPUS_NAME = "closure_corpus.real.json"
REAL_CORPUS_LOCAL = REPO / "tests" / "fixtures" / REAL_CORPUS_NAME


def default_real_corpus_path() -> Path:
    """Where the builder writes the real corpus: the owner's private
    directory (EA_PRIVATE_DIR, default ~/.ea-private)."""
    base = os.environ.get("EA_PRIVATE_DIR") or str(Path.home() / ".ea-private")
    return Path(base) / REAL_CORPUS_NAME


def real_corpus_path() -> Path | None:
    """The owner-private real corpus when one is present, else None."""
    env = os.environ.get(REAL_CORPUS_ENV, "").strip()
    candidates = [Path(env).expanduser()] if env else [
        default_real_corpus_path(), REAL_CORPUS_LOCAL]
    for p in candidates:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


_EMPTY_PROFILE = {"name": "", "email": "", "gmail_accounts": []}


# The profile the corpus was built against, from the corpus's own
# `profile` key. `_user_tokens()` drops the user's own name and
# mail-domain labels from every overlap, so pointing EA_DIR at a scratch
# dir without one would make every case score against a wider token bag
# than production ever uses.
def profile_of(corpus: dict) -> dict:
    prof = corpus.get("profile") if isinstance(corpus, dict) else None
    return prof if isinstance(prof, dict) else dict(_EMPTY_PROFILE)


def corpus_profile(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or CORPUS_PATH).read_text())
    except (OSError, ValueError):
        return dict(_EMPTY_PROFILE)
    return profile_of(data)


# ── corpus ─────────────────────────────────────────────────────────────

def load_corpus(path: Path | None = None) -> dict:
    return json.loads((path or CORPUS_PATH).read_text())


# ── scratch environment ────────────────────────────────────────────────

class Sandbox:
    """A scratch EA_DIR + ledger DB + veto store, torn down on exit.

    Everything the scrubber can write to is redirected here: LOG_PATH and
    the suppressed log derive from EA_DIR, the veto store is passed
    explicitly, and dry_run keeps the ledger UPDATE from running at all.
    Three layers, so no one of them has to be right on its own."""

    def __init__(self, scrubber, profile: dict | None = None):
        self.scrubber = scrubber
        self.tmp = Path(tempfile.mkdtemp(prefix="closure-harness-"))
        (self.tmp / "config").mkdir(parents=True, exist_ok=True)
        (self.tmp / "logs").mkdir(parents=True, exist_ok=True)
        (self.tmp / "state").mkdir(parents=True, exist_ok=True)
        (self.tmp / "config" / "user-profile.json").write_text(
            json.dumps(profile if profile is not None else corpus_profile()))
        self._prev_ea = os.environ.get("EA_DIR")
        os.environ["EA_DIR"] = str(self.tmp)
        # LOG_PATH is bound at import time in every revision, so rebind it.
        self.scrubber.LOG_PATH = self.tmp / "logs" / "closure-scrubber.log"
        self._prev_tokens = getattr(self.scrubber, "_USER_TOKENS", None)
        self.scrubber._USER_TOKENS = None
        self.db = self.tmp / "state" / "ledger.db"
        self._init_db()

    def _init_db(self) -> None:
        import ledger as ledger_mod
        conn = sqlite3.connect(str(self.db))
        conn.executescript(ledger_mod.SCHEMA)
        for migration in getattr(ledger_mod, "MIGRATIONS", []):
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass
        conn.commit()
        conn.close()

    def seed_closed_conversations(self, mapping: dict) -> None:
        """Signal 6 reads closed sibling entries out of the ledger, so a
        case that wants to exercise it declares them in its world."""
        conn = sqlite3.connect(str(self.db))
        for i, (cid, closed_at) in enumerate(mapping.items()):
            conn.execute(
                "INSERT OR REPLACE INTO ledger (id, type, status, "
                "description, conversation_id, closed_at, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"_sibling-{i}", "response_needed", "completed",
                 "closed sibling", cid, closed_at, closed_at, closed_at))
        conn.commit()
        conn.close()

    def seed_closed_entries(self, rows: list) -> None:
        """Signal 10 reads closed rows' descriptions out of the ledger (a
        hand-made row naming manuscript ids closes once every row with
        the same id was closed by the user), so a case that wants to
        exercise it declares those rows in its world."""
        conn = sqlite3.connect(str(self.db))
        for i, row in enumerate(rows):
            closed_at = row.get("closed_at") or ""
            conn.execute(
                "INSERT OR REPLACE INTO ledger (id, type, status, "
                "description, notes, last_action, conversation_id, "
                "closed_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (row.get("id") or f"_closed-{i}",
                 row.get("type") or "response_needed", "completed",
                 row.get("description") or "", row.get("notes") or "",
                 row.get("last_action") or "",
                 row.get("conversation_id") or "",
                 closed_at, row.get("created_at") or closed_at, closed_at))
        conn.commit()
        conn.close()

    def reset_db(self) -> None:
        self.db.unlink(missing_ok=True)
        self._init_db()

    def close(self) -> None:
        if self._prev_ea is None:
            os.environ.pop("EA_DIR", None)
        else:
            os.environ["EA_DIR"] = self._prev_ea
        self.scrubber._USER_TOKENS = self._prev_tokens
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── scoring ────────────────────────────────────────────────────────────

def _call_kwargs(scrubber, candidate: dict) -> dict:
    """Drop kwargs the target revision does not have.

    `scrub_closures` gained kwargs over time, so scoring the trade curve
    means calling several different signatures. Probe rather than
    assume."""
    params = inspect.signature(scrubber.scrub_closures).parameters
    return {k: v for k, v in candidate.items() if k in params}


def run_case(case: dict, ballast: list, scrubber, sandbox: Sandbox) -> dict:
    """Run one case and report whether the case entry closed."""
    world = case.get("world") or {}
    entry = dict(case["entry"])
    # Ballast gives the distinctiveness test a realistic document
    # frequency. Below _RARE_MIN_CORPUS entries it skips the rarity check
    # entirely, so a corpus of one would exercise a path production never
    # takes.
    others = [e for e in ballast if e.get("id") != entry.get("id")]
    sandbox.reset_db()
    closed_convs = world.get("closed_conversations") or {}
    if closed_convs:
        sandbox.seed_closed_conversations(closed_convs)
    closed_rows = world.get("closed_entries") or []
    if closed_rows:
        sandbox.seed_closed_entries(closed_rows)
    veto_path = sandbox.tmp / "state" / "vetoes.json"
    veto_path.unlink(missing_ok=True)

    res = scrubber.scrub_closures(**_call_kwargs(scrubber, {
        "open_entries": [entry] + others,
        "outlook_sent": world.get("sent") or [],
        "outlook_inbox": world.get("inbox") or [],
        "calendar_events": world.get("events") or [],
        "gmail_accounts": [],
        # Raw sent messages per Gmail account; scrub_closures indexes
        # them with the live code path, no API. _call_kwargs drops the
        # kwarg for revisions that predate it, so `compare --rev` still
        # scores the whole trade curve (gmail cases simply cannot fire
        # a hold there, which is itself the honest answer: those
        # revisions had no injectable gmail path to defend).
        "gmail_sent": world.get("gmail_sent") or {},
        # {account: {thread_id: [messages]}} — the thread contents the
        # gmail signal verifies before closing (a reply needs an inbound
        # message to answer). A candidate thread absent from this map
        # reads as unverifiable, and an unverifiable thread never closes.
        "gmail_threads": world.get("gmail_threads") or {},
        "dry_run": True,
        "db_path": sandbox.db,
        "veto_store_path": veto_path,
    }))
    hit = next((c for c in res.get("closures") or []
                if c.get("id") == entry.get("id")), None)
    # A revision before the closure policy closed on every fired signal.
    return {"fired": hit is not None,
            "action": (hit or {}).get("action", "closed") if hit else "",
            "reason": (hit or {}).get("reason", "")}


def score(corpus: dict, scrubber, *, cohort: str = "",
          linkage: str = "") -> dict:
    import closure_metrics as cm

    ballast = corpus.get("ballast") or []
    cases = [c for c in corpus["cases"]
             if (not cohort or c["cohort"] == cohort)
             and (not linkage or c["linkage"] == linkage)]

    sandbox = Sandbox(scrubber, profile_of(corpus))
    results: list[dict] = []
    try:
        for case in cases:
            out = run_case(case, ballast, scrubber, sandbox)
            expected_close = case["label"] == "should_close"
            closed = out["fired"] and out["action"] == "closed"
            proposed = out["fired"] and out["action"] == "proposed"
            if expected_close and closed:
                verdict = "true_close"
            elif expected_close and proposed:
                verdict = "proposed_close"
            elif expected_close:
                verdict = "missed_close"
            elif closed:
                verdict = "false_close"
            elif proposed:
                verdict = "false_proposal"
            else:
                verdict = "correct_hold"
            results.append({
                "case_id": case["case_id"],
                "cohort": case["cohort"],
                "linkage": case["linkage"],
                "label": case["label"],
                # False when no scored scrubber build has ever closed this
                # case, i.e. holding it proves nothing. See cmd_audit.
                "discriminating": case.get("discriminating", True),
                "verdict": verdict,
                "fired_signal": (cm.signal_of(out["reason"])
                                 if out["fired"] else ""),
                "reason": out["reason"][:160],
                "rationale": case.get("rationale", "")[:200],
            })
    finally:
        sandbox.close()

    return _summarise(results, corpus)


def _counts(rows: list[dict]) -> dict:
    """Precision counts closes only: a close the user did not want is the
    failure that loses an obligation. Recall counts a should-close case
    the policy closed or proposed, since a proposal is in front of the user
    with a one-click close; auto_recall counts the closes alone. A false
    proposal is noise on the confirm card, not a lost row."""
    c = {"true_closes": sum(1 for r in rows if r["verdict"] == "true_close"),
         "proposed_closes": sum(1 for r in rows if r["verdict"] == "proposed_close"),
         "missed_closes": sum(1 for r in rows if r["verdict"] == "missed_close"),
         "false_closes": sum(1 for r in rows if r["verdict"] == "false_close"),
         "false_proposals": sum(1 for r in rows if r["verdict"] == "false_proposal"),
         "correct_holds": sum(1 for r in rows if r["verdict"] == "correct_hold")}
    tp, fn, fp = c["true_closes"], c["missed_closes"], c["false_closes"]
    pp = c["proposed_closes"]
    c["precision"] = round(tp / (tp + fp), 4) if (tp + fp) else None
    c["recall"] = round((tp + pp) / (tp + pp + fn), 4) if (tp + pp + fn) else None
    c["auto_recall"] = round(tp / (tp + pp + fn), 4) if (tp + pp + fn) else None
    p, r = c["precision"], c["recall"]
    c["f1"] = round(2 * p * r / (p + r), 4) if p and r else None
    c["n"] = len(rows)
    return c


def _summarise(results: list[dict], corpus: dict) -> dict:
    def group(key: str) -> dict:
        keys = sorted({r[key] for r in results})
        return {k: _counts([r for r in results if r[key] == k]) for k in keys}

    # A must_not_close case no scrubber build has ever closed inflates
    # precision for free, so drop it from the honest headline. A
    # should_close case nothing closes is the OPPOSITE: it is the most
    # important recall case in the corpus (the two id_orphan misses live
    # there), so it always counts.
    disc = [r for r in results
            if r["discriminating"] or r["label"] == "should_close"]
    return {
        "overall": _counts(results),
        "discriminating_only": _counts(disc),
        "vacuous_holds": len(results) - len(disc),
        "by_cohort": group("cohort"),
        "by_linkage": group("linkage"),
        "fired_signal_distribution": _signal_dist(results),
        "failures": [r for r in results
                     if r["verdict"] in ("false_close", "missed_close")],
        "proposals": [r for r in results
                      if r["verdict"] in ("proposed_close", "false_proposal")],
        "results": results,
        "unactionable_count": len(corpus.get("unactionable") or []),
        "contested_count": len(corpus.get("contested") or []),
        "corpus_generated_at": corpus.get("generated_at"),
    }


def _signal_dist(results: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in results:
        if r["fired_signal"]:
            out[r["fired_signal"]] = out.get(r["fired_signal"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ── rendering ──────────────────────────────────────────────────────────

def render_md(rep: dict, label: str = "HEAD") -> str:
    o = rep["overall"]
    lines = [f"*Closure corpus — {label}*",
             f"  cases {o['n']}  |  {_line(o)}",
             f"  true {o['true_closes']}  missed {o['missed_closes']}  "
             f"false {o['false_closes']}  held {o['correct_holds']}"]
    d = rep.get("discriminating_only")
    if d and rep.get("vacuous_holds"):
        lines.append(f"  discriminating only (n={d['n']}, "
                     f"{rep['vacuous_holds']} vacuous holds excluded): "
                     f"precision {_pct(d['precision'])} "
                     f"recall {_pct(d['recall'])}")
    for group in ("by_cohort", "by_linkage"):
        lines.append(f"  {group.replace('_', ' ')}:")
        for k, v in rep[group].items():
            lines.append(f"    {k:42s} n={v['n']:3d} {_line(v)}")
    if rep["failures"]:
        lines.append(f"  failures ({len(rep['failures'])}):")
        for f in rep["failures"][:25]:
            lines.append(f"    [{f['verdict']:12s}] {f['case_id'][:52]:52s} "
                         f"{f['reason'][:60]}")
    lines.append(f"  unactionable (recorded, not scored): "
                 f"{rep['unactionable_count']}")
    lines.append(f"  contested (recorded, not scored): "
                 f"{rep.get('contested_count', 0)}")
    return "\n".join(lines)


def _line(v: dict) -> str:
    """A cohort of only must_not_close cases has no precision to report
    (its numerator is closures that SHOULD have happened, and there are
    none by construction). Report what it actually measures: how many it
    held."""
    if v["true_closes"] == v["missed_closes"] == v["proposed_closes"] == 0:
        return f"held {v['correct_holds']}/{v['n']}"
    if v["false_closes"] == 0 and v["correct_holds"] == 0:
        return (f"recall {_pct(v['recall'])} "
                f"({v['true_closes']} closed, {v['proposed_closes']} proposed "
                f"of {v['n']})")
    return f"precision {_pct(v['precision'])} recall {_pct(v['recall'])}"


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:.0%}"


# ── historical revisions ───────────────────────────────────────────────

def export_rev(rev: str, dest: Path) -> Path:
    """Copy `<rev>:preprocessor/` out of git. Read-only: `git archive`
    writes nothing to the working tree and touches no index."""
    dest.mkdir(parents=True, exist_ok=True)
    tar = subprocess.run(["git", "archive", rev, "preprocessor"],
                         cwd=str(REPO), capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)],
                   input=tar.stdout, check=True)
    return dest / "preprocessor"


def score_in_subprocess(scrubber_dir: Path, corpus_path: Path,
                        extra: list[str]) -> dict:
    """Score against another revision without importing two copies of
    `closure_scrubber` into one interpreter."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(scrubber_dir) + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, str(Path(__file__).resolve()), "score",
           "--json", "--scrubber-dir", str(scrubber_dir),
           "--corpus", str(corpus_path)] + extra
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout)[-800:]}
    return json.loads(r.stdout)


# ── CLI ────────────────────────────────────────────────────────────────

def _import_scrubber(scrubber_dir: Path | None):
    if scrubber_dir:
        sys.path.insert(0, str(scrubber_dir))
    import closure_scrubber
    return closure_scrubber


def _corpus_arg(args) -> Path:
    """--corpus, else --real (the owner-private corpus), else the
    committed synthetic one."""
    if args.corpus:
        return Path(args.corpus)
    if getattr(args, "real", False):
        p = real_corpus_path()
        if p is None:
            raise SystemExit(f"no real corpus: set {REAL_CORPUS_ENV} or build "
                             f"one into {default_real_corpus_path()}")
        return p
    return CORPUS_PATH


def cmd_score(args) -> int:
    corpus = load_corpus(_corpus_arg(args))
    scrubber = _import_scrubber(Path(args.scrubber_dir)
                                if args.scrubber_dir else None)
    rep = score(corpus, scrubber, cohort=args.cohort, linkage=args.linkage)
    if args.json:
        print(json.dumps(rep, indent=1))
    else:
        print(render_md(rep, args.label))
    return 0


def cmd_compare(args) -> int:
    corpus_path = _corpus_arg(args)
    tmp = Path(tempfile.mkdtemp(prefix="closure-revs-"))
    try:
        rows = []
        for rev in args.rev:
            pdir = export_rev(rev, tmp / rev.replace("/", "_"))
            rep = score_in_subprocess(pdir, corpus_path, [])
            rows.append((rev, rep))
        rows.append(("WORKTREE", score(load_corpus(corpus_path),
                                       _import_scrubber(None))))
        if args.json:
            print(json.dumps({r: rep for r, rep in rows}, indent=1))
            return 0
        for rev, rep in rows:
            if "error" in rep:
                print(f"*{rev}*: FAILED\n  {rep['error'][:300]}")
                continue
            print(render_md(rep, rev))
            print()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def cmd_audit(args) -> int:
    """Which cases actually discriminate?

    A must_not_close case only earns its place if SOME scrubber build
    closes it: a case no revision ever fires on proves nothing and would
    quietly pad the precision score. Reconstruction can produce one, when
    the trigger email is gone from the mailbox and its world carries the
    sender and the overlap but not the original body. This reports the
    count so the corpus is read for what it is."""
    corpus_path = _corpus_arg(args)
    corpus = load_corpus(corpus_path)
    tmp = Path(tempfile.mkdtemp(prefix="closure-audit-"))
    fires: dict[str, list] = {c["case_id"]: [] for c in corpus["cases"]}
    try:
        for rev in args.rev:
            pdir = export_rev(rev, tmp / rev.replace("/", "_"))
            rep = score_in_subprocess(pdir, corpus_path, [])
            if "error" in rep:
                print(f"  {rev}: FAILED {rep['error'][:200]}")
                continue
            for r in rep["results"]:
                if r["verdict"] in ("true_close", "false_close"):
                    fires[r["case_id"]].append(rev)
        rep = score(corpus, _import_scrubber(None))
        for r in rep["results"]:
            if r["verdict"] in ("true_close", "false_close"):
                fires[r["case_id"]].append("WORKTREE")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    by_label = {c["case_id"]: c["label"] for c in corpus["cases"]}
    dead = [cid for cid, revs in fires.items()
            if not revs and by_label[cid] == "must_not_close"]
    live = [cid for cid, revs in fires.items()
            if revs and by_label[cid] == "must_not_close"]
    print(f"must_not_close cases that SOME revision closes (discriminating): "
          f"{len(live)}")
    print(f"must_not_close cases no scored revision closes (vacuous): "
          f"{len(dead)}")
    for cid in sorted(dead):
        print(f"    {cid}")
    if args.write:
        doc = json.loads(corpus_path.read_text())
        for c in doc["cases"]:
            c["discriminating"] = bool(fires.get(c["case_id"]))
        doc["discriminating_audit"] = {
            "revisions": list(args.rev) + ["WORKTREE"],
            "note": ("a case is discriminating when at least one scored "
                     "scrubber build closes it. A must_not_close case no "
                     "build closes proves nothing and must not be counted "
                     "as a caught bug."),
        }
        corpus_path.write_text(json.dumps(doc, indent=1))
        print(f"  annotated {corpus_path}")
    if args.json:
        print(json.dumps({"fires": fires, "vacuous": dead}, indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Closure precision/recall harness")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("score")
    p.add_argument("--corpus", default="")
    p.add_argument("--real", action="store_true",
                   help="score the owner-private real corpus (real_corpus_path)")
    p.add_argument("--scrubber-dir", default="",
                   help="a preprocessor/ directory to import from")
    p.add_argument("--cohort", default="")
    p.add_argument("--linkage", default="")
    p.add_argument("--label", default="HEAD")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("compare")
    p.add_argument("--rev", action="append", default=[],
                   help="git revision to score (repeatable)")
    p.add_argument("--corpus", default="")
    p.add_argument("--real", action="store_true",
                   help="score the owner-private real corpus (real_corpus_path)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("audit")
    p.add_argument("--rev", action="append", default=[])
    p.add_argument("--corpus", default="")
    p.add_argument("--real", action="store_true",
                   help="score the owner-private real corpus (real_corpus_path)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", action="store_true",
                   help="write the `discriminating` flag back into the corpus")

    args = ap.parse_args()
    return {"score": cmd_score, "compare": cmd_compare,
            "audit": cmd_audit}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
