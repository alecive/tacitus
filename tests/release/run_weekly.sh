#!/usr/bin/env bash
#
# The weekly release tests (tests/release/run_release.py), for cron.
#
# Runs as the owner: only an owner process can reach the remote endpoint,
# and the Claude runs are this user's own `claude -p`. Skips when the
# prompt-affecting code, the two models and the switch values all match
# the last full run, so a quiet week costs nothing. A red, amber or
# harness verdict goes to the health channel; the log keeps the report.
#
# Usage:
#   tests/release/run_weekly.sh            # cron: skip when unchanged
#   tests/release/run_weekly.sh --force    # run regardless
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PRIVATE_DIR="${EA_PRIVATE_DIR:-$HOME/.ea-private}"
LOG_DIR="$PRIVATE_DIR/logs"
mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR" 2>/dev/null || true

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# The owner's on/off setting: `release_tests_enabled` in
# <EA_PRIVATE_DIR>/evals.json (tests/evals/eval_gates.py), never
# ea-config.json, which the resident writes. A helper that fails reads as
# off.
ENABLED=$(python3 "$REPO_ROOT/tests/evals/eval_gates.py" enabled release || echo "False")
if [[ "$ENABLED" != "True" ]]; then
    log "Release tests disabled (release_tests_enabled in the owner's evals.json, or an unreadable gate). Skipping."
    exit 0
fi

# The bot's Claude default, so the suite tests the model the chat lanes run on.
if [[ -z "${EA_MODEL:-}" && -f "$PRIVATE_DIR/bot.env" ]]; then
    EA_MODEL=$(grep -E '^EA_MODEL=' "$PRIVATE_DIR/bot.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' ')
    export EA_MODEL
fi

ARGS=(--alert)
if [[ "${1:-}" != "--force" ]]; then
    ARGS+=(--skip-if-unchanged)
fi

log "Release tests starting (claude ${EA_MODEL:-default})"
python3 "$SCRIPT_DIR/run_release.py" "${ARGS[@]}"
RC=$?
case $RC in
    0) log "Release tests: green." ;;
    1) log "Release tests: red or amber (see the report above; alert sent)." ;;
    2) log "Release tests: harness failure (not a regression; alert sent)." ;;
    3) log "Release tests: skipped." ;;
    *) log "Release tests: run_release.py exited $RC." ;;
esac
exit $RC
