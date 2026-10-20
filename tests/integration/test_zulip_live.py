"""Live Zulip smoke: runs scripts/zulip_smoke.py against a real server.

Skipped unless every EA_ZULIP_TEST_* variable below is set. The smoke
prints PASS, FAIL or SKIP per item and exits 1 when any item fails; its
output is shown when the test fails.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "scripts" / "zulip_smoke.py"

ENV = {
    "site": "EA_ZULIP_TEST_SITE",
    "bot-email": "EA_ZULIP_TEST_BOT_EMAIL",
    "bot-key": "EA_ZULIP_TEST_BOT_KEY",
    "owner-email": "EA_ZULIP_TEST_OWNER_EMAIL",
    "owner-key": "EA_ZULIP_TEST_OWNER_KEY",
}
MISSING = [name for name in ENV.values() if not os.environ.get(name)]

pytestmark = pytest.mark.skipif(
    bool(MISSING), reason=f"live Zulip not configured: {', '.join(MISSING)} unset")


def _live_bot_is_on_this_site(site: str) -> bool:
    """True when the production bot is configured on the same Zulip site:
    the smoke runs its own bot instance, and two instances would answer the
    same traffic (and the live one would send test lines to the model)."""
    import json
    try:
        cfg = json.load(open("/home/ea-bot/.ea/config/ea-config.json")).get("chat", {})
    except OSError:
        return False
    return cfg.get("frontend") == "zulip" and cfg.get("zulip", {}).get("site", "").rstrip("/") == site.rstrip("/")


def test_zulip_smoke():
    site = os.environ.get("EA_ZULIP_TEST_SITE", "")
    if _live_bot_is_on_this_site(site) and not os.environ.get("EA_ZULIP_TEST_FORCE"):
        pytest.skip(f"the production bot is configured on {site}; the smoke would run a second "
                    "bot instance against the same realm (set EA_ZULIP_TEST_FORCE=1 to override)")
    cmd = [sys.executable, str(SMOKE)]
    for flag, name in ENV.items():
        cmd += [f"--{flag}", os.environ[name]]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                       cwd=str(ROOT))
    print(r.stdout)
    assert r.returncode == 0, (
        f"zulip_smoke.py exited {r.returncode}\n"
        f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr[-4000:]}")
