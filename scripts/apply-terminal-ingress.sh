#!/usr/bin/env bash
# web-terminal ingress + client-IP validation on the reverse-proxy tier.
# Idempotent: writes the target nginx state on LXC 100 + LXC 101, tests, reloads,
# and verifies both pickle and the opus.pusan.ac.kr tenant afterwards.
#
# NOTE (2026-07-28 main-domain cutover): this script reproduces the
# pre-cutover state where pickle.pnuops.com serves the app — its own
# pre/post checks assert that mid-state. On a rebuild, run
# apply-main-domain-vhost.sh AFTER this one; it rewrites the pickle-dev
# vhosts to their final redirect-only form and adds pickle.pusan.ac.kr.
#
# DO NOT run this on its own against a cut-over proxy. It restores
# `default_server` on the pickle-dev TLS vhost, which then collides with the
# same flag on pickle-main-tls.conf: `nginx -t` fails and the run aborts with
# the stream router and conf.d already rewritten but never reloaded. Always
# follow it with apply-main-domain-vhost.sh in the same sitting.
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
#     - sites-available/pickle-dev-tls.conf: accepts PROXY protocol, restores
#       the peer via realip, adds `location = /terminal/ws` → bridge
#       172.30.1.30:8082 (sshgw-terminal-bridge, LXC 102), and passes the
#       validated X-Real-IP on the /-proxy to LXC 101.
#     - sites pickle-dev.conf (:80): realip from CF ranges (plain-HTTP path has
#       the true peer directly — no PP needed).
#   LXC 101
#     - /api/ vhost: X-Real-IP now forwards the LXC-100-computed value
#       ($http_x_real_ip) instead of trusting the raw CF-Connecting-IP header.
#
# NOTE for runbooks: after this change, `curl https://127.0.0.1:8443` on LXC 100
# no longer works (the 8443 socket requires a PROXY header). Verify through the
# real :443 stream path instead (see the checks at the bottom).
set -euo pipefail

RP=100   # reverse-proxy LXC
APP=101  # app LXC

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
# and the run carried on rewriting nginx. The expected values encode the
# PRE-CUTOVER state this script reproduces: pickle.pnuops.com still serves the
# app (apply-main-domain-vhost.sh, which runs after this one, is what turns it
# into a redirect), so 200 is right here and would be wrong on a cut-over proxy.
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

echo "== pre-change reachability (must both be healthy before we touch anything)"
expect_http "pre pickle :443" 200 --resolve pickle.pnuops.com:443:172.30.1.10 https://pickle.pnuops.com/
expect_http "pre opus   :443" 200 --resolve opus.pusan.ac.kr:443:172.30.1.10 https://opus.pusan.ac.kr/
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
# In pickle-dev-tls.conf the realip module restores $remote_addr to the true
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

echo "== LXC $RP: TLS tier vhost (PP listen + /terminal/ws branch + validated X-Real-IP)"
pct exec "$RP" -- bash -c 'cat > /etc/nginx/sites-available/pickle-dev-tls.conf' <<'EOF'
server {
    listen 127.0.0.1:8443 ssl proxy_protocol default_server;
    http2 on;

    server_name pickle.pnuops.com;

    ssl_certificate     /etc/nginx/pickle-certs/origin.crt;
    ssl_certificate_key /etc/nginx/pickle-certs/origin.key;

    # the stream tier (the only peer of this loopback socket) prepends a
    # PROXY header carrying the true public :443 peer; restore it into
    # $remote_addr. $pickle_client_ip (conf.d/pickle-terminal.conf) then
    # decides whether CF-Connecting-IP may be believed.
    set_real_ip_from 127.0.0.1;
    real_ip_header proxy_protocol;

    # web terminal — bridge-owned path, deliberately OUTSIDE /api/v1
    # (the openapi contract surface stays 1:1 with pickle-api). Terminated by
    # sshgw-terminal-bridge on LXC 102.
    location = /terminal/ws {
        proxy_pass http://172.30.1.30:8082;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Origin $http_origin;
        proxy_set_header X-Real-IP $pickle_client_ip;
        proxy_connect_timeout 10s;
        # long-lived WS: liveness is the bridge's job (30s pings, 15min idle,
        # 60s revalidation) — nginx just needs to not cut the stream.
        proxy_send_timeout 1h;
        proxy_read_timeout 1h;
        proxy_buffering off;
    }

    location / {
        proxy_pass http://172.30.1.20:80;
        # snippets/proxy-common.conf, except X-Real-IP carries the validated
        # client IP instead of the raw (CF-edge) peer address.
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $pickle_client_ip;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_connect_timeout 10s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
EOF

echo "== LXC $RP: :80 vhost — realip from CF ranges (plain-HTTP path sees the peer directly)"
pct exec "$RP" -- bash -c 'cat > /etc/nginx/sites-available/pickle-dev.conf' <<'EOF'
server {
    listen 80;
    listen [::]:80;

    server_name pickle.pnuops.com;

    # on :80 the TCP peer is direct (no stream hop). Restore the client IP
    # from CF-Connecting-IP only for genuine Cloudflare peers; any other peer
    # keeps its own address in $remote_addr (proxy-common passes it upstream).
    include /etc/nginx/pickle-realip.conf;
    real_ip_header CF-Connecting-IP;

    location / {
        proxy_pass http://172.30.1.20:80;
        include /etc/nginx/snippets/proxy-common.conf;
    }
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
expect_http "post pickle :443" 200 --resolve pickle.pnuops.com:443:172.30.1.10 https://pickle.pnuops.com/
expect_http "post pickle /api :443" 200 --resolve pickle.pnuops.com:443:172.30.1.10 https://pickle.pnuops.com/api/v1/meta/status
expect_http "post opus   :443" 200 --resolve opus.pusan.ac.kr:443:172.30.1.10 https://opus.pusan.ac.kr/
# The terminal path 502s until the bridge is deployed — 502 is the
# expected value here; anything else means the branch is miswired.
expect_http "post /terminal/ws (502 expected pre-bridge)" 502 --resolve pickle.pnuops.com:443:172.30.1.10 https://pickle.pnuops.com/terminal/ws

if [ "$fails" -ne 0 ]; then
  echo "FAILED — $fails check(s) did not hold; the new nginx state is live but unverified." >&2
  echo "         Roll back: untar $BK/lxc100-nginx-etc.tgz / $BK/lxc101-nginx-etc.tgz over /etc/nginx and reload." >&2
  exit 1
fi

echo "OK — rollback if ever needed: untar $BK/lxc100-nginx-etc.tgz / lxc101-nginx-etc.tgz over /etc/nginx and reload."
