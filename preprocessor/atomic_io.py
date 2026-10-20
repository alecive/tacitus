"""Atomic file writes that a symlink in the target directory cannot subvert.

Several state and registry files live in group-writable directories
(`~/.ea/state`, setgid, group `ea`), and the long-running services and the
crons write them as the owner while the resident runs as `ea-bot`
in the same group. A write that names its temp file predictably
(`foo.json.tmp`) lets another group member pre-create that name as a
symlink, so the owner's write lands wherever the link points. The kernel's
`protected_symlinks` does not cover a group-writable directory that is not
also world-writable and sticky, so it is no help here.

`write_text` / `write_json` close that: the temp file is made with
`mkstemp` in the destination directory (a random name, `O_EXCL`, mode
0600), written, `fchmod`-ed to the requested mode, then `os.replace`-d
onto the destination. `mkstemp`'s `O_EXCL` refuses a name that already
exists, so a pre-created symlink cannot be written through; `os.replace`
renames onto the destination, which replaces a symlink sitting there
rather than following it. The write is atomic: a reader sees the old file
or the new one, never a half-written one.

`mode` defaults to 0o644, the umask result the previous `write_text`
calls produced, so a cross-user reader keeps its access; pass 0o600 for a
private file or 0o660 for one the group must write.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_text(path: str | Path, data: str, *, mode: int = 0o644,
               encoding: str = "utf-8") -> None:
    """Atomically write `data` to `path`. Raises on failure (the caller
    decides whether a failed state write is fatal)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: str | Path, obj: Any, *, mode: int = 0o644,
               indent: int | None = 2, sort_keys: bool = False) -> None:
    """Atomically write `obj` as JSON to `path`."""
    write_text(path, json.dumps(obj, indent=indent, sort_keys=sort_keys), mode=mode)
