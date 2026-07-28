# DB Restore Runbook — pickle_dev (PostgreSQL in LXC 101)

> Restores a `db-backup.sh` dump (`pickle_dev-YYYYMMDD-HHMMSS.sql.gz`, plain-format
> pg_dump). Backups live on both sides: host `/root/pickle/backup/db/` and
> LXC 101 `/var/backups/pickle/` (14-day retention). This procedure was exercised
> in essence during the 2026-07-17 full DB reset (drop → recreate → replay).
> Production readiness additionally requires an OFF-HOST backup copy + a timed
> restore drill — both still open.

## When to use

- Bad deploy/migration that cannot be fixed forward (note: V20+ era jars and DBs
  must stay paired — a jar must not run against an older-schema DB).
- Data corruption / accidental destructive operation.
- NOT for a clean slate: for that, drop/create + let Flyway replay V1→latest
  (fresh-install path, validated 2026-07-17) instead of restoring a dump.

## Procedure (≈2–5 min, full downtime of the api)

```bash
# 0) pick the dump (host side shown; LXC copies are identical)
ls -lt /root/pickle/backup/db/ | head
DUMP=/root/pickle/backup/db/pickle_dev-YYYYMMDD-HHMMSS.sql.gz

# 1) verify the dump BEFORE touching anything (content check, not just gzip -t)
zcat "$DUMP" | head -5 | grep -q "PostgreSQL database dump" && echo dump-ok

# 2) stop the api (stops JobRunr workers/pollers too — single-process design)
pct exec 101 -- systemctl stop pickle-api

# 3) take a safety dump of the CURRENT (broken) state first — always
bash /root/pickle/infra/scripts/db-backup.sh   # or: suffix the file "-prerestore"

# 4) drop & recreate (owner must stay `pickle`)
pct exec 101 -- runuser -u postgres -- psql -qc \
  "select pg_terminate_backend(pid) from pg_stat_activity where datname='pickle_dev' and pid<>pg_backend_pid()"
pct exec 101 -- runuser -u postgres -- dropdb pickle_dev
pct exec 101 -- runuser -u postgres -- createdb -O pickle pickle_dev

# 5) restore (push the dump into the LXC, feed psql as postgres)
pct push 101 "$DUMP" /tmp/restore.sql.gz
pct exec 101 -- bash -c "set -eo pipefail; zcat /tmp/restore.sql.gz | runuser -u postgres -- psql -q -d pickle_dev"
pct exec 101 -- rm -f /tmp/restore.sql.gz

# 6) resync vmid_seq BEFORE the api starts: the dump restores the sequence to
#    its backup-time value, but guests provisioned after the backup still sit
#    on the cluster at higher vmids. Without this, the next provision draws an
#    occupied number and parks with a vmid-conflict (the pipeline refuses to
#    touch the resident guest). Set it past the highest user-band vmid live on
#    the cluster:
MAXVMID=$(qm list | awk '$1 ~ /^[0-9]+$/ && $1+0 >= 100000 {print $1}' | sort -n | tail -1)
# greatest() also keeps the restored sequence value itself, so a restore can
# never REWIND the sequence below where the dump left it (no-reuse policy)
pct exec 101 -- runuser -u postgres -- psql -d pickle_dev -qtAc \
  "select setval('vmid_seq', greatest((select last_value from vmid_seq), 100000, ${MAXVMID:-100000} + 1))"

# 7) start the api — Flyway validates (and applies any migrations newer than
#    the dump); seeders are idempotent (insert-if-absent)
pct exec 101 -- systemctl start pickle-api
for i in $(seq 1 30); do sleep 2; pct exec 101 -- curl -fsS http://127.0.0.1:8080/actuator/health >/dev/null 2>&1 && { echo health-ok; break; }; done
```

## Post-restore checks

```bash
pct exec 101 -- runuser -u postgres -- psql -d pickle_dev -qtAc \
  "select version, success from flyway_schema_history order by installed_rank desc limit 3"
pct exec 101 -- runuser -u postgres -- psql -d pickle_dev -qtAc \
  "select key, value from settings where key='ssh_gateway_enabled'"
# ^ if the dump predates the operator's kill-switch flip, re-enable it via the
#   admin settings UI/API (V13 seed default is false — same trap as a reset).
# Drift: restored `vms` rows may disagree with live Proxmox guests — the 10min
# reconciler surfaces findings (never destroys); resolve via the drift report.
```

## Gotchas

- **Jar/DB pairing**: restoring a pre-V20 dump under a post-V20 jar fails at
  Flyway validate?? No — the dump CONTAINS flyway_schema_history; Flyway will
  apply only missing migrations. A dump that predates V20 carries the old enum
  values, but that era is long past (live Flyway head is V54 as of 2026-07-28 —
  the current head is always the last file in `api/src/main/resources/db/migration`) and
  the 14-day retention holds no such dump — it is a manual-import concern only. If
  one is ever imported, the paired-era jar rule applies: a jar must not run against
  an older-schema DB, so pair an old dump with a jar of its own era.
- audit_logs append-only REVOKEs are part of the dump (ACLs included in plain
  pg_dump as owner GRANT/REVOKE statements run as postgres) — verify with a
  denied `UPDATE audit_logs` as role pickle after restore.
- JobRunr tables are inside the dump too; stale queued jobs referencing VMs
  that no longer exist on Proxmox will park as NEEDS_ADMIN (never destroy).
