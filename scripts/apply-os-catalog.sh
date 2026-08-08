#!/usr/bin/env bash
# Puts the OS catalog rows for the templates that exist on this host into the
# application database.
#
# Why this is a script and not a migration: which templates exist is a property
# of the host, not of the schema. A migration would insert these rows into every
# environment whether or not the templates are there, and a rebuild that lands on
# a different VMID would need another migration to say so. The schema side
# (the unique constraint on name and version) lives in the migrations; the rows
# live here, next to the host they describe.
#
# Rows are inserted DISABLED. A catalog row is what makes an OS selectable in the
# request form, so enabling one is a separate, deliberate act after a VM built
# from that template has been provisioned end to end. Use the admin console to
# flip it.
#
# Idempotent: rerunning updates the existing row for a (name, version) pair
# rather than inserting a second one, and never changes a row's status. An OS
# already enabled stays enabled.
set -euo pipefail

CTID="${PICKLE_APP_CTID:-101}"
DB="${PICKLE_DB:-pickle_dev}"
NODE="${PICKLE_NODE:-pve1}"

# name | display | family | version | account | vmid | template | revision
#
# `template` is the name the VM carries on this host, listed rather than derived
# because nothing guarantees the naming convention. It exists so the existence
# check can compare a name and not only a number: VMIDs get reused across
# rebuilds, so a stale or typo'd number that happens to be occupied would
# otherwise pass and put a different OS behind a catalog entry. The limit of the
# check is worth knowing — two templates of the SAME distribution revision carry
# the same name (1000 and 1001 both carried ubuntu-2404-template until 1000 was
# destroyed on 2026-08-07),
# and a mix-up between those two is invisible to any name comparison. Retire the
# superseded VMID rather than relying on this check to tell them apart.
CATALOG=(
  "ubuntu-24.04|Ubuntu 24.04 LTS|ubuntu|24.04|ubuntu|1001|ubuntu-2404-template|2"
  "ubuntu-26.04|Ubuntu 26.04 LTS|ubuntu|26.04|ubuntu|1002|ubuntu-2604-template|1"
  "ubuntu-22.04|Ubuntu 22.04 LTS|ubuntu|22.04|ubuntu|1003|ubuntu-2204-template|1"
  "debian-13|Debian 13|debian|13|debian|1004|debian-13-template|1"
  "debian-12|Debian 12|debian|12|debian|1005|debian-12-template|1"
  "rocky-10|Rocky Linux 10|rocky|10|rocky|1006|rocky-10-template|1"
  "rocky-9|Rocky Linux 9|rocky|9|rocky|1007|rocky-9-template|1"
)

# Statements are fed on STDIN, never as `psql -c "…"`. A -c argument travels
# through the second shell `su -c` spawns, and that parse expands $ and command
# substitution and eats quotes — a distribution codename carrying a backtick
# would run as a command as the postgres user, and psql would still exit 0.
# Nothing re-parses stdin.
psqlq() {
  local out
  if ! out=$(pct exec "$CTID" -- su - postgres -c \
      "psql -q -X -v ON_ERROR_STOP=1 -tA -d $DB -f -" <<<"$1" 2>&1); then
    printf 'query failed: %s\n%s\n' "${1%%$'\n'*}" "$out" >&2
    return 1
  fi
  printf '%s' "$out"
}
psqlshow() {
  pct exec "$CTID" -- su - postgres -c \
    "psql -q -X -v ON_ERROR_STOP=1 -d $DB -f -" <<<"$1"
}

sql_escape() { printf '%s' "$1" | sed "s/'/''/g"; }

echo "== check the templates exist on this host"
missing=0
for row in "${CATALOG[@]}"; do
  IFS='|' read -r name _display _family _version _account vmid template _revision <<<"$row"
  # A row for a template that is not here would be selectable in the request
  # form and fail at clone time, which is the one failure worth refusing up
  # front. The name is compared too, because a VMID alone could be anything.
  if ! cfg=$(qm config "$vmid" 2>/dev/null); then
    echo "  MISSING: $name expects template $vmid, which does not exist" >&2
    missing=1
    continue
  fi
  actual=$(printf '%s\n' "$cfg" | sed -n 's/^name: //p')
  if [ "$actual" != "$template" ]; then
    echo "  MISMATCH: $name expects template $vmid to be named '$template', but $vmid is named '${actual:-<unnamed>}'" >&2
    missing=1
  fi
done
[ "$missing" -eq 0 ] || { echo "refusing to write catalog rows" >&2; exit 1; }

echo "== upsert the catalog rows (new rows land DISABLED)"
for row in "${CATALOG[@]}"; do
  IFS='|' read -r name display family version account vmid _template revision <<<"$row"
  n=$(sql_escape "$name"); d=$(sql_escape "$display")
  f=$(sql_escape "$family"); v=$(sql_escape "$version"); a=$(sql_escape "$account")
  result=$(psqlq "
    insert into os_images (name, display_name, os_family, os_version, ssh_username,
                           proxmox_vmid, node_id, version, min_disk_gb, status)
    select '$n', '$d', '$f', '$v', '$a', $vmid, n.id, $revision, 10, 'DISABLED'
      from nodes n where n.name = '$NODE'
    on conflict (name, version) do update
       set display_name  = excluded.display_name,
           os_family     = excluded.os_family,
           os_version    = excluded.os_version,
           ssh_username  = excluded.ssh_username,
           proxmox_vmid  = excluded.proxmox_vmid,
           node_id       = excluded.node_id,
           min_disk_gb   = excluded.min_disk_gb
     returning (xmax = 0) as inserted, status;")
  [ -n "$result" ] || { echo "  FAILED: $name rev $revision wrote no row (is node '$NODE' registered?)" >&2; exit 1; }
  IFS='|' read -r inserted status <<<"$result"
  if [ "$inserted" = "t" ]; then
    echo "  added   $name rev $revision -> template $vmid ($status)"
  else
    echo "  updated $name rev $revision -> template $vmid ($status, unchanged)"
  fi
done

echo "== current catalog"
psqlshow "
  select id, name, version, proxmox_vmid, os_family, os_version, ssh_username, status
    from os_images order by os_family, name, version;"
