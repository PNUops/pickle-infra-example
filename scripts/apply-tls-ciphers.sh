#!/usr/bin/env bash
# Modern TLS cipher suite on both nginx tiers (LXC 100 reverse proxy, LXC 101 app).
# Idempotent: writes conf.d/pickle-tls.conf, tests, reloads, verifies.
#
# Why: nginx.conf already pins `ssl_protocols TLSv1.2 TLSv1.3` and leaves
# `ssl_prefer_server_ciphers off`, but `ssl_ciphers` was never set, so TLS 1.2
# negotiated whatever the OpenSSL default list allows — including CBC and non-PFS
# suites. This pins the Mozilla intermediate suites instead. TLS 1.3 suites are not
# controlled by ssl_ciphers (they are always the three AEAD suites) and need no
# setting here.
#
# LXC 100 also fronts the opus.pusan.ac.kr tenant, so the script verifies opus
# before and after. The opus :443 SNI is passthrough (the origin terminates its own
# TLS), so this change cannot affect the opus cipher negotiation; the check is there
# because the two share one nginx process.
set -euo pipefail

RP=100   # reverse-proxy LXC
APP=101  # app LXC
CONF=/etc/nginx/conf.d/pickle-tls.conf

ts=$(date +%Y%m%d-%H%M%S)
BK="${BK:-/root/pickle/backup/tls-ciphers-$ts}"
mkdir -p "$BK"

echo "== backup current nginx state of LXC $RP and LXC $APP -> $BK"
for ct in "$RP" "$APP"; do
  pct exec "$ct" -- tar czf /tmp/nginx-etc.tgz -C / etc/nginx
  pct pull "$ct" /tmp/nginx-etc.tgz "$BK/lxc$ct-nginx-etc.tgz"
  pct exec "$ct" -- rm /tmp/nginx-etc.tgz
done

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

echo "== pre-change reachability"
expect_http "pre  pickle :443" 200 --resolve pickle.pusan.ac.kr:443:198.18.1.10 https://pickle.pusan.ac.kr/
expect_http "pre  opus   :443" 200 --resolve opus.pusan.ac.kr:443:198.18.1.10 https://opus.pusan.ac.kr/
[ "$fails" -eq 0 ] || { echo "aborting: the tiers are not healthy before the change" >&2; exit 1; }

# Mozilla intermediate, ECDHE only: the DHE suites of that profile need an
# ssl_dhparam file, which neither tier has, and every client that reaches TLS 1.2
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
# selected through ssl_ciphers. ssl_protocols and ssl_prefer_server_ciphers stay in
# nginx.conf where the distribution put them.
ssl_ciphers $CIPHERS;
EOF

for ct in "$RP" "$APP"; do
  echo "== LXC $ct: install $CONF"
  pct push "$ct" "$tmp" "$CONF" --perms 644
  pct exec "$ct" -- nginx -t
  pct exec "$ct" -- systemctl reload nginx
done
rm -f "$tmp"

echo "== post-change verification"
expect_http "post pickle :443" 200 --resolve pickle.pusan.ac.kr:443:198.18.1.10 https://pickle.pusan.ac.kr/
expect_http "post opus   :443" 200 --resolve opus.pusan.ac.kr:443:198.18.1.10 https://opus.pusan.ac.kr/

echo "== negotiated TLS 1.2 suite"
# The whole point of the script is which suite gets negotiated, and nothing here
# used to read it — only the status code was printed, which is identical whether
# the policy took effect or not. curl reports the suite it settled on; assert it
# against the very list installed above.
neg=$(curl -skv --tlsv1.2 --tls-max 1.2 -o /dev/null \
        --resolve pickle.pusan.ac.kr:443:198.18.1.10 https://pickle.pusan.ac.kr/ 2>&1 \
      | sed -n 's|^\* SSL connection using [^/]*/ *\([^ /]*\).*|\1|p' | head -1)
if [ -z "$neg" ]; then
  echo "  FAIL could not determine the negotiated TLS 1.2 suite" >&2
  fails=$((fails + 1))
elif printf '%s' ":$CIPHERS:" | grep -qF ":$neg:"; then
  echo "  OK   pickle tls1.2 negotiated $neg (on the allowed list)"
else
  echo "  FAIL pickle tls1.2 negotiated $neg, which is NOT on the allowed list" >&2
  fails=$((fails + 1))
fi

if [ "$fails" -ne 0 ]; then
  echo "FAILED — $fails check(s) did not hold. Roll back: remove $CONF on both" >&2
  echo "         containers (or untar $BK/lxc100-nginx-etc.tgz /" >&2
  echo "         $BK/lxc101-nginx-etc.tgz over /etc/nginx) and reload." >&2
  exit 1
fi

echo "OK — rollback if ever needed: remove $CONF on both containers (or untar"
echo "     $BK/lxc100-nginx-etc.tgz / lxc101-nginx-etc.tgz over /etc/nginx) and reload."
