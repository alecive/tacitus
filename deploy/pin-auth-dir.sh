#!/usr/bin/env bash
# pin-auth-dir.sh: keep the resident from renaming the token directory away.
#
# The mail tokens live in ~ea-bot/.ea/auth, owner-owned, which ea-bot can
# neither read nor change. But ~ea-bot/.ea is ea-bot's own directory, and the
# owner of a directory may rename any entry in it, so ea-bot could move auth/
# (or .ea itself) aside and put its own in place. The owner checks refuse a
# directory that is not the owner's, so that ends in no mail, not in a
# planted token; this script removes even that.
#
# Linux refuses to rename or remove a mount point (EBUSY), from any path to
# it, and /home is root's. So ~ea-bot/.ea and ~ea-bot/.ea/auth are each
# bind-mounted onto themselves. Nothing moves: the tokens stay where they
# are, every path stays as it was, and no code, plugin or unit changes. Both
# mounts are in /etc/fstab, tagged.
#
#     sudo bash deploy/pin-auth-dir.sh            # enable (idempotent)
#     sudo bash deploy/pin-auth-dir.sh --status
#     sudo bash deploy/pin-auth-dir.sh --rollback # unmount, drop the fstab lines
#     bash deploy/pin-auth-dir.sh --print         # the fstab lines, no changes
#
# Linux only. On macOS the residual stays: a renamed auth/ stops mail and the
# daily audit reports it.
set -uo pipefail

REAL_USER="${SUDO_USER:-$(id -un)}"
EA_BOT_USER=ea-bot
EA_BOT_HOME="$(eval echo ~$EA_BOT_USER 2>/dev/null || echo /home/ea-bot)"
EA_DIR="${EA_PIN_EA_DIR:-$EA_BOT_HOME/.ea}"
AUTH="$EA_DIR/auth"
FSTAB="${EA_PIN_FSTAB:-/etc/fstab}"
FSTAB_TAG="# ea-pin-auth"

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
fail(){ red "FAIL: $*"; exit 1; }
need_root(){ [ "$(id -u)" = 0 ] || fail "run as root:  sudo bash $0 ${1:-}"; }

fstab_lines(){
  # .ea first: auth/ is mounted inside it. rbind, so a mount already under
  # .ea stays visible.
  printf '%s\n%s %s none rbind,nofail 0 0\n' "$FSTAB_TAG" "$EA_DIR" "$EA_DIR"
  printf '%s\n%s %s none bind,nofail,x-systemd.requires-mounts-for=%s 0 0\n' \
    "$FSTAB_TAG" "$AUTH" "$AUTH" "$EA_DIR"
}

drop_fstab(){
  python3 - "$FSTAB" "$FSTAB_TAG" <<'EOF'
import sys
path, tag = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines()
out, skip = [], False
for ln in lines:
    if skip:
        skip = False
        continue
    if ln.strip() == tag:
        skip = True
        continue
    out.append(ln)
open(path, "w").write("\n".join(out) + "\n")
EOF
}

pinned(){ mountpoint -q "$EA_DIR" && mountpoint -q "$AUTH"; }

probe(){  # as ea-bot, a rename of auth/ and of .ea must fail; put back if not
  local bad=0
  if sudo -n -u "$EA_BOT_USER" mv -T "$AUTH" "$AUTH.pin-probe" 2>/dev/null; then
    sudo -n -u "$EA_BOT_USER" mv -T "$AUTH.pin-probe" "$AUTH"; red "ea-bot could rename $AUTH"; bad=1
  fi
  if sudo -n -u "$EA_BOT_USER" mv -T "$EA_DIR" "$EA_DIR.pin-probe" 2>/dev/null; then
    sudo -n -u "$EA_BOT_USER" mv -T "$EA_DIR.pin-probe" "$EA_DIR"; red "ea-bot could rename $EA_DIR"; bad=1
  fi
  return $bad
}

status(){
  for d in "$EA_DIR" "$AUTH"; do
    if mountpoint -q "$d"; then echo "  $d  mount point"; else echo "  $d  not a mount point"; fi
  done
  echo "  fstab entries: $(grep -c "^$FSTAB_TAG\$" "$FSTAB" 2>/dev/null || echo 0)"
  if pinned; then grn "pinned"; else red "not pinned"; return 1; fi
}

enable(){
  need_root
  [ "$(uname -s)" = Linux ] || fail "Linux only (bind mounts)"
  [ -L "$EA_DIR" ] && fail "$EA_DIR is a link"
  [ -d "$AUTH" ] && [ ! -L "$AUTH" ] || fail "$AUTH is not a real directory"
  [ "$(stat -c %u "$AUTH")" = "$(id -u "$REAL_USER")" ] \
    || fail "$AUTH is not $REAL_USER's: recover it first (scripts/boundary-audit.py names the steps), never adopt it"
  mountpoint -q "$EA_DIR" || mount --rbind "$EA_DIR" "$EA_DIR" || fail "could not bind $EA_DIR onto itself"
  mountpoint -q "$AUTH" || mount --bind "$AUTH" "$AUTH" || fail "could not bind $AUTH onto itself"
  [ "$(stat -c %u "$AUTH")" = "$(id -u "$REAL_USER")" ] || fail "$AUTH changed owner during the run"
  grep -qsF "$FSTAB_TAG" "$FSTAB" || fstab_lines >> "$FSTAB"
  systemctl daemon-reload
  pinned || fail "the mounts are not up: $(findmnt --target "$AUTH")"
  probe || fail "a rename by ea-bot was not refused"
  grn "pinned: ea-bot cannot rename $AUTH or $EA_DIR"
}

rollback(){
  need_root --rollback
  if mountpoint -q "$AUTH"; then umount "$AUTH" || fail "could not unmount $AUTH"; fi
  if mountpoint -q "$EA_DIR"; then umount "$EA_DIR" || umount -l "$EA_DIR" || fail "could not unmount $EA_DIR"; fi
  drop_fstab
  systemctl daemon-reload
  grn "rolled back: mounts and fstab lines gone"
}

case "${1:-}" in
  ""|--enable) enable ;;
  --status)    status ;;
  --rollback)  rollback ;;
  --print)     fstab_lines ;;
  *) fail "usage: $0 [--status|--rollback|--print]" ;;
esac
