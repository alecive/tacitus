"""The web chat relay: the crypto, the relay's routing rules, and a loopback
run of the whole path (docs/webchat-relay.md).

The loopback run starts a fake web chat API (a few routes and an event
stream) on an ephemeral port, the relay server on another, and the bot's
relay client between them; a phone made of relay_crypto calls pairs,
sends requests through the relay and reads an event. Every frame the
relay handles is tapped, and no plaintext marker appears in them, in its
log lines or in what it holds.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "botcore"))
sys.path.insert(0, str(ROOT / "relay"))

pytest.importorskip("cryptography")

import relay_server as rs  # noqa: E402
from botcore.chat import relay_client as rcl  # noqa: E402
from botcore.chat import relay_crypto as rc  # noqa: E402

PAIR_TOKEN = "pair-token-for-tests"
REPLY_MARKER = "relay-reply-marker-7f3a2b"
EVENT_MARKER = "relay-event-marker-91c2d4"
REQUEST_MARKER = "relay-request-marker-55e1aa"
FILE_MARKER = b"relay-file-marker-0c9e33"
MARKERS = [REPLY_MARKER.encode(), EVENT_MARKER.encode(), REQUEST_MARKER.encode(), FILE_MARKER]
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _needles(marker: bytes) -> list[bytes]:
    """The marker and the part of its base64 that shows at any alignment,
    in both alphabets: a body sent unsealed but base64-encoded is caught."""
    out = [marker]
    for k in range(3):
        core = base64.b64encode(b"\x00" * k + marker)[4:-4]
        out += [core, core.replace(b"+", b"-").replace(b"/", b"_")]
    return out


def _leaks(blob: bytes) -> list[bytes]:
    return [n for m in MARKERS for n in _needles(m) if n in blob]


# ── crypto ────────────────────────────────────────────────────────────

def _pair(ident=None, psk_ok=True):
    """A bot identity, a pairing, and a hello/welcome round: (bot session,
    phone session, accepted)."""
    ident = ident or rc.BotIdentity.generate()
    payload = rc.new_pairing(ident, "https://relay.example.net")
    device = rc.generate_private()
    hello, pending = rc.client_hello(payload.bot_public, payload.install_id, device,
                                     pairing=(payload.pairing_id, payload.psk),
                                     info={"name": "test phone"})
    acc = rc.bot_accept(ident, hello, psk_for=lambda pid: payload.psk if psk_ok else None,
                        seen=rc.HelloCache().seen)
    return acc.session, rc.client_finish(pending, acc.welcome), acc


def test_handshake_round_trip_both_directions():
    bot, phone, acc = _pair()
    assert bot.sid == phone.sid and len(bot.sid) == 32
    assert acc.info["name"] == "test phone" and acc.pairing_id
    assert phone.device == bot.device == rc.device_id_for(acc.device_public)
    req = phone.seal("req", "r1", b'{"m":"GET","p":"/api/me"}')
    assert bot.open(req) == b'{"m":"GET","p":"/api/me"}'
    res = bot.seal("res", "r1", b"answer")
    assert phone.open(res) == b"answer"
    # Each side sends only its own kinds.
    with pytest.raises(rc.FrameError):
        phone.seal("res", "r2", b"x")
    with pytest.raises(rc.FrameError):
        bot.open(bot.seal("res", "r3", b"x"))


def test_replayed_frame_is_refused_and_reordering_is_not():
    bot, phone, _ = _pair()
    f0, f1, f2 = (phone.seal("req", f"r{i}", b"x") for i in range(3))
    assert bot.open(f2) == b"x" and bot.open(f0) == b"x"  # out of order is fine
    with pytest.raises(rc.ReplayError):
        bot.open(f2)
    with pytest.raises(rc.ReplayError):
        bot.open(f0)
    assert bot.open(f1) == b"x"
    # A counter older than the window is refused.
    w = rc.ReplayWindow(size=8)
    w.mark(20)
    assert w.seen(12) and not w.seen(13) and w.seen(20) and not w.seen(21)


def test_frame_bound_to_its_id_kind_and_session():
    bot, phone, _ = _pair()
    f = phone.seal("req", "r1", b"secret body")
    for change in ({"id": "r2"}, {"k": "ping"}, {"n": f["n"] + 1}):
        with pytest.raises(rc.FrameError):
            bot.open({**f, **change})
    raw = bytearray(rc.b64d(f["c"]))
    raw[5] ^= 1
    with pytest.raises(rc.FrameError):
        bot.open({**f, "c": rc.b64e(bytes(raw))})
    other_bot, other_phone, _ = _pair()
    with pytest.raises(rc.FrameError):
        other_bot.open({**f, "sid": other_bot.sid})
    assert bot.open(f) == b"secret body"  # the failures did not burn its counter


def test_padding_hides_exact_sizes():
    bot, phone, _ = _pair()
    sizes = {len(rc.b64d(phone.seal("req", f"r{i}", b"x" * i)["c"])) for i in range(0, 200, 7)}
    assert sizes == {rc.PAD + 16}


def test_hello_replay_and_wrong_secrets_are_refused():
    ident = rc.BotIdentity.generate()
    payload = rc.new_pairing(ident, "https://relay.example.net")
    device = rc.generate_private()
    hello, _ = rc.client_hello(payload.bot_public, payload.install_id, device,
                               pairing=(payload.pairing_id, payload.psk))
    cache = rc.HelloCache()
    rc.bot_accept(ident, hello, psk_for=lambda pid: payload.psk, seen=cache.seen)
    with pytest.raises(rc.HandshakeError, match="replayed"):
        rc.bot_accept(ident, hello, psk_for=lambda pid: payload.psk, seen=cache.seen)
    # The wrong pairing secret, no open pairing, a stale clock, another bot.
    with pytest.raises(rc.HandshakeError):
        rc.bot_accept(ident, hello, psk_for=lambda pid: os.urandom(32))
    with pytest.raises(rc.HandshakeError, match="no such pairing"):
        rc.bot_accept(ident, hello, psk_for=lambda pid: None)
    old, _ = rc.client_hello(payload.bot_public, payload.install_id, device,
                             pairing=(payload.pairing_id, payload.psk), now=time.time() - 3600)
    with pytest.raises(rc.HandshakeError, match="clock"):
        rc.bot_accept(ident, old, psk_for=lambda pid: payload.psk)
    with pytest.raises(rc.HandshakeError):
        rc.bot_accept(rc.BotIdentity.generate(), hello, psk_for=lambda pid: payload.psk)


def test_a_welcome_from_anyone_but_the_bot_is_refused():
    ident = rc.BotIdentity.generate()
    device = rc.generate_private()
    hello, pending = rc.client_hello(ident.public, ident.install_id, device)
    # An impostor that knows the bot's public key, not its private key,
    # answers with its own identity.
    impostor = rc.BotIdentity(rc.generate_private(), ident.token)
    with pytest.raises(rc.HandshakeError):
        acc = rc.bot_accept(impostor, hello, psk_for=lambda pid: None)
        rc.client_finish(pending, acc.welcome)
    acc = rc.bot_accept(ident, hello, psk_for=lambda pid: None)
    forged = dict(acc.welcome)
    blob = bytearray(rc.b64d(forged["c"]))
    blob[-1] ^= 1
    forged["c"] = rc.b64e(bytes(blob))
    with pytest.raises(rc.HandshakeError):
        rc.client_finish(pending, forged)
    assert rc.client_finish(pending, acc.welcome).sid == acc.session.sid


def test_pairing_payload_round_trip_keeps_the_secret_in_the_fragment():
    ident = rc.BotIdentity.generate()
    p = rc.new_pairing(ident, "https://relay.example.net")
    link = p.to_string()
    assert link.startswith("https://relay.example.net/#tr1=")
    assert rc.b64e(p.psk) not in link.split("#")[0]
    assert rc.PairingPayload.from_string(link) == p
    assert rc.PairingPayload.from_string(link.split("#", 1)[1]) == p
    tailnet = p.to_string("https://host.example.ts.net/chat/")
    assert tailnet.startswith("https://host.example.ts.net/chat/#tr1=")
    with pytest.raises(rc.HandshakeError):
        rc.PairingPayload.from_string("https://relay.example.net/#tr1=bm90LWpzb24")


def test_relay_and_bot_derive_the_same_ids():
    for _ in range(5):
        token = os.urandom(32)
        assert rs.install_id_for(token) == rc.install_id_for(token)
        assert rs.client_key_for(token) == rc.client_key_for(token)
        assert rs.ROUTE_RE.match(f"/r/{rc.install_id_for(token)}/bot")


# ── the relay's rules, without HTTP ───────────────────────────────────

def _bot_auth(token: bytes) -> str:
    return "Bearer " + rc.b64e(token)


def test_bot_must_prove_its_install_id():
    relay = rs.Relay(log=lambda line: None)
    token = os.urandom(32)
    install = rc.install_id_for(token)
    assert relay.bot_exchange(install, "", {"frames": []})[0] == 401
    assert relay.bot_exchange(install, _bot_auth(os.urandom(32)), {"frames": []})[0] == 403
    assert relay.bot_exchange(install, _bot_auth(token), {"frames": [], "wait": 0}) == (
        200, {"frames": []})


def test_allow_file_activates_installs(tmp_path):
    allow = tmp_path / "installs.txt"
    on, off = os.urandom(32), os.urandom(32)
    allow.write_text(f"# hosted installs\n{rc.install_id_for(on)}  # one\n")
    relay = rs.Relay(allow_file=str(allow), log=lambda line: None)
    assert relay.bot_exchange(rc.install_id_for(on), _bot_auth(on), {"wait": 0})[0] == 200
    status, out = relay.bot_exchange(rc.install_id_for(off), _bot_auth(off), {"wait": 0})
    assert status == 403 and "activated" in out["error"]


def test_client_limits_by_install():
    lim = rs.Limits(burst=2, rate=0.0, client_timeout=0.05)
    relay = rs.Relay(lim, log=lambda line: None)
    token = os.urandom(32)
    install, key = rc.install_id_for(token), rc.client_key_for(token)
    frame = lambda i: {"id": f"q{i}", "k": "ping", "sid": "s" * 32, "n": i, "c": "AAAA"}  # noqa: E731
    assert relay.client_request(install, key, frame(0))[0] == 503  # bot not connected
    relay.bot_exchange(install, _bot_auth(token), {"wait": 0})
    assert relay.client_request(install, "wrong", frame(0))[0] == 403
    assert relay.client_request(install, key, {"id": "x", "k": "res"})[0] == 400
    statuses = [relay.client_request(install, key, frame(i))[0] for i in range(1, 4)]
    assert statuses == [504, 504, 429]  # two time out unanswered, the third is over the burst
    # The timed-out frames left the bot's queue.
    assert relay.snapshot()["routes"][install]["inbox"] == []


def test_offline_bot_is_reaped():
    now = [1000.0]
    relay = rs.Relay(rs.Limits(offline_after=60), log=lambda line: None, clock=lambda: now[0])
    token = os.urandom(32)
    relay.bot_exchange(rc.install_id_for(token), _bot_auth(token), {"wait": 0})
    assert relay.reap() == 0
    now[0] += 61
    assert relay.reap() == 1 and relay.snapshot()["routes"] == {}


def test_relay_strips_fields_it_does_not_route():
    frame = rs.clean_frame({"id": "a1", "k": "res", "sid": "b" * 32, "n": 3, "c": "QUJD",
                            "text": "plaintext", "headers": {"x": "y"}}, rs.BOT_KINDS)
    assert frame == {"id": "a1", "k": "res", "sid": "b" * 32, "n": 3, "c": "QUJD"}
    assert rs.clean_frame({"id": "a1", "k": "err", "e": "free text"}, rs.BOT_KINDS)["e"] == "bad"
    assert rs.clean_frame({"id": "../x", "k": "res"}, rs.BOT_KINDS) is None
    assert rs.clean_frame({"id": "a1", "k": "res", "c": "not base64!"}, rs.BOT_KINDS) is None


def test_path_policy():
    ok = ["/api/me", "/api/channels", "/api/channels/C%3A1/threads?limit=50&before=9",
          "/api/files/f1", "/api/cards/C1/w12/actions"]
    bad = ["/api/pair", "/api/pair/", "/api/relay/pair", "/api/relay", "/index.html",
           "/api/../api/pair", "/api/%2e%2e/x", "/api/x y", "http://evil.example/api/me",
           "/api/me#frag", "/api/me\r\nX: 1", "/api//cards/C1/w1/actions", None, 7]
    assert all(rcl.path_allowed(p) for p in ok)
    assert not any(rcl.path_allowed(p) for p in bad)


def test_access_policy_for_relay_origin_devices():
    card = "/api/cards/C1/w1/actions"
    assert rcl.access_allows("read", "GET", "/api/me")
    assert not rcl.access_allows("read", "POST", "/api/channels/C1/messages")
    assert rcl.access_allows("chat", "POST", "/api/channels/C1/messages")
    assert not rcl.access_allows("chat", "POST", card)
    assert rcl.access_allows("all", "POST", card)
    for spelling in ("/api//cards/C1/w1/actions", "/api/%63ards/C1/w1/actions",
                     "/api/CARDS/C1/w1/actions", "/api/cards", "/api/cards?x=1"):
        assert not rcl.access_allows("chat", "POST", spelling), spelling
    relay = "https://relay.example.net"
    assert rcl.origin_of(None, relay) == "relay"
    assert rcl.origin_of("https://relay.example.net/app/", relay) == "relay"
    assert rcl.origin_of("https://relay.example.net:8443/", relay) == "own"
    assert rcl.origin_of("https://host.example.ts.net/chat/", relay) == "own"


def test_sse_block_parsing():
    assert rcl.parse_sse(["id: 7", "event: message", "data: {\"a\":1}"]) == {
        "id": "7", "event": "message", "data": "{\"a\":1}"}
    assert rcl.parse_sse([": ping"]) is None
    assert rcl.parse_sse(["data: a", "data: b"]) == {"data": "a\nb"}


def test_key_store_pairing_is_one_time(tmp_path):
    store = rcl.KeyStore.create(tmp_path / "relay.json", relay_url="https://relay.example.net")
    assert (os.stat(store.path).st_mode & 0o777) == 0o600
    with pytest.raises(rcl.RelayError):
        rcl.KeyStore.create(store.path)
    p = store.add_pairing()
    pub = rc.public_of(rc.generate_private())
    assert store.pairing_psk(p.pairing_id) == p.psk
    dev = store.register(p.pairing_id, pub, "phone <script>")
    assert dev["name"] == "phone script" and store.device(pub)["id"] == dev["id"]
    with pytest.raises(rcl.RelayError):
        store.register(p.pairing_id, pub)
    late = store.add_pairing(ttl=1, now=time.time() - 10)
    assert store.pairing_psk(late.pairing_id) is None
    assert store.revoke(dev["id"]) and store.device(pub) is None
    os.chmod(store.path, 0o644)
    with pytest.raises(rcl.RelayError, match="readable"):
        rcl.KeyStore(store.path).load()


def test_owner_cli(tmp_path, capsys):
    key = str(tmp_path / "relay.json")
    assert rcl.main(["init", "--key-file", key, "--relay-url", "https://relay.example.net"]) == 0
    made = json.loads(capsys.readouterr().out)
    assert made["relay"] == {"url": "https://relay.example.net",
                             "install_id": made["install_id"], "key_file": key}
    assert rcl.main(["init", "--key-file", key]) == 1  # never replaces an identity
    capsys.readouterr()
    assert rcl.main(["pair", "--key-file", key, "--app-url", "https://host.example.ts.net/chat/"]) == 0
    link = capsys.readouterr().out.strip()
    p = rc.PairingPayload.from_string(link)
    assert link.startswith("https://host.example.ts.net/chat/#tr1=")
    assert p.install_id == made["install_id"] and p.relay_url == "https://relay.example.net"
    assert rcl.main(["show", "--key-file", key]) == 0
    assert json.loads(capsys.readouterr().out)["open_pairings"] == 1
    assert rcl.main(["revoke", "--key-file", key, "--device", "0" * 16]) == 1


def test_start_relay_needs_a_relay_block(tmp_path):
    assert rcl.start_relay({}, "http://127.0.0.1:1/chat") is None
    assert rcl.start_relay({"relay": {"url": "https://r.example.net"}}, "http://127.0.0.1:1/chat") is None
    cfg = {"relay": {"url": "https://r.example.net", "key_file": str(tmp_path / "k.json"),
                     "install_id": "a" * 26}}
    assert rcl.start_relay(cfg, "http://127.0.0.1:1/chat") is None  # pinned id, no file
    cfg = {"relay": {"url": "http://relay.example.net", "key_file": str(tmp_path / "k.json")}}
    assert rcl.start_relay(cfg, "http://127.0.0.1:1/chat") is None  # plain http off loopback
    cfg = {"relay": {"url": "https://r.example.net", "key_file": str(tmp_path / "k2.json")}}
    assert rcl.start_relay(cfg, "http://192.0.2.1:8470/chat") is None  # not loopback
    assert rcl.start_relay({"relay": {"enabled": False, "url": "x", "key_file": "y"}}, "") is None


# ── the loopback run ──────────────────────────────────────────────────

class FakeAPI(BaseHTTPRequestHandler):
    """The web front end's API, as much of it as the relay needs."""
    seen: list = []
    stop = threading.Event()

    def log_message(self, *a):
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _owner(self):
        FakeAPI.seen.append((self.command, self.path, dict(self.headers)))
        if self.headers.get("Cookie") != f"ea_web={PAIR_TOKEN}":
            self._json(401, {"error": "not the owner"})
            return False
        return True

    def do_GET(self):
        if not self._owner():
            return
        if self.path == "/chat/api/me":
            self._json(200, {"owner": "owner", "bot": "bot", "note": REPLY_MARKER})
        elif self.path == "/chat/api/files/f1":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="a.bin"')
            self.send_header("Content-Length", str(len(FILE_MARKER)))
            self.send_header("Set-Cookie", "leak=1")
            self.end_headers()
            self.wfile.write(FILE_MARKER)
        elif self.path.startswith("/chat/api/events"):
            since = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("since", ["-"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(b": ping\n\n")
                data = json.dumps({"text": EVENT_MARKER, "since": since})
                self.wfile.write(f"id: 8\nevent: message\ndata: {data}\n\n".encode())
                self.wfile.flush()
                for _ in range(100):
                    if FakeAPI.stop.wait(0.1):
                        break
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except OSError:
                pass
        else:
            self._json(404, {"error": "no such route"})

    def do_POST(self):
        if not self._owner():
            return
        self._raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            body = json.loads(self._raw or b"{}")
        except ValueError:
            body = {}
        if self.path == "/chat/api/channels/C1/messages":
            self._json(200, {"id": "w2", "text": body.get("text"), "author": "owner"})
        elif self.path == "/chat/api/channels/C1/files":
            self._json(200, {"size": len(self._raw), "head": self._raw[:16].decode("latin-1")})
        elif self.path == "/chat/api/cards/C1/w1/actions":
            self._json(200, {"ok": True, "action_id": body.get("action_id")})
        else:
            self._json(404, {"error": "no such route"})


def _serve(server):
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


class Phone:
    """The phone: relay_crypto and urllib, nothing of the bot's."""

    def __init__(self, base: str, payload: rc.PairingPayload):
        self.base = base
        self.payload = payload
        self.url = f"{base}/r/{payload.install_id}"
        self.key = rc.generate_private()
        self.session: rc.Session | None = None

    def post(self, frame: dict, key: str | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(self.url + "/c", data=json.dumps(frame).encode(),
                                     method="POST", headers={
                                         "Content-Type": "application/json",
                                         "X-Relay-Key": key or self.payload.client_key})
        try:
            with NO_PROXY.open(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def hello(self, pairing: bool = True) -> tuple[int, dict]:
        p = self.payload
        frame, pending = rc.client_hello(p.bot_public, p.install_id, self.key,
                                         pairing=(p.pairing_id, p.psk) if pairing else None,
                                         info={"name": "loopback phone"})
        status, answer = self.post(frame)
        if status == 200 and answer.get("k") == "welcome":
            self.session = rc.client_finish(pending, answer)
        return status, answer

    def call(self, method: str, path: str, body: bytes | None = None,
             headers: dict | None = None) -> tuple[dict, dict]:
        """(the sealed frame sent, the answer: decrypted, or the err frame)."""
        req = {"m": method, "p": path, "h": headers or {"content-type": "application/json"}}
        frame = self.session.seal("req", rc.new_id(), rc.pack(req, body or b""))
        return frame, self.send(frame)

    def send(self, frame: dict) -> dict:
        status, answer = self.post(frame)
        if answer.get("k") == "res":
            meta, body = rc.unpack(self.session.open(answer))
            return {**meta, "body": body}
        return answer


@pytest.fixture(scope="module")
def loop(tmp_path_factory):
    d = tmp_path_factory.mktemp("relay-loop")
    (d / "webui").mkdir()
    (d / "webui" / "index.html").write_text("<!doctype html><title>chat</title>")
    (d / "secret.txt").write_text("outside the static folder")
    (d / "pair.token").write_text(PAIR_TOKEN + "\n")
    FakeAPI.seen = []
    FakeAPI.stop.clear()
    api = _serve(ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI))
    api.daemon_threads = True
    taps, logs = [], []
    relay = rs.Relay(rs.Limits(keepalive=0.3, client_timeout=10),
                     log=logs.append, tap=lambda d_, f: taps.append((d_, json.dumps(f))))
    server = _serve(rs.RelayHTTPServer(("127.0.0.1", 0), relay, str(d / "webui")))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    web_cfg = {"pair_token_file": str(d / "pair.token"),
               "relay": {"url": base, "key_file": str(d / "relay-key.json"), "wait": 1}}
    client = rcl.start_relay(web_cfg, f"http://127.0.0.1:{api.server_address[1]}/chat")
    assert client is not None
    deadline = time.time() + 10
    while not client.connected and time.time() < deadline:
        time.sleep(0.05)
    assert client.connected, client.last_error
    phone = Phone(base, rc.PairingPayload.from_string(client.new_pairing()))
    status, welcome = phone.hello()
    assert status == 200 and welcome["k"] == "welcome", welcome
    yield {"relay": relay, "server": server, "base": base, "client": client, "phone": phone,
           "taps": taps, "logs": logs, "dir": d, "cfg": web_cfg}
    FakeAPI.stop.set()
    client.stop()
    server.shutdown()
    api.shutdown()


def test_pairing_registers_the_phone(loop):
    devices = loop["client"].devices()
    assert [d["name"] for d in devices] == ["loopback phone"]
    assert devices[0]["id"] == loop["phone"].session.device
    key_file = loop["dir"] / "relay-key.json"
    assert (os.stat(key_file).st_mode & 0o777) == 0o600
    # The pairing was spent: a second phone with the same QR is refused.
    again = Phone(loop["base"], loop["phone"].payload)
    status, answer = again.hello()
    assert status == 200 and answer["k"] == "err" and answer["e"] == "auth"
    assert again.session is None


def test_request_through_the_relay(loop):
    phone = loop["phone"]
    _, me = phone.call("GET", "/api/me")
    assert me["s"] == 200 and json.loads(me["body"])["note"] == REPLY_MARKER
    body = json.dumps({"text": REQUEST_MARKER}).encode()
    _, posted = phone.call("POST", "/api/channels/C1/messages", body)
    assert posted["s"] == 200 and json.loads(posted["body"])["text"] == REQUEST_MARKER
    _, f = phone.call("GET", "/api/files/f1")
    assert f["body"] == FILE_MARKER
    assert f["h"] == {"content-type": "application/octet-stream",
                      "content-disposition": 'attachment; filename="a.bin"'}
    # The owner credential is added on the bot; the phone's own is dropped.
    _, me2 = phone.call("GET", "/api/me", headers={"cookie": "ea_web=forged",
                                                    "tailscale-user-login": "someone"})
    assert me2["s"] == 200
    hdrs = FakeAPI.seen[-1][2]
    assert hdrs["Cookie"] == f"ea_web={PAIR_TOKEN}"
    assert "Tailscale-User-Login" not in hdrs
    assert hdrs["X-Tacitus-Relay"] == phone.session.device
    assert hdrs["X-Tacitus-Relay-Cards"] == "deny"  # paired on the relay's origin


def test_large_bodies_cross_once_encoded(loop):
    """A body near the chat's 10 MiB file cap fits one frame; one over it
    is refused by the bot, not dropped by the relay."""
    phone = loop["phone"]
    big = b"\x89PNG" + os.urandom(rcl.MAX_BODY - 4)
    _, out = phone.call("POST", "/api/channels/C1/files", big,
                        headers={"content-type": "application/octet-stream"})
    assert out["s"] == 200 and json.loads(out["body"])["size"] == rcl.MAX_BODY
    _, out = phone.call("POST", "/api/channels/C1/files", big + b"x")
    assert out["s"] == 413


def test_ping_and_denied_paths(loop):
    phone = loop["phone"]
    frame = phone.session.seal("ping", rc.new_id(), rc.pack({}))
    status, answer = phone.post(frame)
    assert "pong" in rc.unpack(phone.session.open(answer))[0]
    n = len(FakeAPI.seen)
    for path in ("/api/pair", "/api/relay/pair", "/../chat/api/me"):
        _, out = phone.call("POST" if "pair" in path else "GET", path, b"{}")
        assert out["s"] == 403, path
    assert len(FakeAPI.seen) == n  # none reached the local server


def test_card_actions_need_a_device_paired_on_the_own_origin(loop):
    """The relay serves the app it pairs; a phone paired there cannot press
    a card's buttons. One paired through the bot's own origin can."""
    phone, client = loop["phone"], loop["client"]
    click = json.dumps({"action_id": "approve_send", "value": "v1"}).encode()
    n = len(FakeAPI.seen)
    _, out = phone.call("POST", "/api/cards/C1/w1/actions", click)
    assert out["s"] == 403 and "origin" in json.loads(out["body"])["error"]
    assert len(FakeAPI.seen) == n
    own_app = loop["cfg"]["relay"]["url"].replace("127.0.0.1", "localhost") + "/chat/"
    own = Phone(loop["base"], rc.PairingPayload.from_string(client.new_pairing(app_url=own_app)))
    assert own.hello()[0] == 200
    assert {d["id"]: d["origin"] for d in client.devices()}[own.session.device] == "own"
    _, out = own.call("POST", "/api/cards/C1/w1/actions", click)
    assert out["s"] == 200 and json.loads(out["body"]) == {"ok": True, "action_id": "approve_send"}
    assert FakeAPI.seen[-1][2]["X-Tacitus-Relay-Cards"] == "allow"
    client.revoke(own.session.device)


def test_moved_replayed_and_unknown_session_frames(loop):
    phone = loop["phone"]
    rid = rc.new_id()
    f = phone.session.seal("req", rid, rc.pack({"m": "GET", "p": "/api/me"}))
    # The sealed body under another id does not open: the id is bound.
    assert phone.post({**f, "id": rc.new_id()})[1]["e"] == "bad"
    # The failure did not spend the counter; the frame itself opens once.
    assert phone.post(f)[1]["k"] == "res"
    assert phone.post(f)[1] == {"id": rid, "k": "err", "e": "replay"}
    status, unknown = phone.post({**f, "id": rc.new_id(), "sid": "0" * 32})
    assert unknown["k"] == "err" and unknown["e"] == "session"


def test_event_stream_through_the_relay(loop):
    phone, base = loop["phone"], loop["base"]
    sid = rc.new_id()
    opener = phone.session.seal("open", sid, rc.pack({"since": 5}))
    q = urllib.parse.urlencode({"f": rc.b64e(json.dumps(opener).encode()),
                                "key": phone.payload.client_key})
    resp = NO_PROXY.open(f"{phone.url}/sse?{q}", timeout=10)
    assert resp.headers["Content-Type"] == "text/event-stream"
    event = None
    for _ in range(50):
        line = resp.readline().decode().strip()
        if line.startswith("data: "):
            frame = json.loads(line[6:])
            assert frame["k"] == "sse" and frame["id"] == sid
            event = rc.unpack(phone.session.open(frame))[0]
            break
    assert event is not None
    assert event["id"] == "8" and event["event"] == "message"
    assert json.loads(event["data"]) == {"text": EVENT_MARKER, "since": "5"}
    resp.close()
    # The relay notices the phone left (its keepalive write fails), sends
    # the bot a close, and the bot closes the local stream.
    client = loop["client"]
    deadline = time.time() + 10
    while client.status()["streams"] and time.time() < deadline:
        time.sleep(0.05)
    assert client.status()["streams"] == 0


def test_wrong_keys_at_the_relay(loop):
    phone = loop["phone"]
    frame = phone.session.seal("ping", rc.new_id(), rc.pack({}))
    assert phone.post(frame, key="not-the-key")[0] == 403
    install = phone.payload.install_id
    req = urllib.request.Request(f"{loop['base']}/r/{install}/bot", data=b'{"frames":[]}',
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "Authorization": "Bearer " + rc.b64e(os.urandom(32))})
    with pytest.raises(urllib.error.HTTPError) as e:
        NO_PROXY.open(req, timeout=5)
    assert e.value.code == 403
    e.value.close()


def test_static_ui_and_traversal(loop):
    base = loop["base"]
    with NO_PROXY.open(base + "/", timeout=5) as r:
        assert b"<title>chat</title>" in r.read()
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]
        assert r.headers["X-Content-Type-Options"] == "nosniff"
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/missing.js"):
        with pytest.raises(urllib.error.HTTPError) as e:
            NO_PROXY.open(base + path, timeout=5)
        assert e.value.code == 404, path
        e.value.close()
    with NO_PROXY.open(base + "/healthz", timeout=5) as r:
        assert json.loads(r.read())["installs"] == 1


def test_revoked_device_loses_its_session(loop):
    client, phone = loop["client"], loop["phone"]
    other = Phone(loop["base"], rc.PairingPayload.from_string(client.new_pairing()))
    assert other.hello()[0] == 200 and other.session is not None
    assert other.call("GET", "/api/me")[1]["s"] == 200
    assert client.revoke(other.session.device)
    _, out = other.call("GET", "/api/me")
    assert out["k"] == "err" and out["e"] in ("session", "auth")
    status, answer = other.hello(pairing=False)
    assert answer["k"] == "err" and answer["e"] == "auth"
    assert phone.call("GET", "/api/me")[1]["s"] == 200  # the first phone is untouched


def test_the_relay_sees_only_ciphertext(loop):
    """Runs last: everything the relay handled in this module."""
    phone = loop["phone"]
    # The positive control: the needles find the markers in plaintext.
    _, me = phone.call("GET", "/api/me")
    assert _leaks(me["body"]) and _leaks(base64.urlsafe_b64encode(me["body"]))
    taps, logs, relay = loop["taps"], loop["logs"], loop["relay"]
    assert len(taps) > 20 and logs
    blobs = [t for _, t in taps] + logs + [json.dumps(relay.snapshot())]
    for _, text in taps:
        c = json.loads(text).get("c")
        if c:
            blobs.append(rc.b64d(c))
    leaks = [(i, _leaks(b if isinstance(b, bytes) else b.encode())) for i, b in enumerate(blobs)]
    assert [x for x in leaks if x[1]] == []
    # The log lines name the route, the install, the status and sizes only.
    assert all(len(line.split()) == 8 for line in logs), logs[:3]
    # Every tapped frame has the protocol's fields and nothing else.
    for _, text in taps:
        assert set(json.loads(text)) <= {"id", "k", "sid", "n", "c", "e"}
