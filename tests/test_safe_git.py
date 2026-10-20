"""The one git runner of the EA's git tools (safe_git.py).

The lanes can write the clones the git tools work in, so nothing in a
clone may change what git runs or what it shows. What must hold:
  * a clone whose .git/config holds a key outside the allowlist (a filter,
    fsmonitor, a pager, an external diff, a textconv, a hooks path, a push
    URL, a URL rewrite, an include, an alias, a gpg program, a credential
    helper) is refused, and so is a .git that is a file or a symlink, and a
    .git/info/attributes or grafts file;
  * no GIT_* variable and no global config reaches git, and the
    .gitattributes files of the work tree do not apply, so a `binary`
    attribute hides no change from the review diff and a filter they name
    never runs;
  * the review diff is every byte as text with the a/ and b/ prefixes;
  * site_tools fetches from and publishes to the repository github.json
    names, never to the clone's remote; doc_review pushes to its configured
    repository the same way; and every git call of the three tools goes
    through the runner;
  * the deny list names the other spellings of a raw push, and says the
    spelling rules are a backstop.

git runs for real against local repositories.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))

import publish_gate as gate  # noqa: E402
import resident_io  # noqa: E402
import safe_git as sg  # noqa: E402
import site_tools as st  # noqa: E402

SLUG = "t-site"
st.BOT_NAME = "t-bot"
st.BOT_EMAIL = "1+t-bot@users.noreply.github.com"
st.BOT_TOKEN_DIR = "t-bot"


def _run(*cmd, cwd=None) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    """A bare 'GitHub', a decoy remote the clone's own config names, and a
    clone with one committed page."""
    origin, decoy, clone = tmp_path / "origin.git", tmp_path / "decoy.git", tmp_path / "clone"
    for bare in (origin, decoy):
        _run("git", "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _run("git", "clone", "-q", str(origin), str(seed))
    (seed / "index.html").write_text("<p>v1</p>\n")
    _run("git", "-c", "user.name=o", "-c", "user.email=o@o", "add", "-A", cwd=seed)
    _run("git", "-c", "user.name=o", "-c", "user.email=o@o", "commit", "-qm", "init", cwd=seed)
    _run("git", "push", "-q", "origin", "main", cwd=seed)
    _run("git", "clone", "-q", str(origin), str(clone))
    return {"origin": origin, "decoy": decoy, "clone": clone, "tmp": tmp_path}


# ── the audit ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,value", [
    ("filter.evil.clean", "touch /tmp/x; cat"),
    ("core.fsmonitor", "/tmp/x"),
    ("core.pager", "touch /tmp/x"),
    ("core.hooksPath", "/tmp/hooks"),
    ("diff.external", "/tmp/x"),
    ("diff.evil.textconv", "/tmp/x"),
    ("diff.noprefix", "true"),
    ("merge.evil.driver", "/tmp/x"),
    ("remote.origin.pushurl", "https://evil.example/x.git"),
    ("url.https://evil.example/.insteadOf", "https://github.com/"),
    ("include.path", "/tmp/other"),
    ("alias.p", "push"),
    ("gpg.program", "/tmp/x"),
    ("credential.helper", "!touch /tmp/x"),
    ("core.sshCommand", "touch /tmp/x"),
])
def test_an_unsafe_key_in_the_clone_config_is_refused(repo, key, value):
    _run("git", "config", key, value, cwd=repo["clone"])
    with pytest.raises(sg.GitError, match="does not run with"):
        sg.run(repo["clone"], "status")


def test_info_attributes_grafts_and_a_redirected_git_dir_are_refused(repo, tmp_path):
    info = repo["clone"] / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("* filter=evil\n")
    with pytest.raises(sg.GitError, match="attributes"):
        sg.run(repo["clone"], "status")
    (info / "attributes").write_text("")
    assert sg.run(repo["clone"], "status").returncode == 0
    (info / "grafts").write_text("x\n")
    with pytest.raises(sg.GitError, match="grafts"):
        sg.run(repo["clone"], "status")
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").write_text(f"gitdir: {repo['clone'] / '.git'}\n")
    with pytest.raises(sg.GitError, match="plain directory"):
        sg.run(other, "status")


def test_the_stock_clone_config_passes(repo):
    keys = {k for k, _ in sg.audit(repo["clone"])}
    assert "remote.origin.url" in keys and "core.bare" in keys


def test_a_merge_or_file_mode_setting_in_the_clone_config_passes(repo):
    _run("git", "config", "pull.ff", "only", cwd=repo["clone"])
    _run("git", "config", "core.sharedRepository", "group", cwd=repo["clone"])
    keys = {k for k, _ in sg.audit(repo["clone"])}
    assert {"pull.ff", "core.sharedrepository"} <= keys


# ── what git runs and shows ───────────────────────────────────────────

def test_no_git_variable_global_config_or_worktree_attribute_applies(repo, monkeypatch):
    clone, tmp = repo["clone"], repo["tmp"]
    marker = tmp / "ran"
    home = tmp / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f"[filter \"evil\"]\n\tclean = touch {marker}; cat\n"
                                     f"[core]\n\tpager = touch {marker}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", f"touch {marker}")
    monkeypatch.setenv("GIT_DIR", str(tmp / "nowhere"))
    (clone / ".gitattributes").write_text("* filter=evil\n*.html binary\n")
    (clone / "index.html").write_text("<p>v2 kit@example.org</p>\n")
    ident = ("t", "t@t")
    assert sg.run(clone, "add", "-A", identity=ident).returncode == 0
    assert sg.run(clone, "commit", "-qm", "edit", identity=ident).returncode == 0
    diff = sg.run(clone, "diff", *sg.REVIEW_DIFF_FLAGS, "HEAD~1", "HEAD").stdout
    assert not marker.exists()
    assert "+++ b/index.html" in diff and "+<p>v2 kit@example.org</p>" in diff
    assert "Binary files" not in diff
    assert gate.scan_diff(diff)["counts"].get("email address") == 1


def test_the_overrides_name_every_program_and_prefix_key():
    keys = {kv.split("=", 1)[0] for kv in sg.OVERRIDES}
    for k in ("core.hooksPath", "core.fsmonitor", "core.pager", "core.attributesFile",
              "credential.helper", "diff.noprefix", "diff.srcPrefix", "diff.dstPrefix",
              "gpg.program", "commit.gpgSign"):
        assert k in keys, k
    for flag in ("--no-textconv", "--no-ext-diff", "--text", "--src-prefix=a/",
                 "--dst-prefix=b/"):
        assert flag in sg.REVIEW_DIFF_FLAGS
    env = sg.env({"GIT_EXTERNAL_DIFF": "x", "GIT_SSH_COMMAND": "x", "PATH": "/bin"})
    assert env["GIT_ATTR_SOURCE"] == sg.EMPTY_TREE and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "GIT_EXTERNAL_DIFF" not in env and "GIT_SSH_COMMAND" not in env


# ── site_tools: the URL comes from github.json ────────────────────────

@pytest.fixture
def site(repo, tmp_path, monkeypatch):
    clone = repo["clone"]
    _run("git", "config", "remote.origin.url", str(repo["decoy"]), cwd=clone)
    token = tmp_path / "token"
    token.write_text("not-a-real-token\n")
    monkeypatch.setattr(st, "SITES", {SLUG: {"title": "T", "repo": "o/t.github.io",
                                              "url": "https://t.example/", "private": True}})
    monkeypatch.setattr(st, "clone_dir", lambda slug: clone)
    monkeypatch.setattr(st, "token_file", lambda slug: token)
    monkeypatch.setattr(gate, "TOKEN_PATH", tmp_path / "card-tokens.json")
    monkeypatch.setattr(resident_io, "_owner_check", lambda: False)
    monkeypatch.delenv(gate.TOKEN_ENV, raising=False)
    return repo


def test_site_remote_url_is_built_from_github_json(site):
    assert st.remote_url(SLUG) == "https://github.com/o/t.github.io.git"
    st.SITES[SLUG]["repo"] = "o/../x"
    with pytest.raises(st.SiteError, match="owner/name"):
        st.remote_url(SLUG)


def test_site_publish_goes_to_the_configured_repo_not_the_clones_remote(site, monkeypatch):
    monkeypatch.setattr(st, "remote_url", lambda slug: str(site["origin"]))
    clone = site["clone"]
    (clone / ".gitattributes").write_text("*.html binary\n")
    (clone / "index.html").write_text("<p>v2</p>\n<p>call 303-555-0100</p>\n")
    sha = st.cmd_commit(SLUG, "Update the page")["commit"]
    pv = st.cmd_preview(SLUG, sha, 20000)
    assert "+<p>call 303-555-0100</p>" in pv["diff"]
    assert gate.scan_diff(pv["diff"])["counts"].get("phone number") == 1
    item = gate.item_of({"target": "site", "name": SLUG, "commit": sha}, pv)
    res = st.cmd_publish(SLUG, item["commit"], item["base"], wait=False, token=gate.mint(item))
    assert res["status"] == "published"
    assert _run("git", "--git-dir", str(site["origin"]), "rev-parse", "main").strip() == sha
    decoy = subprocess.run(["git", "--git-dir", str(site["decoy"]), "rev-parse", "--verify",
                            "-q", "main"], capture_output=True, text=True)
    assert decoy.returncode != 0


# ── every git call goes through the runner ────────────────────────────

@pytest.mark.parametrize("name", ["site_tools.py", "doc_review.py", "overleaf_tools.py"])
def test_the_git_tools_run_git_only_through_safe_git(name):
    src = (ROOT / "preprocessor" / name).read_text()
    assert "safe_git" in src
    assert not re.search(r"""\[\s*["']git["']""", src), name
    assert not re.search(r"""["'](?:push|fetch|ls-remote)["'],\s*(?:["']-q["'],\s*)?["']origin["']""",
                         src), name


def test_doc_review_pushes_to_the_configured_repository():
    import doc_review as dr
    old = dr.REPO
    try:
        dr.REPO = "o/doc-review"
        assert dr.remote_url() == "https://github.com/o/doc-review.git"
        dr.REPO = "o/x y"
        with pytest.raises(dr.ReviewError, match="owner/name"):
            dr.remote_url()
    finally:
        dr.REPO = old


# ── the deny list's push spellings ────────────────────────────────────

def _deny_rules() -> list[str]:
    text = (ROOT / "scripts" / "disallowed-tools.txt").read_text()
    return [ln.strip() for ln in text.splitlines()
            if ln.strip().startswith("Bash(") and not ln.lstrip().startswith("#")]


def _denied(command: str) -> bool:
    """Read the widest way a permission rule can match: `*` is anything."""
    return any(fnmatch.fnmatchcase(command, r[5:-1]) for r in _deny_rules())


@pytest.mark.parametrize("command", [
    "git push",
    "git push origin main",
    "/usr/bin/git push -q https://github.com/o/t.git HEAD:main",
    "GIT_DIR=/x/.git git push origin",
    "git -C /home/ea-bot/presentations/sites/x push",
    "git -c core.hooksPath=/dev/null push origin main",
    "git --git-dir=/x/.git push origin",
    "git --git-dir /x/.git --work-tree /x push",
    "git --work-tree=/x push",
    "git --no-pager push",
    "git send-pack https://github.com/o/t.git main",
    "git -C /x send-pack https://github.com/o/t.git main",
    "/usr/lib/git-core/git-send-pack https://github.com/o/t.git main",
    "git -c alias.p=push p origin main",
    "git config alias.ship push",
    "git -C /x config alias.ship '!git push'",
    "git subtree push --prefix=d origin main",
    "git remote-https origin https://github.com/o/t.git",
])
def test_the_deny_list_names_the_other_push_spellings(command):
    assert _denied(command), command


@pytest.mark.parametrize("command", [
    "python3 /home/ea-bot/ea-system/preprocessor/site_tools.py commit --site s --message m",
    "python3 /home/ea-bot/ea-system/preprocessor/overleaf_tools.py pull --dir /d",
    "python3 /home/ea-bot/ea-system/preprocessor/doc_review.py update --pr 3 --file f "
    "--message m",
])
def test_the_push_rules_leave_the_tools_verbs_alone(command):
    assert not _denied(command), command


def test_the_deny_list_calls_spelling_rules_a_backstop():
    text = (ROOT / "scripts" / "disallowed-tools.txt").read_text()
    assert "backstop, not a boundary" in text
    sec = (ROOT / "docs" / "SECURITY.md").read_text()
    assert "backstop, not a boundary" in sec
