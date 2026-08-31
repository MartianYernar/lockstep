#!/usr/bin/env bash
# Nightly backup. Run from cron:
#   0 3 * * * /opt/lockstep/deploy/backup.sh >> /var/log/lockstep-backup.log 2>&1
#
# Keeps the ledger recoverable. If the database is ever lost or wiped, these
# snapshots are what let the guardian see the history that existed before.
set -euo pipefail
# bootstrap.sh puts the data dir at $REPO_DIR/deploy/data (compose mounts ./data
# from inside deploy/). An earlier default pointed one level up, so the nightly
# cron failed every night into a log nobody reads -- and these snapshots are the
# only thing that makes the ledger recoverable after a wipe. Fail loudly instead.
DB="${LOCKSTEP_DB:-/opt/lockstep/deploy/data/lockstep.db}"
DEST="${LOCKSTEP_BACKUP_DIR:-/opt/lockstep/backups}"
KEEP=30

[[ -f "$DB" ]] || { echo "$(date '+%F %T') NO DATABASE AT $DB -- backup did nothing" >&2; exit 1; }
mkdir -p "$DEST"
STAMP=$(date +%Y%m%d-%H%M%S)
# .backup is safe on a live WAL database; cp is not.
sqlite3 "$DB" ".backup '$DEST/lockstep-$STAMP.db'"
gzip -f "$DEST/lockstep-$STAMP.db"
ls -1t "$DEST"/lockstep-*.db.gz | tail -n +$((KEEP + 1)) | xargs -r rm --
echo "$(date '+%F %T') backed up to $DEST/lockstep-$STAMP.db.gz"
