#!/usr/bin/env bash
# Registers this host's real inventory in the application database: the Proxmox
# node with its MEASURED capacity, the guest IP pool, the forwarding relay
# (including the public host users connect to) and the platform wildcard
# certificate row.
#
# Why this is a script and not a migration: every value below is a property of
# THIS machine and network, not of the schema. A migration carrying them puts
# them into every database whether or not they are true there, so on any other
# host they are wrong rather than merely unhelpful. Capacity is the dangerous
# one: node placement uses the node's memory figure as a hard admission filter
# and divides already-granted vCPU by the thread count to score placement, so a
# figure copied from a bigger machine does not fail — it silently admits guests
# that do not fit and over-commits vCPU. Hence the rule this script is built
# around: capacity is measured on the running host at every run and never read
# from a literal here. Everything that is genuinely configuration (addresses,
# names, bridge, storage, root domain) comes from the environment with a default
# for this environment, and the one value with no safe default — the relay's
# public host — is required.
#
# Idempotent: every row is keyed by its name and updated in place, so a re-run
# after a RAM or CPU change re-measures and corrects the node row. Columns that
# belong to the operator are never touched: the node's status (a node parked in
# MAINTENANCE stays parked), the relay's sync token, enabled flag and generation
# counters, and a REVOKED certificate row (a compromised wildcard is never
# brought back to ACTIVE by a capacity re-run). Writing rows is guarded — a CIDR
# change that would orphan a live allocation, a port band that would drop a live
# mapping, or a changed pool/relay NAME (which would fork the row rather than
# rename it) is refused. The four rows are written in one transaction, so a
# failure part way leaves the database as it was.
#
# Usage:
#   PICKLE_RELAY_PUBLIC_HOST=<relay's public host> bash scripts/apply-platform-inventory.sh
#
# Environment (required first, then defaults for this environment):
#   PICKLE_RELAY_PUBLIC_HOST   REQUIRED. Address users are handed with a
#                              forwarded port. No API writes this column and the
#                              entity has no setter, so a database row is the
#                              only way to set it; blank means users receive a
#                              port with nowhere to connect. Never defaulted —
#                              the real public address does not live in a repo.
#   PICKLE_APP_CTID            101      container running PostgreSQL + the api
#   PICKLE_PROXY_CTID          100      container holding the wildcard material
#   PICKLE_DB                  pickle_dev
#   PICKLE_NODE                pve-node     Proxmox node name (checked against the API)
#   PICKLE_NODE_API_HOST       https://<node>:8006  must name a SAN of the API cert
#   PICKLE_PVE_CERT            /etc/pve/local/pve-ssl.pem   the cert that is checked
#   PICKLE_NODE_BRIDGE         vmbr2    bridge guest NICs attach to
#   PICKLE_NODE_STORAGE        local-lvm  storage clones land on
#   PICKLE_NODE_MEMORY_RESERVE_MB  0    subtracted from the measured total; see below
#   PICKLE_POOL_NAME           guest-private
#   PICKLE_POOL_CIDR           198.19.0.0/16
#   PICKLE_POOL_GATEWAY        198.19.0.1
#   PICKLE_POOL_DNS            ["8.8.8.8"]      jsonb array
#   PICKLE_POOL_RESERVED       two /24s         jsonb array of {from,to}
#   PICKLE_RELAY_NAME          lightsail-1
#   PICKLE_RELAY_SOURCE_IP     100.64.0.1     relay's tunnel-side address
#   PICKLE_RELAY_PORT_BAND     10000-19999
#   PICKLE_ROOT_DOMAIN         pusan.dev
#   PICKLE_WILDCARD_CERT       /etc/nginx/pickle-certs/<root, dots as dashes>.crt
set -euo pipefail

CTID="${PICKLE_APP_CTID:-101}"
PROXY_CTID="${PICKLE_PROXY_CTID:-100}"
# shellcheck source=scripts/lib/ct.sh
. "$(dirname "$0")/lib/ct.sh"
require_ct "$CTID" pickle-app
require_ct "$PROXY_CTID" reverse-proxy
DB="${PICKLE_DB:-pickle_dev}"

NODE="${PICKLE_NODE:-pve-node}"
# Addressed by NAME, not by the bridge address. The api pins the Proxmox API
# certificate as its only trusted CA and has no skip-verification option, and
# that certificate's SANs cover the hostname, not the bridge address — so the
# bridge address fails hostname verification. The app container resolves the name
# to the bridge address in its own hosts file. This default is derived from the
# node name for the same reason; the SAN check below enforces it either way.
NODE_API_HOST="${PICKLE_NODE_API_HOST:-https://$NODE:8006}"
PVE_CERT="${PICKLE_PVE_CERT:-/etc/pve/local/pve-ssl.pem}"
NODE_BRIDGE="${PICKLE_NODE_BRIDGE:-vmbr2}"
NODE_STORAGE="${PICKLE_NODE_STORAGE:-local-lvm}"
MEMORY_RESERVE_MB="${PICKLE_NODE_MEMORY_RESERVE_MB:-0}"

POOL_NAME="${PICKLE_POOL_NAME:-guest-private}"
POOL_CIDR="${PICKLE_POOL_CIDR:-198.19.0.0/16}"
POOL_GATEWAY="${PICKLE_POOL_GATEWAY:-198.19.0.1}"
POOL_DNS="${PICKLE_POOL_DNS:-[\"8.8.8.8\"]}"
# The gateway's own /24 (gateway + infra headroom) and the top /24 (management
# headroom, including the broadcast address) are never handed to a guest.
POOL_RESERVED_DEFAULT='[{"from": "198.19.0.0", "to": "198.19.0.255"},
                        {"from": "198.19.255.0", "to": "198.19.255.255"}]'
POOL_RESERVED="${PICKLE_POOL_RESERVED:-$POOL_RESERVED_DEFAULT}"

RELAY_NAME="${PICKLE_RELAY_NAME:-lightsail-1}"
RELAY_PUBLIC_HOST="${PICKLE_RELAY_PUBLIC_HOST:-}"
RELAY_SOURCE_IP="${PICKLE_RELAY_SOURCE_IP:-100.64.0.1}"
RELAY_PORT_BAND="${PICKLE_RELAY_PORT_BAND:-10000-19999}"

ROOT_DOMAIN="${PICKLE_ROOT_DOMAIN:-pusan.dev}"
CERT_SCOPE="*.$ROOT_DOMAIN"
# One wildcard pair per root domain, the file named after the root with dots as
# dashes — the same convention the proxy tier and the host health snapshot read.
WILDCARD_CERT="${PICKLE_WILDCARD_CERT:-/etc/nginx/pickle-certs/${ROOT_DOMAIN//./-}.crt}"

# ── database access ──────────────────────────────────────────────────────────
# Statements are fed on STDIN, never as `psql -c "…"`. A -c argument travels
# through the second shell `su -c` spawns, and that parse expands $ and eats
# quotes: the pool's reserved-range JSON is full of double quotes and would
# terminate the argument early. Nothing re-parses stdin.
#
# pgq  → tuples-only, unaligned (one value or one row per line, | separated)
# pgshow → aligned table, for the verification pass a human reads
pgq() {
  local out
  if ! out=$(pct exec "$CTID" -- su - postgres -c \
      "psql -q -X -v ON_ERROR_STOP=1 -tA -d $DB -f -" <<<"$1" 2>&1); then
    printf 'query failed: %s\n%s\n' "${1%%$'\n'*}" "$out" >&2
    return 1
  fi
  printf '%s' "$out"
}
pgshow() {
  pct exec "$CTID" -- su - postgres -c \
    "psql -q -X -v ON_ERROR_STOP=1 -d $DB -f -" <<<"$1"
}
# pgtx → the same, wrapped in ONE transaction (-1). The four inventory rows
# describe one machine and are useless in halves: a run that wrote the node and
# the pool and then failed on the relay used to leave the node pointing at a new
# pool with no relay to reach it, and nothing said which half had landed. With -1
# plus ON_ERROR_STOP the whole file commits or none of it does.
pgtx() {
  local out
  if ! out=$(pct exec "$CTID" -- su - postgres -c \
      "psql -q -X -v ON_ERROR_STOP=1 -1 -tA -d $DB -f -" <<<"$1" 2>&1); then
    printf 'the write transaction failed and was rolled back:\n%s\n' "$out" >&2
    return 1
  fi
  printf '%s' "$out"
}
sql_escape() { printf '%s' "$1" | sed "s/'/''/g"; }

die() { echo "refusing to run: $*" >&2; exit 1; }
# For every exit AFTER the backup has been taken. An operator who hits one needs
# to be told where the previous rows are, in the message that stopped them.
fail() {
  echo "FAILED: $*" >&2
  echo "        the rows as they stood before this run are in $BK/inventory-before.sql" >&2
  echo "        (data-only inserts; a rollback deletes the rows this run wrote and replays it)" >&2
  exit 1
}

echo "== preflight"

for tool in pvesh jq openssl; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not on PATH (run this on the Proxmox host as root)"
done

# The one value that must be chosen, checked before anything else is measured.
case "$RELAY_PUBLIC_HOST" in
  '') die "PICKLE_RELAY_PUBLIC_HOST is unset. A relay row with no public host hands
                users a forwarded port with no address to connect to, and nothing else
                writes that column — set it to the relay's public name or address." ;;
  *[[:space:]]*|*/*|*:*) die "PICKLE_RELAY_PUBLIC_HOST='$RELAY_PUBLIC_HOST' must be a bare
                host (no scheme, no port, no path)" ;;
  localhost|127.0.0.1|0.0.0.0|changeme|CHANGEME|TODO)
    die "PICKLE_RELAY_PUBLIC_HOST='$RELAY_PUBLIC_HOST' is a placeholder, not this relay" ;;
  # Reserved and documentation names, matched on the SUFFIX rather than as whole
  # strings: `relay.invalid` is the placeholder this project's own examples and
  # fixtures use, and a whole-string list never catches the next one somebody
  # invents under the same TLD. Nothing under these can ever resolve, so a relay
  # row carrying one hands every user a forwarded port with no address behind it.
  *.invalid|*.example|*.test|*.localhost|\
  example.com|example.net|example.org|*.example.com|*.example.net|*.example.org)
    die "PICKLE_RELAY_PUBLIC_HOST='$RELAY_PUBLIC_HOST' is a reserved/documentation name
                that can never resolve — set it to the relay's real public name or address" ;;
esac
case "$RELAY_PUBLIC_HOST" in
  *.*) : ;;
  *) die "PICKLE_RELAY_PUBLIC_HOST='$RELAY_PUBLIC_HOST' is not a resolvable name or address" ;;
esac
echo "  relay public host: $RELAY_PUBLIC_HOST"

case "$NODE_API_HOST" in
  https://*:*) : ;;
  *) die "PICKLE_NODE_API_HOST='$NODE_API_HOST' must look like https://<host>:<port>" ;;
esac
API_HOST_NAME="${NODE_API_HOST#https://}"; API_HOST_NAME="${API_HOST_NAME%%:*}"

# The api pins this certificate as its only trusted CA and offers no way to skip
# verification, so an api_host the certificate does not cover fails hostname
# verification on every Proxmox call. That is not hypothetical: a seeded literal
# once addressed the API by the bridge address, which is absent from the SANs,
# and the failure only surfaced at the first provisioning run. Check it here,
# where the value is being written.
[ -f "$PVE_CERT" ] || die "no Proxmox API certificate at $PVE_CERT"
sans=$(openssl x509 -in "$PVE_CERT" -noout -ext subjectAltName 2>/dev/null \
       | grep -E '(DNS|IP Address):' | tr -d ' ' | paste -sd, -)
case "$sans," in
  *"DNS:$API_HOST_NAME,"*|*"IPAddress:$API_HOST_NAME,"*) : ;;
  *) die "PICKLE_NODE_API_HOST names '$API_HOST_NAME', which is not a subject-alternative
                name of $PVE_CERT — the api pins that certificate and cannot skip hostname
                verification, so every Proxmox call would fail. Its names are:
                ${sans:-<none>}" ;;
esac
# The name also has to resolve from inside the app container, which is where the
# api runs; the container maps it in its own hosts file rather than via DNS.
pct exec "$CTID" -- getent hosts "$API_HOST_NAME" >/dev/null 2>&1 \
  || die "container $CTID cannot resolve '$API_HOST_NAME' — add it to that container's
                hosts file (pointing at this host's infrastructure-bridge address) first"
echo "  api host $API_HOST_NAME is a certificate SAN and resolves in container $CTID"

case "$RELAY_PORT_BAND" in
  [0-9]*-[0-9]*) BAND_START="${RELAY_PORT_BAND%-*}"; BAND_END="${RELAY_PORT_BAND#*-}" ;;
  *) die "PICKLE_RELAY_PORT_BAND='$RELAY_PORT_BAND' must be <start>-<end>" ;;
esac
if ! { [ "$BAND_START" -ge 1024 ] && [ "$BAND_END" -le 65535 ] \
       && [ "$BAND_END" -ge "$BAND_START" ]; }; then
  die "PICKLE_RELAY_PORT_BAND='$RELAY_PORT_BAND' must sit inside 1024-65535, end >= start"
fi

[ "$MEMORY_RESERVE_MB" -ge 0 ] 2>/dev/null \
  || die "PICKLE_NODE_MEMORY_RESERVE_MB='$MEMORY_RESERVE_MB' is not a non-negative integer"

# The bridge and storage are named in every clone this node performs; a value
# that does not exist here fails at provision time, per VM, long after this run.
ip link show "$NODE_BRIDGE" >/dev/null 2>&1 \
  || die "bridge '$NODE_BRIDGE' does not exist on this host"
pvesm status --storage "$NODE_STORAGE" >/dev/null 2>&1 \
  || die "storage '$NODE_STORAGE' is not present/active on this host"
echo "  bridge $NODE_BRIDGE and storage $NODE_STORAGE exist"

# The pool gateway is the address guests are told to route through, so it has to
# be an address this host actually answers on that bridge. A pool copied from
# another environment fails here rather than at the first guest's first packet.
if ! ip -4 -o addr show "$NODE_BRIDGE" | grep -qF " ${POOL_GATEWAY}/"; then
  die "pool gateway $POOL_GATEWAY is not configured on $NODE_BRIDGE — the address a
                guest routes through must be one this host answers on that bridge"
fi
echo "  pool gateway $POOL_GATEWAY answers on $NODE_BRIDGE"

# ── capacity: measured, never a literal ──────────────────────────────────────
# Source: the Proxmox node status API. `.cpuinfo.cpus` is the logical-thread
# count the hypervisor assigns vCPUs from and `.memory.total` is the memory the
# kernel actually has, which is exactly what Proxmox schedules and reports
# against — the same numbers the node summary shows an operator.
#
# Alternatives and why not:
#   nproc          reports the CALLING process's CPU affinity/cgroup allowance,
#                  so a run under a restricted cpuset under-reports and the
#                  under-report is invisible.
#   lscpu          re-derives sockets x cores x threads that the API already
#                  publishes, and disagrees with it when a socket is offline.
#   dmidecode      reports installed DIMMs — memory the kernel cannot allocate
#                  (firmware-reserved regions) counts in, which is the direction
#                  that over-commits, the exact failure this script exists to stop.
#   free -m        agrees with the API here, but says nothing about whether the
#                  node NAME being registered is this host.
# That last point is the second reason for this source: querying by node name
# means a wrong PICKLE_NODE fails here instead of writing a row describing a
# node Proxmox does not have.
node_status=$(pvesh get "/nodes/$NODE/status" --output-format json 2>/dev/null) \
  || die "Proxmox has no node named '$NODE' (or the API is unreachable)"
CPU_THREADS=$(jq -r '.cpuinfo.cpus // empty' <<<"$node_status")
MEM_BYTES=$(jq -r '.memory.total // empty' <<<"$node_status")
if ! { [ -n "$CPU_THREADS" ] && [ -n "$MEM_BYTES" ]; }; then
  die "node status for '$NODE' carried no cpu/memory figures"
fi
MEASURED_MEMORY_MB=$(( MEM_BYTES / 1048576 ))
# Disk capacity comes from the storage status of the SAME storage entry the
# node row registers for clones — the identical number the api reads live via
# GET /nodes/{n}/storage, so the stored figure and the live figure can never
# disagree about what they measure. For an lvmthin storage this is the pool's
# real size; guests over-provision against it, so the column is an advisory
# denominator, not a physical guarantee. Stored in GiB to match vms.disk_gb.
storage_status=$(pvesh get "/nodes/$NODE/storage" --output-format json 2>/dev/null) \
  || die "could not read storage status for '$NODE'"
# Exactly one entry has to answer, checked before the value is read: two lines
# out of jq would flow into the arithmetic below as a single string and either
# abort the run mid-transaction or register a nonsense capacity. Every other
# measurement here is likewise pinned to one source.
disk_entries=$(jq -r --arg s "$NODE_STORAGE" '[.[] | select(.storage == $s)] | length' \
  <<<"$storage_status")
case "$disk_entries" in
  1) : ;;
  0) die "node storage status has no entry for '$NODE_STORAGE' — the node does not
                present that storage, so there is nothing to measure" ;;
  *) die "node storage status returned ${disk_entries} entries for '$NODE_STORAGE' —
                exactly one is expected, so the measured capacity is ambiguous" ;;
esac
DISK_BYTES=$(jq -r --arg s "$NODE_STORAGE" \
  'first(.[] | select(.storage == $s) | .total) // empty' <<<"$storage_status")
[ -n "$DISK_BYTES" ] || die "storage '$NODE_STORAGE' has no total in the node storage status"
DISK_CAPACITY_GB=$(( DISK_BYTES / 1073741824 ))
[ "$DISK_CAPACITY_GB" -gt 0 ] || die "storage '$NODE_STORAGE' measured ${DISK_BYTES} bytes"
# The registered figure is the measured total minus an optional reserve. The
# admission filter counts guest intent only, so nothing in it accounts for the
# host's own footprint and the infrastructure containers sharing this RAM; the
# reserve is how an operator hands that back. Default 0 = register what was
# measured, unmodified.
MEMORY_MB=$(( MEASURED_MEMORY_MB - MEMORY_RESERVE_MB ))
[ "$MEMORY_MB" -gt 0 ] || die "memory reserve ${MEMORY_RESERVE_MB}MB leaves no capacity
                (measured ${MEASURED_MEMORY_MB}MB)"
echo "  measured on $NODE: $CPU_THREADS threads, ${MEASURED_MEMORY_MB}MB memory, ${DISK_CAPACITY_GB}GiB pool ($NODE_STORAGE)"
[ "$MEMORY_RESERVE_MB" -eq 0 ] \
  || echo "  registering ${MEMORY_MB}MB (reserve ${MEMORY_RESERVE_MB}MB withheld)"

# ── certificate: expiry read off the installed material ──────────────────────
# not_after is READ from the certificate this host actually serves, because the
# column exists so the admin certificate list and the host health snapshot can
# warn before it lapses. A literal date is an assertion nobody checked, and one
# later than the truth silences the warning exactly when it is needed. So: no
# material, no row.
pct exec "$PROXY_CTID" -- test -f "$WILDCARD_CERT" \
  || die "no wildcard certificate at $WILDCARD_CERT in container $PROXY_CTID — install the
                pair for $ROOT_DOMAIN first; this script describes material, it does not
                invent it"
cert_text=$(pct exec "$PROXY_CTID" -- openssl x509 -noout -text -in "$WILDCARD_CERT") \
  || die "openssl could not read $WILDCARD_CERT"
grep -qF "DNS:$CERT_SCOPE" <<<"$cert_text" \
  || die "the certificate at $WILDCARD_CERT does not cover $CERT_SCOPE — registering it
                under that scope would point every $ROOT_DOMAIN publish at the wrong pair"
cert_end=$(pct exec "$PROXY_CTID" -- \
  openssl x509 -noout -enddate -in "$WILDCARD_CERT" | cut -d= -f2-)
CERT_NOT_AFTER=$(date -u -d "$cert_end" +'%Y-%m-%dT%H:%M:%S+00:00') \
  || die "could not parse the certificate end date '$cert_end'"
cert_epoch=$(date -u -d "$cert_end" +%s)
CERT_DAYS_LEFT=$(( (cert_epoch - $(date +%s)) / 86400 ))
[ "$CERT_DAYS_LEFT" -ge 0 ] \
  || die "the certificate at $WILDCARD_CERT expired ${CERT_DAYS_LEFT#-} days ago — replace it
                before registering a row that claims it is usable"
echo "  certificate $CERT_SCOPE valid until $CERT_NOT_AFTER (${CERT_DAYS_LEFT}d left)"

# ── the schema must already be there ─────────────────────────────────────────
# Flyway owns these tables; it runs when the api starts. Say so plainly instead
# of failing four statements deep.
missing=$(pgq "
  select string_agg(t, ', ' order by t)
    from (values ('nodes'),('ip_pools'),('relays'),('certificates'),
                 ('ip_allocations'),('port_mappings'),('vms')) as w(t)
   where not exists (select 1 from information_schema.tables
                      where table_schema = 'public' and table_name = w.t);")
[ -z "$missing" ] || die "database $DB has no $missing table — start the api once so the
                migrations run, then re-run this"
# The disk column arrived later than the tables (V76); an older database would
# make every statement below fail four writes deep, so say so plainly here.
has_disk_col=$(pgq "
  select count(*) from information_schema.columns
   where table_schema = 'public' and table_name = 'nodes'
     and column_name = 'disk_capacity_gb';")
[ "$has_disk_col" = "1" ] || die "nodes has no disk_capacity_gb column — deploy an api that
                carries migration V76 first, then re-run this"

# Configuration shapes are checked by the types that will store them, and the
# relationships the shapes alone cannot show are checked here: the gateway must
# be inside the pool, and every reserved range must be a non-inverted range
# inside it too. A typo'd reserved range silently hands out an infrastructure
# address later.
gw_cidr=$(sql_escape "$POOL_CIDR"); gw_ip=$(sql_escape "$POOL_GATEWAY")
res_json=$(sql_escape "$POOL_RESERVED"); dns_json=$(sql_escape "$POOL_DNS")
shape=$(pgq "
  select case when not ('$gw_ip'::inet <<= '$gw_cidr'::cidr)
              then 'gateway $POOL_GATEWAY is outside $POOL_CIDR'
              when jsonb_typeof('$dns_json'::jsonb) <> 'array'
              then 'PICKLE_POOL_DNS is not a json array'
              when jsonb_typeof('$res_json'::jsonb) <> 'array'
              then 'PICKLE_POOL_RESERVED is not a json array'
              when exists (
                     select 1 from jsonb_array_elements('$res_json'::jsonb) e
                      where e->>'from' is null or e->>'to' is null
                         or not ((e->>'from')::inet <<= '$gw_cidr'::cidr)
                         or not ((e->>'to')::inet <<= '$gw_cidr'::cidr)
                         or (e->>'from')::inet > (e->>'to')::inet)
              then 'a reserved range is inverted, incomplete, or outside $POOL_CIDR'
              else '' end;")
[ -z "$shape" ] || die "$shape"

# Guards: renaming. Every write below is an upsert keyed on the row's NAME, so a
# changed name does not rename anything — it conflicts with nothing and inserts a
# SECOND row describing the same real thing, while the guards below (which join on
# the requested name) look at the new row and find it innocent. For the pool that
# is silent corruption: a second pool over the same CIDR, the node repointed at
# it, the old pool's ALLOCATED rows left behind, and IPAM then handing out
# addresses that are already in use. For the relay it is a crash instead, because
# source_ip is unique and the insert dies on that constraint half way through the
# run. Neither is a rename this script knows how to perform, so refuse and say so.
existing_pool=$(pgq "
  select coalesce(
    (select p.name from ip_pools p
      where p.cidr = '$(sql_escape "$POOL_CIDR")'::cidr
        and p.name <> '$(sql_escape "$POOL_NAME")' limit 1),
    (select p.name from nodes n join ip_pools p on p.id = n.ip_pool_id
      where n.name = '$(sql_escape "$NODE")'
        and p.name <> '$(sql_escape "$POOL_NAME")' limit 1),
    '');")
[ -z "$existing_pool" ] || die "pool '$existing_pool' already describes this network (same CIDR,
                or already linked to node '$NODE'), but PICKLE_POOL_NAME asks for
                '$POOL_NAME'. Writing it would add a second pool over the same
                addresses instead of renaming the first, and every allocation on
                '$existing_pool' would be orphaned. This script does not rename rows —
                either set PICKLE_POOL_NAME='$existing_pool', or rename the row in
                place first and re-run"
existing_relay=$(pgq "
  select coalesce((select r.name from relays r
                    where r.source_ip = '$(sql_escape "$RELAY_SOURCE_IP")'
                      and r.name <> '$(sql_escape "$RELAY_NAME")' limit 1), '');")
[ -z "$existing_relay" ] || die "relay '$existing_relay' is already registered on source address
                $RELAY_SOURCE_IP, but PICKLE_RELAY_NAME asks for '$RELAY_NAME'. That address is
                unique per relay, so this run would fail on the constraint rather than rename
                anything. This script does not rename rows — either set
                PICKLE_RELAY_NAME='$existing_relay', or rename the row in place first and re-run"

# Guards: the two updates that could invalidate live state.
orphans=$(pgq "
  select count(*) from ip_allocations a
    join ip_pools p on p.id = a.pool_id
   where p.name = '$(sql_escape "$POOL_NAME")' and a.status = 'ALLOCATED'
     and not (a.ip <<= '$gw_cidr'::cidr);")
[ "$orphans" = "0" ] || die "$orphans live allocation(s) in pool '$POOL_NAME' fall outside
                $POOL_CIDR — moving the pool would orphan addresses guests are using"
stranded=$(pgq "
  select count(*) from port_mappings m
    join relays r on r.id = m.relay_id
   where r.name = '$(sql_escape "$RELAY_NAME")'
     and (m.public_port < $BAND_START or m.public_port > $BAND_END);")
[ "$stranded" = "0" ] || die "$stranded live port mapping(s) on relay '$RELAY_NAME' sit
                outside $RELAY_PORT_BAND — narrowing the band would strand them"

echo "  preflight OK"

# ── backup the rows about to be overwritten ─────────────────────────────────
ts=$(date +%Y%m%d-%H%M%S)
BK="${BK:-/srv/pickle/backup/platform-inventory-$ts}"
mkdir -p "$BK"
echo "== backup the inventory rows -> $BK/inventory-before.sql"
pct exec "$CTID" -- su - postgres -c \
  "pg_dump -d $DB --data-only --inserts -t nodes -t ip_pools -t relays -t certificates" \
  > "$BK/inventory-before.sql"
echo "  $(wc -l < "$BK/inventory-before.sql") lines dumped"

# ── writes: all four rows, one transaction ──────────────────────────────────
# Pool first: the node row references it, and \gset carries the id forward
# without a round trip that would have to happen outside the transaction.
#
# The node's status is deliberately absent from its update list: it is the
# operator's admission switch, and a node parked in MAINTENANCE must not come
# back ACTIVE because someone re-measured its RAM. The relay's token_hash,
# enabled flag and generation counters are left alone for the same reason — the
# token is issued by the operator at deploy time and the counters are the sync
# protocol's state.
#
# certificates has no unique key over (kind, scope) — a wildcard row is not
# schema-unique because per-domain rows share the table — so that one is an
# update-then-insert-if-the-scope-has-no-row rather than an upsert. status is
# forced to ACTIVE because publishing accepts only an ACTIVE wildcard row for a
# root, so a row left FAILED or RENEWING by an earlier attempt would refuse every
# publish under a certificate we have just read off the host and found valid.
# REVOKED is the one status that is never overwritten: a certificate is revoked
# when its key is considered compromised, publishing being gated closed is then
# the POINT, and this script is re-run for unrelated reasons (its own runbook
# says to re-run it whenever the host's capacity changes). So the update skips
# REVOKED rows and the insert asks whether the scope has any row at all — a
# revoked row therefore blocks both paths, nothing is written, and the operator
# is told below.
echo "== register the inventory (node, pool, relay, certificate — one transaction)"
if ! result=$(pgtx "
  insert into ip_pools (name, cidr, gateway, dns, reserved_ranges)
  values ('$(sql_escape "$POOL_NAME")', '$gw_cidr', '$gw_ip',
          '$dns_json'::jsonb, '$res_json'::jsonb)
  on conflict (name) do update
     set cidr = excluded.cidr, gateway = excluded.gateway, dns = excluded.dns,
         reserved_ranges = excluded.reserved_ranges, updated_at = now()
  returning id as pool_id, (xmax = 0)::text as pool_new
  \gset

  insert into nodes (name, api_host, cpu_threads, memory_mb, disk_capacity_gb,
                     vm_bridge, storage, ip_pool_id)
  values ('$(sql_escape "$NODE")', '$(sql_escape "$NODE_API_HOST")',
          $CPU_THREADS, $MEMORY_MB, $DISK_CAPACITY_GB, '$(sql_escape "$NODE_BRIDGE")',
          '$(sql_escape "$NODE_STORAGE")', :pool_id)
  on conflict (name) do update
     set api_host = excluded.api_host, cpu_threads = excluded.cpu_threads,
         memory_mb = excluded.memory_mb, disk_capacity_gb = excluded.disk_capacity_gb,
         vm_bridge = excluded.vm_bridge, storage = excluded.storage,
         ip_pool_id = excluded.ip_pool_id, updated_at = now()
  returning id as node_id, (xmax = 0)::text as node_new, status::text as node_state
  \gset

  insert into relays (name, public_host, source_ip, port_band_start, port_band_end)
  values ('$(sql_escape "$RELAY_NAME")', '$(sql_escape "$RELAY_PUBLIC_HOST")',
          '$(sql_escape "$RELAY_SOURCE_IP")', $BAND_START, $BAND_END)
  on conflict (name) do update
     set public_host = excluded.public_host, source_ip = excluded.source_ip,
         port_band_start = excluded.port_band_start,
         port_band_end = excluded.port_band_end, updated_at = now()
  returning id as relay_id, (xmax = 0)::text as relay_new,
            (token_hash is not null)::text as relay_tokened, enabled::text as relay_enabled
  \gset

  with upd as (
    update certificates
       set not_after = '$CERT_NOT_AFTER', status = 'ACTIVE', last_error = null,
           updated_at = now()
     where kind = 'ORIGIN_CA_WILDCARD' and domain_id is null
       and scope = '$(sql_escape "$CERT_SCOPE")'
       and status <> 'REVOKED'
    returning id
  ), ins as (
    insert into certificates (domain_id, kind, scope, not_after, status)
    select null, 'ORIGIN_CA_WILDCARD', '$(sql_escape "$CERT_SCOPE")',
           '$CERT_NOT_AFTER', 'ACTIVE'
     where not exists (select 1 from certificates
                        where kind = 'ORIGIN_CA_WILDCARD' and domain_id is null
                          and scope = '$(sql_escape "$CERT_SCOPE")')
    returning id
  )
  select (select count(*) from ins) as cert_ins,
         (select count(*) from upd) as cert_upd,
         (select count(*) from certificates
           where kind = 'ORIGIN_CA_WILDCARD' and domain_id is null
             and scope = '$(sql_escape "$CERT_SCOPE")'
             and status = 'REVOKED') as cert_revoked
  \gset

  select :pool_id, :'pool_new', :node_id, :'node_new', :'node_state',
         :relay_id, :'relay_new', :'relay_tokened', :'relay_enabled',
         :cert_ins, :cert_upd, :cert_revoked;"); then
  fail "no inventory row was written — the transaction rolled back, so nothing was applied"
fi
IFS='|' read -r POOL_ID pool_new NODE_ID node_new node_state \
                RELAY_ID relay_new tokened enabled \
                cert_ins cert_upd cert_revoked <<<"$result"
{ [ -n "$POOL_ID" ] && [ -n "$NODE_ID" ] && [ -n "$RELAY_ID" ]; } \
  || fail "the write transaction returned '$result' instead of the four row ids"

[ "$pool_new" = "true" ] && echo "  added   pool $POOL_NAME ($POOL_CIDR) id $POOL_ID" \
                      || echo "  updated pool $POOL_NAME ($POOL_CIDR) id $POOL_ID"
[ "$node_new" = "true" ] && echo "  added   node $NODE id $NODE_ID ($node_state)" \
                      || echo "  updated node $NODE id $NODE_ID ($node_state, status untouched)"
[ "$relay_new" = "true" ] && echo "  added   relay $RELAY_NAME id $RELAY_ID -> $RELAY_PUBLIC_HOST" \
                       || echo "  updated relay $RELAY_NAME id $RELAY_ID -> $RELAY_PUBLIC_HOST"
echo "  sync token issued: $tokened, enabled: $enabled"
if [ "${cert_ins:-0}" -gt 0 ]; then
  echo "  added   certificate $CERT_SCOPE until $CERT_NOT_AFTER"
elif [ "${cert_upd:-0}" -gt 0 ]; then
  # More than one row here means the table already held duplicates for this
  # scope; they are all brought to the same measured date, and the verification
  # table below shows every wildcard row so a duplicate is visible.
  echo "  updated $cert_upd certificate row(s) for $CERT_SCOPE until $CERT_NOT_AFTER"
elif [ "${cert_revoked:-0}" -gt 0 ]; then
  : # reported as a warning below; not an error, and not something to overwrite
else
  fail "no certificate row was written for $CERT_SCOPE"
fi
if [ "${cert_revoked:-0}" -gt 0 ]; then
  echo "  WARN $cert_revoked certificate row(s) for $CERT_SCOPE are REVOKED and were LEFT ALONE." >&2
  echo "       This script will not bring a revoked certificate back: the row is revoked because" >&2
  echo "       its key was considered compromised, and every publish under $ROOT_DOMAIN staying" >&2
  echo "       refused is the intended consequence, not a fault to repair. To restore publishing:" >&2
  echo "       issue a NEW wildcard pair for $ROOT_DOMAIN, install it at $WILDCARD_CERT in" >&2
  echo "       container $PROXY_CTID, delete the REVOKED row (or re-scope it so it no longer" >&2
  echo "       claims $CERT_SCOPE), then re-run this script." >&2
fi

# ── verification: print what is now true ─────────────────────────────────────
echo
echo "== node (stored vs measured)"
pgshow "
  select n.name, n.status, n.api_host, n.vm_bridge, n.storage,
         n.cpu_threads as threads_stored, $CPU_THREADS as threads_measured,
         n.memory_mb as memory_stored, $MEASURED_MEMORY_MB as memory_measured,
         n.disk_capacity_gb as disk_stored, $DISK_CAPACITY_GB as disk_measured,
         coalesce(v.vcpu, 0) as vcpu_granted,
         coalesce(v.memory_mb, 0) as memory_granted,
         n.memory_mb - coalesce(v.memory_mb, 0) as memory_free,
         coalesce(v.disk_gb, 0) as disk_granted
    from nodes n
    left join (select node_id, sum(vcpu) as vcpu, sum(memory_mb) as memory_mb,
                      sum(disk_gb) as disk_gb
                 from vms where deleted_at is null and status <> 'DELETED'
                group by node_id) v on v.node_id = n.id
   where n.id = $NODE_ID;"

echo "== ip pool (usage)"
pgshow "
  select p.name, p.cidr, p.gateway, p.dns, p.reserved_ranges,
         (2 ^ (32 - masklen(p.cidr)))::bigint as addresses,
         coalesce((select sum((e->>'to')::inet - (e->>'from')::inet + 1)
                     from jsonb_array_elements(p.reserved_ranges) e), 0) as reserved,
         (select count(*) from ip_allocations a
           where a.pool_id = p.id and a.status = 'ALLOCATED') as allocated,
         (select count(*) from nodes n where n.ip_pool_id = p.id) as nodes_linked
    from ip_pools p where p.id = $POOL_ID;"

echo "== relay"
pgshow "
  select r.name, r.public_host, r.source_ip,
         r.port_band_start || '-' || r.port_band_end as port_band,
         r.enabled, (r.token_hash is not null) as token_issued,
         (select count(*) from port_mappings m where m.relay_id = r.id) as mappings
    from relays r where r.id = $RELAY_ID;"

echo "== platform wildcard certificate"
pgshow "
  select c.id, c.kind, c.scope, c.status, c.not_after,
         (date_part('day', c.not_after - now()))::int as days_left
    from certificates c
   where c.kind = 'ORIGIN_CA_WILDCARD' and c.domain_id is null
   order by c.scope;"

echo "== checks"
vfail=0
check() { # check LABEL ACTUAL EXPECTED
  if [ "$2" = "$3" ]; then echo "  OK   $1 = $2"
  else echo "  FAIL $1 = $2 (expected $3)"; vfail=$((vfail + 1)); fi
}
row=$(pgq "select cpu_threads, memory_mb, disk_capacity_gb, ip_pool_id, api_host, vm_bridge, storage
             from nodes where id = $NODE_ID;")
IFS='|' read -r s_threads s_memory s_disk s_pool s_api s_bridge s_storage <<<"$row"
check "node threads (measured)" "$s_threads" "$CPU_THREADS"
check "node memory  (registered)" "$s_memory" "$MEMORY_MB"
check "node disk    (measured)" "$s_disk" "$DISK_CAPACITY_GB"
check "node pool link" "$s_pool" "$POOL_ID"
check "node api host" "$s_api" "$NODE_API_HOST"
check "node bridge" "$s_bridge" "$NODE_BRIDGE"
check "node storage" "$s_storage" "$NODE_STORAGE"
check "relay public host" "$(pgq "select coalesce(public_host, '') from relays where id = $RELAY_ID;")" \
      "$RELAY_PUBLIC_HOST"
# One ACTIVE wildcard row is the expectation, except when the scope's row is
# REVOKED and was deliberately left that way: then zero is the correct answer and
# demanding one would report the refusal-to-resurrect as a failure.
cert_expect=1
{ [ "${cert_ins:-0}" -gt 0 ] || [ "${cert_upd:-0}" -gt 0 ]; } || cert_expect=0
check "ACTIVE wildcard rows for $CERT_SCOPE" \
      "$(pgq "select count(*) from certificates
                where kind = 'ORIGIN_CA_WILDCARD' and domain_id is null
                  and scope = '$(sql_escape "$CERT_SCOPE")' and status = 'ACTIVE';")" "$cert_expect"

# The api reaches Proxmox from the app container only (the host firewall admits
# :8006 from that address alone), so the address is verified from there. Any HTTP
# response proves reachability; an auth failure still means the address is right.
if pct exec "$CTID" -- curl -sk -o /dev/null --max-time 10 "$NODE_API_HOST/api2/json/version"; then
  echo "  OK   api host $NODE_API_HOST answers from container $CTID"
else
  echo "  FAIL api host $NODE_API_HOST does not answer from container $CTID"
  vfail=$((vfail + 1))
fi

# Not a failure — a state the operator has to see. Capacity already granted can
# exceed a freshly measured (or reserved) total after a downgrade, and placement
# will simply refuse every new guest until the excess is released.
granted=$(pgq "select coalesce(sum(memory_mb), 0) from vms
                where node_id = $NODE_ID and deleted_at is null and status <> 'DELETED';")
if [ "$granted" -gt "$MEMORY_MB" ]; then
  echo "  WARN ${granted}MB already granted on $NODE exceeds the registered ${MEMORY_MB}MB —"
  echo "       placement will admit no further guests until that is released"
fi
[ "$tokened" = "true" ] || echo "  WARN relay '$RELAY_NAME' has no sync token: every relay sync
       fails closed until one is issued and installed on both sides"
[ "$CERT_DAYS_LEFT" -ge 30 ] \
  || echo "  WARN the wildcard for $ROOT_DOMAIN expires in ${CERT_DAYS_LEFT}d"

[ "$vfail" -eq 0 ] || fail "$vfail verification check(s) — the rows above WERE committed"
echo "OK — inventory registered for $NODE. Pre-change rows kept at $BK/inventory-before.sql."
