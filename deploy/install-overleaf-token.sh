#!/usr/bin/env bash
#
# install-overleaf-token.sh: give the ea-bot account its Overleaf git token.
#
# The EA has an Overleaf account of its own. The owner invites that account
# to each project the EA may read (Viewer) or write (Editor), and the
# collaborator list in Overleaf is the whole boundary: a token minted on the
# EA account reaches those projects and no other. The owner's own token
# never enters ea-bot space.
#
# The token lives at ~ea-bot/.config/overleaf/token (ea-bot, 0600), next to
# the EA's other per-identity tokens, and git reads it through a credential
# helper that answers `get` only. git's stock `store` helper is not used for
# this host on purpose: it erases a credential from its file after any
# failed authentication, and the EA's first probe of a project it is not a
# member of would silently remove the token. The helper here ignores
# `store` and `erase`, so a refused project costs nothing.
#
#     sudo bash deploy/install-overleaf-token.sh TOKEN_FILE [--check PROJECT_ID]
#     sudo bash deploy/install-overleaf-token.sh --status
#     sudo bash deploy/install-overleaf-token.sh --helper-only
#
# TOKEN_FILE holds the token on one line (a `-` reads stdin). The file is
# consumed: it is shredded once the installed copy matches it. --check
# runs `git ls-remote` as ea-bot against one project; "Authentication
# failed" there means the EA account is not a member of that project yet,
# while Overleaf's "Enter your Git authentication token" banner means the
# token itself is not accepted. --helper-only installs the helper and the
# git config without a token (for a host waiting on its first token).
# Idempotent: every run rewrites the helper and the config, and removes any
# git.overleaf.com line the stock store file still holds.

set -euo pipefail

EA_USER="${EA_USER:-ea-bot}"
EA_HOME=$(getent passwd "$EA_USER" | cut -d: -f6)
[ -n "$EA_HOME" ] || { echo "no such user: $EA_USER" >&2; exit 2; }
CONF_DIR="$EA_HOME/.config/overleaf"
TOKEN_FILE="$CONF_DIR/token"
HELPER="$CONF_DIR/git-credential-overleaf"
HOST_KEY="credential.https://git.overleaf.com.helper"

as_bot() { sudo -n -u "$EA_USER" env -i HOME="$EA_HOME" PATH=/usr/bin:/bin bash -c "cd '$EA_HOME' && $1"; }

status() {
    echo "helper:      $(as_bot "test -x '$HELPER'" && echo installed || echo missing)"
    echo "git config:  $(as_bot "git config --global --get '$HOST_KEY'" 2>/dev/null || echo unset)"
    if as_bot "test -s '$TOKEN_FILE'"; then
        echo "token:       present ($(as_bot "stat -c '%U %a %y' '$TOKEN_FILE'" | cut -c1-30), expires one year after mint)"
    else
        echo "token:       absent"
    fi
    echo "store lines: $(as_bot "grep -c git.overleaf.com ~/.git-credentials 2>/dev/null || true") for git.overleaf.com in the stock store (should be 0)"
}

install_helper() {
    as_bot "mkdir -p '$CONF_DIR' && chmod 700 '$CONF_DIR'"
    as_bot "cat > '$HELPER'" <<'EOF'
#!/bin/sh
# git credential helper for git.overleaf.com: answers `get` from the token
# file beside it, ignores `store` and `erase` so a failed login never
# removes the token. Username is the literal `git`, as Overleaf expects.
case "$1" in
    get)
        f="$(dirname "$0")/token"
        [ -s "$f" ] || exit 0
        printf 'username=git\npassword=%s\n' "$(head -n1 "$f" | tr -d '\r\n ')"
        ;;
esac
exit 0
EOF
    as_bot "chmod 700 '$HELPER'"
    as_bot "git config --global '$HOST_KEY' '$HELPER'"
    # The stock store must not hold an overleaf line: git would try it first,
    # and a failure there is what erased the token before this helper existed.
    as_bot "if [ -f ~/.git-credentials ]; then { grep -v git.overleaf.com ~/.git-credentials || true; } > ~/.git-credentials.new && chmod 600 ~/.git-credentials.new && mv ~/.git-credentials.new ~/.git-credentials; fi"
}

install_token() {
    local src="$1" tmp
    tmp=$(mktemp)
    trap "rm -f '$tmp'" EXIT
    if [ "$src" = "-" ]; then tr -d '\r\n ' > "$tmp"; else tr -d '\r\n ' < "$src" > "$tmp"; fi
    [ -s "$tmp" ] || { echo "empty token" >&2; exit 2; }
    as_bot "cat > '$TOKEN_FILE' && chmod 600 '$TOKEN_FILE'" < "$tmp"
    local want have
    want=$(sha256sum < "$tmp" | cut -c1-16)
    have=$(as_bot "cat '$TOKEN_FILE'" | sha256sum | cut -c1-16)
    [ "$want" = "$have" ] || { echo "installed token does not match the source" >&2; exit 1; }
    if [ "$src" != "-" ]; then shred -u "$src"; echo "token installed; $src shredded"; else echo "token installed"; fi
}

check_project() {
    local id="$1" out
    if out=$(as_bot "GIT_TERMINAL_PROMPT=0 git ls-remote 'https://git.overleaf.com/$id'" 2>&1); then
        echo "project $id: reachable ($(echo "$out" | head -1 | cut -c1-7))"
    elif echo "$out" | grep -q "Enter your Git authentication token"; then
        echo "project $id: token not accepted by Overleaf (revoked, expired or mistyped)"; return 1
    elif echo "$out" | grep -q "Authentication failed"; then
        echo "project $id: token accepted, but the EA account is not a member of this project"; return 1
    else
        echo "project $id: $(echo "$out" | tail -1)"; return 1
    fi
}

case "${1:-}" in
    --status) status ;;
    --helper-only) install_helper; status ;;
    "") echo "usage: $0 TOKEN_FILE [--check PROJECT_ID] | --status | --helper-only" >&2; exit 2 ;;
    *)
        install_helper
        install_token "$1"
        if [ "${2:-}" = "--check" ] && [ -n "${3:-}" ]; then check_project "$3" || true; fi
        status
        ;;
esac
