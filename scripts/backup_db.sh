#!/bin/sh
# Nightly consistent backup of the Fikarta databases and the PAN encryption key.
# Uses SQLite's online backup API, so it is safe while the app is running.
# Output: /var/backups/fikarta/fikarta-YYYYmmdd-HHMM.tar.gz (root only, 0600), last 14 kept.
# Off-server copies: set BACKUP_S3_URI (e.g. s3://bucket/fikarta) and install the AWS CLI with
# credentials; the archive is then also uploaded.
set -eu
APP=/var/www/fikarta/fikarta-backend
DEST=/var/backups/fikarta
KEEP=14
STAMP=$(date +%Y%m%d-%H%M)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
umask 077
mkdir -p "$DEST"

for db in fikarta.db fikarta_mobile.db official_kb.db; do
  if [ -f "$APP/db/$db" ]; then
    python3 - "$APP/db/$db" "$WORK/$db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close(); src.close()
if ok != "ok":
    sys.exit(f"integrity check failed for {sys.argv[1]}: {ok}")
PY
  fi
done
# Without this key the encrypted PANs in the backup cannot be read.
[ -f "$APP/db/.mobile_secret" ] && cp "$APP/db/.mobile_secret" "$WORK/mobile_secret"

tar -czf "$DEST/fikarta-$STAMP.tar.gz" -C "$WORK" .
chmod 600 "$DEST/fikarta-$STAMP.tar.gz"

if [ -n "${BACKUP_S3_URI:-}" ] && command -v aws >/dev/null 2>&1; then
  aws s3 cp --only-show-errors "$DEST/fikarta-$STAMP.tar.gz" "$BACKUP_S3_URI/" || logger -t fikarta-backup "S3 upload failed"
fi

ls -1t "$DEST"/fikarta-*.tar.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f
logger -t fikarta-backup "backup ok: fikarta-$STAMP.tar.gz"
