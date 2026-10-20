#!/usr/bin/env python3
"""preprocessor/chat_notify.py: the notifier every cron agent and plugin uses.

These pin: the Slack webhook payloads are the same bytes the old
slack_notifier sent (the old function is executed from git and both
paths record requests.post); the quiet-hours gate defers before any
routing; Zulip routing maps a channel key to its stream, opens a new
topic per post (or the pinned one) and converts mrkdwn; post_as_bot on Slack sends the fields scheduled_sends
always sent; the LLM grants name tools, never a wildcard.
"""
import ast
import io
import json
import subprocess
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import requests

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import chat_notify  # noqa: E402
import ea_config  # noqa: E402
import slack_notifier  # noqa: E402

BASE_COMMIT = "28a3633"


def _git_show(path: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "show", f"{BASE_COMMIT}:{path}"],
                              capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"{BASE_COMMIT} not available in this checkout")


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.ok = status == 200
        self._body = body if body is not None else {"ok": True}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _PostRecorder:
    def __init__(self, status=200, body=None):
        self.calls = []
        self.status = status
        self.body = body

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _Resp(self.status, self.body)

    def wire(self):
        """What went over the wire: url, headers and the JSON body bytes."""
        return [(a, k.get("headers"), k.get("timeout"),
                 json.dumps(k.get("json"), ensure_ascii=False).encode())
                for a, k in self.calls]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A scratch config (slack by default), webhooks file and private dir."""
    cfg = tmp_path / "ea-config.json"
    cfg.write_text("{}")
    hooks = tmp_path / "slack_webhooks.json"
    hooks.write_text(json.dumps({"ea-assistant": "https://hooks.example/A",
                                 "personal": "https://hooks.example/P"}))
    monkeypatch.setattr(ea_config, "CONFIG_FILE", cfg)
    monkeypatch.setattr(chat_notify, "WEBHOOKS_FILE", hooks)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "private"))
    qh = types.SimpleNamespace(should_defer=lambda priority: False,
                               defer=lambda *a: None)
    monkeypatch.setitem(sys.modules, "quiet_hours", qh)
    chat_notify.MEMORY_POSTS.clear()
    return types.SimpleNamespace(cfg=cfg, hooks=hooks, tmp=tmp_path, qh=qh)


def _set_chat(env, chat: dict):
    env.cfg.write_text(json.dumps({"chat": chat}))


def _old_notifier(env):
    """The pre-migration slack_notifier, executed from git."""
    mod = types.ModuleType("old_slack_notifier")
    exec(compile(_git_show("preprocessor/slack_notifier.py"), "old_slack_notifier", "exec"),
         mod.__dict__)
    mod.WEBHOOKS_FILE = env.hooks
    return mod


CASES = [
    ("ea-assistant", "plain *bold* text", None),
    ("personal", "one\n" + chat_notify.SPLIT_MARKER + "\ntwo :lock: é", None),
    ("ea-assistant", f"a{chat_notify.SPLIT_MARKER}  {chat_notify.SPLIT_MARKER}b",
     [{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}]),
    ("ea-assistant", "", None),
    ("ea-assistant", chat_notify.SPLIT_MARKER, [{"type": "divider"}]),
    ("personal", "with blocks", [{"type": "divider"}]),
]


@pytest.mark.parametrize("key,text,blocks", CASES)
def test_slack_webhook_payloads_byte_identical(env, monkeypatch, key, text, blocks):
    old = _old_notifier(env)
    rec_old, rec_new = _PostRecorder(), _PostRecorder()
    monkeypatch.setattr(requests, "post", rec_old)
    r_old = old.post_to_slack(key, text, blocks=blocks)
    monkeypatch.setattr(requests, "post", rec_new)
    r_new = chat_notify.post(key, text, blocks=blocks)
    assert r_old == r_new
    assert rec_old.wire() == rec_new.wire()
    # The shim goes the same way.
    rec_shim = _PostRecorder()
    monkeypatch.setattr(requests, "post", rec_shim)
    assert slack_notifier.post_to_slack(key, text, blocks) == r_old
    assert rec_shim.wire() == rec_old.wire()


def test_slack_failures_match_old(env, monkeypatch):
    old = _old_notifier(env)
    for status in (500, 200):
        rec_old, rec_new = _PostRecorder(status), _PostRecorder(status)
        monkeypatch.setattr(requests, "post", rec_old)
        r_old = old.post_to_slack("nope", "x")          # unknown key
        r_old2 = old.post_to_slack("personal", "x")
        monkeypatch.setattr(requests, "post", rec_new)
        assert chat_notify.post("nope", "x") is r_old is False
        assert chat_notify.post("personal", "x") == r_old2 == (status == 200)
        assert rec_old.wire() == rec_new.wire()


def _private(env, hooks: dict):
    """The owner-private dir as an owner process finds it: chat.json (an
    owner process takes the chat block only from there) and the webhooks
    file, owner-only."""
    private = env.tmp / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    private.chmod(0o700)
    for name, doc in (("chat.json", {"frontend": "slack"}), ("slack_webhooks.json", hooks)):
        (private / name).write_text(json.dumps(doc))
        (private / name).chmod(0o600)
    return private


def test_private_dir_webhooks_win(env, monkeypatch):
    _private(env, {"personal": "https://hooks.example/PRIVATE"})
    rec = _PostRecorder()
    monkeypatch.setattr(requests, "post", rec)
    assert chat_notify.post("personal", "hi")
    assert rec.calls[0][0] == ("https://hooks.example/PRIVATE",)


def test_the_shim_reads_webhooks_through_the_same_lookup(env):
    """slack_notifier's webhook path and loader are chat_notify's: the
    owner-private copy."""
    assert slack_notifier._webhooks_path is chat_notify._webhooks_path
    private = _private(env, {"personal": "P"})
    assert slack_notifier._webhooks_path() == private / "slack_webhooks.json"
    assert slack_notifier._load_webhooks() == {"personal": "P"}


def test_quiet_hours_defers_before_routing(env, monkeypatch):
    deferred = []
    env.qh.should_defer = lambda priority: priority == "normal"
    env.qh.defer = lambda key, text, priority: deferred.append((key, text, priority))
    rec = _PostRecorder()
    monkeypatch.setattr(requests, "post", rec)
    assert chat_notify.post("ea-assistant", "night note") is True
    assert slack_notifier.post_to_slack("personal", "via shim") is True
    assert deferred == [("ea-assistant", "night note", "normal"),
                        ("personal", "via shim", "normal")]
    assert rec.calls == []
    # Urgent is not deferred by this gate, and _bypass_quiet skips it.
    assert chat_notify.post("ea-assistant", "urgent", priority="urgent")
    assert chat_notify.post("ea-assistant", "flush", _bypass_quiet=True)
    assert len(rec.calls) == 2 and len(deferred) == 2


def test_quiet_hours_gate_error_fails_open(env, monkeypatch):
    def boom(priority):
        raise RuntimeError("bad tz")
    env.qh.should_defer = boom
    rec = _PostRecorder()
    monkeypatch.setattr(requests, "post", rec)
    out = io.StringIO()
    with redirect_stdout(out):
        assert chat_notify.post("ea-assistant", "x")
    assert "[quiet-hours] gate skipped (bad tz)" in out.getvalue()
    assert len(rec.calls) == 1


def test_quiet_hours_flush_calls_through_the_shim_name():
    import quiet_hours as qh
    assert qh.slack_notifier.post_to_slack is slack_notifier.post_to_slack
    assert qh.slack_notifier.SPLIT_MARKER == chat_notify.SPLIT_MARKER


class _FakeZulipClient:
    instances = []
    topics = {"ea": ["alert"], "mine": []}

    def __init__(self, email, api_key, site):
        self.email, self.api_key, self.site = email, api_key, site
        self.sent = []
        _FakeZulipClient.instances.append(self)

    def get_stream_id(self, name):
        return {"result": "success", "stream_id": sorted(self.topics).index(name) + 1}

    def get_stream_topics(self, stream_id):
        name = sorted(self.topics)[stream_id - 1]
        return {"result": "success", "topics": [{"name": n} for n in self.topics[name]]}

    def send_message(self, request):
        self.sent.append(request)
        return {"result": "success", "id": len(self.sent)}


@pytest.fixture
def zulip_env(env, monkeypatch):
    _FakeZulipClient.instances = []
    monkeypatch.setitem(sys.modules, "zulip", types.SimpleNamespace(Client=_FakeZulipClient))
    monkeypatch.setenv("ZULIP_API_KEY", "zkey")
    _set_chat(env, {"frontend": "zulip",
                    "zulip": {"site": "https://zulip.example", "bot_email": "wynn.rasmussen47@example.com",
                              "notify": {"personal": {"stream": "mine"},
                                         "pinned": {"stream": "ea", "topic": "alerts"}}}})
    return env


def test_zulip_post_opens_its_own_topic(zulip_env, monkeypatch):
    rec = _PostRecorder()
    monkeypatch.setattr(requests, "post", rec)
    text = "*New* <https://x.example|link>" + chat_notify.SPLIT_MARKER + "second"
    assert chat_notify.post("personal", text, blocks=[{"type": "divider"}])
    assert rec.calls == []                                  # no webhook
    (client,) = _FakeZulipClient.instances
    assert (client.email, client.api_key, client.site) == \
        ("wynn.rasmussen47@example.com", "zkey", "https://zulip.example")
    assert client.sent == [
        {"type": "stream", "to": "mine", "topic": "New link",
         "content": "**New** [link](https://x.example)"},
        {"type": "stream", "to": "mine", "topic": "New link", "content": "second"},
    ]
    assert chat_notify.post("ea-assistant", "b")
    assert _FakeZulipClient.instances[-1].sent[0] == {"type": "stream", "to": "ea",
                                                      "topic": "b", "content": "b"}
    assert chat_notify.post("pinned", "c")
    assert _FakeZulipClient.instances[-1].sent[0]["topic"] == "alerts"


def test_zulip_unknown_key_warns_and_fails(zulip_env):
    out = io.StringIO()
    with redirect_stdout(out):
        assert chat_notify.post("nope", "x") is False
    assert "No Zulip notify target configured for 'nope'" in out.getvalue()
    assert _FakeZulipClient.instances == []


def test_zulip_missing_key_fails(zulip_env, monkeypatch, tmp_path):
    monkeypatch.delenv("ZULIP_API_KEY")
    monkeypatch.setattr(chat_notify, "EA_DIR", tmp_path / "noea")
    assert chat_notify.post("personal", "x") is False


def test_scheduling_channel_key(env):
    assert chat_notify.scheduling_channel_key() == "ea-assistant"
    env.hooks.write_text(json.dumps({"scheduling": "https://hooks.example/S"}))
    assert chat_notify.scheduling_channel_key() == "scheduling"
    assert slack_notifier.scheduling_channel_key() == "scheduling"
    _set_chat(env, {"frontend": "zulip"})
    assert chat_notify.scheduling_channel_key() == "scheduling"
    _set_chat(env, {"frontend": "zulip",
                    "zulip": {"notify": {"scheduling": {"stream": ""}}}})
    assert chat_notify.scheduling_channel_key() == "ea-assistant"


def test_llm_grants_per_frontend(env):
    grants = chat_notify.llm_tool_grants()
    assert grants.split(",") == ["mcp__claude_ai_Slack__slack_read_channel",
                                 "mcp__claude_ai_Slack__slack_read_thread",
                                 "mcp__claude_ai_Slack__slack_read_user_profile"]
    assert "*" not in grants
    for name in ("zulip", "memory"):
        _set_chat(env, {"frontend": name})
        assert chat_notify.llm_tool_grants() == ""


def test_cli_llm_grants(env):
    out = io.StringIO()
    with redirect_stdout(out):
        assert chat_notify.main(["llm-grants"]) == 0
    assert out.getvalue().strip() == chat_notify.SLACK_LLM_GRANTS


def test_cli_post_memory(env):
    _set_chat(env, {"frontend": "memory"})
    assert chat_notify.main(["post", "--key", "personal", "--text", "hello"]) == 0
    assert chat_notify.MEMORY_POSTS == [("personal", "hello", None)]


def _old_function(source: str, name: str) -> types.FunctionType:
    """One function from an old module's source, compiled alone."""
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {"os": __import__("os")}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), name, "exec"), ns)
    return ns[name]


def test_post_as_bot_matches_scheduled_sends(env, monkeypatch):
    old_post = _old_function(_git_show("preprocessor/scheduled_sends.py"), "_post_slack")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("EA_SLACK_CHANNEL_ID", "C0TEST")
    import scheduled_sends
    rec_old, rec_new = _PostRecorder(), _PostRecorder()
    monkeypatch.setattr(requests, "post", rec_old)
    old_post(":warning: Scheduled send held - x")
    monkeypatch.setattr(requests, "post", rec_new)
    scheduled_sends._post_slack(":warning: Scheduled send held - x")
    assert rec_old.calls == rec_new.calls
    assert rec_new.calls == [(("https://slack.com/api/chat.postMessage",),
                              {"headers": {"Authorization": "Bearer xoxb-test"},
                               "json": {"channel": "C0TEST",
                                        "text": ":warning: Scheduled send held - x"},
                               "timeout": 10})]


def test_post_as_bot_slack_no_token_posts_nothing(env, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    rec = _PostRecorder()
    monkeypatch.setattr(requests, "post", rec)
    reasons = []
    assert chat_notify.post_as_bot("C0TEST", "x", log=reasons.append) is False
    assert rec.calls == [] and reasons


def test_post_as_bot_zulip_uses_ea_assistant_target(zulip_env):
    assert chat_notify.post_as_bot("C_IGNORED", "*alert*")
    (client,) = _FakeZulipClient.instances
    assert client.sent == [{"type": "stream", "to": "ea", "topic": "alert (2)",
                            "content": "**alert**"}]


def test_health_key_uses_the_health_channel_when_configured(monkeypatch):
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "zulip")
    monkeypatch.setattr(chat_notify, "_chat_cfg",
                        lambda: {"zulip": {"notify": {"health": {"stream": "ea-health"}}}})
    assert chat_notify.health_channel_key() == "health"
    assert slack_notifier.health_channel_key() == "health"


def test_health_key_falls_back_to_the_main_channel(monkeypatch):
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "zulip")
    monkeypatch.setattr(chat_notify, "_chat_cfg", lambda: {"zulip": {"notify": {}}})
    assert chat_notify.health_channel_key() == "ea-assistant"
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "slack")
    monkeypatch.setattr(chat_notify, "_load_webhooks", lambda: {})
    assert chat_notify.health_channel_key() == "ea-assistant"
    monkeypatch.setattr(chat_notify, "_load_webhooks", lambda: {"health": "https://hooks.example/H"})
    assert chat_notify.health_channel_key() == "health"


def test_calendar_key_prefers_calendar_then_scheduling_then_main(monkeypatch):
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "zulip")
    cfg = {"zulip": {"notify": {"calendar": {"stream": "ea-scheduling"},
                                "scheduling": {"stream": "ea"}}}}
    monkeypatch.setattr(chat_notify, "_chat_cfg", lambda: cfg)
    assert chat_notify.calendar_channel_key() == "calendar"
    assert slack_notifier.calendar_channel_key() == "calendar"
    del cfg["zulip"]["notify"]["calendar"]
    assert chat_notify.calendar_channel_key() == "scheduling"
    cfg["zulip"]["notify"] = {}
    assert chat_notify.calendar_channel_key() == "ea-assistant"
    monkeypatch.setattr(chat_notify, "frontend_name", lambda: "slack")
    monkeypatch.setattr(chat_notify, "_load_webhooks", lambda: {})
    assert chat_notify.calendar_channel_key() == "ea-assistant"
    monkeypatch.setattr(chat_notify, "_load_webhooks", lambda: {"calendar": "https://hooks.example/C"})
    assert chat_notify.calendar_channel_key() == "calendar"


def test_calendar_reconciler_routes_through_the_calendar_key():
    root = Path(__file__).resolve().parent.parent
    src = (root / "preprocessor/calendar_reconciler.py").read_text()
    assert "calendar_channel_key()" in src and 'post_to_slack("personal"' not in src


def test_the_health_senders_route_through_the_health_key():
    root = Path(__file__).resolve().parent.parent
    for f in ("preprocessor/sentinel.py", "scripts/boundary-audit.py",
              "scripts/outbound-review.py", "scripts/preferences-digest.py",
              "tests/evals/run_full_if_changed.sh"):
        assert "health_channel_key()" in (root / f).read_text(), f
