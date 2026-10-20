#!/usr/bin/env python3
"""Prepare a Zulip realm for the EA, or check that it is prepared.

The script talks to the Zulip REST API as the realm owner. It:

  1. reads the owner's user id (GET /users/me);
  2. creates a generic bot named "EA" (POST /bots), or reuses the owner's
     existing bot with that name, and reads its API key back (GET /bots);
     a bot it creates gets the portrait in assets/tacitus-avatar.png as
     its avatar (PATCH /bots/<id> with the file), and ``--avatar`` gives
     an existing bot the portrait too;
  3. creates the private streams ``ea``, ``ea-scheduling`` and ``ea-dm``
     with the owner and the bot subscribed (POST /users/me/subscriptions),
     makes an existing stream private if it is not, and sets each to allow
     both named topics and "general chat"
     (``topics_policy=allow_empty_topic``): the owner posts in general
     chat and the EA moves each post into a topic of its own;
  4. lifts the realm's message-edit time limit (PATCH /realm with
     ``message_content_edit_limit_seconds="unlimited"``), so the bot can
     rewrite a confirmation card at any age;
  5. prints the ``chat`` block for ea-config.json and the line
     ``ZULIP_API_KEY=<key>`` for ~/.ea/.env.secrets.

Every step is idempotent. The bot key goes to stdout only; the script
writes no file.

``--check`` changes nothing. It verifies the bot, each stream (private,
owner and bot subscribed, topics and general chat both allowed) and the
edit limit, prints OK or FAIL per item and exits 1 when any item fails.

Usage:
  python3 scripts/zulip_bootstrap.py --site http://zulip.localhost:8080 \\
      --owner-email <your gmail> --owner-key <owner api key> [--check | --avatar]

Dependencies: the standard library and requests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import requests

BOT_FULL_NAME = "EA"
BOT_SHORT_NAME = "ea"
GENERIC_BOT = 1
STREAMS = {
    "ea": "EA conversations.",
    "ea-scheduling": "EA scheduling requests.",
    "ea-dm": "The owner's private line to the EA and its confirmation cards.",
}
MAIN_STREAM = "ea"
SCHED_STREAM = "ea-scheduling"
DM_STREAM = "ea-dm"
NOTIFY = {
    "ea-assistant": {"stream": MAIN_STREAM},
    "scheduling": {"stream": MAIN_STREAM},
    "personal": {"stream": MAIN_STREAM},
    # calendar-protection warnings: a scheduling matter, not a conversation
    "calendar": {"stream": SCHED_STREAM},
}
# The stream setting that allows named topics and "general chat" both.
TOPICS_AND_GENERAL_CHAT = "allow_empty_topic"
# Settings that break the model: no general chat, or no named topics.
BAD_TOPICS_POLICIES = {"disable_empty_topic", "empty_topic_only"}
TIMEOUT = 30
# The EA's picture: a public-domain portrait of Tacitus (assets/README.md).
AVATAR = Path(__file__).resolve().parent.parent / "assets" / "tacitus-avatar.png"


class ApiError(RuntimeError):
    """A Zulip API call failed; the message names the call and the reason."""


class Client:
    """A minimal Zulip REST client with HTTP basic auth."""

    def __init__(self, site: str, email: str, key: str) -> None:
        self.base = site.rstrip("/") + "/api/v1"
        self.session = requests.Session()
        self.session.auth = (email, key)
        self.session.headers["User-Agent"] = "ea-zulip-bootstrap/1"

    def call(self, method: str, path: str, **params: Any) -> dict:
        """Call the API; encode lists, dicts and bools as JSON as Zulip expects."""
        data = {
            k: json.dumps(v) if isinstance(v, (list, dict, bool)) else v
            for k, v in params.items()
        }
        url = self.base + path
        try:
            if method == "GET":
                resp = self.session.get(url, params=data, timeout=TIMEOUT)
            else:
                resp = self.session.request(method, url, data=data, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise ApiError(f"{method} {path}: cannot reach {self.base}: {exc}") from exc
        return self._result(method, path, resp)

    def upload(self, method: str, path: str, file: Path) -> dict:
        """Send one file as multipart form data, the way Zulip takes an avatar."""
        url = self.base + path
        try:
            with open(file, "rb") as fh:
                resp = self.session.request(
                    method, url, files={"file": (file.name, fh, "image/png")}, timeout=TIMEOUT)
        except (OSError, requests.RequestException) as exc:
            raise ApiError(f"{method} {path}: cannot send {file.name}: {exc}") from exc
        return self._result(method, path, resp)

    def _result(self, method: str, path: str, resp: requests.Response) -> dict:
        try:
            body = resp.json()
        except ValueError:
            raise ApiError(
                f"{method} {path}: HTTP {resp.status_code}, not JSON: {resp.text[:200]!r}"
            ) from None
        if resp.status_code != 200 or body.get("result") != "success":
            msg = body.get("msg") or body.get("code") or resp.reason
            raise ApiError(f"{method} {path}: HTTP {resp.status_code}: {msg}")
        return body


def owner_profile(client: Client) -> dict:
    me = client.call("GET", "/users/me")
    if not (me.get("is_owner") or me.get("is_admin")):
        raise ApiError(f"{me.get('email')} is not an owner or administrator of this realm")
    return me


def find_bot(client: Client) -> dict | None:
    """The owner's active bot named EA, or None."""
    for bot in client.call("GET", "/bots")["bots"]:
        if bot.get("full_name") == BOT_FULL_NAME:
            return bot
    return None


def user_id_for(client: Client, email: str) -> int:
    return int(client.call("GET", f"/users/{email}")["user"]["user_id"])


def ensure_bot(client: Client) -> tuple[str, str, int, bool]:
    """Return (bot_email, api_key, user_id, created)."""
    bot = find_bot(client)
    created = False
    if bot is None:
        client.call(
            "POST",
            "/bots",
            full_name=BOT_FULL_NAME,
            short_name=BOT_SHORT_NAME,
            bot_type=GENERIC_BOT,
        )
        created = True
        bot = find_bot(client)
        if bot is None:
            raise ApiError("POST /bots succeeded but GET /bots does not list the EA bot")
    email = bot["username"]
    key = bot.get("api_key") or ""
    if not key:
        raise ApiError("GET /bots returned no API key for the EA bot")
    return email, key, user_id_for(client, email), created


def ensure_avatar(client: Client, bot_id: int, created: bool, force: bool) -> str:
    """Give a bot this run created, or any bot with --avatar, the portrait.

    A failed upload is a log line, not an error: the bot works without it.
    """
    if not (created or force):
        return "avatar: kept (--avatar uploads the portrait)"
    if not AVATAR.is_file():
        return f"avatar: not uploaded, {AVATAR} is missing"
    try:
        client.upload("PATCH", f"/bots/{bot_id}", AVATAR)
    except ApiError as exc:
        return f"avatar: not uploaded: {exc}"
    return f"avatar: uploaded {AVATAR.name}"


def stream_id(client: Client, name: str) -> int | None:
    try:
        return int(client.call("GET", "/get_stream_id", stream=name)["stream_id"])
    except ApiError as exc:
        if "HTTP 400" in str(exc):
            return None
        raise


def stream_state(client: Client, name: str) -> tuple[int | None, bool, set[int], str]:
    """Return (stream_id, invite_only, subscriber ids, topics_policy); id
    None if absent."""
    sid = stream_id(client, name)
    if sid is None:
        return None, False, set(), ""
    stream = client.call("GET", f"/streams/{sid}")["stream"]
    members = client.call("GET", f"/streams/{sid}/members")["subscribers"]
    return (sid, bool(stream.get("invite_only")), {int(m) for m in members},
            str(stream.get("topics_policy") or ""))


def ensure_stream(client: Client, name: str, owner_id: int, bot_id: int) -> str:
    """Create or repair one private stream; return a one-line note."""
    sid, private, members, policy = stream_state(client, name)
    notes = []
    if sid is None or not {owner_id, bot_id} <= members:
        client.call(
            "POST",
            "/users/me/subscriptions",
            subscriptions=[{"name": name, "description": STREAMS[name]}],
            invite_only=True,
            history_public_to_subscribers=True,
            principals=[owner_id, bot_id],
            # A Json[str] parameter here: the string goes JSON-quoted.
            topics_policy=json.dumps(TOPICS_AND_GENERAL_CHAT),
        )
        notes.append("created" if sid is None else "subscribed owner and bot")
        sid, private, members, policy = stream_state(client, name)
    if sid is None:
        raise ApiError(f"stream {name!r} does not exist after the subscribe call")
    if not private:
        client.call("PATCH", f"/streams/{sid}", is_private=True)
        notes.append("made private")
    if policy != TOPICS_AND_GENERAL_CHAT:
        client.call("PATCH", f"/streams/{sid}", topics_policy=TOPICS_AND_GENERAL_CHAT)
        notes.append("topics and general chat allowed")
    return f"stream {name} (id {sid}): {', '.join(notes) or 'already set'}"


def realm_edit_settings(client: Client) -> tuple[bool, int | None]:
    """Return (allow_message_editing, edit limit seconds; None is unlimited)."""
    reg = client.call(
        "POST", "/register", event_types=["realm"], fetch_event_types=["realm"]
    )
    queue_id = reg.get("queue_id")
    if queue_id:
        try:
            client.call("DELETE", "/events", queue_id=queue_id)
        except ApiError:
            pass
    return (
        bool(reg.get("realm_allow_message_editing")),
        reg.get("realm_message_content_edit_limit_seconds"),
    )


def ensure_edit_limit(client: Client) -> str:
    allowed, limit = realm_edit_settings(client)
    if allowed and limit is None:
        return "realm message editing: already unlimited"
    client.call(
        "PATCH",
        "/realm",
        allow_message_editing=True,
        # A Json[int | str] parameter: the string goes JSON-quoted.
        message_content_edit_limit_seconds=json.dumps("unlimited"),
    )
    allowed, limit = realm_edit_settings(client)
    if not allowed or limit is not None:
        raise ApiError(
            f"realm edit limit still {limit!r} (editing allowed: {allowed}) after PATCH /realm"
        )
    return "realm message editing: set to unlimited"


def chat_block(site: str, bot_email: str, owner_id: int) -> dict:
    return {
        "chat": {
            "frontend": "zulip",
            "zulip": {
                "site": site.rstrip("/"),
                "bot_email": bot_email,
                "owner_user_id": str(owner_id),
                "stream": MAIN_STREAM,
                "sched_stream": SCHED_STREAM,
                "dm_stream": DM_STREAM,
                "notify": NOTIFY,
            },
        }
    }


def bootstrap(client: Client, site: str, avatar: bool = False) -> int:
    me = owner_profile(client)
    owner_id = int(me["user_id"])
    bot_email, bot_key, bot_id, created = ensure_bot(client)
    log = [
        f"owner: user id {owner_id}",
        f"bot {bot_email} (user id {bot_id}): {'created' if created else 'reused'}",
        ensure_avatar(client, bot_id, created, avatar),
    ]
    for name in STREAMS:
        log.append(ensure_stream(client, name, owner_id, bot_id))
    log.append(ensure_edit_limit(client))
    for line in log:
        print(line, file=sys.stderr)
    print(json.dumps(chat_block(site, bot_email, owner_id), indent=2))
    print(f"ZULIP_API_KEY={bot_key}")
    return 0


def check(client: Client) -> int:
    failures = 0

    def report(ok: bool, item: str, detail: str) -> None:
        nonlocal failures
        failures += not ok
        print(f"{'OK  ' if ok else 'FAIL'} {item}: {detail}")

    me = owner_profile(client)
    owner_id = int(me["user_id"])
    report(True, "owner", f"user id {owner_id}")
    bot = find_bot(client)
    bot_id = None
    if bot is None:
        report(False, "bot", f"no active bot named {BOT_FULL_NAME!r} owned by the owner")
    else:
        bot_id = user_id_for(client, bot["username"])
        report(bool(bot.get("api_key")), "bot", f"{bot['username']} (user id {bot_id})")
    for name in STREAMS:
        sid, private, members, policy = stream_state(client, name)
        if sid is None:
            report(False, f"stream {name}", "does not exist")
            continue
        missing = [
            who
            for who, uid in (("owner", owner_id), ("bot", bot_id))
            if uid is None or uid not in members
        ]
        problems = ([] if private else ["not private"]) + [f"{w} not subscribed" for w in missing]
        if policy in BAD_TOPICS_POLICIES:
            problems.append(f"topics_policy {policy}: needs named topics and general chat")
        report(
            not problems,
            f"stream {name}",
            f"id {sid}, " + ("; ".join(problems) if problems
                             else f"private, owner and bot subscribed, topics_policy {policy}"),
        )
    allowed, limit = realm_edit_settings(client)
    report(
        allowed and limit is None,
        "edit limit",
        "unlimited" if allowed and limit is None else f"editing allowed {allowed}, limit {limit!r} s",
    )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--site", required=True, help="realm URL, e.g. http://zulip.localhost:8080")
    ap.add_argument("--owner-email", required=True, help="the realm owner's login email")
    ap.add_argument("--owner-key", required=True, help="the realm owner's API key")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify only; change nothing")
    mode.add_argument("--avatar", action="store_true",
                      help="give an existing bot the portrait in assets/tacitus-avatar.png")
    args = ap.parse_args(argv)
    client = Client(args.site, args.owner_email, args.owner_key)
    try:
        return check(client) if args.check else bootstrap(client, args.site, args.avatar)
    except ApiError as exc:
        print(f"zulip_bootstrap: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
