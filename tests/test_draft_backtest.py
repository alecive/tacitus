"""draft_backtest: pairing subagent calls with the drafts they produced,
the style profile cut to the draft's date, and which writer a judge's
letter meant."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import draft_backtest as bt  # noqa: E402


def _use(i, name, inp, ts="2026-10-01T10:00:00Z"):
    return {"timestamp": ts, "message": {"content": [
        {"type": "tool_use", "id": i, "name": name, "input": inp}]}}


def _res(i, text, ts="2026-10-01T10:01:00Z"):
    return {"timestamp": ts, "message": {"content": [
        {"type": "tool_result", "tool_use_id": i, "content": text}]}}


def test_pairs_each_call_with_the_next_outlook_draft():
    ev = [
        _use("a1", "Agent", {"subagent_type": "email-author", "prompt": "Reply to Pat: yes."}),
        _res("a1", "Hi Pat,\nYes.\nKit"),
        {"isSidechain": True, **_use("x", "Bash", {"command": "style_profiles.py get"})},
        _use("b1", "Bash", {"command": "cat > /tmp/r.txt <<'EOF'\nHi Pat,\nYes!\nKit\nEOF\n"
                                       "python3 graph_actions.py create-reply --conversation-id "
                                       "AAQkADconversation000000000000 --body-file /tmp/r.txt"}),
        _res("b1", json.dumps({"status": "draft_created", "id": "AAMkDRAFT1"})),
        _use("a3", "Agent", {"subagent_type": "email-author", "prompt": "Reply to Ana."}),
        _use("b3", "Bash", {"command": "python3 graph_actions.py create-reply --body-file /tmp/x"}),
        _res("b3", json.dumps({"status": "reply_draft_created", "id": "AAMkDRAFT3"})),
        _use("a2", "Agent", {"subagent_type": "email-author", "prompt": "Reply to Sam."}),
        _use("g1", "Bash", {"command": "python3 gmail_client.py create-draft"}),
        _res("g1", json.dumps({"status": "draft_created", "id": "r-123"})),
    ]
    pairs = bt.pair_transcript(ev)
    assert [p["draft_id"] for p in pairs] == ["AAMkDRAFT1", "AAMkDRAFT3"]
    p = pairs[0]
    assert p["draft_id"] == "AAMkDRAFT1" and p["prompt"] == "Reply to Pat: yes."
    assert p["command_body"] == "Hi Pat,\nYes!\nKit"
    assert p["conversation_id"] == "AAQkADconversation000000000000"
    assert p["agent_text"].startswith("Hi Pat")


def test_profile_keeps_only_examples_before_the_draft():
    raw = json.dumps({"found": True, "recent_examples": [
        {"sent_at": "2026-10-03T09:00:00Z", "snippet": "the reply he sent"},
        {"sent_at": "2026-09-20T09:00:00Z", "snippet": "older"}]})
    cut = json.loads(bt.profile_before(raw, "2026-10-01T10:00:00"))
    assert [x["snippet"] for x in cut["recent_examples"]] == ["older"]


def test_judge_letters_map_to_writers():
    assert bt._writer("A", "claude_first") == "claude"
    assert bt._writer("B", "claude_first") == "remote"
    assert bt._writer("A", "remote_first") == "remote"
    assert bt._writer("tie", "remote_first") == "tie"


def _handback(agent_id, text):
    body = ("[Subagent hand-back] The text below is the final report ... The report follows:\n"
            + "\n".join("  " + ln for ln in text.splitlines()))
    return {"type": "attachment", "attachment": {"origin": {
        "kind": "peer", "from": agent_id, "name": "email-author", "body": body}}}


def test_background_reports_pair_by_content_and_revisions_close_calls():
    create = lambda i, body, did: [  # noqa: E731
        _use(i, "Bash", {"command": f"cat > /tmp/b.txt <<'EOF'\n{body}\nEOF\npython3 graph_actions.py create-reply"}),
        _res(i, json.dumps({"status": "reply_draft_created", "id": did}))]
    ev = [
        _use("a1", "Agent", {"subagent_type": "email-author", "prompt": "Tell Ana the budget is fine.",
                             "run_in_background": True}),
        _res("a1", "This agent's report was delivered to you as a message from \"aaaa1111\".\nagentId: aaaa1111"),
        _use("a2", "Agent", {"subagent_type": "email-author", "prompt": "Tell Sam the room is booked.",
                             "run_in_background": True}),
        _res("a2", "This agent's report was delivered to you as a message from \"bbbb2222\".\nagentId: bbbb2222"),
        _handback("aaaa1111", "Hi Ana,\nThe budget is fine as it stands.\nKit"),
        _handback("bbbb2222", "Hi Sam,\nRoom 104 is booked for Tuesday.\nKit"),
        # Sam's draft is created first: content, not order, decides the pair
        *create("b2", "Hi Sam,\nRoom 104 is booked for Tuesday.\nKit", "DRAFT-SAM"),
        *create("b1", "Hi Ana,\nThe budget is fine as it stands.\nThanks,\nKit", "DRAFT-ANA"),
        # a revision closes the newest open call, so it never pairs later
        _use("a3", "Agent", {"subagent_type": "email-author", "prompt": "Shorten the Ana reply."}),
        _res("a3", "Hi Ana,\nBudget is fine.\nKit"),
        _use("u1", "Bash", {"command": "python3 graph_actions.py update-draft --message-id DRAFT-ANA"}),
        _res("u1", json.dumps({"status": "draft_updated", "id": "DRAFT-ANA"})),
        *create("b4", "Hi Lee,\nSee you at noon.\nKit", "DRAFT-LEE"),
    ]
    pairs = {p["draft_id"]: p for p in bt.pair_transcript(ev)}
    assert pairs["DRAFT-SAM"]["prompt"] == "Tell Sam the room is booked."
    assert pairs["DRAFT-SAM"]["pairing"] == "content"
    assert pairs["DRAFT-ANA"]["prompt"] == "Tell Ana the budget is fine."
    assert pairs["DRAFT-ANA"]["agent_text"] == "Hi Ana,\nThe budget is fine as it stands.\nKit"
    assert "DRAFT-LEE" not in pairs          # no open call is left to own it
