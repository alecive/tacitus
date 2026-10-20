#!/usr/bin/env bash
#
# enable-tool-gateway.sh: the owner's tool gateway (preprocessor/tool_gateway.py)
# as a systemd USER service of the owner. Run as the owner, no root:
#
#     bash deploy/enable-tool-gateway.sh            # install + start
#     bash deploy/enable-tool-gateway.sh --status   # is it up
#     bash deploy/enable-tool-gateway.sh --rollback # stop + remove
#
# The gateway gives the on-prem model tool calling on loopback: a harness
# (Claude Code through ANTHROPIC_BASE_URL, opencode, Qwen Code) sends its
# tool list, the gateway renders it into the model's own tool prompt and
# parses the <tool_call> blocks back into tool calls. It runs as the owner
# because remote_llm.chat holds the endpoint key; a harness gets only the
# bearer token (`tool_gateway.py env` prints the environment a lane needs).
#
# The resident (ea-bot) reaches it only after the resident network policy
# allows the port: re-run `sudo bash deploy/enable-resident-netpolicy.sh`
# once after this (the policy renders tool_gateway.PORT as the `gateway`
# entry). Without that step the gateway serves owner processes only.
#
# Runs from the deployed copy (/opt/ea-system when present, else this
# tree) with the repo's venv when it has one. The user's manager must
# linger (`loginctl enable-linger <owner>`) for the service to survive
# logout; the script says so when it does not.
set -uo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY=/opt/ea-system; [ -d "$DEPLOY/preprocessor" ] || DEPLOY="$SRC"
PYBIN="$SRC/.venv/bin/python3"; [ -x "$PYBIN" ] || PYBIN=/usr/bin/python3
EA_BOT_HOME="$(eval echo ~ea-bot 2>/dev/null || echo /home/ea-bot)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/ea-tool-gateway.service"
PRIVATE="${EA_PRIVATE_DIR:-$HOME/.ea-private}"

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }
fail(){ red "FAIL: $*"; exit 1; }

[ "$(id -u)" != 0 ] || fail "run as the owner, not root"

status(){
  systemctl --user --no-pager status ea-tool-gateway.service 2>&1 | head -5
  "$PYBIN" "$DEPLOY/preprocessor/tool_gateway.py" status
}

case "${1:-}" in
  --status) status; exit 0 ;;
  --rollback)
    systemctl --user disable --now ea-tool-gateway.service 2>/dev/null
    rm -f "$UNIT"; systemctl --user daemon-reload
    grn "removed ea-tool-gateway.service"; exit 0 ;;
  "") ;;
  *) fail "unknown option $1" ;;
esac

[ -f "$PRIVATE/remote-llm.json" ] || fail "$PRIVATE/remote-llm.json is missing: no endpoint to gate"
mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<UNIT
[Unit]
Description=EA tool gateway (tool calling for the on-prem model, loopback only)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$DEPLOY
Environment=HOME=$HOME
Environment=EA_DIR=$EA_BOT_HOME/.ea
ExecStart=$PYBIN $DEPLOY/preprocessor/tool_gateway.py serve
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now ea-tool-gateway.service || fail "the service did not start"
sleep 1
status
if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != "yes" ]; then
  ylw "the owner's user manager does not linger: run  sudo loginctl enable-linger $(id -un)  or the gateway stops at logout"
fi
grn "installed $UNIT"
ylw "next, once, as root:  sudo bash $SRC/deploy/enable-resident-netpolicy.sh   (opens the gateway port to ea-bot)"
