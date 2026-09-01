#!/bin/bash
# shellcheck disable=SC2015  # ok/ko idiom (ok/ko always return 0) is safe here
# ==========================================================================
# Production smoke for pickle — READ-ONLY by default.
#
# Layers:
#   [0] infra snapshot     — reuses health-check.sh (host/LXC/DB/jobs/gw/backup)
#   [1] auth               — login with a probe account
#   [2] read-only API      — GET the core read surface, assert 200s
#   [3] provision cycle    — ONLY with --allow-provision: one real VM
#                            request→approve→RUNNING→force-delete cycle
#
# Probe account (env; in real prod use a DEDICATED low-privilege account):
#   PICKLE_SMOKE_EMAIL / PICKLE_SMOKE_PASSWORD
# If unset, falls back to the dev seed ORG_ADMIN read from LXC 101 api.env.
#
# Deliberately does NOT cover (verify out of band):
#   - SSH gateway end-to-end (needs an off-campus client through the Lightsail
#     relay — that is smoke-ssh-gateway.sh's job)
#   - real email delivery (SMTP) and custom-domain Let's Encrypt issuance
#   - any data mutation beyond the optional --allow-provision cycle
#
# The --allow-provision cycle uses REAL host capacity (prod == the dev host
# today) and, as written, the dev mock-mail verification path — on a real-SMTP
# prod, seed a pre-verified probe group/account instead.
# ==========================================================================
set -uo pipefail

ALLOW_PROVISION=0
[ "${1:-}" = "--allow-provision" ] && ALLOW_PROVISION=1

BASE="${BASE:-https://pickle.pusan.ac.kr/api/v1}"
# Signup requires consent to every current terms version (422 otherwise).
# Built once from the public endpoint so version bumps never break the smoke.
CONSENTS_JSON=$(curl -fsS "$BASE/meta/terms" 2>/dev/null | jq -c '[.[] | {docType, version}]' 2>/dev/null)
[ -n "$CONSENTS_JSON" ] || CONSENTS_JSON='[]'

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
# shellcheck source=scripts/lib/auth.sh
. "$(dirname "$0")/lib/auth.sh"

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
  # listing keys is not a sudo-mode endpoint — no X-Reauth-Token needed here
  req "GET /me/ssh-keys"  200 "$BASE/me/ssh-keys"  -H "Authorization: Bearer $AT"
fi

if [ "$ALLOW_PROVISION" = 1 ] && [ -n "$AT" ]; then
  echo "== [3] provision → destroy cycle (--allow-provision; REAL capacity) =="
  TS=$(date +%s)-$RANDOM
  VM=""; VNAME=""
  # requester: fresh signup via dev mock-mail verification. Real-SMTP prod: swap
  # for a pre-verified probe account and skip the mock-mail token read.
  OEMAIL="prodsmoke-${TS}@pusan.ac.kr"; OPW="prodsmoke-${TS}!"
  curl -sS -o /dev/null --max-time 20 -X POST "$BASE/auth/signup" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$OEMAIL\",\"password\":\"$OPW\",\"name\":\"prod smoke\",\"consents\":$CONSENTS_JSON}"
  sleep 2
  TOK=$(pct exec "$CTID" -- sh -c "grep -o 'token=[A-Za-z0-9_-]*' /var/lib/pickle/mock-mail.log | tail -1 | cut -d= -f2" 2>/dev/null)
  if [ -n "$TOK" ]; then
    curl -sS -o /dev/null --max-time 20 -X POST "$BASE/auth/verify-email" -H 'Content-Type: application/json' -d "{\"token\":\"$TOK\"}"
  else
    ko "no mock-mail token (real-SMTP prod: use a pre-verified probe account)"
  fi
  req "owner login" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$OEMAIL\",\"password\":\"$OPW\"}"
  OAT=$(jq -r '.accessToken // empty' "$B")
  req "create workspace" 201 -X POST "$BASE/workspaces" -H "Authorization: Bearer $OAT" -H 'Content-Type: application/json' -d "{\"name\":\"prodsmoke\",\"kind\":\"TEAM\"}"
  GID=$(jq -r '.id // empty' "$B")
  # the seed org is hidden and GET /orgs filters hidden orgs for USER tokens — list as orgadmin
  ADMIN_PW="$(seed_env PICKLE_SEED_ORGADMIN_PASSWORD)"
  ADMIN_EMAIL="$(seed_env PICKLE_SEED_ORGADMIN_EMAIL)"; ADMIN_EMAIL="${ADMIN_EMAIL:-orgadmin@pnuops.com}"
  req "orgadmin login (org lookup)" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PW\"}"
  AAT=$(jq -r '.accessToken // empty' "$B")
  req "orgs" 200 "$BASE/orgs" -H "Authorization: Bearer $AAT"; OID=$(jq -r '.[0].id // empty' "$B")
  req "os-images" 200 "$BASE/os-images" -H "Authorization: Bearer $OAT"
  TID=$(jq -r '.[0].id // empty' "$B")
  # os-images is the OS catalog; specs come from vm-flavors and POST
  # /requests requires flavorId ('basic' preset, else the first ACTIVE row)
  req "vm-flavors" 200 "$BASE/vm-flavors" -H "Authorization: Bearer $OAT"
  FSEL='(map(select(.name=="basic"))[0] // .[0])'
  FID=$(jq -r "$FSEL.id // empty" "$B"); VC=$(jq -r "$FSEL.vcpu // empty" "$B"); MM=$(jq -r "$FSEL.memoryMb // empty" "$B"); DG=$(jq -r "$FSEL.diskGb // empty" "$B")
  if [ -n "$GID" ] && [ -n "$OID" ] && [ -n "$TID" ] && [ -n "$FID" ]; then
    req "vm request" 201 -X POST "$BASE/requests" -H "Authorization: Bearer $OAT" -H 'Content-Type: application/json' -d "{\"type\":\"VM\",\"workspaceId\":$GID,\"orgId\":$OID,\"purpose\":\"prod smoke\",\"courseOrProject\":null,\"extraNote\":null,\"reqStartDate\":null,\"reqEndDate\":null,\"vm\":{\"imageId\":$TID,\"flavorId\":$FID,\"reqVcpu\":$VC,\"reqMemoryMb\":$MM,\"reqDiskGb\":$DG,\"specReason\":null}}"
    RID=$(jq -r '.id // empty' "$B")
    # approve as seed ORG_ADMIN (token from the org-lookup login above)
    req "approve" 200 -X POST "$BASE/admin/requests/$RID/approve" -H "Authorization: Bearer $AAT" -H 'Content-Type: application/json' -d "{\"grantedStartDate\":null,\"grantedEndDate\":null,\"comment\":\"prod smoke\",\"vm\":{\"grantedVcpu\":$VC,\"grantedMemoryMb\":$MM,\"grantedDiskGb\":$DG,\"grantedImageId\":$TID,\"nodeId\":null}}"
    req "vm list" 200 "$BASE/vms?workspaceId=$GID" -H "Authorization: Bearer $OAT"
    VM=$(jq -r '.content[0].id // empty' "$B"); VNAME=$(jq -r '.content[0].name // empty' "$B")
    [ -n "$VM" ] && ok "vm id=$VM name=$VNAME" || ko "no VM created"
    if [ -n "$VM" ]; then
      echo "-- poll RUNNING (<=15min) --"
      DL=$((SECONDS+900)); ST=""
      while :; do
        curl -sS -o "$B" --max-time 20 "$BASE/vms/$VM" -H "Authorization: Bearer $OAT"
        ST=$(jq -r '.status // empty' "$B")
        [ "$ST" = RUNNING ] && break
        { [ "$ST" = ERROR ] || [ "$ST" = NEEDS_ADMIN ]; } && break
        [ "$SECONDS" -ge "$DL" ] && break
        sleep 10
      done
      [ "$ST" = RUNNING ] && ok "VM RUNNING" || ko "VM not RUNNING (last=$ST)"
      # force-delete as seed SYS_ADMIN (confirmName only — no reason field)
      SYS_PW="$(seed_env PICKLE_SEED_SYSADMIN_PASSWORD)"
      SYS_EMAIL="$(seed_env PICKLE_SEED_SYSADMIN_EMAIL)"; SYS_EMAIL="${SYS_EMAIL:-admin@pnuops.com}"
      # An enrolled administrator answers the login with a challenge, and a bare
      # status assertion would call that a pass while leaving XAT empty. This is
      # the cleanup path for a VM that is already running, so a silent empty
      # token here leaves it on the host.
      if XAT=$(login_token "$BASE" "$SYS_EMAIL" "$SYS_PW"); then ok "sysadmin login"; else ko "sysadmin login"; fi
      req "force-delete" 202 -X POST "$BASE/admin/vms/$VM/force-delete" -H "Authorization: Bearer $XAT" -H 'Content-Type: application/json' -d "{\"confirmName\":\"$VNAME\"}"
      echo "-- poll DELETED (<=3min) --"
      DL=$((SECONDS+180)); DC=""; DST=""
      while :; do
        DC=$(curl -sS -o "$B" -w '%{http_code}' --max-time 20 "$BASE/vms/$VM" -H "Authorization: Bearer $XAT")
        DST=$(jq -r '.status // empty' "$B" 2>/dev/null)
        { [ "$DC" = 404 ] || [ "$DST" = DELETED ]; } && break
        [ "$SECONDS" -ge "$DL" ] && break
        sleep 10
      done
      { [ "$DC" = 404 ] || [ "$DST" = DELETED ]; } && ok "VM destroyed" || ko "VM not destroyed (http=$DC status=$DST) — MANUAL CLEANUP of $VNAME needed"
    fi
  else
    ko "provision preconditions missing (group/org/os-image/flavor) — skipping cycle"
  fi
fi

echo
echo "smoke-prod: $P passed / $((P+F)) checks (mode: $([ "$ALLOW_PROVISION" = 1 ] && echo full-cycle || echo read-only))"
[ "$F" -eq 0 ] && exit 0 || exit 1
