#!/bin/bash
# shellcheck disable=SC2015  # ok/ko idiom (ok/ko always return 0) is safe here
# ==========================================================================
# Production smoke for pickle — READ-ONLY.
#
# Layers:
#   [0] infra snapshot     — reuses health-check.sh (host/LXC/DB/jobs/gw/backup)
#   [1] auth               — login with a probe account
#   [2] read-only API      — GET the core read surface, assert 200s
#
# Probe account (env; in real prod use a DEDICATED low-privilege account):
#   PICKLE_SMOKE_EMAIL / PICKLE_SMOKE_PASSWORD
# If unset, falls back to the dev seed ORG_ADMIN read from LXC 101 api.env.
#
# Deliberately does NOT cover (verify out of band):
#   - SSH gateway end-to-end (needs an off-campus client through the Lightsail
#     relay — that is smoke-ssh-gateway.sh's job)
#   - real email delivery (SMTP) and custom-domain Let's Encrypt issuance
#   - any data mutation. The request → approve → RUNNING → delete cycle this
#     script once ran under --allow-provision is smoke-provisioning.sh's job,
#     which covers it in more depth; the flag is refused rather than ignored,
#     so nobody reads a read-only pass as a provisioning one.
# ==========================================================================
set -uo pipefail

if [ "$#" -gt 0 ]; then
  echo "smoke-prod.sh takes no arguments (the provision cycle is smoke-provisioning.sh)" >&2
  exit 2
fi

BASE="${BASE:-https://pickle.pusan.ac.kr/api/v1}"
CTID="${CTID:-101}"
HC="$(dirname "$0")/health-check.sh"
B=$(mktemp); trap 'rm -f "$B"' EXIT

P=0; F=0
ok(){ echo "PASS  $1"; P=$((P+1)); }
ko(){ echo "FAIL  $1"; F=$((F+1)); }
# req NAME EXPECT curl-args...  → asserts HTTP status, body captured in $B
req(){ local n="$1" e="$2"; shift 2; local c
  c=$(curl -sS -o "$B" -w '%{http_code}' --max-time 20 "$@")
  [ "$c" = "$e" ] && ok "$n ($c)" || { ko "$n (want $e got $c)"; head -c 200 "$B"; echo; }
}
seed_env(){ pct exec "$CTID" -- sh -c "grep '^$1=' /etc/pickle/api.env | cut -d= -f2-" 2>/dev/null; }

echo "== [0] infra health snapshot (health-check.sh) =="
if [ -x "$HC" ]; then
  "$HC" && ok "health-check clean" || ko "health-check reported FAIL (see table above)"
else ko "health-check.sh not executable at $HC"; fi

echo "== [1] auth (probe account) =="
EMAIL="${PICKLE_SMOKE_EMAIL:-orgadmin@pnuops.com}"
PW="${PICKLE_SMOKE_PASSWORD:-$(seed_env PICKLE_SEED_ORGADMIN_PASSWORD)}"
AT=""
if [ -z "$PW" ]; then
  ko "no probe password (set PICKLE_SMOKE_PASSWORD, or run on pve-node where api.env is readable)"
else
  req "login" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
      -d "{\"email\":\"$EMAIL\",\"password\":\"$PW\"}"
  AT=$(jq -r '.accessToken // empty' "$B" 2>/dev/null)
  [ -n "$AT" ] && ok "obtained access token" || ko "login returned no accessToken"
fi

if [ -n "$AT" ]; then
  echo "== [2] read-only API surface =="
  req "GET /me"           200 "$BASE/me"           -H "Authorization: Bearer $AT"
  req "GET /orgs"         200 "$BASE/orgs"         -H "Authorization: Bearer $AT"
  req "GET /os-images"    200 "$BASE/os-images"    -H "Authorization: Bearer $AT"
  req "GET /vm-flavors"   200 "$BASE/vm-flavors"   -H "Authorization: Bearer $AT"
  # SSH keys hang off a VM now, so there is no account-level key surface to
  # probe here; the per-VM one is exercised by the SSH gateway smoke.
  req "GET /resources"    200 "$BASE/resources"    -H "Authorization: Bearer $AT"
fi

echo
echo "smoke-prod: $P passed / $((P+F)) checks (mode: read-only)"
[ "$F" -eq 0 ] && exit 0 || exit 1
