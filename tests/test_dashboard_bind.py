"""The project dashboard serves a non-loopback address only with a token.

Bound to the Tailscale IP with no EA_DASH_TOKEN, every device on the
tailnet could refile and rewrite the project store. Pins:
  * loopback (127/8, ::1, localhost) needs no token;
  * any other address (the Tailscale IP, a LAN address, 0.0.0.0, a host
    name) needs one; without it the server binds 127.0.0.1 instead, with
    a WARNING line, and does not exit (a Restart=on-failure unit would
    loop with no word to the owner);
  * the fallback leaves a mode 0600 file in the private directory for
    ea-doctor.py, and a start on the asked address removes it;
  * with a token every address is allowed, and the token may come from
    the owner-private secrets.env.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import owner_trust as pd_owner_trust  # noqa: E402
import project_dashboard as pd  # noqa: E402


def _private() -> Path:
    d = Path(tempfile.mkdtemp(prefix="ea-test-dash-priv-"))
    d.chmod(0o700)
    return d


def test_loopback_needs_no_token():
    for addr in ("127.0.0.1", "127.0.1.1", "::1", "[::1]", "localhost"):
        assert pd.check_bind(addr, "") is None, addr
        assert pd.effective_bind(addr, "") == (addr, None), addr


def test_other_addresses_need_a_token_and_fall_back_to_loopback():
    for addr in ("100.101.102.103", "192.168.1.5", "0.0.0.0", "::", "dash.example"):
        why = pd.check_bind(addr, "")
        assert why and "EA_DASH_TOKEN" in why, addr
        assert pd.effective_bind(addr, "") == ("127.0.0.1", why), addr
        assert pd.check_bind(addr, "s3cret-token-value") is None, addr
        assert pd.effective_bind(addr, "s3cret-token-value") == (addr, None), addr


def test_the_fallback_file_is_private_and_cleared():
    marker = _private() / pd.FALLBACK_FILE
    pd.record_fallback("100.101.102.103", "needs a token", marker)
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    doc = json.loads(marker.read_text())
    assert doc["wanted"] == "100.101.102.103" and doc["bound"] == "127.0.0.1"
    pd.record_fallback("100.101.102.103", None, marker)
    assert not marker.exists()
    pd.record_fallback("127.0.0.1", None, marker)   # nothing to remove: no error


def test_the_token_comes_from_the_env_else_the_owner_secrets(monkeypatch):
    asked = []

    def secret(name):
        asked.append(name)
        return " from-secrets-env "

    monkeypatch.setattr(pd_owner_trust, "owner_secret", secret)
    monkeypatch.setenv("EA_DASH_TOKEN", "from-env")
    assert pd._dash_token() == "from-env" and asked == []
    monkeypatch.delenv("EA_DASH_TOKEN")
    assert pd._dash_token() == "from-secrets-env" and asked == ["EA_DASH_TOKEN"]


def _start(env_extra: dict, priv: Path):
    """The server on a free port, with a scratch private dir and EA tree."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.pop("EA_DASH_TOKEN", None)
    scratch = Path(tempfile.mkdtemp(prefix="ea-test-dash-ea-"))
    env.update({"EA_PRIVATE_DIR": str(priv), "EA_DIR": str(scratch / ".ea"),
                "EA_DASH_PORT": "0"})
    env.update(env_extra)
    return subprocess.Popen([sys.executable, str(ROOT / "preprocessor" / "project_dashboard.py")],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


def _first_lines(proc, marker: Path | None = None, timeout: float = 30.0) -> tuple[str, str]:
    """stdout's first line once the server is up, and stderr so far."""
    deadline = time.time() + timeout
    while time.time() < deadline and proc.poll() is None:
        if marker is None or marker.exists():
            break
        time.sleep(0.1)
    out = proc.stdout.readline()
    proc.terminate()
    _, err = proc.communicate(timeout=30)
    return out, err


def test_main_falls_back_to_loopback_without_exiting():
    priv = _private()
    marker = priv / pd.FALLBACK_FILE
    proc = _start({"EA_DASH_BIND": "0.0.0.0"}, priv)
    out, err = _first_lines(proc, marker)
    assert "http://127.0.0.1:" in out, (out, err)
    assert "WARNING" in err and "refusing to bind 0.0.0.0" in err and "EA_DASH_TOKEN" in err
    doc = json.loads(marker.read_text())
    assert doc["wanted"] == "0.0.0.0" and doc["bound"] == "127.0.0.1"


def test_a_token_allows_the_address_and_clears_the_fallback_file():
    priv = _private()
    marker = priv / pd.FALLBACK_FILE
    marker.write_text("{}")
    marker.chmod(0o600)
    proc = _start({"EA_DASH_BIND": "0.0.0.0", "EA_DASH_TOKEN": "a-long-random-test-token"}, priv)
    out, err = _first_lines(proc)
    assert "http://0.0.0.0:" in out, (out, err)
    assert "WARNING" not in err
    assert not marker.exists(), "a start on the asked address clears the fallback file"
