#!/usr/bin/env bash
# Main-entry domain on the reverse-proxy tier: pickle.pusan.ac.kr (Let's
# Encrypt, campus zone, direct — no CDN in path), with the retired
# pickle.pnuops.com entry reduced to a 301 redirect.
#
# Idempotent: owns the FINAL state of the reverse-proxy ingress — the four
# main-entry vhost files, the edge capacity and rate-limit configuration, and
# the default server for unknown names. Run it LAST when rebuilding the proxy
# LXC: it intentionally overwrites the pickle-dev app vhosts that
# apply-terminal-ingress.sh writes (that script reproduces the pre-cutover
# mid-state its own checks expect).
#
# Everything lands in one `nginx -t` and one reload because parts of it must
# change together: the socket may name exactly one default server, so handing
# that role from the main vhost to the reject vhost is not divisible, and a
# vhost may not reference a limit zone before the zone exists.
#
# What it changes (LXC 100):
#   - installs certbot (Debian package; systemd timer handles auto-renew) and
#     the renewal deploy-hook that reloads nginx after a renewal. The hook is
#     byte-identical to the one pickle-proxy-agent scripts/deploy.sh installs,
#     so either script may write it, in any order.
#   - sites-available/pickle-main.conf      (:80  — ACME webroot + 301 to HTTPS)
#   - sites-available/pickle-main-tls.conf  (8443 — LE cert, /terminal/ws + /
#     and the rate-limited credential paths; mirror of the retired
#     pickle-dev-tls.conf app vhost with the LE pair instead of the CF Origin
#     CA pair)
#   - raises the connection ceiling. One core means one nginx worker, so the
#     stock `worker_connections 768` was the limit for the entire public entry;
#     a request holds several slots across the stream router, the TLS tier and
#     the upstream, putting real concurrency under two hundred — low enough for
#     idle connections alone to saturate the ingress.
#   - conf.d/pickle-ratelimit.conf: limit_req/limit_conn zones and the TLS
#     session cache. Nothing fronts the main entry any more, and neither tier
#     had a single limit or session cache. Keys are built so the platform's own
#     probes are never counted (limit_req ignores an empty key), values are
#     deliberately loose because the real client-address distribution is
#     unknown until launch, and a fixed-key zone caps total password-hashing
#     work that no per-address rule can bound.
#   - sites-available/pickle-reject.conf: the default server for unknown names.
#     While the main vhost held that role, any name pointed here was answered
#     with a publicly trusted certificate and the whole console.
#   - issues the LE certificate (HTTP-01 via the webroot) if it is not
#     already present; requires LE_EMAIL for first-time account registration.
#   - sites-available/pickle-dev.conf / pickle-dev-tls.conf — redirect-only
#     (301 to the new domain; the TLS side keeps the CF Origin CA pair so
#     HSTS-pinned clients arriving through the CDN get a clean redirect).
#
# NOT handled here (host-level, see the network runbook): the pve-node /etc/hosts
# hairpin entry `198.18.1.10 pickle.pusan.ac.kr` — campus NAT has no hairpin,
# so on-host smoke/e2e need it to reach the public name.
set -euo pipefail

RP=100  # reverse-proxy LXC
DOMAIN=pickle.pusan.ac.kr
OLD_DOMAIN=pickle.pnuops.com
HOST_PROBE_IP=198.18.0.1   # the Proxmox host on vmbr1: smoke suite + health snapshot

ts=$(date +%Y%m%d-%H%M%S)
BK="/root/pickle/backup/main-domain-vhost-$ts"
mkdir -p "$BK"

# Unpacking the archive restores files but never removes the sites-enabled links
# this script adds, and the restored vhost carries default_server too — nginx -t
# would refuse the collision and the rollback itself would stall. Drop the links
# first.
ROLLBACK="on LXC $RP run 'rm -f /etc/nginx/sites-enabled/pickle-main*.conf /etc/nginx/sites-enabled/pickle-reject.conf' FIRST, then untar $BK/lxc100-nginx-etc.tgz over /etc/nginx and reload."

echo "== backup current nginx state of LXC $RP -> $BK"
pct exec "$RP" -- tar czf /tmp/nginx-etc.tgz -C / etc/nginx
pct pull "$RP" /tmp/nginx-etc.tgz "$BK/lxc100-nginx-etc.tgz"
pct exec "$RP" -- rm /tmp/nginx-etc.tgz

echo "== pre-change reachability (stream tier must be healthy before we touch anything)"
# Assert, so that a tenant that was already broken cannot be mistaken for
# collateral damage from this run.
pre_opus=$(curl -sk -o /dev/null -w '%{http_code}' --resolve opus.pusan.ac.kr:443:198.18.1.10 https://opus.pusan.ac.kr/ || true)
if [ "$pre_opus" = 200 ]; then
  echo "  OK   pre opus :443 -> 200"
else
  echo "  FAIL pre opus :443 -> $pre_opus (expected 200) — fix the tenant before changing this tier"
  exit 1
fi

echo "== LXC $RP: certbot (package brings the auto-renew systemd timer)"
pct exec "$RP" -- bash -c 'command -v certbot >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot; }; certbot --version'
pct exec "$RP" -- install -d /var/www/certbot

echo "== LXC $RP: certbot renewal deploy-hook (nginx reload after successful renew)"
# shellcheck disable=SC2016
pct exec "$RP" -- sh -c "install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy \
  && printf '%s\n' \
  '#!/bin/sh' \
  '# Installed by pickle-proxy-agent scripts/deploy.sh. Certbot runs deploy-hooks' \
  '# only after a successful renewal; reload nginx so the renewed certificate is' \
  '# served immediately instead of waiting for the next apply/sync reload.' \
  'systemctl reload nginx' \
  > /etc/letsencrypt/renewal-hooks/deploy/pickle-nginx-reload.sh \
  && chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/pickle-nginx-reload.sh"

echo "== LXC $RP: bootstrap guard — the HTTPS vhost cannot load without its certificate"
# On a rebuilt container the restored nginx tree still has the HTTPS vhosts
# enabled, but /etc/letsencrypt is deliberately not part of any nginx backup.
# nginx -t then fails on the missing certificate, which blocks the very :80
# vhost the ACME challenge needs in order to issue it: a bootstrap deadlock.
# Only disable the link when that is genuinely what is wrong — on a healthy
# host both conditions are false and nothing is touched.
bootstrap_disabled=0
if ! pct exec "$RP" -- test -e "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" \
   && ! pct exec "$RP" -- nginx -t >/dev/null 2>&1; then
  pct exec "$RP" -- rm -f /etc/nginx/sites-enabled/pickle-main-tls.conf
  bootstrap_disabled=1
  echo "   certificate absent and nginx -t fails on it — link disabled until issuance"
fi

echo "== LXC $RP: :80 vhost for $DOMAIN (ACME webroot + 301) — needed BEFORE issuance"
pct exec "$RP" -- bash -c 'cat > /etc/nginx/sites-available/pickle-main.conf' <<EOF
# $DOMAIN — main entry (campus zone, direct: no CDN in path).
# :80 serves only the ACME webroot for certbot (issuance + auto-renew) and
# redirects everything else to HTTPS. The TCP peer here is the real client
# (no realip rewrite needed, unlike the CF-era vhosts).
server {
    listen 80;
    listen [::]:80;

    server_name $DOMAIN;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}
EOF
pct exec "$RP" -- ln -sf /etc/nginx/sites-available/pickle-main.conf /etc/nginx/sites-enabled/pickle-main.conf
pct exec "$RP" -- nginx -t
pct exec "$RP" -- systemctl reload nginx

echo "== LXC $RP: LE certificate for $DOMAIN (skipped when already present)"
if ! pct exec "$RP" -- test -e "/etc/letsencrypt/live/$DOMAIN/fullchain.pem"; then
  # First issuance registers the ACME account; the expiry-notice address is
  # deliberately not committed here.
  : "${LE_EMAIL:?first-time issuance: export LE_EMAIL=<account email for expiry notices>}"
  if ! pct exec "$RP" -- certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" \
       --email "$LE_EMAIL" --agree-tos --no-eff-email --non-interactive; then
    echo "ISSUANCE FAILED — the main domain has no HTTPS vhost until this succeeds."
    if [ "$bootstrap_disabled" = 1 ]; then
      echo "  Its vhost link was disabled by the bootstrap guard above and stays that way;"
      echo "  re-run this script once the cause is fixed (usually DNS, the :80 DNAT, or"
      echo "  reachability from the ACME servers). Do NOT re-link it by hand: nginx would"
      echo "  fail to start on the missing certificate."
    fi
    exit 1
  fi
else
  echo "   certificate already present — leaving issuance to the certbot timer"
fi

echo "== LXC $RP: raise the connection ceiling"
# events{} lives in the distribution nginx.conf, so this is an in-place edit
# guarded to stay idempotent. A few KB per connection against 512 MB of
# container memory leaves ample headroom at this size.
# shellcheck disable=SC2016  # runs in the container's shell, not this one
pct exec "$RP" -- bash -c '
set -euo pipefail
conf=/etc/nginx/nginx.conf
grep -q "worker_connections 4096;" "$conf" \
  || sed -i "s/^\tworker_connections 768;$/\tworker_connections 4096;/" "$conf"
grep -q "^worker_rlimit_nofile" "$conf" \
  || sed -i "/^worker_processes auto;$/a worker_rlimit_nofile 16384;" "$conf"
grep -q "worker_connections 4096;" "$conf" && grep -q "^worker_rlimit_nofile 16384;" "$conf"'
echo "  OK   worker_connections 4096, worker_rlimit_nofile 16384"

echo "== LXC $RP: rate-limit zones + TLS session cache (http context)"
pct exec "$RP" -- bash -c 'cat > /etc/nginx/conf.d/pickle-ratelimit.conf' <<EOF
# Edge rate limiting and TLS session reuse. Written by
# infra/scripts/apply-main-domain-vhost.sh — reasoning lives in that header.

# The platform's own callers must never be throttled: the smoke suite and the
# health snapshot run on the Proxmox host and arrive from its bridge address.
# A request whose limit key is empty is not counted at all, which is how the
# exemption is expressed.
geo \$pickle_rl_exempt {
    default            0;
    $HOST_PROBE_IP     1;
    127.0.0.1          1;
}

# Direct-path clients are their own address. The CDN-fronted vhosts have their
# own validated client variable and are not covered here.
map \$pickle_rl_exempt \$pickle_rl_key {
    1 "";
    0 \$binary_remote_addr;
}

# One fixed key, so this zone caps the TOTAL rate on the credential paths no
# matter how many addresses or accounts a flood is spread across. Per-address
# rules cannot bound password-hashing work; this can.
map \$pickle_rl_exempt \$pickle_rl_auth_total {
    1 "";
    0 "auth";
}

limit_req_zone \$pickle_rl_key        zone=pickle_auth:10m     rate=10r/s;
limit_req_zone \$pickle_rl_key        zone=pickle_api:10m      rate=50r/s;
limit_req_zone \$pickle_rl_auth_total zone=pickle_auth_all:1m  rate=20r/s;
limit_conn_zone \$pickle_rl_key       zone=pickle_perip:10m;

limit_req_status 429;
limit_conn_status 429;
limit_req_log_level error;
limit_conn_log_level error;

# The direct path terminates TLS here, so handshake cost is ours now. Tickets
# stay off: one node makes the shared cache sufficient, and it avoids the
# forward-secrecy tradeoff a long-lived ticket key carries.
ssl_session_cache shared:PICKLE_SSL:10m;
ssl_session_timeout 8h;
ssl_session_tickets off;
EOF
echo "  OK   conf.d/pickle-ratelimit.conf"

echo "== LXC $RP: connection cap at the stream tier"
# The http-context limits above cannot help here: a client that opens sockets
# and never negotiates TLS is already holding worker slots by the time any
# vhost is chosen. This bounds that before termination. The loopback hop and
# the platform's own probes are exempt by the same empty-key trick used above.
pct exec "$RP" -- bash -c 'cat > /etc/nginx/stream-conf.d/pickle-stream-limits.conf' <<EOF
# Per-address connection cap on the public :443 socket. Written by
# infra/scripts/apply-main-domain-vhost.sh.
geo \$pickle_stream_exempt {
    default        0;
    127.0.0.1      1;
    $HOST_PROBE_IP 1;
}

map \$pickle_stream_exempt \$pickle_stream_key {
    1 "";
    0 \$binary_remote_addr;
}

limit_conn_zone \$pickle_stream_key zone=pickle_stream_perip:10m;
# Generous enough that a shared campus address stays comfortable (browsers open
# a handful of sockets per origin) while still bounding what one source can
# hold against the worker ceiling.
limit_conn pickle_stream_perip 500;
limit_conn_log_level error;
EOF
echo "  OK   stream-conf.d/pickle-stream-limits.conf"

echo "== LXC $RP: default server for unknown SNI/Host"
pct exec "$RP" -- bash -c 'cat > /etc/nginx/sites-available/pickle-reject.conf' <<'EOF'
# Unknown SNI / unknown Host on the local TLS tier.
#
# While the main vhost held default_server, any name pointed at this address was
# answered with a publicly trusted certificate and the full console: free
# fingerprinting, and a third-party name could be made to carry our HSTS policy.
# An unknown SNI now fails the handshake; a known SNI with an unmatched Host
# reaches this block and gets nothing back.
#
# Names that are meant to work are unaffected — each has its own server_name,
# the tenant passthrough never reaches this socket (the stream router sends it
# straight out), and a published subdomain whose vhost is not rendered yet was
# already presenting a certificate-name mismatch rather than a working site.
server {
    listen 127.0.0.1:8443 ssl proxy_protocol default_server;
    http2 on;

    ssl_reject_handshake on;
    return 444;
}
EOF
pct exec "$RP" -- ln -sf /etc/nginx/sites-available/pickle-reject.conf /etc/nginx/sites-enabled/pickle-reject.conf
echo "  OK   sites-available/pickle-reject.conf"

echo "== LXC $RP: 8443 TLS vhost for $DOMAIN (LE pair, rate-limited)"
pct exec "$RP" -- bash -c 'cat > /etc/nginx/sites-available/pickle-main-tls.conf' <<EOF
# $DOMAIN — main entry TLS vhost (Let's Encrypt, publicly trusted;
# this domain resolves straight to the campus IP, no CDN in front).
# Selected by SNI on the shared 127.0.0.1:8443 tier behind the :443 stream
# router. Unknown names go to pickle-reject.conf, which holds default_server.
server {
    listen 127.0.0.1:8443 ssl proxy_protocol;
    http2 on;

    server_name $DOMAIN;

    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;

    # The stream tier (the only peer of this loopback socket) prepends a
    # PROXY header carrying the true public :443 peer; restore it into
    # \$remote_addr, which for this domain IS the client: it resolves straight
    # to us with no CDN in between.
    #
    # Deliberately NOT \$pickle_client_ip here. That map believes a CDN's
    # client-IP header whenever the peer falls in the CDN's ranges, which was
    # sound while the CDN was the only way in. On this direct path anyone able
    # to originate from those ranges could forge the audited client address and
    # the value downstream rate limiting keys on, so the peer address stands on
    # its own. The CDN-fronted vhosts keep using the map.
    set_real_ip_from 127.0.0.1;
    real_ip_header proxy_protocol;

    # Web terminal — bridge-owned path, deliberately OUTSIDE /api/v1
    # (the openapi contract surface stays 1:1 with pickle-api). Terminated by
    # sshgw-terminal-bridge on LXC 102.
    location = /terminal/ws {
        proxy_pass http://198.18.1.30:8082;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Origin \$http_origin;
        proxy_set_header X-Real-IP \$remote_addr;
        # The bridge authenticates with a one-time ticket and must never see a
        # session credential: it is the lower-privilege component in this split
        # precisely so that a compromise there cannot yield one. Session cookies
        # are scoped to the root path, so without this they would ride along.
        proxy_set_header Cookie "";
        proxy_connect_timeout 10s;
        # long-lived WS: liveness is the bridge's job (30s pings, 15min idle,
        # 60s revalidation) — nginx just needs to not cut the stream.
        proxy_send_timeout 1h;
        proxy_read_timeout 1h;
        proxy_buffering off;
    }

    # Credential paths carry the per-address zone plus the total-volume cap, so
    # a flood spread across many accounts still cannot monopolise password
    # hashing. nodelay keeps an ordinary burst (a page opening several requests
    # at once) from being queued behind the rate.
    location ^~ /api/v1/auth/ {
        limit_req zone=pickle_auth burst=40 nodelay;
        limit_req zone=pickle_auth_all burst=100 nodelay;
        limit_conn pickle_perip 100;
        proxy_pass http://198.18.1.20:80;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 10s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    location / {
        limit_req zone=pickle_api burst=200 nodelay;
        limit_conn pickle_perip 100;
        proxy_pass http://198.18.1.20:80;
        # snippets/proxy-common.conf, except X-Real-IP carries the validated
        # client IP instead of the raw peer address.
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_connect_timeout 10s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
EOF
pct exec "$RP" -- ln -sf /etc/nginx/sites-available/pickle-main-tls.conf /etc/nginx/sites-enabled/pickle-main-tls.conf

echo "== LXC $RP: retired $OLD_DOMAIN entry -> redirect-only (default_server moves to $DOMAIN)"
pct exec "$RP" -- bash -c 'cat > /etc/nginx/sites-available/pickle-dev.conf' <<EOF
# $OLD_DOMAIN — retired main entry (temporary club domain, replaced by
# $DOMAIN on 2026-07-28). Redirect-only: the app is no longer
# served here. Kept because browsers hold a long HSTS pin for this name and
# the CDN-proxied DNS record still points at us; user-facing subdomains
# (*.$OLD_DOMAIN) are separate vhosts and unaffected.
server {
    listen 80;
    listen [::]:80;

    server_name $OLD_DOMAIN;

    return 301 https://$DOMAIN\$request_uri;
}
EOF
pct exec "$RP" -- bash -c 'cat > /etc/nginx/sites-available/pickle-dev-tls.conf' <<EOF
# $OLD_DOMAIN — retired main entry, TLS side (see pickle-dev.conf).
# Still terminates TLS with the CF Origin CA pair (the CDN edge is the only
# path that resolves here) purely to serve the redirect without cert errors
# for HSTS-pinned clients.
server {
    listen 127.0.0.1:8443 ssl proxy_protocol;
    http2 on;

    server_name $OLD_DOMAIN;

    ssl_certificate     /etc/nginx/pickle-certs/origin.crt;
    ssl_certificate_key /etc/nginx/pickle-certs/origin.key;

    return 301 https://$DOMAIN\$request_uri;
}
EOF

echo "== LXC $RP: nginx -t + reload (atomic: default_server handover + redirects)"
pct exec "$RP" -- nginx -t
pct exec "$RP" -- systemctl reload nginx

# A graceful reload hands the listening socket over asynchronously, so a probe
# fired immediately after it can still be served by a worker running the old
# configuration — that produced a spurious failure here once. Give the handover
# a moment; a genuinely broken configuration still fails the checks below.
sleep 3

echo "== post-change verification"
# Assert, do not merely print: a bare `curl -w '%{http_code}'` reports 301/404/502
# just as happily as 200 and the run still ends in "OK". Failures are collected so
# every check runs (a `set -e` abort in the middle would skip the opus check, which
# is the one covering the pre-existing tenant).
vfail=0
check() { # check <label> <expected-code> <curl args...>
  local label="$1" want="$2"; shift 2
  # curl already prints 000 on a connection/TLS failure, so swallow its exit
  # status rather than appending a second code to the captured output.
  local got; got=$(curl -s -o /dev/null -w '%{http_code}' "$@" || true)
  if [ "$got" = "$want" ]; then echo "  OK   $label -> $got"
  else echo "  FAIL $label -> $got (expected $want)"; vfail=$((vfail + 1)); fi
}
# No -k on the new domain: the publicly trusted chain is part of what we verify.
check "new  :443 (trusted chain)" 200 --resolve "$DOMAIN:443:198.18.1.10" "https://$DOMAIN/"
check "new  /api" 200 --resolve "$DOMAIN:443:198.18.1.10" "https://$DOMAIN/api/v1/meta/status"
# 403 = the bridge is up and rejecting a ticketless/wrong-origin plain GET —
# anything else means the branch is miswired (502 would mean no bridge).
check "new  /terminal/ws (bridge reachable)" 403 --resolve "$DOMAIN:443:198.18.1.10" "https://$DOMAIN/terminal/ws"
check "new  :80 (redirect to https)" 301 --resolve "$DOMAIN:80:198.18.1.10" "http://$DOMAIN/"
# The retired entry keeps the CDN origin cert, which is not publicly trusted: -k.
check "old  :443 (redirect)" 301 -k --resolve "$OLD_DOMAIN:443:198.18.1.10" "https://$OLD_DOMAIN/"
check "old  :80  (redirect)" 301 --resolve "$OLD_DOMAIN:80:198.18.1.10" "http://$OLD_DOMAIN/"
check "opus :443 (pre-existing tenant)" 200 -k --resolve opus.pusan.ac.kr:443:198.18.1.10 https://opus.pusan.ac.kr/
# 405 proves the rate-limited credential location is wired and reaching the app
# (login is POST-only); a 404 would mean the prefix match never took effect.
check "new  /api/v1/auth (limited location live)" 405 --resolve "$DOMAIN:443:198.18.1.10" "https://$DOMAIN/api/v1/auth/login"
# An unknown name must no longer complete a handshake at all.
if curl -sk -o /dev/null --max-time 10 --resolve "unknown.invalid:443:198.18.1.10" https://unknown.invalid/ 2>/dev/null; then
  echo "  FAIL unknown SNI still answered"; vfail=$((vfail + 1))
else
  echo "  OK   unknown SNI refused"
fi
# The exemption is the reason the smoke suite and the health snapshot keep
# working, so prove it rather than assume it: a burst well past the zone rate
# must not produce a single 429 from the probe address.
burst_429=0
for _ in $(seq 1 60); do
  c=$(curl -s -o /dev/null -w '%{http_code}' --resolve "$DOMAIN:443:198.18.1.10" "https://$DOMAIN/api/v1/meta/status" || true)
  [ "$c" = 429 ] && burst_429=$((burst_429 + 1))
done
if [ "$burst_429" -eq 0 ]; then
  echo "  OK   60-request burst from the probe address drew no 429"
else
  echo "  FAIL probe address was throttled ($burst_429 of 60)"; vfail=$((vfail + 1))
fi

if [ "$vfail" -ne 0 ]; then
  echo "FAILED — $vfail check(s) did not match. $ROLLBACK"
  exit 1
fi
echo "OK — rollback if ever needed: $ROLLBACK"
