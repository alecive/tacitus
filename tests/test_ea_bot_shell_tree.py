"""scripts/ea-bot-shell.sh runs as the owner and runs Python and reads deny
lists from the tree it sits in. ea-bot owns the directory above its
~/ea-system and can swap that tree, so the script refuses to run from a
tree under ea-bot's home and names /opt/ea-system instead."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = (ROOT / "scripts" / "ea-bot-shell.sh").read_text()


def _guard() -> str:
    start = SHELL.index("OWNER_CODE_DIR=/opt/ea-system")
    end = SHELL.index("esac\n", start) + len("esac\n")
    return SHELL[start:end]


def _run_guard(where: Path, home: Path) -> subprocess.CompletedProcess:
    scripts = where / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "guard.sh"
    script.write_text("set -euo pipefail\n" + _guard() + 'echo "code=$CODE_DIR"\n')
    return subprocess.run(["bash", str(script)], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "EA_BOT_HOME": str(home)})


def test_the_shell_refuses_to_run_from_ea_bots_tree(tmp_path):
    home = tmp_path / "ea-bot-home"
    r = _run_guard(home / "ea-system", home)
    assert r.returncode == 1 and "refusing to run owner-side code" in r.stderr
    assert "/opt/ea-system" in r.stderr


def test_the_shell_runs_from_a_tree_outside_ea_bots_home(tmp_path):
    home = tmp_path / "ea-bot-home"
    home.mkdir()
    r = _run_guard(tmp_path / "checkout", home)
    assert r.returncode == 0 and f"code={tmp_path / 'checkout'}" in r.stdout


def test_a_link_into_ea_bots_tree_is_resolved_first(tmp_path):
    home = tmp_path / "ea-bot-home"
    (home / "ea-system").mkdir(parents=True)
    link = tmp_path / "looks-like-mine"
    link.symlink_to(home / "ea-system")
    scripts = home / "ea-system" / "scripts"
    scripts.mkdir()
    (scripts / "guard.sh").write_text("set -euo pipefail\n" + _guard())
    r = subprocess.run(["bash", str(link / "scripts" / "guard.sh")], capture_output=True,
                       text=True, env={"PATH": "/usr/bin:/bin", "EA_BOT_HOME": str(home)})
    assert r.returncode == 1


def test_the_owner_side_python_comes_from_the_checked_tree():
    body = SHELL[SHELL.index("esac\n"):]
    assert "$SCRIPT_DIR/../preprocessor" not in SHELL
    for script in ("ea_config.py", "plugin_registry.py", "slack_sessions.py"):
        assert f'"$CODE_DIR/preprocessor/{script}"' in body, script
    # the inline snippets never import from the caller's working directory
    assert "python3 -c" not in SHELL.replace("`python3 -c`", "")
    assert SHELL.index("EA_BOT_HOME=$(") < SHELL.index("OWNER_CODE_DIR=")
