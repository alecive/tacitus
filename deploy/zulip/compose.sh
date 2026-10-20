#!/usr/bin/env bash
# compose.sh: docker compose for the EA's Zulip stack, with the env file from
# the owner-private directory (env.sh says where it is looked for).
#
#   deploy/zulip/compose.sh up -d
#   deploy/zulip/compose.sh logs --tail 50 zulip
#   deploy/zulip/compose.sh config --environment   # check what it reads
#
# Runs docker through `sg docker` when this shell is not yet in the group.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/zulip/env.sh
. "$HERE/env.sh"
ENVF="$(zulip_env_file)"
zulip_env_check "$ENVF"
if docker ps >/dev/null 2>&1; then
    exec docker compose -f "$HERE/docker-compose.yml" --env-file "$ENVF" "$@"
fi
args=""
for a in "$@"; do args+=" $(printf '%q' "$a")"; done
exec sg docker -c "docker compose -f $(printf '%q' "$HERE/docker-compose.yml") --env-file $(printf '%q' "$ENVF")$args"
