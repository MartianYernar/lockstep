#!/usr/bin/env bash
# Nightly backup. Run from cron:
#   0 3 * * * /opt/lockstep/deploy/backup.sh >> /var/log/lockstep-backup.log 2>&1
#
# Keeps the ledger recoverable. If the database is ever lost or wiped, these
# snapshots are what let the guardian see the history that existed before.
set -euo pipefail
DB="${LOCKSTEP_DB:-/opt/lockstep/data/lockstep.db}"
DEST="${LOCKSTEP_BACKUP_DIR:-/opt/lockstep/backups}"
KEEP=30

mkdir -p "$DEST"
STAMP=$(date +%Y%m%d-%H%M%S)
# .backup is safe on a live WAL database; cp is not.
sqlite3 "$DB" ".backup '$DEST/lockstep-$STAMP.db'"
gzip -f "$DEST/lockstep-$STAMP.db"
ls -1t "$DEST"/lockstep-*.db.gz | tail -n +$((KEEP + 1)) | xargs -r rm --
echo "$(date '+%F %T') backed up to $DEST/lockstep-$STAMP.db.gz"
