#!/usr/bin/env python3
"""zulip_migrate_rooms: carry the Slack project rooms over to Zulip.

Every live project room in ~/.ea/config/ea-channels.json (kind project, a
session id, not archived, a Slack channel id) gets a private Zulip stream
of the same name with the owner and the bot subscribed, and a registry row
keyed stream:<id> that carries the SAME session id, model and effort. The
room's specialist therefore resumes on Zulip with all of its history. The
Slack rows stay untouched, so switching back to Slack needs nothing.

Runs as the owner with the bot's API key in ZULIP_API_KEY (or found by the
owner_trust rule: the owner-private secrets.env, then a checked legacy
~/.ea/.env.secrets). Prints the plan; --apply performs it. Idempotent: a
project that already has a stream row is skipped, and an existing stream
of the same name is reused.

    scripts/zulip_migrate_rooms.py --site https://host:8443 [--apply]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))

EA_DIR = Path(os.environ.get("EA_DIR", "/home/ea-bot/.ea"))
REGISTRY = EA_DIR / "config" / "ea-channels.json"
CONFIG = EA_DIR / "config" / "ea-config.json"


def bot_key() -> str:
    """ZULIP_API_KEY by the owner_trust rule."""
    import owner_trust
    return owner_trust.owner_secret("ZULIP_API_KEY")


def api(site: str, auth: tuple, method: str, path: str, **data) -> dict:
    # GET parameters travel in the query string; everything else as a form body
    kw = {"params": data} if method == "GET" else {"data": data or None}
    r = requests.request(method, f"{site}/api/v1/{path}", auth=auth, timeout=30, **kw)
    try:
        body = r.json()
    except ValueError:
        body = {"result": "error", "msg": r.text[:200]}
    return body


def stream_id(site: str, auth: tuple, name: str) -> int | None:
    res = api(site, auth, "GET", "get_stream_id", stream=name)
    return int(res["stream_id"]) if res.get("result") == "success" else None


def slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name).strip("-").lower()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True, help="the Zulip site URL the bot uses (https://host:port)")
    ap.add_argument("--apply", action="store_true", help="create streams and registry rows (default: plan only)")
    a = ap.parse_args()
    site = a.site.rstrip("/")
    cfg = json.loads(CONFIG.read_text()).get("chat", {}).get("zulip", {})
    bot_email, owner_id = cfg.get("bot_email", ""), str(cfg.get("owner_user_id", ""))
    key = bot_key()
    if not (bot_email and owner_id and key):
        print("need chat.zulip.bot_email and owner_user_id in the live config and ZULIP_API_KEY", file=sys.stderr)
        return 2
    auth = (bot_email, key)
    me = api(site, auth, "GET", "users/me")
    if me.get("result") != "success":
        print(f"the bot key does not authenticate against {site}: {me.get('msg')}", file=sys.stderr)
        return 2
    # principals must be all ids or all emails; ids for both
    principals = json.dumps([int(me["user_id"]), int(owner_id)])
    from preprocessor import ea_channels as ch
    rows = ch.load(REGISTRY)
    done_projects = {r.get("project") for cid, r in rows.items()
                     if cid.startswith("stream:") and r.get("kind") == "project"}
    todo = [(cid, r) for cid, r in rows.items()
            if r.get("kind") == "project" and r.get("session_id") and not r.get("archived")
            and not cid.startswith("stream:") and r.get("project") not in done_projects]
    print(f"{len(todo)} room(s) to carry over, {len(done_projects)} already on Zulip")
    for cid, r in todo:
        print(f"  {r.get('name', ''):32} project={r.get('project', ''):28} session={r['session_id'][:8]} model={r.get('model', '')}")
    if not a.apply:
        print("plan only; add --apply to create the streams and registry rows")
        return 0
    made = 0
    for cid, r in todo:
        name = (r.get("name") or f"ea-{r.get('project')}").lstrip("#")
        sid = stream_id(site, auth, name)
        if sid is None:
            res = api(site, auth, "POST", "users/me/subscriptions",
                      subscriptions=json.dumps([{"name": name, "description": f"EA room for {r.get('project')}"}]),
                      invite_only="true", history_public_to_subscribers="true",
                      principals=principals)
            if res.get("result") != "success":
                print(f"  FAIL {name}: {res.get('msg')}")
                continue
            sid = stream_id(site, auth, name)
        else:
            api(site, auth, "POST", "users/me/subscriptions",
                subscriptions=json.dumps([{"name": name}]), principals=principals)
        if sid is None:
            print(f"  FAIL {name}: no stream id after creation")
            continue
        new_id = f"stream:{sid}"
        url = f"{site}/#narrow/channel/{sid}-{slug(name)}"
        ch.register(new_id, r.get("project", ""), name, kind="project", session_id=r["session_id"], url=url,
                    model=r.get("model", ""), effort=r.get("effort", ""), created_by="zulip-migration", path=REGISTRY)
        print(f"  ok   {name} -> {new_id} (session {r['session_id'][:8]} kept)")
        made += 1
    print(f"{made} room(s) migrated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
