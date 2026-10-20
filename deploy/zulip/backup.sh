#!/usr/bin/env bash
# backup.sh: nightly Zulip backup into the owner's home, where the daily
# mirror already copies it. Uses Zulip's own backup tool inside the
# container (a consistent database dump plus uploads and settings), keeps
# seven days. Run by the owner's crontab; the owner is in the docker group.
set -euo pipefail
OUT="${ZULIP_BACKUP_DIR:-$HOME/zulip-backups}"
CONTAINER=ea-zulip-zulip-1
mkdir -p "$OUT"; chmod 700 "$OUT"
STAMP=$(date +%F-%H%M)
docker exec -u zulip "$CONTAINER" /home/zulip/deployments/current/manage.py backup --output "/tmp/zulip-backup-$STAMP.tar.gz" >/dev/null
docker cp "$CONTAINER:/tmp/zulip-backup-$STAMP.tar.gz" "$OUT/zulip-backup-$STAMP.tar.gz"
docker exec "$CONTAINER" rm -f "/tmp/zulip-backup-$STAMP.tar.gz"
chmod 600 "$OUT/zulip-backup-$STAMP.tar.gz"
find "$OUT" -name 'zulip-backup-*.tar.gz' -mtime +7 -delete
echo "$(date -Is) backup written: $OUT/zulip-backup-$STAMP.tar.gz ($(du -h "$OUT/zulip-backup-$STAMP.tar.gz" | cut -f1))"
