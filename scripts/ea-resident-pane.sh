#!/usr/bin/env bash
#
# Manage the resident EA Claude pane: a long-lived INTERACTIVE Claude
# Code session running as ea-bot inside a tmux session on the owner's server.
# The Slack bot dispatches messages into it via resident_bridge.py
# (tmux paste-buffer in, transcript JSONL out). The owner can attach at any
# time and watch or type into the SAME conversation:
#
#     tmux attach -t ea-resident
#
# Interactive rather than an SDK daemon: interactive Claude Code bills
# against the Max subscription; the Agent SDK bills API tokens.
#
# Usage:
#   scripts/ea-resident-pane.sh ensure   # create/heal (idempotent, fast)
#   scripts/ea-resident-pane.sh status   # 0=healthy, 1=down (prints state)
#   scripts/ea-resident-pane.sh stop     # kill the pane (state retained;
#                                        # next ensure RESUMES the session)
#   scripts/ea-resident-pane.sh rotate   # mint a fresh session id (new
#                                        # conversation on next ensure)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMUX_SESSION="ea-resident"
STATE_FILE="/home/ea-bot/.ea/state/resident-pane.json"
CMD="${1:-ensure}"
# The state file is ea-bot space: it is written and removed as ea-bot.
OWNER_IO="$SCRIPT_DIR/owner-io.sh"
UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

_ensure_state() {
    if [[ ! -f "$STATE_FILE" ]]; then
        local sid
        sid=$(python3 -c 'import uuid; print(uuid.uuid4())')
        python3 -c '
import json, sys
from datetime import datetime, timezone
sys.stdout.write(json.dumps({"session_id": sys.argv[1],
                             "tmux_session": "ea-resident",
                             "created": datetime.now(timezone.utc).isoformat()}, indent=2))
' "$sid" | "$OWNER_IO" write "$STATE_FILE"
        echo "[resident] new session id ${sid:0:8}…"
    fi
}

_alive() {
    tmux has-session -t "$TMUX_SESSION" 2>/dev/null
}

case "$CMD" in
    ensure)
        _ensure_state
        # ea-bot transcripts live under ~ea-bot/.claude (mode 700);
        # g+x lets the bridge reach the files it reads via sudo paths.
        sudo -n -u ea-bot chmod g+x /home/ea-bot/.claude 2>/dev/null || true
        # Pre-accept Bypass Permissions for ea-bot (one-time dialog would
        # otherwise block the headless pane at boot — the Unix user IS
        # the sandbox here; deny-list + confirm tokens still apply).
        sudo -n -u ea-bot python3 -c "
import json
from pathlib import Path
p = Path('/home/ea-bot/.claude.json')
data = json.loads(p.read_text()) if p.exists() else {}
if not data.get('bypassPermissionsModeAccepted'):
    data['bypassPermissionsModeAccepted'] = True
    p.write_text(json.dumps(data, indent=2))
" 2>/dev/null || true
        if _alive; then
            echo "[resident] pane already running."
            exit 0
        fi
        # Respawn loop: an accidental claude exit (Ctrl-D, /exit, crash)
        # relaunches inner.sh, which RESUMES the same session id, so the
        # pane self-heals in ~5s with the conversation intact. Intentional
        # shutdown is `ea-resident-pane.sh stop`, which kills the session
        # and ends the loop. A fast crash-loop is bounded by the 5s sleep
        # and stays visible to the sentinel.
        tmux new-session -d -s "$TMUX_SESSION" -n claude \
            "while true; do bash '$SCRIPT_DIR/ea-resident-inner.sh'; \
             echo '[resident] claude exited — respawning in 5s '\
'(stop with: ea-resident-pane.sh stop)'; sleep 5; done"
        echo "[resident] pane started, auto-respawn on (attach: tmux attach -t $TMUX_SESSION)"
        ;;
    status)
        if _alive; then
            SID=$(python3 -c "import json;print(json.load(open('$STATE_FILE'))['session_id'])" 2>/dev/null || echo "?")
            echo "healthy session=$SID tmux=$TMUX_SESSION"
            exit 0
        fi
        echo "down"
        exit 1
        ;;
    stop)
        tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
        echo "[resident] stopped (session id retained; ensure resumes it)"
        ;;
    rotate)
        tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
        "$OWNER_IO" rm "$STATE_FILE"
        echo "[resident] state cleared — next ensure starts a fresh conversation"
        ;;
    rotate-daily)
        # Quiet-hour (pre-briefing) reset, CONDITIONAL on size: the
        # conversation persists across days for continuity and resets only
        # once it has grown enough to slow turns. The mid-day hard cap
        # (resident_bridge.maybe_rotate, 500KB) is the safety net between
        # these quiet-hour checks. This threshold is lower on purpose: a
        # 3:45 reset costs nobody a wait, a mid-day one costs a dispatch
        # 10-15s, so it waits for real bloat.
        # The bridge keeps its pending queue in the owner-private dir.
        PENDING="${EA_PRIVATE_DIR:-$HOME/.ea-private}/resident-pending.json"
        if [[ -s "$PENDING" ]] && python3 -c "
import json,sys
sys.exit(0 if json.load(open(sys.argv[1])) else 1)" "$PENDING" 2>/dev/null; then
            echo "[resident] rotate-daily skipped: pending dispatches exist"
            exit 0
        fi
        THRESHOLD=${EA_RESIDENT_ROTATE_THRESHOLD:-300000}
        SID=$(python3 -c "import json;print(json.load(open('$STATE_FILE'))['session_id'])" 2>/dev/null || echo "")
        # The id names a file under ea-bot's home, so it must be a uuid.
        [[ "$SID" =~ $UUID_RE ]] || SID=""
        TRANSCRIPT="/home/ea-bot/.claude/projects/-home-ea-bot-ea-system/${SID}.jsonl"
        SIZE=$(sudo -n -u ea-bot stat -c %s "$TRANSCRIPT" 2>/dev/null || echo 0)
        if [[ -n "$SID" ]] && [[ "$SIZE" -ge "$THRESHOLD" ]]; then
            echo "[resident] rotate-daily: transcript ${SIZE}B >= ${THRESHOLD}B — rotating for speed"
            tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
            "$OWNER_IO" rm "$STATE_FILE"
            bash "$0" ensure
            echo "[resident] daily rotation complete"
        else
            echo "[resident] rotate-daily: transcript ${SIZE}B < ${THRESHOLD}B — keeping conversation (continuity)"
            # Double as an overnight liveness heal: bring the pane back if
            # it died since the last dispatch, without wiping the session.
            bash "$0" ensure
        fi
        ;;
    *)
        echo "usage: $0 {ensure|status|stop|rotate}" >&2
        exit 2
        ;;
esac
