#!/bin/bash
# shellcheck disable=SC2015  # `[ cond ] && ok || ko` — ok/ko always succeed, safe idiom
# HTTP-publishing e2e — run on pve-node as root. Provisions a real dev VM, starts a
# web server on it, publishes via the API, verifies HTTPS 200 via the origin :443
# SNI path (the Cloudflare leg is unreachable from pve-node — hairpin NAT), then
# force-deletes and confirms publishing teardown removed the vhost
# before IP release. Cleans up the VM regardless.
set -uo pipefail
BASE="${BASE:-https://pickle.pusan.ac.kr/api/v1}"
# Signup requires consent to every current terms version (422 otherwise).
# Built once from the public endpoint so version bumps never break the smoke.
CONSENTS_JSON=$(curl -fsS "$BASE/meta/terms" 2>/dev/null | jq -c '[.[] | {docType, version}]' 2>/dev/null)
[ -n "$CONSENTS_JSON" ] || CONSENTS_JSON='[]'

CTID="${CTID:-101}"
TS=$(date +%s)-$RANDOM
SUB="e2e-${TS}"
# Platform root to publish under. Overridable so the smoke can exercise a second
# root the moment one exists, without editing the script.
ROOT="${ROOT:-pusan.dev}"
USER_EMAIL="http-${TS}@pusan.ac.kr"; USER_PW="http-pass-${TS}!"
seed_env(){ pct exec "$CTID" -- sh -c "grep '^$1=' /etc/pickle/api.env | cut -d= -f2-"; }
ORGADMIN_EMAIL="orgadmin@pickle.local"; ORGADMIN_PW="$(seed_env PICKLE_SEED_ORGADMIN_PASSWORD)"
SYSADMIN_EMAIL="admin@pickle.local"; SYSADMIN_PW="$(seed_env PICKLE_SEED_SYSADMIN_PASSWORD)"
B=$(mktemp); RT_DIR=$(mktemp -d)   # RT_DIR: sudo-mode token cache (see reauth below)
# Always-run cleanup (EXIT trap): a mid-run failure after the VM exists must
# not leak a real guest + IP — force-delete best-effort, mirroring smoke-provisioning.
VM=""; VM_DELETED=0
cleanup(){
  local rc=$?
  if [ -n "$VM" ] && [ "$VM_DELETED" != 1 ]; then
    echo "-- cleanup: force-deleting leftover VM $VM --"
    local at
    at=$(curl -sS -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
      -d "{\"email\":\"$SYSADMIN_EMAIL\",\"password\":\"$SYSADMIN_PW\"}" | jq -r '.accessToken // empty')
    if [ -n "$at" ] && [ -n "${VNAME:-}" ]; then
      # This is the last chance to avoid leaking a real guest and its IP, so the
      # response code decides what gets printed — a swallowed failure here reads
      # as "cleaned up" while the VM is still running.
      local dc
      dc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/admin/vms/$VM/force-delete" \
        -H "Authorization: Bearer $at" -H 'Content-Type: application/json' \
        -d "{\"confirmName\":\"$VNAME\",\"reason\":\"smoke cleanup (trap)\"}")
      if [ "$dc" = 202 ]; then
        echo "-- cleanup: force-delete accepted (202) --"
      else
        echo "-- cleanup: force-delete REJECTED (http=${dc:-none}); manual cleanup needed (vm id $VM) --" >&2
      fi
    else
      echo "-- cleanup: could not obtain admin token or VM name; manual cleanup needed (vm id $VM) --" >&2
    fi
  fi
  rm -f "$B"; rm -rf "$RT_DIR"
  exit "$rc"
}
trap cleanup EXIT
P=0; F=0; ok(){ echo "PASS  $1"; P=$((P+1)); }; ko(){ echo "FAIL  $1"; F=$((F+1)); }
req(){ local n="$1" e="$2"; shift 2; local c; c=$(curl -sS -o "$B" -w '%{http_code}' "$@"); if [ "$c" = "$e" ]; then ok "$n ($c)"; return 0; else ko "$n (expected $e got $c)"; head -c 300 "$B"; echo; return 1; fi; }
# Sudo-mode reauth: the password-reveal / VM-delete / settings / ssh-key /
# group-member endpoints answer 403 REAUTH_REQUIRED without a fresh password
# proof (X-Reauth-Token). The token is per-account and multi-use for 10 minutes,
# and POST /auth/reverify is rate-limited per IP and per account, so cache it
# per access token instead of minting one per call. A password change bumps the
# token version (killing both tokens), but that also forces a re-login here, so
# the new access token becomes a new cache key and the cache self-heals.
# The cache is FILE-backed on purpose: every call site invokes reauth from a
# command substitution (a subshell), so an in-memory array would be written in
# the subshell and thrown away — each protected call would then mint a fresh
# token and the per-IP reverify limit (every account here shares this host's
# egress IP) would start answering 429 with an empty token, i.e. a spurious
# REAUTH_REQUIRED failure. Entries expire well inside the 10-minute server TTL.
reauth(){ # reauth ACCESS_TOKEN PASSWORD → echoes the X-Reauth-Token value
  local f exp tok
  f="$RT_DIR/$(printf '%s' "$1" | md5sum | cut -d' ' -f1)"
  if [ -s "$f" ]; then
    { read -r exp; read -r tok; } < "$f"
    [ "$SECONDS" -lt "${exp:-0}" ] && { printf '%s' "$tok"; return 0; }
  fi
  tok=$(curl -sS -X POST "$BASE/auth/reverify" -H "Authorization: Bearer $1" \
    -H 'Content-Type: application/json' -d "$(jq -nc --arg p "$2" '{password:$p}')" \
    | jq -r '.reauthToken // empty')
  [ -n "$tok" ] && printf '%s\n%s\n' "$((SECONDS+480))" "$tok" > "$f"
  printf '%s' "$tok"
}
# 'basic' spec preset (first ACTIVE row as fallback) out of GET /vm-flavors
FSEL='(map(select(.name=="basic"))[0] // .[0])'

echo "== auth =="
req "signup" 202 -X POST "$BASE/auth/signup" -H 'Content-Type: application/json' -d "{\"email\":\"$USER_EMAIL\",\"password\":\"$USER_PW\",\"name\":\"HTTP E2E\",\"consents\":$CONSENTS_JSON}" || exit 1
sleep 2
TOKEN=$(pct exec "$CTID" -- sh -c "grep -o 'token=[A-Za-z0-9_-]*' /var/lib/pickle/mock-mail.log 2>/dev/null | tail -1 | cut -d= -f2")
req "verify-email" 200 -X POST "$BASE/auth/verify-email" -H 'Content-Type: application/json' -d "{\"token\":\"$TOKEN\"}" || exit 1
req "user login" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$USER_EMAIL\",\"password\":\"$USER_PW\"}" || exit 1
SAT=$(jq -r .accessToken "$B")
req "create group" 201 -X POST "$BASE/groups" -H "Authorization: Bearer $SAT" -H 'Content-Type: application/json' -d "{\"name\":\"http e2e\",\"slug\":\"httpteam-${TS}\",\"kind\":\"TEAM\"}" || exit 1
GID=$(jq -r .id "$B")
req "orgadmin login" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$ORGADMIN_EMAIL\",\"password\":\"$ORGADMIN_PW\"}" || exit 1
AAT=$(jq -r .accessToken "$B")
# the seed org is hidden and GET /orgs filters hidden orgs for USER tokens — list as orgadmin
req "orgs" 200 "$BASE/orgs" -H "Authorization: Bearer $AAT" || exit 1; OID=$(jq -r '.[0].id' "$B")
req "os-images" 200 "$BASE/os-images" -H "Authorization: Bearer $SAT" || exit 1
TID=$(jq -r '.[0].id // empty' "$B")
[ -n "$TID" ] || { ko "no ACTIVE OS image to request with"; exit 1; }
# os-images is a pure OS catalog — the spec axis lives in vm-flavors, and
# POST /vm-requests requires the chosen flavorId alongside the req* specs
req "vm-flavors" 200 "$BASE/vm-flavors" -H "Authorization: Bearer $SAT" || exit 1
FID=$(jq -r "$FSEL.id // empty" "$B"); VC=$(jq -r "$FSEL.vcpu // empty" "$B"); MM=$(jq -r "$FSEL.memoryMb // empty" "$B"); DG=$(jq -r "$FSEL.diskGb // empty" "$B")
[ -n "$FID" ] && ok "flavor id=$FID (${VC}c/${MM}MB/${DG}GB)" || { ko "no ACTIVE vm-flavor"; exit 1; }

echo "== request + approve (subdomain pre-picked=$SUB) =="
req "vm-request" 201 -X POST "$BASE/vm-requests" -H "Authorization: Bearer $SAT" -H 'Content-Type: application/json' -d "{\"groupId\":$GID,\"orgId\":$OID,\"imageId\":$TID,\"flavorId\":$FID,\"purpose\":\"HTTP publish e2e\",\"courseOrProject\":null,\"specReason\":null,\"extraNote\":null,\"reqVcpu\":$VC,\"reqMemoryMb\":$MM,\"reqDiskGb\":$DG,\"reqStartDate\":null,\"reqEndDate\":null,\"desiredSubdomain\":\"$SUB\",\"rootDomain\":\"$ROOT\"}" || exit 1
RID=$(jq -r .id "$B")
req "approve" 200 -X POST "$BASE/admin/vm-requests/$RID/approve" -H "Authorization: Bearer $AAT" -H 'Content-Type: application/json' -d "{\"grantedVcpu\":$VC,\"grantedMemoryMb\":$MM,\"grantedDiskGb\":$DG,\"grantedImageId\":$TID,\"grantedStartDate\":null,\"grantedEndDate\":null,\"nodeId\":null,\"comment\":\"http e2e\"}" || exit 1
req "vm list" 200 "$BASE/vms?groupId=$GID" -H "Authorization: Bearer $SAT" || exit 1
VM=$(jq -r '.content[0].id // empty' "$B"); VNAME=$(jq -r '.content[0].name // empty' "$B")
[ -n "$VM" ] && ok "vm id=$VM name=$VNAME" || { ko "vm id (empty list)"; exit 1; }
curl -sS -o "$B" "$BASE/vms/$VM" -H "Authorization: Bearer $SAT"
RSUB=$(jq -r '.requestedSubdomain // empty' "$B"); [ "$RSUB" = "$SUB" ] && ok "requestedSubdomain=$SUB (VmDetail)" || ko "requestedSubdomain=$RSUB (expected $SUB)"

echo "== poll RUNNING (<=15m) =="
DL=$((SECONDS+900)); ST=""
while :; do curl -sS -o "$B" "$BASE/vms/$VM" -H "Authorization: Bearer $SAT"; ST=$(jq -r .status "$B"); VIP=$(jq -r '.ipAddress // empty' "$B")
  [ "$ST" = "RUNNING" ] && break
  if [ "$ST" = "ERROR" ] || [ "$ST" = "NEEDS_ADMIN" ]; then ko "provision reached $ST"; jq -r '.statusDetail' "$B"; break; fi
  [ "$SECONDS" -ge "$DL" ] && { ko "not RUNNING in 15m (last=$ST)"; break; }; sleep 10; done
[ "$ST" = "RUNNING" ] && ok "VM RUNNING ip=$VIP" || exit 1

echo "== reveal password + start web server on VM:80 =="
curl -sS -o "$B" "$BASE/vms/$VM/password" -H "Authorization: Bearer $SAT" \
  -H "X-Reauth-Token: $(reauth "$SAT" "$USER_PW")"
PWV=$(jq -r '.password // empty' "$B"); [ -n "$PWV" ] && ok "initial password revealed (masked)" || ko "password reveal"
# wait ssh
for _ in $(seq 1 18); do nc -z -w5 "$VIP" 22 2>/dev/null && break; sleep 5; done
# the VM password IS the sudo credential (template sudoers demands it) — feed it to
# a single sudo -S via stdin, then probe as the unprivileged user
printf '%s\n' "$PWV" | sshpass -p "$PWV" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 "ubuntu@$VIP" \
  "sudo -S bash -c 'mkdir -p /var/e2e; echo PICKLE-HTTP-E2E-OK > /var/e2e/index.html; cd /var/e2e; nohup python3 -m http.server 80 >/tmp/httpd.log 2>&1 & sleep 2' 2>/dev/null; curl -s -o /dev/null -w 'local:%{http_code}' http://127.0.0.1:80/index.html" >"$B" 2>/dev/null
PWV=""; grep -q "local:200" "$B" && ok "web server up on VM:80" || { ko "web server on VM ($(cat "$B"))"; }
# proxy-agent can reach VM:80 from LXC 100
pct exec 100 -- sh -c "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 http://$VIP:80/index.html" >"$B" 2>/dev/null
[ "$(cat "$B")" = "200" ] && ok "LXC100 → VM:80 reachable" || ko "LXC100 → VM:80 ($(cat "$B"))"

echo "== publish (port 80, request-form subdomain) =="
req "publish" 202 -X POST "$BASE/vms/$VM/publish" -H "Authorization: Bearer $SAT" -H 'Content-Type: application/json' -d "{\"port\":80}" || true
echo "== poll route APPLIED =="
DL=$((SECONDS+120)); RST=""; FQDN=""
while :; do curl -sS -o "$B" "$BASE/vms/$VM" -H "Authorization: Bearer $SAT"; RST=$(jq -r '.publication.route.status // empty' "$B"); FQDN=$(jq -r '.publication.fqdn // empty' "$B")
  [ "$RST" = "APPLIED" ] && break; [ "$RST" = "FAILED" ] && { ko "route FAILED: $(jq -r '.publication.route.lastError' "$B")"; break; }
  [ "$SECONDS" -ge "$DL" ] && { ko "route not APPLIED in 120s (last=$RST)"; break; }; sleep 5; done
[ "$RST" = "APPLIED" ] && ok "route APPLIED fqdn=$FQDN" || true
[ "$FQDN" = "${SUB}.${ROOT}" ] && ok "fqdn = pre-picked subdomain" || ko "fqdn=$FQDN (expected ${SUB}.${ROOT})"

echo "== vhost rendered on LXC 100 =="
pct exec 100 -- sh -c "ls /etc/nginx/pickle.d/ | grep -c '$SUB' " >"$B" 2>/dev/null
[ "$(cat "$B")" -ge 1 ] 2>/dev/null && ok "vhost file present in pickle.d" || ko "vhost file missing"

echo "== origin chain: LXC100 :443 SNI → 8443 → subdomain vhost → VM:80 =="
# The Cloudflare→pve-node:443 leg is identical infra to opus (externally verified);
# from pve-node that leg can't be curled (hairpin NAT), so validate the NEW segment
# directly: hit LXC100 :443 with the real SNI, exercising ssl_preread → default
# 8443 → the rendered subdomain vhost → proxy_pass to the VM. TLS: the wildcard is
# a Cloudflare Origin CA cert, NOT in the public trust store (plain validation
# gives 000) — so pin the DEPLOYED cert's public key instead (-k disables chain
# checks but --pinnedpubkey still enforces the pin ⇒ a cert regression fails).
# Read the pair the agent is configured to serve for THIS root rather than
# repeating a filename convention here: the agent's map is what actually decides
# which certificate the vhost gets, so pinning anything else could pass while the
# served certificate is a different one.
CERTS_ENV=$(pct exec 100 -- sh -c "grep '^PICKLE_PROXY_AGENT_WILDCARD_CERTS=' /etc/pickle-proxy-agent/agent.env | cut -d= -f2-" 2>/dev/null)
# Split on the FIRST '=' only, like the agent's own parser does — awk -F= would
# truncate a path that itself contains '=' and pin the wrong file.
CERT_PATH=$(printf '%s' "$CERTS_ENV" | tr ',' '\n' \
  | sed -n "s/^[[:space:]]*${ROOT}=//p" | cut -d: -f1)
[ -n "$CERT_PATH" ] && ok "agent has a wildcard certificate for $ROOT" || ko "agent has no wildcard certificate configured for $ROOT"
PIN=$(pct exec 100 -- cat "$CERT_PATH" 2>/dev/null \
  | openssl x509 -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform der 2>/dev/null \
  | openssl dgst -sha256 -binary | base64)
if [ -n "$FQDN" ] && [ -n "$PIN" ]; then
  C=""
  for _ in $(seq 1 6); do
    C=$(curl -sS -o "$B" -w '%{http_code}' -k --pinnedpubkey "sha256//$PIN" --max-time 15 --resolve "$FQDN:443:198.18.1.10" "https://$FQDN/index.html" 2>/dev/null)
    [ "$C" = "200" ] && break; sleep 5; done
  [ "$C" = "200" ] && ok "origin HTTPS 200 via :443 SNI, cert pin OK ($FQDN)" || ko "origin HTTPS $C (or cert pin mismatch)"
  grep -q "PICKLE-HTTP-E2E-OK" "$B" && ok "served content = VM (PICKLE-HTTP-E2E-OK)" || ko "content mismatch ($(head -c 60 "$B"))"
else
  ko "origin HTTPS (no FQDN or no cert pin)"
  ko "served content (no FQDN or no cert pin)"
fi

echo "== teardown on delete: force-delete → vhost removed =="
req "sysadmin login" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$SYSADMIN_EMAIL\",\"password\":\"$SYSADMIN_PW\"}" || true
XAT=$(jq -r .accessToken "$B")
req "force-delete" 202 -X POST "$BASE/admin/vms/$VM/force-delete" -H "Authorization: Bearer $XAT" -H 'Content-Type: application/json' -d "{\"confirmName\":\"$VNAME\",\"reason\":\"http e2e cleanup\"}" || true
# poll until GET /vms/{id} 404s or status=DELETED (http code, not jq-on-error-body)
DL=$((SECONDS+180)); DC=""; DST=""
while :; do
  DC=$(curl -sS -o "$B" -w '%{http_code}' "$BASE/vms/$VM" -H "Authorization: Bearer $XAT" 2>/dev/null)
  DST=$(jq -r '.status // empty' "$B" 2>/dev/null)
  { [ "$DC" = "404" ] || [ "$DST" = "DELETED" ]; } && break
  [ "$SECONDS" -ge "$DL" ] && break; sleep 10; done
{ [ "$DC" = "404" ] || [ "$DST" = "DELETED" ]; } && ok "VM deleted (http=$DC${DST:+ status=$DST})" || ko "VM not deleted in 180s (http=$DC status=$DST)"
{ [ "$DC" = "404" ] || [ "$DST" = "DELETED" ]; } && VM_DELETED=1
# The API hides the VM as soon as the delete is accepted, but the teardown runs
# inside the async delete pipeline (teardown → destroy → mark deleted) — so poll
# the vhost's disappearance with a deadline instead of a single snapshot.
TDL=$((SECONDS+120)); VCNT="?"
while :; do
  VCNT=$(pct exec 100 -- sh -c "ls /etc/nginx/pickle.d/ | grep -c '$SUB'" 2>/dev/null)
  [ "$VCNT" = "0" ] && break
  [ "$SECONDS" -ge "$TDL" ] && break; sleep 5; done
[ "$VCNT" = "0" ] && ok "vhost removed after delete (teardown)" || ko "stale vhost remains after 120s (count=$VCNT)"
# re-check via the origin :443 SNI path (the external FQDN can't be curled from
# pve-node — hairpin NAT — so `!=200` there would be vacuously true). -k on purpose:
# a cert error must not mask a stale vhost still serving the VM content.
# NOTE: an HTTP 200 here is EXPECTED after teardown — with the vhost gone the SNI
# falls through to the default 8443 server (the console), which answers 200. The
# meaningful assertion is the VM marker's absence (+ the pickle.d count above).
if [ -n "$FQDN" ]; then
  : >"$B"
  C=$(curl -sS -o "$B" -w '%{http_code}' -k --max-time 15 --resolve "$FQDN:443:198.18.1.10" "https://$FQDN/index.html" 2>/dev/null)
  # Two outcomes are correct after teardown, and both have to be told apart from
  # a check that merely failed to reach anything.
  #
  # Since the reverse proxy gained a reject default server, the removed name is
  # an unknown SNI and the handshake is refused outright — curl reports 000 with
  # a TLS-level error. That is the strongest possible teardown result, so accept
  # it, but only when curl actually says the handshake was rejected: a network
  # failure also yields 000 and must not read as success.
  #
  # Otherwise a response must genuinely arrive before "the marker is absent"
  # means anything. An empty body or a connect failure used to satisfy that test
  # just as well and was reported as a successful teardown.
  # Captured, not piped: curl exits non-zero when it refuses, and under
  # `pipefail` that would sink the whole pipeline regardless of what grep found.
  TLS_ERR=$(curl -sS -o /dev/null -k --max-time 15 \
       --resolve "$FQDN:443:198.18.1.10" "https://$FQDN/index.html" 2>&1 || true)
  if [ "$C" = 000 ] && printf '%s' "$TLS_ERR" | grep -qiE 'ssl|tls|handshake'; then
    ok "origin refuses the removed name at the TLS handshake after teardown"
  elif ! [[ "$C" =~ ^[1-5][0-9][0-9]$ ]]; then
    ko "teardown origin check: no HTTP response and no TLS refusal (curl code $C)"
  elif [ ! -s "$B" ]; then
    ko "teardown origin check: HTTP $C with an empty body — nothing to assert on"
  elif grep -q "PICKLE-HTTP-E2E-OK" "$B"; then
    ko "origin still serves VM content after delete ($C)"
  else
    ok "origin answered $C without the VM marker after teardown"
  fi
else
  ko "teardown origin check (no FQDN)"
fi

echo; echo "HTTP-PUBLISH E2E: $P passed / $((P+F)) checks"
[ "$F" -eq 0 ] && exit 0 || exit 1
