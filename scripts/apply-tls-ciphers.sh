#!/usr/bin/env bash
# Modern TLS cipher suite on the nginx tiers that terminate browser TLS.
#
#   apply-tls-ciphers.sh [development|production]
#
#   development (default, run on pve-node): LXC 100 reverse proxy and LXC 101 app.
#   production  (run on the production host): the production proxy container,
#               which terminates TLS for every public platform name. LXC 100 on
#               pve-node only routes those names by SNI to it and never sees the
#               handshake, so its own policy file does not cover them.
#
# Idempotent: writes conf.d/pickle-tls.conf, tests, reloads, verifies.
#
# Why: nginx.conf pins `ssl_protocols TLSv1.2 TLSv1.3` (or, on the nginx.org
# build in production, each vhost does), but `ssl_ciphers` was never set, so TLS
# 1.2 negotiated whatever the OpenSSL default list allows — including CBC and
# non-PFS suites. This pins the Mozilla intermediate suites instead. TLS 1.3
# suites are not controlled by ssl_ciphers (they are always the three AEAD
# suites) and need no setting here. The file sits in the http{} context, so it
# covers every server block that does not set its own list, including the
# vhosts the proxy agent renders at runtime.
#
# In development LXC 100 also fronts the opus.pusan.ac.kr tenant, so the script
# verifies opus before and after. The opus :443 SNI is passthrough (the origin
# terminates its own TLS), so this change cannot affect the opus cipher
# negotiation; the check is there because the two share one nginx process.
set -euo pipefail

TARGET="${1:-development}"
# shellcheck source=scripts/lib/ct.sh
. "$(dirname "$0")/lib/ct.sh"
CONF=/etc/nginx/conf.d/pickle-tls.conf

# Each target names its containers, the hostname guard for each, and the
# names checked before and after. `host|address` pins a name to an address with
# --resolve; a bare host goes through ordinary resolution, which for production
# is the public entry, so the check exercises the path a browser takes.
case "$TARGET" in
  development)
    CTS=("${PICKLE_PROXY_CTID:-100}" "${PICKLE_APP_CTID:-101}")
    NAMES=(reverse-proxy pickle-app)
    # label|host|resolve-address|path|expected status|suite asserted
    # opus is passthrough to its own origin: reachability is checked because it
    # shares the nginx process, but its suites are not this tier's to assert.
    CHECKS=("pickle|pickle.pusan.ac.kr|198.18.1.10|/|200|yes"
            "opus|opus.pusan.ac.kr|198.18.1.10|/|200|no")
    ;;
  production)
    CTS=("${PICKLE_PRODUCTION_PROXY_CTID:-1200}")
    NAMES=(example-proxy-prod)
    # No --resolve: the production proxy listens only behind the PROXY-protocol
    # loopback hop, so the public name is the only way in.
    CHECKS=("pickle|pickle.pusan.ac.kr||/|200|yes"
            "llm|llm.pcl.kr||/v1/models|401|yes")
    ;;
  *)
    echo "usage: $0 [development|production]" >&2
    exit 2
    ;;
esac
for i in "${!CTS[@]}"; do
  require_ct "${CTS[$i]}" "${NAMES[$i]}"
done

ts=$(date +%Y%m%d-%H%M%S)
BK="${BK:-/srv/pickle/backup/tls-ciphers-$TARGET-$ts}"
mkdir -p "$BK"

echo "== backup current nginx state of LXC ${CTS[*]} -> $BK"
for ct in "${CTS[@]}"; do
  pct exec "$ct" -- tar czf /tmp/nginx-etc.tgz -C / etc/nginx
  pct pull "$ct" /tmp/nginx-etc.tgz "$BK/lxc$ct-nginx-etc.tgz"
  pct exec "$ct" -- rm /tmp/nginx-etc.tgz
done

# curl_args HOST ADDRESS — the --resolve pin when an address is given.
curl_args() {
  if [ -n "$2" ]; then
    printf '%s\n' --resolve "$1:443:$2"
  fi
}

# expect_http LABEL EXPECTED-CODE CURL-ARGS… — a reachability probe that is
# actually checked. Printing %{http_code} and moving on made every one of these
# a no-op: a 000 (connect failed) or a 502 scrolled past exactly like a 200.
fails=0
expect_http() {
  local label="$1" want="$2" got
  shift 2
  got=$(curl -sk -o /dev/null -w '%{http_code}' "$@") || got=000
  if [ "$got" = "$want" ]; then
    echo "  OK   $label -> $got"
  else
    echo "  FAIL $label -> ${got:-none} (expected $want)" >&2
    fails=$((fails + 1))
  fi
}

run_checks() { # $1 = pre|post
  local check label host addr path want args
  for check in "${CHECKS[@]}"; do
    IFS='|' read -r label host addr path want _ <<<"$check"
    mapfile -t args < <(curl_args "$host" "$addr")
    expect_http "$1 $label :443" "$want" "${args[@]}" "https://$host$path"
  done
}

echo "== pre-change reachability"
run_checks pre
[ "$fails" -eq 0 ] || { echo "aborting: the tiers are not healthy before the change" >&2; exit 1; }

# Mozilla intermediate, ECDHE only: the DHE suites of that profile need an
# ssl_dhparam file, which no tier has, and every client that reaches TLS 1.2
# here supports ECDHE. Order is irrelevant while ssl_prefer_server_ciphers is off.
CIPHERS='ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305'

tmp=$(mktemp)
cat > "$tmp" <<EOF
# Managed by infra/scripts/apply-tls-ciphers.sh — TLS cipher policy (http{} context).
#
# Mozilla intermediate suites, ECDHE only (no ssl_dhparam on this host, so the
# profile's DHE suites would never negotiate anyway). Everything left out of this
# list is either non-PFS or CBC.
#
# TLS 1.3 is deliberately absent: its suites are fixed by the protocol and are not
# selected through ssl_ciphers. ssl_protocols and ssl_prefer_server_ciphers stay
# where the distribution or the vhost put them.
ssl_ciphers $CIPHERS;
EOF

for ct in "${CTS[@]}"; do
  echo "== LXC $ct: install $CONF"
  pct push "$ct" "$tmp" "$CONF" --perms 644
  pct exec "$ct" -- nginx -t
  pct exec "$ct" -- systemctl reload nginx
done
rm -f "$tmp"

echo "== post-change verification"
run_checks post

echo "== negotiated TLS 1.2 suite, and a CBC suite refused"
# The whole point of the script is which suite gets negotiated, and nothing here
# used to read it — only the status code was printed, which is identical whether
# the policy took effect or not. curl reports the suite it settled on; assert it
# against the very list installed above. Then offer only a CBC suite: the
# handshake has to fail, which is the property this policy exists for. A
# positive check alone passes on a server that still accepts CBC from a client
# that offers nothing better.
for check in "${CHECKS[@]}"; do
  IFS='|' read -r label host addr path _ asserted <<<"$check"
  [ "$asserted" = yes ] || continue
  mapfile -t args < <(curl_args "$host" "$addr")
  # Same capture-then-parse shape as the CBC probe below: a failed connection
  # must reach the FAIL branch, not end the run through pipefail.
  out=$(curl -skv --tlsv1.2 --tls-max 1.2 -o /dev/null "${args[@]}" "https://$host$path" 2>&1) || true
  neg=$(printf '%s\n' "$out" | sed -n 's|^\* SSL connection using [^/]*/ *\([^ /]*\).*|\1|p' | head -1)
  if [ -z "$neg" ]; then
    echo "  FAIL $label could not determine the negotiated TLS 1.2 suite" >&2
    fails=$((fails + 1))
  elif printf '%s' ":$CIPHERS:" | grep -qF ":$neg:"; then
    echo "  OK   $label tls1.2 negotiated $neg (on the allowed list)"
  else
    echo "  FAIL $label tls1.2 negotiated $neg, which is NOT on the allowed list" >&2
    fails=$((fails + 1))
  fi
  connect="$host:443"
  [ -z "$addr" ] || connect="$addr:443"
  # s_client exits 1 when the handshake is refused, which is the outcome
  # wanted here, so its status cannot sit in a pipeline under pipefail: the
  # assignment would trip errexit exactly when the policy works. Capture first,
  # parse second. Only an explicit "(NONE)" counts as a refusal; no line at all
  # means the connection never got that far, which proves nothing.
  out=$(openssl s_client -connect "$connect" -servername "$host" -tls1_2 \
          -cipher ECDHE-ECDSA-AES128-SHA:ECDHE-RSA-AES128-SHA </dev/null 2>&1) || true
  cbc=$(printf '%s\n' "$out" | sed -n 's/^New, [^,]*, Cipher is \(.*\)$/\1/p' | head -1)
  if [ "$cbc" = "(NONE)" ]; then
    echo "  OK   $label refuses a CBC-only TLS 1.2 offer"
  elif [ -z "$cbc" ]; then
    echo "  FAIL $label CBC probe got no handshake result (connection failed?)" >&2
    fails=$((fails + 1))
  else
    echo "  FAIL $label still accepts $cbc" >&2
    fails=$((fails + 1))
  fi
done

restore_hint() {
  local ct
  for ct in "${CTS[@]}"; do
    echo "         LXC $ct: remove $CONF (or untar $BK/lxc$ct-nginx-etc.tgz over /) and reload"
  done
}

if [ "$fails" -ne 0 ]; then
  echo "FAILED — $fails check(s) did not hold. Roll back:" >&2
  restore_hint >&2
  exit 1
fi

echo "OK — rollback if ever needed:"
restore_hint
