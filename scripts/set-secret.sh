#!/usr/bin/env bash
#
# Put one owner secret into the private secrets file, typed or pasted at a
# hidden prompt, so it never lands in shell history, a process list or a
# terminal scrollback.
#
#   scripts/set-secret.sh REMOTE_LLM_API_KEY
#
# The file is <private dir>/secrets.env (default ~/.ea-private/secrets.env),
# mode 600, owned by you; ea-bot cannot read it. An existing line for the
# same name is replaced; every other line is kept. Only names the code reads
# are accepted (owner_trust.SECRET_KEYS), since any other name would be
# ignored. Restart what uses the secret afterwards (the line it prints says
# how), because owner processes read the file when they start.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="${1:-}"
ALLOWED=$(python3 -c "
import sys; sys.path.insert(0, sys.argv[1])
import owner_trust; print(' '.join(sorted(owner_trust.SECRET_KEYS)))" "$REPO_ROOT/preprocessor")

if [[ -z "$NAME" ]] || ! grep -qw -- "$NAME" <<< "$ALLOWED"; then
    echo "usage: scripts/set-secret.sh NAME" >&2
    echo "NAME is one of: $ALLOWED" >&2
    exit 2
fi
if [[ "$(id -un)" == "ea-bot" ]]; then
    echo "set-secret: run this as yourself, not as ea-bot" >&2
    exit 1
fi

PRIV="${EA_PRIVATE_DIR:-$HOME/.ea-private}"
FILE="$PRIV/secrets.env"
umask 077
mkdir -p "$PRIV"
chmod 700 "$PRIV"
if [[ -L "$FILE" ]]; then
    echo "set-secret: $FILE is a symlink; refusing" >&2
    exit 1
fi

printf 'Paste %s and press Enter (nothing will show): ' "$NAME" >&2
IFS= read -rs VALUE
echo >&2
VALUE="${VALUE//$'\r'/}"
VALUE="$(printf '%s' "$VALUE" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
if [[ -z "$VALUE" ]]; then
    echo "set-secret: nothing pasted; $FILE unchanged" >&2
    exit 1
fi
if [[ "$VALUE" == *$'\n'* || "$VALUE" == *" "* ]]; then
    echo "set-secret: the value has a space or a line break; $FILE unchanged" >&2
    exit 1
fi

TMP="$(mktemp "$PRIV/.secrets.env.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
if [[ -f "$FILE" ]]; then
    grep -v -E "^[[:space:]]*(export[[:space:]]+)?${NAME}=" "$FILE" > "$TMP" || true
fi
printf '%s=%s\n' "$NAME" "$VALUE" >> "$TMP"
chmod 600 "$TMP"
mv -f "$TMP" "$FILE"
trap - EXIT
unset VALUE

LEN=$(grep -E "^${NAME}=" "$FILE" | head -1 | cut -d= -f2- | tr -d '\n' | wc -c)
echo "set-secret: $NAME saved to $FILE ($LEN characters, mode 600)."
CHECK=$(python3 -c "
import sys; sys.path.insert(0, sys.argv[1])
import owner_trust; print('readable' if owner_trust.owner_secret(sys.argv[2]) else 'NOT readable')" "$REPO_ROOT/preprocessor" "$NAME" 2>/dev/null || echo "not checked")
echo "set-secret: the owner code finds it: $CHECK."
echo "set-secret: a running service reads it on its next start (sudo systemctl restart ea-chat-bot ea-mail-mediator)."
