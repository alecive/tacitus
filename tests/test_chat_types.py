"""Tests for the chat package's types, text helpers, config and factory."""
from __future__ import annotations

import json
import os
import pwd
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import ea_config  # noqa: E402
from chat import ChatError, load_frontend, private_dir, prompt_strings_for_config  # noqa: E402
from chat.memory_frontend import MemoryFrontend  # noqa: E402
from chat.text import (  # noqa: E402
    EMOJI_ALIASES,
    is_ported,
    mrkdwn_to_zulip,
    ported_header,
    smart_chunk,
    topic_from_text,
    unique_topic,
)
from chat.types import THREAD_KEY_RE, Status  # noqa: E402


def _old_smart_chunk():
    """The bot's own _smart_chunk, exec'd from its source."""
    src = (ROOT / "ea-chat-bot.py").read_text()
    m = re.search(r"^def _smart_chunk\(.*?(?=^\S)", src, re.S | re.M)
    assert m, "_smart_chunk not found in ea-chat-bot.py"
    ns: dict = {}
    exec(compile(m.group(0), "ea-chat-bot.py[_smart_chunk]", "exec"), ns)
    return ns["_smart_chunk"]


@pytest.mark.parametrize("key", ["1712345678.123456", "stream:12:topic:Hello%20there",
                                 "dm:5:c3", "m1"])
def test_thread_key_accepts(key):
    assert THREAD_KEY_RE.match(key)


@pytest.mark.parametrize("key", ["a b", "x]y", "", ":lead", "a" * 401, "a\n"])
def test_thread_key_rejects(key):
    assert not THREAD_KEY_RE.fullmatch(key)


_CHUNK_CASES = [
    ("short text", 3000),
    ("", 10),
    ("para one\n\npara two\n\npara three " * 40, 300),
    ("line\n" * 500, 97),
    ("word " * 2000, 250),
    ("x" * 7001, 3000),
    ("abc\n\n" + "y" * 40 + " tail\nmore", 20),
    ("  lead\n\n  spaced   \n\n" * 30, 50),
]


@pytest.mark.parametrize("text,limit", _CHUNK_CASES)
def test_smart_chunk_matches_bot(text, limit):
    assert smart_chunk(text, limit) == _old_smart_chunk()(text, limit)


def test_smart_chunk_respects_limit():
    for text, limit in _CHUNK_CASES:
        assert all(len(c) <= limit for c in smart_chunk(text, limit))


@pytest.mark.parametrize("src,want", [
    ("*bold*", "**bold**"),
    ("a *b c* d", "a **b c** d"),
    ("_it_", "*it*"),
    ("~gone~", "~~gone~~"),
    ("see <https://x.org/a_b_c|the page>", "see [the page](https://x.org/a_b_c)"),
    ("<https://x.org/a_b_c>", "https://x.org/a_b_c"),
    ("in <#C0EXAMPLE01>", "in #C0EXAMPLE01"),
    ("in <#C0EXAMPLE01|ea>", "in #ea"),
    ("hi <@U123ABC>", "hi @**U123ABC**"),
    ("a &amp; b &lt;c&gt;", "a & b <c>"),
    ("&amp;lt;", "&lt;"),
    ("done :white_check_mark: :x: :tada:", "done :check: :cross_mark: :tada:"),
    ("run `*not bold*` now", "run `*not bold*` now"),
    ("```\n*keep* _this_ <https://a|b>\n```\n*x*", "```\n*keep* _this_ <https://a|b>\n```\n**x**"),
    ("snake_case_name stays", "snake_case_name stays"),
    ("2*3*4", "2*3*4"),
    ("**already**", "**already**"),
    ("", ""),
])
def test_mrkdwn_to_zulip(src, want):
    assert mrkdwn_to_zulip(src) == want


def test_mrkdwn_channel_mention_function():
    out = mrkdwn_to_zulip("go to <#C1> and <#C2|two>", mention=lambda c: f"#**room-{c}**")
    assert out == "go to #**room-C1** and #**room-C2**"


def test_emoji_aliases_known():
    assert EMOJI_ALIASES["white_check_mark"] == "check"
    assert EMOJI_ALIASES["mailbox_with_no_mail"] == "inbox_zero"
    assert EMOJI_ALIASES["ballot_box_with_check"] == "checkbox"
    assert EMOJI_ALIASES["clock3"] == "clock"


def test_emoji_aliases_convert_once():
    # No target is also a key, so converting a converted message is a no-op.
    # The exception is x: Slack's x is set to cross_mark, and x is the only
    # Zulip name for the boxed cross negative_squared_cross_mark maps to.
    assert set(EMOJI_ALIASES.values()) & set(EMOJI_ALIASES) == {"x"}
    once = mrkdwn_to_zulip(":mailbox_with_no_mail: :pushpin: :ok_hand:")
    assert once == ":inbox_zero: :push_pin: :ok:"
    assert mrkdwn_to_zulip(once) == once


@pytest.mark.parametrize("text,limit,want", [
    ("Hello there", 60, "Hello there"),
    ("\n\n  *Draft* reply to _Maria_ :email:\nsecond line", 60, "Draft reply to Maria"),
    ("see <https://x.org|the page> now", 60, "see the page now"),
    ("one two three four five", 12, "one two"),
    ("abcdefghijklmnop", 5, "abcde"),
    ("", 60, "chat"),
    (":lock: *", 60, "chat"),
    ("keep snake_case words", 60, "keep snake_case words"),
    ("[receipt.pdf](/user_uploads/2/ab/x/receipt.pdf)", 60, "receipt.pdf"),
    ("@**Hale Okonkwo|12** can you check **this**", 60, "Hale Okonkwo can you check this"),
    ("> quoted opening\nmore", 60, "quoted opening"),
    ("Stage the items for this trip, which should be two PDFs", 32, "Stage the items for this trip"),
])
def test_topic_from_text(text, limit, want):
    assert topic_from_text(text, limit) == want


def test_unique_topic_suffixes_a_name_in_use():
    assert unique_topic("usage", []) == "usage"
    assert unique_topic("usage", ["Usage"]) == "usage (2)"
    assert unique_topic("usage", ["usage", "usage (2)"]) == "usage (3)"
    long = "x" * 60
    got = unique_topic(long, [long], 60)
    assert len(got) == 60 and got.endswith(" (2)")


def test_ported_header_marks_only_its_own_first_line():
    head = ported_header("Kit", "2026-06-12 09:30")
    assert head == "**Kit** · 2026-06-12 09:30 · from Slack"
    assert is_ported(head) and is_ported(head + "\nbody")
    assert not is_ported("body\n" + head)
    assert not is_ported("**Kit** · 2026-06-12 09:30\nbody")
    assert not is_ported("")


def test_status_values():
    assert [s.value for s in Status] == [
        "eyes", "hourglass_flowing_sand", "white_check_mark", "x",
        "warning", "double_vertical_bar", "question"]
    assert Status.DONE == "white_check_mark"


def test_get_chat_defaults_missing_file(tmp_path):
    cfg = ea_config.get_chat(tmp_path / "none.json")
    assert cfg == ea_config.DEFAULTS["chat"]
    assert cfg["frontend"] == "slack"
    assert cfg["zulip"]["dm_stream"] == "ea-dm"
    cfg["zulip"]["notify"]["personal"]["stream"] = "mutated"
    assert ea_config.DEFAULTS["chat"]["zulip"]["notify"]["personal"]["stream"] == "ea"


def test_get_chat_deep_merge(tmp_path):
    path = tmp_path / "ea-config.json"
    path.write_text(json.dumps({"chat": {
        "frontend": "zulip",
        "zulip": {"site": "https://z.example", "stream": "ea2",
                  "notify": {"personal": {"stream": "mine"},
                             "extra": {"stream": "s"}}}}}))
    cfg = ea_config.get_chat(path)
    assert cfg["frontend"] == "zulip"
    z = cfg["zulip"]
    assert z["site"] == "https://z.example"
    assert z["stream"] == "ea2"
    assert z["dm_stream"] == "ea-dm"
    assert z["bot_email"] == ""
    assert z["notify"]["personal"] == {"stream": "mine"}
    assert z["notify"]["scheduling"] == {"stream": "ea"}
    assert z["notify"]["extra"] == {"stream": "s"}


def test_template_has_chat_block():
    tpl = json.loads((ROOT / "config" / "ea-config.json").read_text())
    assert tpl["chat"] == ea_config.DEFAULTS["chat"]


def _ea_dir(tmp_path, chat):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ea-config.json").write_text(json.dumps({"chat": chat}))
    return tmp_path


def test_load_frontend_memory(tmp_path):
    fe = load_frontend(ea_dir=_ea_dir(tmp_path, {"frontend": "memory"}), env={})
    assert isinstance(fe, MemoryFrontend)
    assert fe.name == "memory"
    assert fe.supports("codes") and not fe.supports("home")
    assert prompt_strings_for_config(tmp_path)["platform"]


def test_load_frontend_memory_config(tmp_path):
    fe = load_frontend(ea_dir=_ea_dir(tmp_path, {
        "frontend": "memory",
        "memory": {"owner_id": "U9", "card_registry": str(tmp_path / "priv" / "cards.json")}}),
        env={})
    assert fe.owner_id == "U9"
    assert fe.card_registry is not None


def test_load_frontend_unknown(tmp_path):
    with pytest.raises(ChatError, match="unknown chat frontend 'irc'"):
        load_frontend(ea_dir=_ea_dir(tmp_path, {"frontend": "irc"}), env={})
    with pytest.raises(ChatError):
        prompt_strings_for_config(tmp_path)


def test_private_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("EA_PRIVATE_DIR", str(tmp_path / "p"))
    assert private_dir() == tmp_path / "p"
    assert not (tmp_path / "p").exists()
    monkeypatch.delenv("EA_PRIVATE_DIR")
    # owner_trust.private_dir: `.ea-private` in the euid's passwd home
    assert private_dir() == Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".ea-private"


def test_feature_chat_aliases_slack(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ea_config.py", "feature", "chat"])
    monkeypatch.setattr(ea_config, "profile_features",
                        lambda *a, **k: {"slack": False, "buttons": True})
    assert ea_config._main() == 1
    monkeypatch.setattr(ea_config, "profile_features",
                        lambda *a, **k: {"slack": True, "buttons": False})
    assert ea_config._main() == 0


def test_load_frontend_slack_until_installed(tmp_path):
    import importlib.util
    if importlib.util.find_spec("chat.slack_frontend") is not None:
        pytest.skip("slack_frontend is installed")
    with pytest.raises(ChatError, match="frontend 'slack' is not installed yet"):
        load_frontend(ea_dir=_ea_dir(tmp_path, {"frontend": "slack"}), env={})
