#!/bin/bash
# shellcheck disable=SC2015,SC2086  # ok/ko idiom safe; $SSHKO/$SSHPO are intentional word-split option lists
# ==========================================================================
# SSH-gateway e2e — run on pve-node as root, AFTER api/console/sshgw deploy and
# the VM template build. Several checks
# assume the template's sudoers PASSWD override and the platform upstream
# key wiring (api.env PICKLE_SSH_PLATFORM_PUBLIC_KEY + sshgw upstream key).
#
# Provisions one real dev VM and drives the SSH-gateway launch-gate scenarios
# (per-user key identity, host-key pin, password opt-in, kill switches, route
# denials, client-IP preservation) through the Lightsail relay, exactly as a
# real off-campus client:
#   relay :22 (HAProxy send-proxy-v2) → WireGuard → sshgw proxyfront (PROXY v2)
#   → sshpiperd + route plugin v2 → user VM.
#
# Identity audit is verified on the **sshgw.session** row (actor_id = real
# user), the authenticated-session audit, NOT the route lookup (the route
# lookup runs on an unauthenticated offered key and is never a per-user
# record). Denials are verified on **sshgw.route_denied** rows scoped by this
# run's unique slug (+ the offered key fingerprint where it disambiguates).
#
# Force-deletes the VM and restores all mutated global/DB state on exit.
# ==========================================================================
set -uo pipefail
BASE="${BASE:-https://pickle.pusan.ac.kr/api/v1}"
# Signup requires consent to every current terms version (422 otherwise).
# Built once from the public endpoint so version bumps never break the smoke.
CONSENTS_JSON=$(curl -fsS "$BASE/meta/terms" 2>/dev/null | jq -c '[.[] | {docType, version}]' 2>/dev/null)
[ -n "$CONSENTS_JSON" ] || CONSENTS_JSON='[]'

RELAY="${RELAY:-198.51.100.10}"          # raw Lightsail IP (works pre/post DNS flip)
CTID="${CTID:-101}"                      # pickle-api LXC (DB + env live here)
TS=$(date +%s)-$RANDOM
B=$(mktemp)
declare -a TMPFILES=("$B")

seed_env(){ pct exec "$CTID" -- sh -c "grep '^$1=' /etc/pickle/api.env | cut -d= -f2-"; }
pgq(){ pct exec "$CTID" -- su - postgres -c "psql -d pickle_dev -tAc \"$1\"" 2>/dev/null | tr -d '[:space:]'; }
# psql -c travels through the shell `su -c` spawns, which re-parses the statement
# (a `$$` there would expand to that shell's PID). Feed it on stdin instead, and
# let a failing statement say so: the host-key pin case below only tests the pin
# if its UPDATE really landed.
pgx(){
  local out
  if ! out=$(pct exec "$CTID" -- su - postgres -c \
      "psql -q -d pickle_dev -v ON_ERROR_STOP=1 -f -" <<<"$1" 2>&1); then
    printf 'pgx failed: %s\n%s\n' "${1%%$'\n'*}" "$out" >&2
    return 1
  fi
}

ORGADMIN_EMAIL="orgadmin@pickle.local"; ORGADMIN_PW="$(seed_env PICKLE_SEED_ORGADMIN_PASSWORD)"
SYSADMIN_EMAIL="admin@pickle.local"; SYSADMIN_PW="$(seed_env PICKLE_SEED_SYSADMIN_PASSWORD)"

P=0; F=0; ok(){ echo "PASS  $1"; P=$((P+1)); }; ko(){ echo "FAIL  $1"; F=$((F+1)); }
req(){ local n="$1" e="$2"; shift 2; local c; c=$(curl -sS -o "$B" -w '%{http_code}' "$@"); [ "$c" = "$e" ] && { ok "$n ($c)"; return 0; } || { ko "$n (want $e got $c)"; head -c 300 "$B"; echo; return 1; }; }
# code_is EXPECTED NAME — asserts the Problem `code` of the last response, so a
# generic 403 (e.g. a missing sudo-mode token) can never satisfy a role-gate check.
code_is(){ local c; c=$(jq -r '.code // empty' "$B"); [ "$c" = "$1" ] && ok "$2 (code=$c)" || ko "$2 (code=${c:-none}, want $1)"; }
# Sudo-mode reauth: SSH-key, VM password/settings and group-member endpoints
# answer 403 REAUTH_REQUIRED without a fresh password proof (X-Reauth-Token).
# The token is per-account and multi-use for 10 minutes, and POST /auth/reverify
# is rate-limited per IP and per account, so cache it per access token instead
# of minting one per call; a password change invalidates it, but that also
# forces a re-login and therefore a new cache key here.
# The cache is FILE-backed on purpose: every call site invokes reauth from a
# command substitution (a subshell), so an in-memory array would be written in
# the subshell and thrown away — each protected call would then mint a fresh
# token and the per-IP reverify limit (every account here shares this host's
# egress IP) would start answering 429 with an empty token, which shows up as a
# spurious REAUTH_REQUIRED failure. Entries expire well inside the 10-minute
# server TTL, so a long run re-mints once instead of once per call.
RT_DIR=$(mktemp -d)
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
rt(){ echo "X-Reauth-Token: $(reauth "$1" "$2")"; }

SSHKO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -o PreferredAuthentications=publickey -o IdentitiesOnly=yes"
SSHPO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -o PreferredAuthentications=password -o PubkeyAuthentication=no"
kssh(){ ssh $SSHKO -i "$1" "$2@$RELAY" "$3" 2>&1; }               # $1=keyfile $2=slug $3=cmd
pssh(){ sshpass -p "$1" ssh $SSHPO "$2@$RELAY" "$3" 2>&1; }        # $1=pw $2=slug $3=cmd
# try_connect MARKER CONNECT-FN ARGS… — retry a *positive* connect until MARKER
# appears; the guest sshd can still be (re)started by cloud-init for a window
# after the VM reports RUNNING (connection refused, not an auth failure). Prints
# the last output; returns 0 on success, 1 on timeout. Denial scenarios assert on
# gateway audit rows instead and never reach the VM sshd, so they don't use this.
try_connect(){ local marker="$1"; shift; local out=""; for _ in $(seq 1 12); do out=$("$@"); echo "$out" | grep -q "$marker" && { printf '%s' "$out"; return 0; }; sleep 5; done; printf '%s' "$out"; return 1; }
denied(){ local q="select count(*) from audit_logs where action='sshgw.route_denied' and detail->>'slug'='$SLUG' and detail->>'reason'='$1'"; [ -n "${2:-}" ] && q="$q and detail->>'fingerprint'='$2'"; pgq "$q"; }
mklocalkey(){ ssh-keygen -q -t ed25519 -N '' -C '' -f "$1"; TMPFILES+=("$1" "$1.pub"); }
fp_of(){ ssh-keygen -lf "$1" | awk '{print $2}'; }

# mk_user EMAIL PW NAME → echoes "<accessToken> <userId>" (signup→verify→login).
mk_user(){
  curl -sS -o /dev/null -X POST "$BASE/auth/signup" -H 'Content-Type: application/json' -d "{\"email\":\"$1\",\"password\":\"$2\",\"name\":\"$3\",\"consents\":$CONSENTS_JSON}"
  sleep 2
  local tok; tok=$(pct exec "$CTID" -- sh -c "grep -o 'token=[A-Za-z0-9_-]*' /var/lib/pickle/mock-mail.log | tail -1 | cut -d= -f2")
  curl -sS -o /dev/null -X POST "$BASE/auth/verify-email" -H 'Content-Type: application/json' -d "{\"token\":\"$tok\"}"
  curl -sS -o "$B" -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$1\",\"password\":\"$2\"}"
  echo "$(jq -r .accessToken "$B") $(pgq "select id from users where email='$1'")"
}
# reg_key TOKEN PW NAME PUBKEYLINE → echoes keyId (paste-registration).
# The account's own password is needed for the sudo-mode token; without it the
# call 403s and the caller would silently receive an empty key id.
reg_key(){ curl -sS -o "$B" -X POST "$BASE/me/ssh-keys" -H "Authorization: Bearer $1" -H "$(rt "$1" "$2")" -H 'Content-Type: application/json' -d "$(jq -nc --arg n "$3" --arg k "$4" '{name:$n,publicKey:$k}')"; jq -r '.id // empty' "$B"; }
# addmember EMAIL ROLE (as group OWNER). Asserted (201): a silently failed add
# would make the membership-scoped checks below vacuous — a VIEWER/MEMBER that
# was never added is denied as a plain non-member and the test still "passes".
addmember(){ req "add member ($1)" 201 -X POST "$BASE/groups/$GID/members" -H "Authorization: Bearer $OAT" -H "$(rt "$OAT" "$OWNER_PW")" -H 'Content-Type: application/json' -d "{\"email\":\"$1\",\"role\":\"MEMBER\"}"; }
# addgrant USERID ROLE — put somebody on THIS VM's access list. Group membership
# admits nobody to a VM on its own; every rung below is granted per resource.
addgrant(){ req "grant $2 on the vm (user $1)" 201 -X POST "$BASE/vms/$VM/access" -H "Authorization: Bearer $OAT" -H "$(rt "$OAT" "$OWNER_PW")" -H 'Content-Type: application/json' -d "{\"granteeType\":\"USER\",\"userId\":$1,\"role\":\"$2\"}"; }

# ---- state to restore on exit ----
VM=""; VNAME=""; VM_DELETED=0; ORIG_HK_B64=""; ORIG_KILL=""
cleanup(){
  local rc=$?
  # ssh_host_key is multi-line (one entry per host-key type); back it up/restore
  # it as base64 so whitespace survives (pgq's tr -d space would corrupt it).
  [ -n "$ORIG_HK_B64" ] && [ -n "$VM" ] && pgx "update vms set ssh_host_key=convert_from(decode('$ORIG_HK_B64','base64'),'UTF8') where id=$VM"
  [ -n "$ORIG_KILL" ] && pgx "update settings set value='$ORIG_KILL'::jsonb where key='ssh_gateway_enabled'"
  if [ -n "$VM" ] && [ "$VM_DELETED" != 1 ]; then
    echo "-- cleanup: force-deleting leftover VM $VM --"
    local at; at=$(curl -sS -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$SYSADMIN_EMAIL\",\"password\":\"$SYSADMIN_PW\"}" | jq -r '.accessToken // empty')
    # The warning used to hang off `||` at the end of an && chain, so it could
    # only fire when the token or the name was missing: curl itself exits 0 on a
    # 403 or a 500, and the rejection printed nothing at all.
    if [ -n "$at" ] && [ -n "$VNAME" ]; then
      local dc
      dc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/admin/vms/$VM/force-delete" \
        -H "Authorization: Bearer $at" -H 'Content-Type: application/json' \
        -d "{\"confirmName\":\"$VNAME\",\"reason\":\"smoke cleanup (trap)\"}") || dc=000
      if [ "$dc" = 202 ]; then
        echo "-- cleanup: force-delete accepted (202) --"
      else
        echo "-- cleanup: force-delete REJECTED (http=${dc:-none}); manual cleanup needed (vm id $VM) --" >&2
      fi
    else
      echo "-- cleanup: no admin token or VM name; manual cleanup needed (vm id $VM) --" >&2
    fi
  fi
  rm -f "${TMPFILES[@]}"; rm -rf "$RT_DIR"
  exit "$rc"
}
trap cleanup EXIT

# ==========================================================================
echo "== [0] advertised SSH hostname resolves to the relay =="
# The scenarios below connect to the relay by raw IP so they work either side of a
# DNS flip. That deliberately leaves the name users are actually told to type
# untested: a missing or stale A record would not fail a single check here. This
# asserts the advertised host — the value the API hands to the console — points at
# the relay this run just exercised, and that something answers SSH on it.
ADV_HOST=$(seed_env PICKLE_SSH_HOST)
if [ -z "$ADV_HOST" ]; then
  ko "advertised SSH host is NOT set in api.env (PICKLE_SSH_HOST)"
else
  ok "advertised SSH host = $ADV_HOST"
  ADV_IPS=$(getent ahostsv4 "$ADV_HOST" 2>/dev/null | awk '{print $1}' | sort -u)
  if echo "$ADV_IPS" | grep -qx "$RELAY"; then
    ok "$ADV_HOST resolves to the relay ($RELAY)"
  else
    ko "$ADV_HOST resolves to [${ADV_IPS:-nothing}], expected $RELAY"
  fi
  # Capture the banner, then test the string. Piping nc into `head -c` and
  # testing the pipeline instead would report FAIL on a healthy relay: head
  # exits at its byte count, nc keeps the connection until timeout kills it
  # (124), and pipefail makes that the pipeline's status regardless of the match.
  BANNER=$(timeout 5 nc "$ADV_HOST" 22 2>/dev/null | head -c 64 || true)
  case "$BANNER" in
    SSH-*) ok "$ADV_HOST:22 answers with an SSH banner" ;;
    *)     ko "$ADV_HOST:22 did not answer with an SSH banner" ;;
  esac
fi

echo "== provision (owner O creates group + VM) =="
OWNER_EMAIL="sgw-owner-${TS}@pusan.ac.kr"; OWNER_PW="sgw-pass-${TS}!"
read -r OAT OUID < <(mk_user "$OWNER_EMAIL" "$OWNER_PW" "SGW Owner")
[ -n "$OAT" ] && [ -n "$OUID" ] && ok "owner user id=$OUID" || { ko "owner signup"; exit 1; }
req "group" 201 -X POST "$BASE/groups" -H "Authorization: Bearer $OAT" -H 'Content-Type: application/json' -d "{\"name\":\"sgw\",\"slug\":\"sgwteam-${TS}\",\"kind\":\"TEAM\"}" || exit 1
GID=$(jq -r .id "$B")
# the seed org is hidden and GET /orgs filters hidden orgs for USER tokens — list as orgadmin
req "orgadmin login" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$ORGADMIN_EMAIL\",\"password\":\"$ORGADMIN_PW\"}" || exit 1
AAT=$(jq -r .accessToken "$B")
req "orgs" 200 "$BASE/orgs" -H "Authorization: Bearer $AAT" || exit 1; OID=$(jq -r '.[0].id' "$B")
req "os-images" 200 "$BASE/os-images" -H "Authorization: Bearer $OAT" || exit 1
TID=$(jq -r '.[0].id // empty' "$B")
# An empty catalog — the state the bootstrap runbook leaves behind, rows
# registered but none enabled — would otherwise reach the request payload as
# "imageId":, which is not JSON, and surface as a bare 400 saying nothing
# about the catalog.
[ -n "$TID" ] || { ko "no ACTIVE OS image to request with (enable one in the catalog)"; exit 1; }
# os-images is the OS catalog; the spec axis is vm-flavors and POST
# /vm-requests requires the chosen flavorId ('basic', else the first ACTIVE row)
req "vm-flavors" 200 "$BASE/vm-flavors" -H "Authorization: Bearer $OAT" || exit 1
FSEL='(map(select(.name=="basic"))[0] // .[0])'
FID=$(jq -r "$FSEL.id // empty" "$B"); VC=$(jq -r "$FSEL.vcpu // empty" "$B"); MM=$(jq -r "$FSEL.memoryMb // empty" "$B"); DG=$(jq -r "$FSEL.diskGb // empty" "$B")
[ -n "$FID" ] && ok "flavor id=$FID (${VC}c/${MM}MB/${DG}GB)" || { ko "no ACTIVE vm-flavor"; exit 1; }
req "request" 201 -X POST "$BASE/vm-requests" -H "Authorization: Bearer $OAT" -H 'Content-Type: application/json' -d "{\"groupId\":$GID,\"orgId\":$OID,\"imageId\":$TID,\"flavorId\":$FID,\"purpose\":\"ssh gateway e2e\",\"courseOrProject\":null,\"specReason\":null,\"extraNote\":null,\"reqVcpu\":$VC,\"reqMemoryMb\":$MM,\"reqDiskGb\":$DG,\"reqStartDate\":null,\"reqEndDate\":null}" || exit 1
RID=$(jq -r .id "$B")
req "approve" 200 -X POST "$BASE/admin/vm-requests/$RID/approve" -H "Authorization: Bearer $AAT" -H 'Content-Type: application/json' -d "{\"grantedVcpu\":$VC,\"grantedMemoryMb\":$MM,\"grantedDiskGb\":$DG,\"grantedImageId\":$TID,\"grantedStartDate\":null,\"grantedEndDate\":null,\"nodeId\":null,\"comment\":\"sgw\"}" || exit 1
req "vm list" 200 "$BASE/vms?groupId=$GID" -H "Authorization: Bearer $OAT" || exit 1
VM=$(jq -r '.content[0].id // empty' "$B"); VNAME=$(jq -r '.content[0].name // empty' "$B")
[ -n "$VM" ] && ok "vm id=$VM name=$VNAME" || { ko "vm id (empty list)"; exit 1; }

echo "== poll RUNNING =="
DL=$((SECONDS+900)); ST=""
while :; do curl -sS -o "$B" "$BASE/vms/$VM" -H "Authorization: Bearer $OAT"; ST=$(jq -r .status "$B"); VIP=$(jq -r '.ipAddress // empty' "$B")
  [ "$ST" = "RUNNING" ] && break; { [ "$ST" = "ERROR" ] || [ "$ST" = "NEEDS_ADMIN" ]; } && { ko "provision $ST"; break; }
  [ "$SECONDS" -ge "$DL" ] && { ko "not RUNNING (last=$ST)"; break; }; sleep 10; done
[ "$ST" = "RUNNING" ] && ok "VM RUNNING ip=$VIP" || exit 1
SLUG=$(pgq "select hostname from vms where id=$VM")
[ -n "$SLUG" ] && ok "slug(hostname)=$SLUG" || { ko "slug"; exit 1; }
# base64 so the multi-line value round-trips intact (pgq strips whitespace).
ORIG_HK_B64=$(pgq "select encode(convert_to(ssh_host_key,'UTF8'),'base64') from vms where id=$VM")
[ -n "$ORIG_HK_B64" ] && ok "host key collected at provisioning" || ko "no vms.ssh_host_key (HOSTKEY step?)"
for _ in $(seq 1 18); do nc -z -w5 "$VIP" 22 2>/dev/null && break; sleep 5; done

# --- 1. server-side key generate + private-key download ---
echo "== [1] create key (server-side generate) =="
req "generate key" 201 -X POST "$BASE/me/ssh-keys/generate" -H "Authorization: Bearer $OAT" -H "$(rt "$OAT" "$OWNER_PW")" -H 'Content-Type: application/json' -d '{"name":"pickle-smoke"}' || exit 1
KID=$(jq -r .id "$B"); OFPR=$(jq -r .fingerprint "$B")
[ "$(jq -r .privateKeyStored "$B")" = true ] && ok "generated key privateKeyStored=true fp=$OFPR" || ko "privateKeyStored not true"
req "download private key" 200 "$BASE/me/ssh-keys/$KID/private-key" -H "Authorization: Bearer $OAT" -H "$(rt "$OAT" "$OWNER_PW")" || exit 1
OKEY=$(mktemp); TMPFILES+=("$OKEY"); jq -r .privateKey "$B" > "$OKEY"; chmod 600 "$OKEY"

# --- 2. connect with the key ---
echo "== [2] publickey SSH via relay =="
OUT=$(kssh "$OKEY" "$SLUG" 'echo PICKLE-SGW-OK; id -un')
echo "$OUT" | grep -q PICKLE-SGW-OK && ok "publickey SSH reached VM shell" || ko "publickey SSH failed ($(echo "$OUT" | tr '\n' ' ' | head -c 80))"

# --- 3. audit actor = real user (sshgw.session, NOT route) ---
echo "== [3] audit: sshgw.session actor_id = real user =="
sleep 1
ACT=$(pgq "select actor_id from audit_logs where action='sshgw.session' and detail->>'slug'='$SLUG' order by id desc limit 1")
[ "$ACT" = "$OUID" ] && ok "sshgw.session actor_id=$ACT is the real user" || ko "session actor_id=$ACT want $OUID"

# --- session audit: real client IP preserved end-to-end (not tunnel/gw addr) ---
echo "== session audit: client IP preserved =="
AIP=$(pgq "select ip from audit_logs where action='sshgw.session' and detail->>'slug'='$SLUG' order by id desc limit 1")
[ -n "$AIP" ] && ok "sshgw.session ip=$AIP (slug=$SLUG)" || ko "no sshgw.session audit for slug=$SLUG"
case "$AIP" in 100.64.0.*|198.18.*|"") ko "audit ip is tunnel/gateway not real client ($AIP)";; *) ok "audit ip is a real client IP ($AIP)";; esac

# --- 7. host-key mismatch → refuse (pin enforced; run while O's key is still valid) ---
echo "== [7] host-key mismatch → session refused =="
# mktemp -u (a path, not a file) like every other keygen here: ssh-keygen must
# create the file itself, and an existing path stops it on an interactive
# "Overwrite (y/n)?" prompt. That left BOGUS_PUB empty, so the pin became '' and
# the route was refused one gate earlier (no collected host key) — the check
# passed without ever exercising the pin.
BOGUS=$(mktemp -u); mklocalkey "$BOGUS"; BOGUS_PUB=$(cat "$BOGUS.pub")
[ -n "$BOGUS_PUB" ] && ok "bogus host key generated (pin will differ, not be empty)" || ko "bogus host key empty — [7] would not test the pin"
AID=$(pgq "select coalesce(max(id),0) from audit_logs")
# The gateway logs one warning per refused upstream verify. Counting that line
# before and after is the only positive evidence that the pin is what refused:
# every other reason the SSH could die (relay down, wg handshake gone, sshpiperd
# stopped) leaves the count unchanged while still producing no SHOULDNOTREACH.
hk_mismatch_count(){ pct exec 102 -- sh -c \
  "journalctl -u sshpiperd --no-pager 2>/dev/null | grep -c 'upstream host key mismatch'" \
  2>/dev/null | tr -d '[:space:]'; }
HKM_BEFORE=$(hk_mismatch_count)
pgx "update vms set ssh_host_key='$BOGUS_PUB' where id=$VM"
PINNED=$(pgq "select count(*) from vms where id=$VM and ssh_host_key='$BOGUS_PUB'")
[ "${PINNED:-0}" = 1 ] && ok "bogus host key stored on the vm row" || ko "bogus host key not stored — the pin was never swapped"
OUT=$(kssh "$OKEY" "$SLUG" 'echo SHOULDNOTREACH')
if echo "$OUT" | grep -q SHOULDNOTREACH; then ko "mismatched host key still connected (pin not enforced)"
else
  sleep 1
  # The refusal has to come from the host-key pin itself. A *different* key still
  # routes (route lookups that pass every gate are granted and not audited) and
  # dies at the gateway's upstream verify, so: no new route_denied row for this
  # slug, and no sshgw.session row (the session never establishes). A route_denied
  # here would mean an earlier gate refused instead — exactly what an empty pin did.
  DENR=$(pgq "select coalesce(string_agg(distinct detail->>'reason',','),'') from audit_logs where id>$AID and action='sshgw.route_denied' and detail->>'slug'='$SLUG'")
  [ -z "$DENR" ] && ok "route granted, refused at the host-key pin (no route_denied)" || ko "refused before the pin (route_denied reason=$DENR)"
  SESS=$(pgq "select count(*) from audit_logs where id>$AID and action='sshgw.session' and detail->>'slug'='$SLUG'")
  [ "${SESS:-0}" = "0" ] && ok "host-key mismatch refused the session (no sshgw.session)" || ko "sshgw.session recorded despite a mismatched pin"
  # Without this the two checks above are also satisfied by an SSH that never
  # reached the upstream verify at all. The gateway's own warning is the proof
  # that the refusal happened at the pin.
  HKM_AFTER=$(hk_mismatch_count)
  [ "${HKM_AFTER:-0}" -gt "${HKM_BEFORE:-0}" ] 2>/dev/null \
    && ok "gateway logged an upstream host-key mismatch (${HKM_BEFORE} → ${HKM_AFTER})" \
    || { ko "no new upstream host-key mismatch in the gateway log (${HKM_BEFORE} → ${HKM_AFTER}) — the SSH died before the pin"; echo "$OUT" | head -c 300; echo; }
fi
pgx "update vms set ssh_host_key=convert_from(decode('$ORIG_HK_B64','base64'),'UTF8') where id=$VM"
RESTORED=$(pgq "select count(*) from vms where id=$VM and ssh_host_key is not null and ssh_host_key<>'$BOGUS_PUB'")
[ "${RESTORED:-0}" = 1 ] && ok "original host key restored on the vm row" || ko "host key not restored — later cases run against a bogus pin"

# --- 4. unregistered key → SSHGW_KEY_UNKNOWN ---
echo "== [4] unregistered key → deny =="
UNREG=$(mktemp -u); mklocalkey "$UNREG"; UNREG_FP=$(fp_of "$UNREG.pub")
kssh "$UNREG" "$SLUG" 'echo X' | grep -q '^X$' && ko "unregistered key routed" || { sleep 1; [ "$(denied SSHGW_KEY_UNKNOWN "$UNREG_FP")" -ge 1 ] 2>/dev/null && ok "unregistered key denied (SSHGW_KEY_UNKNOWN)" || ko "no SSHGW_KEY_UNKNOWN audit for unreg fp"; }

# --- 5. non-member's registered key → SSHGW_KEY_NOT_MEMBER ---
echo "== [5] non-member key → deny =="
NM_PW="nm-pw-${TS}!"
read -r NMAT _ < <(mk_user "sgw-nm-${TS}@pusan.ac.kr" "$NM_PW" "SGW NonMember")
NMKEY=$(mktemp -u); mklocalkey "$NMKEY"; NM_FP=$(fp_of "$NMKEY.pub")
reg_key "$NMAT" "$NM_PW" "nm-key" "$(cat "$NMKEY.pub")" >/dev/null
kssh "$NMKEY" "$SLUG" 'echo X' | grep -q '^X$' && ko "non-member routed" || { sleep 1; [ "$(denied SSHGW_KEY_NOT_MEMBER "$NM_FP")" -ge 1 ] 2>/dev/null && ok "non-member denied (SSHGW_KEY_NOT_MEMBER)" || ko "no SSHGW_KEY_NOT_MEMBER audit for non-member"; }

# --- 6. in the group, not on the VM's list → SSHGW_KEY_NOT_MEMBER ---
# The gateway asks the access list, not the group. Somebody the owner invited to
# the group but never added to this VM is refused with the same code as a
# stranger, so the refusal leaks nothing about who is a colleague.
echo "== [6] group member absent from the access list → deny =="
VW_PW="vw-pw-${TS}!"
read -r VWAT VW_ID < <(mk_user "sgw-unlisted-${TS}@pusan.ac.kr" "$VW_PW" "SGW Unlisted")
addmember "sgw-unlisted-${TS}@pusan.ac.kr"
VWKEY=$(mktemp -u); mklocalkey "$VWKEY"; VW_FP=$(fp_of "$VWKEY.pub")
reg_key "$VWAT" "$VW_PW" "vw-key" "$(cat "$VWKEY.pub")" >/dev/null
kssh "$VWKEY" "$SLUG" 'echo X' | grep -q '^X$' && ko "unlisted member routed" || { sleep 1; [ "$(denied SSHGW_KEY_NOT_MEMBER "$VW_FP")" -ge 1 ] 2>/dev/null && ok "unlisted member denied (SSHGW_KEY_NOT_MEMBER)" || ko "no SSHGW_KEY_NOT_MEMBER audit for the unlisted member"; }

# The same person, once listed, reaches the shell — otherwise the denial above
# would also pass if the gateway were simply broken for everyone but the owner.
echo "== [7] the same key after the owner adds them to the list =="
addgrant "$VW_ID" MEMBER
sleep 1
try_connect PICKLE-LISTED kssh "$VWKEY" "$SLUG" 'echo PICKLE-LISTED' >/dev/null \
  && ok "listed member routed to the VM" || ko "listed member still refused"

# --- 8. password default-deny (ssh_password_enabled=false) → SSHGW_PASSWORD_DISABLED ---
echo "== [8] password default-deny =="
req "reveal password" 200 "$BASE/vms/$VM/password" -H "Authorization: Bearer $OAT" -H "$(rt "$OAT" "$OWNER_PW")" || exit 1
VMPW=$(jq -r .password "$B")
pssh "$VMPW" "$SLUG" 'echo X' | grep -q '^X$' && ko "password worked while disabled" || { sleep 1; [ "$(denied SSHGW_PASSWORD_DISABLED)" -ge 1 ] 2>/dev/null && ok "password denied by default (SSHGW_PASSWORD_DISABLED)" || ko "no SSHGW_PASSWORD_DISABLED audit"; }

# --- 9. opt-in password enable → password SSH allowed ---
echo "== [9] opt-in password enable → allowed =="
req "enable ssh_password" 200 -X PATCH "$BASE/vms/$VM/settings" -H "Authorization: Bearer $OAT" -H "$(rt "$OAT" "$OWNER_PW")" -H 'Content-Type: application/json' -d '{"settings":{"ssh_password_enabled":true}}' || ko "enable settings"
sleep 1
try_connect PICKLE-PW-OK pssh "$VMPW" "$SLUG" 'echo PICKLE-PW-OK' >/dev/null && ok "password SSH allowed after opt-in" || ko "password SSH failed after opt-in"

# --- 10. MEMBER cannot change VM settings → 403 ---
echo "== [10] MEMBER PATCH settings → 403 =="
MB_PW="mb-pw-${TS}!"
read -r MBAT MB_ID < <(mk_user "sgw-member-${TS}@pusan.ac.kr" "$MB_PW" "SGW Member")
addmember "sgw-member-${TS}@pusan.ac.kr"
# Listed at the rung that carries access but not editing. Granting first is what
# makes this a test of the rung: an unlisted person is refused one step earlier,
# and the check would pass without the settings gate ever being consulted.
addgrant "$MB_ID" MEMBER
# Hand the MEMBER a VALID sudo-mode token on purpose: without one the endpoint
# 403s on REAUTH_REQUIRED and this check would pass without ever reaching the
# role gate. The Problem code is asserted for the same reason.
req "member settings forbidden" 403 -X PATCH "$BASE/vms/$VM/settings" -H "Authorization: Bearer $MBAT" -H "$(rt "$MBAT" "$MB_PW")" -H 'Content-Type: application/json' -d '{"settings":{"ssh_password_enabled":false}}'
code_is GROUP_ROLE_INSUFFICIENT "  refused by the role gate, not by reauth"

# --- 12. EDITOR cannot raise password_reveal_min_role (OWNER-gated) → 403 ---
echo "== [12] EDITOR raise min_role → 403 =="
ED_PW="ed-pw-${TS}!"
read -r EDAT ED_ID < <(mk_user "sgw-editor-${TS}@pusan.ac.kr" "$ED_PW" "SGW Editor")
addmember "sgw-editor-${TS}@pusan.ac.kr"
addgrant "$ED_ID" EDITOR
# valid sudo-mode token here too — the OWNER-only key is what must refuse
req "editor min_role forbidden" 403 -X PATCH "$BASE/vms/$VM/settings" -H "Authorization: Bearer $EDAT" -H "$(rt "$EDAT" "$ED_PW")" -H 'Content-Type: application/json' -d '{"settings":{"password_reveal_min_role":"EDITOR"}}'
code_is GROUP_ROLE_INSUFFICIENT "  refused by the role gate, not by reauth"

# --- 13. sudo demands a password inside the VM (sudoers PASSWD override) ---
echo "== [13] sudo -n fails in guest (NOPASSWD overridden) =="
# A marker proves the shell was reached before judging the sudo result, so a
# connection blip can't masquerade as "sudo refused" (a false pass).
OUT=$(try_connect PICKLE-SUDO pssh "$VMPW" "$SLUG" 'echo PICKLE-SUDO; sudo -n true >/dev/null 2>&1; echo RC=$?')
if ! echo "$OUT" | grep -q PICKLE-SUDO; then ko "[13] could not reach VM shell to test sudo"
elif echo "$OUT" | grep -q 'RC=0'; then ko "sudo -n succeeded — zz-pickle PASSWD override missing (template rebuilt?)"
else ok "sudo requires a password (sudo -n refused)"; fi

# --- 15. password regenerate → new password, audited, old fails ---
echo "== [15] password regenerate =="
req "regenerate password" 200 -X POST "$BASE/vms/$VM/password/regenerate" -H "Authorization: Bearer $OAT" -H "$(rt "$OAT" "$OWNER_PW")" || ko "regenerate"
NEWPW=$(jq -r .password "$B")
[ -n "$NEWPW" ] && [ "$NEWPW" != "$VMPW" ] && ok "password changed on regenerate" || ko "regenerate did not change password"
sleep 1
[ "$(pgq "select count(*) from audit_logs where action='vm.password_regenerate' and target_id=$VM")" -ge 1 ] 2>/dev/null && ok "vm.password_regenerate audited" || ko "no vm.password_regenerate audit"
try_connect PICKLE-NEWPW-OK pssh "$NEWPW" "$SLUG" 'echo PICKLE-NEWPW-OK' >/dev/null && ok "new password works" || ko "new password failed"
# sshd is confirmed up by the line above, so a refused old password here is a
# genuine auth rejection, not a not-ready blip.
pssh "$VMPW" "$SLUG" 'echo X' | grep -q '^X$' && ko "old password still works after regenerate" || ok "old password rejected"
VMPW="$NEWPW"

# --- 11. delete key → immediate deny (same key that worked in [2]) ---
echo "== [11] delete key → immediate deny =="
req "delete key" 204 -X DELETE "$BASE/me/ssh-keys/$KID" -H "Authorization: Bearer $OAT" -H "$(rt "$OAT" "$OWNER_PW")" || ko "delete key"
kssh "$OKEY" "$SLUG" 'echo X' | grep -q '^X$' && ko "deleted key still routed" || { sleep 1; [ "$(denied SSHGW_KEY_UNKNOWN "$OFPR")" -ge 1 ] 2>/dev/null && ok "deleted key denied immediately (SSHGW_KEY_UNKNOWN)" || ko "no SSHGW_KEY_UNKNOWN audit for the deleted key fp"; }

# --- per-VM gateway block → deny, with its own audit reason ---
echo "== per-VM gateway block → deny =="
pgx "update vms set ssh_gateway_blocked=true where id=$VM"
sleep 1
pssh "$VMPW" "$SLUG" 'echo X' | grep -q '^X$' && ko "blocked VM still reachable" || { sleep 1; [ "$(denied SSHGW_VM_BLOCKED)" -ge 1 ] 2>/dev/null && ok "per-VM block denies SSH (SSHGW_VM_BLOCKED)" || ko "no SSHGW_VM_BLOCKED audit"; }
pgx "update vms set ssh_gateway_blocked=false where id=$VM"

# --- unknown slug → deny (unique per run so the audit query is unambiguous) ---
echo "== unknown slug → deny =="
BADSLUG="no-such-slug-${TS}"
pssh x "$BADSLUG" 'echo X' | grep -q '^X$' && ko "unknown slug routed somewhere" || { sleep 1; DEN=$(pgq "select count(*) from audit_logs where action='sshgw.route_denied' and detail->>'slug'='$BADSLUG' and detail->>'reason'='SSHGW_ROUTE_NOT_FOUND'"); [ "${DEN:-0}" -ge 1 ] 2>/dev/null && ok "unknown slug denied (SSHGW_ROUTE_NOT_FOUND)" || ko "no SSHGW_ROUTE_NOT_FOUND audit for $BADSLUG"; }

# --- 14. global kill switch → deny (checked first, reveals nothing) ---
echo "== [14] global kill switch → deny =="
ORIG_KILL=$(pgq "select value from settings where key='ssh_gateway_enabled'")
pgx "update settings set value='false'::jsonb where key='ssh_gateway_enabled'"
sleep 1
pssh "$VMPW" "$SLUG" 'echo X' | grep -q '^X$' && ko "kill switch off but still reachable" || { sleep 1; [ "$(denied SSHGW_GATEWAY_DISABLED)" -ge 1 ] 2>/dev/null && ok "kill switch denies SSH (SSHGW_GATEWAY_DISABLED)" || ko "no SSHGW_GATEWAY_DISABLED audit"; }
pgx "update settings set value='$ORIG_KILL'::jsonb where key='ssh_gateway_enabled'"; ORIG_KILL=""

# --- cleanup: force-delete the VM ---
echo "== cleanup: force-delete VM =="
req "sysadmin login" 200 -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$SYSADMIN_EMAIL\",\"password\":\"$SYSADMIN_PW\"}" || true
XAT=$(jq -r .accessToken "$B")
req "force-delete" 202 -X POST "$BASE/admin/vms/$VM/force-delete" -H "Authorization: Bearer $XAT" -H 'Content-Type: application/json' -d "{\"confirmName\":\"$VNAME\",\"reason\":\"ssh gateway e2e\"}" || true
DL=$((SECONDS+180)); DC=""; DST=""
while :; do
  DC=$(curl -sS -o "$B" -w '%{http_code}' "$BASE/vms/$VM" -H "Authorization: Bearer $XAT" 2>/dev/null)
  DST=$(jq -r '.status // empty' "$B" 2>/dev/null)
  { [ "$DC" = "404" ] || [ "$DST" = "DELETED" ]; } && break
  [ "$SECONDS" -ge "$DL" ] && break; sleep 10; done
{ [ "$DC" = "404" ] || [ "$DST" = "DELETED" ]; } && { ok "VM deleted (http=$DC${DST:+ status=$DST})"; VM_DELETED=1; } || ko "VM not deleted in 180s (http=$DC status=$DST)"

echo; echo "SSH-GATEWAY E2E: $P passed / $((P+F)) checks"
[ "$F" -eq 0 ] && exit 0 || exit 1
