#!/usr/bin/env python3
"""End-to-end check of the Zulip frontend against a live server.

The script runs the frontend in-process as the bot, with a recording
handler registry and the event loop in a thread, and drives it from the
owner's side with a second zulip.Client. It checks, printing PASS, FAIL
or SKIP per item:

   1. inbound: an owner message in a fresh topic yields an InboundMessage,
      and an owner message in general chat is moved into a topic of its
      own and arrives as that thread's first message;
   2. reply: the bot's reply lands in that topic with converted markup
      (read back with GET /api/v1/messages/{id}?apply_markdown=true);
   3. reactions: every Status resolves to an emoji name (table printed);
   4. files: upload into the topic and download it back;
   5. rooms: create, invite, describe, archive and unarchive a private
      stream `ea-smoke-<ts>`, seen in archived_map both ways;
   6. permalink: the constructed URL names the message's stream, topic
      and id, and the message exists;
   7. cards in ea-dm: one confirmed by a check reaction, one by
      `cancel <code>`, one by the bare code, and a code posted by the bot
      itself ignored;
   8. queue resume: a message posted while the frontend is stopped
      arrives after a restart on the same saved queue.

Exit status is 1 when any item fails. `--dry-run` only parses the
arguments. The private directory is a fresh temporary directory, so the
smoke run never touches the live card registry or queue file.

Usage:
  .venv/bin/python3 scripts/zulip_smoke.py --site http://zulip.localhost:8080 \\
      --bot-email ea-bot@zulip.localhost --bot-key <key> \\
      --owner-email <your gmail> --owner-key <key> [--stream ea] [--dm-stream ea-dm]
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

from chat.base import NotSupported  # noqa: E402
from chat.cards import CardAction, CardField, ConfirmationCard  # noqa: E402
from chat.types import ActionEvent, InboundMessage, Status  # noqa: E402
from chat.zulip_frontend import ZulipFrontend, thread_key_for  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--site", required=True)
    p.add_argument("--bot-email", required=True)
    p.add_argument("--bot-key", required=True)
    p.add_argument("--owner-email", required=True)
    p.add_argument("--owner-key", required=True)
    p.add_argument("--stream", default="ea")
    p.add_argument("--dm-stream", default="ea-dm")
    p.add_argument("--timeout", type=float, default=20.0,
                   help="seconds to wait for each event (default 20)")
    p.add_argument("--dry-run", action="store_true",
                   help="parse the arguments and exit")
    return p.parse_args(argv)


def record(item: str, status: str, detail: str = "") -> None:
    RESULTS.append((item, status, detail))
    print(f"{status:4} {item}" + (f": {detail}" if detail else ""), flush=True)


def check(item: str, ok: bool, detail: str = "") -> bool:
    record(item, "PASS" if ok else "FAIL", detail)
    return ok


class Recorder:
    """Collects everything the frontend dispatches."""

    def __init__(self) -> None:
        self.messages: list[InboundMessage] = []
        self.actions: list[ActionEvent] = []
        self.events: list[object] = []
        self.lock = threading.Lock()

    def attach(self, fe: ZulipFrontend) -> None:
        fe.registry.event("message")(self._msg)
        fe.registry.action(re.compile(".*"))(self._act)
        fe.registry.event(re.compile(".*"))(self._ev)

    def _msg(self, m: InboundMessage) -> None:
        with self.lock:
            self.messages.append(m)

    def _act(self, a: ActionEvent) -> None:
        with self.lock:
            self.actions.append(a)

    def _ev(self, e: object) -> None:
        with self.lock:
            self.events.append(e)


def wait_for(pred: Callable[[], object], timeout: float) -> object:
    end = time.time() + timeout
    while time.time() < end:
        got = pred()
        if got:
            return got
        time.sleep(0.2)
    return None


def start_loop(fe: ZulipFrontend) -> threading.Thread:
    t = threading.Thread(target=fe.run_forever, name="zulip-loop", daemon=True)
    t.start()
    return t


def stop_loop(fe: ZulipFrontend, t: threading.Thread, owner, stream: str) -> None:
    """Stop the loop; a message wakes the pending long poll so it returns."""
    fe.stop()
    owner.send_message({"type": "stream", "to": stream, "topic": "smoke wake",
                        "content": "wake"})
    t.join(timeout=100)


def smoke_card(title: str, subject: str) -> ConfirmationCard:
    return ConfirmationCard(
        kind="send", title=title,
        fields=[CardField("To", "someone@example.org"), CardField("Subject", subject)],
        body="Smoke test body.\n\nSecond paragraph.",
        actions=[CardAction("approve_send", "Send", {"draft_id": "smoke"}, style="primary",
                            word="send"),
                 CardAction("cancel_send", "Cancel", "smoke", style="danger", word="cancel"),
                 CardAction("draft_handled", "Handled", "smoke", word="handled")])


def decode_hash(part: str) -> str:
    return urllib.parse.unquote(part.replace(".", "%"))


def run(a: argparse.Namespace) -> int:
    import zulip

    owner = zulip.Client(email=a.owner_email, api_key=a.owner_key, site=a.site)
    prof = owner.get_profile()
    if prof.get("result") != "success":
        record("owner profile", "FAIL", str(prof))
        return 1
    owner_id = str(prof["user_id"])
    priv = Path(tempfile.mkdtemp(prefix="zulip-smoke-"))
    cfg = {"site": a.site, "bot_email": a.bot_email, "owner_user_id": owner_id,
           "stream": a.stream, "sched_stream": "", "dm_stream": a.dm_stream}
    ts = time.strftime("%Y%m%d-%H%M%S")
    rec = Recorder()
    fe = ZulipFrontend(cfg, api_key=a.bot_key, private_dir=priv)
    try:
        fe.start()
    except Exception as e:
        record("start", "FAIL", str(e))
        return 1
    record("start", "PASS", f"main={fe.main_channel} dm={fe.dm_channel(owner_id)} "
           f"limits={tuple(fe.limits)}")
    rec.attach(fe)
    loop = start_loop(fe)
    T = a.timeout

    def owner_get(mid: int, markdown: bool) -> dict:
        res = owner.call_endpoint(url=f"messages/{mid}", method="GET",
                                  request={"apply_markdown": markdown})
        return res.get("message") or {}

    # 1. inbound
    topic = f"smoke {ts}"
    sent = owner.send_message({"type": "stream", "to": a.stream, "topic": topic,
                               "content": f"smoke inbound {ts}"})
    mid = str(sent.get("id"))
    got = wait_for(lambda: next((m for m in rec.messages if m.msg_id == mid), None), T)
    key = thread_key_for(int(fe.main_channel.split(":")[1]), topic)
    check("inbound message", bool(got) and got.thread_key == key and not got.is_reply
          and got.user_id == owner_id,
          f"thread_key={getattr(got, 'thread_key', None)} is_reply={getattr(got, 'is_reply', None)}")
    gc_topic = f"smoke general chat {ts}"
    gc = owner.send_message({"type": "stream", "to": a.stream, "topic": "", "content": gc_topic})
    gc_id = str(gc.get("id"))
    got = wait_for(lambda: next((m for m in rec.messages if m.msg_id == gc_id), None), T)
    moved_to = owner_get(int(gc_id), False).get("subject") if got else None
    check("general chat post gets its own topic",
          bool(got) and not got.is_reply and moved_to == gc_topic
          and got.thread_key == thread_key_for(int(fe.main_channel.split(":")[1]), gc_topic),
          f"thread_key={getattr(got, 'thread_key', None)} topic={moved_to!r}")

    # 2. reply with converted markup
    ref = None
    try:
        ref = fe.post(fe.main_channel, "*bold* _it_ ~gone~ <https://example.org|a link> "
                      ":white_check_mark:", thread_ts=key)
        m = owner_get(int(ref.id), True)
        html = str(m.get("content", ""))
        wants = ["<strong>bold</strong>", "<em>it</em>", "<del>gone</del>",
                 'href="https://example.org"', "emoji"]
        missing = [w for w in wants if w not in html]
        check("reply markup", m.get("subject") == topic and not missing,
              f"missing={missing}" if missing else html[:160])
    except Exception as e:
        record("reply markup", "FAIL", repr(e))

    # 3. status reactions
    table, bad = [], []
    for st in Status:
        ok = fe.react(fe.main_channel, mid, st)
        name = fe._status_names.get(st.value, "")
        off = fe.unreact(fe.main_channel, mid, st) if ok else False
        table.append((st.name, st.value, name or "-", ok, off))
        if not (ok and off):
            bad.append(st.name)
    print("     status     slack name              zulip name     add  remove")
    for n, v, z, ok, off in table:
        print(f"     {n:10} {v:23} {z:14} {ok!s:5} {off!s}")
    check("status reactions", not bad, f"unresolved={bad}" if bad else "")

    # 4. files
    tmp = priv / "upload"
    tmp.mkdir()
    src = tmp / "smoke.txt"
    src.write_bytes(f"smoke file {ts}\n".encode())
    try:
        fref = fe.upload(fe.main_channel, src, thread_ts=key, title="smoke file",
                         comment="upload check")
        path = fe.download(fref, priv / "download")
        check("file upload and download",
              bool(path) and Path(path).read_bytes() == src.read_bytes(), fref.url)
    except Exception as e:
        record("file upload and download", "FAIL", repr(e))

    # 5. rooms
    room_name = f"ea-smoke-{ts}"
    try:
        room = fe.create_private_room(room_name, "smoke room")
        fe.invite(room.id, owner_id)
        fe.set_description(room.id, "smoke room, described")
        info = fe.channel_info(room.id)
        check("room create, invite, describe", info.is_private and info.is_member
              and info.name == room_name, room.url)
        fe.archive(room.id)
        amap = fe.archived_map()
        check("room archive", amap.get(room.id) is True, f"archived_map[{room.id}]={amap.get(room.id)}")
        try:
            fe.unarchive(room.id)
            amap = fe.archived_map()
            check("room unarchive", amap.get(room.id) is False,
                  f"archived_map[{room.id}]={amap.get(room.id)}")
            fe.archive(room.id)
        except NotSupported as e:
            record("room unarchive", "SKIP", f"NotSupported: {e}")
    except Exception as e:
        record("rooms", "FAIL", repr(e))

    # 6. permalink
    link = fe.permalink(fe.main_channel, mid)
    mm = re.search(r"#narrow/channel/(\d+)-[^/]+/topic/([^/]+)/near/(\d+)$", link)
    exists = bool(owner_get(int(mid), False))
    check("permalink", bool(mm) and mm.group(1) == fe.main_channel.split(":")[1]
          and decode_hash(mm.group(2)) == topic and mm.group(3) == mid and exists, link)

    # 7. cards in ea-dm
    dm = fe.dm_channel(owner_id)

    def action_for(card_id: str, source: str):
        return lambda: next((x for x in rec.actions if x.card.id == card_id
                             and x.source == source), None)

    try:
        c1 = fe.post_card(dm, smoke_card("Smoke card one", f"reaction {ts}"))
        owner.add_reaction({"message_id": int(c1.id), "emoji_name": "check"})
        ev = wait_for(action_for(c1.id, "reaction"), T)
        check("card by reaction", bool(ev) and ev.action_id == "approve_send",
              getattr(ev, "action_id", "no event"))
        fe.replace_card_with_status(c1, "Sent (smoke)")

        c2 = fe.post_card(dm, smoke_card("Smoke card two", f"cancel code {ts}"))
        code2 = fe.last_card_row["code"]
        r = owner.send_message({"type": "stream", "to": a.dm_stream,
                                "topic": fe._msg_topics[int(c2.id)][1],
                                "content": f"cancel {code2}"})
        ev = wait_for(action_for(c2.id, "code"), T)
        reacted = wait_for(lambda: owner_get(int(r["id"]), False).get("reactions"), T)
        consumed = not any(m.msg_id == str(r["id"]) for m in rec.messages)
        check("card by `cancel <code>`", bool(ev) and ev.action_id == "cancel_send"
              and bool(reacted) and consumed,
              f"action={getattr(ev, 'action_id', None)} reacted={bool(reacted)} consumed={consumed}")
        fe.replace_card_with_status(c2, "Cancelled (smoke)")

        c3 = fe.post_card(dm, smoke_card("Smoke card three", f"bare code {ts}"))
        code3 = fe.last_card_row["code"]
        owner.send_message({"type": "stream", "to": a.dm_stream,
                            "topic": fe._msg_topics[int(c3.id)][1], "content": code3})
        ev = wait_for(action_for(c3.id, "code"), T)
        check("card by bare code", bool(ev) and ev.action_id == "approve_send",
              getattr(ev, "action_id", "no event"))
        fe.replace_card_with_status(c3, "Sent (smoke)")

        c4 = fe.post_card(dm, smoke_card("Smoke card four", f"bot code {ts}"))
        code4 = fe.last_card_row["code"]
        own = fe.post(dm, f"send {code4}", thread_ts=c4.thread_key)
        seen = wait_for(lambda: any(m.msg_id == own.id for m in rec.messages), T)
        fired = any(x.card.id == c4.id for x in rec.actions)
        row = fe.card_registry.by_ref(c4.channel, c4.id) or {}
        check("bot-authored code ignored", bool(seen) and not fired
              and row.get("status") == "live",
              f"echo_seen={bool(seen)} fired={fired} status={row.get('status')}")
        fe.replace_card_with_status(c4, "Cancelled (smoke)")
    except Exception as e:
        record("cards", "FAIL", repr(e))

    # 8. queue resume
    old_queue = fe.queue_id
    stop_loop(fe, loop, owner, a.stream)
    gap = owner.send_message({"type": "stream", "to": a.stream, "topic": topic,
                              "content": f"sent while stopped {ts}"})
    rec2 = Recorder()
    fe2 = ZulipFrontend(cfg, api_key=a.bot_key, private_dir=priv)
    fe2.start()
    rec2.attach(fe2)
    loop2 = start_loop(fe2)
    got = wait_for(lambda: next((m for m in rec2.messages if m.msg_id == str(gap["id"])), None), T)
    check("queue resume", bool(got) and fe2.queue_id == old_queue,
          f"queue {old_queue} -> {fe2.queue_id}, message={'yes' if got else 'no'}")
    stop_loop(fe2, loop2, owner, a.stream)

    shutil.rmtree(priv, ignore_errors=True)
    fails = [r for r in RESULTS if r[1] == "FAIL"]
    print(f"\n{len(RESULTS) - len(fails)} of {len(RESULTS)} items passed or skipped; "
          f"{len(fails)} failed")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    if a.dry_run:
        print(f"dry run: site={a.site} bot={a.bot_email} owner={a.owner_email} "
              f"stream={a.stream} dm_stream={a.dm_stream}")
        return 0
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
