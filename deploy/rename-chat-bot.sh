#!/usr/bin/env bash
# Root step: move the bot's system unit from ea-slack-bot.service to
# ea-chat-bot.service. The repo, the deploy hook, the sentinel and the
# doctor already use the new name; the hook and the sentinel fall back
# to the old unit until this has run.
#
#   sudo bash deploy/rename-chat-bot.sh
#
# What it does: copies the unit and its drop-in under the new name with
# ExecStart pointing at ea-chat-bot.py, grants the owner the same passwordless
# restart for the new unit, swaps the running service over (one restart,
# the bot drains in-flight turns for up to 80 s), then removes the old
# unit and the old sudoers grant. Idempotent.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0" >&2; exit 1; }

OLD=ea-slack-bot
NEW=ea-chat-bot
UNIT_DIR=/etc/systemd/system
OWNER="${SUDO_USER:?run with sudo from the owner account}"

if [ ! -f "$UNIT_DIR/$OLD.service" ] && [ -f "$UNIT_DIR/$NEW.service" ]; then
    echo "[rename] $NEW.service is already the unit; nothing to do"
    exit 0
fi
[ -f "$UNIT_DIR/$OLD.service" ] || { echo "[rename] no $OLD.service here" >&2; exit 1; }

echo "[rename] 1/5  unit $NEW.service"
sed "s#/$OLD\.py#/$NEW.py#g; s/EA Slack Bot/EA chat bot/" "$UNIT_DIR/$OLD.service" > "$UNIT_DIR/$NEW.service"
chmod 644 "$UNIT_DIR/$NEW.service"
if [ -d "$UNIT_DIR/$OLD.service.d" ]; then
    install -d -m 755 "$UNIT_DIR/$NEW.service.d"
    for f in "$UNIT_DIR/$OLD.service.d"/*.conf; do
        [ -f "$f" ] || continue
        sed "s#/$OLD\.py#/$NEW.py#g" "$f" > "$UNIT_DIR/$NEW.service.d/$(basename "$f")"
        chmod 644 "$UNIT_DIR/$NEW.service.d/$(basename "$f")"
    done
fi

echo "[rename] 2/5  sudoers grant for $OWNER: restart $NEW.service"
printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl restart %s.service\n' "$OWNER" "$NEW" > "/etc/sudoers.d/$NEW"
chmod 440 "/etc/sudoers.d/$NEW"
visudo -cf "/etc/sudoers.d/$NEW" >/dev/null

echo "[rename] 3/5  swap the running service"
systemctl daemon-reload
systemctl disable --now "$OLD.service"
systemctl enable --now "$NEW.service"
sleep 3
systemctl is-active --quiet "$NEW.service" || { echo "[rename] $NEW.service is not active; the old unit is disabled, check: journalctl -u $NEW -n 30" >&2; exit 1; }

echo "[rename] 4/5  remove the old unit and grant"
rm -f "$UNIT_DIR/$OLD.service"
rm -rf "$UNIT_DIR/$OLD.service.d"
rm -f "/etc/sudoers.d/$OLD"
systemctl daemon-reload
systemctl reset-failed "$OLD.service" 2>/dev/null || true

echo "[rename] 5/5  done: $(systemctl show -p MainPID --value "$NEW.service") runs $(systemctl show -p ExecStart --value "$NEW.service" | sed 's/.*argv\[\]=//; s/ ;.*//')"
