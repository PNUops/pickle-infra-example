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
# counters. Writing rows is guarded — a CIDR change that would orphan a live
# allocation, or a port band that would drop a live mapping, is refused.
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
#   PICKLE_NODE                pve1     Proxmox node name (checked against the API)
#   PICKLE_NODE_API_HOST       https://<node>:8006  must name a SAN of the API cert
#   PICKLE_PVE_CERT            /etc/pve/local/pve-ssl.pem   the cert that is checked
#   PICKLE_NODE_BRIDGE         vmbr2    bridge guest NICs attach to
#   PICKLE_NODE_STORAGE        local-lvm  storage clones land on
#   PICKLE_NODE_MEMORY_RESERVE_MB  0    subtracted from the measured total; see below
#   PICKLE_POOL_NAME           guest-private
#   PICKLE_POOL_CIDR           172.29.0.0/16
#   PICKLE_POOL_GATEWAY        172.29.0.1
#   PICKLE_POOL_DNS            ["8.8.8.8"]      jsonb array
#   PICKLE_POOL_RESERVED       two /24s         jsonb array of {from,to}
#   PICKLE_RELAY_NAME          lightsail-1
#   PICKLE_RELAY_SOURCE_IP     10.100.100.1     relay's tunnel-side address
#   PICKLE_RELAY_PORT_BAND     10000-19999
#   PICKLE_ROOT_DOMAIN         pusan.dev
#   PICKLE_WILDCARD_CERT       /etc/nginx/pickle-certs/<root, dots as dashes>.crt
set -euo pipefail

CTID="${PICKLE_APP_CTID:-101}"
PROXY_CTID="${PICKLE_PROXY_CTID:-100}"
DB="${PICKLE_DB:-pickle_dev}"

NODE="${PICKLE_NODE:-pve1}"
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
POOL_CIDR="${PICKLE_POOL_CIDR:-172.29.0.0/16}"
POOL_GATEWAY="${PICKLE_POOL_GATEWAY:-172.29.0.1}"
POOL_DNS="${PICKLE_POOL_DNS:-[\"8.8.8.8\"]}"
# The gateway's own /24 (gateway + infra headroom) and the top /24 (management
# headroom, including the broadcast address) are never handed to a guest.
POOL_RESERVED_DEFAULT='[{"from": "172.29.0.0", "to": "172.29.0.255"},
                        {"from": "172.29.255.0", "to": "172.29.255.255"}]'
POOL_RESERVED="${PICKLE_POOL_RESERVED:-$POOL_RESERVED_DEFAULT}"

RELAY_NAME="${PICKLE_RELAY_NAME:-lightsail-1}"
RELAY_PUBLIC_HOST="${PICKLE_RELAY_PUBLIC_HOST:-}"
RELAY_SOURCE_IP="${PICKLE_RELAY_SOURCE_IP:-10.100.100.1}"
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
sql_escape() { printf '%s' "$1" | sed "s/'/''/g"; }

die() { echo "refusing to run: $*" >&2; exit 1; }

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
  localhost|127.0.0.1|0.0.0.0|changeme|CHANGEME|TODO|example.com|relay.example.net)
    die "PICKLE_RELAY_PUBLIC_HOST='$RELAY_PUBLIC_HOST' is a placeholder, not this relay" ;;
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
# The registered figure is the measured total minus an optional reserve. The
# admission filter counts guest intent only, so nothing in it accounts for the
# host's own footprint and the infrastructure containers sharing this RAM; the
# reserve is how an operator hands that back. Default 0 = register what was
# measured, unmodified.
MEMORY_MB=$(( MEASURED_MEMORY_MB - MEMORY_RESERVE_MB ))
[ "$MEMORY_MB" -gt 0 ] || die "memory reserve ${MEMORY_RESERVE_MB}MB leaves no capacity
                (measured ${MEASURED_MEMORY_MB}MB)"
echo "  measured on $NODE: $CPU_THREADS threads, ${MEASURED_MEMORY_MB}MB memory"
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
BK="${BK:-/root/pickle/backup/platform-inventory-$ts}"
mkdir -p "$BK"
echo "== backup the inventory rows -> $BK/inventory-before.sql"
pct exec "$CTID" -- su - postgres -c \
  "pg_dump -d $DB --data-only --inserts -t nodes -t ip_pools -t relays -t certificates" \
  > "$BK/inventory-before.sql"
echo "  $(wc -l < "$BK/inventory-before.sql") lines dumped"

# ── writes ───────────────────────────────────────────────────────────────────
# Pool first: the node row references it.
echo "== register the IP pool"
result=$(pgq "
  insert into ip_pools (name, cidr, gateway, dns, reserved_ranges)
  values ('$(sql_escape "$POOL_NAME")', '$gw_cidr', '$gw_ip',
          '$dns_json'::jsonb, '$res_json'::jsonb)
  on conflict (name) do update
     set cidr = excluded.cidr, gateway = excluded.gateway, dns = excluded.dns,
         reserved_ranges = excluded.reserved_ranges, updated_at = now()
  returning (xmax = 0) as inserted, id;")
IFS='|' read -r inserted POOL_ID <<<"$result"
[ -n "$POOL_ID" ] || die "the pool row was not written"
[ "$inserted" = "t" ] && echo "  added   pool $POOL_NAME ($POOL_CIDR) id $POOL_ID" \
                      || echo "  updated pool $POOL_NAME ($POOL_CIDR) id $POOL_ID"

# The node row carries the measured capacity. status is deliberately absent from
# the update list: it is the operator's admission switch, and a node parked in
# MAINTENANCE must not come back ACTIVE because someone re-measured its RAM.
echo "== register the node with its measured capacity"
result=$(pgq "
  insert into nodes (name, api_host, cpu_threads, memory_mb, vm_bridge, storage, ip_pool_id)
  values ('$(sql_escape "$NODE")', '$(sql_escape "$NODE_API_HOST")',
          $CPU_THREADS, $MEMORY_MB, '$(sql_escape "$NODE_BRIDGE")',
          '$(sql_escape "$NODE_STORAGE")', $POOL_ID)
  on conflict (name) do update
     set api_host = excluded.api_host, cpu_threads = excluded.cpu_threads,
         memory_mb = excluded.memory_mb, vm_bridge = excluded.vm_bridge,
         storage = excluded.storage, ip_pool_id = excluded.ip_pool_id,
         updated_at = now()
  returning (xmax = 0) as inserted, id, status::text;")
IFS='|' read -r inserted NODE_ID node_state <<<"$result"
[ -n "$NODE_ID" ] || die "the node row was not written"
[ "$inserted" = "t" ] && echo "  added   node $NODE id $NODE_ID ($node_state)" \
                      || echo "  updated node $NODE id $NODE_ID ($node_state, status untouched)"

# The relay row's public host is the point of this step. token_hash, enabled and
# the generation counters are left alone: the token is issued by the operator at
# deploy time and the counters are the sync protocol's state.
echo "== register the relay"
result=$(pgq "
  insert into relays (name, public_host, source_ip, port_band_start, port_band_end)
  values ('$(sql_escape "$RELAY_NAME")', '$(sql_escape "$RELAY_PUBLIC_HOST")',
          '$(sql_escape "$RELAY_SOURCE_IP")', $BAND_START, $BAND_END)
  on conflict (name) do update
     set public_host = excluded.public_host, source_ip = excluded.source_ip,
         port_band_start = excluded.port_band_start,
         port_band_end = excluded.port_band_end, updated_at = now()
  returning (xmax = 0) as inserted, id, (token_hash is not null) as tokened, enabled;")
IFS='|' read -r inserted RELAY_ID tokened enabled <<<"$result"
[ -n "$RELAY_ID" ] || die "the relay row was not written"
[ "$inserted" = "t" ] && echo "  added   relay $RELAY_NAME id $RELAY_ID -> $RELAY_PUBLIC_HOST" \
                      || echo "  updated relay $RELAY_NAME id $RELAY_ID -> $RELAY_PUBLIC_HOST"
echo "  sync token issued: $tokened, enabled: $enabled"

# certificates has no unique key over (kind, scope) — a wildcard row is not
# schema-unique because per-domain rows share the table — so this is an
# update-then-insert-if-nothing-was-updated in one statement rather than an
# upsert. status is set ACTIVE on both paths on purpose: publishing accepts only
# an ACTIVE wildcard row for a root, so a row left FAILED or RENEWING from an
# earlier attempt would refuse every publish under a certificate we have just
# read off the host and found valid.
echo "== register the platform wildcard certificate"
result=$(pgq "
  with upd as (
    update certificates
       set not_after = '$CERT_NOT_AFTER', status = 'ACTIVE', last_error = null,
           updated_at = now()
     where kind = 'ORIGIN_CA_WILDCARD' and domain_id is null
       and scope = '$(sql_escape "$CERT_SCOPE")'
    returning id
  ), ins as (
    insert into certificates (domain_id, kind, scope, not_after, status)
    select null, 'ORIGIN_CA_WILDCARD', '$(sql_escape "$CERT_SCOPE")',
           '$CERT_NOT_AFTER', 'ACTIVE'
     where not exists (select 1 from upd)
    returning id
  )
  select (select count(*) from ins), (select count(*) from upd);")
IFS='|' read -r cert_ins cert_upd <<<"$result"
if [ "${cert_ins:-0}" -gt 0 ]; then
  echo "  added   certificate $CERT_SCOPE until $CERT_NOT_AFTER"
elif [ "${cert_upd:-0}" -gt 0 ]; then
  # More than one row here means the table already held duplicates for this
  # scope; they are all brought to the same measured date, and the verification
  # table below shows every wildcard row so a duplicate is visible.
  echo "  updated $cert_upd certificate row(s) for $CERT_SCOPE until $CERT_NOT_AFTER"
else
  die "no certificate row was written"
fi

# ── verification: print what is now true ─────────────────────────────────────
echo
echo "== node (stored vs measured)"
pgshow "
  select n.name, n.status, n.api_host, n.vm_bridge, n.storage,
         n.cpu_threads as threads_stored, $CPU_THREADS as threads_measured,
         n.memory_mb as memory_stored, $MEASURED_MEMORY_MB as memory_measured,
         coalesce(v.vcpu, 0) as vcpu_granted,
         coalesce(v.memory_mb, 0) as memory_granted,
         n.memory_mb - coalesce(v.memory_mb, 0) as memory_free
    from nodes n
    left join (select node_id, sum(vcpu) as vcpu, sum(memory_mb) as memory_mb
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
row=$(pgq "select cpu_threads, memory_mb, ip_pool_id, api_host, vm_bridge, storage
             from nodes where id = $NODE_ID;")
IFS='|' read -r s_threads s_memory s_pool s_api s_bridge s_storage <<<"$row"
check "node threads (measured)" "$s_threads" "$CPU_THREADS"
check "node memory  (registered)" "$s_memory" "$MEMORY_MB"
check "node pool link" "$s_pool" "$POOL_ID"
check "node api host" "$s_api" "$NODE_API_HOST"
check "node bridge" "$s_bridge" "$NODE_BRIDGE"
check "node storage" "$s_storage" "$NODE_STORAGE"
check "relay public host" "$(pgq "select coalesce(public_host, '') from relays where id = $RELAY_ID;")" \
      "$RELAY_PUBLIC_HOST"
check "wildcard rows for $CERT_SCOPE" \
      "$(pgq "select count(*) from certificates
                where kind = 'ORIGIN_CA_WILDCARD' and domain_id is null
                  and scope = '$(sql_escape "$CERT_SCOPE")' and status = 'ACTIVE';")" "1"

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
[ "$tokened" = "t" ] || echo "  WARN relay '$RELAY_NAME' has no sync token: every relay sync
       fails closed until one is issued and installed on both sides"
[ "$CERT_DAYS_LEFT" -ge 30 ] \
  || echo "  WARN the wildcard for $ROOT_DOMAIN expires in ${CERT_DAYS_LEFT}d"

if [ "$vfail" -ne 0 ]; then
  echo "FAILED — $vfail check(s). Pre-change rows: $BK/inventory-before.sql (data-only" >&2
  echo "         inserts; a rollback deletes the rows written above and replays that file)." >&2
  exit 1
fi
echo "OK — inventory registered for $NODE. Pre-change rows kept at $BK/inventory-before.sql."
