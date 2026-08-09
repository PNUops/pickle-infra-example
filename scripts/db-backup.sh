#!/usr/bin/env bash
# Nightly pickle_dev backup: pg_dump inside the pickle-app LXC,
# copy to the host, keep 14 days on both sides. Run from host cron.
set -euo pipefail

# cron's PATH lacks /usr/sbin, where Proxmox puts pct — without this the job
# dies with "pct: command not found" (silently broke 2026-07-08~13).
export PATH="/usr/sbin:/usr/bin:/bin:$PATH"

CTID="${CTID:-101}"
# shellcheck source=scripts/lib/ct.sh
. "$(dirname "$0")/lib/ct.sh"
require_ct "$CTID" pickle-app
DB="${DB:-pickle_dev}"
LXC_DIR=/var/backups/pickle
HOST_DIR=/root/pickle/backup/db
RETENTION_DAYS=14
TS=$(date +%Y%m%d-%H%M%S)
FILE="$DB-$TS.sql.gz"

# pipefail is load-bearing: without it a failed pg_dump still leaves gzip
# exiting 0 with a valid EMPTY archive that passes `gzip -t`, while the
# retention sweep keeps deleting the last good dumps (same silent-failure
# class as the 2026-07 cron PATH incident).
pct exec "$CTID" -- bash -c "
set -eo pipefail
mkdir -p $LXC_DIR
runuser -u postgres -- pg_dump $DB | gzip > $LXC_DIR/$FILE
find $LXC_DIR -name '$DB-*.sql.gz' -mtime +$RETENTION_DAYS -delete
"

mkdir -p "$HOST_DIR"
pct pull "$CTID" "$LXC_DIR/$FILE" "$HOST_DIR/$FILE"
find "$HOST_DIR" -name "$DB-*.sql.gz" -mtime +"$RETENTION_DAYS" -delete

# sanity: non-empty gzip whose payload is an actual pg_dump (an empty-payload
# gzip is ~20 bytes and would otherwise pass gzip -t).
[ -s "$HOST_DIR/$FILE" ] && gzip -t "$HOST_DIR/$FILE"
# (command substitution + `|| true` so head's early exit / zcat's SIGPIPE
# can't trip pipefail — the capture is what we grep, not the pipeline rc)
dump_head=$(zcat "$HOST_DIR/$FILE" | head -5 || true)
printf '%s\n' "$dump_head" | grep -q "PostgreSQL database dump" \
  || { echo "backup FAILED: $FILE is not a pg_dump" >&2; exit 1; }
echo "backup OK: $HOST_DIR/$FILE ($(du -h "$HOST_DIR/$FILE" | cut -f1))"
