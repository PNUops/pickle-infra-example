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
  (fresh-install path, validated 2026-07-17) instead of restoring a dump. A replay
  gives you the schema and nothing about this host — see
  [clean-slate bootstrap](#clean-slate-bootstrap) for the rows you must then add.

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
#   admin settings UI/API. A reset is the sharper version of the same trap:
#   there the row comes back from apply-settings.sh, which writes it off.
# Drift: restored `vms` rows may disagree with live Proxmox guests — the 10min
# reconciler surfaces findings (never destroys); resolve via the drift report.
```

## Gotchas

- **Jar/DB pairing**: restoring a pre-V20 dump under a post-V20 jar fails at
  Flyway validate?? No — the dump CONTAINS flyway_schema_history; Flyway will
  apply only missing migrations. A dump that predates V20 carries the old enum
  values, but that era is long past (live Flyway head is V65 as of 2026-08-07 —
  the current head is always the last file in `api/src/main/resources/db/migration`) and
  the 14-day retention holds no such dump — it is a manual-import concern only. If
  one is ever imported, the paired-era jar rule applies: a jar must not run against
  an older-schema DB, so pair an old dump with a jar of its own era.
- audit_logs append-only REVOKEs are part of the dump (ACLs included in plain
  pg_dump as owner GRANT/REVOKE statements run as postgres) — verify with a
  denied `UPDATE audit_logs` as role pickle after restore.
- JobRunr tables are inside the dump too; stale queued jobs referencing VMs
  that no longer exist on Proxmox will park as NEEDS_ADMIN (never destroy).

## Clean-slate bootstrap

A restore brings the host's inventory back with the dump. A **replay** does not: the
migrations create the schema and deliberately seed no data at all. Nothing —
no node, IP pool, relay or platform certificate, no runtime setting, no OS
catalog row, no legal document. Those are properties of this machine, this
network and this organisation rather than of the schema, and a literal carried
in a migration is wrong — not merely unhelpful — anywhere else. Worse, a seeded
value is wrong *silently*: the operator edits it, the database keeps the edit,
and the migration goes on claiming otherwise until a replay quietly restores the
stale figure. Five scripts write it all instead:

| Script | Writes | Re-run behaviour |
|---|---|---|
| `apply-platform-inventory.sh` | node with measured capacity, IP pool, relay, wildcard certificate row | re-measures and corrects |
| `apply-settings.sh` | the runtime settings rows | inserts missing keys only; never overwrites a value |
| `apply-terms.sh` | terms of service and privacy policy | publishes new versions; refuses to rewrite a published one |
| `apply-os-catalog.sh` | the OS catalog rows | upserts; never changes a row's status |
| `apply-relay-token.sh` | the relay's sync token, on both sides at once | re-issues, which invalidates the previous token immediately |

So after a drop/create + Flyway replay (or a first build of a new environment), the
database is schema-complete and the platform cannot place a VM, cannot be
configured, and will not let anybody register. Work through the order below.

### Order

| # | Step | What breaks if it runs out of order |
|---|---|---|
| 1 | Host bridges, NAT and firewall are up, and the host answers on the guest-network gateway address | `apply-platform-inventory.sh` refuses: the gateway a guest is told to route through must be an address this host holds on the guest bridge. Bypassed, guests get an unroutable gateway and the fault looks like a template problem |
| 2 | App container built (`create-app-lxc.sh`): PostgreSQL, the database, and the container's hosts entry mapping the Proxmox node name to the infrastructure-bridge address | The inventory script refuses: the api pins the Proxmox API certificate and cannot skip hostname verification, so `api_host` must name a SAN of that certificate **and** resolve inside the container. A bridge address is not a SAN — that exact value was once seeded and only failed at the first provisioning run |
| 3 | Start the api once so Flyway applies V1→latest | The inventory script refuses by name: "database … has no nodes table" |
| 3b | **On any environment running the `dev` profile, clear `settings` and `terms_versions` immediately after that first start**, before step 7 or 8 runs | Step 3 does not only create the schema. The api's development seeder runs at the same start and fills both tables — seven reserved names instead of the curated list, an empty contact address, a placeholder legal text — and it fills them *because they are empty*. Steps 7 and 8 then find rows already there and, by design, do not take them over: `apply-settings.sh` reports how many keys it left alone, and `apply-terms.sh` refuses outright because the published wording differs from the file. The damage is not cosmetic. The short reserved list leaves several hundred names claimable, slugs are never recycled, and a user who takes one has taken it for good; the placeholder terms collect real consents that then point at a document nobody wrote. Two details the 2026-08-07 rebuild found by hitting them: the seed accounts have **already consented** to the placeholder terms, so the delete has to start with `user_consents` or a foreign key rejects the whole statement (and with both deletes in one transaction, settings survives too and the failure looks like it only concerned the terms); and the seeder writes an **OS catalog row** as well, at a version the catalog script does not use, so it survives as a duplicate and the request form offers the same OS twice. The whole clearing step is therefore `delete from user_consents; delete from terms_versions; delete from settings; delete from os_images;`. The remaining inventory rows do not need it — `apply-platform-inventory.sh` updates node, pool, relay and certificate in place |
| 4 | Reverse-proxy container built and the wildcard Origin CA pair for **each** platform root domain installed at `/etc/nginx/pickle-certs/<root, dots as dashes>.{crt,key}` | The inventory script refuses: it reads `not_after` off the installed certificate and will not assert an expiry nobody checked. Skipping the pair and inserting a row by hand is worse — the database reports an ACTIVE certificate while the proxy refuses every apply for that root, by name |
| 5 | Relay host provisioned and its public address decided | `PICKLE_RELAY_PUBLIC_HOST` cannot be filled honestly, and it is required. A blank `public_host` hands users a forwarded port with no address to connect to; no API writes that column |
| 6 | **`bash scripts/apply-platform-inventory.sh`** — node with measured capacity, IP pool, relay, wildcard certificate row | — |
| 7 | **`bash scripts/apply-settings.sh`** — the runtime settings rows | Scalar reads fall back to defaults compiled into the api, but the LIST keys do not — a missing list reads as empty. With no `allowed_root_domains` row every VM request naming a platform root is rejected as "not an allowed root domain" and an AUTO subdomain resolves no root at all; with no `reserved_subdomains`/`profanity_subdomains` rows those checks pass everything, and a claimed name is claimed for good (slugs are never recycled); the check that stops a custom domain squatting a platform zone matches nothing. So publishing is broken outright, not invisibly degraded. The settings screen lists exactly the rows that exist, and editing a key with no row answers 404. Pass the same `PICKLE_ROOT_DOMAIN` as step 6, or the request form offers a root domain no certificate covers; a second platform root cannot come from this script — add it in the admin console afterwards |
| 8 | **`bash scripts/apply-terms.sh`** — the legal documents | Registration requires consenting to the current version, so with no rows nobody can sign up. This is the intended failure, not a bug to work around: a service whose terms are unpublished should not be collecting accounts |
| 9 | `apply-os-catalog.sh` — the OS catalog rows | Its upsert resolves the node by name, so with no node row it writes nothing and says so. New rows land DISABLED |
| 10 | **`bash scripts/apply-relay-token.sh`** — issues the token and installs it on the relay | Until then step 6 reports `token_issued f` and every relay sync fails closed. The script does both halves in one run because they are one act: issuing invalidates the old token immediately, so any gap between issuing and installing is a window with the relay locked out. It refuses when the relay has no environment file yet, since the agent's other variables have no in-code defaults and it will not invent them |
| 11 | Enable an OS in the admin console, **and turn the feature switches back on** | An empty catalog leaves the request form with nothing selectable. The switches are the trap: `apply-settings.sh` writes `ssh_gateway_enabled`, `web_terminal_enabled` and `port_forwarding_enabled` **off**, because a fresh deployment has not yet proven any of them works. A restore of a system where they were on therefore comes back with SSH and the web terminal dead, and nothing reports it — the platform behaves exactly as if the operator had thrown the kill switches |
| 12 | Smoke tests | They provision real guests and need every row above |

Re-run step 6 whenever the host's capacity changes (RAM or CPU) or a wildcard
certificate is re-issued: it re-measures and corrects the row rather than adding a
second one. Nothing that belongs to the operator is touched — a node parked in
MAINTENANCE stays parked, and the relay's token, `enabled` flag and generation
counters are left alone. It refuses outright if a CIDR change would orphan a live
allocation or a narrowed port band would strand a live mapping.

The registered memory figure is a hard admission filter for placement and counts
guest intent only, so it accounts for neither this host's own footprint nor the
infrastructure containers sharing the same RAM. `PICKLE_NODE_MEMORY_RESERVE_MB`
withholds capacity for them; it defaults to 0, i.e. register exactly what was
measured.

### Environment — `apply-platform-inventory.sh`

Only the first has no default and must be set. The rest default to this
environment's values; every one of them is configuration, so overriding them is how
this script serves a second host.

| Variable | Example | Notes |
|---|---|---|
| `PICKLE_RELAY_PUBLIC_HOST` | `ssh.example.dev` | **Required.** Bare host, no scheme or port. Obvious placeholders are refused |
| `PICKLE_APP_CTID` | `101` | Container running PostgreSQL and the api |
| `PICKLE_PROXY_CTID` | `100` | Container holding the wildcard certificate material |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_NODE` | `pve1` | Checked against the Proxmox API; a wrong name fails before anything is written |
| `PICKLE_NODE_API_HOST` | `https://pve1:8006` | Defaults to `https://<node>:8006`. Must name a SAN of the API certificate |
| `PICKLE_PVE_CERT` | `/etc/pve/local/pve-ssl.pem` | The certificate whose SANs are checked |
| `PICKLE_NODE_BRIDGE` | `vmbr2` | Must exist on the host |
| `PICKLE_NODE_STORAGE` | `local-lvm` | Must be present and active |
| `PICKLE_NODE_MEMORY_RESERVE_MB` | `0` | Withheld from the measured total |
| `PICKLE_POOL_NAME` | `guest-private` | |
| `PICKLE_POOL_CIDR` | `172.29.0.0/16` | |
| `PICKLE_POOL_GATEWAY` | `172.29.0.1` | Must be inside the CIDR and held by the host on the bridge |
| `PICKLE_POOL_DNS` | `["8.8.8.8"]` | JSON array |
| `PICKLE_POOL_RESERVED` | `[{"from": "172.29.0.0", "to": "172.29.0.255"}]` | JSON array of inclusive ranges, each inside the CIDR |
| `PICKLE_RELAY_NAME` | `lightsail-1` | |
| `PICKLE_RELAY_SOURCE_IP` | `10.100.100.1` | The relay's tunnel-side address — the only peer accepted for its sync calls |
| `PICKLE_RELAY_PORT_BAND` | `10000-19999` | Inside 1024-65535 |
| `PICKLE_ROOT_DOMAIN` | `pusan.dev` | Certificate scope becomes `*.<root>` |
| `PICKLE_WILDCARD_CERT` | `/etc/nginx/pickle-certs/pusan-dev.crt` | Defaults from the root domain. Must cover `*.<root>` |

```bash
PICKLE_RELAY_PUBLIC_HOST=ssh.example.dev \
  bash /root/pickle/infra/scripts/apply-platform-inventory.sh
```

The run ends by printing the node against the numbers just measured, the pool with
its usage, the relay with its public host and every platform wildcard row, then a
pass/fail list. Pre-change rows are dumped to
`/root/pickle/backup/platform-inventory-<timestamp>/inventory-before.sql`
(data-only inserts) before anything is written.

### Environment — `apply-settings.sh`

| Variable | Example | Notes |
|---|---|---|
| `PICKLE_CONTACT_EMAIL` | `ops@example.org` | **Required.** Shown in the console footer and on the maintenance and error screens, and named as the contact point by both legal documents. Leave it unset and the script asks at the terminal; it refuses only when there is no terminal to ask at, so an unattended run cannot invent one. Answer `none` to publish it empty on purpose |
| `PICKLE_ROOT_DOMAIN` | `pusan.dev` | Becomes the single entry of `allowed_root_domains`. The same variable the inventory script reads, so the two cannot disagree about which domain this deployment publishes under. On a deployment with more than one platform root (step 4 installs a wildcard pair per root), add the remaining roots in the admin console after this step — like every key, an existing row is never rewritten by a re-run |
| `PICKLE_APP_CTID` | `101` | |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_DATA_DIR` | `<repo>/data` | Holds the curated word lists |

```bash
PICKLE_CONTACT_EMAIL=ops@example.org \
  bash /root/pickle/infra/scripts/apply-settings.sh
```

Two settings are lists the operator curates rather than values a default can
supply, so they live in `data/` as one entry per line: `reserved-subdomains.txt`
(names nobody may claim, because the organisation already publishes them) and
`profanity-subdomains.txt`. Adding a name is a one-line diff a reviewer can read.
They are read fresh on every run, but — like every other key — only for a row
that does not exist yet: once the rows are there, extending the lists is done in
the admin console, and the files are the bootstrap copy. Keep them in step by
hand, or a rebuild silently reverts months of curation.

### Environment — `apply-terms.sh`

| Variable | Example | Notes |
|---|---|---|
| `PICKLE_APP_CTID` | `101` | |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_DATA_DIR` | `<repo>/data` | Documents are read from `<data>/terms/*.md` |

```bash
bash /root/pickle/infra/scripts/apply-terms.sh
```

Each document is one file: a four-line header (`doc_type`, `version`, `title`,
`effective_at`), a `---` line, then the document in Markdown. `effective_at` is a
real timestamp and the application treats the current version as the highest one
whose date has passed — so a revision can be published ahead of time and take
effect on its own.

Revising a document means **adding a file with the next version number**, not
editing the published one. The script enforces that: it compares the file against
what is in the database and refuses if a published version's text or title has
changed, because users have already consented to the published wording and a
silent rewrite leaves their consent record pointing at a document that no longer
exists.

### Environment — `apply-os-catalog.sh`

| Variable | Example | Notes |
|---|---|---|
| `PICKLE_APP_CTID` | `101` | |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_NODE` | `pve1` | The catalog rows are resolved against this node by name; with no node row the script writes nothing and says so |

The templates themselves are a list inside the script, not an environment
variable: it checks each one exists on this host by name **and** VMID before it
writes anything, because a row for a template that is not here would be
selectable in the request form and fail at clone time.

### Environment — `apply-relay-token.sh`

| Variable | Example | Notes |
|---|---|---|
| `PICKLE_ADMIN_EMAIL` | | **Asked at the terminal if unset.** The token is issued through the admin API, so the run needs a SYS_ADMIN to act as |
| `PICKLE_ADMIN_PASSWORD` | | Asked at the terminal if unset, never echoed. Issuing a token is behind re-authentication, so the password is proven twice in the one run |
| `PICKLE_RELAY_NAME` | `lightsail-1` | Which relay row to issue for |
| `PICKLE_APP_CTID` | `101` | |
| `PICKLE_DB` | `pickle_dev` | |
| `PICKLE_TUNNEL_CTID` | `102` | The container that can reach the relay's tunnel address. The relay's admin port is not reachable from the Proxmox host, so the TCP connection is proxied through this container while ssh itself runs on the host — the private key never enters it |
| `PICKLE_RELAY_SSH_KEY` | `secrets/lightsail-ssh.pem` | |
| `PICKLE_RELAY_SSH_USER` | `admin` | |
| `PICKLE_RELAY_SSH_PORT` | `2222` | |

Note the naming: `deploy-relay.sh` reads the same three SSH values under
`RELAY_SSH_KEY` / `_PORT` and a `RELAY_HOST`. The two scripts were written apart
and neither name is wrong, but a new environment has to set both sets.
