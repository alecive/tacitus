#!/usr/bin/env bash
#
# ssh-deny-resident.sh: refuse SSH logins for the resident account (ea-bot).
# Run as root:  sudo bash deploy/ssh-deny-resident.sh [--status | --rollback]
#
# The resident owns its home, so it controls its own authorized_keys; sshd
# must therefore never accept a login for it, whatever keys that file holds.
# This writes one drop-in, /etc/ssh/sshd_config.d/01-ea-deny-resident.conf,
# with `DenyUsers <resident>`, loaded before any other drop-in. sshd -t
# validates the result before sshd is reloaded; a failed check removes the
# drop-in again. Other accounts, and SSH sessions already open, are not
# affected. Idempotent; --rollback removes the drop-in.
#
set -uo pipefail

RESIDENT="${EA_RESIDENT_USER:-ea-bot}"
SSHD_CONFIG=/etc/ssh/sshd_config
DROPIN_DIR=/etc/ssh/sshd_config.d
DROPIN="$DROPIN_DIR/01-ea-deny-resident.conf"

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }
fail(){ red "FAIL: $*"; exit 1; }
need_root(){ [ "$(id -u)" = 0 ] || fail "run as root:  sudo bash $0 ${1:-}"; }

dropin_text(){
  cat <<EOF
# deploy/ssh-deny-resident.sh: the resident account controls its own
# authorized_keys, so sshd never accepts a login for it.
DenyUsers $RESIDENT
EOF
}

includes_dropins(){ grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' "$SSHD_CONFIG"; }

reload_sshd(){
  # Socket-activated sshd reads its configuration per connection; a running
  # listener is reloaded, which keeps open sessions.
  local u
  for u in ssh.service sshd.service; do
    if systemctl is-active --quiet "$u" 2>/dev/null; then
      systemctl reload "$u" || fail "systemctl reload $u"
      echo "  reloaded $u"
      return 0
    fi
  done
  echo "  no running sshd listener to reload (socket activation reads the new file per connection)"
}

effective(){  # the DenyUsers sshd applies to the resident, or nothing
  sshd -T -C "user=$RESIDENT,host=localhost,addr=127.0.0.1" 2>/dev/null | grep -i '^denyusers' || true
}

stage(){
  need_root
  getent passwd "$RESIDENT" >/dev/null || fail "no account '$RESIDENT'"
  command -v sshd >/dev/null || fail "sshd is not installed"
  includes_dropins || fail "$SSHD_CONFIG does not include $DROPIN_DIR/*.conf; add DenyUsers $RESIDENT there by hand"
  if [ -f "$DROPIN" ] && [ "$(cat "$DROPIN")" = "$(dropin_text)" ]; then
    echo "  $DROPIN is in place"
  else
    install -d -m 755 "$DROPIN_DIR" || fail "mkdir $DROPIN_DIR"
    dropin_text > "$DROPIN.tmp" && chmod 644 "$DROPIN.tmp" && mv -f "$DROPIN.tmp" "$DROPIN" \
      || fail "write $DROPIN"
    grn "  wrote $DROPIN"
  fi
  if ! sshd -t; then
    rm -f "$DROPIN"
    fail "sshd -t rejected the configuration; $DROPIN removed, nothing reloaded"
  fi
  reload_sshd
  local e; e="$(effective)"
  case " $e " in
    *" $RESIDENT "*|*" $RESIDENT") grn "done: sshd applies '$e' to $RESIDENT" ;;
    *) ylw "written, but sshd -T reports '${e:-no DenyUsers}' for $RESIDENT; check for an earlier DenyUsers or AllowUsers" ;;
  esac
}

status(){
  need_root --status
  if [ -f "$DROPIN" ]; then echo "drop-in: $DROPIN"; sed 's/^/  /' "$DROPIN"; else echo "drop-in: none"; fi
  echo "effective for $RESIDENT: $(effective || true)"
}

rollback(){
  need_root --rollback
  if [ -f "$DROPIN" ]; then
    rm -f "$DROPIN" || fail "rm $DROPIN"
    sshd -t || fail "sshd -t rejects the configuration without $DROPIN; fix it before reloading"
    reload_sshd
    grn "removed $DROPIN"
  else
    echo "no $DROPIN; nothing to roll back"
  fi
}

case "${1:-}" in
  "")          stage ;;
  --status)    status ;;
  --rollback)  rollback ;;
  *) echo "usage: sudo bash $0 [--status | --rollback]"; exit 2 ;;
esac
