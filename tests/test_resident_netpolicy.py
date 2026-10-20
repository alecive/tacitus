"""The resident's network policy: its rules, its probes, the root script, and
the boundary audit's check.

Pins, on fixed facts and never the host's install:

  * the rendered ruleset filters only the resident's sockets (meta skuid),
    replaces its own table in one transaction and touches nothing else;
  * the allowlist is exactly the resolver, the EA's deck ports and the
    owner's tool gateway, each with its reason, in front of the refusals;
    tcp 443 on the tailnet addresses (tailscale serve, every route on the
    node) is refused like the rest of this host;
  * the refused ranges cover this host, loopback, the private, link-local,
    CGNAT and tailnet ranges, multicast, the on-link prefixes and the
    NAT64 and 6to4 forms, on IPv4 and IPv6, and leave the internet open;
  * the facts read from resolv.conf, `ip -j` and /proc/net;
  * the probes: a free port for every refused target, one target per class,
    the verdicts, and the probe program itself on loopback as this user;
  * deploy/enable-resident-netpolicy.sh: --print renders without root, the
    other modes need root, the unit loads at boot and a refresh reloads;
  * check_netpolicy: not installed, installed and holding, drift, a flushed
    table, a refused allowlist entry, another user's listener, and a probe
    the sudo policy refuses.
"""
from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

# These probes run the boundary audit, a Linux-only tool.
LINUX_AUDIT = pytest.mark.skipif(sys.platform == "darwin", reason="Linux-only audit")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

os.environ.setdefault("EA_DIR", tempfile.mkdtemp(prefix="ea-test-netpolicy-"))

import resident_netpolicy as NP  # noqa: E402
from conftest import NEEDS_RESIDENT, resident_child_available

_spec = importlib.util.spec_from_file_location("boundary_audit", ROOT / "scripts" / "boundary-audit.py")
audit = sys.modules.get("boundary_audit")
if audit is None:
    audit = importlib.util.module_from_spec(_spec)
    sys.modules["boundary_audit"] = audit   # dataclasses look their module up
    _spec.loader.exec_module(audit)

SCRIPT = ROOT / "deploy" / "enable-resident-netpolicy.sh"
STRANGER = audit.Who(2 ** 31 - 7, frozenset({2 ** 31 - 7}))

FACTS = {
    "user": "ea-bot", "uid": 997,
    "nameservers": ["127.0.0.53"],
    "onlink": ["203.0.113.0/24", "172.17.0.0/16", "172.19.0.0/16", "fe80::/64",
               "fd7a:115c:a1e0::1:2/128", "2001:db8:5::/64"],
    "tailnet": ["100.101.102.103", "fd7a:115c:a1e0::1:2"],
    "addresses": [
        {"dev": "eth0", "addr": "203.0.113.7", "scope": "global", "up": True},
        {"dev": "eth0", "addr": "fe80::200:ff:fe00:1%eth0", "scope": "link", "up": True},
        {"dev": "docker0", "addr": "172.17.0.1", "scope": "global", "up": False},
        {"dev": "veth1", "addr": "fe80::1%veth1", "scope": "link", "up": True},
        {"dev": "tailscale0", "addr": "100.101.102.103", "scope": "global", "up": True},
        {"dev": "tailscale0", "addr": "fd7a:115c:a1e0::1:2", "scope": "global", "up": True},
    ],
    "deck_ports": [3050, 3069],
    "gateway_port": 3070,
}
SOCKS = {"tcp": [("127.0.0.1", 3052, 997), ("0.0.0.0", 22, 0), ("127.0.0.1", 11434, 993)],
         "udp": [("127.0.0.53", 53, 989)]}


def _chain(text: str, name: str) -> list[str]:
    m = re.search(r"\tchain %s \{\n(.*?)\n\t\}" % name, text, re.S)
    assert m, name
    return [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]


def _elements(text: str, name: str) -> list:
    m = re.search(r"\tset %s \{.*?elements = \{(.*?)\}" % name, text, re.S)
    assert m, name
    return [ipaddress.ip_network(x.strip()) for x in m.group(1).split(",") if x.strip()]


# ── the ruleset ───────────────────────────────────────────────────────

def test_the_table_filters_only_the_residents_sockets_and_replaces_itself():
    text = NP.render(FACTS)
    assert _chain(text, "output") == [
        "type filter hook output priority filter; policy accept;",
        "meta skuid 997 jump resident"]
    assert text.count("hook ") == 1, "one base chain, on output"
    assert "flush" not in text and "ruleset" not in text
    idiom = "table inet ea_resident {\n}\ndelete table inet ea_resident\n"
    assert text.index(idiom) < text.index("table inet ea_resident {\n\tset deny4")
    tables = [ln for ln in text.splitlines() if ln.startswith(("table ", "delete table "))]
    assert tables == ["table inet ea_resident {", "delete table inet ea_resident",
                      "table inet ea_resident {"]


def test_the_allowlist_is_the_resolver_the_deck_ports_and_the_gateway():
    rules = _chain(NP.render(FACTS), "resident")
    accepts = [r.split(" comment ")[0] for r in rules if " accept" in r]
    assert accepts == [
        "ip daddr 127.0.0.53 udp dport 53 accept",
        "ip daddr 127.0.0.53 tcp dport 53 accept",
        "ip daddr 127.0.0.1 tcp dport 3050-3069 accept",
        "ip daddr 127.0.0.1 tcp dport 3070 accept",
        "ct direction reply ip daddr 127.0.0.0/8 accept",
        "ct direction reply ip6 daddr ::1 accept",
    ]
    assert all(" comment \"" in r for r in rules)
    for port in (3050, 3060, 3069, 3070):
        assert NP.allowed(FACTS, "127.0.0.1", "tcp", port)
    for host, proto, port in (("127.0.0.1", "tcp", 3049), ("127.0.0.1", "tcp", 3071),
                              ("127.0.0.1", "udp", 3070), ("::1", "tcp", 3070),
                              ("127.0.0.1", "tcp", 3033), ("127.0.0.1", "tcp", 8080),
                              ("127.0.0.1", "tcp", 11434), ("127.0.0.1", "tcp", 631),
                              ("127.0.0.1", "tcp", 445), ("127.0.0.1", "tcp", 22),
                              ("::1", "tcp", 3050), ("127.0.0.53", "tcp", 5355),
                              ("127.0.0.54", "udp", 53), ("100.101.102.103", "tcp", 8443),
                              ("100.101.102.103", "tcp", 8377), ("100.101.102.103", "udp", 443),
                              ("fd7a:115c:a1e0::1:2", "tcp", 8443), ("100.100.100.100", "udp", 53),
                              ("100.101.102.103", "tcp", 443), ("fd7a:115c:a1e0::1:2", "tcp", 443)):
        assert not NP.allowed(FACTS, host, proto, port), (host, proto, port)


def test_every_allow_entry_names_its_reason_in_the_file():
    text = NP.render(FACTS)
    for line in ("#   dns    udp 127.0.0.53 port 53: name resolution through a nameserver in /etc/resolv.conf",
                 "#   decks  tcp 127.0.0.1 port 3050-3069: the EA's own deck servers",
                 "#   gateway tcp 127.0.0.1 port 3070: the owner's tool gateway"):
        assert line in text
    allow_block = text.split("# allow:")[1].split("# refuse:")[0]
    assert "port 443" not in allow_block and "tailscale serve" not in allow_block
    for net, why in NP.DENY4 + NP.DENY6:
        assert f"#   {net:<16} {why}" in text
    assert "#   203.0.113.0/24   on-link here" in text


def test_the_refusals_come_after_the_allowlist_and_cover_every_protocol():
    rules = _chain(NP.render(FACTS), "resident")
    last_accept = max(i for i, r in enumerate(rules) if " accept" in r)
    refusals = rules[last_accept + 1:]
    assert [r.split(" counter")[0] for r in refusals] == [
        "fib daddr type { local, broadcast, multicast }",
        "ip daddr @deny4", "ip6 daddr @deny6"]
    assert all("counter goto refuse" in r for r in refusals)
    assert _chain(NP.render(FACTS), "refuse") == [
        f"limit rate 10/minute burst 20 packets log prefix \"{NP.LOG_PREFIX}\" level info",
        "reject with icmpx admin-prohibited"]


REFUSED_HOSTS = ["127.0.0.1", "127.9.9.9", "0.0.0.0", "10.1.2.3", "172.17.0.2", "172.31.0.1",
                 "192.168.1.1", "169.254.169.254", "100.64.0.1", "100.100.100.100",
                 "100.101.102.103", "224.0.0.251", "239.255.255.250", "255.255.255.255",
                 "203.0.113.9", "::", "::1", "fd7a:115c:a1e0::53", "fd00::1", "fe80::1",
                 "fec0::1", "ff02::fb", "64:ff9b:1::1", "64:ff9b::a00:1", "64:ff9b::7f00:1",
                 "64:ff9b::a9fe:a9fe", "2002:c0a8:101::1", "2002:7f00:1::1", "2002:cb00:7109::1",
                 "2001:db8:5::1", "::ffff:127.0.0.1"]
OPEN_HOSTS = ["1.1.1.1", "8.8.8.8", "93.184.216.34", "140.82.112.3", "160.79.104.10",
              "203.0.114.1", "100.128.0.1", "2606:4700::1111", "2a00:1450:4001::1",
              "64:ff9b::101:101", "2002:808:808::1", "2001:db8:6::1"]


@pytest.mark.parametrize("host", REFUSED_HOSTS)
def test_this_host_and_local_networks_are_refused(host):
    assert NP.refused_range(FACTS, host)


@pytest.mark.parametrize("host", OPEN_HOSTS)
def test_the_internet_stays_open(host):
    assert not NP.refused_range(FACTS, host)


def test_the_sets_hold_the_ranges_with_no_overlap():
    text = NP.render(FACTS)
    d4, d6 = NP.deny_sets(dict(FACTS, onlink=NP.extra_onlink(FACTS["onlink"])))
    for name, want in (("deny4", d4), ("deny6", d6)):
        got = _elements(text, name)
        assert got == want
        for a, b in zip(got, got[1:]):
            assert not a.overlaps(b), (a, b)
    assert ipaddress.ip_network("203.0.113.0/24") in _elements(text, "deny4")
    assert ipaddress.ip_network("64:ff9b::cb00:7100/120") in _elements(text, "deny6")


def test_on_link_prefixes_inside_a_static_range_add_nothing():
    got = NP.extra_onlink(["172.17.0.0/16", "fe80::/64", "fd7a:115c:a1e0::1:2/128",
                           "0.0.0.0/0", "::/0", "203.0.113.0/25", "203.0.113.128/25",
                           "2001:db8:5::/64", "not-a-prefix"])
    assert [str(n) for n in got] == ["203.0.113.0/24", "2001:db8:5::/64"]


def test_v4_in_v6_forms():
    net = ipaddress.ip_network("192.168.0.0/16")
    assert [str(n) for n in NP.v4_in_v6(net)] == ["64:ff9b::c0a8:0/112", "2002:c0a8::/32"]


def test_the_rules_file_keeps_the_facts_it_was_rendered_from():
    text = NP.render(FACTS)
    stored = NP.parse(text)
    assert stored == {"user": "ea-bot", "uid": 997, "nameservers": ["127.0.0.53"],
                      "onlink": ["203.0.113.0/24", "2001:db8:5::/64"],
                      "deck_ports": [3050, 3069], "gateway_port": 3070}
    assert NP.parse("table inet x {}\n") is None
    assert NP.parse(NP.FACTS_MARK + "{not json\n") is None
    assert NP.render(dict(FACTS, onlink=stored["onlink"])) == text


def test_comments_fit_an_nft_rule():
    for m in re.finditer(r'comment "([^"\n]*)"', NP.render(FACTS)):
        assert len(m.group(1).encode()) <= 128


def test_no_host_gets_a_tailscale_serve_entry():
    for facts in (FACTS, dict(FACTS, tailnet=[])):
        text = NP.render(facts)
        assert "dport 443" not in text
        assert "ip daddr 127.0.0.1 tcp dport 3050-3069 accept" in text
        assert "tailnet" not in NP.parse(text)


def _nft_check(path: Path) -> list[str]:
    """`nft -c -f`: every error but the one an unprivileged run ends with.
    The parser runs before nft reads the kernel's ruleset, so without
    privileges the check still reports every syntax error; as root it is
    the full check the script makes."""
    nft = shutil.which("nft") or next((p for p in ("/usr/sbin/nft", "/sbin/nft")
                                       if os.access(p, os.X_OK)), None)
    if not nft:
        pytest.skip("nft is not installed")
    r = subprocess.run([nft, "-c", "-f", str(path)], capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        return []
    if "cache initialization failed" not in r.stderr:
        # An nft that cannot reach netfilter at all (a container, a CI
        # runner's kernel) ends with no message and no parse; nothing to
        # check here.
        pytest.skip(f"nft cannot run the check here (exit {r.returncode}, "
                    f"stderr {r.stderr.strip()[:80]!r})")
    return [ln for ln in r.stderr.splitlines()
            if "Error:" in ln and "cache initialization failed" not in ln]


def test_nft_parses_the_rules(tmp_path):
    good = tmp_path / "rules.nft"
    good.write_text(NP.render(FACTS))
    assert _nft_check(good) == []
    bad = tmp_path / "broken.nft"
    bad.write_text(NP.render(FACTS).replace("tcp dport 3050-3069 accept", "tcp dport 3050-3069 acept"))
    assert any("syntax error" in ln for ln in _nft_check(bad)), "the check finds a broken rule"


# ── this host's facts ─────────────────────────────────────────────────

def test_nameservers_from_resolv_conf():
    text = ("# Generated by resolvconf\nnameserver 127.0.0.53\n  nameserver 127.0.0.53\n"
            "nameserver fe80::1%eth0 # zone\nnameserver foo\noptions edns0 trust-ad\n"
            "; nameserver 10.0.0.1\nsearch example.org\nnameserver 8.8.8.8\n")
    assert NP.nameservers(text) == ["127.0.0.53", "fe80::1", "8.8.8.8"]


def test_on_link_prefixes_from_ip_route():
    routes = [
        {"dst": "default", "gateway": "203.0.113.1", "dev": "eth0"},
        {"dst": "203.0.113.0/24", "dev": "eth0", "protocol": "kernel", "scope": "link"},
        {"dst": "172.17.0.0/16", "dev": "docker0", "flags": ["linkdown"]},
        {"dst": "10.9.0.0/16", "gateway": "10.8.0.1", "dev": "tun0"},
        {"dst": "198.18.0.0/15", "nexthops": [{"gateway": "203.0.113.2"}]},
        {"dst": "127.0.0.0/8", "dev": "lo"},
        {"type": "unreachable", "dst": "192.0.2.0/24"},
        {"dst": "fd7a:115c:a1e0::1:2", "dev": "tailscale0"},
        {"dst": "fe80::/64", "dev": "eth0"},
        {"dst": "fe80::/64", "dev": "docker0"},
        "junk",
    ]
    assert NP.onlink_prefixes(routes) == ["203.0.113.0/24", "172.17.0.0/16",
                                          "fd7a:115c:a1e0::1:2/128", "fe80::/64"]


def test_addresses_and_the_tailnet_from_ip_addr():
    links = [
        {"ifname": "lo", "operstate": "UNKNOWN",
         "addr_info": [{"family": "inet", "local": "127.0.0.1", "scope": "host"}]},
        {"ifname": "eth0", "operstate": "UP",
         "addr_info": [{"family": "inet", "local": "203.0.113.7", "scope": "global"},
                       {"family": "inet6", "local": "fe80::2", "scope": "link"}]},
        {"ifname": "tailscale0", "operstate": "UNKNOWN",
         "addr_info": [{"family": "inet", "local": "100.101.102.103", "scope": "global"},
                       {"family": "inet6", "local": "fd7a:115c:a1e0::1:2", "scope": "global"},
                       {"family": "inet6", "local": "fe80::3", "scope": "link"}]},
        {"ifname": "docker0", "operstate": "DOWN",
         "addr_info": [{"family": "inet", "local": "172.17.0.1", "scope": "global"}]},
    ]
    f = NP.facts_from(997, "nameserver 127.0.0.53\n", [], links, deck_ports=(3050, 3069))
    assert f["tailnet"] == ["100.101.102.103", "fd7a:115c:a1e0::1:2"]
    assert [a["addr"] for a in f["addresses"]] == [
        "203.0.113.7", "fe80::2%eth0", "100.101.102.103", "fd7a:115c:a1e0::1:2",
        "fe80::3%tailscale0", "172.17.0.1"]
    assert [a["up"] for a in f["addresses"]] == [True, True, True, True, True, False]
    assert f["deck_ports"] == [3050, 3069] and f["uid"] == 997


def test_the_deck_ports_come_from_deck_serve():
    import deck_serve
    f = NP.facts_from(997, "", [], [])
    assert tuple(f["deck_ports"]) == deck_serve.PORT_RANGE


def test_proc_net_sockets():
    tcp = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
           "   0: 0100007F:0BEC 00000000:0000 0A 00000000:00000000 00:00000000 00000000   997        0 1 1\n"
           "   1: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 2 1\n"
           "   2: 0100007F:0BEC 0100007F:A1B2 01 00000000:00000000 00:00000000 00000000   997        0 3 1\n")
    tcp6 = ("  sl  local_address                         remote_address                        st\n"
            "   0: 00000000000000000000000001000000:0277 00000000000000000000000000000000:0000 0A "
            "00000000:00000000 00:00000000 00000000     0        0 4 1\n")
    assert NP.parse_sockets(tcp, listening_only=True) == [("127.0.0.1", 3052, 997), ("0.0.0.0", 22, 0)]
    assert len(NP.parse_sockets(tcp, listening_only=False)) == 3
    assert NP.parse_sockets(tcp6, listening_only=True) == [("::1", 631, 0)]


# ── probes ────────────────────────────────────────────────────────────

def test_a_refused_target_is_probed_on_a_port_nobody_holds():
    socks = {"tcp": [("0.0.0.0", 9, 0)], "udp": [("127.0.0.1", 9, 0), ("::", 47000, 0)]}
    targets = NP.probe_targets(FACTS, socks)
    refused = [t for t in targets if t["expect"] == "refused"]
    assert {t["port"] for t in refused if t["proto"] == "tcp"} == {47000}
    assert {t["port"] for t in refused if t["proto"] == "udp"} == {47001}


def test_the_probes_cover_each_class_and_agree_with_the_rules():
    targets = NP.probe_targets(FACTS, SOCKS)
    hosts = [t["host"] for t in targets if t["kind"] == "host"]
    assert hosts == ["127.0.0.1", "::1", "0.0.0.0", "::", "127.0.0.1", "203.0.113.7",
                     "fe80::200:ff:fe00:1%eth0", "172.17.0.1", "100.101.102.103",
                     "fd7a:115c:a1e0::1:2"]
    assert not [t for t in targets if t["port"] == 443]
    assert [t["host"] for t in targets if t["kind"] == "net"] == \
        [h for h, _ in NP.REMOTE_PROBES] + [NP.TAILNET_V6_PROBE[0]]
    allow = [(t["host"], t["proto"], t["port"]) for t in targets if t["kind"] == "allow"]
    assert allow == [("127.0.0.53", "tcp", 53), ("127.0.0.53", "udp", 53), ("127.0.0.1", "tcp", 3052)]
    for t in targets:
        if t["expect"] == "refused":
            assert NP.refused_range(FACTS, t["host"]) and not NP.allowed(FACTS, t["host"], t["proto"], t["port"])
        else:
            assert NP.allowed(FACTS, t["host"], t["proto"], t["port"])
    assert [t["dns"] for t in targets if t["kind"] == "allow"] == [False, True, False]


def test_a_public_nameserver_and_an_idle_deck_range_need_no_listener():
    f = dict(FACTS, nameservers=["8.8.8.8"], tailnet=[])
    targets = NP.probe_targets(f, {"tcp": [], "udp": []})
    allow = [(t["host"], t["port"]) for t in targets if t["kind"] == "allow"]
    assert allow == [("127.0.0.1", 3050)]
    assert NP.TAILNET_V6_PROBE[0] not in [t["host"] for t in targets]


@pytest.mark.parametrize("expect, result, want", [
    ("refused", "EHOSTUNREACH", "pass"), ("refused", "EACCES", "pass"), ("refused", "EPERM", "pass"),
    ("refused", "ENETUNREACH", "noroute"), ("refused", "ECONNREFUSED", "fail"),
    ("refused", "open", "fail"), ("refused", "timeout", "fail"), ("refused", "EIO", "unknown"),
    ("reached", "open", "pass"), ("reached", "ECONNREFUSED", "pass"),
    ("reached", "EHOSTUNREACH", "fail"), ("reached", "EPERM", "fail"), ("reached", "timeout", "unknown"),
])
def test_verdicts(expect, result, want):
    assert NP.verdict(expect, result) == want


def test_a_timeout_on_this_hosts_own_address_is_a_refusal():
    """The policy's drop of a link-local packet to this host shows as a
    timeout; without the policy the free port answers at once."""
    assert NP.verdict("refused", "timeout", "host") == "pass"
    assert NP.verdict("refused", "timeout", "net") == "fail"
    assert NP.verdict("refused", "ECONNREFUSED", "host") == "fail"
    assert NP.verdict("reached", "timeout", "host") == "unknown"


def _answers(targets, **over):
    """Results as the policy gives them, with `over` {host: result} applied."""
    out = []
    for t in targets:
        if t["host"] in over:
            out.append(over[t["host"]])
        elif t["expect"] == "reached":
            out.append("open")
        elif t["proto"] == "udp":
            out.append("EPERM")
        else:
            out.append("EACCES" if ":" in t["host"] else "EHOSTUNREACH")
    return out


def test_judge_needs_the_refusal_on_loopback():
    targets = NP.probe_targets(FACTS, SOCKS)
    j = NP.judge(targets, _answers(targets))
    assert j["loaded"] and not j["deny_fail"] and not j["allow_fail"]
    assert j["refused"] == len([t for t in targets if t["expect"] == "refused"])
    j = NP.judge(targets, _answers(targets, **{"127.0.0.1": "ECONNREFUSED"}))
    assert not j["loaded"] and "tcp 127.0.0.1:9 (loopback): ECONNREFUSED" in j["deny_fail"]
    j = NP.judge(targets, _answers(targets, **{"10.255.255.254": "timeout"}))
    assert j["loaded"] and j["deny_fail"] == [
        "tcp 10.255.255.254:9 (private 10.0.0.0/8): no answer: the packet left the host"]
    j = NP.judge(targets, _answers(targets, **{"::1": "EADDRNOTAVAIL", "fd7a:115c:a1e0::53": "ENETUNREACH"}))
    assert not j["loaded"] and not j["deny_fail"] and j["noroute"] == 2
    j = NP.judge(targets, _answers(targets, **{"127.0.0.53": "EHOSTUNREACH"}))
    assert len(j["allow_fail"]) == 2


@pytest.fixture
def loopback_services():
    """A TCP listener, a closed TCP port, a UDP resolver that answers, and a
    UDP port nobody holds, all on 127.0.0.1."""
    lsn = socket.socket()
    lsn.bind(("127.0.0.1", 0))
    lsn.listen(4)
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dns.bind(("127.0.0.1", 0))
    dns.settimeout(10)

    def answer():
        try:
            data, peer = dns.recvfrom(512)
            dns.sendto(data[:2] + b"\x81\x80" + data[4:], peer)
        except OSError:
            pass
    th = threading.Thread(target=answer, daemon=True)
    th.start()
    idle = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    idle.bind(("127.0.0.1", 0))
    idle_port = idle.getsockname()[1]
    idle.close()
    yield {"open": lsn.getsockname()[1], "closed": closed_port,
           "dns": dns.getsockname()[1], "idle": idle_port}
    lsn.close()
    dns.close()
    th.join(timeout=5)


def test_the_probe_program_on_loopback(loopback_services):
    if not os.path.exists(NP.PYTHON):
        pytest.skip(f"{NP.PYTHON} is missing")
    s = loopback_services
    targets = [{"host": "127.0.0.1", "port": s["open"], "proto": "tcp"},
               {"host": "127.0.0.1", "port": s["closed"], "proto": "tcp"},
               {"host": "127.0.0.1", "port": s["dns"], "proto": "udp", "dns": True},
               {"host": "127.0.0.1", "port": s["idle"], "proto": "udp"},
               {"host": "not-an-address", "port": 9, "proto": "tcp"}]
    assert NP.run_probes(targets, []) == ["open", "ECONNREFUSED", "open", "open", "EINVAL"]


def test_the_probe_runs_isolated_with_no_environment(monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, b'[{"id": 0, "result": "open"}]', b"")
    monkeypatch.setattr(NP.subprocess, "run", fake)
    assert NP.run_probes([{"host": "127.0.0.1", "port": 9, "proto": "tcp"}], ["sudo", "-n", "-u", "ea-bot"]) == ["open"]
    assert seen["argv"][:7] == ["sudo", "-n", "-u", "ea-bot", NP.PYTHON, "-I", "-c"]
    assert seen["env"] == {"PATH": "/usr/bin:/bin"} and seen["cwd"] == "/"
    assert json.loads(seen["argv"][-1]) == [{"id": 0, "host": "127.0.0.1", "port": 9, "proto": "tcp",
                                             "dns": False, "timeout": NP.PROBE_TIMEOUT_S}]


@pytest.mark.parametrize("rc, out, err, why", [
    (0, b"[]", b"", "does not cover every target"),
    (0, b"not json", b"", "not JSON"),
    (0, b'[{"id": 0, "result": 5}]', b"", "does not cover every target"),
    (1, b"", b"Traceback\nsudo: a password is required\n", "exited 1: sudo: a password is required"),
])
def test_a_probe_that_does_not_answer_cleanly_is_an_error(monkeypatch, rc, out, err, why):
    monkeypatch.setattr(NP.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, rc, out, err))
    with pytest.raises(NP.ProbeError, match=why):
        NP.run_probes([{"host": "127.0.0.1", "port": 9, "proto": "tcp"}], [])


def test_drift_names_what_changed():
    stored = NP.parse(NP.render(FACTS))
    assert NP.drift(stored, FACTS) == []
    live = dict(FACTS, uid=998, nameservers=["127.0.0.53", "10.0.0.2"],
                onlink=FACTS["onlink"] + ["198.51.100.0/24", "203.0.113.64/26"],
                tailnet=["100.101.102.104"], deck_ports=[3050, 3079])
    got = NP.drift(stored, live)
    assert got == [
        "the rules name uid 997, the resident is uid 998",
        "the nameserver 10.0.0.2 is not on the allowlist (name resolution fails)",
        "the on-link prefix 198.51.100.0/24 is not refused",
        "the deck ports are 3050-3079, the rules allow 3050-3069",
    ]


def test_a_rules_file_that_still_opens_tailscale_serve_is_drift():
    """An older render allowed tcp 443 to the tailnet addresses and named them
    in its facts; the drift check names each, so --status and the audit ask
    for the re-run that closes it."""
    stored = dict(NP.parse(NP.render(FACTS)), tailnet=["100.101.102.103"])
    assert NP.drift(stored, FACTS) == [
        "the rules allow tcp 443 to the tailnet address 100.101.102.103 (every tailscale "
        "serve route); re-run deploy/enable-resident-netpolicy.sh to close it"]


# ── the root script ───────────────────────────────────────────────────

def _run(*args, **kw):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60, **kw)


@pytest.mark.skipif(not resident_child_available(), reason=NEEDS_RESIDENT)
def test_print_renders_the_rules_without_root(tmp_path):
    f = tmp_path / "facts.json"
    f.write_text(json.dumps(FACTS))
    r = _run("--print", "--facts", str(f))
    assert r.returncode == 0, r.stderr
    assert r.stdout == NP.render(FACTS)


@pytest.mark.skipif(os.geteuid() == 0, reason="runs as root")
@pytest.mark.parametrize("mode", ["", "--enable", "--status", "--rollback"])
def test_every_other_mode_needs_root(mode):
    r = _run(*([mode] if mode else []))
    assert r.returncode == 1 and "run as root" in r.stderr


def test_an_unknown_mode_prints_the_usage():
    r = _run("--flush")
    assert r.returncode == 2 and "usage:" in r.stderr


SH = SCRIPT.read_text()


def test_the_script_loads_the_rules_at_boot_and_refreshes_in_one_transaction():
    assert SH.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in SH
    assert os.access(SCRIPT, os.X_OK)
    unit = SH[SH.index("[Unit]"):SH.index("[Install]") + 40]
    for line in ("DefaultDependencies=no", "Wants=network-pre.target", "Before=network-pre.target",
                 "After=local-fs.target nftables.service", "ConditionPathExists=$RULES",
                 "Type=oneshot", "RemainAfterExit=yes", "ExecStart=$1 -f $RULES",
                 "ExecReload=$1 -f $RULES", "ExecStop=-$1 delete table inet ea_resident",
                 "WantedBy=sysinit.target"):
        assert line in unit, line
    enable = SH[SH.index("enable(){"):SH.index("status(){")]
    assert enable.index('"$nft" -c -f "$TMP"') < enable.index('install -o root -g root -m 644 "$TMP" "$RULES"')
    assert 'systemctl reload "$UNIT_NAME"' in enable and "systemctl restart" not in SH
    assert "trusted_repo" in enable and "policy probe" in enable
    assert "flush ruleset" not in SH.replace("starts with `flush ruleset`", "")


def test_the_script_refuses_code_in_the_residents_home_and_names_no_owner():
    guard = SH[SH.index("trusted_repo(){"):SH.index("nft_bin(){")]
    assert 'getent passwd "$RESIDENT"' in guard and "refusing to run code from" in guard
    status = SH[SH.index("status(){"):SH.index("rollback(){")]
    assert "trusted_repo" in status
    assert "crh" not in SH and "/home/" not in SH and "SUDO_USER" not in SH


def test_rollback_removes_the_table_the_unit_and_the_file():
    rb = SH[SH.index("rollback(){"):SH.index("case \"${1:-}\"")]
    assert 'systemctl disable --now "$UNIT_NAME"' in rb
    assert "nft delete table inet ea_resident" in rb
    assert 'rm -f "$UNIT" "$RULES"' in rb and "systemctl daemon-reload" in rb


def test_the_new_files_carry_no_em_dash():
    for p in (SCRIPT, ROOT / "preprocessor" / "resident_netpolicy.py", Path(__file__)):
        assert chr(0x2014) not in p.read_text(), p


# ── the boundary audit's check ────────────────────────────────────────

@pytest.fixture
def installed(monkeypatch, tmp_path):
    """check_netpolicy on a scratch rules file, a stand-in resident, fixed
    facts and sockets, a unit that is enabled and active, and probes that
    answer as the policy does. `state` changes any of them."""
    if not os.path.isdir("/tmp") or (os.stat("/tmp").st_mode & 0o1777) != 0o1777:
        pytest.skip("no sticky world-writable /tmp")
    base = Path(tempfile.mkdtemp(dir="/tmp", prefix="ea-test-netpolicy-"))
    base.chmod(0o755)
    facts = dict(FACTS, uid=STRANGER.uid)
    rules = base / "resident-netpolicy.nft"
    rules.write_text(NP.render(facts))
    rules.chmod(0o644)
    state = {"enabled": "enabled", "active": "active", "facts": facts, "policy_allows": True,
             "socks": {"tcp": [("127.0.0.1", 3052, STRANGER.uid)], "udp": []},
             "over": {}, "error": None, "probed": 0}
    monkeypatch.setattr(NP, "RULES_FILE", rules)
    monkeypatch.setattr(audit, "NETPOLICY_OWNER_UID", os.geteuid())
    monkeypatch.setattr(audit, "_systemctl", lambda *a: {"is-enabled": state["enabled"],
                                                         "is-active": state["active"]}.get(a[0], ""))
    monkeypatch.setattr(audit.shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    monkeypatch.setattr(NP, "system_facts", lambda: state["facts"])
    monkeypatch.setattr(NP, "sockets", lambda: state["socks"])
    import resident_io
    monkeypatch.setattr(resident_io, "_policy_allows", lambda argv: state["policy_allows"])

    def probes(targets, prefix):
        state["probed"] += 1
        assert prefix[-3:] == ["-n", "-u", "ea-bot"] or prefix[-2:] == ["-u", "ea-bot"]
        if state["error"]:
            raise NP.ProbeError(state["error"])
        return _answers(targets, **state["over"])
    monkeypatch.setattr(NP, "run_probes", probes)
    ctx = audit.Ctx(ea_dir=base / "ea", private=base / "priv", owner_home=base, owner_logs=base / "logs",
                    live_tree=base / "no-live", lanes_tree=None, eabot_home=None, resident=STRANGER,
                    repo=ROOT)
    yield ctx, state, rules
    shutil.rmtree(base, ignore_errors=True)


def _statuses(results):
    return [(r["name"].split(":")[0], r["status"]) for r in results]


def test_not_installed_is_one_warning_and_no_probe(installed):
    ctx, state, rules = installed
    rules.unlink()
    state["enabled"], state["active"] = "", "inactive"
    got = audit.check_netpolicy(ctx)
    assert [r["status"] for r in got] == ["WARN"] and "not installed" in got[0]["detail"]
    assert got[0]["fix"] == "sudo bash deploy/enable-resident-netpolicy.sh"
    assert state["probed"] == 0


@LINUX_AUDIT
def test_an_installed_policy_that_holds_passes(installed):
    ctx, state, _ = installed
    got = audit.check_netpolicy(ctx)
    assert [r["status"] for r in got] == ["PASS", "PASS", "PASS"], got
    assert "refused" in got[1]["detail"] and "an EA deck port" in got[2]["detail"]


def test_a_unit_that_is_not_loaded_at_boot_fails(installed):
    ctx, state, _ = installed
    state["enabled"], state["active"] = "disabled", "inactive"
    got = audit.check_netpolicy(ctx)
    assert got[0]["status"] == "FAIL" and "a boot does not load it" in got[0]["detail"]
    assert state["probed"] == 1, "the probes still run"


@LINUX_AUDIT
def test_a_rules_file_others_can_change_fails(installed):
    ctx, state, rules = installed
    rules.chmod(0o666)
    got = audit.check_netpolicy(ctx)
    assert got[0]["status"] == "FAIL" and "not root's alone" in got[0]["detail"]
    rules.chmod(0o644)
    rules.write_text("table inet ea_resident {}\n")
    assert "names no facts" in audit.check_netpolicy(ctx)[0]["detail"]


@LINUX_AUDIT
def test_rules_for_another_uid_fail(installed):
    ctx, state, rules = installed
    rules.write_text(NP.render(dict(FACTS, uid=4242)))
    got = audit.check_netpolicy(ctx)
    assert got[0]["status"] == "FAIL" and "the rules filter uid 4242" in got[0]["detail"]


@LINUX_AUDIT
def test_drift_is_a_warning_with_the_rerun_as_fix(installed):
    ctx, state, _ = installed
    state["facts"] = dict(state["facts"], onlink=FACTS["onlink"] + ["198.51.100.0/24"])
    got = audit.check_netpolicy(ctx)
    assert got[0]["status"] == "WARN" and "198.51.100.0/24 is not refused" in got[0]["detail"]
    assert "renders them again" in got[0]["fix"]


@LINUX_AUDIT
def test_a_flushed_table_fails_the_probes(installed):
    ctx, state, _ = installed
    state["over"] = {"127.0.0.1": "ECONNREFUSED", "::1": "ECONNREFUSED", "0.0.0.0": "ECONNREFUSED",
                     "::": "ECONNREFUSED", "10.255.255.254": "timeout"}
    got = audit.check_netpolicy(ctx)
    assert _statuses(got) == [("netpolicy", "PASS"), ("netpolicy", "FAIL"), ("netpolicy", "PASS")]
    assert "tcp 127.0.0.1:9 (loopback): ECONNREFUSED" in got[1]["detail"]
    assert got[1]["fix"] == "sudo bash deploy/enable-resident-netpolicy.sh"


def test_a_refused_need_of_the_ea_fails(installed):
    ctx, state, _ = installed
    state["over"] = {"127.0.0.53": "EHOSTUNREACH"}
    got = audit.check_netpolicy(ctx)
    assert got[2]["status"] == "FAIL" and "127.0.0.53:53" in got[2]["detail"]


def test_another_users_listener_on_an_allowed_port_is_a_warning(installed):
    ctx, state, _ = installed
    state["socks"] = {"tcp": [("127.0.0.1", 3052, STRANGER.uid), ("127.0.0.1", 3055, 1000)], "udp": []}
    got = audit.check_netpolicy(ctx)
    assert got[2]["status"] == "WARN" and "127.0.0.1:3055 (uid 1000)" in got[2]["detail"]


@LINUX_AUDIT
def test_a_probe_the_sudo_policy_refuses_is_a_warning(installed):
    ctx, state, _ = installed
    state["policy_allows"] = False
    got = audit.check_netpolicy(ctx)
    assert [r["status"] for r in got] == ["PASS", "WARN", "WARN"] and state["probed"] == 0
    state["policy_allows"], state["error"] = True, "the probe exited 1"
    got = audit.check_netpolicy(ctx)
    assert [r["status"] for r in got] == ["PASS", "WARN", "WARN"] and "exited 1" in got[1]["detail"]


def test_no_resident_is_a_warning():
    ctx = audit.Ctx(ea_dir=Path("/nonexistent"), private=Path("/nonexistent"), owner_home=Path("/"),
                    owner_logs=Path("/nonexistent"), live_tree=Path("/nonexistent"), lanes_tree=None,
                    eabot_home=None, resident=None, repo=ROOT)
    got = audit.check_netpolicy(ctx)
    assert [r["status"] for r in got] == ["WARN"]


def test_the_audit_runs_the_check():
    assert audit.check_netpolicy in audit.CHECKS
