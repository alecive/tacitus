#!/usr/bin/env python3
"""safe_git: git in the EA's own clones, with nothing the clone holds able
to change what git executes or what it shows.

site_tools.py, doc_review.py and overleaf_tools.py run git in clones that
the lanes can write: the file tools reach the clone directories, and the
resident pane runs in bypass mode. A clone's own .git/config, its
.git/info/attributes and the .gitattributes files it carries can name
programs git runs (filters, fsmonitor, hooks, an external diff, textconv,
a pager, a gpg program, an ssh command, a credential helper) or change the
text git prints (diff prefixes, a binary attribute that hides a change, a
URL rewrite, a second push URL). Every git call these tools make goes
through `run()` here, which:

  * clears every GIT_* variable, turns system and global config off, turns
    replace refs off, and reads attributes from the empty tree, so no
    .gitattributes file in the work tree or the index applies;
  * passes `-c` overrides for the single keys that name a program or a
    diff prefix, which outrank the clone's own config;
  * refuses a clone whose .git is not a plain directory, whose .git/config
    holds a key outside SAFE_KEYS (filters, diff and merge drivers,
    includes, URL rewrites, push URLs, proxies, aliases, credential
    helpers: none of these is on the list), or whose .git/info holds an
    attributes or grafts file.

The tools fetch from and push to a URL built from owner-trusted config
(github.json, or a project id the owner's click binds), never the clone's
remote, and the publish preview diffs with REVIEW_DIFF_FLAGS.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

# The empty tree: attributes are read from it, so none apply.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# The keys a clone's .git/config may hold. `git config --list` prints the
# section and the key name in lower case; a subsection keeps its case.
SAFE_KEYS = re.compile(
    r"^(?:core\.(?:repositoryformatversion|filemode|bare|logallrefupdates|ignorecase"
    r"|precomposeunicode|symlinks)"
    r"|remote\.origin\.(?:url|fetch)"
    r"|branch\.[A-Za-z0-9._/-]+\.(?:remote|merge)"
    r"|user\.(?:name|email)"
    # A merge setting and a file-mode setting: neither runs a program.
    r"|pull\.ff|core\.sharedrepository)$")

# Single keys that name a program or change what a diff or log prints. A
# command-line `-c` outranks the clone's config.
OVERRIDES: tuple[str, ...] = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.pager=cat",
    "core.editor=false",
    "core.attributesFile=/dev/null",
    "core.sshCommand=ssh",
    "credential.helper=",
    "diff.noprefix=false",
    "diff.mnemonicPrefix=false",
    "diff.relative=false",
    "diff.srcPrefix=a/",
    "diff.dstPrefix=b/",
    "color.ui=never",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
    "push.gpgSign=false",
    "log.showSignature=false",
    "gpg.program=false",
    "gpg.ssh.program=false",
    "gpg.x509.program=false",
    "protocol.ext.allow=never",
    "submodule.recurse=false",
    "fetch.recurseSubmodules=false",
    "push.recurseSubmodules=no",
    "sequence.editor=false",
)

# The diff a publish card shows and scans: every byte as text, the stock
# a/ and b/ prefixes, no program of the clone's.
REVIEW_DIFF_FLAGS: tuple[str, ...] = ("--no-textconv", "--no-ext-diff", "--text", "--no-color",
                                      "--src-prefix=a/", "--dst-prefix=b/")

DEFAULT_TIMEOUT_S = 120


class GitError(Exception):
    """A refused clone or a failed git command."""


def env(base: dict | None = None) -> dict[str, str]:
    """The environment every git call runs with."""
    src = os.environ if base is None else base
    out = {k: v for k, v in src.items() if not k.startswith("GIT_")}
    out.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1", GIT_ATTR_SOURCE=EMPTY_TREE, GIT_PAGER="cat",
               PAGER="cat")
    return out


def credential_helper(token_file: Path, username: str) -> str:
    """A `get`-only helper that answers from the token file, for `-c`."""
    path = shlex.quote(str(token_file))
    return (f'!f() {{ test "$1" = get || exit 0; echo username={username}; '
            f'printf "password=%s\\n" "$(tr -d "[:space:]" < {path})"; }}; f')


def argv(*args: str, clone: Path | None = None, identity: tuple[str, str] | None = None,
         credentials: dict[str, str] | None = None) -> list[str]:
    """The full git command line: the overrides, the identity, a credential
    helper per URL prefix, `-C clone` and `args`."""
    cmd = ["git"]
    for kv in OVERRIDES:
        cmd += ["-c", kv]
    for prefix, helper in (credentials or {}).items():
        cmd += ["-c", f"credential.{prefix}.helper=", "-c", f"credential.{prefix}.helper={helper}"]
    if identity:
        cmd += ["-c", f"user.name={identity[0]}", "-c", f"user.email={identity[1]}"]
    if clone is not None:
        cmd += ["-C", str(clone)]
    return cmd + list(args)


def config_entries(clone: Path) -> list[tuple[str, str]]:
    """(key, value) for each entry of the clone's own .git/config, read as a
    plain file (no includes followed)."""
    r = subprocess.run(["git", "config", "--file", str(Path(clone) / ".git" / "config"),
                        "--no-includes", "--list", "-z"], capture_output=True, text=True,
                       errors="replace", timeout=30, env=env())
    if r.returncode != 0:
        raise GitError(f"cannot read {clone}/.git/config: {r.stderr.strip()[:200]}")
    out = []
    for entry in r.stdout.split("\0"):
        if entry:
            key, _, value = entry.partition("\n")
            out.append((key, value))
    return out


def audit(clone: Path, safe_keys: re.Pattern = SAFE_KEYS) -> list[tuple[str, str]]:
    """The clone's config entries, once nothing in its .git can change what
    git runs or shows. Raises GitError naming the first problem."""
    clone = Path(clone)
    git_dir = clone / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise GitError(f"{clone} is not a git clone (.git must be a plain directory)")
    if (git_dir / "commondir").exists():
        raise GitError(f"{clone} is a linked worktree; use a plain clone")
    for name in ("attributes", "grafts"):
        p = git_dir / "info" / name
        if p.is_symlink() or (p.exists() and (not p.is_file() or p.stat().st_size)):
            raise GitError(f"{p} changes what git runs or shows; remove it and try again")
    entries = config_entries(clone)
    for key, _value in entries:
        if not safe_keys.match(key):
            raise GitError(f"{clone}/.git/config sets {key}, which this tool does not run "
                           f"with; remove that line and try again")
    return entries


def run(clone: Path, *args: str, identity: tuple[str, str] | None = None,
        credentials: dict[str, str] | None = None, timeout: int = DEFAULT_TIMEOUT_S,
        text: bool = True, safe_keys: re.Pattern = SAFE_KEYS) -> subprocess.CompletedProcess:
    """git `args` in the audited clone. Returns the finished process; the
    caller reads the exit code."""
    audit(clone, safe_keys)
    kw = {"text": True, "errors": "replace"} if text else {}
    return subprocess.run(argv(*args, clone=clone, identity=identity, credentials=credentials),
                          capture_output=True, timeout=timeout, env=env(), **kw)


def run_outside(*args: str, identity: tuple[str, str] | None = None,
                credentials: dict[str, str] | None = None,
                timeout: int = DEFAULT_TIMEOUT_S) -> subprocess.CompletedProcess:
    """git `args` with no clone yet (a `clone`), under the same overrides."""
    return subprocess.run(argv(*args, identity=identity, credentials=credentials),
                          capture_output=True, text=True, errors="replace", timeout=timeout,
                          env=env())
