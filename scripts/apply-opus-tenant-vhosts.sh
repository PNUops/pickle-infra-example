#!/usr/bin/env bash
# opus.pusan.ac.kr — the tenant that shared this proxy before the platform did.
#
# Idempotent: owns the FINAL state of the tenant's :80 vhost on LXC 100, and
# removes the TLS-termination arrangement this tier carried while the path to
# that origin's :443 was unusable.
#
# WHY THIS EXISTS. The name is an SNI passthrough: :443 is handed to the origin
# untouched by the stream router (apply-terminal-ingress.sh), so the origin
# holds the certificate for its own name and this tier never sees the
# plaintext. Only :80 is proxied, and that vhost is what this script writes.
#
# THE TERMINATION WINDOW, 2026-08-19 to 2026-09-08. It is described here
# because an nginx archive from those weeks still carries its files, and
# restoring one beside this state is how they come back. On 2026-08-18 the
# origin's :443 stopped completing handshakes while :80 kept serving, so the
# operator had the origin serve plain HTTP and TLS moved onto this tier: an LE
# certificate for a name that is not ours, a vhost on the shared 8443 tier, and
# a :80 vhost holding an ACME webroot. Days later the campus path to the origin
# began blackholing for 50 to 112 seconds at a time — ICMP and every TCP port
# together — and reached readers as a dead site, so the upstream was pointed
# off campus at the tenant's Cloudflare-fronted STAGING hostname, with
# sub_filter over the response body and Origin rewriting to get past that
# stack's CORS allowlist. Readers were served staging data for that window.
# Each file it left is named below and removed by name.
#
# What it changes (LXC 100):
#   - sites-available/opus-http.conf   (:80, proxied to the origin)
#   - removes the 8443 termination vhost  sites-{available,enabled}/opus-tls.conf
#   - removes snippets/opus-origin.conf, snippets/opus-rewrite.conf and
#     conf.d/opus-upstream.conf, the staging-detour plumbing
#
# The certificate this tier once issued for the name is NOT deleted here.
# certbot state is not nginx state, a rebuilt container never has it, and
# deletion is one-way; the script reports the lineage if it is still present so
# an operator can remove it deliberately (`certbot delete --cert-name <name>`).
#
# Run order on a rebuild: apply-terminal-ingress.sh first (it writes the stream
# router that carries :443 for this name), then this, then
# apply-main-domain-vhost.sh last.
#
# Environment (defaults reproduce this deployment exactly):
#   PICKLE_OPUS_HOST     opus.pusan.ac.kr   the tenant name
#   PICKLE_OPUS_ORIGIN   203.0.113.20     its origin
#   PICKLE_PROXY_CTID    100                reverse-proxy container
#   PICKLE_PROXY_IP      198.18.1.10        its address, used by every --resolve

set -euo pipefail

RP="${PICKLE_PROXY_CTID:-100}"
# shellcheck source=scripts/lib/ct.sh
. "$(dirname "$0")/lib/ct.sh"
require_ct "$RP" reverse-proxy

HOST="${PICKLE_OPUS_HOST:-opus.pusan.ac.kr}"
ORIGIN="${PICKLE_OPUS_ORIGIN:-203.0.113.20}"
PROXY_IP="${PICKLE_PROXY_IP:-198.18.1.10}"

ts=$(date +%Y%m%d-%H%M%S)
PICKLE_ROOT="${PICKLE_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
BK="${PICKLE_ROOT}/backup/opus-tenant-vhosts-$ts"
mkdir -p "$BK"

fails=0
# expect_http LABEL EXPECTED-CODE CURL-ARGS… — a probe that prints its code and
# continues reads exactly like a healthy one, so every check asserts instead.
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

echo "== backup current nginx state of LXC $RP -> $BK"
pct exec "$RP" -- tar czf /tmp/nginx-etc.tgz -C / etc/nginx
pct pull "$RP" /tmp/nginx-etc.tgz "$BK/lxc100-nginx-etc.tgz"
pct exec "$RP" -- rm /tmp/nginx-etc.tgz
ROLLBACK="untar $BK/lxc100-nginx-etc.tgz over /etc/nginx on LXC $RP, then nginx -t and reload."

echo "== pre-flight: the origin must terminate its own TLS for $HOST"
# This is the whole premise of the passthrough. The chain is checked WITHOUT
# -k: a reader's browser validates it directly, so an origin certificate that
# does not verify reaches every visitor as an interstitial and nothing on this
# tier can soften it.
if pct exec "$RP" -- bash -c "curl -s -o /dev/null --max-time 15 --resolve '$HOST:443:$ORIGIN' 'https://$HOST/'"; then
  echo "  OK   origin :443 serves $HOST and its chain verifies"
else
  echo "  FAIL origin :443 does not serve a verifiable certificate for $HOST" >&2
  echo "       The passthrough would hand readers a broken handshake. Fix the origin first." >&2
  exit 1
fi

echo "== pre-flight: the origin must serve plain HTTP on :80"
# The :80 vhost below proxies rather than redirects, matching what this tier
# has always done for the name. An origin that answered :80 with a redirect to
# https would still work, but a redirect to a name that resolves back here is
# what a loop looks like, so the code is asserted rather than assumed.
origin_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H "Host: $HOST" "http://$ORIGIN/") || origin_code=000
case "$origin_code" in
  200) echo "  OK   origin :80 -> 200" ;;
  *)   echo "  FAIL origin :80 -> ${origin_code:-none} (expected 200)" >&2; exit 1 ;;
esac

echo "== LXC $RP: :80 vhost (proxied to the origin)"
# Self-contained on purpose: the shared snippet this vhost used to include is
# one of the files removed below, and a vhost that outlives its snippet fails
# `nginx -t` for the whole configuration.
pct exec "$RP" -- bash -c "cat > /etc/nginx/sites-available/opus-http.conf" <<EOF
# $HOST — pre-existing tenant. Only :80 is proxied here; :443 is an SNI
# passthrough written by apply-terminal-ingress.sh, so the origin holds the
# certificate for the name and this tier never sees the plaintext.
server {
    listen 80;
    listen [::]:80;

    server_name $HOST;

    location / {
        proxy_pass http://$ORIGIN:80;
        proxy_http_version 1.1;

        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;

        proxy_connect_timeout 10s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
EOF
pct exec "$RP" -- ln -sf /etc/nginx/sites-available/opus-http.conf /etc/nginx/sites-enabled/opus-http.conf

echo "== LXC $RP: remove the termination-window files"
# Order: the vhost that includes the snippets goes first. Removing a snippet
# while an enabled vhost still includes it fails `nginx -t` for every name on
# the container, not just this one.
# shellcheck disable=SC2016  # the loop variable must expand on the container
pct exec "$RP" -- bash -c '
set -euo pipefail
removed=0
for f in /etc/nginx/sites-enabled/opus-tls.conf \
         /etc/nginx/sites-available/opus-tls.conf \
         /etc/nginx/snippets/opus-origin.conf \
         /etc/nginx/snippets/opus-rewrite.conf \
         /etc/nginx/conf.d/opus-upstream.conf; do
  if [ -e "$f" ] || [ -L "$f" ]; then rm -f "$f"; echo "  removed $f"; removed=$((removed + 1)); fi
done
[ "$removed" -gt 0 ] || echo "  nothing to remove (already at the passthrough state)"'

pct exec "$RP" -- nginx -t
pct exec "$RP" -- systemctl reload nginx

echo "== post-change verification"
# Through the real :443 path, and without -k: what must reach a reader is the
# ORIGIN's certificate. A -k probe would pass just as happily on a tier that
# had quietly gone back to terminating.
expect_http "opus :80  (proxied to the origin)" 200 --resolve "$HOST:80:$PROXY_IP" "http://$HOST/"
if pct exec "$RP" -- bash -c "curl -s -o /dev/null --max-time 15 --resolve '$HOST:443:$PROXY_IP' 'https://$HOST/'"; then
  echo "  OK   opus :443 verifies through the passthrough"
else
  echo "  FAIL opus :443 does not verify through this tier" >&2
  fails=$((fails + 1))
fi

# The certificate presented through the proxy must be the origin's own, not one
# held here. Comparing the serial to what the origin serves directly is what
# separates a passthrough from a termination that happens to hold a valid cert.
via=$(pct exec "$RP" -- bash -c "echo | openssl s_client -connect $PROXY_IP:443 -servername $HOST 2>/dev/null | openssl x509 -noout -serial" || true)
direct=$(pct exec "$RP" -- bash -c "echo | openssl s_client -connect $ORIGIN:443 -servername $HOST 2>/dev/null | openssl x509 -noout -serial" || true)
if [ -n "$via" ] && [ "$via" = "$direct" ]; then
  echo "  OK   the certificate through this tier is the origin's ($via)"
else
  echo "  FAIL certificate mismatch — via='$via' direct='$direct'" >&2
  echo "       A differing serial means this tier is terminating, not passing through." >&2
  fails=$((fails + 1))
fi

if pct exec "$RP" -- test -e "/etc/letsencrypt/live/$HOST/fullchain.pem"; then
  echo "  NOTE this container still holds an LE lineage for $HOST, left from the"
  echo "       termination window. Nothing serves it. Remove it deliberately with"
  echo "       'pct exec $RP -- certbot delete --cert-name $HOST'."
fi

if [ "$fails" -ne 0 ]; then
  echo "FAILED — $fails check(s) did not hold; the new nginx state is live but unverified." >&2
  echo "         Roll back: $ROLLBACK" >&2
  exit 1
fi

echo "OK — rollback if ever needed: $ROLLBACK"
