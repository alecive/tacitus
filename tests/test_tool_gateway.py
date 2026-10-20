"""tool_gateway: the tool prompt, the parse of the model's calls, both
faces of the translation, the streamed shapes and the server with a
fake upstream. Nothing reaches the network beyond loopback."""
import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import owner_trust  # noqa: E402
import remote_llm as rl  # noqa: E402
import tool_gateway as gw  # noqa: E402

TOOLS_A = [{"name": "get_weather", "description": "Current weather",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
           {"name": "web_search", "type": "web_search_20250305"}]


def test_render_tools_is_the_models_own_block():
    text = gw.render_tools([{"name": "f", "description": "d", "parameters": {"type": "object"}}])
    assert text.startswith("# Tools") and "<tools>" in text and "</tools>" in text
    assert '{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}' in text
    assert "<tool_call>" in text and gw.render_tools([]) == ""


@pytest.mark.parametrize("text,expect_text,expect_calls", [
    ('<tool_call>\n{"name": "get_weather", "arguments": {"city": "Boulder"}}\n</tool_call>',
     "", [{"name": "get_weather", "arguments": {"city": "Boulder"}}]),
    ('Let me check.\n<tool_call>{"name":"get_weather","arguments":"{\\"city\\":\\"Denver\\"}"}</tool_call>\n'
     '<tool_call>```json\n{"name": "get_weather", "arguments": {"city": "Golden"}}\n```</tool_call>',
     "Let me check.", [{"name": "get_weather", "arguments": {"city": "Denver"}},
                       {"name": "get_weather", "arguments": {"city": "Golden"}}]),
    ('<tool_call>\n{"name": "get_weather", "arguments": {"city": "Boulder"}}',      # cut off: no closing tag
     "", [{"name": "get_weather", "arguments": {"city": "Boulder"}}]),
    ('<tool_call>{"name": "rm_rf", "arguments": {}}</tool_call> done', '<tool_call>{"name": "rm_rf", "arguments": {}}</tool_call> done', []),
    ("It is sunny.", "It is sunny.", []),
    ('<tool_call>not json</tool_call>', '<tool_call>not json</tool_call>', []),
])
def test_parse_tool_calls(text, expect_text, expect_calls):
    kept, calls = gw.parse_tool_calls(text, {"get_weather"})
    assert kept == expect_text and calls == expect_calls


def test_anthropic_request_becomes_one_chat_with_the_tool_block_and_history_in_tags():
    req = {"model": "m", "max_tokens": 100, "stream": True,
           "system": [{"type": "text", "text": "You are terse.", "cache_control": {"type": "ephemeral"}}],
           "tools": TOOLS_A,
           "messages": [
               {"role": "user", "content": "Weather in Boulder?"},
               {"role": "assistant", "content": [{"type": "text", "text": "Checking."},
                                                 {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                                                  "input": {"city": "Boulder"}}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                             "content": [{"type": "text", "text": "sunny, 20C"}]},
                                            {"type": "image", "source": {}}]},
           ]}
    messages, tools = gw.anthropic_to_chat(req)
    assert [t["name"] for t in tools] == ["get_weather"]           # the server tool is dropped
    assert messages[0]["role"] == "system" and messages[0]["content"].startswith("You are terse.")
    assert "# Tools" in messages[0]["content"] and "get_weather" in messages[0]["content"]
    assert messages[0]["content"].endswith(gw.GUIDANCE)
    assert [m["role"] for m in messages[1:]] == ["user", "assistant", "user"]
    assert messages[2]["content"] == ('Checking.\n<tool_call>\n{"name": "get_weather", "arguments": {"city": "Boulder"}}'
                                      "\n</tool_call>")
    assert messages[3]["content"] == "<tool_response>\nsunny, 20C\n</tool_response>\n[image omitted]"


def test_anthropic_response_and_events_carry_the_calls():
    req = {"model": "claude-x"}
    msg = gw.anthropic_response(req, "Checking.", [{"name": "get_weather", "arguments": {"city": "B"}}],
                                "stop", {"prompt_tokens": 10, "completion_tokens": 5})
    assert msg["stop_reason"] == "tool_use" and msg["model"] == "claude-x"
    assert [b["type"] for b in msg["content"]] == ["text", "tool_use"]
    assert msg["content"][1]["name"] == "get_weather" and msg["content"][1]["id"].startswith("toolu_")
    assert msg["usage"] == {"input_tokens": 10, "output_tokens": 5}
    names = [n for n, _d in gw.anthropic_events(msg)]
    assert names == ["message_start", "content_block_start", "content_block_delta", "content_block_stop",
                     "content_block_start", "content_block_delta", "content_block_stop",
                     "message_delta", "message_stop"]
    deltas = [d for n, d in gw.anthropic_events(msg) if n == "content_block_delta"]
    assert deltas[1]["delta"] == {"type": "input_json_delta", "partial_json": '{"city": "B"}'}
    plain = gw.anthropic_response(req, "Sunny.", [], "length", {})
    assert plain["stop_reason"] == "max_tokens" and plain["content"] == [{"type": "text", "text": "Sunny."}]
    assert gw.anthropic_response(req, "", [], "stop", {})["content"] == [{"type": "text", "text": ""}]


def test_openai_request_and_response_round_trip():
    req = {"model": "m", "stream": False,
           "tools": [{"type": "function", "function": {"name": "get_weather", "description": "w",
                                                       "parameters": {"type": "object"}}}],
           "messages": [{"role": "system", "content": "Be brief."},
                        {"role": "user", "content": "Boulder?"},
                        {"role": "assistant", "content": None, "tool_calls": [
                            {"id": "call_1", "type": "function",
                             "function": {"name": "get_weather", "arguments": '{"city": "Boulder"}'}}]},
                        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}]}
    messages, tools = gw.openai_to_chat(req)
    assert tools[0]["name"] == "get_weather" and messages[0]["role"] == "system"
    assert messages[2]["content"].startswith("<tool_call>") and '"city": "Boulder"' in messages[2]["content"]
    assert messages[3]["content"] == "<tool_response>\n(get_weather)\nsunny\n</tool_response>"
    resp = gw.openai_response(req, "", [{"name": "get_weather", "arguments": {"city": "B"}}], "stop",
                              {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
    m = resp["choices"][0]
    assert m["finish_reason"] == "tool_calls" and m["message"]["content"] is None
    assert m["message"]["tool_calls"][0]["function"] == {"name": "get_weather", "arguments": '{"city": "B"}'}
    chunks = gw.openai_chunks(resp)
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls" and chunks[-1]["usage"]["total_tokens"] == 3


def test_answer_routes_and_maps_errors(monkeypatch):
    seen = []

    def chat(messages, **kw):
        seen.append((messages, kw))
        return {"content": '<tool_call>{"name": "get_weather", "arguments": {"city": "Boulder"}}</tool_call>',
                "usage": {"prompt_tokens": 3, "completion_tokens": 4}, "finish": "stop"}
    status, body, streamed = gw.answer("/v1/messages", {"model": "m", "max_tokens": 50_000, "temperature": 0.3,
                                                        "tools": TOOLS_A, "messages": [{"role": "user", "content": "hi"}]},
                                       chat=chat)
    assert status == 200 and not streamed and body["stop_reason"] == "tool_use"
    assert seen[0][1]["max_tokens"] == gw.MAX_TOKENS_CAP and seen[0][1]["temperature"] == 0.3
    assert seen[0][1]["purpose"] == gw.PURPOSE
    status, body, streamed = gw.answer("/v1/messages", {"stream": True, "messages": [{"role": "user", "content": "hi"}]},
                                       chat=chat)
    assert streamed and body[0][0] == "message_start"
    # An unknown function in the answer is not run: the model is not let loose on names it made up.
    # Nor does its markup reach the reader: with no tools to re-ask with, the block is dropped.
    status, body, _s = gw.answer("/v1/messages", {"messages": [{"role": "user", "content": "hi"}]}, chat=chat)
    assert body["stop_reason"] == "end_turn" and body["content"][0]["type"] == "text"
    assert "<tool_call>" not in json.dumps(body) and body["content"][0]["text"] == gw.MALFORMED_STOP
    status, body, _s = gw.answer("/v1/messages/count_tokens", {"messages": [{"role": "user", "content": "x" * 400}]})
    assert status == 200 and body["input_tokens"] >= 100
    assert gw.answer("/v1/other", {})[0] == 404

    def down(messages, **kw):
        raise rl.RemoteLLMError("circuit open after repeated failures")
    status, body, _s = gw.answer("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}, chat=down)
    assert status == 502 and body["error"]["type"] == "api_error"

    def empty(messages, **kw):
        raise rl.ModelOutputError("empty completion")
    status, body, _s = gw.answer("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}, chat=empty)
    assert status == 200 and body["choices"][0]["finish_reason"] == "stop"


@pytest.fixture
def served(tmp_path, monkeypatch):
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    monkeypatch.setattr(rl, "_owner", lambda: True)
    monkeypatch.setattr(rl, "config", lambda: {"endpoint": "https://llm.example.edu/v1", "model": "m-1"})

    def chat(messages, **kw):
        last = messages[-1]["content"]
        if "weather" in last.lower():
            return {"content": '<tool_call>{"name": "get_weather", "arguments": {"city": "Boulder"}}</tool_call>',
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4}, "finish": "stop"}
        return {"content": "Hello.", "usage": {"prompt_tokens": 3, "completion_tokens": 1}, "finish": "stop"}
    srv = gw.make_server(0, chat=chat)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}", gw.token()
    srv.shutdown()


def _call(url, path, body=None, token="", method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), ""


def test_the_server_takes_the_token_only_and_streams_both_faces(served):
    srv, url, tok = served
    assert _call(url, "/health")[0] == 200
    assert _call(url, "/v1/models")[0] == 401
    assert _call(url, "/v1/messages", {"messages": []}, token="wrong")[0] == 401
    status, text, _ct = _call(url, "/v1/models", token=tok)
    assert status == 200 and json.loads(text)["data"][0]["id"] == "m-1"
    status, text, ct = _call(url, "/v1/messages", {"model": "x", "max_tokens": 10, "stream": True, "tools": TOOLS_A,
                                                   "messages": [{"role": "user", "content": "weather?"}]}, token=tok)
    assert status == 200 and ct.startswith("text/event-stream")
    events = [l for l in text.splitlines() if l.startswith("event: ")]
    assert events[0] == "event: message_start" and events[-1] == "event: message_stop"
    assert '"name": "get_weather"' in text and '"stop_reason": "tool_use"' in text
    status, text, _ct = _call(url, "/v1/chat/completions", {"model": "x", "stream": True,
                                                            "messages": [{"role": "user", "content": "hi"}]}, token=tok)
    assert status == 200 and text.strip().endswith("data: [DONE]")
    assert '"content": "Hello."' in text
    status, text, _ct = _call(url, "/v1/chat/completions", {"model": "x",
                                                            "messages": [{"role": "user", "content": "hi"}]}, token=tok)
    assert json.loads(text)["choices"][0]["message"]["content"] == "Hello."
    assert _call(url, "/v1/messages", token=tok, method="POST")[0] == 413
    # The token lives in the owner's private directory, owner-only.
    p = owner_trust.private_dir() / gw.TOKEN_FILE
    assert p.read_text().strip() == tok and (p.stat().st_mode & 0o777) == 0o600
    assert gw.token() == tok


def test_env_names_the_gateway_and_turns_the_side_traffic_off(served, capsys):
    srv, url, tok = served
    port = srv.server_address[1]
    assert gw.main(["env", "--port", str(port)]) == 0
    out = capsys.readouterr().out
    assert f"ANTHROPIC_BASE_URL=http://127.0.0.1:{port}" in out and f"ANTHROPIC_AUTH_TOKEN={tok}" in out
    assert "ANTHROPIC_MODEL=m-1" in out and "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in out
    assert "DISABLE_BUG_COMMAND=1" in out
    assert gw.main(["status", "--port", str(port)]) == 0
    assert json.loads(capsys.readouterr().out)["up"] is True


def test_the_loop_guard_stops_the_third_identical_call():
    call = '<tool_call>{"name": "get_weather", "arguments": {"city": "B"}}</tool_call>'
    hist = [{"role": "system", "content": "s"}, {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": call},
            {"role": "user", "content": "<tool_response>\nExit code 1\n</tool_response>"},
            {"role": "assistant", "content": call},
            {"role": "user", "content": "<tool_response>\nExit code 1\n</tool_response>"}]
    calls = [{"name": "get_weather", "arguments": {"city": "B"}}]
    stop = gw.loop_guard(hist, calls)
    assert stop.startswith("I stopped: the same call to get_weather came back the same way 2 times (Exit code 1)")
    # After a failed result one earlier identical call is enough; after a
    # good result it takes two; a different call is not a loop; no call is not.
    assert gw.loop_guard(hist[:4], calls).startswith("I stopped")
    ok_hist = [dict(m, content=m["content"].replace("Exit code 1", "sunny")) for m in hist]
    assert gw.loop_guard(ok_hist[:4], calls) == "" and gw.loop_guard(ok_hist, calls).startswith("I stopped")
    assert gw.loop_guard(hist, [{"name": "get_weather", "arguments": {"city": "C"}}]) == ""
    assert gw.loop_guard(hist, []) == ""
    # Through `answer`: the third repeat comes back as text, end_turn.
    def chat(messages, **kw):
        return {"content": call, "usage": {}, "finish": "stop"}
    req = {"tools": TOOLS_A, "messages": [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "get_weather", "input": {"city": "B"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Exit code 1"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "get_weather", "input": {"city": "B"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "Exit code 1"}]}]}
    status, body, _s = gw.answer("/v1/messages", req, chat=chat)
    assert body["stop_reason"] == "end_turn" and body["content"][0]["text"].startswith("I stopped")


def test_the_step_budget_tells_the_model_to_finish_and_then_answers_for_it():
    call = '<tool_call>{"name": "get_weather", "arguments": {"city": "B%d"}}</tool_call>'

    def hist(n):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "weather?"}]
        for i in range(n):
            msgs.append({"role": "assistant", "content": call % i})
            msgs.append({"role": "user", "content": "<tool_response>\nnothing\n</tool_response>"})
        return msgs
    assert gw._calls_so_far(hist(5)) == 5
    msgs, stop = gw.step_budget(hist(gw.STEP_SOFT - 1))
    assert stop == "" and "[You have made" not in msgs[-1]["content"]
    msgs, stop = gw.step_budget(hist(gw.STEP_SOFT))
    assert stop == "" and msgs[-1]["content"].endswith("unless one is essential.]")
    assert f"You have made {gw.STEP_SOFT} tool calls" in msgs[-1]["content"]
    msgs, stop = gw.step_budget(hist(gw.STEP_HARD))
    assert stop.startswith(f"I stopped after {gw.STEP_HARD} tool calls")
    # Through `answer`: past the hard limit no completion is asked for.
    calls = []

    def chat(messages, **kw):
        calls.append(1)
        return {"content": "x", "usage": {}, "finish": "stop"}
    req = {"tools": TOOLS_A, "messages": [{"role": "user", "content": "weather?"}]}
    for i in range(gw.STEP_HARD):
        req["messages"].append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "get_weather", "input": {"city": f"B{i}"}}]})
        req["messages"].append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": "nothing"}]})
    status, body, _s = gw.answer("/v1/messages", req, chat=chat)
    assert calls == [] and body["stop_reason"] == "end_turn" and body["content"][0]["text"].startswith("I stopped after")


def test_the_coder_style_call_form_is_parsed_too():
    text = ('<tool_call>\n<function=Bash>\narguments= {"command": "date +%Y", "description": "year"}\n</tool_call>')
    kept, calls = gw.parse_tool_calls(text, {"Bash"})
    assert kept == "" and calls == [{"name": "Bash", "arguments": {"command": "date +%Y", "description": "year"}}]
    text = ('<tool_call>\n<function=Bash>\n<parameter=command>\ndate +%Y\n</parameter>\n'
            '<parameter=timeout>\n5000\n</parameter>\n</function>\n</tool_call>')
    kept, calls = gw.parse_tool_calls(text, {"Bash"})
    assert calls == [{"name": "Bash", "arguments": {"command": "date +%Y", "timeout": "5000"}}]
    kept, calls = gw.parse_tool_calls('<tool_call>\n<function=rm_rf>\narguments= {}\n</tool_call>', {"Bash"})
    assert calls == [] and "<function=rm_rf>" in kept


def test_variants_come_from_the_model_name_and_reach_the_upstream_call():
    v = gw.variant("qwen3.8-mm")
    assert v["thinking"] is False and v["sampling"]["temperature"] == 0.7 and v["sampling"]["top_p"] == 0.8
    assert v["sampling"]["presence_penalty"] == 1.0
    v = gw.variant("qwen3.8-mm+think")
    assert v["thinking"] is True and v["sampling"]["temperature"] == 0.6 and v["sampling"]["top_p"] == 0.95
    v = gw.variant("qwen3.8-mm+greedy")
    assert v["sampling"]["temperature"] == 0.0 and v["sampling"]["presence_penalty"] == 0.0
    v = gw.variant("qwen3.8-mm+t0.3+p1.5")
    assert v["sampling"]["temperature"] == 0.3 and v["sampling"]["presence_penalty"] == 1.5
    seen = {}

    def chat(messages, **kw):
        seen.update(kw)
        return {"content": "ok", "usage": {}, "finish": "stop"}
    gw.answer("/v1/messages", {"model": "x+think", "messages": [{"role": "user", "content": "hi"}]}, chat=chat)
    assert seen["thinking"] is True and seen["temperature"] == 0.6
    assert seen["sampling"] == {"top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.0}
    # A temperature the harness sets wins over the variant's.
    gw.answer("/v1/messages", {"model": "x", "temperature": 0.2, "messages": [{"role": "user", "content": "hi"}]}, chat=chat)
    assert seen["temperature"] == 0.2 and seen["thinking"] is False
    env = gw.lane_env(3070, model="m-1", variant_name="think")
    assert env["ANTHROPIC_MODEL"] == "m-1+think" and env["ANTHROPIC_SMALL_FAST_MODEL"] == "m-1+think"


def test_the_context_is_trimmed_to_the_models_window(monkeypatch):
    monkeypatch.setattr(gw, "CONTEXT_TOKENS", 12000)
    big = "x" * 3000                      # ~940 tokens each
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
    for i in range(12):
        msgs.append({"role": "assistant", "content": '<tool_call>{"name": "f", "arguments": {}}</tool_call>'})
        msgs.append({"role": "user", "content": f"<tool_response>\n{big}{i}\n</tool_response>"})
    out, trimmed = gw.trim_context(msgs, 500)
    assert trimmed >= 1 and gw._tokens(out) <= 12000 - 500 - gw.CONTEXT_MARGIN
    # The newest results are untouched, the oldest became stubs.
    assert big in out[-1]["content"] and "[earlier result trimmed" in out[3]["content"]
    assert msgs[3]["content"].startswith("<tool_response>\n" + big)     # the input is not mutated
    same, n = gw.trim_context(msgs[:4], 500)
    assert n == 0 and same is msgs[:4] or same == msgs[:4]


def test_a_server_overflow_trims_harder_and_retries_once(monkeypatch):
    """A 292k-character turn passed the estimate and the server answered
    400 context length; the harness then repeated it eleven times. The
    gateway now trims to a smaller budget with a two-result tail and
    calls once more."""
    monkeypatch.setattr(gw, "CONTEXT_TOKENS", 12000)
    big = "x" * 3000
    msgs = [{"role": "user", "content": "go"}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": '<tool_call>{"name": "f", "arguments": {}}</tool_call>'})
        msgs.append({"role": "user", "content": f"<tool_response>\n{big}{i}\n</tool_response>"})
    seen = []

    def chat(messages, **kw):
        seen.append(messages)
        if len(seen) == 1:
            raise gw._rl.RemoteLLMError("HTTP 400: ContextWindowExceededError: This model's maximum context length is 12000 tokens")
        return {"content": "done", "finish": "stop", "usage": {}}
    req = {"model": "m", "max_tokens": 500, "messages": [{"role": "user", "content": "go"}]}
    monkeypatch.setattr(gw, "anthropic_to_chat", lambda r, slim=False: (list(msgs), []))
    code, body, _ = gw.answer("/v1/messages", req, chat=chat, model="m")
    assert code == 200 and len(seen) == 2
    stubs = [m for m in seen[1] if "[earlier result trimmed" in str(m.get("content"))]
    assert stubs and gw._tokens(seen[1]) <= (12000 - 500 - gw.CONTEXT_MARGIN) * gw.OVERFLOW_CUT
    # Any other server error still comes back as a 502.
    def chat2(messages, **kw):
        raise gw._rl.RemoteLLMError("HTTP 500: upstream")
    code, body, _ = gw.answer("/v1/messages", req, chat=chat2, model="m")
    assert code == 502


def test_other_json_keys_for_the_function_name_are_taken():
    for text in ('<tool_call>\n{"function": "Bash", "arguments": {"command": "date"}}\n</tool_call>',
                 '<tool_call>{"tool": "Bash", "arguments": {"command": "date"}}</tool_call>',
                 '<tool_call>{"function": {"name": "Bash", "arguments": {"command": "date"}}}</tool_call>',
                 '<tool_call>{"type": "function", "function": {"name": "Bash", "arguments": "{\\"command\\": \\"date\\"}"}}</tool_call>'):
        kept, calls = gw.parse_tool_calls(text, {"Bash"})
        assert calls == [{"name": "Bash", "arguments": {"command": "date"}}], text
        assert kept == ""


def test_the_slim_variant_keeps_only_the_environment_section_of_the_harness_prompt():
    harness = ("You are a Claude agent.\n\n# Harness\n - rules\n\n# Memory\n\nlots\n\n# Environment\n - cwd: /x\n"
               " - Today's date is 2026-10-12.\n\n# Context management\nmore\n")
    s = gw.slim_system(harness)
    assert s.startswith(gw.SLIM_SYSTEM) and "Today's date is 2026-10-12" in s
    assert "# Memory" not in s and "# Context management" not in s and "# Harness" not in s
    assert gw.variant("m+slim")["slim"] is True and gw.variant("m")["slim"] is False
    req = {"model": "m+slim", "system": harness, "tools": TOOLS_A, "messages": [{"role": "user", "content": "hi"}]}
    messages, _t = gw.anthropic_to_chat(req, slim=True)
    assert messages[0]["content"].startswith(gw.SLIM_SYSTEM) and "# Tools" in messages[0]["content"]
    seen = {}

    def chat(messages, **kw):
        seen["system"] = messages[0]["content"]
        return {"content": "ok", "usage": {}, "finish": "stop"}
    gw.answer("/v1/messages", req, chat=chat)
    assert "# Memory" not in seen["system"] and "Today's date" in seen["system"]
    gw.answer("/v1/messages", dict(req, model="m"), chat=chat)
    assert "# Memory" in seen["system"]


def test_a_call_missing_its_closing_braces_is_still_taken():
    text = '<tool_call>\n{"name": "Bash", "arguments": {"command": "python3 -c \\"print(1)\\""}'   # no }} and no closing tag
    kept, calls = gw.parse_tool_calls(text, {"Bash"})
    assert calls == [{"name": "Bash", "arguments": {"command": 'python3 -c "print(1)"'}}] and kept == ""
    text = '<tool_call>{"name": "Bash", "arguments": {"command": "date"'
    assert gw.parse_tool_calls(text, {"Bash"})[1] == [{"name": "Bash", "arguments": {"command": "date"}}]
    assert gw.parse_tool_calls('<tool_call>{"name": "Bash", "arguments": {"command": "da', {"Bash"})[1] == []


def test_a_key_the_model_left_unquoted_is_repaired():
    # The solar-room turn of 2026-10-15: `arguments` lost its opening quote
    # and the raw block reached the user as the reply.
    text = '<tool_call>\n{"name": "Bash",\narguments": {"command": "ls /home/ea-bot/.ea/"}}\n</tool_call>'
    kept, calls = gw.parse_tool_calls(text, {"Bash"})
    assert calls == [{"name": "Bash", "arguments": {"command": "ls /home/ea-bot/.ea/"}}] and kept == ""
    text = '<tool_call>{name: "Bash", arguments: {"command": "date"}}</tool_call>'
    assert gw.parse_tool_calls(text, {"Bash"})[1] == [{"name": "Bash", "arguments": {"command": "date"}}]
    # A key inside an argument string is not touched.
    text = '<tool_call>{"name": "Bash", "arguments": {"command": "echo {name: 1}"}}</tool_call>'
    assert gw.parse_tool_calls(text, {"Bash"})[1] == [{"name": "Bash", "arguments": {"command": "echo {name: 1}"}}]


def test_the_step_budget_counts_this_turn_only():
    call = '<tool_call>{"name": "get_weather", "arguments": {"city": "B%d"}}</tool_call>'
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "weather?"}]
    for i in range(gw.STEP_HARD + 20):          # an earlier turn's calls, in a resumed session
        msgs.append({"role": "assistant", "content": call % i})
        msgs.append({"role": "user", "content": "<tool_response>\nnothing\n</tool_response>"})
    msgs.append({"role": "assistant", "content": "It is sunny."})
    msgs.append({"role": "user", "content": "and tomorrow?"})
    assert gw._calls_so_far(msgs) == 0
    msgs.append({"role": "assistant", "content": call % 99})
    msgs.append({"role": "user", "content": "<tool_response>\nnothing\n</tool_response>"})
    assert gw._calls_so_far(msgs) == 1
    assert gw.step_budget(msgs)[1] == ""


def test_no_budget_applies_to_a_request_without_tools():
    # The harness's compaction summary: no functions, a long history of
    # calls. The model's text is the answer, never the stop text.
    calls = []

    def chat(messages, **kw):
        calls.append(1)
        return {"content": "Summary: the user asked about the solar portal.", "usage": {}, "finish": "stop"}
    req = {"messages": [{"role": "user", "content": "weather?"}]}
    for i in range(gw.STEP_HARD):
        req["messages"].append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "get_weather", "input": {"city": f"B{i}"}}]})
        req["messages"].append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": "nothing"}]})
    req["messages"].append({"role": "user", "content": "Summarize the conversation so far."})
    status, body, _s = gw.answer("/v1/messages", req, chat=chat)
    assert calls == [1] and body["content"][0]["text"].startswith("Summary:")


def test_a_malformed_call_is_asked_again_and_never_reaches_the_answer():
    bad = '<tool_call>\n{"name": "get_weather" "arguments": {"city": "Boulder"}}\n</tool_call>'   # no comma
    good = '<tool_call>{"name": "get_weather", "arguments": {"city": "Boulder"}}</tool_call>'
    seen = []

    def chat(messages, **kw):
        seen.append(messages[-1]["content"])
        return {"content": bad if len(seen) == 1 else good, "usage": {}, "finish": "stop"}
    req = {"tools": TOOLS_A, "messages": [{"role": "user", "content": "weather?"}]}
    status, body, _s = gw.answer("/v1/messages", req, chat=chat)
    assert len(seen) == 2 and seen[1] == gw.REASK
    assert body["stop_reason"] == "tool_use" and body["content"][0]["input"] == {"city": "Boulder"}
    # Still malformed on the second try: the markup is dropped; with
    # nothing left the stop text is the answer.
    seen.clear()

    def chat2(messages, **kw):
        seen.append(1)
        return {"content": bad, "usage": {}, "finish": "stop"}
    status, body, _s = gw.answer("/v1/messages", req, chat=chat2)
    assert len(seen) == 2 and body["stop_reason"] == "end_turn"
    assert body["content"][0]["text"] == gw.MALFORMED_STOP and "<tool_call>" not in json.dumps(body)

    def chat3(messages, **kw):
        return {"content": "Checking.\n<tool_call>{\"name\": \"rm_rf\", \"arguments\": {}}</tool_call>", "usage": {}, "finish": "stop"}
    status, body, _s = gw.answer("/v1/messages", req, chat=chat3)
    assert body["content"][0]["text"] == "Checking." and body["stop_reason"] == "end_turn"
