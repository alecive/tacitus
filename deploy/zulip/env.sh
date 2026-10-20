# env.sh: where the Zulip stack's env file lives. Sourced by compose.sh and
# scripts/zulip_switch.sh; defines zulip_env_file.
#
# The file holds the stack's database, queue and cache passwords and the
# Django secret key, so it lives in the owner-private directory, outside
# the repo checkout (which the deploy archives and other tools walk):
#
#   $ZULIP_ENV_FILE                       when set
#   ${EA_PRIVATE_DIR:-~/.ea-private}/zulip.env   the default
#   deploy/zulip/.env                     the fallback (gitignored), with a
#                                         warning to move it
#
# The owner steps for the move are in docs/zulip-switch.md ("The server's
# env file and two-factor sign-in").

zulip_env_file() {
    local here priv
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    priv="${EA_PRIVATE_DIR:-$HOME/.ea-private}"
    if [ -n "${ZULIP_ENV_FILE:-}" ]; then
        printf '%s\n' "$ZULIP_ENV_FILE"
    elif [ -f "$priv/zulip.env" ]; then
        printf '%s\n' "$priv/zulip.env"
    else
        if [ -f "$here/.env" ]; then
            echo "warning: the Zulip env file is in the repo checkout ($here/.env);" \
                 "move it to $priv/zulip.env (docs/zulip-switch.md)" >&2
        fi
        printf '%s\n' "$here/.env"
    fi
}

# Warn when the env file is a link or anyone but its owner can read it.
zulip_env_check() {
    local f="$1" mode
    [ -e "$f" ] || { echo "warning: no Zulip env file at $f" >&2; return 0; }
    if [ -L "$f" ]; then
        echo "warning: $f is a symlink; keep the real file in the private directory" >&2
    fi
    mode="$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f" 2>/dev/null || echo "")"
    case "$mode" in
        600|400) ;;
        *) echo "warning: $f is mode ${mode:-unknown}; chmod 600 it" >&2 ;;
    esac
}
