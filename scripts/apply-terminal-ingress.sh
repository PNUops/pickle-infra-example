#!/usr/bin/env bash
# web-terminal ingress + client-IP validation on the reverse-proxy tier.
# Idempotent: writes the target nginx state on LXC 100 + LXC 101, tests, reloads,
# and verifies both pickle and the opus.pusan.ac.kr tenant afterwards.
#
# SCOPE: the ingress plumbing only — the stream router, the client-IP
# validation map, and the app tier's X-Real-IP source. It no longer writes any
# app vhost. It used to also reproduce the club domain's app vhosts, but that
# domain and its certificate are gone; re-creating a vhost that references a
# deleted certificate would fail `nginx -t` mid-rebuild, before the script that
# installs the real vhosts ever runs.
#
# On a rebuild the app vhosts come from apply-main-domain-vhost.sh, which runs
# after this one and owns their final state.
#
# What it changes:
#   LXC 100
#     - stream :443 SNI router now sends PROXY protocol to its LOCAL backends so
#       the TLS tier learns the true :443 TCP peer. The opus passthrough
#       gets a PP-stripping hop (127.0.0.1:8441) so the external opus origin
#       keeps receiving a plain TLS stream — behaviour unchanged for opus.
#     - conf.d/pickle-terminal.conf: geo+map computing $pickle_client_ip —
#       CF-Connecting-IP is trusted ONLY when the :443 peer is a Cloudflare
#       edge (pickle-cf-geo.conf); any other peer is itself the client.
#   LXC 101
#     - /api/ vhost: X-Real-IP now forwards the LXC-100-computed value
#       ($http_x_real_ip) instead of trusting the raw CF-Connecting-IP header.
#
# NOTE for runbooks: after this change, `curl https://127.0.0.1:8443` on LXC 100
# no longer works (the 8443 socket requires a PROXY header). Verify through the
# real :443 stream path instead (see the checks at the bottom).
set -euo pipefail

RP="${PICKLE_PROXY_CTID:-100}"   # reverse-proxy LXC
APP="${PICKLE_APP_CTID:-101}"  # app LXC

ts=$(date +%Y%m%d-%H%M%S)
BK="/root/pickle/backup/terminal-ingress-$ts"
mkdir -p "$BK"

echo "== backup current nginx state of LXC $RP and LXC $APP -> $BK"
pct exec "$RP"  -- tar czf /tmp/nginx-etc.tgz -C / etc/nginx
pct pull "$RP"  /tmp/nginx-etc.tgz "$BK/lxc100-nginx-etc.tgz"
pct exec "$RP"  -- rm /tmp/nginx-etc.tgz
pct exec "$APP" -- tar czf /tmp/nginx-etc.tgz -C / etc/nginx
pct pull "$APP" /tmp/nginx-etc.tgz "$BK/lxc101-nginx-etc.tgz"
pct exec "$APP" -- rm /tmp/nginx-etc.tgz

# expect_http LABEL EXPECTED-CODE CURL-ARGS… — every probe below used to print
# %{http_code} and continue, so a 000 or a 502 read exactly like a healthy 200
# and the run carried on rewriting nginx.
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

echo "== pre-change reachability (must be healthy before we touch anything)"
# Only the passthrough tenant is asserted: on a rebuild this script runs before
# any app vhost exists, so requiring the platform to answer here would make the
# ingress plumbing unappliable exactly when it is needed.
expect_http "pre opus   :443" 200 --resolve opus.pusan.ac.kr:443:198.18.1.10 https://opus.pusan.ac.kr/
[ "$fails" -eq 0 ] || { echo "aborting before any change: the tiers are not healthy" >&2; exit 1; }

echo "== LXC $RP: pickle-cf-geo.conf (geo-format CF ranges, derived from pickle-realip.conf)"
# shellcheck disable=SC2016  # nginx-side $ must not expand here
pct exec "$RP" -- bash -c '
set -euo pipefail
{
  echo "# Cloudflare edge ranges in geo{} form — generated from pickle-realip.conf"
  echo "# ($(grep -m1 Generated /etc/nginx/pickle-realip.conf | sed "s/^# *//"))"
  grep -o "set_real_ip_from [^;]*" /etc/nginx/pickle-realip.conf | awk "{print \$2\" 1;\"}"
} > /etc/nginx/pickle-cf-geo.conf'

echo "== LXC $RP: conf.d/pickle-terminal.conf (client-IP validation map)"
pct exec "$RP" -- bash -c 'cat > /etc/nginx/conf.d/pickle-terminal.conf' <<'EOF'
# web terminal + client-IP validation (http{} context, LXC 100).
#
# In the TLS-tier vhosts the realip module restores $remote_addr to the true
# public :443 TCP peer (carried in by the stream tier via PROXY protocol).
# CF-Connecting-IP is only meaningful when that peer is a Cloudflare edge —
# from any other peer the header is attacker-controlled and the peer address
# itself is the client.
geo $pickle_peer_is_cf {
    default 0;
    include /etc/nginx/pickle-cf-geo.conf;
}

map "$pickle_peer_is_cf:$http_cf_connecting_ip" $pickle_client_ip {
    default $remote_addr;
    "~^1:(?<ip>.+)$" $ip;
}
EOF

echo "== LXC $RP: stream SNI router with PROXY protocol + opus strip hop"
pct exec "$RP" -- bash -c 'cat > /etc/nginx/stream-conf.d/opus-sni.conf' <<'EOF'
map $ssl_preread_server_name $tls_backend {
    opus.pusan.ac.kr 127.0.0.1:8441;
    default 127.0.0.1:8443;
}

server {
    listen 443;
    listen [::]:443;

    ssl_preread on;
    proxy_pass $tls_backend;
    # hand the true :443 peer to the LOCAL tiers. Both local backends
    # (8443 TLS tier, 8441 opus hop) expect the header.
    proxy_protocol on;

    proxy_connect_timeout 10s;
    proxy_timeout 1h;
}

# opus.pusan.ac.rk passthrough: strip the PROXY header again — the external
# opus origin must keep receiving a plain TLS stream (behaviour identical to
# the earlier direct passthrough; opus never saw the client IP either way).
server {
    listen 127.0.0.1:8441 proxy_protocol;
    proxy_pass 203.0.113.20:443;

    proxy_connect_timeout 10s;
    proxy_timeout 1h;
}
EOF

echo "== LXC $RP: nginx -t + reload"
pct exec "$RP" -- nginx -t
pct exec "$RP" -- systemctl reload nginx

echo "== LXC $APP: /api/ vhost forwards the LXC-100-validated X-Real-IP"
# shellcheck disable=SC2016  # nginx-side $ must not expand here
# sed targets sites-available (the real file): sed -i on the sites-enabled
# symlink would replace the link with a detached copy, silently orphaning
# every later edit made on the sites-available side.
pct exec "$APP" -- sed -i 's|proxy_set_header X-Real-IP \$http_cf_connecting_ip;|proxy_set_header X-Real-IP $http_x_real_ip;|' /etc/nginx/sites-available/pickle.conf
pct exec "$APP" -- nginx -t
pct exec "$APP" -- systemctl reload nginx

echo "== post-change verification"
# The tenant passthrough is the invariant this script must never break. The
# platform's own paths are verified by apply-main-domain-vhost.sh, which owns the
# vhosts they live on and runs after this.
expect_http "post opus   :443" 200 --resolve opus.pusan.ac.kr:443:198.18.1.10 https://opus.pusan.ac.kr/

if [ "$fails" -ne 0 ]; then
  echo "FAILED — $fails check(s) did not hold; the new nginx state is live but unverified." >&2
  echo "         Roll back: untar $BK/lxc100-nginx-etc.tgz / $BK/lxc101-nginx-etc.tgz over /etc/nginx and reload." >&2
  exit 1
fi

echo "OK — rollback if ever needed: untar $BK/lxc100-nginx-etc.tgz / lxc101-nginx-etc.tgz over /etc/nginx and reload."
