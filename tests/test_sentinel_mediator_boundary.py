"""sentinel and the mail mediator follow the owner trust rules.

sentinel runs git in the lanes' copy only after the deploy hook's check and
with GIT_* cleared, and reads the resident's state files with the
resident's access. The mediator reads each request and writes each reply
with the resident's access.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Sandbox before import: owner_trust counts a process whose EA_DIR is the
# live tree as the owner.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-sentinel-mediator-")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import sentinel  # noqa: E402


def _repo(root: Path) -> Path:
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    (root / "f").write_text("x")
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                    "add", "f"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "c"], check=True, env=env)
    (root / ".git").chmod(0o755)
    return root


def test_git_runs_in_a_checked_tree(tmp_path, monkeypatch):
    tree = _repo(tmp_path / "deploy")
    monkeypatch.setattr(sentinel, "DEPLOY_DIR", tree)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))   # a leaked hook variable
    r = sentinel._git_in_deploy_tree("rev-parse", "--short", "HEAD")
    assert r is not None and r.returncode == 0 and r.stdout.strip()


def test_a_linked_or_shared_tree_is_refused(tmp_path, monkeypatch):
    tree = _repo(tmp_path / "deploy")
    link = tmp_path / "link"
    link.symlink_to(tree)
    monkeypatch.setattr(sentinel, "DEPLOY_DIR", link)
    assert sentinel._git_in_deploy_tree("rev-parse", "HEAD") is None
    monkeypatch.setattr(sentinel, "DEPLOY_DIR", tree)
    tree.chmod(0o775)
    assert sentinel._git_in_deploy_tree("rev-parse", "HEAD") is None
    tree.chmod(0o755)
    (tree / ".git").chmod(0o775)
    assert sentinel._git_in_deploy_tree("rev-parse", "HEAD") is None


def test_shared_state_is_read_with_the_residents_access(monkeypatch):
    import resident_io
    seen = []

    def fake_read(path):
        seen.append(Path(path).name)
        return json.dumps({"morning-briefing": {"ts": 1}})

    monkeypatch.setattr(resident_io, "read_text", fake_read)
    assert sentinel._load_shared_json(sentinel.POST_SUCCESS_FILE) == {
        "morning-briefing": {"ts": 1}}
    monkeypatch.setattr(resident_io, "read_text", lambda p: "[1, 2]")
    assert sentinel._load_shared_json(sentinel.LAST_RUN_FILE) == {}   # not an object
    assert seen == ["agent-post-success.json"]


def test_mediator_queue_io_goes_through_resident_io(tmp_path, monkeypatch):
    import mail_mediator
    import resident_io
    reads, writes = [], []
    monkeypatch.setattr(resident_io, "read_text", lambda p: reads.append(Path(p).name) or None)
    monkeypatch.setattr(resident_io, "replace_text",
                        lambda p, t, mode=0o664: writes.append((Path(p).name, mode)) or True)
    monkeypatch.setattr(mail_mediator.M, "QUEUE", tmp_path)
    req = tmp_path / "req-abc.json"
    req.write_text(json.dumps({"tool": "graph_actions", "verb": "list-mail"}))
    mail_mediator.handle(str(req))
    assert reads == ["req-abc.json"]            # read with the resident's access
    assert not req.exists()                     # an unreadable request is dropped
    mail_mediator._respond("abc", "out", "", 0)
    assert writes == [("resp-abc.json", 0o640)]


def test_mediator_main_makes_the_queue_as_the_resident():
    src = (REPO / "preprocessor" / "mail_mediator.py").read_text()
    main = src[src.index("def main():"):]
    assert "resident_io.make_dir(M.QUEUE, mode=0o2770)" in main
    assert "M.QUEUE.mkdir" not in main and "os.chmod(M.QUEUE" not in main
