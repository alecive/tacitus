"""reply_check_report: both logs, catches need a read, each pass costs its own."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import reply_check_report as rr  # noqa: E402


def _rec(ts, *, changed, reads_after, cost=None, delta=False, room="ea-x", thread="t1", ok=True):
    r = {"ts": ts, "room": room, "thread_ts": thread, "model": "m", "effort": "xhigh",
         "reasons": ["ledger-status-without-read"], "reads_before": {"ledger": 0, "mail": 0, "web": 0},
         "reads_after": reads_after, "claims": {"ledger": ["x is still open."], "mail": [], "negative": []},
         "second_pass_ok": ok, "changed": changed, "second_pass_s": 10.0}
    if cost is not None:
        r["second_pass_cost_usd"] = cost
    if delta:
        r["cost_is_delta"] = True
    return r


def test_load_merges_the_pre_move_copy_once(tmp_path):
    log = tmp_path / "reply-check.jsonl"
    (tmp_path / "pre-move").mkdir()
    old = _rec("2026-09-25T18:00:00+00:00", changed=False, reads_after={"ledger": 1}, thread="a")
    both = _rec("2026-09-29T18:00:00+00:00", changed=True, reads_after={"ledger": 1}, thread="b")
    new = _rec("2026-09-30T18:00:00+00:00", changed=True, reads_after={"ledger": 1}, thread="c")
    (tmp_path / "pre-move" / "reply-check.jsonl").write_text(
        json.dumps(old) + "\n" + json.dumps(both) + "\n")
    log.write_text(json.dumps(both) + "\n" + json.dumps(new) + "\n")
    rows = rr.load(log, rr._day("2026-09-24"), None)
    assert [r["thread_ts"] for r in rows] == ["a", "b", "c"]
    assert rr.load(log, rr._day("2026-09-30"), None)[0]["thread_ts"] == "c"


def test_a_catch_needs_a_read_behind_the_change():
    rows = [
        _rec("2026-10-01T00:00:00+00:00", changed=True, reads_after={"ledger": 1, "mail": 0, "web": 0}),
        _rec("2026-10-01T00:01:00+00:00", changed=True, reads_after={"ledger": 0, "mail": 0, "web": 0}),
        _rec("2026-10-01T00:02:00+00:00", changed=False, reads_after={"ledger": 1, "mail": 0, "web": 0}),
        _rec("2026-10-01T00:03:00+00:00", changed=True, reads_after={}, ok=False),
    ]
    rows[1]["read_made"] = False  # the field, when present, is authoritative
    s = rr.summarize(rows, {"ea-x": 10})
    assert (s["catches"], s["unverified_changes"], s["false_alarms"], s["second_pass_failed"]) == (1, 1, 1, 1)
    assert s["by_room"]["ea-x"]["unverified"] == 1


def test_pass_cost_is_the_delta_or_the_bot_log_pairing(tmp_path):
    # a record logged as a delta is taken as is; a legacy record (running
    # total) is paired with the bot log's mono-check line within 2 s
    local = datetime.now().astimezone().tzinfo
    when = datetime(2026, 9, 30, 10, 22, 18, 597000, tzinfo=local)
    bot = tmp_path / "slack-bot.log"
    stamp = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S,") + f"{dt.microsecond // 1000:03d}"
    bot.write_text(
        f"{stamp(when.replace(second=2, microsecond=259000))} INFO [mono] done in 129.9s | turns=8 | "
        "tokens=4+1cache->2 | cost=$50.2215 | tools: Bash(8s)\n"
        f"{stamp(when)} INFO [mono-check] done in 16.2s | turns=2 | tokens=4+1cache->818 | "
        "cost=$50.5372 | tools: Bash(2s)\n")
    legacy = rr.marginal_costs(bot)
    assert len(legacy) == 1 and abs(legacy[0][1] - 0.3157) < 1e-6
    utc_ts = when.astimezone(timezone.utc).isoformat()
    old = _rec(utc_ts, changed=True, reads_after={"ledger": 1}, cost=50.537)
    assert abs(rr._pass_cost(old, legacy) - 0.3157) < 1e-6
    new = _rec("2026-10-05T00:00:00+00:00", changed=True, reads_after={"ledger": 1}, cost=0.21, delta=True)
    assert rr._pass_cost(new, legacy) == 0.21
    far = _rec("2026-09-30T16:22:30+00:00", changed=True, reads_after={"ledger": 1}, cost=50.537)
    assert rr._pass_cost(far, legacy) is None
    s = rr.summarize([old, new, far], {}, legacy)
    assert (s["passes_costed"], s["passes_uncosted"]) == (2, 1)
    assert abs(s["second_pass_cost_usd"] - 0.53) < 1e-6
    text = rr.render(s, "2026-09-24", "now")
    assert "catches (changed after a read): 3" in text and "1 passes without a cost" in text
