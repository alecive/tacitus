"""The Zulip stack reads its env file from the owner-private directory and
ships with two-factor sign-in on.

Pins:
  * env.sh's order: ZULIP_ENV_FILE, then <private dir>/zulip.env, then the
    gitignored deploy/zulip/.env with a warning to move it; a group- or
    world-readable file is warned about;
  * compose.sh and scripts/zulip_switch.sh take the file from env.sh, and
    no doc or compose comment still tells the owner to pass
    deploy/zulip/.env;
  * the compose file sets TWO_FACTOR_AUTHENTICATION_ENABLED;
  * docs/zulip-switch.md has the owner steps and the Tailscale ACL snippet.
"""
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZDIR = ROOT / "deploy" / "zulip"


def _resolve(env_extra: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k not in ("ZULIP_ENV_FILE", "EA_PRIVATE_DIR")}
    env.update(env_extra)
    script = f'. "{ZDIR}/env.sh"; f="$(zulip_env_file)"; zulip_env_check "$f"; echo "$f"'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def test_the_private_file_wins_over_the_checkout():
    with tempfile.TemporaryDirectory() as td:
        priv = Path(td)
        (priv / "zulip.env").write_text("X=1\n")
        (priv / "zulip.env").chmod(0o600)
        r = _resolve({"EA_PRIVATE_DIR": str(priv)})
        assert r.stdout.strip() == str(priv / "zulip.env")
        assert r.stderr == ""


def test_an_explicit_file_wins_and_a_loose_mode_is_warned():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "other.env"
        f.write_text("X=1\n")
        f.chmod(0o644)
        r = _resolve({"EA_PRIVATE_DIR": td, "ZULIP_ENV_FILE": str(f)})
        assert r.stdout.strip() == str(f)
        assert "chmod 600" in r.stderr


def test_the_checkout_file_is_the_fallback():
    with tempfile.TemporaryDirectory() as td:
        r = _resolve({"EA_PRIVATE_DIR": td})
        assert r.stdout.strip() == str(ZDIR / ".env")


def test_compose_and_the_switch_script_use_env_sh():
    compose_sh = (ZDIR / "compose.sh").read_text()
    assert '. "$HERE/env.sh"' in compose_sh and '--env-file "$ENVF"' in compose_sh
    assert os.access(ZDIR / "compose.sh", os.X_OK)
    switch = (ROOT / "scripts" / "zulip_switch.sh").read_text()
    assert '. "$REPO/deploy/zulip/env.sh"' in switch and 'ENVF="$(zulip_env_file)"' in switch
    for path in (ZDIR / "README.md", ZDIR / "docker-compose.yml", ROOT / "docs" / "zulip-switch.md"):
        assert "--env-file deploy/zulip/.env" not in path.read_text(), path


def test_two_factor_sign_in_is_on():
    compose = (ZDIR / "docker-compose.yml").read_text()
    assert 'SETTING_TWO_FACTOR_AUTHENTICATION_ENABLED: "True"' in compose


def test_the_owner_steps_and_the_acl_snippet_are_documented():
    doc = (ROOT / "docs" / "zulip-switch.md").read_text()
    i = doc.index("## The server's env file and two-factor sign-in")
    sec = doc[i:doc.index("## Notifications", i)]
    assert "mv ~/ea-system/deploy/zulip/.env ~/.ea-private/zulip.env" in sec
    assert "deploy/zulip/compose.sh up -d" in sec
    assert "/account/two_factor/" in sec
    assert '"dst": ["tag:ea-host:8443"]' in sec and '"tests"' in sec
