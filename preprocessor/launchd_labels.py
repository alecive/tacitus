"""
macOS LaunchDaemon labels for the owner-side services.

A new install uses the neutral prefix. An install deployed under the
legacy prefix keeps it: its plists, its sudoers rules and its logs all
carry that label, so the code finds whichever plist is installed and
only names the neutral label when neither exists. The shell installers
(deploy/macos/*.sh, deploy/enable-mail-mediation.sh) apply the same rule.
"""

from __future__ import annotations

from pathlib import Path

NEUTRAL_PREFIX = "org.ea-system"
LEGACY_PREFIX = "edu.colorado"  # legacy-launchd-label: installs deployed before the rename
PLIST_DIR = Path("/Library/LaunchDaemons")


def plist_path(label: str) -> Path:
    return PLIST_DIR / f"{label}.plist"


def prefix() -> str:
    """The prefix this host's services were installed under: the legacy
    one when its chat-bot or mediator plist exists, else the neutral one."""
    for name in ("ea-chat-bot", "ea-mail-mediator"):
        if plist_path(f"{LEGACY_PREFIX}.{name}").exists():
            return LEGACY_PREFIX
    return NEUTRAL_PREFIX


def label(name: str) -> str:
    """'ea-chat-bot' -> '<prefix>.ea-chat-bot' for this host."""
    return f"{prefix()}.{name}"
