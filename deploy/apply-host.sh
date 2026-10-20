#!/usr/bin/env bash
# apply-host.sh: one command for every root-level step of the install and of
# every update. Run it after setup and again after any update:
#
#     sudo bash /opt/ea-system/deploy/apply-host.sh            # check, fix what is missing, check again
#     sudo bash /opt/ea-system/deploy/apply-host.sh --plan     # say what it would run, change nothing
#
# It runs the daily boundary audit (scripts/boundary-audit.py) as you, takes
# each warning or failure whose fix is one of the root scripts below, runs
# those scripts once each in a safe order, and audits again. A step whose
# check already passes is not run. What it cannot fix itself it prints, with
# the audit's own instructions.
set -uo pipefail

REAL_USER="${SUDO_USER:-}"
CODE="$(cd "$(dirname "$0")/.." && pwd)"
PLAN=0
case "${1:-}" in
  "") ;;
  --plan) PLAN=1 ;;
  *) echo "usage: sudo bash $0 [--plan]" >&2; exit 2 ;;
esac

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
bold(){ printf '\n\033[1m%s\033[0m\n' "$*"; }

[ "$(id -u)" = 0 ] || { red "run as root: sudo bash $0"; exit 1; }
[ -n "$REAL_USER" ] && [ "$REAL_USER" != root ] || { red "run it with sudo from your own account"; exit 1; }
case "$CODE" in
  /home/ea-bot/*) red "run the copy in /opt/ea-system (or your own checkout), not the resident's tree"; exit 1 ;;
esac

# The root scripts, in the order they run, each with what it does in plain
# words. Only these run; any other fix is printed for you to do by hand.
ORDER="harden-permissions enable-mail-mediation pin-auth-dir enable-resident-netpolicy ssh-deny-resident stage-proc-hidepid"
what(){
  case "$1" in
    harden-permissions)        echo "runs your services from /opt with your private settings files, and denies cron to the EA user" ;;
    enable-mail-mediation)     echo "keeps the mail tokens yours: the EA reads and drafts mail only through your mediator" ;;
    pin-auth-dir)              echo "makes the token folder and the folder above it mount points, so the EA user cannot rename them" ;;
    enable-resident-netpolicy) echo "blocks the EA user from this machine's own services and your local network" ;;
    ssh-deny-resident)         echo "stops anyone from logging in over ssh as the EA user" ;;
    stage-proc-hidepid)        echo "hides other users' processes from the EA user (takes effect after a reboot)" ;;
  esac
}

audit_json(){
  sudo -u "$REAL_USER" -H python3 "$CODE/scripts/boundary-audit.py" --dry-run --json 2>/dev/null
}

# Each non-passing check: its status, name, and which root script fixes it
# ("-" when the fix is not one of ours).
findings(){
  python3 -c '
import json, re, sys
order = sys.argv[1].split()
try:
    results = json.load(sys.stdin)["results"]
except Exception:
    sys.exit(3)
for r in results:
    if r.get("status") == "PASS":
        continue
    m = re.search(r"deploy/([a-z-]+)\.sh", r.get("fix") or "")
    name = m.group(1) if m and m.group(1) in order else "-"
    print("\t".join([r["status"], name, r.get("name", ""), (r.get("fix") or "").replace("\n", " ")]))
' "$ORDER"
}

bold "Checking this machine (the daily boundary audit, run as $REAL_USER)"
before="$(audit_json | findings)" || { red "the audit did not run: try  python3 $CODE/scripts/boundary-audit.py --dry-run"; exit 1; }
if [ -z "$before" ]; then
  grn "Every check passes. Nothing to do."
  exit 0
fi

todo=""
for s in $ORDER; do
  if printf '%s\n' "$before" | awk -F'\t' -v s="$s" '$2 == s' | grep -q .; then todo="$todo $s"; fi
done

if [ -n "$todo" ]; then
  bold "Steps to run"
  for s in $todo; do
    echo "  $s: $(what "$s")"
    printf '%s\n' "$before" | awk -F'\t' -v s="$s" '$2 == s {print "      because: " $3}'
  done
fi
manual="$(printf '%s\n' "$before" | awk -F'\t' '$2 == "-"')"
if [ -n "$manual" ]; then
  bold "Not fixed by this command (do these by hand)"
  printf '%s\n' "$manual" | awk -F'\t' '{print "  " $1 ": " $3; if ($4 != "") print "      fix: " $4}'
fi
[ "$PLAN" = 1 ] && { echo; echo "  (--plan: nothing changed)"; exit 0; }
[ -n "$todo" ] || exit 1

failed=""
for s in $todo; do
  bold "Running $s: $(what "$s")"
  if bash "$CODE/deploy/$s.sh"; then grn "  $s: done"; else red "  $s: failed (see above)"; failed="$failed $s"; fi
done

bold "Checking again"
after="$(audit_json | findings)" || after="?"
if [ -z "$after" ]; then
  grn "Every check passes."
else
  printf '%s\n' "$after" | awk -F'\t' '{print "  " $1 ": " $3; if ($4 != "") print "      fix: " $4}'
fi
[ -z "$failed" ] || { red "failed:$failed"; exit 1; }
[ -z "$after" ]
