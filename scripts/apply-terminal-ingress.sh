#!/usr/bin/env bash
# web-terminal ingress on the reverse-proxy tier, and the app tier's client-IP
# source. Idempotent: writes the target nginx state on LXC 100 + LXC 101,
# tests, reloads, and verifies both pickle and the opus.pusan.ac.kr tenant
# afterwards.
#
# SCOPE: the ingress plumbing only, the stream router and the app tier's
# X-Real-IP source. It writes no app vhost: on a rebuild those come from
# apply-main-domain-vhost.sh, which runs after this one and owns their final
# state, and re-creating a vhost here that references a certificate not yet
# installed would fail `nginx -t` mid-rebuild.
#
# The direct path. Every public name resolves straight to this host; nothing
# sits in front of the :443 socket. The stream tier owns that socket, routes
# on SNI, and prepends a PROXY protocol header carrying the true TCP peer to
# the local TLS tier, which restores it into $remote_addr with the realip
# module (set_real_ip_from 127.0.0.1; real_ip_header proxy_protocol). That
# peer IS the client, so no request header is trusted for the client address:
# the vhosts forward $remote_addr as X-Real-IP, and the app tier forwards what
# the TLS tier sent. The CDN-era client-IP map ($pickle_client_ip, built from
# a geo{} of the CDN's edge ranges) trusted a client-IP header from those
# ranges; with no CDN in the path that is a forgery surface and nothing else,
# so this script removes it and refuses to while anything still references it.
#
# What it changes:
#   LXC 100
#     - stream :443 SNI router sends PROXY protocol to its LOCAL backends so
#       the TLS tier learns the true :443 TCP peer. The opus passthrough
#       gets a PP-stripping hop (127.0.0.1:8441) so the external opus origin
#       keeps receiving a plain TLS stream — behaviour unchanged for opus.
#     - removes conf.d/pickle-terminal.conf (the geo+map), pickle-cf-geo.conf
#       and pickle-realip.conf (the CDN range lists), once no vhost references
#       $pickle_client_ip any more. Rendered vhosts stop referencing it when
#       the proxy agent is redeployed and an admin resync rewrites them; until
#       then this script refuses, because removing the map first would break
#       `nginx -t` for every rendered vhost.
#   LXC 101
#     - /api/ vhost: X-Real-IP forwards the value the TLS tier sent
#       ($http_x_real_ip); LXC 100 is that vhost's only client.
#
# NOTE for runbooks: `curl https://127.0.0.1:8443` on LXC 100 does not work
# (the 8443 socket requires a PROXY header). Verify through the real :443
# stream path instead (see the checks at the bottom).
set -euo pipefail

RP="${PICKLE_PROXY_CTID:-100}"   # reverse-proxy LXC
APP="${PICKLE_APP_CTID:-101}"  # app LXC
# shellcheck source=scripts/lib/ct.sh
. "$(dirname "$0")/lib/ct.sh"
require_ct "$RP" reverse-proxy
require_ct "$APP" pickle-app

ts=$(date +%Y%m%d-%H%M%S)
BK="/srv/pickle/backup/terminal-ingress-$ts"
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

echo "== LXC $RP: retire the CDN client-IP map and range lists"
# Order matters: the map is defined in conf.d and referenced from the rendered
# vhosts in pickle.d, so deleting the definition while a reference remains
# makes `nginx -t` fail and the reload below would never happen. The
# references leave when the proxy agent is redeployed and an admin resync
# rewrites every vhost; that has to come first, and this script checks rather
# than assumes it. Its own definition file is excluded from the search, since
# that is the file about to be removed.
# shellcheck disable=SC2016  # nginx-side $ must not expand here
pct exec "$RP" -- bash -c '
set -euo pipefail
refs=$(grep -rl "pickle_client_ip" /etc/nginx --exclude=pickle-terminal.conf 2>/dev/null || true)
# The two files below are also reachable by path, not only through the
# variable: anything that `include`s them keeps working until they are gone and
# then fails `nginx -t` for the whole configuration. A hand-written vhost or a
# fragment restored from an archive can carry such an include even when no
# rendered vhost does, so both shapes are checked before anything is deleted.
# The three files this script removes are excluded, since one of them includes
# another and a file on its way out cannot be a reason to keep the set.
incs=$(grep -rlE "include[[:space:]]+[^;]*(pickle-realip|pickle-cf-geo)" /etc/nginx \
         --exclude=pickle-terminal.conf --exclude=pickle-cf-geo.conf \
         --exclude=pickle-realip.conf 2>/dev/null || true)
if [ -n "$refs" ] || [ -n "$incs" ]; then
  [ -n "$refs" ] && { echo "  the following nginx files still reference \$pickle_client_ip:" >&2
                      printf "    %s\n" $refs >&2; }
  [ -n "$incs" ] && { echo "  the following nginx files still include a file this removes:" >&2
                      printf "    %s\n" $incs >&2; }
  echo "  redeploy proxy-agent and run an admin resync first, then re-run this script" >&2
  exit 1
fi
removed=0
for f in /etc/nginx/conf.d/pickle-terminal.conf /etc/nginx/pickle-cf-geo.conf /etc/nginx/pickle-realip.conf; do
  if [ -e "$f" ]; then rm -f "$f"; echo "  removed $f"; removed=$((removed + 1)); fi
done
[ "$removed" -gt 0 ] || echo "  nothing to remove (already retired)"'

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

echo "== LXC $APP: /api/ vhost forwards the X-Real-IP the TLS tier sent"
# shellcheck disable=SC2016  # nginx-side $ must not expand here
# sed targets sites-available (the real file): sed -i on the sites-enabled
# symlink would replace the link with a detached copy, silently orphaning
# every later edit made on the sites-available side.
pct exec "$APP" -- sed -i 's|proxy_set_header X-Real-IP \$http_cf_connecting_ip;|proxy_set_header X-Real-IP $http_x_real_ip;|' /etc/nginx/sites-available/pickle.conf
# sed reports success when it matches nothing, and the vhost is not written by
# this script, so a renamed directive or a reworded line would leave the app tier
# trusting the raw header while every step here still passes. Assert the state
# the substitution was supposed to reach, not the fact that sed ran.
# shellcheck disable=SC2016  # nginx-side $ must not expand here
pct exec "$APP" -- bash -c '
set -euo pipefail
vhost=/etc/nginx/sites-available/pickle.conf
grep -q "proxy_set_header X-Real-IP \$http_x_real_ip;" "$vhost" || {
  echo "  $vhost does not forward the X-Real-IP the TLS tier sent" >&2
  exit 1
}
! grep -q "X-Real-IP \$http_cf_connecting_ip;" "$vhost" || {
  echo "  $vhost still trusts CF-Connecting-IP directly" >&2
  exit 1
}'
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
